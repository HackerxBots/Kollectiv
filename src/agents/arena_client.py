"""Async client for one worker agent account.

Kollektiv is provider agnostic: an "Arena account" is any HTTP endpoint that
accepts a prompt and returns text. Two request shapes are supported and
auto-detected:

``openai``
    ``POST {base_url}/chat/completions`` with ``{"model", "messages"}`` --
    works with OpenAI, DeepSeek, Groq, vLLM, Ollama, LiteLLM, ...
``custom``
    ``POST {base_url}{ARENA_CHAT_PATH}`` with ``{"prompt", "model",
    "agent_mode"}`` -- for bespoke endpoints (including a private browser
    automation bridge that drives a hosted chat UI on your behalf).

Both shapes normalise to a single string response, so the pool does not care
which one is in use.

Configuration notes
-------------------
* Set ``ARENA_BASE_URL`` and ``ARENA_LOGIN_PATH``/``ARENA_CHAT_PATH`` to match
  your endpoint, or override them per account with the ``base_url`` field in
  ``ARENA_ACCOUNTS``.
* Kollektiv never automates or scrapes a service that forbids it, and it does
  not bypass paid-plan limits. Point the pool at models or endpoints you are
  allowed to use.
* Session tokens are stored encrypted (see :mod:`src.utils.token_store`).

Usage::

    client = ArenaClient({"email": "w1@example.com", "session_token": "..."})
    await client.authenticate()
    print(await client.send_prompt("Write a haiku about merge conflicts"))
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any, Dict, List, Optional

import httpx

from config.settings import Settings, get_settings
from src.utils.crypto import mask_email
from src.utils.errors import (
    ArenaError,
    ArenaTransientError,
    AuthenticationError,
    ConfigurationError,
    RateLimitError,
)
from src.utils.logger import get_logger
from src.utils.net import async_client_kwargs
from src.utils.retry import async_retry
from src.utils.token_store import SERVICE_ARENA, TokenStore

LOGGER = get_logger(__name__)

#: Default cooldown after a rate limit response, in seconds.
DEFAULT_COOLDOWN_SECONDS = 300


class ArenaClient:
    """A single worker agent (one account) capable of running prompts.

    Args:
        account: Account dict with ``email``, ``password``, ``session_token``
            and optional ``base_url``/``model`` overrides.
        settings: Optional settings override.
        token_store: Encrypted token store override.
        client: Pre-built :class:`httpx.AsyncClient` (used by tests).
    """

    def __init__(
        self,
        account: Dict[str, Any],
        settings: Optional[Settings] = None,
        token_store: Optional[TokenStore] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.account: Dict[str, Any] = dict(account or {})
        self.email: str = str(self.account.get("email") or "")
        self.password: str = str(self.account.get("password") or "")
        self.account_id: str = str(
            self.account.get("account_id") or self.account.get("id") or self._derive_id()
        )
        #: Operator-chosen label; the agent pool fills it in when it is empty
        #: (see :mod:`src.agents.names`). It is a display name, never a key.
        self.name: str = str(self.account.get("name") or "")
        self.base_url: str = str(self.account.get("base_url") or self.settings.ARENA_BASE_URL or "").rstrip("/")
        self.model: str = str(self.account.get("model") or self.settings.ARENA_MODEL or "")
        self.api_style: str = str(self.account.get("api_style") or "auto").lower()
        self.max_concurrency: int = int(
            self.account.get("max_concurrency") or self.settings.ARENA_MAX_CONCURRENCY or 1
        )

        self.session_token: str = str(self.account.get("session_token") or "")
        self.session_expires_at: float = 0.0

        self._store = token_store
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            **async_client_kwargs(
                self.settings,
                timeout=httpx.Timeout(self.settings.ARENA_REQUEST_TIMEOUT, connect=15.0),
                headers={"Accept": "application/json"},
            )
        )

        # Runtime statistics / scheduling state
        self.busy: bool = False
        self.tasks_done: int = 0
        self.tasks_failed: int = 0
        self.total_latency_ms: float = 0.0
        self.last_error: str = ""
        self.cooldown_until: float = 0.0
        self.last_used_at: float = 0.0
        self.authenticated: bool = bool(self.session_token)
        self._semaphore = asyncio.Semaphore(max(1, self.max_concurrency))

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------
    def _derive_id(self) -> str:
        """Derive a stable account id when the config omits one."""
        seed = (self.email or self.base_url or "agent").strip().lower()
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def from_config(cls, account: Any, **kwargs: Any) -> "ArenaClient":
        """Build a client from a settings account model or a plain dict."""
        if hasattr(account, "model_dump"):
            data = account.model_dump()
            data.setdefault("account_id", getattr(account, "account_id", ""))
        elif isinstance(account, dict):
            data = dict(account)
        else:  # pragma: no cover - defensive
            raise ConfigurationError(f"Unsupported agent account type: {type(account)!r}")
        return cls(data, **kwargs)

    @property
    def label(self) -> str:
        """Safe label for logs (never the password or token)."""
        return mask_email(self.email) if self.email else self.account_id

    @property
    def client(self) -> httpx.AsyncClient:
        """The underlying HTTP client."""
        return self._client

    def _resolve_style(self) -> str:
        """Decide whether to use the OpenAI or the custom request shape."""
        if self.api_style in {"openai", "custom"}:
            return self.api_style
        # Explicit chat path + no /v1 style base URL implies a custom endpoint.
        if self.settings.ARENA_CHAT_PATH and not self.base_url.endswith("/v1"):
            return "custom"
        return "openai"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def __aenter__(self) -> "ArenaClient":
        """Authenticate and return the client for ``async with`` usage."""
        await self.authenticate()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        """Close the HTTP client."""
        await self.close()

    async def close(self) -> None:
        """Close the HTTP client (only when this instance created it)."""
        if self._owns_client:
            await self._client.aclose()

    def _token_store(self) -> TokenStore:
        """Lazily create the encrypted token store."""
        if self._store is None:
            self._store = TokenStore(self.settings.fernet_secret)
        return self._store

    def _load_stored_token(self) -> None:
        """Load a previously stored session token from the encrypted store."""
        if self.session_token:
            return
        try:
            stored = self._token_store().get_token(SERVICE_ARENA, self.account_id)
        except Exception as exc:  # pragma: no cover - storage failures are not fatal
            LOGGER.warning("Could not read stored session token for %s: %s", self.label, exc)
            return
        if stored:
            self.session_token = stored.get("session_token") or ""
            expires_at = stored.get("expires_at")
            if isinstance(expires_at, (int, float)):
                self.session_expires_at = float(expires_at)

    def _persist_session(self, payload: Dict[str, Any]) -> None:
        """Encrypt and store the session payload."""
        try:
            self._token_store().save_token(SERVICE_ARENA, self.account_id, payload)
        except Exception as exc:  # pragma: no cover - never crash on persistence
            LOGGER.error("Failed to persist the session token for %s: %s", self.label, exc)

    async def authenticate(self) -> str:
        """Ensure a usable session token exists.

        Order of preference:

        1. A non-expired in-memory / stored session token.
        2. The configured ``session_token`` (trusted even without expiry).
        3. A login request when the endpoint exposes one. If the endpoint
           reports that no login route exists, an
           :class:`~src.utils.errors.AuthenticationError` is raised with the
           exact configuration needed to fix it.

        Returns:
            The session token (possibly empty for anonymous endpoints).
        """
        if not self.session_token:
            self._load_stored_token()

        if self.session_token and self.is_authenticated():
            return self.session_token

        if self.password and self.email:
            try:
                return await self._login()
            except AuthenticationError:
                raise
            except Exception as exc:  # noqa: BLE001 - fall through to token
                LOGGER.warning("Login for %s failed: %s", self.label, exc)

        if self.session_token:
            self.authenticated = True
            return self.session_token

        if self.settings.ARENA_CHAT_PATH and self._resolve_style() == "custom":
            raise AuthenticationError(
                "No session token for this agent. Either put a session_token in ARENA_ACCOUNTS, "
                "or expose a login endpoint at ARENA_LOGIN_PATH that returns "
                "{'session_token': ...} for the configured email/password.",
                account=self.label,
            )
        # OpenAI-style endpoints carry the credential in the base URL / headers
        # configured outside the account dict, so nothing to authenticate here.
        self.authenticated = True
        return ""

    async def _login(self) -> str:
        """Perform a login request and store the returned session token.

        The endpoint is expected to return either
        ``{"session_token": "..."}``, ``{"access_token": "..."}`` or
        ``{"token": "..."}``. Set ``ARENA_LOGIN_PATH`` accordingly.

        Returns:
            The session token.

        Raises:
            AuthenticationError: When the endpoint rejects the credentials or
                does not return a token.
        """
        url = f"{self.base_url}{self.settings.ARENA_LOGIN_PATH}"
        payload = {"email": self.email, "password": self.password}
        try:
            response = await self._client.post(url, json=payload)
        except httpx.HTTPError as exc:
            raise AuthenticationError(f"Login request to {url} failed: {exc}", account=self.label) from exc

        if response.status_code in (404, 405):
            raise AuthenticationError(
                f"The configured login endpoint ({url}) does not exist. Set ARENA_LOGIN_PATH, or "
                "supply a ready-made session_token per account in ARENA_ACCOUNTS.",
                status=response.status_code,
            )
        if response.status_code >= 400:
            raise AuthenticationError(
                f"Login rejected with HTTP {response.status_code}: {response.text[:200]}",
                account=self.label,
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise AuthenticationError("Login response was not JSON") from exc

        token = (
            data.get("session_token")
            or data.get("access_token")
            or data.get("token")
            or (data.get("data") or {}).get("session_token")
            or ""
        )
        if not token:
            raise AuthenticationError(f"Login response contained no token: {str(data)[:200]}", account=self.label)

        self.session_token = str(token)
        expires_in = data.get("expires_in")
        if isinstance(expires_in, (int, float)) and expires_in > 0:
            self.session_expires_at = time.time() + float(expires_in)
        self.authenticated = True
        self._persist_session(
            {
                "session_token": self.session_token,
                "expires_in": expires_in,
                "expires_at": self.session_expires_at,
                "email": self.email,
            }
        )
        LOGGER.info("Authenticated worker agent %s", self.label)
        return self.session_token

    def is_authenticated(self) -> bool:
        """Return ``True`` when the session token is present and not expired."""
        if not self.session_token:
            return False
        if self.session_expires_at <= 0:
            return True
        # Refresh when less than 10% of the lifetime remains (min 60s).
        return time.time() < self.session_expires_at

    def _headers(self) -> Dict[str, str]:
        """Return request headers including the session credential."""
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self.session_token:
            headers["Authorization"] = f"Bearer {self.session_token}"
        return headers

    # ------------------------------------------------------------------
    # Prompting
    # ------------------------------------------------------------------
    async def send_prompt(
        self,
        prompt: str,
        use_agent_mode: bool = True,
        system_prompt: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        timeout: Optional[float] = None,
    ) -> str:
        """Send a prompt and return the response text.

        Args:
            prompt: The user prompt.
            use_agent_mode: Request the endpoint's agentic/long-horizon mode
                where supported (ignored by plain OpenAI-style endpoints).
            system_prompt: Optional system message.
            max_tokens: Output token cap.
            temperature: Sampling temperature.
            timeout: Per-request timeout override in seconds.

        Returns:
            The response text (``""`` when the model returned nothing).

        Raises:
            RateLimitError: When the endpoint reports a rate limit.
            ArenaError: For transport or protocol failures.
            AuthenticationError: When the session is rejected.
        """
        if self.is_rate_limited():
            remaining = int(self.cooldown_until - time.time())
            raise RateLimitError(
                f"Agent {self.label} is in cooldown for another {remaining}s",
                retry_after=float(max(remaining, 1)),
            )

        if not self.authenticated and not self.session_token:
            await self.authenticate()

        started = time.perf_counter()
        self.busy = True
        try:
            async with self._semaphore:
                style = self._resolve_style()
                if style == "openai":
                    text = await self._send_openai_style(
                        prompt, system_prompt, max_tokens, temperature, timeout
                    )
                else:
                    text = await self._send_custom_style(prompt, use_agent_mode, system_prompt, timeout)

            latency_ms = (time.perf_counter() - started) * 1000
            self.total_latency_ms += latency_ms
            self.tasks_done += 1
            self.last_used_at = time.time()
            self.last_error = ""
            LOGGER.info(
                "Agent %s answered in %.0f ms (%s chars)",
                self.label,
                latency_ms,
                len(text),
            )
            return text
        except Exception as exc:
            self.tasks_failed += 1
            self.last_error = str(exc)
            raise
        finally:
            self.busy = False

    async def _send_openai_style(
        self,
        prompt: str,
        system_prompt: Optional[str],
        max_tokens: Optional[int],
        temperature: Optional[float],
        timeout: Optional[float],
    ) -> str:
        """Call an OpenAI-compatible ``/chat/completions`` endpoint."""
        messages: List[Dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload: Dict[str, Any] = {
            "model": self.model or "default",
            "messages": messages,
            "temperature": self.settings.BRAIN_TEMPERATURE if temperature is None else temperature,
            "max_tokens": max_tokens or self.settings.BRAIN_MAX_TOKENS,
        }
        url = f"{self.base_url}/chat/completions"
        data = await self._post_json(url, payload, context="chat completion", timeout=timeout)

        choices = data.get("choices") or []
        if not choices:
            raise ArenaError(f"Endpoint returned no choices: {str(data)[:200]}", agent=self.label)
        message = choices[0].get("message") or {}
        content = message.get("content") or choices[0].get("text") or ""
        return self._coerce_text(content)

    async def _send_custom_style(
        self,
        prompt: str,
        use_agent_mode: bool,
        system_prompt: Optional[str],
        timeout: Optional[float],
    ) -> str:
        """Call a bespoke chat endpoint with the Kollektiv prompt envelope."""
        payload: Dict[str, Any] = {
            "prompt": prompt,
            "agent_mode": bool(use_agent_mode),
            "model": self.model or None,
            "stream": False,
        }
        if system_prompt:
            payload["system"] = system_prompt
        url = f"{self.base_url}{self.settings.ARENA_CHAT_PATH}"
        data = await self._post_json(url, payload, context="chat", timeout=timeout)
        return self._coerce_text(_extract_text(data))

    @staticmethod
    def _coerce_text(content: Any) -> str:
        """Normalise the many possible response shapes into plain text."""
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: List[str] = []
            for item in content:
                if isinstance(item, dict):
                    parts.append(str(item.get("text") or item.get("content") or ""))
                else:
                    parts.append(str(item))
            return "".join(parts)
        if isinstance(content, dict):
            return str(content.get("text") or content.get("content") or content)
        return str(content)

    @async_retry(max_retries=3, base_delay=2.0, max_delay=30.0, exclude=(RateLimitError,))
    async def _post_json(
        self,
        url: str,
        payload: Dict[str, Any],
        context: str = "request",
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """POST JSON and decode the response, mapping errors to typed ones.

        Raises:
            RateLimitError: On HTTP 429 (sets the cooldown window).
            AuthenticationError: On HTTP 401/403.
            ArenaError: For every other failure.
        """
        try:
            response = await self._client.post(
                url,
                json=payload,
                headers=self._headers(),
                timeout=timeout or self.settings.ARENA_REQUEST_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise ArenaTransientError(f"{context} request to {url} failed: {exc}", agent=self.label) from exc

        if response.status_code == 429:
            retry_after = _retry_after(response, self.settings.ARENA_RATE_LIMIT_COOLDOWN)
            self.cooldown_until = time.time() + retry_after
            raise RateLimitError(
                f"Agent {self.label} was rate limited during {context}",
                retry_after=retry_after,
                agent=self.label,
            )
        if response.status_code in (401, 403):
            self.authenticated = False
            self.session_token = ""
            raise AuthenticationError(
                f"{context} rejected with HTTP {response.status_code} for {self.label}",
                status=response.status_code,
            )
        if response.status_code >= 500:
            raise ArenaTransientError(
                f"{context} failed with HTTP {response.status_code}: {response.text[:300]}",
                agent=self.label,
                status=response.status_code,
            )
        if response.status_code >= 400:
            raise ArenaError(
                f"{context} failed with HTTP {response.status_code}: {response.text[:300]}",
                agent=self.label,
                status=response.status_code,
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ArenaTransientError(f"{context} returned non-JSON: {response.text[:200]}") from exc
        return data if isinstance(data, dict) else {"data": data}

    # ------------------------------------------------------------------
    # Health / sessions
    # ------------------------------------------------------------------
    async def get_session_status(self, probe: bool = False) -> Dict[str, Any]:
        """Report whether this session is usable.

        Args:
            probe: When ``True``, perform a lightweight HTTP request against
                the endpoint's models/login route to verify liveness.

        Returns:
            ``{alive, token_valid, authenticated, rate_limited, cooldown_seconds,
            busy, tasks_done, tasks_failed, last_error, probed, http_status}``.
        """
        # Key-based endpoints (OpenAI-compatible base URLs) have no session
        # token, but they are still "authenticated" once the client is usable.
        token_valid = self.is_authenticated() or (self.authenticated and not self.session_token)

        # ``probe`` additionally asks the endpoint whether the credential is
        # accepted (see ``probe_endpoint``); the local snapshot is instant.
        status: Dict[str, Any] = {
            "account_id": self.account_id,
            "label": self.label,
            "alive": token_valid and not self.is_rate_limited(),
            "token_valid": token_valid,
            "authenticated": self.authenticated,
            "rate_limited": self.is_rate_limited(),
            "cooldown_seconds": max(0, int(self.cooldown_until - time.time())),
            "busy": self.busy,
            "tasks_done": self.tasks_done,
            "tasks_failed": self.tasks_failed,
            "average_latency_ms": round(self.total_latency_ms / self.tasks_done, 1) if self.tasks_done else 0.0,
            "last_error": self.last_error,
            "probed": False,
            "http_status": None,
        }
        if probe:
            status.update(await self._probe())
        return status

    async def _probe(self) -> Dict[str, Any]:
        """Perform a lightweight liveness request without consuming quota."""
        try:
            response = await self._client.get(
                f"{self.base_url}/models" if self._resolve_style() == "openai" else f"{self.base_url}/",
                headers=self._headers(),
                timeout=10.0,
            )
            return {
                "probed": True,
                "http_status": response.status_code,
                "alive": response.status_code < 500,
                "token_valid": response.status_code not in (401, 403),
            }
        except httpx.HTTPError as exc:
            return {"probed": True, "http_status": None, "alive": False, "probe_error": str(exc)}

    async def is_ready(self) -> bool:
        """Return ``True`` when this agent can accept a task right now."""
        if self.busy or self.is_rate_limited():
            return False
        if not self.is_authenticated() and not self.session_token:
            try:
                await self.authenticate()
            except AuthenticationError as exc:
                self.last_error = str(exc)
                return False
            except Exception as exc:  # noqa: BLE001 - not ready on any failure
                self.last_error = str(exc)
                return False
        return True

    def is_rate_limited(self) -> bool:
        """Return ``True`` while the agent is inside a rate-limit cooldown."""
        return time.time() < self.cooldown_until

    async def reset_session(self) -> bool:
        """Clear the current session and authenticate again.

        Returns:
            ``True`` when a usable session exists afterwards.
        """
        LOGGER.info("Resetting session for agent %s", self.label)
        self.session_token = ""
        self.session_expires_at = 0.0
        self.authenticated = False
        self.cooldown_until = 0.0
        try:
            self._token_store().delete_token(SERVICE_ARENA, self.account_id)
        except Exception as exc:  # pragma: no cover - best effort
            LOGGER.debug("Could not delete the stored token for %s: %s", self.label, exc)
        try:
            await self.authenticate()
        except AuthenticationError as exc:
            self.last_error = str(exc)
            LOGGER.error("Session reset failed for %s: %s", self.label, exc)
            return False
        return self.is_authenticated() or bool(self.session_token)

    def apply_rate_limit(self, seconds: Optional[float] = None) -> None:
        """Put this agent into cooldown (used by the pool on 429s)."""
        self.cooldown_until = time.time() + float(seconds or self.settings.ARENA_RATE_LIMIT_COOLDOWN)

    def stats(self) -> Dict[str, Any]:
        """Return cumulative statistics for this agent."""
        return {
            "account_id": self.account_id,
            "name": self.name or self.label,
            "label": self.label,
            "status": "rate_limited" if self.is_rate_limited() else ("busy" if self.busy else "idle"),
            "tasks_done": self.tasks_done,
            "tasks_failed": self.tasks_failed,
            "average_latency_ms": round(self.total_latency_ms / self.tasks_done, 1) if self.tasks_done else 0.0,
            "cooldown_seconds": max(0, int(self.cooldown_until - time.time())),
            "last_error": self.last_error,
            "authenticated": self.authenticated,
        }


def _retry_after(response: httpx.Response, default: float) -> float:
    """Extract a cooldown from a 429 response, falling back to ``default``."""
    raw = response.headers.get("Retry-After") or response.headers.get("retry-after")
    if raw:
        try:
            return max(float(raw), 1.0)
        except (TypeError, ValueError):
            pass
    try:
        body = response.json()
        for key in ("retry_after", "retryAfter", "reset_after"):
            if isinstance(body, dict) and body.get(key) is not None:
                return max(float(body[key]), 1.0)
    except (ValueError, TypeError):
        pass
    return max(float(default), 1.0)


def _extract_text(data: Any) -> Any:
    """Pull the assistant text out of the many shapes bespoke APIs return."""
    if isinstance(data, str):
        return data
    if not isinstance(data, dict):
        return data
    for key in ("response", "text", "content", "answer", "output", "message", "result"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, (list, dict)):
            return value
    for wrapper in ("data", "result", "output"):
        nested = data.get(wrapper)
        if isinstance(nested, dict):
            inner = _extract_text(nested)
            if inner:
                return inner
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            return (first.get("message") or {}).get("content") or first.get("text") or ""
    return ""


__all__ = ["ArenaClient", "DEFAULT_COOLDOWN_SECONDS"]
