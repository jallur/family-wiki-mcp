from __future__ import annotations

import contextvars
import logging
from collections.abc import Callable
from typing import Any

import httpx

log = logging.getLogger("wiki-mcp")

# Sätts bara under ett anrop som ska gå som en Wiki.js-användare.
# Tomt värde betyder API-nyckeln. new-jwt sparas av callbacken och loggas inte.
wiki_bearer: contextvars.ContextVar[str | None] = contextvars.ContextVar("wiki_bearer", default=None)
wiki_on_new_jwt: contextvars.ContextVar[Callable[[str], None] | None] = contextvars.ContextVar(
    "wiki_on_new_jwt", default=None
)

PROFILE_QUERY = """
query {
  users {
    profile { id name email }
  }
}
"""


# Wiki.js 2.5.315: pages.create och pages.update har inget fält för författare
# eller ändringskommentar. description är sidans sammanfattning. Ändringskommentaren
# skrivs i comments.create.content och sidans content lämnas oförändrat.
CHANGE_COMMENT_GRAPHQL_FIELD = "comments.create.content"


class WikiError(Exception):
    def __init__(self, message: str, details: Any = None):
        super().__init__(message)
        self.details = details


class WikiClient:
    def __init__(self, url: str, token: str, locale: str = "sv", editor: str = "markdown"):
        self.url = url
        self.token = token
        self.locale = locale
        self.editor = editor
        self._client = httpx.Client(
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )

    def close(self) -> None:
        self._client.close()

    def _exchange(
        self,
        token: str | None,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        """POST som API-nyckel eller som token. Sparar new-jwt via callback, utan att logga den."""
        used = token if token else (wiki_bearer.get() or self.token)
        payload: dict[str, Any] = {"query": query}
        if variables is not None:
            payload["variables"] = variables
        response = self._client.post(
            self.url,
            json=payload,
            headers={
                "Authorization": f"Bearer {used}",
                "Content-Type": "application/json",
            },
        )
        renewed = (response.headers.get("new-jwt") or "").strip() or None
        if renewed:
            callback = wiki_on_new_jwt.get()
            if callback is not None:
                try:
                    callback(renewed)
                except Exception:
                    log.error("wiki-identitet: new-jwt kunde inte sparas")
        if response.status_code in (401, 403):
            raise WikiError(f"Wiki HTTP {response.status_code}", {"status": response.status_code})
        if response.status_code >= 400:
            raise WikiError(f"Wiki HTTP {response.status_code}", response.text[:500])
        body = response.json()
        if not isinstance(body, dict):
            raise WikiError("Wiki JSON-svar är ogiltigt", body)
        return body, renewed

    def _post(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        body, _renewed = self._exchange(None, query, variables)
        return body

    def profile_as(self, token: str) -> tuple[dict[str, Any] | None, str | None]:
        """Läs users.profile med en användar-JWT. Returnerar (profil eller None, new-jwt eller None)."""
        body, renewed = self._exchange(token, PROFILE_QUERY, None)
        if body.get("errors"):
            return None, renewed
        profile = ((body.get("data") or {}).get("users") or {}).get("profile")
        if not isinstance(profile, dict):
            return None, renewed
        return profile, renewed

    def gql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        body = self._post(query, variables)
        if body.get("errors"):
            raise WikiError("Wiki GraphQL error", body["errors"])
        return body.get("data") or {}

    def id_by_path(self, path: str, locale: str | None = None) -> int | None:
        """Hämta bara sid-id. Urvalet har inga fält som Wiki.js kan lämna null."""
        loc = locale or self.locale
        body = self._post(
            """
            query($p: String!, $l: String!) {
              pages {
                singleByPath(path: $p, locale: $l) { id }
              }
            }
            """,
            {"p": path.lstrip("/"), "l": loc},
        )
        page = ((body.get("data") or {}).get("pages") or {}).get("singleByPath") or {}
        if isinstance(page, dict) and page.get("id"):
            return int(page["id"])
        return None

    def list_pages(self, limit: int = 200) -> list[dict[str, Any]]:
        data = self.gql(
            """
            query {
              pages {
                list(orderBy: TITLE) {
                  id path title description updatedAt locale
                }
              }
            }
            """
        )
        pages = (data.get("pages") or {}).get("list") or []
        return pages[: max(1, min(limit, 1000))]

    def search(self, query: str) -> dict[str, Any]:
        data = self.gql(
            """
            query($q: String!) {
              pages {
                search(query: $q) {
                  results { id path title description }
                  totalHits
                }
              }
            }
            """,
            {"q": query},
        )
        return (data.get("pages") or {}).get("search") or {"results": [], "totalHits": 0}

    def get_by_path(self, path: str, locale: str | None = None) -> dict[str, Any] | None:
        loc = locale or self.locale
        data = self.gql(
            """
            query($p: String!, $l: String!) {
              pages {
                singleByPath(path: $p, locale: $l) {
                  id path title description content updatedAt editor locale
                  tags { id tag title }
                }
              }
            }
            """,
            {"p": path.lstrip("/"), "l": loc},
        )
        return (data.get("pages") or {}).get("singleByPath")

    def add_change_comment(self, page_id: int, content: str) -> dict[str, Any]:
        """Skriv ändringskommentar i comments.create.content. Rör inte sidans content."""
        data = self.gql(
            """
            mutation($pageId: Int!, $content: String!) {
              comments {
                create(pageId: $pageId, content: $content) {
                  responseResult { succeeded message errorCode }
                  id
                }
              }
            }
            """,
            {"pageId": page_id, "content": content},
        )
        return (data.get("comments") or {}).get("create") or {}

    def _with_change_comment(
        self,
        result: dict[str, Any],
        change_comment: str | None,
        page_id: int | None = None,
    ) -> dict[str, Any]:
        if not change_comment:
            return result
        result = dict(result)
        if page_id is None:
            page_id = (result.get("page") or {}).get("id")
        if not page_id:
            result["change_comment_error"] = "sid-id saknas, ändringskommentar skrevs inte"
            return result
        try:
            response = self.add_change_comment(int(page_id), change_comment)
        except Exception as exc:
            message = _comment_error_text(exc)
            log.warning("ändringskommentar misslyckades page_id=%s error=%s", page_id, message)
            result["change_comment_error"] = message
            return result
        inner = response.get("responseResult") if isinstance(response, dict) else None
        if isinstance(inner, dict) and inner.get("succeeded") is False:
            message = str(inner.get("message") or "ändringskommentar avböjdes")
            log.warning("ändringskommentar misslyckades page_id=%s error=%s", page_id, message)
            result["change_comment_error"] = message
            return result
        result["change_comment"] = {
            "field": CHANGE_COMMENT_GRAPHQL_FIELD,
            "content": change_comment,
            "response": response,
        }
        return result

    def create_page(
        self,
        path: str,
        title: str,
        content: str,
        description: str | None = None,
        tags: list[str] | None = None,
        locale: str | None = None,
        change_comment: str | None = None,
    ) -> dict[str, Any]:
        loc = locale or self.locale
        body = self._post(
            """
            mutation(
              $path: String!, $title: String!, $content: String!,
              $description: String!, $tags: [String]!, $locale: String!, $editor: String!
            ) {
              pages {
                create(
                  content: $content
                  description: $description
                  editor: $editor
                  isPublished: true
                  isPrivate: false
                  locale: $locale
                  path: $path
                  tags: $tags
                  title: $title
                ) {
                  responseResult { succeeded message errorCode }
                  page { id }
                }
              }
            }
            """,
            {
                "path": path.lstrip("/"),
                "title": title,
                "content": content,
                "description": description or title,
                "tags": tags or [],
                "locale": loc,
                "editor": self.editor,
            },
        )
        result, page_id = self._written_page(body, "create", path, loc)
        return self._with_change_comment(result, change_comment, page_id=page_id)

    def update_page(
        self,
        path: str,
        content: str,
        title: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        locale: str | None = None,
        change_comment: str | None = None,
    ) -> dict[str, Any]:
        loc = locale or self.locale
        page = self.get_by_path(path, loc)
        if not page and loc != "en":
            page = self.get_by_path(path, "en")
            if page:
                loc = "en"
        if not page:
            raise WikiError(f"Page not found: {path} (locale={loc})")

        existing_tags = [t.get("tag") for t in (page.get("tags") or []) if t.get("tag")]
        if tags is None:
            raise WikiError("tags is required on update (pass existing tags to preserve)")
        if not isinstance(tags, list):
            raise WikiError("tags must be a list of strings")

        body = self._post(
            """
            mutation(
              $id: Int!, $path: String!, $title: String!, $description: String!,
              $content: String!, $tags: [String]!, $locale: String!, $editor: String!
            ) {
              pages {
                update(
                  id: $id
                  content: $content
                  title: $title
                  description: $description
                  editor: $editor
                  isPublished: true
                  isPrivate: false
                  locale: $locale
                  path: $path
                  tags: $tags
                ) {
                  responseResult { succeeded message errorCode }
                  page { id }
                }
              }
            }
            """,
            {
                "id": page["id"],
                "path": path.lstrip("/"),
                "title": title or page["title"],
                "description": description if description is not None else (page.get("description") or page["title"]),
                "content": content,
                "tags": tags,
                "locale": loc,
                "editor": self.editor,
            },
        )
        result, page_id = self._written_page(body, "update", path, loc, known_id=page.get("id"))
        result["_previous_tags"] = existing_tags
        return self._with_change_comment(result, change_comment, page_id=page_id)

    def _written_page(
        self,
        body: dict[str, Any],
        operation: str,
        path: str,
        locale: str,
        known_id: int | None = None,
    ) -> tuple[dict[str, Any], int]:
        """Tolka mutationssvaret. Null i urvalet får inte dölja en lyckad skrivning."""
        errors = body.get("errors") or []
        node = ((body.get("data") or {}).get("pages") or {}).get(operation) or {}
        if not isinstance(node, dict):
            node = {}
        response = node.get("responseResult")
        if isinstance(response, dict) and response.get("succeeded") is False:
            raise WikiError(str(response.get("message") or "Wiki avböjde skrivningen"), response)
        if errors and not _only_null_selection_errors(errors):
            raise WikiError("Wiki GraphQL error", errors)
        page_id = _page_id(node) or (int(known_id) if known_id else None)
        if page_id is None:
            page_id = self.id_by_path(path, locale)
        if page_id is None:
            raise WikiError("Wiki GraphQL error", errors or "sid-id saknas")
        result = dict(node)
        if not isinstance(response, dict):
            result["responseResult"] = {"succeeded": True, "message": "ok", "errorCode": 0}
        page = result.get("page") if isinstance(result.get("page"), dict) else {}
        if not page.get("id"):
            result["page"] = {"id": page_id}
        return result, page_id


def _comment_error_text(exc: Exception) -> str:
    details = getattr(exc, "details", None)
    if isinstance(details, list):
        parts = [
            str(err["message"])
            for err in details
            if isinstance(err, dict) and err.get("message")
        ]
        if parts:
            return "; ".join(parts)
    return str(exc)


def _only_null_selection_errors(errors: Any) -> bool:
    if not isinstance(errors, list) or not errors:
        return False
    for err in errors:
        message = err.get("message") if isinstance(err, dict) else str(err)
        if "Cannot return null for non-nullable field" not in str(message):
            return False
    return True


def _page_id(node: dict[str, Any]) -> int | None:
    page = node.get("page")
    if isinstance(page, dict) and page.get("id"):
        return int(page["id"])
    return None
