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

            workspace = kwargs.get("ai_memory_workspace", "")
            if workspace:
                self._config.workspace = workspace

            project = kwargs.get("project", "")
            if project:
                self._config.project = project
            else:
                profile = kwargs.get("profile", "default")
                self._config.project = f"hermes-{profile}"

            self._client = AiMemoryClient(self._config)

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

    def system_prompt_block(self) -> str:
        return "Long-term memory is backed by ai-memory wiki."

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
                self._client.send_hook(
                    event="user-prompt",
                    session_id=sid,
                    payload={
                        # Server wire contract (payload.rs extract_content):
                        # observation content comes from `prompt` / `message` /
                        # `text` keys. {user, assistant} alone produced EMPTY
                        # observations (body_chars=0) — auto-improve reviewed
                        # content-less sessions and found nothing to propose.
                        "prompt": user,
                        "text": assistant,
                        "messages": [
                            {"role": "user", "content": user},
                            {"role": "assistant", "content": assistant},
                        ],
                    },
                    workspace=ws,
                    project=proj,
                )
            except Exception:
                pass

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
                    payload={
                        "messages": messages,
                        # Server contract keys so the consolidation LLM gets
                        # extractable content, not an empty body.
                        "prompt": "\n".join(
                            m.get("content", "") for m in messages if m.get("role") == "user"
                        ),
                        "text": "\n".join(
                            m.get("content", "") for m in messages if m.get("role") == "assistant"
                        ),
                    },
                    workspace=ws,
                    project=proj,
                )
            except Exception:
                pass

        threading.Thread(target=_do, daemon=True).start()

    def on_pre_compress(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        """Called by Hermes BEFORE context compression discards messages.

        Hermes v0.19+ calls this hook on every compaction. The ai-memory
        server implements a ``pre-compact`` hook event that checkpoints the
        session (refreshes ``sessions/<id>.md`` via LLM consolidation)
        WITHOUT ending the session — so long-running Hermes sessions get
        their knowledge captured at each compression stage instead of only
        at session end (which may be days away or never).

        The hook runs on a pooled daemon thread inside Hermes; the send is
        fire-and-forget. Returns "" (no contribution to the compression
        summary prompt — the server-side consolidation is the checkpoint).
        """
        sid = self.session_id
        ws = self._config.workspace
        proj = self._config.project

        def _do() -> None:
            try:
                self._client.send_hook(
                    event="pre-compact",
                    session_id=sid,
                    payload={
                        "messages": messages,
                        # Server contract keys: the PreCompact consolidation
                        # extracts content from prompt/text (payload.rs).
                        "prompt": "\n".join(
                            m.get("content", "") for m in messages if m.get("role") == "user"
                        ),
                        "text": "\n".join(
                            m.get("content", "") for m in messages if m.get("role") == "assistant"
                        ),
                    },
                    workspace=ws,
                    project=proj,
                )
            except Exception:
                pass

        threading.Thread(target=_do, daemon=True).start()
        return ""

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        reason: str = "new_session",
        **kwargs: Any,
    ) -> None:
        """Rebind the provider to the rotated session id.

        Hermes rotates the session_id on context compression, /resume,
        /branch and /new. Without this hook the provider keeps writing
        observations to the stale session id after a rotation. A blank
        new_session_id keeps the current binding (safety default).
        """
        if new_session_id:
            self.session_id = new_session_id

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

    def shutdown(self) -> None:
        pass

    def _search(self, args: dict[str, Any]) -> dict[str, Any]:
        query = args.get("query", "")
        max_results = args.get("max_results", 5)
        if not isinstance(max_results, int) or isinstance(max_results, bool):
            max_results = 5
        results = self._client.search(
            query=query,
            limit=max_results,
            workspace=self._config.workspace,
            project=self._config.project,
        )
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
