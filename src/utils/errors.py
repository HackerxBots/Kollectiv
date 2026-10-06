"""Exception hierarchy for Kollektiv.

Every layer raises a subclass of :class:`KollektivError` so callers can catch
one base type and still inspect the originating subsystem. The orchestrator
treats :class:`RetryableError` instances as transient (retry) and everything
else as permanent (fail the task, record it in the project state).
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class KollektivError(Exception):
    """Base class for every error raised by Kollektiv.

    Args:
        message: Human readable description.
        details: Optional structured data attached to the error.
    """

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details: Dict[str, Any] = details

    def __str__(self) -> str:
        if not self.details:
            return self.message
        rendered = ", ".join(f"{key}={value!r}" for key, value in self.details.items())
        return f"{self.message} ({rendered})"

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable representation of the error."""
        return {"type": type(self).__name__, "message": self.message, "details": self.details}


class RetryableError(KollektivError):
    """Marker mixin for errors that a retry may plausibly fix.

    Args:
        retry_after: Optional server suggested cooldown in seconds.
    """

    def __init__(self, message: str, retry_after: Optional[float] = None, **details: Any) -> None:
        super().__init__(message, **details)
        self.retry_after = retry_after


class ConfigurationError(KollektivError):
    """Raised when required configuration is missing or malformed."""


class AuthenticationError(KollektivError):
    """Raised when a credential is rejected or cannot be refreshed."""


class RateLimitError(RetryableError):
    """Raised when an upstream API reports a rate limit.

    Args:
        retry_after: Server suggested cooldown in seconds, when provided.
    """

    def __init__(self, message: str, retry_after: Optional[float] = None, **details: Any) -> None:
        super().__init__(message, **details)
        self.retry_after = retry_after


class TeraBoxError(KollektivError):
    """Raised for TeraBox API failures that a retry will not fix.

    Examples: a rejected path, a quota error, a malformed response. Transport
    level problems raise :class:`TeraBoxTransientError` instead, which the
    retry decorator does retry.
    """


class TeraBoxTransientError(TeraBoxError, RetryableError):
    """A TeraBox failure that is worth retrying (5xx, timeout, connection)."""


class R2Error(KollektivError):
    """Raised for object-storage (R2/S3) failures that a retry will not fix."""


class R2TransientError(R2Error, RetryableError):
    """A storage failure worth retrying (5xx, 429, timeout, connection)."""


class NotFoundError(KollektivError):
    """Raised when a remote object, project or resource does not exist."""


class EmailError(KollektivError):
    """Raised when a transactional email cannot be sent."""


class EmailTransientError(EmailError, RetryableError):
    """A Resend failure worth retrying (5xx, 429, timeout)."""


class AuthError(KollektivError):
    """Raised when a request cannot be authenticated or authorised."""


class ArenaError(KollektivError):
    """Raised for worker-agent failures that a retry will not fix."""


class ArenaTransientError(ArenaError, RetryableError):
    """A worker-agent failure worth retrying (5xx, timeout, connection)."""


class GitHubError(KollektivError):
    """Raised for GitHub API failures that a retry will not fix."""


class GitHubTransientError(GitHubError, RetryableError):
    """A GitHub failure worth retrying (5xx, timeout, connection)."""


class BrainError(KollektivError):
    """Raised when the LLM brain cannot produce a usable answer."""


class BrainTransientError(BrainError, RetryableError):
    """An LLM failure worth retrying (5xx, timeout, connection)."""


class StateError(KollektivError):
    """Raised when the shared project state cannot be read or written."""


class TaskTimeoutError(KollektivError):
    """Raised when an agent task exceeds ``TASK_TIMEOUT_SECONDS``."""


class MaxRetriesExceeded(KollektivError):
    """Raised by the retry decorator once every attempt has failed."""

    def __init__(self, message: str, attempts: int = 0, last_error: Optional[BaseException] = None) -> None:
        super().__init__(message, attempts=attempts)
        self.attempts = attempts
        self.last_error = last_error


__all__ = [
    "KollektivError",
    "RetryableError",
    "ConfigurationError",
    "AuthenticationError",
    "RateLimitError",
    "TeraBoxError",
    "TeraBoxTransientError",
    "ArenaError",
    "ArenaTransientError",
    "GitHubError",
    "GitHubTransientError",
    "BrainError",
    "BrainTransientError",
    "StateError",
    "TaskTimeoutError",
    "MaxRetriesExceeded",
]
