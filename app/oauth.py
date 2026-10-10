"""Minimal OAuth 2.1 + PKCE for Grok custom MCP connectors."""
from __future__ import annotations

import hashlib
import base64
import secrets
import time
from dataclasses import dataclass
from typing import Dict
from urllib.parse import urlencode, parse_qs, urlparse, urlunparse, parse_qsl

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response


@dataclass
class AuthCode:
    api_key_ok: bool
    redirect_uri: str
    code_challenge: str | None
    code_challenge_method: str | None
    client_id: str
    expires_at: float
    scope: str


_codes: Dict[str, AuthCode] = {}
CLIENT_ID = "family-wiki-mcp"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _verify_pkce(verifier: str, challenge: str, method: str | None) -> bool:
    method = (method or "S256").upper()
    if method == "S256":
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        return secrets.compare_digest(_b64url(digest), challenge)
    if method == "PLAIN":
        return secrets.compare_digest(verifier, challenge)
    return False


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


def make_oauth_routes(api_key: str, public_base: str):
    """public_base e.g. https://wiki.aldenback.com/mcp-auth — issuer base without trailing slash."""

    async def well_known_as(_: Request) -> Response:
        return JSONResponse(oauth_metadata(public_base))

    async def well_known_prm(_: Request) -> Response:
        # Resource is the MCP endpoint
        resource = public_base.replace("/mcp-auth", "/mcp") if public_base.endswith("/mcp-auth") else f"{public_base}/mcp"
        if public_base.endswith("/mcp"):
            resource = public_base
            issuer = public_base  # same host paths
        else:
            issuer = public_base
        # For our deploy: issuer and routes live under wiki host with path prefix handled by Caddy
        return JSONResponse(protected_resource_metadata(resource, public_base))

    async def authorize(request: Request) -> Response:
        q = request.query_params
        client_id = q.get("client_id") or CLIENT_ID
        redirect_uri = q.get("redirect_uri") or ""
        state = q.get("state") or ""
        code_challenge = q.get("code_challenge")
        code_challenge_method = q.get("code_challenge_method")
        scope = q.get("scope") or "wiki"
        response_type = q.get("response_type") or "code"

        if response_type != "code" or not redirect_uri:
            return JSONResponse({"error": "invalid_request"}, status_code=400)

        # Only allow grok.com redirect URIs
        if "grok.com" not in urlparse(redirect_uri).netloc and "x.ai" not in urlparse(redirect_uri).netloc:
            return JSONResponse({"error": "invalid_redirect_uri", "detail": redirect_uri}, status_code=400)

        if request.method == "POST":
            form = await request.form()
            provided = str(form.get("api_key") or "").strip()
            if not secrets.compare_digest(provided, api_key):
                return HTMLResponse(_authorize_html(error="Fel API-nyckel", **_form_hidden(q)), status_code=401)
            code = secrets.token_urlsafe(32)
            _codes[code] = AuthCode(
                api_key_ok=True,
                redirect_uri=redirect_uri,
                code_challenge=code_challenge,
                code_challenge_method=code_challenge_method,
                client_id=client_id,
                expires_at=time.time() + 300,
                scope=scope,
            )
            # prune
            now = time.time()
            for k, v in list(_codes.items()):
                if v.expires_at < now:
                    _codes.pop(k, None)
            parts = list(urlparse(redirect_uri))
            qs = dict(parse_qsl(parts[4], keep_blank_values=True))
            qs["code"] = code
            if state:
                qs["state"] = state
            parts[4] = urlencode(qs)
            return RedirectResponse(urlunparse(parts), status_code=302)

        return HTMLResponse(_authorize_html(**_form_hidden(q)))

    async def token(request: Request) -> Response:
        if request.method == "POST":
            ctype = request.headers.get("content-type", "")
            if "application/json" in ctype:
                body = await request.json()
            else:
                form = await request.form()
                body = dict(form)
        else:
            return JSONResponse({"error": "invalid_request"}, status_code=400)

        if body.get("grant_type") != "authorization_code":
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
        code = str(body.get("code") or "")
        redirect_uri = str(body.get("redirect_uri") or "")
        verifier = str(body.get("code_verifier") or "")
        rec = _codes.pop(code, None)
        if not rec or rec.expires_at < time.time():
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if rec.redirect_uri != redirect_uri:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if rec.code_challenge and not _verify_pkce(verifier, rec.code_challenge, rec.code_challenge_method):
            return JSONResponse({"error": "invalid_grant", "error_description": "pkce"}, status_code=400)
        return JSONResponse(
            {
                "access_token": api_key,
                "token_type": "bearer",
                "expires_in": 86400 * 365,
                "scope": rec.scope,
            }
        )

    return well_known_as, well_known_prm, authorize, token


def _form_hidden(q) -> dict:
    return {
        "client_id": q.get("client_id") or CLIENT_ID,
        "redirect_uri": q.get("redirect_uri") or "",
        "state": q.get("state") or "",
        "code_challenge": q.get("code_challenge") or "",
        "code_challenge_method": q.get("code_challenge_method") or "S256",
        "scope": q.get("scope") or "wiki",
        "response_type": q.get("response_type") or "code",
    }


def _authorize_html(error: str = "", **fields) -> str:
    err = f'<p style="color:#b00020">{error}</p>' if error else ""
    hiddens = "\n".join(
        f'<input type="hidden" name="{k}" value="{v}" />'
        for k, v in fields.items()
        if k != "api_key"
    )
    return f"""<!doctype html>
<html lang="sv"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wiki MCP — behörighet</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:28rem;margin:3rem auto;padding:0 1rem}}
input[type=password],button{{width:100%;padding:.75rem;font-size:1rem;margin:.4rem 0}}
button{{background:#111;color:#fff;border:0;border-radius:8px}}
</style></head><body>
<h1>Anslut Grok till familjens wiki</h1>
<p>Klistra in MCP API-nyckeln (samma som i <code>/root/family-wiki-mcp/.mcp-api-key</code>).</p>
{err}
<form method="post">
{hiddens}
<label>API-nyckel<br><input type="password" name="api_key" autocomplete="current-password" required></label>
<button type="submit">Godkänn</button>
</form>
</body></html>"""
