from __future__ import annotations

import json
import logging
import os
import signal

from app.digest import sha256_hex
from app.oauth import WWW_AUTHENTICATE
from tests.conftest import build, call_tool, client_for, key_record, tool_payload

SECRET = "s3cret-do-not-log-ZZZ-0123456789abcd"
READ_SECRET = "read-only-secret-do-not-log-0123456789"


def test_www_authenticate_and_health(tmp_path):
    harness = build(tmp_path, [key_record("grok-web", SECRET, author="Jonas")])
    with client_for(harness) as client:
        health = client.get("/health")
        assert health.status_code == 200
        denied = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}})
        assert denied.status_code == 401
        header = denied.headers["www-authenticate"]
        assert header.startswith('Bearer resource_metadata="')
        assert "FAKESECRET_" not in header
        assert header == WWW_AUTHENTICATE


def test_bearer_and_x_api_key_and_hash_is_not_the_secret(tmp_path, caplog):
    harness = build(tmp_path, [key_record("grok-web", SECRET, author="Jonas")])
    harness.wiki.seed("projekt/a", "A")
    caplog.set_level(logging.INFO, logger="wiki-mcp")
    with client_for(harness) as client:
        via_bearer = tool_payload(call_tool(client, SECRET, "get_page", {"path": "projekt/a"}))
        via_header = tool_payload(
            call_tool(client, SECRET, "get_page", {"path": "projekt/a"}, header="x-api-key")
        )
        hashed = call_tool(client, sha256_hex(SECRET), "get_page", {"path": "projekt/a"})
    assert "page" in via_bearer and "page" in via_header
    assert hashed.status_code == 401
    assert SECRET not in caplog.text
    assert "key=grok-web" in caplog.text
    assert "client=-" in caplog.text
    assert "tool=get_page" in caplog.text
    assert "path=projekt/a" in caplog.text
    assert "result=ok" in caplog.text


def test_scope_and_prefix(tmp_path, caplog):
    harness = build(
        tmp_path,
        [
            key_record("openwebui", READ_SECRET, scope="read", prefixes=[], author="OpenWebUI"),
            key_record(
                "jonas",
                SECRET,
                scope="write",
                prefixes=["projekt/"],
                author="Jonas",
            ),
        ],
    )
    caplog.set_level(logging.INFO, logger="wiki-mcp")
    with client_for(harness) as client:
        denied_scope = tool_payload(
            call_tool(client, READ_SECRET, "create_page", {"path": "projekt/ny", "title": "Ny", "content": "# hej"})
        )
        denied_prefix = tool_payload(
            call_tool(
                client,
                SECRET,
                "create_page",
                {"path": "familj/ny", "title": "Ny", "content": "# hej"},
            )
        )
        created = tool_payload(
            call_tool(
                client,
                SECRET,
                "create_page",
                {"path": "projekt/ny", "title": "Ny", "content": "# hej", "description": "Beskrivning"},
            )
        )
    assert denied_scope["error"] == "write scope required"
    assert denied_prefix["error"] == "path not allowed"
    assert created["responseResult"]["succeeded"] is True
    assert harness.wiki.creates[0]["content"] == "# hej"
    assert harness.wiki.creates[0]["path"] == "projekt/ny"
    assert "result=denied" in caplog.text
    assert READ_SECRET not in caplog.text
    assert SECRET not in caplog.text


def test_read_prefixes_filter_and_empty_means_all(tmp_path):
    limited = "limited-read-secret-0123456789abcd"
    open_secret = "open-read-secret-0123456789abcd"
    harness = build(
        tmp_path,
        [
            key_record("nuria", limited, scope="read", prefixes=[], read_prefixes=["projekt/"], author="Nuria"),
            key_record("jonas", open_secret, scope="read", prefixes=[], read_prefixes=[], author="Jonas"),
        ],
    )
    harness.wiki.seed("projekt/a", "Alpha", "alfa")
    harness.wiki.seed("familj/b", "Beta", "beta")
    before_gets = len(harness.wiki.gets)
    with client_for(harness) as client:
        hidden = tool_payload(call_tool(client, limited, "get_page", {"path": "familj/b"}))
        visible = tool_payload(call_tool(client, limited, "get_page", {"path": "projekt/a"}))
        listed = tool_payload(call_tool(client, limited, "list_pages", {}))
        found = tool_payload(call_tool(client, limited, "search_pages", {"query": "a"}))
        everything = tool_payload(call_tool(client, open_secret, "list_pages", {}))
    assert hidden["error"] == "path not allowed"
    assert harness.wiki.gets[before_gets:] == ["projekt/a"]
    assert visible["page"]["path"] == "projekt/a"
    assert [page["path"] for page in listed["pages"]] == ["projekt/a"]
    assert [row["path"] for row in found["results"]] == ["projekt/a"]
    assert found["totalHits"] == 1
    assert {page["path"] for page in everything["pages"]} == {"projekt/a", "familj/b"}


def test_expired_and_disabled(tmp_path):
    harness = build(
        tmp_path,
        [
            key_record("old", SECRET, expires="2020-01-01T00:00:00Z"),
            key_record("off", READ_SECRET, disabled=True),
        ],
    )
    with client_for(harness) as client:
        assert call_tool(client, SECRET, "get_page", {"path": "projekt/a"}).status_code == 401
        assert call_tool(client, READ_SECRET, "get_page", {"path": "projekt/a"}).status_code == 401


def test_reload_mtime_and_broken_json_keeps_previous(tmp_path, caplog):
    harness = build(tmp_path, [key_record("grok-web", SECRET, author="Jonas")])
    harness.wiki.seed("projekt/a", "A")
    caplog.set_level(logging.ERROR, logger="wiki-mcp")
    new_secret = "new-key-secret-do-not-log-0123456789"
    with client_for(harness) as client:
        assert "page" in tool_payload(call_tool(client, SECRET, "get_page", {"path": "projekt/a"}))
        path = harness.keys_path
        original = open(path, encoding="utf-8").read()
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{")
        os.utime(path, (2_000_000_000, 2_000_000_000))
        assert "page" in tool_payload(call_tool(client, SECRET, "get_page", {"path": "projekt/a"}))
        assert "keys.json är trasig" in caplog.text
        assert call_tool(client, new_secret, "get_page", {"path": "projekt/a"}).status_code == 401
        records = json.loads(original)
        records.append(key_record("bots", new_secret, author="Bots"))
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(records, handle)
        os.utime(path, (2_000_000_100, 2_000_000_100))
        assert "page" in tool_payload(call_tool(client, new_secret, "get_page", {"path": "projekt/a"}))
    assert SECRET not in caplog.text
    assert new_secret not in caplog.text


def test_sighup_reloads_when_mtime_is_unchanged(tmp_path):
    harness = build(tmp_path, [key_record("grok-web", SECRET)])
    new_secret = "sighup-secret-do-not-log-0123456789"
    st = os.stat(harness.keys_path)
    with open(harness.keys_path, "w", encoding="utf-8") as handle:
        json.dump([key_record("bots", new_secret)], handle)
    os.utime(harness.keys_path, (st.st_atime, st.st_mtime))
    harness.keys.maybe_reload()
    assert harness.keys.by_name("bots") is None
    previous = signal.getsignal(signal.SIGHUP)
    signal.signal(signal.SIGHUP, harness.keys.on_sighup)
    try:
        os.kill(os.getpid(), signal.SIGHUP)
    finally:
        signal.signal(signal.SIGHUP, previous)
    assert harness.keys.by_name("bots") is not None
    assert harness.keys.by_name("grok-web") is None


def test_legacy_full_write_and_flag(tmp_path, caplog):
    legacy = "legacy-mcp-api-key-do-not-log-0123456789"
    harness = build(
        tmp_path,
        [],
        mcp_api_key=legacy,
        allow_legacy_api_key=True,
    )
    caplog.set_level(logging.INFO, logger="wiki-mcp")
    with client_for(harness) as client:
        created = tool_payload(
            call_tool(
                client,
                legacy,
                "create_page",
                {"path": "handbok/ny", "title": "Handbok", "content": "# legacy"},
            )
        )
        assert created["responseResult"]["succeeded"] is True
        listed = tool_payload(call_tool(client, legacy, "allowed_write_prefixes", {}))
        assert "handbok/" in listed["allowed_prefixes"]
        assert listed["scope"] == "write"
    assert "legacy MCP_API_KEY accepterades med full skrivrätt" in caplog.text
    assert legacy not in caplog.text
    assert harness.wiki.creates[0]["content"] == "# legacy"

    off = tmp_path / "off"
    off.mkdir()
    blocked = build(off, [], mcp_api_key=legacy, allow_legacy_api_key=False)
    with client_for(blocked) as client:
        assert call_tool(client, legacy, "get_page", {"path": "projekt/a"}).status_code == 401
