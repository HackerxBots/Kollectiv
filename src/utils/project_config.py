"""``.kollektiv.yml`` — per-project defaults that live with the project.

A repository that Kollektiv works on can carry its own settings: how many worker
agents to use, what a run is allowed to cost, which brain model to prefer. The
file is optional — without it, everything falls back to environment settings —
and it is read the same way by the CLI, the API, the MCP server and the gateway,
so "how this project is supposed to run" has exactly one definition.

Lookup order: an explicit path (``PROJECT_CONFIG_PATH`` or an argument), then
``.kollektiv.yml`` / ``.kollektiv.yaml`` / ``.kollektiv.json`` in the working
directory, then in the repository root. YAML is parsed with PyYAML when it is
installed and with the built-in strict subset reader otherwise (see
:mod:`src.utils.yaml_subset`); JSON always works.

Nothing here is allowed to raise. A broken config file must never stop a run —
it is reported in ``ProjectConfig.problems``, logged, and the defaults are used,
because "the project failed to start because of a typo in a config file" is a
worse outcome than "the config file was ignored, loudly".
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.utils.errors import ConfigurationError
from src.utils.logger import get_logger

LOGGER = get_logger(__name__)

#: File names checked, in order.
CONFIG_NAMES = (".kollektiv.yml", ".kollektiv.yaml", ".kollektiv.json")

#: Top-level keys that are understood. Anything else is reported, not applied.
KNOWN_KEYS = ("project", "budget", "brain", "storage", "workers")

#: A starter file, written by ``kollektiv init-config`` (comments included).
STARTER_TEMPLATE = """# Kollektiv project settings. Everything here is optional.
#
# This file is read by the CLI, the API, the MCP server and the gateway, and it
# wins over the matching environment variable for this project only.

project:
  # Worker agents to plan for and run with (1-20).
  n_agents: 3
  # Simultaneous worker calls; defaults to n_agents.
  max_concurrency: 3

budget:
  # Refuse to run a project whose *estimate* exceeds this. 0 = no cap.
  max_usd: 0
  # Warn from this fraction of the cap (0.8 = 80%).
  warn_at: 0.8

brain:
  # Any OpenAI-compatible provider name (deepseek, groq, openai, ollama, ...).
  provider: deepseek
  # Leave empty to use the provider's default model.
  model: ""
  # 0.0-1.0.
  temperature: 0.3

storage:
  # local | r2 | terabox -- local needs no credentials at all.
  backend: local
"""


@dataclass
class ProjectConfig:
    """The resolved project configuration, with its origin and any complaints.

    Attributes:
        path: The file this came from, or ``None`` when no file was found.
        n_agents: Worker agents to plan for.
        max_concurrency: Simultaneous worker calls.
        budget_max_usd: Refuse to run above this estimate (0 = no cap).
        budget_warn_at: Warn from this fraction of the cap.
        brain_provider: Preferred brain provider name.
        brain_model: Preferred brain model name.
        brain_temperature: Preferred sampling temperature.
        storage_backend: ``local``, ``r2`` or ``terabox``.
        raw: The parsed document, for anything that needs the original.
        problems: Human-readable issues; the run still proceeds.
    """

    path: Optional[Path] = None
    n_agents: Optional[int] = None
    max_concurrency: Optional[int] = None
    budget_max_usd: Optional[float] = None
    budget_warn_at: Optional[float] = None
    brain_provider: str = ""
    brain_model: str = ""
    brain_temperature: Optional[float] = None
    storage_backend: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)
    problems: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Return the resolved config as JSON."""
        return {
            "path": str(self.path) if self.path else None,
            "n_agents": self.n_agents,
            "max_concurrency": self.max_concurrency,
            "budget_max_usd": self.budget_max_usd,
            "budget_warn_at": self.budget_warn_at,
            "brain": {
                "provider": self.brain_provider,
                "model": self.brain_model,
                "temperature": self.brain_temperature,
            },
            "storage_backend": self.storage_backend,
            "problems": list(self.problems),
        }

    @property
    def found(self) -> bool:
        """Whether a config file was actually read."""
        return self.path is not None


def find_config_file(start: Optional[Path] = None) -> Optional[Path]:
    """Find the nearest project config file.

    Args:
        start: Directory to search from (defaults to the working directory).

    Returns:
        The first :data:`CONFIG_NAMES` match in ``start``, then in each parent up
        to the filesystem root, or ``None``.
    """
    base = Path(start or Path.cwd()).expanduser().resolve()
    for directory in (base, *base.parents):
        for name in CONFIG_NAMES:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return None


def parse_config_text(text: str, *, suffix: str = ".yml", source: str = "<config>") -> Any:
    """Parse a config document as YAML (if available) or as the strict subset.

    Args:
        text: The file contents.
        suffix: The file extension, used to decide whether the text is JSON.
        source: Name used in error messages.

    Returns:
        The parsed document.

    Raises:
        ConfigurationError: When the text cannot be parsed.
    """
    if suffix == ".json":
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigurationError(f"{source} is not valid JSON: {exc}") from exc
    try:
        import yaml  # type: ignore[import-untyped]

        return yaml.safe_load(text)
    except ImportError:
        from src.utils.yaml_subset import loads as subset_loads

        try:
            return subset_loads(text, source=source)
        except ConfigurationError as exc:
            raise ConfigurationError(
                f"{exc}. Install pyyaml (pip install pyyaml) for full YAML, or use a .kollektiv.json file"
            ) from exc
    except Exception as exc:  # noqa: BLE001 - PyYAML raises its own error tree
        raise ConfigurationError(f"{source} is not valid YAML: {exc}") from exc


def load_project_config(path: Optional[str] = None, start: Optional[Path] = None) -> ProjectConfig:
    """Load the project configuration, never raising.

    Args:
        path: Explicit file path (``PROJECT_CONFIG_PATH`` or a CLI flag). A
            missing explicit path is reported as a problem.
        start: Directory to search from when no path is given.

    Returns:
        The resolved :class:`ProjectConfig`. Unreadable keys are listed in
        ``problems`` and fall back to their defaults.
    """
    config = ProjectConfig()
    target: Optional[Path] = Path(path).expanduser() if path else find_config_file(start)
    if target is None:
        return config
    if not target.is_file():
        config.problems.append(f"{target} does not exist")
        LOGGER.warning("Project config %s does not exist; using defaults", target)
        return config

    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        config.problems.append(f"could not read {target}: {exc}")
        LOGGER.error("Could not read the project config %s: %s", target, exc)
        return config

    config.path = target
    try:
        document = parse_config_text(text, suffix=target.suffix.lower(), source=target.name)
    except ConfigurationError as exc:
        config.problems.append(str(exc))
        LOGGER.error("Project config %s was ignored: %s", target, exc)
        return config

    if document is None:
        return config
    if not isinstance(document, dict):
        config.problems.append(f"{target.name} must contain a mapping at the top level")
        return config

    config.raw = document
    for key in document:
        if key not in KNOWN_KEYS:
            config.problems.append(f"unknown section {key!r} in {target.name} (ignored)")
    _apply(config, document, target.name)
    if config.problems:
        LOGGER.warning("Project config %s: %s", target, "; ".join(config.problems))
    else:
        LOGGER.info("Loaded project config from %s", target)
    return config


def _apply(config: ProjectConfig, document: Dict[str, Any], name: str) -> None:
    """Copy validated values from ``document`` onto ``config``.

    Args:
        config: Config being built (problems are appended here).
        document: The parsed file.
        name: File name for messages.
    """
    project = document.get("project")
    if isinstance(project, dict):
        config.n_agents = _int_value(project.get("n_agents"), 1, 20, "project.n_agents", name, config.problems)
        config.max_concurrency = _int_value(
            project.get("max_concurrency"), 1, 100, "project.max_concurrency", name, config.problems
        )
    elif project is not None:
        config.problems.append(f"{name}: 'project' must be a mapping")

    budget = document.get("budget")
    if isinstance(budget, dict):
        config.budget_max_usd = _float_value(budget.get("max_usd"), 0.0, "budget.max_usd", name, config.problems)
        warn_at = _float_value(budget.get("warn_at"), 0.0, "budget.warn_at", name, config.problems)
        if warn_at is not None and not 0.0 < warn_at <= 1.0:
            config.problems.append(f"{name}: budget.warn_at must be between 0 and 1")
            warn_at = None
        config.budget_warn_at = warn_at
    elif budget is not None:
        config.problems.append(f"{name}: 'budget' must be a mapping")

    brain = document.get("brain")
    if isinstance(brain, dict):
        config.brain_provider = str(brain.get("provider") or "")
        config.brain_model = str(brain.get("model") or "")
        config.brain_temperature = _float_value(brain.get("temperature"), 0.0, "brain.temperature", name, config.problems)
    elif brain is not None:
        config.problems.append(f"{name}: 'brain' must be a mapping")

    storage = document.get("storage")
    if isinstance(storage, dict):
        backend = str(storage.get("backend") or "")
        if backend and backend not in ("local", "r2", "terabox"):
            config.problems.append(f"{name}: storage.backend must be local, r2 or terabox")
        else:
            config.storage_backend = backend
    elif storage is not None:
        config.problems.append(f"{name}: 'storage' must be a mapping")

    workers = document.get("workers")
    if workers is not None and not isinstance(workers, (list, dict)):
        config.problems.append(f"{name}: 'workers' must be a list or a mapping")


def _int_value(
    value: Any, low: int, high: int, label: str, name: str, problems: List[str]
) -> Optional[int]:
    """Validate an integer setting.

    Args:
        value: The raw value (``None`` means "not set").
        low: Inclusive minimum.
        high: Inclusive maximum.
        label: Dotted key name for messages.
        name: File name for messages.
        problems: List to append complaints to.

    Returns:
        The validated integer, or ``None``.
    """
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        problems.append(f"{name}: {label} must be a whole number")
        return None
    if not low <= number <= high:
        problems.append(f"{name}: {label} must be between {low} and {high}")
        return None
    return number


def _float_value(value: Any, low: float, label: str, name: str, problems: List[str]) -> Optional[float]:
    """Validate a float setting.

    Args:
        value: The raw value (``None`` means "not set").
        low: Inclusive minimum.
        label: Dotted key name for messages.
        name: File name for messages.
        problems: List to append complaints to.

    Returns:
        The validated float, or ``None``.
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        problems.append(f"{name}: {label} must be a number")
        return None
    if number < low:
        problems.append(f"{name}: {label} must be at least {low}")
        return None
    return number


__all__ = [
    "CONFIG_NAMES",
    "KNOWN_KEYS",
    "STARTER_TEMPLATE",
    "ProjectConfig",
    "find_config_file",
    "load_project_config",
    "parse_config_text",
]
