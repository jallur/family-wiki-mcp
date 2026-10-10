from __future__ import annotations

import os
from dataclasses import dataclass


def _csv(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default)
    return [p.strip() for p in raw.split(",") if p.strip()]


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    wiki_url: str
    wiki_token: str
    mcp_api_key: str
    allowed_prefixes: tuple[str, ...]
    locale: str
    editor: str
    host: str
    port: int
    public_host: str
    allowed_origins: tuple[str, ...]
    keys_file: str
    token_store_path: str
    allow_legacy_api_key: bool
    forwarded_allow_ips: str

    @classmethod
    def from_env(cls) -> "Settings":
        wiki_url = os.environ.get("WIKI_URL", "https://wiki.aldenback.com/graphql").rstrip("/")
        if not wiki_url.endswith("/graphql"):
            base = os.environ.get("WIKI_BASE_URL", "").rstrip("/")
            if base:
                wiki_url = f"{base}/graphql"
        token = os.environ.get("WIKI_TOKEN", "").strip()
        token_file = os.environ.get("WIKI_TOKEN_FILE", "").strip()
        if not token and token_file:
            with open(token_file, encoding="utf-8") as f:
                token = f.read().strip()
        if not token:
            raise RuntimeError("WIKI_TOKEN or WIKI_TOKEN_FILE is required")

        api_key = os.environ.get("MCP_API_KEY", "").strip()
        allow_legacy = _flag("ALLOW_LEGACY_API_KEY", True)
        if allow_legacy and api_key and len(api_key) < 24:
            raise RuntimeError("MCP_API_KEY must be at least 24 characters when legacy is enabled")

        prefixes = _csv("ALLOWED_PREFIXES", "projekt/,familj/,studier/")
        normalized = []
        for p in prefixes:
            p = p.lstrip("/")
            if not p.endswith("/"):
                p = p + "/"
            normalized.append(p)

        origins = _csv(
            "ALLOWED_ORIGINS",
            "https://grok.com,https://www.grok.com,https://x.ai,https://mcp.wiki.aldenback.com",
        )
        return cls(
            wiki_url=wiki_url,
            wiki_token=token,
            mcp_api_key=api_key,
            allowed_prefixes=tuple(normalized),
            locale=os.environ.get("WIKI_LOCALE", "sv"),
            editor=os.environ.get("WIKI_EDITOR", "markdown"),
            host=os.environ.get("HOST", "0.0.0.0"),
            port=int(os.environ.get("PORT", "8080")),
            public_host=os.environ.get("PUBLIC_HOST", "mcp.wiki.aldenback.com"),
            allowed_origins=tuple(origins),
            keys_file=os.environ.get("KEYS_FILE", "/run/secrets/keys/keys.json"),
            token_store_path=os.environ.get("TOKEN_STORE_PATH", "/data/oauth-tokens.json"),
            allow_legacy_api_key=allow_legacy,
            forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "172.20.0.2").strip() or "172.20.0.2",
        )


def path_allowed(path: str, prefixes: tuple[str, ...]) -> bool:
    path = (path or "").lstrip("/")
    if ".." in path or path.startswith("/") or "\x00" in path:
        return False
    return any(path == p.rstrip("/") or path.startswith(p) for p in prefixes)
