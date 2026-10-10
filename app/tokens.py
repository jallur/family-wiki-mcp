"""Beständig lagring av OAuth-access-tokens som sha256."""
from __future__ import annotations

import json
import logging
import os
import secrets
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
        self.load()

    def load(self) -> None:
        if not os.path.exists(self.path):
            self.tokens = []
            return
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
            raw = data.get("tokens") if isinstance(data, dict) else None
            if not isinstance(raw, list):
                raise ValueError("tokenfilen saknar tokens-lista")
            self.tokens = [TokenRecord.from_json(item) for item in raw if isinstance(item, dict)]
        except Exception:
            log.exception("tokenfilen är trasig, behåller tom lista i minnet: %s", self.path)
            self.tokens = []

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
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def find(self, digest: str) -> TokenRecord | None:
        found: TokenRecord | None = None
        with self._lock:
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
            self.tokens.append(record)
            self.save()
        return raw

    def revoke_hash(self, digest: str) -> bool:
        changed = False
        with self._lock:
            for token in self.tokens:
                if const_eq(digest, token.sha256) and not token.revoked:
                    token.revoked = True
                    changed = True
            if changed:
                self.save()
        return changed

    def revoke_key(self, key_name: str) -> int:
        count = 0
        with self._lock:
            for token in self.tokens:
                if token.key_name == key_name and not token.revoked:
                    token.revoked = True
                    count += 1
            if count:
                self.save()
        return count
