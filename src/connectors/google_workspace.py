"""Google Workspace connector: Gmail, Calendar and Drive as agent tools.

One OAuth refresh token covers all three services (the scopes are chosen when
the token is minted, see ``.env.example``). The access token is refreshed on
demand, cached in memory and written back to the encrypted token store so a
restart does not need a new consent screen.

Actions are read-mostly; the ones that send or create something are flagged
``dangerous`` and require ``confirm=True``.
"""

from __future__ import annotations

import base64
import time
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any, Dict, List, Optional

import httpx

from config.settings import Settings
from src.connectors.base import Connector, ConnectorAction, Payload
from src.utils.errors import AuthenticationError, ConnectorError, ConnectorTransientError
from src.utils.logger import get_logger
from src.utils.retry import async_retry

LOGGER = get_logger(__name__)

GOOGLE_ACTIONS: List[ConnectorAction] = [
    ConnectorAction(
        name="gmail_search",
        description="Search Gmail with the standard query syntax (`from:`, `is:unread`, …).",
        params={"query": "Gmail search query.", "max_results": "How many messages (default 10)."},
    ),
    ConnectorAction(
        name="gmail_read",
        description="Read one message (headers + text body).",
        params={"message_id": "Gmail message id, as returned by gmail_search."},
    ),
    ConnectorAction(
        name="gmail_send",
        description="Send an email from the connected account.",
        params={"to": "Recipient address.", "subject": "Subject line.", "body": "Plain text body."},
        dangerous=True,
    ),
    ConnectorAction(
        name="calendar_events",
        description="List upcoming events from the primary calendar.",
        params={"max_results": "How many events (default 10).", "days": "Look-ahead window in days (default 7)."},
    ),
    ConnectorAction(
        name="calendar_create_event",
        description="Create a calendar event on the primary calendar.",
        params={"summary": "Event title.", "start": "ISO-8601 start.", "end": "ISO-8601 end."},
        dangerous=True,
    ),
    ConnectorAction(
        name="drive_search",
        description="Search Drive files (`name contains 'x'`, `mimeType = …`).",
        params={"query": "Drive query.", "max_results": "How many files (default 20)."},
    ),
    ConnectorAction(
        name="drive_export",
        description="Export a Google Doc/Sheet/Slide to text (or download a text file).",
        params={"file_id": "Drive file id.", "mime_type": "Export mime type (default text/plain)."},
    ),
]

GMAIL_API = "https://gmail.googleapis.com/gmail/v1"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"
DRIVE_API = "https://www.googleapis.com/drive/v3"


class GoogleWorkspaceConnector(Connector):
    """Gmail + Calendar + Drive behind one OAuth token.

    TODO(rule 9): Drive *upload* is not implemented. It needs
    ``POST /upload/drive/v3/files?uploadType=multipart`` with a multipart body
    that mixes JSON metadata and the file bytes; add it when an agent actually
    has to write to Drive (`mimeType` + resumable support should be decided
    then, because resumable uploads need a session URI and chunk loop).
    """

    name = "google"
    category = "productivity"
    description = "Gmail, Google Calendar and Google Drive of the connected account."
    required_env = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN")

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
                    timeout=httpx.Timeout(self.settings.GOOGLE_REQUEST_TIMEOUT, connect=10.0),
                )
            )

    # ------------------------------------------------------------------
    # Credentials
    # ------------------------------------------------------------------
    @property
    def is_configured(self) -> bool:
        """True when a refresh token and an OAuth client are available."""
        stored = self.stored_token() or {}
        if stored.get("refresh_token"):
            return bool(
                (stored.get("client_id") or self.settings.GOOGLE_CLIENT_ID)
                and (stored.get("client_secret") or self.settings.GOOGLE_CLIENT_SECRET)
            )
        return self.settings.is_google_configured

    def _refresh_token(self) -> str:
        """Return the refresh token from the encrypted store, else settings."""
        stored = self.stored_token() or {}
        return str(stored.get("refresh_token") or self.settings.GOOGLE_REFRESH_TOKEN)

    def _credentials(self) -> Dict[str, str]:
        """Return the OAuth client id/secret (store wins over environment)."""
        stored = self.stored_token() or {}
        client_id = str(stored.get("client_id") or self.settings.GOOGLE_CLIENT_ID)
        client_secret = str(stored.get("client_secret") or self.settings.GOOGLE_CLIENT_SECRET)
        if not client_id or not client_secret:
            raise AuthenticationError(
                "Google OAuth client is incomplete: set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET"
            )
        return {"client_id": client_id, "client_secret": client_secret}

    @async_retry(max_retries=3, retry_on=(ConnectorTransientError, httpx.TransportError, httpx.TimeoutException))
    async def _fetch_access_token(self) -> Payload:
        """Exchange the refresh token for an access token."""
        stored = self.stored_token() or {}
        refresh_token = str(stored.get("refresh_token") or self.settings.GOOGLE_REFRESH_TOKEN)
        if not refresh_token:
            raise AuthenticationError(
                "No Google refresh token: set GOOGLE_REFRESH_TOKEN or store one via the token store "
                "(service='google', account='default', key='refresh_token')"
            )
        payload = {"client_id": "", "client_secret": "", "refresh_token": refresh_token, "grant_type": "refresh_token"}
        payload.update(self._credentials())
        response = await self._client.post(self.settings.GOOGLE_TOKEN_URL, data=payload)
        if response.status_code in (400, 401, 403):
            raise AuthenticationError(f"Google refused the refresh token: {response.text[:200]}")
        if response.status_code >= 500:
            raise ConnectorTransientError(f"Google token endpoint returned {response.status_code}")
        data = response.json()
        if "access_token" not in data:
            raise AuthenticationError(f"Google token response had no access_token: {data}")
        token_data = {**data, **self._credentials(), "refresh_token": refresh_token}
        self.save_token(token_data)
        return token_data

    async def access_token(self) -> str:
        """Return a valid access token, refreshing it when it expires."""
        if self._access_token_cache and time.monotonic() < self._token_expiry - 60:
            return self._access_token_cache
        async with self._token_lock:
            if self._access_token_cache and time.monotonic() < self._token_expiry - 60:
                return self._access_token_cache
            stored = self.stored_token() or {}
            token = str(stored.get("access_token") or "")
            expires_at = float(stored.get("expires_at") or 0.0)
            if token and expires_at and time.time() < expires_at - 60:
                self._access_token_cache = token
                self._token_expiry = time.monotonic() + (expires_at - time.time())
                return token
            data = await self._fetch_access_token()
            self._access_token_cache = str(data["access_token"])
            self._token_expiry = time.monotonic() + float(data.get("expires_in") or 3600)
            return self._access_token_cache

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------
    @async_retry(max_retries=3, retry_on=(ConnectorTransientError, httpx.TransportError, httpx.TimeoutException))
    async def _request(self, method: str, url: str, **kwargs: Any) -> Payload:
        """Call a Google API with the bearer token attached."""
        token = await self.access_token()
        headers = {**kwargs.pop("headers", {}), "Authorization": f"Bearer {token}"}
        response = await self._client.request(method, url, headers=headers, **kwargs)
        if response.status_code in (401, 403) and "insufficient" in response.text.lower():
            raise ConnectorError(
                f"Google scope error on {url}: {response.text[:200]} (re-mint the refresh token with "
                "https://www.googleapis.com/auth/gmail.readonly, .../calendar and .../drive.readonly)"
            )
        if response.status_code in (401, 403):
            raise AuthenticationError(f"Google rejected the call to {url}: {response.text[:200]}")
        if response.status_code >= 500:
            raise ConnectorTransientError(f"Google returned {response.status_code} for {url}")
        if response.status_code >= 400:
            raise ConnectorError(f"Google returned {response.status_code} for {url}: {response.text[:200]}")
        if response.headers.get("content-type", "").startswith("text/"):
            return {"text": response.text}
        if not response.content:
            return {}
        return response.json()

    # ------------------------------------------------------------------
    # Contract
    # ------------------------------------------------------------------
    #: Calendar read touches the token refresh path and nothing else.
    probe_action = "calendar_events"
    probe_params = {"max_results": 1, "days": 1}

    def actions(self) -> List[ConnectorAction]:
        """Return the Workspace actions."""
        return GOOGLE_ACTIONS

    async def call(self, action: str, params: Payload) -> Any:
        """Perform a Gmail/Calendar/Drive action."""
        if action == "gmail_search":
            limit = max(1, min(int(params.get("max_results") or 10), 50))
            listing = await self._request(
                "GET",
                f"{GMAIL_API}/users/me/messages",
                params={"q": params.get("query", ""), "maxResults": limit},
            )
            messages: List[Payload] = []
            for stub in listing.get("messages", []) or []:
                detail = await self._request("GET", f"{GMAIL_API}/users/me/messages/{stub['id']}")
                messages.append(_gmail_summary(detail))
            return messages
        if action == "gmail_read":
            detail = await self._request("GET", f"{GMAIL_API}/users/me/messages/{params['message_id']}")
            return {**_gmail_summary(detail), "body": _gmail_body(detail)}
        if action == "gmail_send":
            message = EmailMessage()
            message["To"] = str(params["to"])
            message["Subject"] = str(params.get("subject") or "(no subject)")
            message.set_content(str(params.get("body") or ""))
            raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
            sent = await self._request("POST", f"{GMAIL_API}/users/me/messages/send", json={"raw": raw})
            return {"sent": True, "id": sent.get("id"), "to": params.get("to")}
        if action == "calendar_events":
            limit = max(1, min(int(params.get("max_results") or 10), 50))
            days = max(1, int(params.get("days") or 7))
            now = datetime.now(UTC)
            listing = await self._request(
                "GET",
                f"{CALENDAR_API}/calendars/primary/events",
                params={
                    "maxResults": limit,
                    "orderBy": "startTime",
                    "singleEvents": "true",
                    "timeMin": now.isoformat().replace("+00:00", "Z"),
                    "timeMax": (now + timedelta(days=days)).isoformat().replace("+00:00", "Z"),
                },
            )
            return [
                {
                    "id": event.get("id"),
                    "summary": event.get("summary"),
                    "start": (event.get("start") or {}).get("dateTime") or (event.get("start") or {}).get("date"),
                    "end": (event.get("end") or {}).get("dateTime") or (event.get("end") or {}).get("date"),
                    "location": event.get("location"),
                    "attendees": [a.get("email") for a in event.get("attendees", []) or []],
                }
                for event in listing.get("items", []) or []
            ]
        if action == "calendar_create_event":
            created = await self._request(
                "POST",
                f"{CALENDAR_API}/calendars/primary/events",
                json={
                    "summary": params["summary"],
                    "start": {"dateTime": params["start"]},
                    "end": {"dateTime": params["end"]},
                },
            )
            return {"created": True, "id": created.get("id"), "html_link": created.get("htmlLink")}
        if action == "drive_search":
            limit = max(1, min(int(params.get("max_results") or 20), 100))
            listing = await self._request(
                "GET",
                f"{DRIVE_API}/files",
                params={
                    "q": params.get("query", ""),
                    "pageSize": limit,
                    "fields": "files(id,name,mimeType,modifiedTime,size,webViewLink)",
                },
            )
            return listing.get("files", []) or []
        if action == "drive_export":
            file_id = str(params["file_id"])
            mime_type = str(params.get("mime_type") or "text/plain")
            response = await self._client.get(
                f"{DRIVE_API}/files/{file_id}/export",
                params={"mimeType": mime_type},
                headers={"Authorization": f"Bearer {await self.access_token()}"},
            )
            if response.status_code == 200:
                return {"file_id": file_id, "mime_type": mime_type, "text": response.text}
            # Not a Google-native file: fall back to a plain download.
            fallback = await self._client.get(
                f"{DRIVE_API}/files/{file_id}",
                params={"alt": "media"},
                headers={"Authorization": f"Bearer {await self.access_token()}"},
            )
            if fallback.status_code != 200:
                raise ConnectorError(
                    f"Drive export failed ({response.status_code}) and download failed ({fallback.status_code}); "
                    "binary files need the file's own mime type"
                )
            try:
                return {"file_id": file_id, "text": fallback.content.decode("utf-8")}
            except UnicodeDecodeError as exc:
                raise ConnectorError(f"Drive file {file_id} is binary; export it with a matching mime_type") from exc
        raise ConnectorError(f"Unhandled Google action {action!r}")


def _headers(message: Payload) -> Dict[str, str]:
    """Return a Gmail message's headers as a dict (lower-cased keys)."""
    payload = message.get("payload") or {}
    return {h.get("name", "").lower(): h.get("value", "") for h in payload.get("headers", []) or []}


def _gmail_summary(message: Payload) -> Payload:
    """Return the small, useful shape of a Gmail message."""
    headers = _headers(message)
    return {
        "id": message.get("id"),
        "thread_id": message.get("threadId"),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "snippet": message.get("snippet", ""),
        "labels": message.get("labelIds", []) or [],
    }


def _gmail_body(message: Payload, max_chars: int = 20_000) -> str:
    """Extract the plain-text body from a Gmail message payload."""
    payload = message.get("payload") or {}

    def walk(part: Payload) -> str:
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            data = part["body"]["data"]
            return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
        for child in part.get("parts", []) or []:
            found = walk(child)
            if found:
                return found
        return ""

    return walk(payload)[:max_chars]


__all__ = ["GOOGLE_ACTIONS", "GoogleWorkspaceConnector"]
