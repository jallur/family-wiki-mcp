from __future__ import annotations

from typing import Any

import httpx


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

    def gql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"query": query}
        if variables is not None:
            payload["variables"] = variables
        r = self._client.post(self.url, json=payload)
        if r.status_code >= 400:
            raise WikiError(f"Wiki HTTP {r.status_code}", r.text)
        data = r.json()
        if data.get("errors"):
            raise WikiError("Wiki GraphQL error", data["errors"])
        return data.get("data") or {}

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
        except WikiError as exc:
            result["change_comment_error"] = str(exc)
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
        data = self.gql(
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
                  page { id path title locale updatedAt }
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
        result = (data.get("pages") or {}).get("create") or {}
        return self._with_change_comment(result, change_comment)

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

        data = self.gql(
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
                  page { id path title locale updatedAt }
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
        result = (data.get("pages") or {}).get("update") or {}
        result["_previous_tags"] = existing_tags
        page_id = (result.get("page") or {}).get("id") or page.get("id")
        return self._with_change_comment(result, change_comment, page_id=page_id)
