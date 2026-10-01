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

from client import SESSION_END_TIMEOUT, AiMemoryClient  # noqa: E402
from config import (  # noqa: E402
    AiMemoryConfig,
    explicit_scope_keys,
    get_config_schema,
    load_config,
    marker_scope,
    save_config,
)

try:
    from agent.memory_provider import MemoryProvider, RecallStatus  # type: ignore[import-untyped]
except ImportError:
    from abc import ABC as _ABC
    from dataclasses import dataclass as _dataclass

    class MemoryProvider(_ABC):  # type: ignore[no-redef]
        pass

    @_dataclass(frozen=True)
    class RecallStatus:  # type: ignore[no-redef]
        """Standalone fallback mirroring Hermes' ``RecallStatus``."""

        provider_label: str
        count: int
        glyph: str = "🧠"


log = logging.getLogger(__name__)

# Recall hits injected per prefetch, and how many sessions' results the
# background cache holds before the oldest is evicted.
_PREFETCH_RESULTS = 3
_PREFETCH_CACHE_MAX = 8


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
        self._cwd: str = ""
        # Non-primary contexts (subagent / cron / flush) get no writes and must
        # not consume the single-use handoff. The default keeps standalone
        # callers writing, since only Hermes reports a context.
        self._agent_context: str = "primary"
        self._writes_enabled: bool = True
        # Background recall from queue_prefetch, consumed once by prefetch:
        # {(session_id, query): [hit, ...]}, oldest entry evicted first.
        self._prefetch_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._last_recall_count: int = 0
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

    def unavailable_reason(self) -> str:
        """Why the provider cannot serve: no URL, or a recorded transport failure."""
        if not self._config.server_url:
            return "AI_MEMORY_SERVER_URL is not configured"
        failure = self._client.last_failure()
        return f"ai-memory server unreachable ({failure})" if failure else ""

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self.session_id = session_id
        hermes_home = kwargs.get("hermes_home", "")
        self._hermes_home = hermes_home
        # Hermes reports the context that owns this provider instance. A
        # subagent or cron run must not write, and must not consume the
        # single-use handoff meant for the session the user is watching.
        self._agent_context = str(kwargs.get("agent_context") or "primary")
        self._writes_enabled = self._agent_context in ("", "primary")
        self._cwd = str(kwargs.get("cwd") or "")

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

            self._resolve_scope(kwargs, hermes_home)

            self._client = AiMemoryClient(self._config)

        # One handoff fetch per session, never polled. Loopback server plus
        # a 2s timeout, so a synchronous call cannot hold up startup, and
        # any failure leaves the provider working without a handoff.
        self._handoff_context = None
        if not self._writes_enabled:
            return
        try:
            self._handoff_context = self._client.fetch_handoff(
                agent="hermes",
                workspace=self._config.workspace,
                project=self._config.project,
            )
        except Exception:
            log.warning("ai-memory handoff fetch failed", exc_info=True)

    def _resolve_scope(self, kwargs: dict[str, Any], hermes_home: str) -> None:
        """Resolve this session's ``workspace``/``project``.

        Precedence: an explicit plugin override (the ``ai_memory_workspace`` /
        ``project`` kwargs, or a workspace/project set in ``ai-memory.json`` or
        an ``AI_MEMORY_*`` env var), then the checkout's ``.ai-memory.toml``
        marker, then what Hermes reports.

        The marker deliberately outranks Hermes: ``agent_workspace`` is the
        literal ``"hermes"``, so letting it win pinned every session to a
        project no other agent in the same repository writes to — which is what
        broke cross-agent handoff. Hermes 0.20.5 also passes neither `project`
        nor `profile`, only `agent_identity`, so the identity fallback below is
        the last resort instead of the value of every session.
        """
        explicit = explicit_scope_keys(hermes_home)
        marker = marker_scope(self._cwd)

        # `agent_workspace` is a workspace NAME, not a filesystem path.
        workspace = kwargs.get("ai_memory_workspace", "")
        if not workspace and "workspace" in explicit:
            workspace = self._config.workspace
        if not workspace:
            workspace = marker.get("workspace", "")
        if not workspace:
            workspace = kwargs.get("agent_workspace", "")
        if workspace:
            self._config.workspace = workspace

        project = kwargs.get("project", "")
        if not project and "project" in explicit:
            project = self._config.project
        if not project:
            project = marker.get("project", "")
        if project:
            self._config.project = project
        elif not self._config.project:
            identity = kwargs.get("agent_identity", "") or "default"
            self._config.project = f"hermes-{identity}"

    def get_config_schema(self) -> list[dict[str, Any]]:
        return get_config_schema()

    def save_config(self, values: dict[str, Any], hermes_home: str) -> list[str]:
        return save_config(values, hermes_home)

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "ai_memory_search",
                "description": "Search the ai-memory wiki for relevant context",
                "parameters": {
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
                "parameters": {
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
                "parameters": {"type": "object", "properties": {}},
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
        return self._BASE_PROMPT + "\n\nPrevious session handoff:\n" + self._handoff_context

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        sid = session_id or self.session_id
        with self._lock:
            hits = self._prefetch_cache.pop((sid, query), None)
        if hits is None:
            # No background recall queued for this exact turn (first turn of a
            # session, or queue_prefetch had nothing to work with).
            results = self._search({"query": query, "max_results": _PREFETCH_RESULTS})
            hits = results.get("results", []) if isinstance(results, dict) else []
        self._last_recall_count = len(hits)
        return self._format_recall(hits)

    @staticmethod
    def _format_recall(hits: list[dict[str, Any]]) -> str:
        """One block per hit, labelled with its page path when the server sends one.

        Recall is deliberately global (every project), so the injected context
        has to say where each fact came from.
        """
        lines: list[str] = []
        for hit in hits:
            snippet = str(hit.get("snippet", ""))
            title = str(hit.get("title", ""))
            path = str(hit.get("path", ""))
            label = f"{title} ({path})" if title and path else (path or title)
            lines.append(f"{label}: {snippet}" if label else snippet)
        return "\n\n".join(lines)

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Recall in the background so the next turn's prefetch is a cache hit.

        Hermes calls this after each turn with that turn's ``session_id``;
        results are cached per ``(session_id, query)`` and consumed once.
        """
        sid = session_id or self.session_id
        if not query:
            return

        def _do() -> None:
            try:
                hits = self._client.search(query=query, limit=_PREFETCH_RESULTS)
            except Exception:
                log.warning("ai-memory queue_prefetch failed", exc_info=True)
                return
            with self._lock:
                self._prefetch_cache[(sid, query)] = hits
                while len(self._prefetch_cache) > _PREFETCH_CACHE_MAX:
                    self._prefetch_cache.pop(next(iter(self._prefetch_cache)))

        threading.Thread(target=_do, daemon=True).start()

    def recall_status(self) -> RecallStatus | None:
        """Hit count from the most recent ``prefetch``, for Hermes's indicator."""
        if self._last_recall_count <= 0:
            return None
        return RecallStatus(provider_label=self.name, count=self._last_recall_count)

    def sync_turn(self, user: str, assistant: str, *, session_id: str = "", **kwargs: Any) -> None:
        if not self._writes_enabled:
            return
        sid = session_id or self.session_id
        ws = self._config.workspace
        proj = self._config.project
        cwd = self._cwd

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
                    cwd=cwd,
                )
            except Exception:
                log.warning("ai-memory sync_turn failed", exc_info=True)

        threading.Thread(target=_do, daemon=True).start()

    def on_session_end(self, messages: list[dict[str, Any]], **kwargs: Any) -> None:
        if not self._writes_enabled:
            return
        sid = self.session_id
        ws = self._config.workspace
        proj = self._config.project
        cwd = self._cwd

        def _do() -> None:
            try:
                # ai-memory reads no body field on SessionEnd: the event closes
                # the session, synthesizes sessions/<id>.md and writes the auto
                # handoff. Posting the transcript only risked the 0.5s turn-hook
                # timeout dropping the close, so the payload stays minimal and
                # the call gets the write budget instead.
                self._client.send_hook(
                    event="session-end",
                    session_id=sid,
                    payload={"session_id": sid},
                    workspace=ws,
                    project=proj,
                    cwd=cwd,
                    timeout=SESSION_END_TIMEOUT,
                )
            except Exception:
                log.warning("ai-memory on_session_end failed", exc_info=True)

        threading.Thread(target=_do, daemon=True).start()

    # Hermes announces built-in memory writes as add|replace|remove; write and
    # append are kept as synonyms for older callers of this hook.
    _MIRRORED_ACTIONS = ("add", "replace", "write", "append")
    _REPLACED_HEADING = "## Substituído"

    def on_memory_write(
        self, action: str, target: str, content: str, metadata: dict[str, Any] | None = None
    ) -> None:
        """Mirror a built-in `MEMORY.md` / `USER.md` write into the wiki.

        ``remove`` is the native store deleting an entry it owns: the mirror
        keeps the page history instead of writing a tombstone, so it is
        skipped. A ``replace`` carries the superseded entry in
        ``metadata["previous_content"]``, which is kept in the mirrored page so
        the replacement does not silently drop it.
        """
        if not self._writes_enabled:
            return
        if action not in self._MIRRORED_ACTIONS:
            return

        meta = metadata or {}
        previous = str(meta.get("previous_content") or meta.get("old_text") or "")
        body = content
        if action == "replace" and previous:
            body = f"{content}\n\n{self._REPLACED_HEADING}\n\n{previous}"

        try:
            self._client.write_page(
                path=f"hermes-memory/{target}.md",
                body=body,
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
