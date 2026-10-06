"""Clerk authentication for the HTTP API.

Clerk issues RS256 session tokens; this module verifies them without pulling in
a JWT library, because ``cryptography`` is already a dependency:

1. the token header names a ``kid``; the matching public key comes from the
   Clerk JWKS endpoint (fetched once, cached, refreshed on rotation),
2. the RS256 signature is verified over ``header.payload``,
3. claims are checked (``exp``, ``nbf``, ``iss``, optional ``azp``),
4. ``sub``/``email``/``org_id`` are exposed to the route via ``request.state``.

When ``AUTH_REQUIRED=false`` (the default) the API stays open — handy for local
development, tests and self-hosted setups — but verified identity is still
attached to every request that carries a token.

Usage::

    from src.api.auth import auth_dependency, install_auth

    install_auth(app, settings)                 # middleware + public paths
    @router.get("/whoami")
    async def whoami(user = Depends(auth_dependency)): ...
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse

from config.settings import Settings, get_settings
from src.utils.errors import AuthError, ConfigurationError
from src.utils.logger import get_logger

LOGGER = get_logger(__name__)

#: Paths that never require a session token.
PUBLIC_PATHS = (
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/favicon.ico",
    "/webhooks/",
    "/",
)

#: How long a fetched JWKS is trusted before it is refreshed.
JWKS_TTL_SECONDS = 3600
#: Clock skew tolerance for ``exp``/``nbf`` (seconds).
CLOCK_SKEW = 60


@dataclass
class ClerkUser:
    """A verified Clerk session."""

    subject: str
    email: str = ""
    name: str = ""
    org_id: str = ""
    org_role: str = ""
    session_id: str = ""
    claims: Dict[str, Any] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        """Return the best available human label."""
        return self.name or self.email or self.subject

    def to_dict(self) -> Dict[str, Any]:
        """Serialise the identity (never the raw token)."""
        return {
            "subject": self.subject,
            "email": self.email,
            "name": self.name,
            "org_id": self.org_id,
            "org_role": self.org_role,
        }


def _b64url_decode(value: str) -> bytes:
    """Decode base64url data with missing padding tolerated."""
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def decode_unverified(token: str) -> Tuple[Dict[str, Any], Dict[str, Any], bytes]:
    """Split a JWT into ``(header, payload, signing_input)`` without verifying.

    Raises:
        AuthError: When the token is not a well-formed three-part JWT.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise AuthError("Malformed token: expected three dot-separated parts.")
    header_raw, payload_raw, signature_raw = parts
    try:
        header = json.loads(_b64url_decode(header_raw))
        payload = json.loads(_b64url_decode(payload_raw))
    except Exception as exc:  # noqa: BLE001 - any decode failure is an auth failure
        raise AuthError(f"Malformed token payload: {exc}") from exc
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise AuthError("Malformed token: header and payload must be JSON objects.")
    return header, payload, f"{header_raw}.{payload_raw}".encode("ascii")


def rsa_public_key_from_jwk(jwk: Dict[str, Any]) -> Any:
    """Build an RSA public key object from a JWK.

    Args:
        jwk: A JWK dict with ``n``/``e`` (base64url).

    Returns:
        A ``cryptography`` RSAPublicKey.

    Raises:
        AuthError: When the JWK is not an RSA key or is missing parameters.
    """
    from cryptography.hazmat.primitives.asymmetric import rsa

    if (jwk.get("kty") or "").upper() != "RSA":
        raise AuthError(f"Unsupported JWK key type: {jwk.get('kty')!r}")
    modulus = jwk.get("n")
    exponent = jwk.get("e")
    if not modulus or not exponent:
        raise AuthError("JWK is missing the RSA modulus/exponent.")
    n = int.from_bytes(_b64url_decode(modulus), "big")
    e = int.from_bytes(_b64url_decode(exponent), "big")
    return rsa.RSAPublicNumbers(e, n).public_key()


def verify_signature(token: str, jwk: Dict[str, Any]) -> Dict[str, Any]:
    """Verify the RS256 signature and return the payload.

    Raises:
        AuthError: When the algorithm or signature is not acceptable.
    """
    from cryptography.exceptions import InvalidSignature

    header, payload, signing_input = decode_unverified(token)
    algorithm = str(header.get("alg") or "").upper()
    if algorithm != "RS256":
        raise AuthError(f"Unsupported token algorithm: {algorithm or 'none'}")
    signature = _b64url_decode(token.split(".")[2])
    public_key = rsa_public_key_from_jwk(jwk)
    try:
        public_key.verify(signature, signing_input, _padding(), _hashes())
    except InvalidSignature as exc:
        raise AuthError("Token signature is invalid.") from exc
    except Exception as exc:  # noqa: BLE001 - malformed key/signature
        raise AuthError(f"Could not verify the token signature: {exc}") from exc
    return payload


def _padding() -> Any:
    """Return the RSASSA-PKCS1-v1_5 padding object."""
    from cryptography.hazmat.primitives.asymmetric import padding

    return padding.PKCS1v15()


def _hashes() -> Any:
    """Return the SHA-256 hash object."""
    from cryptography.hazmat.primitives import hashes

    return hashes.SHA256()


def validate_claims(
    payload: Dict[str, Any],
    settings: Settings,
    now: Optional[float] = None,
) -> ClerkUser:
    """Validate standard claims and return the identity.

    Raises:
        AuthError: When a required claim is missing, expired or unexpected.
    """
    moment = now if now is not None else time.time()
    exp = payload.get("exp")
    if exp is not None and moment > float(exp) + CLOCK_SKEW:
        raise AuthError("Token has expired.")
    nbf = payload.get("nbf")
    if nbf is not None and moment < float(nbf) - CLOCK_SKEW:
        raise AuthError("Token is not valid yet.")
    subject = str(payload.get("sub") or "")
    if not subject:
        raise AuthError("Token is missing the 'sub' claim.")

    issuer = settings.clerk_issuer
    token_issuer = str(payload.get("iss") or "").rstrip("/")
    if issuer and token_issuer and token_issuer != issuer:
        raise AuthError(f"Unexpected token issuer: {token_issuer}")

    authorised = settings.clerk_authorized_parties
    azp = str(payload.get("azp") or "")
    if authorised and azp and azp not in authorised:
        raise AuthError(f"Token was issued for an unauthorised party: {azp}")

    return ClerkUser(
        subject=subject,
        email=str(payload.get("email") or ""),
        name=str(payload.get("name") or ""),
        org_id=str(payload.get("org_id") or ""),
        org_role=str(payload.get("org_role") or ""),
        session_id=str(payload.get("sid") or ""),
        claims=payload,
    )


class ClerkVerifier:
    """Verifies Clerk session tokens, caching the JWKS.

    Args:
        settings: Settings override.
        client: ``httpx.AsyncClient`` override (tests).
    """

    def __init__(self, settings: Optional[Settings] = None, client: Optional[httpx.AsyncClient] = None) -> None:
        self.settings = settings or get_settings()
        self._client = client
        self._owns_client = client is None
        self._keys: Dict[str, Dict[str, Any]] = {}
        self._fetched_at = 0.0
        self._lock = None

    async def _http(self) -> httpx.AsyncClient:
        """Return (creating on first use) the JWKS HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        return self._client

    async def close(self) -> None:
        """Close the HTTP client (only when this instance created it)."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def jwks(self, force: bool = False) -> Dict[str, Dict[str, Any]]:
        """Return ``{kid: jwk}``, fetching from Clerk when stale.

        Raises:
            ConfigurationError: When Clerk is not configured.
            AuthError: When the JWKS cannot be fetched or parsed.
        """
        url = self.settings.clerk_jwks_url
        if not url:
            raise ConfigurationError(
                "Clerk is not configured: set CLERK_SECRET_KEY and CLERK_PUBLISHABLE_KEY "
                "(or CLERK_ISSUER / CLERK_JWKS_URL)."
            )
        fresh = self._keys and (time.time() - self._fetched_at) < JWKS_TTL_SECONDS
        if fresh and not force:
            return self._keys
        client = await self._http()
        try:
            response = await client.get(url)
        except httpx.HTTPError as exc:
            raise AuthError(f"Could not reach the Clerk JWKS endpoint: {exc}") from exc
        if response.status_code >= 400:
            raise AuthError(f"Clerk JWKS request failed ({response.status_code}).")
        payload = response.json()
        keys: Dict[str, Dict[str, Any]] = {}
        for jwk in payload.get("keys", []):
            kid = str(jwk.get("kid") or "")
            if kid:
                keys[kid] = jwk
        if not keys:
            raise AuthError("Clerk returned an empty JWKS.")
        self._keys = keys
        self._fetched_at = time.time()
        LOGGER.debug("Fetched %s Clerk signing key(s)", len(keys))
        return keys

    async def verify(self, token: str) -> ClerkUser:
        """Verify ``token`` end to end and return the identity.

        Raises:
            ConfigurationError: When Clerk is not configured (checked first, so
                a misconfigured deployment never looks like a bad token).
            AuthError: When the token is malformed, unsigned by a known key,
                expired or carries unexpected claims.
        """
        if not self.settings.clerk_jwks_url:
            await self.jwks()  # raises ConfigurationError naming the missing keys
        header, _, _ = decode_unverified(token)
        kid = str(header.get("kid") or "")
        keys = await self.jwks()
        jwk = keys.get(kid)
        if jwk is None:
            # The key may have rotated: refetch once before failing.
            keys = await self.jwks(force=True)
            jwk = keys.get(kid)
        if jwk is None:
            raise AuthError(f"Unknown signing key: {kid or '(none)'}")
        payload = verify_signature(token, jwk)
        return validate_claims(payload, self.settings)


def bearer_token(request: Request) -> str:
    """Extract a bearer token from the ``Authorization`` header (or ``__session``)."""
    header = request.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    cookie = request.cookies.get("__session")
    return cookie or ""


def is_public_path(path: str) -> bool:
    """Return ``True`` when ``path`` never requires authentication."""
    normalised = path.rstrip("/") or "/"
    for public in PUBLIC_PATHS:
        if public == "/":
            if normalised == "/":
                return True
            continue
        candidate = public.rstrip("/")
        if normalised == candidate or normalised.startswith(candidate + "/"):
            return True
    return False


def _verifier_for(request: Request) -> Optional[ClerkVerifier]:
    """Return the app-level verifier, if one was installed."""
    return getattr(request.app.state, "clerk_verifier", None)


async def auth_dependency(request: Request) -> Optional[ClerkUser]:
    """FastAPI dependency returning the authenticated user (or ``None``).

    Raises:
        HTTPException: ``401`` when ``AUTH_REQUIRED`` is true and the request
            carries no valid Clerk token.
    """
    user: Optional[ClerkUser] = getattr(request.state, "user", None)
    if user is not None:
        return user
    token = bearer_token(request)
    if not token:
        if request.app.state.auth_required:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required: send a Clerk session token.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return None
    verifier = _verifier_for(request)
    if verifier is None:
        if request.app.state.auth_required:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication is required but Clerk is not configured.",
            )
        return None
    try:
        user = await verifier.verify(token)
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except ConfigurationError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    request.state.user = user
    return user


def install_auth(app: Any, settings: Optional[Settings] = None) -> Optional[ClerkVerifier]:
    """Attach authentication state and middleware to ``app``.

    Returns the verifier (``None`` when Clerk is not configured), so callers can
    close it during shutdown.
    """
    resolved = settings or get_settings()
    app.state.auth_required = bool(resolved.AUTH_REQUIRED)
    app.state.settings_used_for_auth = resolved
    verifier: Optional[ClerkVerifier] = None
    if resolved.is_clerk_configured:
        verifier = ClerkVerifier(resolved)
        app.state.clerk_verifier = verifier
        LOGGER.info(
            "Clerk authentication enabled (issuer=%s, required=%s)",
            resolved.clerk_issuer or "(from JWKS URL)",
            resolved.AUTH_REQUIRED,
        )
    else:
        app.state.clerk_verifier = None
        if resolved.AUTH_REQUIRED:
            LOGGER.error(
                "AUTH_REQUIRED is true but Clerk is not configured; every request will fail."
            )
        else:
            LOGGER.info("Clerk is not configured; the API is open (AUTH_REQUIRED=false)")

    @app.middleware("http")
    async def _clerk_middleware(request: Request, call_next: Any) -> Any:
        """Attach identity when possible and reject anonymous calls when required."""
        path = request.url.path
        token = bearer_token(request)
        if token and request.app.state.clerk_verifier is not None:
            try:
                request.state.user = await request.app.state.clerk_verifier.verify(token)
            except AuthError as exc:
                LOGGER.debug("Rejected a token on %s: %s", path, exc)
                request.state.auth_error = str(exc)
            except Exception as exc:  # noqa: BLE001 - never break the request pipeline
                LOGGER.error("Token verification failed on %s: %s", path, exc)
                request.state.auth_error = "token verification failed"
        needs_auth = (
            request.app.state.auth_required
            and not is_public_path(path)
            and getattr(request.state, "user", None) is None
        )
        if needs_auth:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={
                    "detail": getattr(request.state, "auth_error", None)
                    or "Authentication required: send a Clerk session token."
                },
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)

    return verifier


# ----------------------------------------------------------------------
# Clerk webhooks (Svix signature scheme)
# ----------------------------------------------------------------------
def verify_svix_signature(
    payload: bytes,
    headers: Dict[str, str],
    secret: str,
    tolerance: int = 300,
    now: Optional[float] = None,
) -> bool:
    """Verify a Clerk/Svix webhook signature.

    The signed content is ``{svix-id}.{svix-timestamp}.{body}`` and the header
    carries one or more space-separated ``v1,<base64>`` signatures.

    Args:
        payload: Raw request body.
        headers: Request headers (case-insensitive lookup expected).
        secret: The ``whsec_…`` signing secret.
        tolerance: Maximum accepted timestamp skew in seconds.
        now: Timestamp override (tests).

    Returns:
        ``True`` when a signature matches.
    """
    lowered = {key.lower(): value for key, value in headers.items()}
    svix_id = lowered.get("svix-id", "")
    timestamp = lowered.get("svix-timestamp", "")
    signature_header = lowered.get("svix-signature", "")
    if not (svix_id and timestamp and signature_header and secret):
        return False
    try:
        moment = now if now is not None else time.time()
        if tolerance and abs(moment - float(timestamp)) > tolerance:
            LOGGER.warning("Rejected a Clerk webhook with a stale timestamp")
            return False
    except ValueError:
        return False

    raw_secret = secret[len("whsec_") :] if secret.startswith("whsec_") else secret
    try:
        key = base64.b64decode(raw_secret)
    except Exception:  # noqa: BLE001 - treat as base64 failure
        return False
    signed = f"{svix_id}.{timestamp}.".encode() + payload
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    for candidate in signature_header.split():
        _, _, value = candidate.partition(",")
        if value and hmac.compare_digest(value.strip(), expected):
            return True
    return False


def parse_svix_headers(raw_headers: List[Tuple[bytes, bytes]]) -> Dict[str, str]:
    """Convert raw ASGI headers into a lowercase dict."""
    return {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in raw_headers}
