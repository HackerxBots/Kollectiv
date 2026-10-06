"""GitHub webhook receiver.

Mounts at ``/webhooks/github`` and turns GitHub events into orchestrator
actions:

===================  ==================================================
Event                Action
===================  ==================================================
``ping``             Verifies the hook (returns the Zen message).
``push``             ``SyncEngine.on_push(payload)``.
``pull_request``     ``SyncEngine.on_pr(payload)``; when the PR is merged,
                     ``Orchestrator.on_pr_merged(payload)`` too.
``issues``           Logged into the shared state history.
===================  ==================================================

Every request body is verified against ``GITHUB_WEBHOOK_SECRET`` using the
``X-Hub-Signature-256`` HMAC header **before** any work happens. When no
secret is configured the hook is accepted but a warning is logged (and the
event is marked as unverified in the project state).

Heavy work runs in a FastAPI ``BackgroundTask`` so GitHub receives its 202
immediately -- GitHub times out callers after 10 seconds.

Usage (from the API app)::

    from src.github.webhook_handler import router as webhook_router
    app.include_router(webhook_router)
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from src.utils.logger import get_logger

LOGGER = get_logger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

#: Populated by the API app (or by tests) with the live orchestrator.
_orchestrator: Optional[Any] = None


def set_orchestrator(orchestrator: Optional[Any]) -> None:
    """Register the orchestrator instance used by the webhook handlers.

    Args:
        orchestrator: An :class:`src.orchestrator.app.Orchestrator`. Pass
            ``None`` to detach (used when shutting the app down or in tests).
    """
    global _orchestrator
    _orchestrator = orchestrator


def get_orchestrator(request: Optional[Request] = None) -> Optional[Any]:
    """Return the active orchestrator.

    Looks at ``request.app.state.orchestrator`` first so multiple app
    instances (tests) do not collide, then falls back to the module global.
    """
    if request is not None:
        candidate = getattr(getattr(request, "app", None), "state", None)
        orchestrator = getattr(candidate, "orchestrator", None) if candidate is not None else None
        if orchestrator is not None:
            return orchestrator
    return _orchestrator


# ----------------------------------------------------------------------
# Signature verification
# ----------------------------------------------------------------------
def verify_signature(payload: bytes, signature: Optional[str], secret: str) -> bool:
    """Verify a GitHub ``X-Hub-Signature-256`` header.

    Args:
        payload: The raw request body, exactly as received.
        signature: The header value, e.g. ``sha256=abcd...``.
        secret: The shared webhook secret.

    Returns:
        ``True`` when the signature matches. Always ``True`` when ``secret``
        is empty (the caller is responsible for the resulting warning).
    """
    if not secret:
        return True
    if not signature:
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def parse_payload(raw: bytes) -> Dict[str, Any]:
    """Decode a webhook body into a dict.

    Raises:
        HTTPException: 400 when the body is not a JSON object.
    """
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        LOGGER.error("Webhook body was not valid JSON: %s", exc)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON payload") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Payload must be a JSON object")
    return data


def _extract_common(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Pull the fields Kollektiv cares about out of any GitHub payload."""
    repository = payload.get("repository") or {}
    sender = payload.get("sender") or {}
    return {
        "repo": repository.get("full_name", ""),
        "sender": sender.get("login", ""),
        "ref": payload.get("ref", ""),
        "action": payload.get("action", ""),
    }


# ----------------------------------------------------------------------
# Background work
# ----------------------------------------------------------------------
async def _handle_push(orchestrator: Any, payload: Dict[str, Any], verified: bool) -> None:
    """Run the push sync in the background."""
    try:
        result = await orchestrator.sync_engine.on_push(payload)
        LOGGER.info("Webhook push handled: %s", _brief(result))
    except Exception as exc:  # noqa: BLE001 - background tasks must not raise
        LOGGER.error("Webhook push handling failed: %s", exc, exc_info=True)


async def _handle_pull_request(orchestrator: Any, payload: Dict[str, Any], verified: bool) -> None:
    """Run the PR sync (and the merge hook when applicable) in the background."""
    try:
        result = await orchestrator.sync_engine.on_pr(payload)
        LOGGER.info("Webhook PR handled: %s", _brief(result))
    except Exception as exc:  # noqa: BLE001 - background tasks must not raise
        LOGGER.error("Webhook PR handling failed: %s", exc, exc_info=True)

    pull_request = payload.get("pull_request") or {}
    if payload.get("action") == "closed" and pull_request.get("merged"):
        try:
            merged = await orchestrator.on_pr_merged(payload)
            LOGGER.info("Webhook PR merge handled: %s", _brief(merged))
        except Exception as exc:  # noqa: BLE001 - background tasks must not raise
            LOGGER.error("Webhook PR merge handling failed: %s", exc, exc_info=True)


async def _handle_issues(orchestrator: Any, payload: Dict[str, Any]) -> None:
    """Record an issue event in the shared state history."""
    issue = payload.get("issue") or {}
    try:
        await orchestrator.state.append_event(
            {
                "agent_id": "github",
                "action": f"issue_{payload.get('action', 'updated')}",
                "result": f"#{issue.get('number')} {issue.get('title', '')}"[:400],
            }
        )
    except Exception as exc:  # noqa: BLE001 - background tasks must not raise
        LOGGER.error("Webhook issue handling failed: %s", exc)


def _brief(result: Any, limit: int = 240) -> str:
    """Render a compact one-line summary of a handler result."""
    if isinstance(result, dict):
        return json.dumps(result, default=str)[:limit]
    return str(result)[:limit]


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------
@router.post("/github", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: Optional[str] = Header(default=None, alias="X-Hub-Signature-256"),
    x_github_event: Optional[str] = Header(default=None, alias="X-GitHub-Event"),
    x_github_delivery: Optional[str] = Header(default=None, alias="X-GitHub-Delivery"),
) -> JSONResponse:
    """Receive, verify and dispatch a GitHub webhook delivery.

    Verification uses the raw body, so the signature check happens before any
    parsing. The heavy lifting is queued as a background task.

    Returns:
        ``202`` with ``{status, event, delivery, verified, handled}``.
    """
    raw = await request.body()
    orchestrator = get_orchestrator(request)
    secret = ""
    if orchestrator is not None:
        secret = getattr(orchestrator.settings, "GITHUB_WEBHOOK_SECRET", "") or ""

    verified = verify_signature(raw, x_hub_signature_256, secret)
    if not verified:
        LOGGER.warning(
            "Rejected webhook delivery %s (%s): signature mismatch",
            x_github_delivery,
            x_github_event,
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")
    if not secret:
        LOGGER.warning(
            "GitHub webhook secret is unset; delivery %s was accepted unverified. "
            "Set GITHUB_WEBHOOK_SECRET to enable signature checking.",
            x_github_delivery,
        )

    payload = parse_payload(raw)
    event = (x_github_event or "").lower()
    common = _extract_common(payload)
    LOGGER.info(
        "GitHub webhook received: event=%s action=%s repo=%s delivery=%s verified=%s",
        event or "unknown",
        common.get("action") or "-",
        common.get("repo") or "-",
        x_github_delivery,
        verified,
    )

    if event == "ping":
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "status": "ok",
                "event": "ping",
                "zen": payload.get("zen", ""),
                "verified": verified,
                "handled": "ping",
            },
        )

    if orchestrator is None:
        LOGGER.error("Webhook received but no orchestrator is registered; event dropped")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "unavailable",
                "event": event,
                "verified": verified,
                "handled": "none",
                "detail": "Orchestrator not initialised",
            },
        )

    handled: List[str] = []
    if event == "push":
        background_tasks.add_task(_handle_push, orchestrator, payload, verified)
        handled.append("sync_engine.on_push")
    elif event == "pull_request":
        background_tasks.add_task(_handle_pull_request, orchestrator, payload, verified)
        handled.append("sync_engine.on_pr")
        if payload.get("action") == "closed" and (payload.get("pull_request") or {}).get("merged"):
            handled.append("orchestrator.on_pr_merged")
    elif event == "issues":
        background_tasks.add_task(_handle_issues, orchestrator, payload)
        handled.append("state.append_event")
    else:
        LOGGER.debug("Ignoring unsupported GitHub event %r", event)

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "status": "accepted",
            "event": event,
            "delivery": x_github_delivery,
            "verified": verified,
            "handled": ",".join(handled) or "ignored",
        },
    )


@router.get("/github/health")
async def webhook_health(request: Request) -> Dict[str, Any]:
    """Report whether the webhook endpoint is wired to an orchestrator."""
    orchestrator = get_orchestrator(request)
    secret = getattr(getattr(orchestrator, "settings", None), "GITHUB_WEBHOOK_SECRET", "") if orchestrator else ""
    return {
        "status": "ok",
        "orchestrator_attached": orchestrator is not None,
        "signature_checking": bool(secret),
    }


__all__ = ["router", "set_orchestrator", "get_orchestrator", "verify_signature", "parse_payload"]
