"""keys.json: namngivna nycklar, omladdning på mtime och SIGHUP."""
from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .digest import const_eq

log = logging.getLogger("wiki-mcp")

_REQUIRED = ("name", "sha256", "scope", "prefixes", "expires", "disabled", "created", "author")


def _parse_expires(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("expires måste vara ISO-tid eller null")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _prefixes(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{field} måste vara en lista")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field} innehåller ett ogiltigt prefix")
        prefix = item.strip().lstrip("/")
        if not prefix.endswith("/"):
            prefix += "/"
        out.append(prefix)
    return tuple(out)


@dataclass(frozen=True)
class KeyRecord:
    name: str
    sha256: str
    scope: str
    prefixes: tuple[str, ...]
    read_prefixes: tuple[str, ...]
    expires: datetime | None
    disabled: bool
    created: str
    author: str

    @property
    def expired(self) -> bool:
        return self.expires is not None and self.expires <= datetime.now(timezone.utc)

    @property
    def active(self) -> bool:
        return not self.disabled and not self.expired


def parse_keys(data: Any) -> list[KeyRecord]:
    if not isinstance(data, list):
        raise ValueError("keys.json måste vara en JSON-lista")
    records: list[KeyRecord] = []
    names: set[str] = set()
    hashes: set[str] = set()
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"post {index} är inte ett objekt")
        missing = [field for field in _REQUIRED if field not in item]
        if missing:
            raise ValueError(f"post {index} saknar {', '.join(missing)}")
        name = item["name"]
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"post {index} har ogiltigt namn")
        name = name.strip()
        digest = item["sha256"]
        if not isinstance(digest, str):
            raise ValueError(f"post {index} har ogiltig sha256")
        digest = digest.strip().lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError(f"post {index} har ogiltig sha256")
        scope = item["scope"]
        if scope not in ("read", "write"):
            raise ValueError(f"post {index} har ogiltig scope")
        if not isinstance(item["disabled"], bool):
            raise ValueError(f"post {index} har ogiltig disabled")
        if not isinstance(item["created"], str) or not item["created"].strip():
            raise ValueError(f"post {index} har ogiltig created")
        if not isinstance(item["author"], str):
            raise ValueError(f"post {index} har ogiltig author")
        if name in names or digest in hashes:
            raise ValueError(f"post {index} har duplicerat namn eller hash")
        names.add(name)
        hashes.add(digest)
        records.append(
            KeyRecord(
                name=name,
                sha256=digest,
                scope=scope,
                prefixes=_prefixes(item["prefixes"], "prefixes"),
                read_prefixes=_prefixes(item.get("read_prefixes", []), "read_prefixes"),
                expires=_parse_expires(item["expires"]),
                disabled=item["disabled"],
                created=item["created"].strip(),
                author=item["author"],
            )
        )
    return records


class KeyStore:
    def __init__(self, path: str):
        self.path = path
        self.keys: list[KeyRecord] = []
        self._mtime: float | None = None
        self.token_store: Any = None

    def attach(self, token_store: Any) -> None:
        self.token_store = token_store

    def by_name(self, name: str) -> KeyRecord | None:
        for key in self.keys:
            if key.name == name:
                return key
        return None

    def match_digest(self, digest: str) -> KeyRecord | None:
        found: KeyRecord | None = None
        for key in self.keys:
            if const_eq(digest, key.sha256) and found is None:
                found = key
        return found

    def maybe_reload(self) -> None:
        try:
            mtime = os.stat(self.path).st_mtime
        except OSError as exc:
            log.error("keys.json går inte att läsa (%s), behåller föregående lista", exc)
            return
        if self._mtime is not None and mtime == self._mtime:
            return
        self.load()

    def on_sighup(self, _signum: int, _frame: Any) -> None:
        log.info("SIGHUP, laddar om keys.json")
        self.load(force=True)

    def load(self, *, force: bool = False) -> None:
        try:
            st = os.stat(self.path)
        except OSError as exc:
            log.error("keys.json går inte att läsa (%s), behåller föregående lista", exc)
            return
        if not force and self._mtime is not None and st.st_mtime == self._mtime:
            return
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
            parsed = parse_keys(data)
        except Exception:
            log.exception("keys.json är trasig, behåller föregående lista")
            self._mtime = st.st_mtime
            return
        mode = stat.S_IMODE(st.st_mode)
        if mode & 0o077:
            log.warning("keys.json har mode %o, ska vara 600: %s", mode, self.path)
        previous = self.keys
        self.keys = parsed
        self._mtime = st.st_mtime
        self._sync_revocations(previous, parsed)

    def _sync_revocations(self, previous: list[KeyRecord], current: list[KeyRecord]) -> None:
        store = self.token_store
        if store is None:
            return
        current_names = {key.name for key in current}
        for key in current:
            if key.disabled:
                count = store.revoke_key(key.name)
                if count:
                    log.info("nyckel disabled, återkallade %s tokens key=%s", count, key.name)
        for key in previous:
            if key.name not in current_names:
                count = store.revoke_key(key.name)
                if count:
                    log.info("nyckel borttagen, återkallade %s tokens key=%s", count, key.name)
