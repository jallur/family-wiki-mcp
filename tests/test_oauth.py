from __future__ import annotations

import json
import logging

from app.digest import sha256_hex
from tests.conftest import build, call_tool, client_for, key_record, oauth_token, tool_payload

SECRET = "oauth-named-key-do-not-log-0123456789"
GROK_REDIRECT = "https://grok.com/connectors-oauth-exchange-code"
BOTS_REDIRECT = "https://x.ai/oauth/callback"


def test_oauth_per_client_opaque_token_and_revocation(tmp_path, caplog):
    harness = build(tmp_path, [key_record("jonas", SECRET, author="Jonas", prefixes=["projekt/"])])
    harness.wiki.seed("projekt/a", "A")
    caplog.set_level(logging.INFO, logger="wiki-mcp")
    with client_for(harness) as client:
        grok = oauth_token(client, client_id="grok-web", redirect_uri=GROK_REDIRECT, api_key=SECRET)
        bots = oauth_token(client, client_id="bots", redirect_uri=BOTS_REDIRECT, api_key=SECRET)
        assert grok != bots
        assert tool_payload(call_tool(client, grok, "get_page", {"path": "projekt/a"}))["page"]["path"] == "projekt/a"
        assert "key=jonas" in caplog.text
        assert "client=grok-web" in caplog.text
        unknown = client.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": "någon-annan",
                "redirect_uri": GROK_REDIRECT,
                "code_challenge": "abc",
                "code_challenge_method": "S256",
            },
        )
        assert unknown.status_code == 400
        wrong_host = client.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": "grok-web",
                "redirect_uri": BOTS_REDIRECT,
                "code_challenge": "abc",
                "code_challenge_method": "S256",
            },
        )
        assert wrong_host.status_code == 400

        stored = json.loads(open(harness.token_path, encoding="utf-8").read())
        assert grok not in json.dumps(stored)
        assert SECRET not in json.dumps(stored)
        by_client = {row["client_id"]: row for row in stored["tokens"]}
        assert set(by_client) == {"grok-web", "bots"}
        assert by_client["grok-web"]["key_name"] == "jonas"
        assert by_client["grok-web"]["sha256"] == sha256_hex(grok)
        assert by_client["bots"]["sha256"] == sha256_hex(bots)
        assert oct(os_mode(harness.token_path)) == "0o600"

        assert harness.tokens.revoke_hash(sha256_hex(grok)) is True
        assert call_tool(client, grok, "get_page", {"path": "projekt/a"}).status_code == 401
        assert tool_payload(call_tool(client, bots, "get_page", {"path": "projekt/a"}))["page"]["path"] == "projekt/a"

        records = json.loads(open(harness.keys_path, encoding="utf-8").read())
        records[0]["disabled"] = True
        with open(harness.keys_path, "w", encoding="utf-8") as handle:
            json.dump(records, handle)
        os_utime_bump(harness.keys_path)
        assert call_tool(client, bots, "get_page", {"path": "projekt/a"}).status_code == 401
        assert call_tool(client, SECRET, "get_page", {"path": "projekt/a"}).status_code == 401

    again = json.loads(open(harness.token_path, encoding="utf-8").read())
    assert all(row["revoked"] for row in again["tokens"])
    assert SECRET not in caplog.text
    assert grok not in caplog.text
    assert bots not in caplog.text


def os_mode(path: str) -> int:
    import os

    return os.stat(path).st_mode & 0o777


def os_utime_bump(path: str) -> None:
    import os

    os.utime(path, (2_100_000_000, 2_100_000_000))
