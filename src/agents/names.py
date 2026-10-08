"""Human-friendly names for worker agents.

An operator with six workers should not have to read six masked keys to
tell them apart, and a dashboard that shows "Agent 1 … Agent 4" is a mock-up, not
a product. So every agent gets a *name*:

* the account's own ``name`` field wins when it is set (``ARENA_ACCOUNTS`` entries
  accept it, exactly like ``base_url`` or ``model``);
* otherwise a stable name is derived from the account id, so the same account is
  always the same name across restarts and machines — no random shuffling;
* names are made unique within one pool by adding `` 2``, `` 3`` … so a twelve
  account pool never shows two "Vega"s.

Names are labels, not identities: the account id stays the key for everything
(links, statistics, the ledger), and emails are still masked in logs.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, Iterable, Optional, Set, Tuple

#: Names handed out when the operator did not choose one. Short, easy to say in a
#: stand-up, and deliberately not human first names — these are workers, not pets.
AGENT_NAMES: Tuple[str, ...] = (
    "Nova",
    "Vega",
    "Orion",
    "Atlas",
    "Lyra",
    "Cygnus",
    "Phoenix",
    "Hydra",
    "Quasar",
    "Pulsar",
    "Zenith",
    "Draco",
    "Corvus",
    "Aquila",
    "Carina",
    "Volans",
    "Tucana",
    "Mensa",
    "Indus",
    "Corona",
    "Delphi",
    "Onyx",
    "Solace",
    "Cobalt",
    "Ember",
    "Jasper",
    "Kestrel",
    "Lumen",
    "Marlin",
    "Nimbus",
    "Terra",
    "Prism",
    "Quill",
    "Raven",
    "Sable",
    "Talon",
    "Umbra",
    "Vesper",
    "Wren",
    "Xenon",
    "Yarrow",
    "Zephyr",
    "Basalt",
    "Citrine",
    "Dune",
    "Flint",
    "Granite",
    "Halcyon",
    "Iris",
)


def _seed(account_id: str) -> int:
    """Return a stable integer seed for an account id."""
    digest = hashlib.sha256((account_id or "agent").encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def base_name(account_id: str) -> str:
    """Return the preferred name for an account, before uniqueness is applied.

    Args:
        account_id: The agent's stable account id.

    Returns:
        A name from :data:`AGENT_NAMES`, chosen deterministically.
    """
    return AGENT_NAMES[_seed(account_id) % len(AGENT_NAMES)]


def unique_name(preferred: str, used: Set[str]) -> str:
    """Return ``preferred``, or ``preferred 2``/`` 3``/… when already taken.

    Args:
        preferred: The name the operator chose or :func:`base_name` produced.
        used: Names already handed out in this pool.

    Returns:
        A name that is not in ``used``.
    """
    cleaned = (preferred or "").strip() or "Agent"
    if cleaned not in used:
        return cleaned
    suffix = 2
    while f"{cleaned} {suffix}" in used:
        suffix += 1
    return f"{cleaned} {suffix}"


def assign_names(agents: Iterable[Any]) -> Dict[str, str]:
    """Give every agent in a pool a unique name, in place.

    An operator-provided name (``account["name"]`` → ``agent.name``) is kept as
    the preferred value but may still be suffixed when duplicated, because two
    agents with the same name in one dashboard is worse than "Vega 2".

    Args:
        agents: Objects exposing ``account_id`` and a settable ``name``.

    Returns:
        Mapping of account id to the assigned name.
    """
    assigned: Dict[str, str] = {}
    used: Set[str] = set()
    for agent in agents:
        account_id = str(getattr(agent, "account_id", "") or "")
        preferred: Optional[str] = str(getattr(agent, "name", "") or "") or None
        name = unique_name(preferred or base_name(account_id), used)
        used.add(name)
        try:
            agent.name = name
        except AttributeError:  # pragma: no cover - defensive, frozen agents
            continue
        assigned[account_id] = name
    return assigned


__all__ = ["AGENT_NAMES", "assign_names", "base_name", "unique_name"]
