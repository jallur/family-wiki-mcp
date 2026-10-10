"""OAuth 2.1 + PKCE. Formuläret tar en namngiven nyckel och ger ut en opak token."""
from __future__ import annotations

import base64
import hashlib
import html
import logging
import secrets
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .digest import sha256_hex
from .keys import KeyRecord, KeyStore
from .tokens import TokenStore

log = logging.getLogger("wiki-mcp")

# Ett client_id per klient. Redirect-värdar är suffix (grok.com matchar www.grok.com).
OAUTH_CLIENTS: dict[str, tuple[str, ...]] = {
    "grok-web": ("grok.com",),
    "bots": ("x.ai", "grok.com"),
}

WWW_AUTHENTICATE = (
    'Bearer resource_metadata="https://wiki.aldenback.com/.well-known/oauth-protected-resource'
    '?resource=https://wiki.aldenback.com/mcp"'
)


@dataclass
class AuthCode:
    key_name: str
    redirect_uri: str
    code_challenge: str
    code_challenge_method: str
    client_id: str
    expires_at: float
    scope: str




def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _verify_pkce(verifier: str, challenge: str) -> bool:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return secrets.compare_digest(_b64url(digest), challenge)


def oauth_metadata(issuer: str) -> dict:
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/oauth/authorize",
        "token_endpoint": f"{issuer}/oauth/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["wiki"],
        "client_id_metadata_document_supported": False,
    }


def protected_resource_metadata(resource: str, issuer: str) -> dict:
    return {
        "resource": resource,
        "authorization_servers": [issuer],
        "scopes_supported": ["wiki"],
        "bearer_methods_supported": ["header"],
    }


def _redirect_allowed(client_id: str, redirect_uri: str) -> bool:
    suffixes = OAUTH_CLIENTS.get(client_id)
    if not suffixes:
        return False
    parsed = urlparse(redirect_uri)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


def _active_named_key(secret: str, key_store: KeyStore) -> KeyRecord | None:
    key_store.maybe_reload()
    key = key_store.match_digest(sha256_hex(secret))
    if key is None or not key.active:
        return None
    return key


def make_oauth_routes(key_store: KeyStore, token_store: TokenStore):
    codes: dict[str, AuthCode] = {}

    async def authorize(request: Request) -> Response:
        q = request.query_params
        form = await request.form() if request.method == "POST" else None

        def field(name: str, default: str = "") -> str:
            if form is not None and form.get(name) not in (None, ""):
                return str(form.get(name))
            return q.get(name) or default

        client_id = field("client_id")
        redirect_uri = field("redirect_uri")
        state = field("state")
        code_challenge = field("code_challenge")
        code_challenge_method = field("code_challenge_method", "S256").upper()
        scope = field("scope", "wiki")
        response_type = field("response_type", "code")

        if client_id not in OAUTH_CLIENTS:
            return JSONResponse({"error": "invalid_client"}, status_code=400)
        if response_type != "code" or not redirect_uri or not code_challenge or code_challenge_method != "S256":
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        if not _redirect_allowed(client_id, redirect_uri):
            return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)

        if request.method == "POST":
            assert form is not None
            provided = str(form.get("api_key") or "").strip()
            key = _active_named_key(provided, key_store)
            fields = {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": code_challenge_method,
                "scope": scope,
                "response_type": response_type,
            }
            if key is None:
                return HTMLResponse(_authorize_html(error="Fel API-nyckel", **fields), status_code=401)
            code = secrets.token_urlsafe(32)
            codes[code] = AuthCode(
                key_name=key.name,
                redirect_uri=redirect_uri,
                code_challenge=code_challenge,
                code_challenge_method="S256",
                client_id=client_id,
                expires_at=time.time() + 300,
                scope=scope,
            )
            now = time.time()
            for stored, rec in list(codes.items()):
                if rec.expires_at < now:
                    codes.pop(stored, None)
            parts = list(urlparse(redirect_uri))
            qs = dict(parse_qsl(parts[4], keep_blank_values=True))
            qs["code"] = code
            if state:
                qs["state"] = state
            parts[4] = urlencode(qs)
            return RedirectResponse(urlunparse(parts), status_code=302)

        return HTMLResponse(_authorize_html(**_form_hidden(q)))

    async def token(request: Request) -> Response:
        ctype = request.headers.get("content-type", "")
        if "application/json" in ctype:
            body = await request.json()
        else:
            form = await request.form()
            body = dict(form)
        if body.get("grant_type") != "authorization_code":
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
        code = str(body.get("code") or "")
        redirect_uri = str(body.get("redirect_uri") or "")
        verifier = str(body.get("code_verifier") or "")
        body_client = str(body.get("client_id") or "")
        rec = codes.pop(code, None)
        if rec is None or rec.expires_at < time.time():
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if rec.redirect_uri != redirect_uri or (body_client and body_client != rec.client_id):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if rec.code_challenge_method != "S256" or not _verify_pkce(verifier, rec.code_challenge):
            return JSONResponse({"error": "invalid_grant", "error_description": "pkce"}, status_code=400)
        key_store.maybe_reload()
        key = key_store.by_name(rec.key_name)
        if key is None or not key.active:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        access = token_store.issue(key_name=key.name, client_id=rec.client_id)
        log.info("oauth token utfärdad key=%s client=%s", key.name, rec.client_id)
        return JSONResponse(
            {
                "access_token": access,
                "token_type": "bearer",
                "expires_in": 86400 * 365,
                "scope": rec.scope,
            }
        )

    return authorize, token


def _form_hidden(q) -> dict[str, str]:
    return {
        "client_id": q.get("client_id") or "",
        "redirect_uri": q.get("redirect_uri") or "",
        "state": q.get("state") or "",
        "code_challenge": q.get("code_challenge") or "",
        "code_challenge_method": q.get("code_challenge_method") or "S256",
        "scope": q.get("scope") or "wiki",
        "response_type": q.get("response_type") or "code",
    }


def _authorize_html(error: str = "", **fields: str) -> str:
    err = f'<p style="color:#b00020">{html.escape(error)}</p>' if error else ""
    hiddens = "\n".join(
        f'<input type="hidden" name="{html.escape(key)}" value="{html.escape(value)}" />'
        for key, value in fields.items()
    )
    client_id = html.escape(fields.get("client_id") or "")
    return f"""<!doctype html>
<html lang="sv"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wiki MCP — behörighet</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:28rem;margin:3rem auto;padding:0 1rem}}
input[type=password],button{{width:100%;padding:.75rem;font-size:1rem;margin:.4rem 0}}
button{{background:#111;color:#fff;border:0;border-radius:8px}}
</style></head><body>
<h1>Anslut till familjens wiki</h1>
<p>Klient: <code>{client_id}</code></p>
<p>Klistra in den namngivna API-nyckeln. En egen opak åtkomsttoken skapas och kopplas till nyckelns namn. Nyckeln lagras inte som token.</p>
{err}
<form method="post">
{hiddens}
<label>API-nyckel<br><input type="password" name="api_key" autocomplete="current-password" required></label>
<button type="submit">Godkänn</button>
</form>
</body></html>"""
