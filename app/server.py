"""ASGI-app: middleware, verktyg, OAuth och loggning."""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import signal
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from .auth import RequestAuth, resolve_credential
from .config import Settings, path_allowed
from .identity import IdentityDenied, IdentityStore, call_as_identity
from .keys import KeyStore
from .oauth import WWW_AUTHENTICATE, make_oauth_routes, oauth_metadata, protected_resource_metadata
from .tokens import TokenStore
from .wiki_client import WikiClient, WikiError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("wiki-mcp")

OAUTH_ISSUER = "https://wiki.aldenback.com"
MCP_RESOURCE = "https://wiki.aldenback.com/mcp"
current_auth: contextvars.ContextVar[RequestAuth | None] = contextvars.ContextVar("wiki_mcp_auth", default=None)
current_ip: contextvars.ContextVar[str] = contextvars.ContextVar("wiki_mcp_ip", default="-")


def _ok(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _err(exc: Exception) -> str:
    if isinstance(exc, WikiError):
        return _ok({"error": str(exc), "details": exc.details})
    return _ok({"error": str(exc)})


def _client_ip(scope: Scope) -> str:
    client = scope.get("client")
    if isinstance(client, tuple) and client:
        return str(client[0])
    return "-"


def _extract_secret(scope: Scope) -> str | None:
    auth_header = ""
    api_key = ""
    for key, value in scope.get("headers") or []:
        name = key.decode("latin-1").lower()
        text = value.decode("latin-1").strip()
        if name == "authorization":
            auth_header = text
        elif name == "x-api-key":
            api_key = text
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        if token:
            return token
    if api_key:
        return api_key
    return None


def _request_auth(ctx: Context) -> RequestAuth | None:
    request = getattr(ctx.request_context, "request", None)
    state = getattr(request, "state", None) if request is not None else None
    auth = getattr(state, "auth", None) if state is not None else None
    if isinstance(auth, RequestAuth):
        return auth
    return current_auth.get()


def _ip_from_ctx(ctx: Context) -> str:
    request = getattr(ctx.request_context, "request", None)
    client = getattr(request, "client", None) if request is not None else None
    host = getattr(client, "host", None) if client is not None else None
    if host:
        return str(host)
    return current_ip.get()


def _log_call(
    auth: RequestAuth | None,
    tool: str,
    path: str,
    result: str,
    ip: str,
    *,
    identity: str = "-",
) -> None:
    log.info(
        "anrop key=%s client=%s tool=%s path=%s result=%s ip=%s identity=%s",
        auth.name if auth else "-",
        auth.client_id if auth else "-",
        tool,
        path or "-",
        result,
        ip,
        identity,
    )


def _change_comment(auth: RequestAuth, action: str) -> str:
    who = auth.display_author
    return f"MCP: sidan {action} av {who} (nyckel {auth.name})."


def _can_read(auth: RequestAuth, path: str) -> bool:
    if not auth.read_prefixes:
        return True
    return path_allowed(path, auth.read_prefixes)


def _write_error(auth: RequestAuth, path: str, settings: Settings) -> str | None:
    if auth.scope != "write":
        return "write scope required"
    if not path_allowed(path, auth.prefixes) or not path_allowed(path, settings.allowed_prefixes):
        return "path not allowed"
    return None


class ApiKeyMiddleware:
    """Bearer eller X-API-Key. Namn, scope och prefix läggs i request-kontexten."""

    def __init__(self, app: ASGIApp, settings: Settings, key_store: KeyStore, token_store: TokenStore):
        self.app = app
        self.settings = settings
        self.key_store = key_store
        self.token_store = token_store

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or ""
        method = scope.get("method") or "GET"
        if (
            method == "OPTIONS"
            or path in {"/health", "/healthz", "/status"}
            or path.startswith("/oauth/")
            or path.startswith("/.well-known/")
        ):
            await self.app(scope, receive, send)
            return

        secret = _extract_secret(scope)
        auth = (
            resolve_credential(
                secret,
                settings=self.settings,
                key_store=self.key_store,
                token_store=self.token_store,
            )
            if secret
            else None
        )
        if auth is None:
            body = b'{"error":"unauthorized"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", WWW_AUTHENTICATE.encode("ascii")),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        state = scope.setdefault("state", {})
        state["auth"] = auth
        state["auth_name"] = auth.name
        state["auth_scope"] = auth.scope
        state["auth_prefixes"] = auth.prefixes
        state["auth_read_prefixes"] = auth.read_prefixes
        state["auth_client"] = auth.client_id
        auth_token = current_auth.set(auth)
        ip_token = current_ip.set(_client_ip(scope))
        try:
            await self.app(scope, receive, send)
        finally:
            current_auth.reset(auth_token)
            current_ip.reset(ip_token)


def create_app(
    settings: Settings,
    wiki: WikiClient,
    key_store: KeyStore,
    token_store: TokenStore,
    *,
    install_signals: bool = False,
    identities: IdentityStore | None = None,
) -> ASGIApp:
    if install_signals:
        signal.signal(signal.SIGHUP, key_store.on_sighup)
    if settings.allow_legacy_api_key and settings.mcp_api_key:
        log.warning(
            "ALLOW_LEGACY_API_KEY är på. MCP_API_KEY ger full skrivrätt inom serverns prefix. "
            "Stäng av med ALLOW_LEGACY_API_KEY=false när klienterna är omkopplade."
        )

    mcp = MCPServer(
        name="family-wiki",
        title="Aldenbäck Family Wiki",
        instructions=(
            "MCP-brygga för familjens Wiki.js. "
            "create_page och update_page kräver scope write och att sökvägen ligger under nyckelns prefix. "
            "Läsning filtreras av read_prefixes. Tom read_prefixes betyder hela wikin. "
            "Inga raderingsverktyg. Standardspråk är svenska (sv), editor är markdown. "
            "Vid update_page måste tags skickas med (ta befintliga taggar från get_page)."
        ),
    )

    def _as_identity(auth: RequestAuth, comment: str | None, operation):
        return call_as_identity(
            wiki,
            identities,
            identity_name=auth.wiki_identity,
            key_name=auth.name,
            fallback=settings.identity_fallback,
            comment=comment,
            operation=operation,
        )

    @mcp.tool()
    def list_pages(limit: int = 100, *, ctx: Context) -> str:
        """List wiki pages (id, path, title, description, updatedAt, locale). Filtered by read_prefixes."""
        auth = _request_auth(ctx)
        ip = _ip_from_ctx(ctx)
        if auth is None:
            _log_call(None, "list_pages", "-", "unauthorized", ip)
            return _ok({"error": "unauthorized"})
        try:
            pages, label = _as_identity(auth, None, lambda _comment: wiki.list_pages(limit=limit))
            visible = [page for page in pages if _can_read(auth, str(page.get("path") or ""))]
            _log_call(auth, "list_pages", "-", "ok", ip, identity=label)
            return _ok({"pages": visible})
        except IdentityDenied as denied:
            _log_call(auth, "list_pages", "-", "error", ip, identity=denied.name)
            return _ok({"error": "wiki-identitet ogiltig", "identity": denied.name})
        except Exception as exc:
            _log_call(auth, "list_pages", "-", "error", ip)
            log.exception("verktyg list_pages misslyckades")
            return _err(exc)

    @mcp.tool()
    def search_pages(query: str, *, ctx: Context) -> str:
        """Full-text search wiki pages. Results outside read_prefixes are omitted."""
        auth = _request_auth(ctx)
        ip = _ip_from_ctx(ctx)
        if auth is None:
            _log_call(None, "search_pages", "-", "unauthorized", ip)
            return _ok({"error": "unauthorized"})
        try:
            data, label = _as_identity(auth, None, lambda _comment: wiki.search(query))
            results = [row for row in (data.get("results") or []) if _can_read(auth, str(row.get("path") or ""))]
            _log_call(auth, "search_pages", "-", "ok", ip, identity=label)
            return _ok({"results": results, "totalHits": len(results)})
        except IdentityDenied as denied:
            _log_call(auth, "search_pages", "-", "error", ip, identity=denied.name)
            return _ok({"error": "wiki-identitet ogiltig", "identity": denied.name})
        except Exception as exc:
            _log_call(auth, "search_pages", "-", "error", ip)
            log.exception("verktyg search_pages misslyckades")
            return _err(exc)

    @mcp.tool()
    def get_page(path: str, locale: str | None = None, *, ctx: Context) -> str:
        """Get a page by path (markdown content, tags, metadata). Tries configured locale then en."""
        auth = _request_auth(ctx)
        ip = _ip_from_ctx(ctx)
        if auth is None:
            _log_call(None, "get_page", path, "unauthorized", ip)
            return _ok({"error": "unauthorized"})
        if not _can_read(auth, path):
            _log_call(auth, "get_page", path, "denied", ip)
            return _ok({"error": "path not allowed", "path": path})
        try:
            def _read(_comment: str | None):
                page = wiki.get_by_path(path, locale)
                if not page and locale is None and settings.locale != "en":
                    page = wiki.get_by_path(path, "en")
                return page

            page, label = _as_identity(auth, None, _read)
            if not page:
                _log_call(auth, "get_page", path, "not_found", ip, identity=label)
                return _ok({"error": f"not found: {path}"})
            _log_call(auth, "get_page", path, "ok", ip, identity=label)
            return _ok({"page": page})
        except IdentityDenied as denied:
            _log_call(auth, "get_page", path, "error", ip, identity=denied.name)
            return _ok({"error": "wiki-identitet ogiltig", "identity": denied.name})
        except Exception as exc:
            _log_call(auth, "get_page", path, "error", ip)
            log.exception("verktyg get_page misslyckades path=%s", path)
            return _err(exc)

    @mcp.tool()
    def create_page(
        path: str,
        title: str,
        content: str,
        description: str | None = None,
        tags: list[str] | None = None,
        locale: str | None = None,
        *,
        ctx: Context,
    ) -> str:
        """Create a published markdown page. Requires write scope and a matching key prefix."""
        auth = _request_auth(ctx)
        ip = _ip_from_ctx(ctx)
        if auth is None:
            _log_call(None, "create_page", path, "unauthorized", ip)
            return _ok({"error": "unauthorized"})
        blocked = _write_error(auth, path, settings)
        if blocked:
            _log_call(auth, "create_page", path, "denied", ip)
            return _ok({"error": blocked, "path": path, "allowed_prefixes": list(auth.prefixes)})
        try:
            result, label = _as_identity(
                auth,
                _change_comment(auth, "skapades"),
                lambda comment: wiki.create_page(
                    path=path,
                    title=title,
                    content=content,
                    description=description,
                    tags=tags or [],
                    locale=locale,
                    change_comment=comment,
                ),
            )
            _log_call(auth, "create_page", path, "ok", ip, identity=label)
            return _ok(result)
        except IdentityDenied as denied:
            _log_call(auth, "create_page", path, "error", ip, identity=denied.name)
            return _ok({"error": "wiki-identitet ogiltig", "identity": denied.name})
        except Exception as exc:
            _log_call(auth, "create_page", path, "error", ip)
            log.exception("verktyg create_page misslyckades path=%s", path)
            return _err(exc)

    @mcp.tool()
    def update_page(
        path: str,
        content: str,
        tags: list[str],
        title: str | None = None,
        description: str | None = None,
        locale: str | None = None,
        *,
        ctx: Context,
    ) -> str:
        """Update an existing page. tags is REQUIRED. Requires write scope and a matching key prefix."""
        auth = _request_auth(ctx)
        ip = _ip_from_ctx(ctx)
        if auth is None:
            _log_call(None, "update_page", path, "unauthorized", ip)
            return _ok({"error": "unauthorized"})
        blocked = _write_error(auth, path, settings)
        if blocked:
            _log_call(auth, "update_page", path, "denied", ip)
            return _ok({"error": blocked, "path": path, "allowed_prefixes": list(auth.prefixes)})
        try:
            result, label = _as_identity(
                auth,
                _change_comment(auth, "uppdaterades"),
                lambda comment: wiki.update_page(
                    path=path,
                    content=content,
                    title=title,
                    description=description,
                    tags=tags,
                    locale=locale,
                    change_comment=comment,
                ),
            )
            _log_call(auth, "update_page", path, "ok", ip, identity=label)
            return _ok(result)
        except IdentityDenied as denied:
            _log_call(auth, "update_page", path, "error", ip, identity=denied.name)
            return _ok({"error": "wiki-identitet ogiltig", "identity": denied.name})
        except Exception as exc:
            _log_call(auth, "update_page", path, "error", ip)
            log.exception("verktyg update_page misslyckades path=%s", path)
            return _err(exc)

    @mcp.tool()
    def allowed_write_prefixes(*, ctx: Context) -> str:
        """Return the path prefixes this key may use for create_page/update_page."""
        auth = _request_auth(ctx)
        ip = _ip_from_ctx(ctx)
        if auth is None:
            _log_call(None, "allowed_write_prefixes", "-", "unauthorized", ip)
            return _ok({"error": "unauthorized"})
        effective = [prefix for prefix in auth.prefixes if path_allowed(prefix.rstrip("/"), settings.allowed_prefixes)]
        _log_call(auth, "allowed_write_prefixes", "-", "ok", ip)
        return _ok(
            {
                "allowed_prefixes": effective,
                "scope": auth.scope,
                "locale_default": settings.locale,
                "editor": settings.editor,
                "delete_supported": False,
            }
        )

    async def health(_: Request) -> Response:
        return JSONResponse({"status": "ok", "service": "family-wiki-mcp"})

    async def identity_status(_: Request) -> Response:
        rows = identities.status() if identities is not None else {}
        degraded = any(row.get("state") == "expired" for row in rows.values())
        return JSONResponse({"status": "degraded" if degraded else "ok", "identities": rows})

    origins = list(settings.allowed_origins) + [
        f"https://{settings.public_host}",
        "https://wiki.aldenback.com",
        "https://grok.com",
        "https://www.grok.com",
    ]
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
        allowed_origins=origins,
    )
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        transport_security=security,
        stateless_http=True,
        json_response=True,
    )
    authorize, token = make_oauth_routes(key_store, token_store)

    async def well_known_as(_: Request) -> Response:
        return JSONResponse(oauth_metadata(OAUTH_ISSUER))

    async def well_known_prm(_: Request) -> Response:
        return JSONResponse(protected_resource_metadata(MCP_RESOURCE, OAUTH_ISSUER))

    async def _renew_loop() -> None:
        assert identities is not None
        while True:
            try:
                await asyncio.to_thread(identities.renew_all)
            except Exception:
                log.error("wiki-identitet förnyelse misslyckades identity=- orsak=loop")
            await asyncio.sleep(settings.identity_renew_seconds)

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette):
        task: asyncio.Task[None] | None = None
        if identities is not None and settings.identity_renew_seconds > 0:
            task = asyncio.create_task(_renew_loop())
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            wiki.close()

    starlette_app = Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/healthz", identity_status, methods=["GET"]),
            Route("/status", identity_status, methods=["GET"]),
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
                allow_origins=origins,
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
    return ApiKeyMiddleware(starlette_app, settings, key_store, token_store)


def create_app_from_env() -> ASGIApp:
    settings = Settings.from_env()
    wiki = WikiClient(
        settings.wiki_url,
        settings.wiki_token,
        locale=settings.locale,
        editor=settings.editor,
    )
    key_store = KeyStore(settings.keys_file)
    token_store = TokenStore(settings.token_store_path)
    key_store.attach(token_store)
    key_store.load()
    identities = IdentityStore(settings.identity_store_dir, settings.identity_key_file, wiki)
    return create_app(settings, wiki, key_store, token_store, install_signals=True, identities=identities)
