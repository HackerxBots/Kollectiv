"""Linear connector: read and file issues through the GraphQL API.

Linear has a single endpoint (``POST /graphql``) and a personal API key
(``lin_api_...``, created in Settings → API → Personal API keys). Everything is
one query away, which is why this connector builds small, readable GraphQL
documents instead of a generic query pass-through: a tool an agent can call is
worth more than a query language it has to get right.

Reading is safe; creating issues and commenting are flagged ``dangerous``.
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

LINEAR_ACTIONS: List[ConnectorAction] = [
    ConnectorAction(
        name="viewer",
        description="Return the authenticated user and their organization (connectivity check).",
        params={},
    ),
    ConnectorAction(
        name="list_teams",
        description="List teams in the workspace (their ids are needed to create issues).",
        params={},
    ),
    ConnectorAction(
        name="list_issues",
        description="List issues, newest first, optionally filtered by team or state.",
        params={
            "limit": "Max issues (default 20, max 100).",
            "team_id": "Optional team id filter.",
            "state": "Optional workflow state name filter (e.g. 'In Progress').",
        },
    ),
    ConnectorAction(
        name="create_issue",
        description="File an issue in a team.",
        params={
            "title": "Issue title.",
            "description": "Issue body (Markdown).",
            "team_id": "Team id; defaults to LINEAR_TEAM_ID.",
            "priority": "Optional priority 0-4 (1 = urgent).",
        },
        dangerous=True,
    ),
    ConnectorAction(
        name="comment_issue",
        description="Add a comment to an existing issue.",
        params={"issue_id": "Issue id (or its identifier, e.g. ENG-42).", "body": "Comment text."},
        dangerous=True,
    ),
]

#: GraphQL documents kept as constants so tests can assert on the shape.
VIEWER_QUERY = "query Viewer { viewer { id name email } organization { id name urlKey } }"
TEAMS_QUERY = "query Teams { teams { nodes { id name key } } }"
ISSUES_QUERY = """
query Issues($first: Int!, $teamId: String, $state: String) {
  issues(first: $first, filter: {team: {id: {eq: $teamId}}, state: {name: {eq: $state}}}) {
    nodes { id identifier title state { name } team { id key name } assignee { name } url updatedAt }
  }
}
"""
CREATE_ISSUE_MUTATION = """
mutation CreateIssue($input: IssueCreateInput!) {
  issueCreate(input: $input) { success issue { id identifier title url } }
}
"""
COMMENT_MUTATION = """
mutation Comment($input: CommentCreateInput!) {
  commentCreate(input: $input) { success comment { id url } }
}
"""


class LinearConnector(Connector):
    """Linear GraphQL access for one personal API key."""

    name = "linear"
    category = "issues"
    description = "Linear: list and file issues, comment on them."
    required_env = ("LINEAR_API_KEY",)

    def __init__(
        self,
        settings: Optional[Settings] = None,
        token_store: Any = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        """Build the connector; the client is injectable for tests.

        Args:
            settings: Optional settings override.
            token_store: Optional encrypted token store.
            client: Optional pre-built client (tests); one is created otherwise.
        """
        super().__init__(settings, token_store=token_store, client=client)
        if client is None:
            from src.utils.net import async_client_kwargs

            self._client = httpx.AsyncClient(
                **async_client_kwargs(
                    self.settings,
                    base_url=self.settings.LINEAR_BASE_URL.rstrip("/"),
                    timeout=httpx.Timeout(self.settings.LINEAR_REQUEST_TIMEOUT, connect=10.0),
                )
            )

    @property
    def token(self) -> str:
        """API key: the encrypted store wins over the environment."""
        stored = self.stored_token() or {}
        return str(stored.get("access_token") or stored.get("token") or self.settings.LINEAR_API_KEY)

    @property
    def is_configured(self) -> bool:
        """True when an API key is available."""
        return bool(self.token)

    #: The viewer query proves the key works and returns no issue data.
    probe_action = "viewer"
    probe_params: Payload = {}

    @async_retry(max_retries=3, retry_on=(ConnectorTransientError, httpx.TransportError, httpx.TimeoutException))
    async def _graphql(self, query: str, variables: Optional[Payload] = None) -> Payload:
        """Run one GraphQL document and return its ``data`` payload.

        Args:
            query: The GraphQL document.
            variables: Variables for the document.

        Returns:
            The ``data`` object.

        Raises:
            AuthenticationError: When Linear rejects the key.
            ConnectorTransientError: On rate limits and 5xx responses.
            ConnectorError: For GraphQL errors or other failures.
        """
        if not self.token:
            raise AuthenticationError("LINEAR_API_KEY is unset; create a personal API key in Linear settings")
        response = await self._client.post(
            "/graphql",
            json={"query": query, "variables": variables or {}},
            headers={"Authorization": self.token, "Content-Type": "application/json"},
        )
        if response.status_code in (401, 403):
            raise AuthenticationError("Linear rejected the API key")
        if response.status_code == 429:
            raise ConnectorTransientError("Linear rate limit reached")
        if response.status_code >= 500:
            raise ConnectorTransientError(f"Linear returned {response.status_code}")
        if response.status_code >= 400:
            raise ConnectorError(f"Linear returned {response.status_code}: {response.text[:200]}")
        payload = response.json() if response.content else {}
        if payload.get("errors"):
            messages = "; ".join(str(item.get("message")) for item in payload["errors"][:3])
            raise ConnectorError(f"Linear GraphQL error: {messages}")
        return payload.get("data") or {}

    def actions(self) -> List[ConnectorAction]:
        """Return the Linear actions."""
        return LINEAR_ACTIONS

    async def call(self, action: str, params: Payload) -> Any:
        """Perform a Linear action.

        Args:
            action: Action name from :data:`LINEAR_ACTIONS`.
            params: Action parameters.

        Returns:
            A JSON-friendly result.

        Raises:
            ConnectorError: For unknown actions or missing required parameters.
        """
        if action == "viewer":
            data = await self._graphql(VIEWER_QUERY)
            viewer = data.get("viewer") or {}
            organization = data.get("organization") or {}
            return {
                "user": {"id": viewer.get("id"), "name": viewer.get("name"), "email": viewer.get("email")},
                "organization": {
                    "id": organization.get("id"),
                    "name": organization.get("name"),
                    "url_key": organization.get("urlKey"),
                },
            }
        if action == "list_teams":
            data = await self._graphql(TEAMS_QUERY)
            nodes = ((data.get("teams") or {}).get("nodes")) or []
            return [{"id": node.get("id"), "name": node.get("name"), "key": node.get("key")} for node in nodes]
        if action == "list_issues":
            variables: Payload = {"first": max(1, min(int(params.get("limit") or 20), 100))}
            if params.get("team_id"):
                variables["teamId"] = str(params["team_id"])
            if params.get("state"):
                variables["state"] = str(params["state"])
            data = await self._graphql(ISSUES_QUERY, variables)
            nodes = ((data.get("issues") or {}).get("nodes")) or []
            return [
                {
                    "id": node.get("id"),
                    "identifier": node.get("identifier"),
                    "title": node.get("title"),
                    "state": (node.get("state") or {}).get("name"),
                    "team": (node.get("team") or {}).get("key"),
                    "assignee": (node.get("assignee") or {}).get("name"),
                    "url": node.get("url"),
                    "updated_at": node.get("updatedAt"),
                }
                for node in nodes
            ]
        if action == "create_issue":
            title = str(params.get("title") or "").strip()
            if not title:
                raise ConnectorError("create_issue needs a title")
            team_id = str(params.get("team_id") or self.settings.LINEAR_TEAM_ID)
            if not team_id:
                raise ConnectorError(
                    "create_issue needs a team_id (or set LINEAR_TEAM_ID); "
                    "run list_teams first to find the id"
                )
            issue_input: Payload = {"teamId": team_id, "title": title}
            if params.get("description"):
                issue_input["description"] = str(params["description"])
            if params.get("priority") is not None:
                try:
                    priority = int(params["priority"])
                except (TypeError, ValueError) as exc:
                    raise ConnectorError("priority must be a number between 0 and 4") from exc
                if not 0 <= priority <= 4:
                    raise ConnectorError("priority must be a number between 0 and 4")
                issue_input["priority"] = priority
            data = await self._graphql(CREATE_ISSUE_MUTATION, {"input": issue_input})
            result = data.get("issueCreate") or {}
            if not result.get("success"):
                raise ConnectorError("Linear refused to create the issue")
            issue = result.get("issue") or {}
            return {
                "created": True,
                "id": issue.get("id"),
                "identifier": issue.get("identifier"),
                "title": issue.get("title"),
                "url": issue.get("url"),
            }
        if action == "comment_issue":
            body = str(params.get("body") or "").strip()
            if not body:
                raise ConnectorError("comment_issue needs a body")
            issue_id = str(params.get("issue_id") or "")
            if not issue_id:
                raise ConnectorError("comment_issue needs an issue_id")
            data = await self._graphql(COMMENT_MUTATION, {"input": {"issueId": issue_id, "body": body}})
            result = data.get("commentCreate") or {}
            if not result.get("success"):
                raise ConnectorError("Linear refused to create the comment")
            comment = result.get("comment") or {}
            return {"created": True, "id": comment.get("id"), "url": comment.get("url")}
        raise ConnectorError(f"Unhandled Linear action {action!r}")


__all__ = ["LINEAR_ACTIONS", "LinearConnector"]
