from __future__ import annotations

import json
import logging
import os
import subprocess
import sys

from app.digest import sha256_hex
from app.tokens import TokenStore
from tests.conftest import build, call_tool, client_for, key_record, oauth_token, tool_payload

JONAS = "jonas-revoke-secret-do-not-log-0123456789"
GROK = "grok-revoke-secret-do-not-log-0123456789"
REDIRECT = "https://grok.com/connectors-oauth-exchange-code"


def _bump(path: str, when: int = 2_100_000_000) -> None:
    os.utime(path, (when, when))


def _stored(path: str) -> list[dict]:
    return json.loads(open(path, encoding="utf-8").read())["tokens"]


def test_cli_revokes_by_hash_key_and_client(tmp_path):
    path = tmp_path / "oauth-tokens.json"
    store = TokenStore(str(path))
    jonas_a = store.issue(key_name="jonas", client_id="jonas")
    jonas_b = store.issue(key_name="jonas", client_id="grok-web")
    bots = store.issue(key_name="bots", client_id="grok-web")
    other = store.issue(key_name="nuria", client_id="nuria")

    by_hash = subprocess.run(
        [sys.executable, "scripts/revoke_token.py", "--file", str(path), "--hash", sha256_hex(jonas_a).upper()],
        capture_output=True,
        text=True,
        check=False,
    )
    assert by_hash.returncode == 0, by_hash.stderr
    assert "via hash" in by_hash.stdout
    for secret in (jonas_a, jonas_b, bots, other):
        assert secret not in by_hash.stdout
        assert secret not in by_hash.stderr
    rows = {row["sha256"]: row for row in _stored(str(path))}
    assert rows[sha256_hex(jonas_a)]["revoked"] is True
    assert rows[sha256_hex(jonas_b)]["revoked"] is False
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"

    again = subprocess.run(
        [sys.executable, "scripts/revoke_token.py", "--file", str(path), "--hash", sha256_hex(jonas_a)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert again.returncode == 1

    by_client = subprocess.run(
        [sys.executable, "scripts/revoke_token.py", "--file", str(path), "--client", "grok-web"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert by_client.returncode == 0, by_client.stderr
    rows = {row["sha256"]: row for row in _stored(str(path))}
    assert rows[sha256_hex(jonas_b)]["revoked"] is True
    assert rows[sha256_hex(bots)]["revoked"] is True
    assert rows[sha256_hex(other)]["revoked"] is False
    assert jonas_b not in by_client.stdout

    by_key = subprocess.run(
        [sys.executable, "scripts/revoke_token.py", "--file", str(path), "--key", "nuria"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert by_key.returncode == 0, by_key.stderr
    rows = {row["sha256"]: row for row in _stored(str(path))}
    assert rows[sha256_hex(other)]["revoked"] is True
    assert "nuria" in by_key.stdout


def test_cli_rejects_bad_arguments(tmp_path):
    path = tmp_path / "oauth-tokens.json"
    TokenStore(str(path)).issue(key_name="jonas", client_id="jonas")
    missing = subprocess.run(
        [sys.executable, "scripts/revoke_token.py", "--file", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    both = subprocess.run(
        [
            sys.executable,
            "scripts/revoke_token.py",
            "--file",
            str(path),
            "--hash",
            "ab" * 32,
            "--key",
            "jonas",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    short = subprocess.run(
        [sys.executable, "scripts/revoke_token.py", "--file", str(path), "--hash", "abcd"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing.returncode == 2
    assert both.returncode == 2
    assert short.returncode == 2
    assert "hex" in short.stderr
    assert all(row["revoked"] is False for row in _stored(str(path)))


def test_token_file_mtime_reload_revokes_one_without_restart(tmp_path, caplog):
    harness = build(
        tmp_path,
        [
            key_record("jonas", JONAS, author="Jonas", prefixes=["projekt/"]),
            key_record("grok-web", GROK, author="Grok", prefixes=["projekt/"]),
        ],
    )
    harness.wiki.seed("projekt/a", "A")
    caplog.set_level(logging.ERROR, logger="wiki-mcp")
    with client_for(harness) as client:
        assert client.post("/oauth/revoke").status_code == 404
        jonas = oauth_token(client, client_id="family-wiki-mcp", redirect_uri=REDIRECT, api_key=JONAS)
        second = oauth_token(client, client_id="family-wiki-mcp", redirect_uri=REDIRECT, api_key=JONAS)
        grok = oauth_token(client, client_id="grok-web", redirect_uri=REDIRECT, api_key=GROK)
        assert tool_payload(call_tool(client, jonas, "get_page", {"path": "projekt/a"}))["page"]["path"] == "projekt/a"

        stored = json.loads(open(harness.token_path, encoding="utf-8").read())
        for row in stored["tokens"]:
            if row["sha256"] == sha256_hex(jonas):
                row["revoked"] = True
        with open(harness.token_path, "w", encoding="utf-8") as handle:
            json.dump(stored, handle)
        _bump(harness.token_path)

        assert call_tool(client, jonas, "get_page", {"path": "projekt/a"}).status_code == 401
        assert tool_payload(call_tool(client, second, "get_page", {"path": "projekt/a"}))["page"]["path"] == "projekt/a"
        assert tool_payload(call_tool(client, grok, "get_page", {"path": "projekt/a"}))["page"]["path"] == "projekt/a"
        assert jonas not in caplog.text
        assert JONAS not in caplog.text

        with open(harness.token_path, "w", encoding="utf-8") as handle:
            handle.write("{")
        _bump(harness.token_path, 2_100_000_100)
        assert tool_payload(call_tool(client, second, "get_page", {"path": "projekt/a"}))["page"]["path"] == "projekt/a"
        assert "tokenfilen är trasig, behåller föregående lista" in caplog.text
        logged = caplog.text.count("tokenfilen är trasig")
        assert tool_payload(call_tool(client, grok, "get_page", {"path": "projekt/a"}))["page"]["path"] == "projekt/a"
        assert caplog.text.count("tokenfilen är trasig") == logged


def test_disabled_key_revokes_its_tokens_immediately(tmp_path):
    harness = build(
        tmp_path,
        [
            key_record("jonas", JONAS, author="Jonas", prefixes=["projekt/"]),
            key_record("grok-web", GROK, author="Grok", prefixes=["projekt/"]),
        ],
    )
    harness.wiki.seed("projekt/a", "A")
    with client_for(harness) as client:
        first = oauth_token(client, client_id="family-wiki-mcp", redirect_uri=REDIRECT, api_key=JONAS)
        second = oauth_token(client, client_id="bots", redirect_uri="https://x.ai/oauth/callback", api_key=JONAS)
        grok = oauth_token(client, client_id="grok-web", redirect_uri=REDIRECT, api_key=GROK)

        records = json.loads(open(harness.keys_path, encoding="utf-8").read())
        for record in records:
            if record["name"] == "jonas":
                record["disabled"] = True
        with open(harness.keys_path, "w", encoding="utf-8") as handle:
            json.dump(records, handle)
        _bump(harness.keys_path)

        assert call_tool(client, first, "get_page", {"path": "projekt/a"}).status_code == 401
        assert call_tool(client, second, "get_page", {"path": "projekt/a"}).status_code == 401
        assert call_tool(client, JONAS, "get_page", {"path": "projekt/a"}).status_code == 401
        assert tool_payload(call_tool(client, grok, "get_page", {"path": "projekt/a"}))["page"]["path"] == "projekt/a"

        rows = _stored(harness.token_path)
        by_hash = {row["sha256"]: row for row in rows}
        assert by_hash[sha256_hex(first)]["revoked"] is True
        assert by_hash[sha256_hex(second)]["revoked"] is True
        assert by_hash[sha256_hex(grok)]["revoked"] is False

        for record in records:
            if record["name"] == "jonas":
                record["disabled"] = False
        with open(harness.keys_path, "w", encoding="utf-8") as handle:
            json.dump(records, handle)
        _bump(harness.keys_path, 2_100_000_050)
        assert tool_payload(call_tool(client, JONAS, "get_page", {"path": "projekt/a"}))["page"]["path"] == "projekt/a"
        assert call_tool(client, first, "get_page", {"path": "projekt/a"}).status_code == 401
        assert call_tool(client, second, "get_page", {"path": "projekt/a"}).status_code == 401
