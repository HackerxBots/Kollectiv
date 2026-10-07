"""Gateway policy: what a client may call, and what needs a second look.

A policy is four lists of glob patterns matched against namespaced tool names
(``projects.*``, ``connectors.telegram.send_message``) plus one flag:

* ``allow`` — patterns the client may call. Empty means **nothing**, because a
  policy that fails open is not a policy. Every preset states its allow list,
  including ``["*"]`` for the permissive ones.
* ``deny`` — patterns refused outright. Evaluated **after** ``allow``, so a
  narrow deny always wins.
* ``confirm`` — tools that additionally require ``confirm=true`` in the call.
  This is a second, explicit "yes" even when the client is allowed to do it.
* ``read_only`` — refuse every tool the catalogue marks dangerous. A read-only
  client can be handed to a model that has not earned trust yet.

Policies are stored per client as JSON on the client row, and may be overridden
by a file (``GATEWAY_POLICY_PATH``) so a deployment can keep them in git. The
rules are deliberately small and boring: a policy language you cannot explain in
a paragraph is a policy language people get wrong.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

from src.utils.logger import get_logger

LOGGER = get_logger(__name__)

#: Presets a client can be created with, so nobody has to write globs by hand.
POLICY_PRESETS: Dict[str, Dict[str, Any]] = {
    # Everything read-only: safe to hand to an assistant that only looks.
    "read-only": {"allow": ["*"], "deny": [], "confirm": [], "read_only": True},
    # The dashboard: reads freely, writes with an explicit confirm flag.
    "dashboard": {
        "allow": ["*"],
        "deny": [],
        "confirm": ["*.run", "*.create*", "*.upload", "connectors.*.send*", "connectors.*.post*", "connectors.*.create*", "connectors.*.comment*"],
        "read_only": False,
    },
    # An agent that may work a project but not talk to the outside world.
    "worker": {"allow": ["projects.*", "storage.*", "agents.*"], "deny": ["gateway.*"], "confirm": ["projects.create", "projects.upload_file"], "read_only": False},
    # A chat connector that may only send messages it is told to send.
    "messenger": {"allow": ["connectors.*", "projects.list", "projects.status"], "deny": ["connectors.github.*"], "confirm": ["connectors.*"], "read_only": False},
    # Full trust (the operator's own token), still denied the gateway's own admin tools.
    "admin": {"allow": ["*"], "deny": [], "confirm": [], "read_only": False},
}

@dataclass(frozen=True)
class Policy:
    """A parsed, ready-to-check policy.

    Attributes:
        allow: Patterns the client may call; empty means everything.
        deny: Patterns refused after ``allow`` is satisfied.
        confirm: Patterns that additionally require an explicit confirmation.
        read_only: When true, every dangerous tool is refused.
        name: Optional label (the preset or file it came from).
    """

    allow: Tuple[str, ...] = ()
    deny: Tuple[str, ...] = ()
    confirm: Tuple[str, ...] = ()
    read_only: bool = False
    name: str = "custom"

    # -- construction --------------------------------------------------
    @classmethod
    def from_dict(cls, payload: Optional[Dict[str, Any]], name: str = "custom") -> "Policy":
        """Build a policy from stored JSON.

        Args:
            payload: ``{"allow": [...], "deny": [...], "confirm": [...], "read_only": bool}``.
            name: Label recorded on the policy.

        Returns:
            The parsed policy. Unknown keys are ignored, so a policy written for
            a newer version still loads.
        """
        data = payload or {}
        return cls(
            allow=tuple(str(item) for item in data.get("allow") or ()),
            deny=tuple(str(item) for item in data.get("deny") or ()),
            confirm=tuple(str(item) for item in data.get("confirm") or ()),
            read_only=bool(data.get("read_only", False)),
            name=name,
        )

    @classmethod
    def from_json(cls, text: str, name: str = "custom") -> "Policy":
        """Build a policy from a JSON string (a malformed one becomes the default)."""
        try:
            payload = json.loads(text or "{}")
        except json.JSONDecodeError as exc:
            LOGGER.warning("Gateway policy %s is not valid JSON (%s); falling back to allow-read", name, exc)
            return cls(allow=("*",), name=f"{name} (unreadable)")
        if not isinstance(payload, dict):
            return cls(allow=("*",), name=f"{name} (not an object)")
        return cls.from_dict(payload, name=name)

    @classmethod
    def preset(cls, name: str) -> "Policy":
        """Return a named preset, or the dashboard preset for an unknown name.

        Args:
            name: One of :data:`POLICY_PRESETS`.

        Returns:
            The preset policy, labelled with the name that was requested.
        """
        chosen = POLICY_PRESETS.get(name)
        if chosen is None:
            LOGGER.warning("Unknown gateway policy preset %r; using 'dashboard'", name)
            chosen = POLICY_PRESETS["dashboard"]
            return cls.from_dict(chosen, name="dashboard")
        return cls.from_dict(chosen, name=name)

    # -- serialisation -------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        """Return the policy in its stored shape."""
        return {
            "name": self.name,
            "allow": list(self.allow),
            "deny": list(self.deny),
            "confirm": list(self.confirm),
            "read_only": self.read_only,
        }

    def to_json(self) -> str:
        """Return the stored JSON for this policy."""
        return json.dumps({key: value for key, value in self.to_dict().items() if key != "name"})

    # -- evaluation ----------------------------------------------------
    def requires_confirmation(self, tool: str) -> bool:
        """Return whether ``tool`` additionally needs ``confirm=true``.

        Args:
            tool: Namespaced tool name.

        Returns:
            True when one of the ``confirm`` globs matches.
        """
        return self._matches(self.confirm, tool)

    def decision(self, tool: str, *, dangerous: bool = False, confirm: bool = False) -> Tuple[bool, str]:
        """Decide whether ``tool`` may run.

        Args:
            tool: Namespaced tool name, e.g. ``projects.run``.
            dangerous: Whether the catalogue marks the tool as changing things.
            confirm: Whether the caller passed an explicit confirmation.

        Returns:
            ``(allowed, reason)``. The reason is a sentence suitable for a log
            line or an error body: it says *which rule* decided.
        """
        if not self._matches(self.allow, tool):
            return False, f"policy {self.name!r} does not allow {tool}"
        if self._matches(self.deny, tool):
            return False, f"policy {self.name!r} denies {tool}"
        if self.read_only and dangerous:
            return False, f"policy {self.name!r} is read-only and {tool} changes data"
        if self.requires_confirmation(tool) and not confirm:
            return False, f"policy {self.name!r} requires confirm=true for {tool}"
        return True, "allowed"

    @staticmethod
    def _matches(patterns: Iterable[str], tool: str) -> bool:
        """Return whether any pattern matches ``tool`` (``*`` spans dots)."""
        for pattern in patterns:
            if pattern == "*":
                return True
            if fnmatch.fnmatchcase(tool, pattern) or fnmatch.fnmatchcase(tool, f"{pattern}.*"):
                return True
        return False


def load_policy_file(path: str) -> Dict[str, Dict[str, Any]]:
    """Load a policy file: ``{"client-name": {...policy...}, "*": {...defaults...}}``.

    Args:
        path: Filesystem path to the JSON file.

    Returns:
        The parsed mapping, or an empty dict when the file is missing or
        unreadable (a broken policy file must not stop the gateway; it just
        falls back to the per-client policies).
    """
    target = Path(path).expanduser()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        LOGGER.warning("GATEWAY_POLICY_PATH %s does not exist", target)
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.error("Could not read the gateway policy file %s: %s", target, exc)
        return {}
    if not isinstance(payload, dict):
        LOGGER.error("Gateway policy file %s must contain an object", target)
        return {}
    return {str(key): value for key, value in payload.items() if isinstance(value, dict)}


def resolve_policy(
    client_name: str,
    stored_json: str,
    *,
    file_policies: Optional[Dict[str, Dict[str, Any]]] = None,
    role: str = "client",
) -> Policy:
    """Combine the stored policy, the policy file and the client's role.

    Precedence: the file wins for the named client, then the file's ``"*"``
    entry, then the stored JSON, then the role's preset. A file entry is what an
    operator edits to override a live client without touching the database.

    Args:
        client_name: The client the policy is for.
        stored_json: The policy JSON stored on the client row.
        file_policies: Mapping from :func:`load_policy_file`, when configured.
        role: The client's role, used as the last-resort preset.

    Returns:
        The policy to enforce.
    """
    from_file = (file_policies or {}).get(client_name) or (file_policies or {}).get("*")
    if from_file is not None:
        return Policy.from_dict(from_file, name=f"file:{client_name}")
    if stored_json and stored_json not in ("", "{}"):
        return Policy.from_json(stored_json, name=f"client:{client_name}")
    return Policy.preset(role if role in POLICY_PRESETS else "dashboard")


__all__ = [
    "POLICY_PRESETS",
    "Policy",
    "load_policy_file",
    "resolve_policy",
]
