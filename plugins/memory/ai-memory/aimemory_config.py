from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, replace
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
    "AI_MEMORY_SEARCH_SCOPE": "search_scope",
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
    search_scope: str = "global"

    def __post_init__(self) -> None:
        _validate_search_scope(self.search_scope)


def _validate_search_scope(value: Any) -> None:
    if value not in ("global", "project"):
        raise ValueError("search_scope must be global or project")


def _validate_scope_origin(values: dict[str, Any]) -> None:
    if "scope_origin" in values and values["scope_origin"] not in ("automatic", "explicit"):
        raise ValueError("scope_origin must be automatic or explicit")


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
            "key": "search_scope",
            "description": "Recall scope: global (legacy default) or project",
            "default": "global",
            "choices": ["global", "project"],
            "required": False,
        },
        {
            "key": "workspace",
            "description": "Workspace override (blank uses the directory marker)",
            "default": "",
            "required": False,
        },
        {
            "key": "project",
            "description": "Project override (blank uses the directory marker)",
            "default": "",
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
    if "search_scope" in values:
        _validate_search_scope(values["search_scope"])
    _validate_scope_origin(values)
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
            existing = json.loads(p.read_text(encoding="utf-8-sig"))
        except Exception:
            pass

    # Also strip any secrets already persisted in the file
    for secret_key in secrets:
        existing.pop(secret_key, None)

    existing.update(safe_values)
    for key in _MARKER_SCOPE_KEYS:
        if key in safe_values and not str(safe_values[key]).strip():
            existing.pop(key, None)
    if "scope_origin" not in safe_values and any(
        str(safe_values.get(key, "")).strip() for key in _MARKER_SCOPE_KEYS
    ):
        existing["scope_origin"] = "explicit"
    p.write_text(json.dumps(existing, indent=2))
    return skipped


def _read_overrides(hermes_home: str) -> dict[str, Any]:
    """JSON-file overrides, then env-var overrides (env wins)."""
    overrides: dict[str, Any] = {}
    p = Path(hermes_home) / "ai-memory.json" if hermes_home else None

    if p is not None and p.exists():
        try:
            overrides = json.loads(p.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            overrides = {}

    _validate_scope_origin(overrides)
    # Only the complete legacy-generated shape is ambiguous. Never infer
    # provenance from a project name alone or reinterpret customized files.
    effective_mode = os.environ.get("AI_MEMORY_SEARCH_SCOPE") or overrides.get(
        "search_scope", "global"
    )
    legacy_keys = {"server_url", "workspace", "project", "search_scope", "scope_origin"}
    if (
        effective_mode == "project"
        and overrides.get("scope_origin") != "explicit"
        and set(overrides) <= legacy_keys
        and overrides.get("workspace") == "hermes"
        and overrides.get("project") == "hermes-default"
    ):
        overrides.pop("workspace")
        overrides.pop("project")

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


def _parse_marker_flag(text: str, key: str) -> str:
    """Line-based ``key = true`` reader, accepting quoted or bare values.

    Mirrors ai-memory's ``parse_flag_in``: section headers are ignored, the
    first match wins, an optional trailing ``# comment`` is stripped, and the
    surrounding double quotes are removed. Used for ``[briefing]`` and the
    other section-style flags, which the strict key reader above rejects.
    """
    for line in text.splitlines():
        trimmed = line.lstrip("\ufeff").lstrip()
        if not trimmed.startswith(key):
            continue
        rest = trimmed[len(key) :].lstrip()
        if not rest.startswith("="):
            continue
        value = rest[1:].split("#", 1)[0].strip().strip('"')
        if value:
            return value
    return ""


_MARKER_QUOTED_SETTINGS: tuple[str, ...] = (
    "workspace",
    "project",
    "project_strategy",
    "drop_subagent_captures",
    "identity",
    "identity_style",
)
_MARKER_FLAG_SETTINGS: tuple[str, ...] = (
    "default_global",
    "inject_on_session_start",
    "max_chars",
    "contribute",
    "consume",
)


def _marker_declares_settings(text: str) -> bool:
    """Whether a marker declares anything beyond a ``[capture]`` section.

    Mirrors ai-memory's ``declares_more_than_capture``: only a marker whose
    sole content is ``[capture]`` is transparent to the settings walk. The
    check is deliberately conservative - a stray key anywhere turns a marker
    into a boundary, never the other way round.
    """
    if any(_parse_marker_key(text, key) for key in _MARKER_QUOTED_SETTINGS):
        return True
    if any(_parse_marker_flag(text, key) for key in _MARKER_FLAG_SETTINGS):
        return True
    if _parse_marker_flag(text, "server"):
        return True
    for line in text.splitlines():
        trimmed = line.lstrip("\ufeff").lstrip()
        if trimmed.startswith("aliases") and trimmed[len("aliases") :].lstrip().startswith("="):
            return True
        if trimmed.startswith(("[routes", "routes", "route_")):
            return True
    return False


def validated_directory(cwd: str) -> Path | None:
    """Native absolute existing directory, never anchored to the process cwd.

    MSYS spellings are deliberately not translated here: the host's --in
    already translates them, but must still pass its session cwd to providers.
    """
    if not cwd:
        return None
    try:
        directory = Path(cwd).expanduser()
        if directory.is_absolute() and directory.is_dir():
            return directory
    except (OSError, ValueError):
        pass
    return None


def marker_scope(cwd: str) -> dict[str, str]:
    """``workspace``/``project`` declared by the nearest ``.ai-memory.toml``.

    Walks up from *cwd* — stopping at ``$HOME`` or the filesystem root — and
    returns the first marker that pins a scope key; a capture-only marker is
    skipped, matching the server's own walk-up. Returns ``{}`` when *cwd* is
    empty, so a caller without a working directory keeps the configured scope.
    """
    directory = validated_directory(cwd)
    if directory is None:
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


def marker_briefing(cwd: str) -> dict[str, str]:
    """``[briefing]`` flags declared by the nearest settings marker.

    Walks up from *cwd* like the hook clients: a marker whose only content is
    ``[capture]`` is transparent and the walk continues, while the first
    marker that declares any setting is the resolution boundary - even when
    it does not opt into the brief, so an outer marker cannot leak through.
    Stops at ``$HOME`` or the filesystem root. Returns ``{}`` when *cwd* is
    empty or no settings marker exists.
    """
    directory = validated_directory(cwd)
    if directory is None:
        return {}

    home = Path.home().absolute()
    while True:
        candidate = directory / _MARKER_FILENAME
        if candidate.is_file():
            try:
                text = candidate.read_text()
            except OSError:
                text = ""
            if _marker_declares_settings(text):
                return {
                    "inject_on_session_start": _parse_marker_flag(
                        text, "inject_on_session_start"
                    ),
                    "max_chars": _parse_marker_flag(text, "max_chars"),
                }
        if directory == home or directory.parent == directory:
            return {}
        directory = directory.parent


def load_config(hermes_home: str, base: AiMemoryConfig | None = None) -> AiMemoryConfig:
    """Apply file/env overrides to defaults or an unscoped programmatic base."""
    overrides = _read_overrides(hermes_home)
    return replace(
        base or AiMemoryConfig(),
        **{k: v for k, v in overrides.items() if hasattr(AiMemoryConfig, k)},
    )
