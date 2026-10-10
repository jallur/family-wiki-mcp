from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from starlette.testclient import TestClient

from app.config import Settings
from app.digest import sha256_hex
from app.keys import KeyStore
from app.server import create_app
from app.tokens import TokenStore
from app.wiki_client import WikiError


class FakeWiki:
    def __init__(self) -> None:
        self.pages: dict[str, dict] = {}
        self.comments: list[dict] = []
        self.creates: list[dict] = []
        self.updates: list[dict] = []
        self.gets: list[str] = []
        self._next_id = 1

    def close(self) -> None:
        return None

    def seed(self, path: str, title: str, content: str = "hej") -> dict:
        page = {
            "id": self._next_id,
            "path": path,
            "title": title,
            "description": title,
            "content": content,
            "tags": [{"tag": "behåll"}],
            "locale": "sv",
            "updatedAt": "2026-10-10T00:00:00Z",
        }
        self._next_id += 1
        self.pages[path] = page
        return page

    def list_pages(self, limit: int = 200) -> list[dict]:
        return list(self.pages.values())[: max(1, min(limit, 1000))]

    def search(self, query: str) -> dict:
        needle = query.lower()
        results = [
            {"id": page["id"], "path": page["path"], "title": page["title"], "description": page["description"]}
            for page in self.pages.values()
            if needle in page["path"].lower() or needle in page["title"].lower() or needle in page["content"].lower()
        ]
        return {"results": results, "totalHits": len(results)}

    def get_by_path(self, path: str, locale: str | None = None) -> dict | None:
        self.gets.append(path.lstrip("/"))
        return self.pages.get(path.lstrip("/"))

    def create_page(
        self,
        path: str,
        title: str,
        content: str,
        description: str | None = None,
        tags: list[str] | None = None,
        locale: str | None = None,
        change_comment: str | None = None,
    ) -> dict:
        path = path.lstrip("/")
        page = self.seed(path, title, content)
        page["description"] = description or title
        page["tags"] = [{"tag": tag} for tag in (tags or [])]
        self.creates.append(
            {
                "path": path,
                "title": title,
                "content": content,
                "description": description,
                "tags": tags,
                "change_comment": change_comment,
            }
        )
        if change_comment:
            self.comments.append(
                {"pageId": page["id"], "content": change_comment, "field": "comments.create.content"}
            )
        return {
            "responseResult": {"succeeded": True, "message": "ok"},
            "page": {"id": page["id"], "path": path, "title": title},
            "change_comment": {"field": "comments.create.content", "content": change_comment} if change_comment else None,
        }

    def update_page(
        self,
        path: str,
        content: str,
        title: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        locale: str | None = None,
        change_comment: str | None = None,
    ) -> dict:
        path = path.lstrip("/")
        page = self.pages.get(path)
        if page is None:
            raise WikiError(f"Page not found: {path}")
        if tags is None:
            raise WikiError("tags is required on update")
        page["content"] = content
        if title is not None:
            page["title"] = title
        if description is not None:
            page["description"] = description
        self.updates.append(
            {
                "path": path,
                "content": content,
                "description": description,
                "tags": list(tags),
                "change_comment": change_comment,
            }
        )
        if change_comment:
            self.comments.append(
                {"pageId": page["id"], "content": change_comment, "field": "comments.create.content"}
            )
        return {
            "responseResult": {"succeeded": True},
            "page": {"id": page["id"], "path": path, "title": page["title"]},
            "change_comment": {"field": "comments.create.content", "content": change_comment} if change_comment else None,
        }


def make_settings(**overrides) -> Settings:
    data = dict(
        wiki_url="https://wiki.example/graphql",
        wiki_token="wiki-token-not-the-mcp-key",
        mcp_api_key="",
        allowed_prefixes=("projekt/", "familj/", "studier/", "handbok/"),
        locale="sv",
        editor="markdown",
        host="0.0.0.0",
        port=8080,
        public_host="localhost",
        allowed_origins=("https://grok.com", "https://www.grok.com", "https://x.ai"),
        keys_file="",
        token_store_path="",
        allow_legacy_api_key=False,
        forwarded_allow_ips="172.20.0.2",
        identity_key_file="",
        identity_store_dir="",
        identity_fallback=True,
        identity_renew_seconds=0,
    )
    data.update(overrides)
    return Settings(**data)


def key_record(
    name: str,
    secret: str,
    *,
    scope: str = "write",
    prefixes: list[str] | None = None,
    read_prefixes: list[str] | None = None,
    author: str | None = None,
    disabled: bool = False,
    expires: str | None = None,
) -> dict:
    if prefixes is None:
        prefixes = ["projekt/", "familj/", "studier/", "handbok/"] if scope == "write" else []
    return {
        "name": name,
        "sha256": sha256_hex(secret),
        "scope": scope,
        "prefixes": prefixes,
        "read_prefixes": [] if read_prefixes is None else read_prefixes,
        "expires": expires,
        "disabled": disabled,
        "created": "2026-10-10T00:00:00Z",
        "author": name if author is None else author,
    }


@dataclass
class Harness:
    app: object
    wiki: FakeWiki
    settings: Settings
    keys: KeyStore
    tokens: TokenStore
    keys_path: str
    token_path: str


def build(tmp_path, records: list[dict], **settings_overrides) -> Harness:
    keys_path = tmp_path / "keys.json"
    keys_path.write_text(json.dumps(records), encoding="utf-8")
    os.chmod(keys_path, 0o600)
    token_path = tmp_path / "tokens.json"
    settings = make_settings(
        keys_file=str(keys_path),
        token_store_path=str(token_path),
        **settings_overrides,
    )
    wiki = FakeWiki()
    store = KeyStore(str(keys_path))
    tokens = TokenStore(str(token_path))
    store.attach(tokens)
    store.load()
    app = create_app(settings, wiki, store, tokens)
    return Harness(app, wiki, settings, store, tokens, str(keys_path), str(token_path))


def client_for(harness: Harness) -> TestClient:
    return TestClient(harness.app, base_url="http://localhost")


def call_tool(client: TestClient, secret: str, name: str, arguments: dict, *, header: str = "bearer"):
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if header == "bearer":
        headers["Authorization"] = f"Bearer {secret}"
    elif header == "x-api-key":
        headers["X-API-Key"] = secret
    response = client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
    )
    return response


def tool_payload(response) -> dict:
    assert response.status_code == 200, response.text
    body = response.json()
    assert "result" in body, body
    text = body["result"]["content"][0]["text"]
    return json.loads(text)


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def oauth_token(client: TestClient, *, client_id: str, redirect_uri: str, api_key: str) -> str:
    verifier, challenge = pkce_pair()
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "tillstånd",
        "scope": "wiki",
    }
    page = client.get("/oauth/authorize", params=params)
    assert page.status_code == 200, page.text
    assert client_id in page.text
    issued = client.post("/oauth/authorize", data={**params, "api_key": api_key}, follow_redirects=False)
    assert issued.status_code == 302, issued.text
    code = parse_qs(urlparse(issued.headers["location"]).query)["code"][0]
    token_resp = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
            "client_id": client_id,
        },
    )
    assert token_resp.status_code == 200, token_resp.text
    access = token_resp.json()["access_token"]
    assert access != api_key
    return access
