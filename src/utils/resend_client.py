"""Transactional email through Resend.

Resend's free tier (3 000 emails/month) is what Kollektiv uses to tell you that
a project finished, a run failed, or an agent pool ran dry — no SMTP server, no
mail library, just the REST API over ``httpx``.

Emails are always optional: without ``RESEND_API_KEY``/``NOTIFY_EMAILS`` every
method is a no-op that returns ``False``, so the orchestrator can call it
unconditionally.

Usage::

    notifier = ResendNotifier(settings)
    await notifier.send_run_summary("demo", {"status": "completed", "completed": 3})
"""

from __future__ import annotations

import html
from typing import Any, Dict, List, Optional

import httpx

from config.settings import Settings, get_settings
from src.utils.errors import ConfigurationError, EmailError, EmailTransientError
from src.utils.logger import get_logger
from src.utils.retry import async_retry

LOGGER = get_logger(__name__)


class ResendNotifier:
    """Send Kollektiv notifications through Resend.

    Args:
        settings: Settings override.
        client: ``httpx.AsyncClient`` override (tests).
    """

    def __init__(
        self, settings: Optional[Settings] = None, client: Optional[httpx.AsyncClient] = None
    ) -> None:
        self.settings = settings or get_settings()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self.settings.RESEND_BASE_URL.rstrip("/"),
            timeout=httpx.Timeout(20.0, connect=10.0),
            headers={"Authorization": f"Bearer {self.settings.RESEND_API_KEY}"},
        )
        self.sent = 0
        self.failures = 0
        self.last_error = ""

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------
    @property
    def client(self) -> httpx.AsyncClient:
        """The underlying HTTP client."""
        return self._client

    def is_configured(self) -> bool:
        """Return ``True`` when an API key and at least one recipient exist."""
        return self.settings.is_resend_configured

    async def close(self) -> None:
        """Close the HTTP client (only when this instance created it)."""
        if self._owns_client:
            await self._client.aclose()

    def stats(self) -> Dict[str, Any]:
        """Return delivery counters for ``/health``."""
        return {
            "configured": self.is_configured(),
            "recipients": len(self.settings.notify_recipients),
            "sent": self.sent,
            "failures": self.failures,
            "last_error": self.last_error,
        }

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------
    @async_retry(max_retries=3, retry_on=(EmailTransientError, httpx.TransportError))
    async def send(
        self,
        subject: str,
        html_body: str,
        text_body: str = "",
        to: Optional[List[str]] = None,
        reply_to: str = "",
    ) -> bool:
        """Send one email.

        Args:
            subject: Subject line.
            html_body: HTML body.
            text_body: Optional plain-text alternative.
            to: Recipients (defaults to ``NOTIFY_EMAILS``).
            reply_to: Optional reply-to address.

        Returns:
            ``True`` when Resend accepted the message, ``False`` when email is
            not configured.

        Raises:
            ConfigurationError: When email is requested without a recipient list.
            EmailError: When Resend rejects the message.
            EmailTransientError: When Resend is rate limiting or down.
        """
        if not self.settings.RESEND_API_KEY:
            LOGGER.debug("Resend is not configured; skipping email %r", subject)
            return False
        recipients = to or self.settings.notify_recipients
        if not recipients:
            raise ConfigurationError(
                "No email recipients configured; set NOTIFY_EMAILS to enable notifications."
            )
        payload: Dict[str, Any] = {
            "from": self.settings.RESEND_FROM,
            "to": recipients,
            "subject": subject,
            "html": html_body,
        }
        if text_body:
            payload["text"] = text_body
        if reply_to:
            payload["reply_to"] = reply_to

        try:
            response = await self._client.post("/emails", json=payload)
        except httpx.TransportError as exc:
            self.failures += 1
            self.last_error = str(exc)
            raise EmailTransientError(f"Could not reach Resend: {exc}") from exc

        if response.status_code in (429, 500, 502, 503, 504):
            self.failures += 1
            self.last_error = response.text[:200]
            retry_after = response.headers.get("retry-after")
            raise EmailTransientError(
                f"Resend is temporarily unavailable ({response.status_code})",
                float(retry_after) if (retry_after or "").isdigit() else None,
            )
        if response.status_code >= 400:
            self.failures += 1
            self.last_error = response.text[:400]
            raise EmailError(f"Resend rejected the email ({response.status_code}): {self.last_error}")

        self.sent += 1
        LOGGER.info("Sent %r to %s", subject, ", ".join(recipients))
        return True

    # ------------------------------------------------------------------
    # Kollektiv notifications
    # ------------------------------------------------------------------
    def _should_send(self, summary: Dict[str, Any]) -> bool:
        """Apply the ``NOTIFY_*`` preferences to a run summary."""
        if not self.is_configured():
            return False
        if not self.settings.NOTIFY_ON_RUN_COMPLETION:
            return False
        failed = int(summary.get("failed") or 0)
        # ``NOTIFY_ON_FAILURE_ONLY`` suppresses the happy-path emails.
        return not (self.settings.NOTIFY_ON_FAILURE_ONLY and failed == 0)

    async def send_run_summary(
        self, project_name: str, summary: Dict[str, Any], project_id: str = ""
    ) -> bool:
        """Email the outcome of a run.

        Failures are logged, never raised: a notification problem must not fail
        the run that triggered it.
        """
        if not self._should_send(summary):
            return False
        status_value = str(summary.get("status") or "unknown")
        emoji = {"completed": "✅", "failed": "❌", "partial": "⚠️"}.get(status_value, "ℹ️")
        subject = f"{emoji} Kollektiv: {project_name or project_id} — {status_value}"
        artifact = summary.get("artifact") or {}
        rows = [
            ("Project", project_name or project_id or "unknown"),
            ("Project id", project_id or "-"),
            ("Status", status_value),
            ("Tasks dispatched", summary.get("tasks_dispatched")),
            ("Completed", summary.get("completed")),
            ("Failed", summary.get("failed")),
            ("Files produced", artifact.get("file_count")),
            ("Conflicts", len(artifact.get("conflicts") or [])),
            ("Duration (s)", summary.get("duration_seconds")),
        ]
        html_body = _render_table(subject, rows, self.settings.APP_BASE_URL, project_id)
        text_body = "\n".join(f"{key}: {value}" for key, value in rows if value is not None)
        try:
            return await self.send(subject, html_body, text_body)
        except (EmailError, EmailTransientError, ConfigurationError) as exc:
            LOGGER.error("Could not send the run summary for %s: %s", project_id or project_name, exc)
            return False

    async def send_alert(self, title: str, message: str, details: Optional[Dict[str, Any]] = None) -> bool:
        """Email an operational alert (empty pool, storage outage, ...)."""
        if not self.is_configured():
            return False
        rows = [("Alert", title), ("Message", message)] + list((details or {}).items())
        subject = f"⚠️ Kollektiv: {title}"
        html_body = _render_table(subject, rows, self.settings.APP_BASE_URL, "")
        text_body = "\n".join(f"{key}: {value}" for key, value in rows)
        try:
            return await self.send(subject, html_body, text_body)
        except (EmailError, EmailTransientError, ConfigurationError) as exc:
            LOGGER.error("Could not send the alert %r: %s", title, exc)
            return False


def _render_table(
    title: str, rows: List[Any], base_url: str, project_id: str
) -> str:
    """Render a small, inline-styled HTML email (email clients ignore <style>)."""
    body = "".join(
        f'<tr><td style="padding:6px 12px;color:#666">{html.escape(str(key))}</td>'
        f'<td style="padding:6px 12px;font-weight:600">{html.escape(str(value))}</td></tr>'
        for key, value in rows
        if value is not None
    )
    link = ""
    if base_url and project_id:
        url = f"{base_url.rstrip('/')}/projects/{project_id}/status"
        link = (
            f'<p style="margin-top:16px"><a href="{html.escape(url)}" '
            f'style="color:#2563eb">Open the project state</a></p>'
        )
    return (
        '<div style="font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;'
        'max-width:560px;margin:0 auto">'
        f'<h2 style="font-size:18px">{html.escape(title)}</h2>'
        '<table style="border-collapse:collapse;width:100%;font-size:14px">'
        f"{body}</table>{link}"
        '<p style="color:#999;font-size:12px;margin-top:24px">'
        "Sent by Kollektiv — the open-source multi-agent dev team orchestrator.</p>"
        "</div>"
    )
