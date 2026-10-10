"""Beständig lagring av OAuth-access-tokens som sha256.

Filen läses om när mtime ändras, på samma sätt som keys.json. En trasig
fil behåller listan som redan ligger i minnet. Det finns ingen HTTP-väg
för återkallning; den sker via scripts/revoke_token.py eller när en
nyckel blir disabled.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import stat
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .digest import const_eq, sha256_hex

log = logging.getLogger("wiki-mcp")

TOKEN_TTL_SECONDS = 86400 * 365


@dataclass
class TokenRecord:
    sha256: str
    key_name: str
    client_id: str
    created: str
    expires: str
    revoked: bool

    def to_json(self) -> dict[str, object]:
        return {
            "sha256": self.sha256,
            "key_name": self.key_name,
            "client_id": self.client_id,
            "created": self.created,
            "expires": self.expires,
            "revoked": self.revoked,
        }

    @classmethod
    def from_json(cls, item: dict[str, object]) -> "TokenRecord":
        digest = str(item["sha256"]).strip().lower()
        if len(digest) != 64:
            raise ValueError("ogiltig token-hash")
        return cls(
            sha256=digest,
            key_name=str(item["key_name"]),
            client_id=str(item["client_id"]),
            created=str(item["created"]),
            expires=str(item["expires"]),
            revoked=bool(item.get("revoked", False)),
        )


class TokenStore:
    def __init__(self, path: str):
        self.path = path
        self.tokens: list[TokenRecord] = []
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self.load()

    def maybe_reload(self) -> None:
        with self._lock:
            self._reload_locked()

    def load(self, *, force: bool = False) -> None:
        with self._lock:
            if force:
                self._mtime = None
            self._reload_locked()

    def _reload_locked(self) -> None:
        try:
            st = os.stat(self.path)
        except OSError as exc:
            if self._mtime is not None:
                log.error("tokenfilen går inte att läsa (%s), behåller föregående lista", exc)
            return
        if self._mtime is not None and st.st_mtime == self._mtime:
            return
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
            raw = data.get("tokens") if isinstance(data, dict) else None
            if not isinstance(raw, list):
                raise ValueError("tokenfilen saknar tokens-lista")
            parsed = [TokenRecord.from_json(item) for item in raw if isinstance(item, dict)]
        except Exception:
            log.exception("tokenfilen är trasig, behåller föregående lista")
            self._mtime = st.st_mtime
            return
        mode = stat.S_IMODE(st.st_mode)
        if mode & 0o077:
            log.warning("tokenfilen har mode %o, ska vara 600: %s", mode, self.path)
        self.tokens = parsed
        self._mtime = st.st_mtime

    def save(self) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        payload = {"tokens": [token.to_json() for token in self.tokens]}
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tokens-", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)
            self._mtime = os.stat(self.path).st_mtime
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def find(self, digest: str) -> TokenRecord | None:
        found: TokenRecord | None = None
        with self._lock:
            self._reload_locked()
            for token in self.tokens:
                if const_eq(digest, token.sha256) and found is None:
                    found = token
        return found

    def issue(self, *, key_name: str, client_id: str, ttl_seconds: int = TOKEN_TTL_SECONDS) -> str:
        raw = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        record = TokenRecord(
            sha256=sha256_hex(raw),
            key_name=key_name,
            client_id=client_id,
            created=now.isoformat(),
            expires=(now + timedelta(seconds=ttl_seconds)).isoformat(),
            revoked=False,
        )
        with self._lock:
            self._reload_locked()
            self.tokens.append(record)
            self.save()
        return raw

    def revoke_hash(self, digest: str) -> bool:
        needle = digest.strip().lower()
        changed = False
        with self._lock:
            self._reload_locked()
            for token in self.tokens:
                if const_eq(needle, token.sha256) and not token.revoked:
                    token.revoked = True
                    changed = True
            if changed:
                self.save()
        return changed

    def revoke_key(self, key_name: str) -> int:
        return self._revoke_where("key_name", key_name)

    def revoke_client(self, client_id: str) -> int:
        return self._revoke_where("client_id", client_id)

    def _revoke_where(self, field: str, value: str) -> int:
        count = 0
        with self._lock:
            self._reload_locked()
            for token in self.tokens:
                if getattr(token, field) == value and not token.revoked:
                    token.revoked = True
                    count += 1
            if count:
                self.save()
        return count
