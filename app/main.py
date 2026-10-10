from __future__ import annotations

import json
import logging
import secrets
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from .config import Settings, path_allowed
from .wiki_client import WikiClient, WikiError
from .oauth import CLIENT_ID, make_oauth_routes, oauth_metadata, protected_resource_metadata

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("wiki-mcp")

settings = Settings.from_env()
wiki = WikiClient(
    settings.wiki_url,
    settings.wiki_token,
    locale=settings.locale,
    editor=settings.editor,
)

mcp = MCPServer(
    name="family-wiki",
    title="Aldenbäck Family Wiki",
    instructions=(
        "MCP bridge for the family Wiki.js. "
        f"Write tools only allow paths under: {', '.join(settings.allowed_prefixes)}. "
        "No delete tools. Locale default is Swedish (sv), editor is markdown. "
        "On update_page you MUST pass tags (use existing tags from get_page to preserve)."
    ),
)


def _ok(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _err(e: Exception) -> str:
    if isinstance(e, WikiError):
        return _ok({"error": str(e), "details": e.details})
    return _ok({"error": str(e)})


@mcp.tool()
def list_pages(limit: int = 100) -> str:
    """List wiki pages (id, path, title, description, updatedAt, locale)."""
    try:
        return _ok({"pages": wiki.list_pages(limit=limit)})
    except Exception as e:
        return _err(e)


@mcp.tool()
def search_pages(query: str) -> str:
    """Full-text search wiki pages."""
    try:
        return _ok(wiki.search(query))
    except Exception as e:
        return _err(e)


@mcp.tool()
def get_page(path: str, locale: str | None = None) -> str:
    """Get a page by path (markdown content, tags, metadata). Tries configured locale then en."""
    try:
        page = wiki.get_by_path(path, locale)
        if not page and locale is None and settings.locale != "en":
            page = wiki.get_by_path(path, "en")
        if not page:
            return _ok({"error": f"not found: {path}"})
        return _ok({"page": page})
    except Exception as e:
        return _err(e)


@mcp.tool()
def create_page(
    path: str,
    title: str,
    content: str,
    description: str | None = None,
    tags: list[str] | None = None,
    locale: str | None = None,
) -> str:
    """Create a published markdown page. Path must be under allowed prefixes (projekt/, familj/, studier/)."""
    try:
        if not path_allowed(path, settings.allowed_prefixes):
            return _ok(
                {
                    "error": "path not allowed",
                    "path": path,
                    "allowed_prefixes": list(settings.allowed_prefixes),
                }
            )
        result = wiki.create_page(
            path=path,
            title=title,
            content=content,
            description=description,
            tags=tags or [],
            locale=locale,
        )
        return _ok(result)
    except Exception as e:
        return _err(e)


@mcp.tool()
def update_page(
    path: str,
    content: str,
    tags: list[str],
    title: str | None = None,
    description: str | None = None,
    locale: str | None = None,
) -> str:
    """Update an existing page. tags is REQUIRED (pass current tags from get_page to preserve). Path must be under allowed prefixes. No delete."""
    try:
        if not path_allowed(path, settings.allowed_prefixes):
            return _ok(
                {
                    "error": "path not allowed",
                    "path": path,
                    "allowed_prefixes": list(settings.allowed_prefixes),
                }
            )
        result = wiki.update_page(
            path=path,
            content=content,
            title=title,
            description=description,
            tags=tags,
            locale=locale,
        )
        return _ok(result)
    except Exception as e:
        return _err(e)


@mcp.tool()
def allowed_write_prefixes() -> str:
    """Return the path prefixes allowed for create_page/update_page. Deletes are not available."""
    return _ok(
        {
            "allowed_prefixes": list(settings.allowed_prefixes),
            "locale_default": settings.locale,
            "editor": settings.editor,
            "delete_supported": False,
        }
    )


async def health(_: Request) -> Response:
    return JSONResponse({"status": "ok", "service": "family-wiki-mcp"})


class ApiKeyMiddleware:
    """Require Authorization Bearer or X-API-Key for non-health routes."""

    def __init__(self, app: ASGIApp, api_key: str):
        self.app = app
        self.api_key = api_key.encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path") or ""
        method = scope.get("method") or "GET"
        if method == "OPTIONS":
            await self.app(scope, receive, send)
            return
        if (
            path == "/health"
            or path.startswith("/health?")
            or path.startswith("/oauth/")
            or path.startswith("/.well-known/")
        ):
            await self.app(scope, receive, send)
            return

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers") or []}
        provided: bytes | None = None
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            provided = auth[7:].strip().encode("utf-8")
        elif headers.get("x-api-key"):
            provided = headers["x-api-key"].strip().encode("utf-8")

        if provided is None or not secrets.compare_digest(provided, self.api_key):
            body = b'{"error":"unauthorized"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (
                            b"www-authenticate",
                            b'Bearer FAKESECRET_g3h4i5j6k7l8m9n0o1p2="https://wiki.aldenback.com/.well-known/oauth-protected-resource?resource=https://wiki.aldenback.com/mcp"',
                        ),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send)



security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[
        settings.public_host,
        f"{settings.public_host}:*",
        "wiki.aldenback.com",
        "wiki.aldenback.com:*",
        "family-wiki-mcp",
        "family-wiki-mcp:*",
        "localhost",
        "localhost:*",
        "127.0.0.1",
        "127.0.0.1:*",
        "wiki-mcp",
        "wiki-mcp:*",
    ],
    allowed_origins=list(settings.allowed_origins)
    + [f"https://{settings.public_host}", "https://wiki.aldenback.com", "https://grok.com", "https://www.grok.com"],
)

mcp_app = mcp.streamable_http_app(
    streamable_http_path="/mcp",
    transport_security=security,
    stateless_http=True,
    json_response=True,
)

# OAuth issuer = https://wiki.aldenback.com  (well-known + /oauth routed here by Caddy)
OAUTH_ISSUER = "https://wiki.aldenback.com"
MCP_RESOURCE = "https://wiki.aldenback.com/mcp"
_well_known_as, _well_known_prm, _authorize, _token = make_oauth_routes(
    settings.mcp_api_key, OAUTH_ISSUER
)


async def well_known_as(request):
    return await _well_known_as(request)


async def well_known_prm(request):
    # Always advertise our MCP resource + this host as AS
    from starlette.responses import JSONResponse
    return JSONResponse(protected_resource_metadata(MCP_RESOURCE, OAUTH_ISSUER))


async def authorize(request):
    return await _authorize(request)


async def token(request):
    return await _token(request)


async def lifespan(app: Starlette):
    async with mcp.session_manager.run():
        yield
    wiki.close()


starlette_app = Starlette(
    routes=[
        Route("/health", health, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server", well_known_as, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource", well_known_prm, methods=["GET"]),
        Route("/oauth/authorize", authorize, methods=["GET", "POST"]),
        Route("/oauth/token", token, methods=["POST"]),
        Mount("/", app=mcp_app),
    ],
    lifespan=lifespan,
    middleware=[
        Middleware(
            CORSMiddleware,
            allow_origins=list(settings.allowed_origins)
            + [
                f"https://{settings.public_host}",
                "https://wiki.aldenback.com",
                "https://grok.com",
                "https://www.grok.com",
            ],
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=[
                "Authorization",
                "X-API-Key",
                "Content-Type",
                "Last-Event-ID",
                "Mcp-Method",
                "Mcp-Name",
                "Mcp-Protocol-Version",
                "Mcp-Session-Id",
            ],
            expose_headers=["Mcp-Session-Id"],
        ),
    ],
)

app = ApiKeyMiddleware(starlette_app, settings.mcp_api_key)
