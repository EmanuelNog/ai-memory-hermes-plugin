from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Ensure sibling modules are findable when this file is loaded standalone
# (Hermes pre-loads submodules before executing __init__.py).
_PLUGIN_DIR = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

DEFAULT_SERVER_URL = "http://127.0.0.1:49374"

# Env vars the plugin honours; each one overrides the JSON file below it.
_ENV_MAP: dict[str, str] = {
    "AI_MEMORY_SERVER_URL": "server_url",
    "AI_MEMORY_AUTH_TOKEN": "auth_token",
    "AI_MEMORY_API_KEY": "api_key",
    "AI_MEMORY_WORKSPACE": "workspace",
    "AI_MEMORY_PROJECT": "project",
}

# Scope keys ``.ai-memory.toml`` may pin, in the server's own order.
_MARKER_SCOPE_KEYS: tuple[str, ...] = ("workspace", "project")
_MARKER_FILENAME = ".ai-memory.toml"


@dataclass
class AiMemoryConfig:
    server_url: str = DEFAULT_SERVER_URL
    api_key: str = ""
    auth_token: str = ""
    workspace: str = "hermes"
    project: str = "hermes-default"


def get_config_schema() -> list[dict[str, Any]]:
    return [
        {
            "key": "server_url",
            "description": "ai-memory server URL",
            "default": DEFAULT_SERVER_URL,
            "required": False,
        },
        {
            "key": "api_key",
            "description": "ai-memory API key (optional for local mode)",
            "secret": True,
            "env_only": True,
            "required": False,
            "env_var": "AI_MEMORY_API_KEY",
        },
        {
            "key": "auth_token",
            "description": "ai-memory auth token (optional for local mode)",
            "secret": True,
            "env_only": True,
            "required": False,
            "env_var": "AI_MEMORY_AUTH_TOKEN",
        },
        {
            "key": "workspace",
            "description": "ai-memory workspace name",
            "default": "hermes",
            "required": False,
        },
        {
            "key": "project",
            "description": "ai-memory project name",
            "default": "hermes-default",
            "required": False,
        },
    ]


def _secret_keys() -> dict[str, str]:
    """Return {config_key: env_var_name} for all secret/env-only fields."""
    schema = get_config_schema()
    result: dict[str, str] = {}
    for item in schema:
        if item.get("secret"):
            env_var = item.get("env_var", f"AI_MEMORY_{item['key'].upper()}")
            result[item["key"]] = env_var
    return result


def save_config(values: dict[str, Any], hermes_home: str) -> list[str]:
    """Save non-secret config values to disk. Returns list of skipped secret keys."""
    secrets = _secret_keys()
    skipped: list[str] = []
    safe_values: dict[str, Any] = {}
    for k, v in values.items():
        if k in secrets:
            log.info("secret %s not written to disk — set %s instead", k, secrets[k])
            skipped.append(k)
            continue
        safe_values[k] = v

    p = Path(hermes_home) / "ai-memory.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if p.exists():
        try:
            existing = json.loads(p.read_text())
        except Exception:
            pass

    # Also strip any secrets already persisted in the file
    for secret_key in secrets:
        existing.pop(secret_key, None)

    existing.update(safe_values)
    p.write_text(json.dumps(existing, indent=2))
    return skipped


def _read_overrides(hermes_home: str) -> dict[str, Any]:
    """JSON-file overrides, then env-var overrides (env wins)."""
    overrides: dict[str, Any] = {}
    p = Path(hermes_home) / "ai-memory.json" if hermes_home else None

    if p is not None and p.exists():
        try:
            overrides = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            overrides = {}

    for env_key, attr in _ENV_MAP.items():
        val = os.environ.get(env_key)
        if val:
            overrides[attr] = val

    return overrides


def explicit_scope_keys(hermes_home: str) -> set[str]:
    """Scope keys the operator set explicitly (file or env), never defaults.

    ``initialize()`` must know whether ``workspace``/``project`` came from the
    operator or from ``AiMemoryConfig``'s defaults: an explicit value beats the
    ``.ai-memory.toml`` marker, a default loses to it.
    """
    overrides = _read_overrides(hermes_home)
    return {key for key in _MARKER_SCOPE_KEYS if str(overrides.get(key, "")).strip()}


def _parse_marker_key(text: str, key: str) -> str:
    """Line-based ``key = "value"`` reader, mirroring ai-memory's parser.

    Section headers are ignored and only double-quoted values are accepted, so
    the plugin resolves the same marker text the server and the shell hooks do.
    """
    for line in text.splitlines():
        trimmed = line.lstrip()
        if not trimmed.startswith(key):
            continue
        rest = trimmed[len(key) :].lstrip()
        if not rest.startswith("="):
            continue
        rest = rest[1:].lstrip()
        if not rest.startswith('"'):
            continue
        end = rest[1:].find('"')
        if end < 0:
            continue
        return rest[1 : end + 1]
    return ""


def marker_scope(cwd: str) -> dict[str, str]:
    """``workspace``/``project`` declared by the nearest ``.ai-memory.toml``.

    Walks up from *cwd* — stopping at ``$HOME`` or the filesystem root — and
    returns the first marker that pins a scope key; a capture-only marker is
    skipped, matching the server's own walk-up. Returns ``{}`` when *cwd* is
    empty, so a caller without a working directory keeps the configured scope.
    """
    if not cwd:
        return {}

    try:
        directory = Path(cwd).expanduser().absolute()
    except OSError:
        return {}

    home = Path.home().absolute()
    while True:
        candidate = directory / _MARKER_FILENAME
        if candidate.is_file():
            try:
                text = candidate.read_text()
            except OSError:
                text = ""
            scope = {
                key: value for key in _MARKER_SCOPE_KEYS if (value := _parse_marker_key(text, key))
            }
            if scope:
                return scope
        if directory == home or directory.parent == directory:
            return {}
        directory = directory.parent


def load_config(hermes_home: str) -> AiMemoryConfig:
    overrides = _read_overrides(hermes_home)
    overrides.setdefault("server_url", DEFAULT_SERVER_URL)

    return AiMemoryConfig(**{k: v for k, v in overrides.items() if hasattr(AiMemoryConfig, k)})
