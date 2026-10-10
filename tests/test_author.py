from __future__ import annotations

import json

import httpx

from app.wiki_client import CHANGE_COMMENT_GRAPHQL_FIELD, WikiClient
from tests.conftest import build, call_tool, client_for, key_record, tool_payload

SECRET = "author-key-do-not-log-0123456789abcd"
CONTENT = "# oförändrat innehåll"
DESCRIPTION = "Beskrivning från anroparen"


def test_author_goes_to_change_comment_not_content(tmp_path):
    harness = build(
        tmp_path,
        [
            key_record("grok-web", SECRET, author="Jonas", prefixes=["projekt/"]),
            key_record("bots", "bots-secret-do-not-log-0123456789abcd", author="", prefixes=["projekt/"]),
        ],
    )
    with client_for(harness) as client:
        created = tool_payload(
            call_tool(
                client,
                SECRET,
                "create_page",
                {
                    "path": "projekt/ny",
                    "title": "Ny",
                    "content": CONTENT,
                    "description": DESCRIPTION,
                    "tags": ["ett"],
                },
            )
        )
        updated = tool_payload(
            call_tool(
                client,
                "bots-secret-do-not-log-0123456789abcd",
                "update_page",
                {"path": "projekt/ny", "content": CONTENT + "\n", "tags": ["ett"], "description": DESCRIPTION},
            )
        )
    assert created["change_comment"]["field"] == CHANGE_COMMENT_GRAPHQL_FIELD
    assert created["change_comment"]["content"] == "MCP: sidan skapades av Jonas (nyckel grok-web)."
    assert harness.wiki.creates[0]["content"] == CONTENT
    assert harness.wiki.creates[0]["description"] == DESCRIPTION
    assert "Jonas" not in harness.wiki.creates[0]["content"]
    assert updated["change_comment"]["content"] == "MCP: sidan uppdaterades av bots (nyckel bots)."
    assert harness.wiki.updates[0]["content"] == CONTENT + "\n"
    assert harness.wiki.updates[0]["description"] == DESCRIPTION
    assert harness.wiki.pages["projekt/ny"]["content"] == CONTENT + "\n"


def test_wiki_client_uses_comments_create_and_leaves_page_fields():
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        calls.append(payload)
        query = payload["query"]
        if "comments" in query:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "comments": {
                            "create": {"responseResult": {"succeeded": True, "message": "ok", "errorCode": 0}, "id": 9}
                        }
                    }
                },
            )
        if "singleByPath" in query:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "pages": {
                            "singleByPath": {
                                "id": 4,
                                "path": "projekt/a",
                                "title": "A",
                                "description": DESCRIPTION,
                                "content": CONTENT,
                                "tags": [{"tag": "ett"}],
                                "locale": "sv",
                            }
                        }
                    }
                },
            )
        if "update(" in query:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "pages": {
                            "update": {
                                "responseResult": {"succeeded": True, "message": "ok", "errorCode": 0},
                                "page": {"id": 4, "path": "projekt/a", "title": "A", "locale": "sv", "updatedAt": "t"},
                            }
                        }
                    }
                },
            )
        if "create(" in query:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "pages": {
                            "create": {
                                "responseResult": {"succeeded": True, "message": "ok", "errorCode": 0},
                                "page": {"id": 4, "path": "projekt/a", "title": "A", "locale": "sv", "updatedAt": "t"},
                            }
                        }
                    }
                },
            )
        raise AssertionError(query)

    comment = "MCP: sidan skapades av Jonas (nyckel jonas)."
    wiki = WikiClient("https://wiki.example/graphql", "wiki-token")
    wiki._client = httpx.Client(transport=httpx.MockTransport(handler))
    created = wiki.create_page(
        path="projekt/a",
        title="A",
        content=CONTENT,
        description=DESCRIPTION,
        tags=["ett"],
        change_comment=comment,
    )
    updated = wiki.update_page(
        path="projekt/a",
        content=CONTENT,
        description=DESCRIPTION,
        tags=["ett"],
        change_comment="MCP: sidan uppdaterades av Jonas (nyckel jonas).",
    )
    wiki.close()

    create_call = next(call for call in calls if "pages" in call["query"] and "create(" in call["query"])
    update_call = next(call for call in calls if "update(" in call["query"])
    comment_calls = [call for call in calls if "comments" in call["query"]]
    assert create_call["variables"]["content"] == CONTENT
    assert create_call["variables"]["description"] == DESCRIPTION
    assert "Jonas" not in create_call["variables"]["content"]
    assert update_call["variables"]["content"] == CONTENT
    assert update_call["variables"]["description"] == DESCRIPTION
    assert comment_calls[0]["variables"] == {"pageId": 4, "content": comment}
    assert "comments" in comment_calls[0]["query"] and "create" in comment_calls[0]["query"]
    assert created["change_comment"]["field"] == CHANGE_COMMENT_GRAPHQL_FIELD
    assert updated["change_comment"]["field"] == CHANGE_COMMENT_GRAPHQL_FIELD
    assert updated["change_comment"]["content"].startswith("MCP: sidan uppdaterades av Jonas")
