"""Autentisering mot keys.json, OAuth-token och legacy-nyckel."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Settings
from .digest import const_eq, sha256_hex
from .keys import KeyRecord, KeyStore

log = logging.getLogger("wiki-mcp")


@dataclass(frozen=True)
class RequestAuth:
    name: str
    scope: str
    prefixes: tuple[str, ...]
    read_prefixes: tuple[str, ...]
    author: str
    client_id: str
    source: str

    @classmethod
    def from_key(cls, key: KeyRecord, *, client_id: str, source: str) -> "RequestAuth":
        return cls(
            name=key.name,
            scope=key.scope,
            prefixes=key.prefixes,
            read_prefixes=key.read_prefixes,
            author=key.author,
            client_id=client_id,
            source=source,
        )

    @property
    def display_author(self) -> str:
        return (self.author or "").strip() or self.name


def legacy_auth(settings: Settings) -> RequestAuth:
    return RequestAuth(
        name="legacy",
        scope="write",
        prefixes=settings.allowed_prefixes,
        read_prefixes=(),
        author="legacy",
        client_id="-",
        source="legacy",
    )


def _token_expired(expires: str) -> bool:
    text = expires.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return True
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment <= datetime.now(timezone.utc)


def resolve_credential(
    secret: str,
    *,
    settings: Settings,
    key_store: KeyStore,
    token_store: object,
) -> RequestAuth | None:
    """Matcha Bearer/X-API-Key. Jämför sha256 med compare_digest. Loggar aldrig secret."""
    key_store.maybe_reload()
    digest = sha256_hex(secret)

    token = token_store.find(digest)
    if token is not None:
        if token.revoked or _token_expired(token.expires):
            return None
        key = key_store.by_name(token.key_name)
        if key is None or key.disabled:
            if key is not None and key.disabled:
                token_store.revoke_key(key.name)
            return None
        if key.expired:
            return None
        return RequestAuth.from_key(key, client_id=token.client_id, source="oauth")

    key = key_store.match_digest(digest)
    if key is not None and key.active:
        return RequestAuth.from_key(key, client_id="-", source="key")

    legacy = settings.mcp_api_key
    if settings.allow_legacy_api_key and legacy and const_eq(secret, legacy):
        log.warning(
            "legacy MCP_API_KEY accepterades med full skrivrätt (key=legacy). "
            "Stäng av med ALLOW_LEGACY_API_KEY=false"
        )
        return legacy_auth(settings)
    return None
