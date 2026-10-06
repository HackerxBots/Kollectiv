"""Exponential-backoff retry helpers used by every external API client.

Two entry points are provided:

* :func:`async_retry` -- decorator for async functions.
* :func:`retry_call` -- decorator for sync functions and sync API adapters.

Both accept an ``on_retry`` callback so clients can log *why* a call is being
retried, and both honour the ``retry_after`` attribute of
:class:`~src.utils.errors.RateLimitError` and of :class:`httpx.HTTPStatusError`
responses that carry a ``Retry-After`` header.
"""

from __future__ import annotations

import asyncio
import functools
import os
import random
import time
from typing import Any, Awaitable, Callable, Iterable, Optional, Tuple, Type, TypeVar

import httpx

from src.utils.errors import RateLimitError, RetryableError
from src.utils.logger import get_logger

LOGGER = get_logger(__name__)

T = TypeVar("T")

#: Exceptions that always count as transient.
DEFAULT_RETRY_EXCEPTIONS: Tuple[Type[BaseException], ...] = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.TransportError,
    ConnectionError,
    asyncio.TimeoutError,
)

#: HTTP status codes worth retrying.
RETRY_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 522, 524})


def is_retryable_exception(
    exc: BaseException,
    extra: Iterable[Type[BaseException]] = (),
    exclude: Iterable[Type[BaseException]] = (),
) -> bool:
    """Return ``True`` when ``exc`` represents a transient failure.

    Args:
        exc: The exception to classify.
        extra: Additional exception types treated as retryable.
        exclude: Types that must never be retried, even though they normally
            would be (e.g. ``RateLimitError`` inside an agent pool that can
            simply pick a different agent instead of sleeping).

    Returns:
        ``True`` for network errors, 5xx/429 responses and
        :class:`~src.utils.errors.RetryableError` subclasses.
    """
    if exclude and isinstance(exc, tuple(exclude)):
        return False
    if isinstance(exc, RateLimitError):
        return True
    if isinstance(exc, RetryableError):
        return True
    if isinstance(exc, (DEFAULT_RETRY_EXCEPTIONS + tuple(extra))):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRY_STATUS_CODES
    return False


def _retry_after_seconds(exc: BaseException) -> Optional[float]:
    """Extract a server suggested delay from ``exc``, when present."""
    if isinstance(exc, RateLimitError) and exc.retry_after is not None:
        return float(exc.retry_after)
    response = getattr(exc, "response", None)
    if response is not None:
        header = getattr(response, "headers", {}) or {}
        raw = header.get("Retry-After") or header.get("retry-after")
        if raw:
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None
    return None


def _env_float(name: str, default: float) -> float:
    """Read a float from the environment, falling back to ``default``."""
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def effective_delays(base_delay: float, max_delay: float) -> Tuple[float, float]:
    """Apply the global ``KOLLEKTIV_RETRY_*_DELAY`` environment overrides.

    Operators (and the test suite) can shorten or lengthen every backoff in
    Kollektiv without touching call sites:

    * ``KOLLEKTIV_RETRY_BASE_DELAY`` -- seconds before the first retry.
    * ``KOLLEKTIV_RETRY_MAX_DELAY`` -- ceiling for a single delay.

    Args:
        base_delay: The decorator's own default.
        max_delay: The decorator's own ceiling.

    Returns:
        The ``(base_delay, max_delay)`` pair to use.
    """
    base = _env_float("KOLLEKTIV_RETRY_BASE_DELAY", base_delay)
    maximum = _env_float("KOLLEKTIV_RETRY_MAX_DELAY", max_delay)
    return base, max(maximum, base)


def compute_delay(
    attempt: int,
    base_delay: float,
    max_delay: float,
    jitter: bool,
    suggested: Optional[float] = None,
) -> float:
    """Compute the backoff delay for the given attempt (1-indexed).

    Args:
        attempt: Attempt number that just failed (1 -> first failure).
        base_delay: Delay used for the first retry, in seconds.
        max_delay: Upper bound for a single delay.
        jitter: Whether to add up to 25% random jitter.
        suggested: Server suggested delay, which takes precedence.

    Returns:
        The number of seconds to sleep before the next attempt.
    """
    if suggested is not None and suggested > 0:
        return min(suggested, max_delay)
    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
    if jitter:
        delay *= 1.0 + random.uniform(0.0, 0.25)
    return min(delay, max_delay)


def async_retry(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    jitter: bool = True,
    retry_on: Tuple[Type[BaseException], ...] = (),
    exclude: Tuple[Type[BaseException], ...] = (),
    on_retry: Optional[Callable[[int, BaseException, float], None]] = None,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Retry an async function with exponential backoff.

    Args:
        max_retries: Number of *retries* after the initial attempt.
        base_delay: Delay before the first retry in seconds.
        max_delay: Maximum delay between attempts.
        jitter: Add random jitter to the delay to avoid thundering herds.
        retry_on: Extra exception types considered retryable.
        exclude: Exception types that must never be retried.
        on_retry: Optional ``(attempt, exc, delay)`` callback for logging.

    Returns:
        A decorator that wraps the coroutine function. The final failure is
        re-raised unchanged so callers keep the original exception type.
    """

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            last_error: Optional[BaseException] = None
            resolved_base, resolved_max = effective_delays(base_delay, max_delay)
            for attempt in range(1, max_retries + 2):
                try:
                    return await func(*args, **kwargs)
                except BaseException as exc:  # noqa: BLE001 - re-raised below
                    last_error = exc
                    if not is_retryable_exception(exc, retry_on, exclude) or attempt > max_retries:
                        raise
                    delay = compute_delay(
                        attempt, resolved_base, resolved_max, jitter, _retry_after_seconds(exc)
                    )
                    if on_retry is not None:
                        on_retry(attempt, exc, delay)
                    else:
                        LOGGER.warning(
                            "%s failed (attempt %s/%s): %s -- retrying in %.1fs",
                            func.__qualname__,
                            attempt,
                            max_retries + 1,
                            exc,
                            delay,
                        )
                    await asyncio.sleep(delay)
            raise last_error if last_error else RuntimeError("retry loop exited unexpectedly")

        return wrapper

    return decorator


def retry_call(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    jitter: bool = True,
    retry_on: Tuple[Type[BaseException], ...] = (),
    exclude: Tuple[Type[BaseException], ...] = (),
    on_retry: Optional[Callable[[int, BaseException, float], None]] = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Synchronous twin of :func:`async_retry`.

    Used by sync adapters (webhook signature work, CLI helpers, SQLite
    transactions) that must not be rewritten as coroutines.
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            last_error: Optional[BaseException] = None
            resolved_base, resolved_max = effective_delays(base_delay, max_delay)
            for attempt in range(1, max_retries + 2):
                try:
                    return func(*args, **kwargs)
                except BaseException as exc:  # noqa: BLE001 - re-raised below
                    last_error = exc
                    if not is_retryable_exception(exc, retry_on, exclude) or attempt > max_retries:
                        raise
                    delay = compute_delay(
                        attempt, resolved_base, resolved_max, jitter, _retry_after_seconds(exc)
                    )
                    if on_retry is not None:
                        on_retry(attempt, exc, delay)
                    else:
                        LOGGER.warning(
                            "%s failed (attempt %s/%s): %s -- retrying in %.1fs",
                            func.__qualname__,
                            attempt,
                            max_retries + 1,
                            exc,
                            delay,
                        )
                    time.sleep(delay)
            raise last_error if last_error else RuntimeError("retry loop exited unexpectedly")

        return wrapper

    return decorator


async def with_retries(
    func: Callable[[], Awaitable[T]],
    *args: Any,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    **kwargs: Any,
) -> T:
    """Call ``func(*args, **kwargs)`` with retries (no decorator required).

    Handy for lambdas and closures built at runtime by the dispatcher.
    """
    return await async_retry(
        max_retries=max_retries, base_delay=base_delay, max_delay=max_delay
    )(func)(*args, **kwargs)


__all__ = [
    "async_retry",
    "retry_call",
    "with_retries",
    "compute_delay",
    "effective_delays",
    "is_retryable_exception",
    "RETRY_STATUS_CODES",
]
