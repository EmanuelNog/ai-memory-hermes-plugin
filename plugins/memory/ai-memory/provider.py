from __future__ import annotations

import json
import logging
import sys
import threading
from pathlib import Path
from typing import Any

# Ensure sibling modules are findable when this file is loaded standalone
# (Hermes pre-loads submodules before executing __init__.py).
_PLUGIN_DIR = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from client import AiMemoryClient  # noqa: E402
from config import AiMemoryConfig, get_config_schema, load_config, save_config  # noqa: E402

try:
    from agent.memory_provider import MemoryProvider  # type: ignore[import-untyped]
except ImportError:
    from abc import ABC as _ABC

    class MemoryProvider(_ABC):  # type: ignore[no-redef]
        pass

log = logging.getLogger(__name__)


class AiMemoryProvider(MemoryProvider):
    def __init__(
        self,
        client: AiMemoryClient | None = None,
        config: AiMemoryConfig | None = None,
    ) -> None:
        self._config = config or AiMemoryConfig()
        self._client = client or AiMemoryClient(self._config)
        self._lock = threading.Lock()
        self.session_id: str = ""
        self._hermes_home: str = ""
        # Previous-session handoff, fetched once per session in initialize()
        # and surfaced through system_prompt_block(). None = none pending.
        self._handoff_context: str | None = None

    @property
    def name(self) -> str:
        return "ai-memory"

    def is_available(self) -> bool:
        # ai-memory does not require authentication by default; the provider
        # is available whenever a server URL is configured.
        return bool(self._config.server_url)

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self.session_id = session_id
        hermes_home = kwargs.get("hermes_home", "")
        self._hermes_home = hermes_home

        with self._lock:
            if hermes_home:
                self._config = load_config(hermes_home)
                self._client = AiMemoryClient(self._config)

            server_url = kwargs.get("ai_memory_server_url", "")
            if server_url:
                self._config.server_url = server_url

            auth_token = kwargs.get("ai_memory_auth_token", "")
            if auth_token:
                self._config.auth_token = auth_token

            # Workspace precedence: explicit override, then what Hermes
            # reports, then whatever ai-memory.json already held.
            # `agent_workspace` is a workspace NAME, not a filesystem path.
            workspace = kwargs.get("ai_memory_workspace", "") or kwargs.get(
                "agent_workspace", ""
            )
            if workspace:
                self._config.workspace = workspace

            # Project precedence: explicit override, then a project already
            # configured in ai-memory.json, then a name derived from the
            # Hermes profile identity.
            #
            # Hermes 0.20.5 passes neither `project` nor `profile` - it
            # passes `agent_identity`. The old code read the missing
            # `profile` kwarg, so the else branch ran on every session and
            # clobbered whatever ai-memory.json configured, pinning every
            # session to "hermes-default".
            project = kwargs.get("project", "")
            if project:
                self._config.project = project
            elif not self._config.project:
                identity = kwargs.get("agent_identity", "") or "default"
                self._config.project = f"hermes-{identity}"

            self._client = AiMemoryClient(self._config)

        # One handoff fetch per session, never polled. Loopback server plus
        # a 2s timeout, so a synchronous call cannot hold up startup, and
        # any failure leaves the provider working without a handoff.
        self._handoff_context = None
        try:
            self._handoff_context = self._client.fetch_handoff(
                agent="hermes",
                workspace=self._config.workspace,
                project=self._config.project,
            )
        except Exception:
            log.warning("ai-memory handoff fetch failed", exc_info=True)

    def get_config_schema(self) -> list[dict[str, Any]]:
        return get_config_schema()

    def save_config(self, values: dict[str, Any], hermes_home: str) -> list[str]:
        return save_config(values, hermes_home)

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "ai_memory_search",
                "description": "Search the ai-memory wiki for relevant context",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "max_results": {"type": "integer", "default": 5},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "ai_memory_write",
                "description": "Write a new page to the ai-memory wiki",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Wiki page path"},
                        "body": {"type": "string", "description": "Markdown body"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["path", "body"],
                },
            },
            {
                "name": "ai_memory_status",
                "description": "Check ai-memory server health",
                "input_schema": {"type": "object", "properties": {}},
            },
        ]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> str:
        if tool_name == "ai_memory_search":
            return json.dumps(self._search(args))
        if tool_name == "ai_memory_write":
            return json.dumps(self._write(args))
        if tool_name == "ai_memory_status":
            return json.dumps(self._status())
        raise ValueError(f"Unknown tool: {tool_name}")

    _BASE_PROMPT = "Long-term memory is backed by ai-memory wiki."

    def system_prompt_block(self) -> str:
        if not self._handoff_context:
            return self._BASE_PROMPT
        return (
            self._BASE_PROMPT
            + "\n\nPrevious session handoff:\n"
            + self._handoff_context
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        results = self._search({"query": query, "max_results": 3})
        if results.get("ok") and results.get("results"):
            return "\n\n".join(r.get("snippet", "") for r in results["results"])
        return ""

    def queue_prefetch(self, query: str) -> None:
        threading.Thread(target=self.prefetch, args=(query,), daemon=True).start()

    def sync_turn(self, user: str, assistant: str, *, session_id: str = "", **kwargs: Any) -> None:
        sid = session_id or self.session_id
        ws = self._config.workspace
        proj = self._config.project

        def _do() -> None:
            try:
                # ai-memory's own lifecycle event. The previous
                # event="user-prompt" with a {user, assistant} body was
                # accepted with 202 and then stored with an EMPTY body,
                # because ai-memory reads `prompt` off a
                # `user-prompt-submit` payload. Every Hermes turn was lost.
                self._client.send_hook(
                    event="user-prompt-submit",
                    session_id=sid,
                    payload={"session_id": sid, "prompt": user},
                    workspace=ws,
                    project=proj,
                )
            except Exception:
                log.warning("ai-memory sync_turn failed", exc_info=True)

        threading.Thread(target=_do, daemon=True).start()

    def on_session_end(self, messages: list[dict[str, Any]], **kwargs: Any) -> None:
        sid = self.session_id
        ws = self._config.workspace
        proj = self._config.project

        def _do() -> None:
            try:
                self._client.send_hook(
                    event="session-end",
                    session_id=sid,
                    payload={"session_id": sid, "messages": messages},
                    workspace=ws,
                    project=proj,
                )
            except Exception:
                log.warning("ai-memory on_session_end failed", exc_info=True)

        threading.Thread(target=_do, daemon=True).start()

    def on_memory_write(
        self, action: str, target: str, content: str, metadata: dict[str, Any] | None = None
    ) -> None:
        if action in ("write", "append"):
            try:
                self._client.write_page(
                    path=f"hermes-memory/{target}.md",
                    body=content,
                    tags=["hermes", "mirror"],
                    workspace=self._config.workspace,
                    project=self._config.project,
                )
            except Exception:
                log.warning("on_memory_write hook failed", exc_info=True)

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs: Any,
    ) -> None:
        """Follow /new, /reset, /resume, /branch and context compression.

        Signature mirrors ``MemoryProvider.on_session_switch``. Hermes swaps
        session_id on these paths without rebuilding the provider, so without
        this every later observation kept the id of the session the provider
        was first initialized with.
        """
        with self._lock:
            self.session_id = new_session_id
            if reset:
                # A reset starts a clean context; the previous session's
                # handoff must not leak into it.
                self._handoff_context = None

    def shutdown(self) -> None:
        pass

    def _search(self, args: dict[str, Any]) -> dict[str, Any]:
        query = args.get("query", "")
        max_results = args.get("max_results", 5)
        if not isinstance(max_results, int) or isinstance(max_results, bool):
            max_results = 5
        # Deliberately UNSCOPED: recall searches every project so Hermes
        # can see what Claude Code and Codex wrote in their own projects.
        # Writes stay scoped to the Hermes workspace/project; only reads
        # are global.
        results = self._client.search(query=query, limit=max_results)
        return {"ok": True, "results": results}

    def _write(self, args: dict[str, Any]) -> dict[str, Any]:
        result = self._client.write_page(
            path=args.get("path", ""),
            body=args.get("body", ""),
            tags=args.get("tags"),
            workspace=self._config.workspace,
            project=self._config.project,
        )
        if not (result.get("ok", False) or result.get("page_id")):
            return result
        return {"ok": True, "written": args.get("path")}

    def _status(self) -> dict[str, Any]:
        return self._client.status()
