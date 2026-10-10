from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from cryptography.fernet import Fernet

from app.config import Settings
from app.identity import IdentityStore
from app.keys import KeyStore, parse_keys
from app.server import create_app
from app.tokens import TokenStore
from app.wiki_client import WikiClient
from app.identity import IdentityError, validate_user_jwt
from tests.conftest import call_tool, key_record, make_settings, tool_payload

SECRET = "identity-key-secret-do-not-log-0123456789"
JWT = "eyJhbGci.jonas-do-not-log-old.signature"
NEW = "eyJhbGci.jonas-do-not-log-new.signature"
API = "wiki-api-token-do-not-log-0123456789"
PROFILE = {"id": 7, "name": "Jonas", "email": "jonas@example.com"}


def _key_file(tmp_path) -> str:
    path = tmp_path / "identity.key"
    path.write_bytes(Fernet.generate_key())
    os.chmod(path, 0o600)
    return str(path)


def _page_body(operation: str) -> dict:
    return {
        "data": {
            "pages": {
                operation: {
                    "responseResult": {"succeeded": True, "message": "ok", "errorCode": 0},
                    "page": {"id": 7},
                }
            }
        }
    }


def _profile_body(profile: dict | None = None) -> dict:
    return {"data": {"users": {"profile": profile or PROFILE}}}


def _app(tmp_path, handler, *, fallback: bool = True, jwt: str = JWT):
    key_file = _key_file(tmp_path)
    store_dir = tmp_path / "identities"
    wiki = WikiClient("https://wiki.example/graphql", API, locale="sv")
    wiki._client = httpx.Client(transport=httpx.MockTransport(handler))
    store = IdentityStore(str(store_dir), key_file, wiki)
    store.save_validated("jonas", jwt, PROFILE)
    keys_path = tmp_path / "keys.json"
    record = key_record("jonas", SECRET, author="Jonas", prefixes=["projekt/"])
    record["wiki_identity"] = "jonas"
    keys_path.write_text(json.dumps([record]), encoding="utf-8")
    os.chmod(keys_path, 0o600)
    token_path = tmp_path / "tokens.json"
    settings = make_settings(
        keys_file=str(keys_path),
        token_store_path=str(token_path),
        identity_key_file=key_file,
        identity_store_dir=str(store_dir),
        identity_fallback=fallback,
        identity_renew_seconds=0,
    )
    key_store = KeyStore(str(keys_path))
    tokens = TokenStore(str(token_path))
    key_store.attach(tokens)
    key_store.load()
    app = create_app(settings, wiki, key_store, tokens, identities=store)
    return app, store


def test_encryption_hides_the_token(tmp_path):
    store = IdentityStore(str(tmp_path / "identities"), _key_file(tmp_path))
    store.save_validated("jonas", JWT, PROFILE)
    raw = (tmp_path / "identities" / "jonas.json").read_bytes()
    assert JWT.encode() not in raw
    assert store.get_token("jonas") == JWT
    assert oct(os.stat(tmp_path / "identities" / "jonas.json").st_mode & 0o777) == "0o600"
    assert store.status()["jonas"]["state"] == "ok"
    assert store.status()["jonas"]["user_id"] == 7


def test_renew_rotates_new_jwt_without_logging_it(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="wiki-mcp")
    key_file = _key_file(tmp_path)
    wiki = WikiClient("https://wiki.example/graphql", API)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        assert request.headers["content-type"] == "application/json"
        assert "profile" in request.content.decode()
        return httpx.Response(200, json=_profile_body(), headers={"new-jwt": NEW})

    wiki._client = httpx.Client(transport=httpx.MockTransport(handler))
    store = IdentityStore(str(tmp_path / "identities"), key_file, wiki)
    store.save_validated("jonas", JWT, PROFILE)

    def spy_state() -> str:
        return store.state("jonas")

    original = wiki.profile_as

    def wrapped(token: str):
        seen.append("state=" + spy_state())
        return original(token)

    wiki.profile_as = wrapped  # type: ignore[method-assign]
    store.renew("jonas")
    wiki.close()
    assert "state=renewing" in seen
    assert f"Bearer {JWT}" in seen
    assert store.state("jonas") == "ok"
    assert store.get_token("jonas") == NEW
    blob = (tmp_path / "identities" / "jonas.json").read_bytes()
    assert JWT.encode() not in blob
    assert NEW.encode() not in blob
    assert JWT not in caplog.text
    assert NEW not in caplog.text


def test_renew_expiry_marks_expired_and_logs_without_token(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="wiki-mcp")
    wiki = WikiClient("https://wiki.example/graphql", API)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "expired"})

    wiki._client = httpx.Client(transport=httpx.MockTransport(handler))
    store = IdentityStore(str(tmp_path / "identities"), _key_file(tmp_path), wiki)
    store.save_validated("jonas", JWT, PROFILE)
    store.renew("jonas")
    wiki.close()
    assert store.state("jonas") == "expired"
    assert "wiki-identitet ogiltig identity=jonas" in caplog.text
    assert JWT not in caplog.text
    assert store.get_token("jonas") == JWT


def test_identity_write_skips_comment_and_rotates(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="wiki-mcp")
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.content.decode()
        authz = request.headers["authorization"]
        calls.append((authz, query))
        assert authz != f"Bearer {API}"
        if "comments" in query:
            raise AssertionError("kommentaren ska hoppas över")
        if "singleByPath" in query:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "pages": {
                            "singleByPath": {
                                "id": 7,
                                "path": "projekt/a",
                                "title": "A",
                                "description": "A",
                                "content": "hej",
                                "editor": "markdown",
                                "locale": "sv",
                                "tags": [{"tag": "ett"}],
                            }
                        }
                    }
                },
            )
        if "update(" in query:
            return httpx.Response(200, json=_page_body("update"))
        if "create(" in query:
            return httpx.Response(200, json=_page_body("create"), headers={"new-jwt": NEW})
        raise AssertionError(query)

    app, store = _app(tmp_path, handler)
    with client_for_app(app) as client:
        status = client.get("/status")
        assert status.status_code == 200
        assert status.json()["identities"]["jonas"]["state"] == "ok"
        assert JWT not in status.text
        created = tool_payload(
            call_tool(
                client,
                SECRET,
                "create_page",
                {"path": "projekt/a", "title": "A", "content": "hej", "description": "A", "tags": ["ett"]},
            )
        )
        updated = tool_payload(
            call_tool(
                client,
                SECRET,
                "update_page",
                {"path": "projekt/a", "content": "hej\n", "tags": ["ett"]},
            )
        )
    assert created["responseResult"]["succeeded"] is True
    assert "change_comment" not in created
    assert updated["responseResult"]["succeeded"] is True
    assert "change_comment" not in updated
    assert store.get_token("jonas") == NEW
    assert "identity=jonas" in caplog.text
    assert JWT not in caplog.text
    assert NEW not in caplog.text
    assert API not in caplog.text
    assert any(authz == f"Bearer {JWT}" and "create(" in query for authz, query in calls)
    assert any(authz == f"Bearer {NEW}" and "update(" in query for authz, query in calls)


def test_fallback_uses_api_key_and_warning(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="wiki-mcp")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.content.decode()
        authz = request.headers["authorization"]
        calls.append(authz)
        if authz == f"Bearer {JWT}":
            return httpx.Response(401, json={"error": "expired"})
        assert authz == f"Bearer {API}"
        if "comments" in query:
            return httpx.Response(
                200,
                json={"data": {"comments": {"create": {"responseResult": {"succeeded": True, "message": "ok", "errorCode": 0}, "id": 3}}}},
            )
        if "create(" in query:
            assert "hej" in query or True
            body = json.loads(request.content)
            assert body["variables"]["content"] == "hej"
            return httpx.Response(200, json=_page_body("create"))
        raise AssertionError(query)

    app, _store = _app(tmp_path, handler, fallback=True)
    with client_for_app(app) as client:
        created = tool_payload(
            call_tool(
                client,
                SECRET,
                "create_page",
                {"path": "projekt/a", "title": "A", "content": "hej", "tags": ["ett"]},
            )
        )
    assert created["responseResult"]["succeeded"] is True
    assert "Varning:" in created["change_comment"]["content"]
    assert "API-nyckel" in created["change_comment"]["content"]
    assert "identity=jonas/fallback" in caplog.text
    assert "wiki-identitet ogiltig identity=jonas" in caplog.text
    assert JWT not in caplog.text
    assert f"Bearer {API}" in calls
    assert f"Bearer {JWT}" in calls


def test_fallback_false_denies_write(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="wiki-mcp")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["authorization"])
        return httpx.Response(401, json={"error": "expired"})

    app, store = _app(tmp_path, handler, fallback=False)
    with client_for_app(app) as client:
        denied = tool_payload(
            call_tool(client, SECRET, "create_page", {"path": "projekt/a", "title": "A", "content": "hej", "tags": ["ett"]})
        )
        healthz = client.get("/healthz")
    assert denied["error"] == "wiki-identitet ogiltig"
    assert denied["identity"] == "jonas"
    assert calls == [f"Bearer {JWT}"]
    assert store.state("jonas") == "expired"
    assert healthz.json()["status"] == "degraded"
    assert healthz.json()["identities"]["jonas"]["state"] == "expired"
    assert JWT not in healthz.text
    assert JWT not in caplog.text


def test_live_user_forbidden_does_not_escalate(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="wiki-mcp")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authz = request.headers["authorization"]
        calls.append(authz)
        query = request.content.decode()
        if "profile" in query:
            return httpx.Response(200, json=_profile_body())
        return httpx.Response(
            200,
            json={"errors": [{"message": "Forbidden"}], "data": {"pages": {"create": None}}},
        )

    app, _store = _app(tmp_path, handler, fallback=True)
    with client_for_app(app) as client:
        failed = tool_payload(
            call_tool(client, SECRET, "create_page", {"path": "projekt/a", "title": "A", "content": "hej", "tags": ["ett"]})
        )
    assert "error" in failed
    assert f"Bearer {API}" not in calls
    assert all(item == f"Bearer {JWT}" for item in calls)
    assert JWT not in caplog.text
    assert API not in caplog.text


def test_guest_profile_is_rejected(tmp_path):
    wiki = WikiClient("https://wiki.example/graphql", API)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_profile_body({"id": 2, "name": "Guest", "email": "guest@example.com"}),
        )

    wiki._client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(IdentityError, match="riktig användare"):
        validate_user_jwt(wiki, JWT)
    wiki.close()
    assert not (tmp_path / "identities").exists()


def test_missing_wiki_identity_field_stays_optional():
    record = key_record("nuria", "nuria-secret-do-not-log-0123456789", scope="read", author="Nuria")
    parsed = parse_keys([record])
    assert parsed[0].wiki_identity == ""
    record["wiki_identity"] = "nuria"
    assert parse_keys([record])[0].wiki_identity == "nuria"
    record["wiki_identity"] = "../jwt"
    with pytest.raises(ValueError, match="wiki_identity"):
        parse_keys([record])


def test_defaults_are_fallback_and_twelve_hours(monkeypatch):
    monkeypatch.setenv("WIKI_TOKEN", "wiki-token-for-defaults")
    monkeypatch.delenv("IDENTITY_FALLBACK", raising=False)
    monkeypatch.delenv("IDENTITY_RENEW_SECONDS", raising=False)
    settings = Settings.from_env()
    assert settings.identity_fallback is True
    assert settings.identity_renew_seconds == 12 * 60 * 60
    monkeypatch.setenv("IDENTITY_FALLBACK", "false")
    assert Settings.from_env().identity_fallback is False


def test_set_identity_reads_stdin_and_hides_token(tmp_path):
    jwt_box = {"value": ""}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            jwt_box["value"] = self.headers.get("Authorization", "")
            body = json.dumps(_profile_body()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("new-jwt", NEW)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    key_file = _key_file(tmp_path)
    store = str(tmp_path / "identities")
    env = os.environ.copy()
    env["WIKI_URL"] = f"http://127.0.0.1:{port}/graphql"
    env["IDENTITY_KEY_FILE"] = key_file
    env["IDENTITY_STORE_DIR"] = store
    try:
        proc = subprocess.run(
            [sys.executable, "scripts/set_identity.py", "--identity", "jonas", "--key-file", key_file, "--store", store],
            input=JWT + "\n",
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
    assert proc.returncode == 0, proc.stderr
    assert "id: 7" in proc.stdout
    assert "namn: Jonas" in proc.stdout
    assert "e-post: jonas@example.com" in proc.stdout
    assert JWT not in proc.stdout
    assert NEW not in proc.stdout
    assert JWT not in proc.stderr
    assert NEW not in proc.stderr
    assert jwt_box["value"] == f"Bearer {JWT}"
    saved = IdentityStore(store, key_file)
    assert saved.get_token("jonas") == NEW
    assert JWT.encode() not in (tmp_path / "identities" / "jonas.json").read_bytes()
    assert NEW.encode() not in (tmp_path / "identities" / "jonas.json").read_bytes()

    guest = subprocess.run(
        [
            sys.executable,
            "scripts/set_identity.py",
            "--identity",
            "jonas",
            "--wiki-url",
            "http://127.0.0.1:9/graphql",
            "--key-file",
            key_file,
            "--store",
            store,
            JWT,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert guest.returncode == 2
    assert "argument" in guest.stderr
    assert JWT not in guest.stdout


def client_for_app(app):
    from starlette.testclient import TestClient

    return TestClient(app, base_url="http://localhost")
