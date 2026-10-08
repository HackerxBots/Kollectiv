"""Async client for one worker agent: a model you reach with your own API key.

A worker is one entry in ``ARENA_ACCOUNTS``. Each entry names a ``provider``
from :mod:`src.agents.providers` (DeepSeek, Groq, OpenRouter, Gemini, Ollama,
any OpenAI-compatible endpoint...), an optional ``model`` and the name of the
environment variable that holds its key (``api_key_env``). The key itself is
never written to ``.env``: ``kollektiv login`` stores it encrypted with
``SECRET_KEY`` and the worker reads it from there.

This is the same bring-your-own-key model OpenCode uses: the software is free,
and every call goes to a provider you already have an account with. Kollectiv
does not log in to web chat UIs, does not replay browser sessions and does not
pool anybody else's subscription.

Key lookup order, first hit wins:

1. ``api_key=`` passed to the constructor (embedding and tests only).
2. The environment variable named by ``api_key_env`` (or the provider default,
   e.g. ``DEEPSEEK_API_KEY``).
3. The encrypted token store, under ``(provider, account_id)``.

Usage::

    worker = ArenaClient({"name": "Vega", "provider": "deepseek"})
    await worker.authenticate()
    print(await worker.send_prompt("Write a haiku about merge conflicts"))
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from typing import Any, Dict, List, Optional

import httpx

from config.settings import Settings, get_settings
from src.agents.providers import PROVIDER_PRESETS
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
from src.utils.token_store import TokenStore

LOGGER = get_logger(__name__)

#: Default cooldown after a rate limit response, in seconds.
DEFAULT_COOLDOWN_SECONDS = 300


class ArenaClient:
    """One worker agent that answers prompts through a provider API.

    Args:
        account: Account dict with ``name``, ``provider``, and optional
            ``account_id``, ``model``, ``base_url``, ``api_key_env`` and
            ``max_concurrency``.
        settings: Optional settings override.
        token_store: Encrypted token store override.
        client: Pre-built :class:`httpx.AsyncClient` (used by tests).
        api_key: Key supplied directly (embedding and tests). Never read from
            ``ARENA_ACCOUNTS``.
    """

    def __init__(
        self,
        account: Dict[str, Any],
        settings: Optional[Settings] = None,
        token_store: Optional[TokenStore] = None,
        client: Optional[httpx.AsyncClient] = None,
        api_key: str = "",
    ) -> None:
        self.settings = settings or get_settings()
        self.account: Dict[str, Any] = dict(account or {})
        self.provider: str = str(self.account.get("provider") or "custom").lower()
        self.preset: Dict[str, str] = PROVIDER_PRESETS.get(self.provider, PROVIDER_PRESETS["custom"])
        self.account_id: str = str(self.account.get("account_id") or self.account.get("id") or self._derive_id())
        #: Operator-chosen display name; the pool fills it when empty (see names.py).
        self.name: str = str(self.account.get("name") or "")
        self.base_url: str = str(self.account.get("base_url") or self.preset["base_url"] or "").rstrip("/")
        self.model: str = str(self.account.get("model") or self.preset["model"] or self.settings.ARENA_MODEL or "")
        self.api_key_env: str = str(self.account.get("api_key_env") or self.preset.get("env") or "")
        self.max_concurrency: int = int(
            self.account.get("max_concurrency") or self.settings.ARENA_MAX_CONCURRENCY or 1
        )

        #: Runtime copy of the key; cleared on auth failure so a re-login is picked up.
        self.api_key: str = api_key or ""
        self.key_source: str = "argument" if api_key else "none"

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
        self.authenticated: bool = False
        self._semaphore = asyncio.Semaphore(max(1, self.max_concurrency))

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------
    def _derive_id(self) -> str:
        """Derive a stable account id when the config omits one."""
        seed = str(
            self.account.get("name") or self.account.get("provider") or self.account.get("base_url") or "agent"
        ).strip().lower()
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
        """Safe label for logs: the display name, else the account id. Never a key."""
        return self.name or self.account_id

    @property
    def client(self) -> httpx.AsyncClient:
        """The underlying HTTP client."""
        return self._client

    async def __aenter__(self) -> "ArenaClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the HTTP client when this worker owns it."""
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # Credentials
    # ------------------------------------------------------------------
    def _token_store(self) -> TokenStore:
        """Lazily create the encrypted token store."""
        if self._store is None:
            self._store = TokenStore(self.settings.fernet_secret)
        return self._store

    def _load_key(self) -> str:
        """Resolve the API key from the argument, the environment or the store."""
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            value = os.environ.get(self.api_key_env, "").strip()
            if value:
                self.api_key, self.key_source = value, "env"
                return value
        try:
            stored = self._token_store().get_token(self.provider, self.account_id)
        except Exception as exc:  # noqa: BLE001 - a storage failure is reported, not fatal
            LOGGER.warning("Could not read the stored key for %s: %s", self.label, exc)
            return ""
        value = str(stored.get("api_key") or "") if stored else ""
        if value:
            self.api_key, self.key_source = value, "store"
        return value

    def is_authenticated(self) -> bool:
        """Return ``True`` when the worker has what it needs to send a request.

        Hosted providers need a key. Local and custom endpoints may run without one.
        """
        if self.api_key:
            return True
        return self.preset["kind"] != "key"

    def _headers(self) -> Dict[str, str]:
        """Return request headers, including the bearer key when there is one."""
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def authenticate(self) -> str:
        """Check the worker can send requests, loading its key if needed.

        Returns:
            The API key (``""`` for keyless local endpoints).

        Raises:
            ConfigurationError: When there is no endpoint to call.
            AuthenticationError: When a hosted provider has no key.
        """
        if not self.base_url:
            raise ConfigurationError(
                f"Worker {self.label} has no base_url. Pick a provider or set base_url in ARENA_ACCOUNTS."
            )
        key = self._load_key()
        if self.preset["kind"] == "key" and not key:
            hint = f" or set {self.api_key_env}" if self.api_key_env else ""
            raise AuthenticationError(
                f"No API key for worker {self.label}. Run `kollektiv login --provider {self.provider}`{hint}.",
                account=self.label,
            )
        self.authenticated = True
        return key

    # ------------------------------------------------------------------
    # Prompting
    # ------------------------------------------------------------------
    async def send_prompt(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        timeout: Optional[float] = None,
    ) -> str:
        """Send one chat completion request and return the reply text.

        Args:
            prompt: The user message.
            system_prompt: Optional system message.
            max_tokens: Output token cap (defaults to ``BRAIN_MAX_TOKENS``).
            temperature: Sampling temperature (defaults to ``BRAIN_TEMPERATURE``).
            timeout: Per-request timeout override in seconds.

        Returns:
            The reply text (``""`` when the model returned nothing).

        Raises:
            RateLimitError: When the provider reports a rate limit.
            AuthenticationError: When the key is missing or rejected.
            ArenaError: For transport or protocol failures.
        """
        if self.is_rate_limited():
            remaining = int(self.cooldown_until - time.time())
            raise RateLimitError(
                f"Agent {self.label} is in cooldown for another {remaining}s",
                retry_after=float(max(remaining, 1)),
            )

        if not self.is_authenticated():
            await self.authenticate()

        started = time.perf_counter()
        self.busy = True
        try:
            async with self._semaphore:
                text = await self._chat(prompt, system_prompt, max_tokens, temperature, timeout)

            latency_ms = (time.perf_counter() - started) * 1000
            self.total_latency_ms += latency_ms
            self.tasks_done += 1
            self.last_used_at = time.time()
            self.last_error = ""
            LOGGER.info("Agent %s answered in %.0f ms (%s chars)", self.label, latency_ms, len(text))
            return text
        except Exception as exc:
            self.tasks_failed += 1
            self.last_error = str(exc)
            raise
        finally:
            self.busy = False

    async def _chat(
        self,
        prompt: str,
        system_prompt: Optional[str],
        max_tokens: Optional[int],
        temperature: Optional[float],
        timeout: Optional[float],
    ) -> str:
        """Call the provider's OpenAI-compatible ``/chat/completions`` endpoint."""
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

    @staticmethod
    def _coerce_text(content: Any) -> str:
        """Normalise the possible response shapes into plain text."""
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

    @async_retry(max_retries=2, base_delay=1.0, max_delay=8.0, exclude=(RateLimitError,))
    async def _post_json(
        self,
        url: str,
        payload: Dict[str, Any],
        context: str = "request",
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """POST JSON and decode the response, mapping errors to typed ones.

        Transport errors and 5xx responses are retried up to twice (three attempts
        in all) with exponential backoff. A 429 is not retried here: it sets the
        worker's cooldown so the pool routes the task to another worker.

        Raises:
            RateLimitError: On HTTP 429 (sets the cooldown window).
            AuthenticationError: On HTTP 401/403 (drops the cached key so a fixed key is re-read).
            ArenaTransientError: On transport errors and HTTP 5xx.
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
            self.api_key = ""
            raise AuthenticationError(
                f"{context} rejected with HTTP {response.status_code} for {self.label}: "
                f"check the key with `kollektiv login --provider {self.provider}`",
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
    # Health
    # ------------------------------------------------------------------
    async def get_session_status(self, probe: bool = False) -> Dict[str, Any]:
        """Report whether this worker can take a task.

        Args:
            probe: When ``True``, call the provider's ``/models`` route (no tokens
                are spent) to confirm the endpoint and key are accepted.

        Returns:
            ``{alive, token_valid, authenticated, key_source, rate_limited,
            cooldown_seconds, busy, tasks_done, tasks_failed, last_error,
            probed, http_status}``.
        """
        usable = self.is_authenticated()
        status: Dict[str, Any] = {
            "account_id": self.account_id,
            "label": self.label,
            "provider": self.provider,
            "alive": usable and not self.is_rate_limited(),
            "token_valid": usable,
            "authenticated": self.authenticated,
            "key_source": self.key_source if self.api_key else "none",
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
        """Ask the provider's ``/models`` route whether the endpoint and key are accepted."""
        if not self.base_url:
            return {"probed": True, "http_status": None, "alive": False, "probe_error": "no base_url"}
        try:
            response = await self._client.get(f"{self.base_url}/models", headers=self._headers(), timeout=10.0)
            return {
                "probed": True,
                "http_status": response.status_code,
                "alive": response.status_code < 500,
                "token_valid": response.status_code not in (401, 403),
            }
        except httpx.HTTPError as exc:
            return {"probed": True, "http_status": None, "alive": False, "probe_error": str(exc)}

    async def is_ready(self) -> bool:
        """Return ``True`` when this worker can accept a task right now."""
        if self.busy or self.is_rate_limited():
            return False
        if not self.is_authenticated():
            try:
                await self.authenticate()
            except Exception as exc:  # noqa: BLE001 - not ready on any failure, but say why
                self.last_error = str(exc)
                return False
        return True

    def is_rate_limited(self) -> bool:
        """Return ``True`` while the worker is inside a rate-limit cooldown."""
        return time.time() < self.cooldown_until

    async def reset_session(self) -> bool:
        """Forget the cached key, re-read it, and check the worker again.

        Use this after you rotate a key with ``kollektiv login``. The stored
        key is kept; only the in-memory copy is dropped.

        Returns:
            ``True`` when the worker has a usable key afterwards.
        """
        LOGGER.info("Reloading credentials for worker %s", self.label)
        self.api_key = ""
        self.key_source = "none"
        self.authenticated = False
        self.cooldown_until = 0.0
        try:
            await self.authenticate()
        except (AuthenticationError, ConfigurationError) as exc:
            self.last_error = str(exc)
            LOGGER.error("Credential reload failed for %s: %s", self.label, exc)
            return False
        return self.is_authenticated()

    def apply_rate_limit(self, seconds: Optional[float] = None) -> None:
        """Put this worker into cooldown (used by the pool on 429s)."""
        self.cooldown_until = time.time() + float(seconds or self.settings.ARENA_RATE_LIMIT_COOLDOWN)

    def stats(self) -> Dict[str, Any]:
        """Return cumulative statistics for this worker."""
        return {
            "account_id": self.account_id,
            "name": self.name or self.label,
            "label": self.label,
            "provider": self.provider,
            "model": self.model,
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


__all__ = ["ArenaClient", "DEFAULT_COOLDOWN_SECONDS"]
