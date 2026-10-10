"""Krypterade Wiki.js-JWT:er för namngivna identiteter.

Wiki.js 2.5.315: en användar-JWT gäller 30 minuter och kan förnyas inom 14 dagar.
Servern skickar den nya JWT:n i headern new-jwt när Content-Type är
application/json (server/core/auth.js). authorId sätts från den
inloggade användaren (server/models/pages.js). Tokenet skrivs aldrig i loggen.
"""
from __future__ import annotations

import json
import logging
import os
import re
import stat
import tempfile
import threading
from datetime import datetime, timezone
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from .wiki_client import WikiClient, WikiError, wiki_bearer, wiki_on_new_jwt

log = logging.getLogger("wiki-mcp")

IDENTITY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_GUEST_EMAILS = frozenset({"guest@example.com", "api@localhost"})
RENEW_INTERVAL_SECONDS = 12 * 60 * 60


class IdentityError(Exception):
    pass


class IdentityDenied(Exception):
    def __init__(self, name: str):
        super().__init__(name)
        self.name = name


def identity_name_ok(name: str) -> bool:
    return bool(IDENTITY_NAME.fullmatch(name))


def profile_ok(profile: dict[str, Any] | None) -> bool:
    if not isinstance(profile, dict):
        return False
    try:
        user_id = int(profile.get("id") or 0)
    except (TypeError, ValueError):
        return False
    email = str(profile.get("email") or "").strip().lower()
    name = str(profile.get("name") or "").strip()
    if user_id <= 0 or not name or not email:
        return False
    if email in _GUEST_EMAILS:
        return False
    return True


def is_identity_auth_failure(exc: BaseException) -> bool:
    if not isinstance(exc, WikiError):
        return False
    details = exc.details
    if isinstance(details, dict) and details.get("status") in (401, 403):
        return True
    if isinstance(details, str):
        blob = details
    else:
        blob = json.dumps(details, ensure_ascii=False)
    low = blob.lower()
    return "unauthorized" in low or "forbidden" in low


class IdentityStore:
    def __init__(self, directory: str, key_file: str, wiki: WikiClient | None = None):
        self.directory = directory
        self.key_file = key_file
        self.wiki = wiki
        self._lock = threading.Lock()
        self._fernet: Fernet | None = None
        self._states: dict[str, str] = {}
        self._load_key()

    def _load_key(self) -> None:
        if not self.key_file or not os.path.isfile(self.key_file):
            log.error("IDENTITY_KEY_FILE saknas, wiki-identiteter är avstängda")
            return
        st = os.stat(self.key_file)
        mode = stat.S_IMODE(st.st_mode)
        if mode & 0o077:
            log.warning("IDENTITY_KEY_FILE har mode %o, ska vara 600: %s", mode, self.key_file)
        with open(self.key_file, "rb") as handle:
            raw = handle.read().strip()
        try:
            self._fernet = Fernet(raw)
        except (ValueError, TypeError):
            self._fernet = None
            log.error("IDENTITY_KEY_FILE är ogiltig, wiki-identiteter är avstängda")

    def _path(self, name: str) -> str:
        if not identity_name_ok(name):
            raise IdentityError("ogiltigt identitetsnamn")
        return os.path.join(self.directory, name + ".json")

    def _read(self, name: str) -> dict[str, Any] | None:
        path = self._path(name)
        if not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise IdentityError("identitetsfilen är ogiltig")
        return data

    def _write(self, name: str, record: dict[str, Any]) -> None:
        os.makedirs(self.directory, exist_ok=True)
        try:
            os.chmod(self.directory, 0o700)
        except OSError:
            pass
        payload = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
        fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=".identity-", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._path(name))
            os.chmod(self._path(name), 0o600)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _require_fernet(self) -> Fernet:
        if self._fernet is None:
            raise IdentityError("IDENTITY_KEY_FILE saknas eller är ogiltig")
        return self._fernet

    def names(self) -> list[str]:
        if not os.path.isdir(self.directory):
            return []
        found: list[str] = []
        for entry in sorted(os.listdir(self.directory)):
            if entry.endswith(".json") and identity_name_ok(entry[: -len(".json")]):
                found.append(entry[: -len(".json")])
        return found

    def status(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for name in self.names():
            try:
                record = self._read(name) or {}
            except Exception:
                record = {}
            state = self._states.get(name, "ok")
            out[name] = {
                "state": state,
                "user_id": record.get("user_id"),
                "name": record.get("user_name") or "",
                "email": record.get("email") or "",
            }
        return out

    def state(self, name: str) -> str:
        return self._states.get(name, "ok" if os.path.isfile(self._path(name)) else "expired")

    def mark_ok(self, name: str, profile: dict[str, Any] | None = None) -> None:
        self._states[name] = "ok"
        if profile and profile_ok(profile):
            self._update_profile(name, profile)

    def mark_expired(self, name: str) -> None:
        self._states[name] = "expired"

    def get_token(self, name: str) -> str | None:
        fernet = self._fernet
        if fernet is None:
            return None
        try:
            record = self._read(name)
        except Exception:
            log.error("wiki-identitet kunde inte läsas identity=%s", name)
            self.mark_expired(name)
            return None
        if record is None:
            return None
        blob = record.get("token")
        if not isinstance(blob, str) or not blob:
            self.mark_expired(name)
            return None
        try:
            return fernet.decrypt(blob.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError):
            log.error("wiki-identitet kunde inte dekrypteras identity=%s", name)
            self.mark_expired(name)
            return None

    def store_jwt(self, name: str, jwt: str) -> None:
        """Byt bara ut tokenet. Profilfält som redan finns ligger kvar."""
        if not jwt or not jwt.strip():
            raise IdentityError("tom JWT")
        fernet = self._require_fernet()
        with self._lock:
            record = self._read(name) or {"identity": name}
            record["identity"] = name
            record["token"] = fernet.encrypt(jwt.strip().encode("utf-8")).decode("ascii")
            record["updated"] = _now()
            self._write(name, record)

    def save_validated(self, name: str, jwt: str, profile: dict[str, Any]) -> None:
        if not identity_name_ok(name):
            raise IdentityError("ogiltigt identitetsnamn")
        if not profile_ok(profile):
            raise IdentityError("tokenet gav inte en riktig användare")
        if not jwt or not jwt.strip():
            raise IdentityError("tom JWT")
        fernet = self._require_fernet()
        record = {
            "identity": name,
            "user_id": int(profile["id"]),
            "user_name": str(profile["name"]).strip(),
            "email": str(profile["email"]).strip(),
            "token": fernet.encrypt(jwt.strip().encode("utf-8")).decode("ascii"),
            "updated": _now(),
        }
        with self._lock:
            self._write(name, record)
        self.mark_ok(name)

    def _update_profile(self, name: str, profile: dict[str, Any]) -> None:
        with self._lock:
            record = self._read(name)
            if record is None:
                return
            record["user_id"] = int(profile["id"])
            record["user_name"] = str(profile["name"]).strip()
            record["email"] = str(profile["email"]).strip()
            record["updated"] = _now()
            self._write(name, record)

    def token_is_live(self, name: str, token: str) -> bool:
        if self.wiki is None:
            return False
        try:
            profile, renewed = self.wiki.profile_as(token)
        except WikiError as exc:
            if is_identity_auth_failure(exc):
                return False
            raise
        if not profile_ok(profile):
            return False
        if renewed:
            self.store_jwt(name, renewed)
        self.mark_ok(name, profile)
        return True

    def renew(self, name: str) -> None:
        self._states[name] = "renewing"
        token = self.get_token(name)
        if not token:
            self.mark_expired(name)
            log.error("wiki-identitet ogiltig identity=%s orsak=saknas", name)
            return
        if self.wiki is None:
            self.mark_expired(name)
            log.error("wiki-identitet förnyelse misslyckades identity=%s orsak=klient", name)
            return
        try:
            profile, renewed = self.wiki.profile_as(token)
        except WikiError as exc:
            self.mark_expired(name)
            if is_identity_auth_failure(exc):
                log.error("wiki-identitet ogiltig identity=%s orsak=http", name)
            else:
                log.error("wiki-identitet förnyelse misslyckades identity=%s orsak=wiki", name)
            return
        except Exception:
            self.mark_expired(name)
            log.error("wiki-identitet förnyelse misslyckades identity=%s orsak=wiki", name)
            return
        if not profile_ok(profile):
            self.mark_expired(name)
            log.error("wiki-identitet ogiltig identity=%s orsak=profil", name)
            return
        if renewed:
            self.store_jwt(name, renewed)
        self.mark_ok(name, profile)

    def renew_all(self) -> None:
        for name in self.names():
            self.renew(name)


def validate_user_jwt(wiki: WikiClient, jwt: str) -> tuple[dict[str, Any], str]:
    """Fråga users.profile. Returnerar (profil, token att spara). Tokenet är new-jwt om den kom."""
    try:
        profile, renewed = wiki.profile_as(jwt)
    except WikiError as exc:
        if is_identity_auth_failure(exc):
            raise IdentityError("Wiki.js avvisade tokenet") from None
        raise IdentityError("Wiki.js svarade inte på profilfrågan") from None
    if not profile_ok(profile):
        raise IdentityError("tokenet gav inte en riktig användare")
    assert profile is not None
    return profile, renewed or jwt


def call_as_identity(
    wiki: WikiClient,
    store: IdentityStore | None,
    *,
    identity_name: str,
    key_name: str,
    fallback: bool,
    comment: str | None,
    operation,
) -> tuple[Any, str]:
    """Kör operation(comment). Identitet utan fel hoppar över kommentaren.

    operation får antingen den vanliga kommentaren, None (identiteten skrev)
    eller kommentaren plus varning (fallback till API-nyckel).
    """
    name = (identity_name or "").strip()
    if not name:
        return operation(comment), "-"
    if not identity_name_ok(name):
        log.error("wiki-identitet ogiltig identity=%s key=%s orsak=namn", name, key_name)
        return _fallback_or_deny(name, key_name, fallback, comment, operation)

    token = store.get_token(name) if store is not None else None
    if not token:
        if store is not None:
            store.mark_expired(name)
        log.error("wiki-identitet ogiltig identity=%s key=%s orsak=saknas", name, key_name)
        return _fallback_or_deny(name, key_name, fallback, comment, operation)

    def _save(renewed: str) -> None:
        if store is not None:
            store.store_jwt(name, renewed)

    bearer_token = wiki_bearer.set(token)
    renew_token = wiki_on_new_jwt.set(_save)
    failed: Exception | None = None
    result: Any = None
    try:
        result = operation(None)
    except Exception as exc:
        failed = exc
    finally:
        wiki_bearer.reset(bearer_token)
        wiki_on_new_jwt.reset(renew_token)
    if failed is None:
        if store is not None:
            store.mark_ok(name)
        return result, name
    if not _token_dead(wiki, store, name, token, failed):
        raise failed
    if store is not None:
        store.mark_expired(name)
    log.error("wiki-identitet ogiltig identity=%s key=%s orsak=avvisad", name, key_name)
    return _fallback_or_deny(name, key_name, fallback, comment, operation)


def _token_dead(wiki: WikiClient, store: IdentityStore | None, name: str, token: str, exc: Exception) -> bool:
    if isinstance(exc, WikiError) and isinstance(exc.details, dict) and exc.details.get("status") in (401, 403):
        return True
    suspect = is_identity_auth_failure(exc) or (isinstance(exc, WikiError) and "not found" in str(exc).lower())
    if not suspect:
        return False
    if store is None:
        return True
    return not store.token_is_live(name, token)


def _fallback_or_deny(name: str, key_name: str, fallback: bool, comment: str | None, operation):
    if not fallback:
        raise IdentityDenied(name)
    log.error("wiki-identitet fallback identity=%s key=%s", name, key_name)
    if comment:
        warning = comment + f" Varning: wiki-identiteten {name} kunde inte användas, skrev med API-nyckel."
        return operation(warning), f"{name}/fallback"
    return operation(None), f"{name}/fallback"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
