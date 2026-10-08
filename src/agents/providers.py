"""Model providers a worker or the orchestrator brain can talk to with your own key.

Kollectiv is bring-your-own-key (BYOK), the same way OpenCode is: the project is
free, and every model call goes to a provider *you* hold an account with. Each
entry below is an OpenAI-compatible ``/chat/completions`` endpoint, so one client
serves all of them.

Two kinds of entry:

``key``
    A hosted API. It needs an API key, which Kollectiv stores encrypted (see
    :mod:`src.utils.token_store`) or reads from the environment variable named in
    ``env``. Nothing is sent anywhere until the worker actually runs a task.
``local``
    A model you run yourself (Ollama, LM Studio). No key and no internet are
    needed for the model itself; the endpoint is on your machine.

``custom`` is any other OpenAI-compatible endpoint you point at with ``base_url``.

Model names change faster than endpoints. Every value here can be overridden per
worker (``model`` in ``ARENA_ACCOUNTS``) or per brain (``BRAIN_MODEL``). A "model
not found" error from a provider means the name in this table is stale: change
the override, not the code.
"""

from __future__ import annotations

from typing import Dict, Optional

from src.utils.errors import ConfigurationError

#: name -> label, base_url, model, kind, env (the API key variable, if any), hint
PROVIDER_PRESETS: Dict[str, Dict[str, str]] = {
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "kind": "key",
        "env": "DEEPSEEK_API_KEY",
        "hint": "API key from platform.deepseek.com",
    },
    "groq": {
        "label": "Groq (free tier)",
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
        "kind": "key",
        "env": "GROQ_API_KEY",
        "hint": "API key from console.groq.com (starts with gsk_)",
    },
    "openrouter": {
        "label": "OpenRouter (includes free models)",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "deepseek/deepseek-chat",
        "kind": "key",
        "env": "OPENROUTER_API_KEY",
        "hint": "API key from openrouter.ai/keys; ':free' model ids cost nothing",
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "kind": "key",
        "env": "OPENAI_API_KEY",
        "hint": "API key from platform.openai.com",
    },
    "gemini": {
        "label": "Google Gemini (OpenAI-compatible endpoint)",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-2.5-flash",
        "kind": "key",
        "env": "GEMINI_API_KEY",
        "hint": "API key from aistudio.google.com",
    },
    "mistral": {
        "label": "Mistral",
        "base_url": "https://api.mistral.ai/v1",
        "model": "mistral-small-latest",
        "kind": "key",
        "env": "MISTRAL_API_KEY",
        "hint": "API key from console.mistral.ai",
    },
    "together": {
        "label": "Together AI",
        "base_url": "https://api.together.xyz/v1",
        "model": "deepseek-ai/DeepSeek-V3",
        "kind": "key",
        "env": "TOGETHER_API_KEY",
        "hint": "API key from api.together.xyz",
    },
    "ollama": {
        "label": "Ollama (runs on this machine, no key, no internet)",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5-coder:7b",
        "kind": "local",
        "env": "",
        "hint": "run `ollama pull <model>` first; no key is needed",
    },
    "lmstudio": {
        "label": "LM Studio (runs on this machine, no key, no internet)",
        "base_url": "http://127.0.0.1:1234/v1",
        "model": "local-model",
        "kind": "local",
        "env": "",
        "hint": "start the local server in LM Studio; no key is needed",
    },
    "custom": {
        "label": "Any OpenAI-compatible endpoint",
        "base_url": "",
        "model": "",
        "kind": "custom",
        "env": "",
        "hint": "give the base URL (ending in /v1) and the model name",
    },
}


def resolve_provider(name: str) -> Dict[str, str]:
    """Return the preset for ``name`` (case-insensitive).

    Raises:
        ConfigurationError: When the provider is not one of :data:`PROVIDER_PRESETS`.
    """
    key = (name or "").strip().lower()
    preset = PROVIDER_PRESETS.get(key)
    if preset is None:
        raise ConfigurationError(
            f"Unknown provider {name!r}. Choose one of: {', '.join(sorted(PROVIDER_PRESETS))}",
        )
    return preset


def default_base_url(name: str) -> Optional[str]:
    """Return the default base URL for a provider, or ``None`` for ``custom``."""
    base = PROVIDER_PRESETS.get((name or "").lower(), {}).get("base_url", "")
    return base or None


#: Brain-facing view: ``{provider: {"base_url": ..., "model": ...}}`` for every
#: preset that has a fixed endpoint. Kept for :mod:`src.orchestrator.brain`.
PROVIDER_DEFAULTS: Dict[str, Dict[str, str]] = {
    name: {"base_url": preset["base_url"], "model": preset["model"]}
    for name, preset in PROVIDER_PRESETS.items()
    if preset["base_url"]
}

__all__ = ["PROVIDER_PRESETS", "PROVIDER_DEFAULTS", "resolve_provider", "default_base_url"]
