"""Notion connector: search, read and write pages and databases.

Uses an internal integration token (``ntn_...``) created in the Notion
dashboard; the integration must be explicitly shared with each page/database it
should see. Read actions are safe; creating pages and appending blocks are
flagged ``dangerous``.
"""

from __future__ import annotations

from typing import Any, List, Optional

import httpx

from config.settings import Settings
from src.connectors.base import Connector, ConnectorAction, Payload
from src.utils.errors import AuthenticationError, ConnectorError, ConnectorTransientError
from src.utils.logger import get_logger
from src.utils.retry import async_retry

LOGGER = get_logger(__name__)

NOTION_ACTIONS: List[ConnectorAction] = [
    ConnectorAction(
        name="search",
        description="Search pages and databases shared with the integration.",
        params={"query": "Search text (empty lists everything).", "limit": "Max results (default 10)."},
    ),
    ConnectorAction(
        name="get_page",
        description="Read a page's properties and its child blocks.",
        params={"page_id": "Notion page id."},
    ),
    ConnectorAction(
        name="query_database",
        description="Query a database and return its pages.",
        params={"database_id": "Notion database id.", "page_size": "Max results (default 20)."},
    ),
    ConnectorAction(
        name="create_page",
        description="Create a page (in a parent page or database) with a title and optional text.",
        params={
            "parent_id": "Parent page or database id.",
            "title": "Page title.",
            "content": "Plain text turned into paragraphs (optional).",
        },
        dangerous=True,
    ),
    ConnectorAction(
        name="append_text",
        description="Append paragraphs to an existing page or block.",
        params={"block_id": "Page/block id.", "text": "Plain text (newlines become paragraphs)."},
        dangerous=True,
    ),
]


class NotionConnector(Connector):
    """Notion workspace access for the connected integration."""

    name = "notion"
    category = "productivity"
    description = "Notion pages and databases shared with the Kollektiv integration."
    required_env = ("NOTION_TOKEN",)

    def __init__(
        self,
        settings: Optional[Settings] = None,
        token_store: Any = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        super().__init__(settings, token_store=token_store, client=client)
        if client is None:
            from src.utils.net import async_client_kwargs

            self._client = httpx.AsyncClient(
                **async_client_kwargs(
                    self.settings,
                    base_url=self.settings.NOTION_BASE_URL.rstrip("/"),
                    timeout=httpx.Timeout(self.settings.NOTION_REQUEST_TIMEOUT, connect=10.0),
                )
            )

    @property
    def token(self) -> str:
        """Integration token: the encrypted store wins over the environment."""
        stored = self.stored_token() or {}
        return str(stored.get("access_token") or stored.get("token") or self.settings.NOTION_TOKEN)

    @property
    def is_configured(self) -> bool:
        """True when an integration token is available."""
        return bool(self.token)

    def _headers(self) -> Payload:
        """Return the request headers Notion requires."""
        if not self.token:
            raise AuthenticationError("NOTION_TOKEN is unset; create an integration token in Notion")
        return {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": self.settings.NOTION_VERSION,
            "Content-Type": "application/json",
        }

    @async_retry(max_retries=3, retry_on=(ConnectorTransientError, httpx.TransportError, httpx.TimeoutException))
    async def _request(self, method: str, path: str, **kwargs: Any) -> Payload:
        """Call the Notion API and translate its errors."""
        response = await self._client.request(method, path, headers=self._headers(), **kwargs)
        if response.status_code in (401, 403):
            raise AuthenticationError(
                f"Notion rejected the token ({response.status_code}); share the page/database with the integration"
            )
        if response.status_code == 404:
            raise ConnectorError(f"Notion object not found at {path}")
        if response.status_code == 429:
            raise ConnectorTransientError("Notion rate limit reached")
        if response.status_code >= 500:
            raise ConnectorTransientError(f"Notion returned {response.status_code}")
        if response.status_code >= 400:
            raise ConnectorError(f"Notion returned {response.status_code}: {response.text[:200]}")
        return response.json() if response.content else {}

    #: A one-result search verifies the token without reading any page body.
    probe_action = "search"
    probe_params = {"limit": 1}

    def actions(self) -> List[ConnectorAction]:
        """Return the Notion actions."""
        return NOTION_ACTIONS

    async def call(self, action: str, params: Payload) -> Any:
        """Perform a Notion action."""
        if action == "search":
            body: Payload = {"page_size": max(1, min(int(params.get("limit") or 10), 100))}
            if params.get("query"):
                body["query"] = params["query"]
            data = await self._request("POST", "/v1/search", json=body)
            return [_notion_object(item) for item in data.get("results", []) or []]
        if action == "get_page":
            page = await self._request("GET", f"/v1/pages/{params['page_id']}")
            blocks = await self._request(
                "GET", f"/v1/blocks/{params['page_id']}/children", params={"page_size": 100}
            )
            return {**_notion_object(page), "blocks": [_notion_block(b) for b in blocks.get("results", []) or []]}
        if action == "query_database":
            data = await self._request(
                "POST",
                f"/v1/databases/{params['database_id']}/query",
                json={"page_size": max(1, min(int(params.get("page_size") or 20), 100))},
            )
            return [_notion_object(item) for item in data.get("results", []) or []]
        if action == "create_page":
            children = _paragraphs(str(params.get("content") or ""))
            body = {
                "parent": _parent(params["parent_id"]),
                "properties": {"title": {"title": [{"type": "text", "text": {"content": str(params["title"])}}]}},
            }
            if children:
                body["children"] = children
            page = await self._request("POST", "/v1/pages", json=body)
            return {"created": True, "id": page.get("id"), "url": page.get("url")}
        if action == "append_text":
            children = _paragraphs(str(params.get("text") or ""))
            if not children:
                raise ConnectorError("append_text needs some text")
            result = await self._request(
                "PATCH", f"/v1/blocks/{params['block_id']}/children", json={"children": children}
            )
            return {"appended": len(result.get("results", []) or []), "block_id": params["block_id"]}
        raise ConnectorError(f"Unhandled Notion action {action!r}")


def _parent(parent_id: str) -> Payload:
    """Build a Notion parent object (database ids are dashed UUIDs)."""
    if "-" in parent_id and len(parent_id.replace("-", "")) == 32:
        return {"type": "page_id", "page_id": parent_id}
    return {"type": "page_id", "page_id": parent_id}


def _paragraphs(text: str, limit: int = 100) -> List[Payload]:
    """Turn plain text into Notion paragraph blocks (one per non-empty line)."""
    return [
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": line}}]}}
        for line in text.splitlines()
        if line.strip()
    ][:limit]


def _notion_object(item: Payload) -> Payload:
    """Return the useful, small shape of a Notion page/database object."""
    properties = item.get("properties") or {}
    title = ""
    for value in properties.values():
        if value.get("type") == "title":
            title = "".join(part.get("plain_text", "") for part in value.get("title", []) or [])
            break
    return {
        "id": item.get("id"),
        "object": item.get("object"),
        "title": title or (item.get("title") or [{}])[0].get("plain_text", "") if item.get("title") else title,
        "url": item.get("url"),
        "last_edited_time": item.get("last_edited_time"),
        "parent": (item.get("parent") or {}).get("type"),
    }


def _notion_block(block: Payload) -> Payload:
    """Return the plain text and type of a Notion block."""
    block_type = block.get("type", "")
    rich = (block.get(block_type) or {}).get("rich_text", []) or []
    return {
        "id": block.get("id"),
        "type": block_type,
        "text": "".join(part.get("plain_text", "") for part in rich),
    }


__all__ = ["NOTION_ACTIONS", "NotionConnector"]
