"""Tests for the free-tier integration stack: Clerk auth, Resend and settings.

The Clerk tests generate a real RSA key pair and sign real RS256 tokens, so the
verification path (JWKS → key → signature → claims) is exercised end to end
against a mock JWKS endpoint. No network, no credentials.

The Resend tests drive the client through ``httpx.MockTransport`` and cover the
retry/back-off behaviour, the notification preferences and the "email is
optional" contract.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from typing import Any, Dict, List, Optional

import httpx
import pytest

from config.settings import Settings
from src.api.auth import (
    CLOCK_SKEW,
    PUBLIC_PATHS,
    ClerkVerifier,
    decode_unverified,
    is_public_path,
    rsa_public_key_from_jwk,
    validate_claims,
    verify_signature,
    verify_svix_signature,
)
from src.api.routes import create_app
from src.utils.errors import AuthError, ConfigurationError
from src.utils.resend_client import ResendNotifier, _render_table

# ----------------------------------------------------------------------
# RS256 test tokens
# ----------------------------------------------------------------------
CLERK_PUBLISHABLE = "pk_test_" + base64.urlsafe_b64encode(
    b"kollektiv.clerk.accounts.dev$"
).decode().rstrip("=")
ISSUER = "https://kollektiv.clerk.accounts.dev"
KID = "ins_2abc"


def _rsa_keypair() -> Any:
    """Generate a throwaway RSA key pair."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk(public_key: Any, kid: str = KID) -> Dict[str, Any]:
    """Serialise an RSA public key as a JWK."""
    numbers = public_key.public_numbers()

    def encode(value: int) -> str:
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return {"kty": "RSA", "kid": kid, "use": "sig", "alg": "RS256", "n": encode(numbers.n), "e": encode(numbers.e)}


def make_token(
    public_key: Any,
    private_key: Any,
    claims: Optional[Dict[str, Any]] = None,
    kid: str = KID,
    algorithm: str = "RS256",
) -> str:
    """Build a signed JWT for the test key pair."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    now = int(time.time())
    payload = {
        "sub": "user_2abc",
        "email": "dev@example.com",
        "name": "Dev Example",
        "iss": ISSUER,
        "azp": "http://localhost:3000",
        "sid": "sess_1",
        "iat": now,
        "nbf": now - 5,
        "exp": now + 3600,
    }
    payload.update(claims or {})
    header = {"alg": algorithm, "typ": "JWT", "kid": kid}

    def encode(data: Dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode().rstrip("=")

    signing_input = f"{encode(header)}.{encode(payload)}".encode()
    if algorithm == "none" or private_key is None:
        signature = b""
    else:
        signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input.decode()}.{base64.urlsafe_b64encode(signature).decode().rstrip('=')}"


@pytest.fixture()
def clerk_keys() -> Any:
    """Return ``(public_key, private_key, jwk)`` for token signing."""
    private_key = _rsa_keypair()
    public_key = private_key.public_key()
    return public_key, private_key, _jwk(public_key)


def clerk_settings(settings: Settings, **overrides: Any) -> Settings:
    """Settings wired for Clerk with a mock JWKS endpoint."""
    return settings.model_copy(
        update={
            "CLERK_SECRET_KEY": "sk_test_123",
            "CLERK_PUBLISHABLE_KEY": CLERK_PUBLISHABLE,
            "CLERK_AUTHORIZED_PARTIES": "http://localhost:3000",
            "SECRET_KEY": "test-secret-key",
            **overrides,
        }
    )


def jwks_transport(keys: List[Dict[str, Any]], calls: Optional[List[str]] = None) -> httpx.MockTransport:
    """Serve a JWKS document at the Clerk well-known path."""
    log = calls if calls is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(str(request.url))
        if request.url.path.endswith("/.well-known/jwks.json"):
            return httpx.Response(200, json={"keys": keys})
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(handler)


def build_verifier(settings: Settings, keys: List[Dict[str, Any]], calls: Optional[List[str]] = None) -> ClerkVerifier:
    """Return a verifier backed by the mock JWKS transport."""
    return ClerkVerifier(settings, client=httpx.AsyncClient(transport=jwks_transport(keys, calls)))


# ----------------------------------------------------------------------
# Settings derivation
# ----------------------------------------------------------------------
def test_clerk_urls_are_derived_from_the_publishable_key(settings: Settings) -> None:
    """The issuer and JWKS URL come straight from the publishable key."""
    resolved = clerk_settings(settings)
    assert resolved.clerk_issuer == ISSUER
    assert resolved.clerk_jwks_url == f"{ISSUER}/.well-known/jwks.json"
    assert resolved.is_clerk_configured is True
    assert resolved.clerk_authorized_parties == ["http://localhost:3000"]


def test_clerk_not_configured_without_keys(settings: Settings) -> None:
    """Without keys Clerk stays off and the API stays open."""
    bare = settings.model_copy(update={"CLERK_SECRET_KEY": "", "CLERK_PUBLISHABLE_KEY": ""})
    assert bare.is_clerk_configured is False
    assert bare.clerk_jwks_url == ""
    assert bare.AUTH_REQUIRED is False


def test_auth_required_without_clerk_is_reported(settings: Settings) -> None:
    """Asking for auth without Clerk is a configuration warning."""
    broken = settings.model_copy(
        update={"AUTH_REQUIRED": True, "CLERK_SECRET_KEY": "", "CLERK_PUBLISHABLE_KEY": ""}
    )
    warnings = broken.config_warnings()
    assert any("AUTH_REQUIRED" in warning for warning in warnings)


def test_notification_settings(settings: Settings) -> None:
    """Recipient parsing and the Resend capability flag."""
    resolved = settings.model_copy(
        update={"RESEND_API_KEY": "re_123", "NOTIFY_EMAILS": "a@example.com, b@example.com"}
    )
    assert resolved.notify_recipients == ["a@example.com", "b@example.com"]
    assert resolved.is_resend_configured is True
    assert settings.is_resend_configured is False
    assert any("NOTIFY_EMAILS" in warning for warning in settings.config_warnings())


def test_database_url_normalisation(settings: Settings) -> None:
    """Neon/Heroku DSNs become SQLAlchemy 2 driver URLs with TLS."""
    neon = settings.model_copy(
        update={"DATABASE_URL": "postgres://u:p@ep-cool-1.us-east-2.aws.neon.tech/kollektiv"}
    )
    assert neon.database_url == (
        "postgresql+psycopg://u:p@ep-cool-1.us-east-2.aws.neon.tech/kollektiv?sslmode=require"
    )
    assert neon.is_postgres is True

    explicit = settings.model_copy(
        update={"DATABASE_URL": "postgresql://u:p@host/db?sslmode=disable"}
    )
    assert explicit.database_url == "postgresql+psycopg://u:p@host/db?sslmode=disable"

    local = settings.model_copy(update={"DATABASE_URL": "sqlite:///./data/local.db"})
    assert local.database_url == "sqlite:///./data/local.db"
    assert local.is_postgres is False


def test_secrets_are_redacted(settings: Settings) -> None:
    """New credentials never leak through ``redacted()``."""
    resolved = settings.model_copy(
        update={
            "R2_ACCOUNTS": '[{"bucket": "b", "access_key_id": "k", "secret_access_key": "s"}]',
            "R2_SECRET_ACCESS_KEY": "secret",
            "CLERK_SECRET_KEY": "sk_live_123",
            "CLERK_WEBHOOK_SECRET": "whsec_123",
            "RESEND_API_KEY": "re_123",
        }
    )
    dumped = resolved.redacted()
    for field in ("R2_ACCOUNTS", "R2_SECRET_ACCESS_KEY", "CLERK_SECRET_KEY", "CLERK_WEBHOOK_SECRET", "RESEND_API_KEY"):
        assert dumped[field] == "***redacted***", field


# ----------------------------------------------------------------------
# JWT verification
# ----------------------------------------------------------------------
def test_signature_verification_accepts_a_valid_token(clerk_keys: Any) -> None:
    """A correctly signed RS256 token verifies."""
    _, private_key, jwk = clerk_keys
    token = make_token(clerk_keys[0], private_key)
    payload = verify_signature(token, jwk)
    assert payload["sub"] == "user_2abc"


def test_signature_verification_rejects_a_tampered_token(clerk_keys: Any) -> None:
    """Changing the payload invalidates the signature."""
    _, private_key, jwk = clerk_keys
    token = make_token(clerk_keys[0], private_key)
    header, payload, signature = token.split(".")
    forged = base64.urlsafe_b64encode(
        json.dumps({"sub": "attacker", "exp": int(time.time()) + 60}).encode()
    ).decode().rstrip("=")
    with pytest.raises(AuthError):
        verify_signature(f"{header}.{forged}.{signature}", jwk)


def test_signature_verification_rejects_other_algorithms(clerk_keys: Any) -> None:
    """``alg=none`` and HS256 must never be accepted."""
    _, private_key, jwk = clerk_keys
    with pytest.raises(AuthError):
        verify_signature(make_token(clerk_keys[0], None, algorithm="none"), jwk)
    with pytest.raises(AuthError):
        verify_signature(make_token(clerk_keys[0], private_key, algorithm="HS256"), jwk)


def test_signature_verification_rejects_a_wrong_key(clerk_keys: Any) -> None:
    """A token signed by another key is rejected."""
    other = _rsa_keypair()
    token = make_token(clerk_keys[0], other)
    with pytest.raises(AuthError):
        verify_signature(token, clerk_keys[2])
    # And a non-RSA JWK is refused outright.
    with pytest.raises(AuthError):
        rsa_public_key_from_jwk({"kty": "oct", "k": "abc"})


def test_decode_unverified_rejects_garbage() -> None:
    """Malformed tokens fail fast with a clear error."""
    for bad in ("", "not-a-token", "a.b", "a.b.c"):
        with pytest.raises(AuthError):
            decode_unverified(bad)


def test_claim_validation(clerk_keys: Any, settings: Settings) -> None:
    """Expiry, issuer and authorised-party claims are enforced."""
    resolved = clerk_settings(settings)
    now = time.time()
    with pytest.raises(AuthError):
        validate_claims({"sub": "u", "exp": now - CLOCK_SKEW - 10}, resolved, now=now)
    with pytest.raises(AuthError):
        validate_claims({"sub": "u", "nbf": now + CLOCK_SKEW + 10}, resolved, now=now)
    with pytest.raises(AuthError):
        validate_claims({"exp": now + 60}, resolved, now=now)
    with pytest.raises(AuthError):
        validate_claims({"sub": "u", "iss": "https://evil.example"}, resolved, now=now)
    with pytest.raises(AuthError):
        validate_claims({"sub": "u", "azp": "https://evil.example"}, resolved, now=now)

    user = validate_claims(
        {"sub": "u", "iss": ISSUER, "azp": "http://localhost:3000", "email": "a@b.c"},
        resolved,
        now=now,
    )
    assert user.subject == "u"
    assert user.display_name == "a@b.c"
    assert user.to_dict()["email"] == "a@b.c"


# ----------------------------------------------------------------------
# JWKS-backed verifier
# ----------------------------------------------------------------------
async def test_verifier_fetches_caches_and_verifies(clerk_keys: Any, settings: Settings) -> None:
    """The verifier fetches the JWKS once, then serves from cache."""
    _, private_key, jwk = clerk_keys
    calls: List[str] = []
    verifier = build_verifier(clerk_settings(settings), [jwk], calls)

    token = make_token(clerk_keys[0], private_key)
    user = await verifier.verify(token)
    assert user.email == "dev@example.com"
    assert user.org_id == ""
    assert len(calls) == 1

    await verifier.verify(token)
    assert len(calls) == 1, "the JWKS must be cached"
    await verifier.close()


async def test_verifier_refreshes_on_key_rotation(clerk_keys: Any, settings: Settings) -> None:
    """An unknown ``kid`` triggers one JWKS refresh (key rotation)."""
    _, private_key, jwk = clerk_keys
    rotated = dict(jwk, kid="ins_rotated")
    calls: List[str] = []
    verifier = build_verifier(clerk_settings(settings), [jwk], calls)

    rotated_token = make_token(clerk_keys[0], private_key, kid="ins_rotated")
    with pytest.raises(AuthError):
        await verifier.verify(rotated_token)  # first fetch has the old key only

    verifier._keys = {"ins_old": jwk}
    verifier._fetched_at = time.time()
    verifier._client = httpx.AsyncClient(transport=jwks_transport([jwk, rotated], calls))
    user = await verifier.verify(rotated_token)
    assert user.subject == "user_2abc"
    assert len(calls) >= 2
    await verifier.close()


async def test_verifier_requires_configuration(settings: Settings) -> None:
    """Without Clerk configured the verifier says so explicitly."""
    bare = settings.model_copy(update={"CLERK_SECRET_KEY": "", "CLERK_PUBLISHABLE_KEY": ""})
    verifier = ClerkVerifier(bare)
    with pytest.raises(ConfigurationError):
        await verifier.verify("a.b.c")
    await verifier.close()


def test_public_paths() -> None:
    """Health, docs and webhooks stay reachable without a token."""
    assert is_public_path("/health") is True
    assert is_public_path("/docs") is True
    assert is_public_path("/openapi.json") is True
    assert is_public_path("/webhooks/github") is True
    assert is_public_path("/") is True
    assert is_public_path("/projects") is False
    assert is_public_path("/projects/abc/status") is False
    assert "/health" in PUBLIC_PATHS


# ----------------------------------------------------------------------
# Svix webhook signatures
# ----------------------------------------------------------------------
def test_svix_signature_round_trip() -> None:
    """A correctly signed Clerk webhook is accepted; tampering is not."""
    key = b"super-secret-key"
    secret = "whsec_" + base64.b64encode(key).decode()
    body = json.dumps({"type": "user.created", "data": {"id": "user_1"}}).encode()
    now = time.time()
    svix_id, timestamp = "msg_1", str(int(now))
    signature = base64.b64encode(
        hmac.new(key, f"{svix_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()
    ).decode()
    headers = {
        "svix-id": svix_id,
        "svix-timestamp": timestamp,
        "svix-signature": f"v1,{signature} v1,deadbeef",
    }
    assert verify_svix_signature(body, headers, secret, now=now) is True
    assert verify_svix_signature(body + b"x", headers, secret, now=now) is False
    assert verify_svix_signature(body, headers, "whsec_" + base64.b64encode(b"other").decode(), now=now) is False
    # Stale timestamps are rejected even with a valid signature.
    assert verify_svix_signature(body, headers, secret, tolerance=10, now=now + 120) is False
    # Missing headers / secret are never accepted.
    assert verify_svix_signature(body, {}, secret, now=now) is False
    assert verify_svix_signature(body, headers, "", now=now) is False


# ----------------------------------------------------------------------
# API wiring
# ----------------------------------------------------------------------
def api_app_for(settings: Settings) -> Any:
    """Build the real app (no orchestrator startup) for auth middleware tests."""
    return create_app(settings, orchestrator=None)


def stub_orchestrator(settings: Settings) -> Any:
    """A minimal orchestrator stand-in for middleware/webhook tests."""

    class _Stub:
        """Only what the middleware and webhook routes touch."""

        def __init__(self, resolved: Settings) -> None:
            self.settings = resolved

        async def start(self) -> Dict[str, Any]:
            """Pretend to start."""
            return {"warnings": []}

        async def stop(self) -> None:
            """Pretend to stop."""

        async def health(self) -> Dict[str, Any]:
            """Report healthy."""
            return {"status": "ok", "subsystems": {}}

    return _Stub(settings)


async def test_api_open_when_auth_is_not_required(settings: Settings) -> None:
    """``AUTH_REQUIRED=false`` (default) leaves the API reachable."""
    app = api_app_for(settings)
    app.state.orchestrator = stub_orchestrator(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        me = await client.get("/auth/me")
        assert me.status_code == 200
        assert me.json() == {"authenticated": False, "auth_required": False}


async def test_api_rejects_anonymous_calls_when_auth_is_required(
    clerk_keys: Any, settings: Settings
) -> None:
    """With Clerk enforced, protected routes need a valid token."""
    _, private_key, jwk = clerk_keys
    resolved = clerk_settings(settings, AUTH_REQUIRED=True)
    app = api_app_for(resolved)
    app.state.clerk_verifier = build_verifier(resolved, [jwk])
    app.state.orchestrator = stub_orchestrator(resolved)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        anonymous = await client.get("/projects")
        assert anonymous.status_code == 401

        # /health and the webhook routes stay public even when auth is enforced
        # (an unsigned GitHub delivery is then rejected by the HMAC check, not
        # by the auth middleware).
        assert (await client.get("/health")).status_code == 200
        health = await client.get("/webhooks/github/health")
        assert health.status_code == 200
        assert "signature" in (await client.post("/webhooks/github", content=b"{}")).text

        token = make_token(clerk_keys[0], private_key)
        me = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200
        body = me.json()
        assert body["authenticated"] is True
        assert body["user"]["email"] == "dev@example.com"

        bad = await client.get("/projects", headers={"Authorization": "Bearer not.a.token"})
        assert bad.status_code == 401
    await app.state.clerk_verifier.close()


async def test_clerk_webhook_requires_the_secret(settings: Settings) -> None:
    """Without ``CLERK_WEBHOOK_SECRET`` the endpoint records but does not trust."""
    app = api_app_for(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/webhooks/clerk", json={"type": "user.created", "data": {}})
        assert response.status_code == 200
        assert response.json()["verified"] is False


async def test_clerk_webhook_verifies_signatures(settings: Settings) -> None:
    """A signed Clerk webhook is accepted; an unsigned one is a 401."""
    key = b"webhook-key"
    resolved = settings.model_copy(update={"CLERK_WEBHOOK_SECRET": "whsec_" + base64.b64encode(key).decode()})
    app = api_app_for(resolved)
    transport = httpx.ASGITransport(app=app)
    payload = json.dumps({"type": "user.created", "data": {"email_addresses": [{"email_address": "a@b.c"}]}}).encode()
    now = str(int(time.time()))
    signature = base64.b64encode(
        hmac.new(key, f"msg_9.{now}.".encode() + payload, hashlib.sha256).digest()
    ).decode()
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        good = await client.post(
            "/webhooks/clerk",
            content=payload,
            headers={
                "svix-id": "msg_9",
                "svix-timestamp": now,
                "svix-signature": f"v1,{signature}",
                "content-type": "application/json",
            },
        )
        assert good.status_code == 200
        assert good.json() == {"received": True, "verified": True, "type": "user.created"}

        bad = await client.post(
            "/webhooks/clerk",
            content=payload,
            headers={"svix-id": "msg_9", "svix-timestamp": now, "svix-signature": "v1,nope"},
        )
        assert bad.status_code == 401


# ----------------------------------------------------------------------
# Resend
# ----------------------------------------------------------------------
def resend_settings(settings: Settings, **overrides: Any) -> Settings:
    """Settings wired for Resend."""
    return settings.model_copy(
        update={
            "RESEND_API_KEY": "re_test_123",
            "NOTIFY_EMAILS": "ops@example.com",
            "RESEND_FROM": "Kollektiv <bot@example.com>",
            **overrides,
        }
    )


def build_notifier(
    settings: Settings, responses: Optional[List[Any]] = None, calls: Optional[List[Any]] = None
) -> ResendNotifier:
    """Build a notifier backed by a scripted mock transport."""
    script = list(responses or [httpx.Response(200, json={"id": "email_1"})])
    log = calls if calls is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        log.append((request.method, str(request.url), json.loads(request.content or b"{}")))
        return script.pop(0) if script else httpx.Response(200, json={"id": "email_extra"})

    return ResendNotifier(
        settings,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url=settings.RESEND_BASE_URL,
            headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
        ),
    )


async def test_resend_sends_an_email(settings: Settings) -> None:
    """A configured notifier posts to ``/emails`` with the right payload."""
    calls: List[Any] = []
    notifier = build_notifier(resend_settings(settings), calls=calls)
    assert notifier.is_configured() is True

    sent = await notifier.send("Subject", "<p>Body</p>", "Body")
    assert sent is True
    assert notifier.sent == 1
    method, url, payload = calls[0]
    assert method == "POST" and url.endswith("/emails")
    assert payload["to"] == ["ops@example.com"]
    assert payload["subject"] == "Subject"
    assert payload["from"] == "Kollektiv <bot@example.com>"
    await notifier.close()


async def test_resend_is_a_noop_when_unconfigured(settings: Settings) -> None:
    """Without a key nothing is sent and no error is raised."""
    notifier = build_notifier(settings.model_copy(update={"RESEND_API_KEY": "", "NOTIFY_EMAILS": ""}))
    assert notifier.is_configured() is False
    assert await notifier.send("Subject", "<p>x</p>") is False
    assert await notifier.send_run_summary("demo", {"status": "completed"}) is False
    assert notifier.stats()["sent"] == 0
    await notifier.close()


async def test_resend_requires_recipients(settings: Settings) -> None:
    """A key without recipients is a configuration error, surfaced as ``False``."""
    notifier = build_notifier(resend_settings(settings, NOTIFY_EMAILS=""))
    with pytest.raises(ConfigurationError):
        await notifier.send("Subject", "<p>x</p>")
    assert notifier.is_configured() is False
    await notifier.close()


async def test_resend_retries_transient_failures(settings: Settings) -> None:
    """429/5xx responses are retried with back-off."""
    responses = [
        httpx.Response(429, text="rate limited", headers={"retry-after": "0"}),
        httpx.Response(503, text="down"),
        httpx.Response(200, json={"id": "email_2"}),
    ]
    notifier = build_notifier(resend_settings(settings), responses=responses)
    assert await notifier.send("Subject", "<p>x</p>") is True
    assert notifier.sent == 1
    await notifier.close()


async def test_resend_fails_fast_on_permanent_errors(settings: Settings) -> None:
    """A 422 is permanent: no retries, error surfaced to the caller."""
    notifier = build_notifier(
        resend_settings(settings), responses=[httpx.Response(422, text="invalid from")]
    )
    from src.utils.errors import EmailError

    with pytest.raises(EmailError):
        await notifier.send("Subject", "<p>x</p>")
    assert notifier.failures == 1
    assert "invalid from" in notifier.last_error
    await notifier.close()


async def test_run_summary_notification_preferences(settings: Settings) -> None:
    """``NOTIFY_ON_*`` controls which runs generate an email."""
    calls: List[Any] = []
    notifier = build_notifier(
        resend_settings(settings, NOTIFY_ON_FAILURE_ONLY=True), calls=calls
    )
    assert await notifier.send_run_summary("demo", {"status": "completed", "failed": 0}) is False
    assert await notifier.send_run_summary("demo", {"status": "failed", "failed": 2}) is True
    assert len(calls) == 1
    assert "demo" in calls[0][2]["subject"]
    await notifier.close()

    disabled = build_notifier(resend_settings(settings, NOTIFY_ON_RUN_COMPLETION=False), calls=[])
    assert await disabled.send_run_summary("demo", {"status": "completed"}) is False
    await disabled.close()


async def test_run_summary_swallows_resend_failures(settings: Settings) -> None:
    """A broken email provider must never fail the run that triggered it."""
    notifier = build_notifier(
        resend_settings(settings), responses=[httpx.Response(500, text="boom")] * 4
    )
    assert await notifier.send_run_summary("demo", {"status": "failed", "failed": 1}) is False
    await notifier.close()


async def test_alert_email_renders_details(settings: Settings) -> None:
    """Alerts include the detail rows and hit the API."""
    calls: List[Any] = []
    notifier = build_notifier(resend_settings(settings), calls=calls)
    assert await notifier.send_alert("Agent pool is empty", "no workers configured", {"agents": 0}) is True
    payload = calls[0][2]
    assert "Agent pool is empty" in payload["subject"]
    assert "no workers configured" in payload["html"]
    assert "agents" in payload["html"]
    await notifier.close()


def test_email_rendering_escapes_html() -> None:
    """User data is escaped before it reaches the email body."""
    rendered = _render_table("<b>bold</b>", [("<script>", "x & y")], "http://localhost:8000", "prj_1")
    assert "<b>bold</b>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "x &amp; y" in rendered
    assert "http://localhost:8000/projects/prj_1/status" in rendered


# ----------------------------------------------------------------------
# CLI bootstrap + dashboard
# ----------------------------------------------------------------------
async def test_bootstrap_prepares_a_working_install(settings: Settings, tmp_path: Any, capsys: Any) -> None:
    """``kollektiv bootstrap`` sets up the database, workspace and report."""
    from src.api.cli import main as cli_main

    resolved = settings.model_copy(
        update={
            "DATABASE_URL": f"sqlite:///{tmp_path / 'bootstrap.db'}",
            "WORKSPACE_DIR": str(tmp_path / "workspace"),
            "SECRET_KEY": "",
        }
    )
    # cmd_bootstrap reads the ambient settings, so install this configuration.
    import src.api.cli as cli_module
    from config import settings as settings_module

    settings_module.get_settings.cache_clear()
    original = settings_module.get_settings
    settings_module.get_settings = lambda: resolved  # type: ignore[assignment]
    cli_module.get_settings = lambda: resolved  # type: ignore[assignment]
    try:
        exit_code = cli_main(["bootstrap", "--json"])
    finally:
        settings_module.get_settings = original  # type: ignore[assignment]
        settings_module.get_settings.cache_clear()

    captured = capsys.readouterr().out
    assert exit_code == 0
    assert '"database": "sqlite"' in captured
    assert (tmp_path / "bootstrap.db").exists()
    assert (tmp_path / "workspace" / "state").is_dir()


async def test_bootstrap_prints_the_free_tier_checklist(settings: Settings, tmp_path: Any, capsys: Any) -> None:
    """The human-readable report lists every free tool and the missing key."""
    from src.api.cli import cmd_bootstrap

    resolved = settings.model_copy(
        update={
            "DATABASE_URL": f"sqlite:///{tmp_path / 'b.db'}",
            "WORKSPACE_DIR": str(tmp_path / "ws"),
            "SECRET_KEY": "",
        }
    )
    import src.api.cli as cli_module
    from config import settings as settings_module

    original = settings_module.get_settings
    settings_module.get_settings = lambda: resolved  # type: ignore[assignment]
    cli_module.get_settings = lambda: resolved  # type: ignore[assignment]
    try:
        args = type("Args", (), {"json": False})()
        exit_code = await cmd_bootstrap(args)
    finally:
        settings_module.get_settings = original  # type: ignore[assignment]
    output = capsys.readouterr().out
    assert exit_code == 0
    for tool in ("Cloudflare R2", "Neon Postgres", "Clerk", "Resend", "Groq"):
        assert tool in output
    assert "SECRET_KEY=" in output


def test_dashboard_calls_the_documented_endpoints() -> None:
    """The static dashboard only uses endpoints the API actually exposes."""
    from pathlib import Path

    page = Path(__file__).resolve().parents[1] / "web" / "index.html"
    html = page.read_text(encoding="utf-8")
    for endpoint in ("/health", "/projects", "/agents/status", "/storage/status", "/sync", "/replan", "/connectors"):
        assert endpoint in html, endpoint
    # It must be self-contained (Cloudflare Pages serves it with no build step).
    assert "<script src=" not in html
    assert "http://localhost:8000" in html, "the default API URL is discoverable"


def test_api_serves_the_dashboard_at_ui(settings: Settings) -> None:
    """`kollektiv serve-api` also serves the static dashboard (single origin)."""
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)

    async def fetch() -> httpx.Response:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/ui/")

    response = asyncio.run(fetch())
    assert response.status_code == 200
    assert "Kollektiv" in response.text
    assert "mission control" in response.text


def test_dashboard_defaults_to_the_api_it_is_served_from() -> None:
    """The page auto-detects same-origin hosting under /ui."""
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text("utf-8")
    assert 'location.pathname.startsWith("/ui")' in html
    assert '"/auth/me"' not in html  # nothing undocumented is called


def test_api_documents_the_new_routes(settings: Settings) -> None:
    """Clerk, per-file URLs and the storage backend are exposed."""
    app = create_app(settings)
    paths = app.openapi()["paths"]
    assert "/auth/me" in paths
    assert "/webhooks/clerk" in paths
    assert "/projects/{project_id}/files/{file_path}/url" in paths
    storage = paths["/storage/status"]["get"]
    assert storage.get("tags") == ["storage"]


def test_root_points_browsers_at_the_dashboard(settings: Settings) -> None:
    """`/` redirects to the bundled dashboard (and to JSON when absent)."""
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)

    async def fetch() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=False
        ) as client:
            return await client.get("/")

    response = asyncio.run(fetch())
    assert response.status_code in (307, 302)
    assert response.headers["location"] == "/ui/"

    # Without the bundled page the route stays machine-readable.
    from src.api.routes import build_router

    machine = build_router(settings, serve_dashboard=False)
    assert any(getattr(route, "path", None) == "/" for route in machine.routes)
