"""Regression tests for the ai-memory 1.28.1 compatibility fixes.

Each test here pins one behaviour that was broken against a real
ai-memory 1.28.1 server: a wrong endpoint, a payload shape the server
accepted and then dropped, or scoping derived from kwargs Hermes never
sends.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest
from client import AiMemoryClient
from config import AiMemoryConfig
from provider import AiMemoryProvider


@pytest.fixture
def cfg() -> AiMemoryConfig:
    return AiMemoryConfig(
        server_url="http://localhost:49374",
        workspace="hermes",
        project="hermes-test",
    )


@pytest.fixture
def offline_provider(cfg: AiMemoryConfig, monkeypatch: pytest.MonkeyPatch) -> AiMemoryProvider:
    """Provider whose client never touches the network.

    ``initialize()`` rebuilds ``self._client`` unconditionally, so replacing
    the attribute is not enough — the constructor itself has to yield the
    mock, otherwise a real client (and a real localhost:49374) sneaks back in
    halfway through the test.
    """
    import provider as provider_module

    fake = MagicMock()
    fake.fetch_handoff.return_value = None
    monkeypatch.setattr(provider_module, "AiMemoryClient", lambda *a, **k: fake)

    p = AiMemoryProvider(config=cfg)
    p._client = fake
    return p


def _wait_for_hook(provider: AiMemoryProvider, timeout: float = 2.0) -> None:
    """sync_turn/on_session_end fire on a daemon thread; wait for the call."""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        if provider._client.send_hook.called:
            return
        time.sleep(0.01)
    raise AssertionError("send_hook was never called")


def _capture(client: AiMemoryClient, response: httpx.Response) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response

    client._transport = httpx.MockTransport(handler)
    return seen


# --------------------------------------------------------------- 1. endpoint
def test_search_uses_admin_search_not_api_v1(cfg: AiMemoryConfig) -> None:
    """POST /api/v1/search 404s on 1.28.1; the route is GET /admin/search."""
    client = AiMemoryClient(cfg)
    seen = _capture(client, httpx.Response(200, json=[]))
    client.search("anything")
    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/admin/search"
    assert "/api/v1/search" not in str(seen[0].url)


# ----------------------------------------------------------- 2. global search
def test_search_without_scope_sends_no_workspace_or_project(cfg: AiMemoryConfig) -> None:
    client = AiMemoryClient(cfg)
    seen = _capture(client, httpx.Response(200, json=[]))
    client.search("q")
    params = dict(seen[0].url.params)
    assert params == {"q": "q", "limit": "3"}


def test_provider_search_is_global(offline_provider: AiMemoryProvider) -> None:
    """Recall must span every project so Claude/Codex memories are visible."""
    offline_provider._client.search.return_value = []
    offline_provider._search({"query": "shared fact", "max_results": 4})
    offline_provider._client.search.assert_called_once_with(query="shared fact", limit=4)
    kwargs = offline_provider._client.search.call_args.kwargs
    assert "workspace" not in kwargs
    assert "project" not in kwargs


def test_provider_write_stays_scoped(offline_provider: AiMemoryProvider) -> None:
    """Only reads go global — writes keep the Hermes scope."""
    offline_provider._client.write_page.return_value = {"ok": True}
    offline_provider._write({"path": "p.md", "body": "b"})
    kwargs = offline_provider._client.write_page.call_args.kwargs
    assert kwargs["workspace"] == "hermes"
    assert kwargs["project"] == "hermes-test"


# ----------------------------------------------------------- 3. scoped search
def test_search_with_both_scope_keys_sends_both(cfg: AiMemoryConfig) -> None:
    client = AiMemoryClient(cfg)
    seen = _capture(client, httpx.Response(200, json=[]))
    client.search("q", workspace="ws", project="proj")
    params = dict(seen[0].url.params)
    assert params["workspace"] == "ws"
    assert params["project"] == "proj"


@pytest.mark.parametrize(
    "workspace,project",
    [("ws", None), (None, "proj")],
)
def test_search_partial_scope_is_dropped(
    cfg: AiMemoryConfig, workspace: str | None, project: str | None
) -> None:
    """Half a scope is worse than none — ai-memory resolves project in a workspace."""
    client = AiMemoryClient(cfg)
    seen = _capture(client, httpx.Response(200, json=[]))
    client.search("q", workspace=workspace, project=project)
    params = dict(seen[0].url.params)
    assert "workspace" not in params
    assert "project" not in params


def test_search_empty_results(cfg: AiMemoryConfig) -> None:
    client = AiMemoryClient(cfg)
    _capture(client, httpx.Response(200, json=[]))
    assert client.search("nothing matches this") == []


def test_search_honours_limit(cfg: AiMemoryConfig) -> None:
    client = AiMemoryClient(cfg)
    seen = _capture(
        client, httpx.Response(200, json=[{"path": f"{i}.md"} for i in range(10)])
    )
    results = client.search("q", limit=2)
    assert dict(seen[0].url.params)["limit"] == "2"
    assert len(results) == 2


# ------------------------------------------------------- 4/5. ingest payload
def test_sync_turn_sends_user_prompt_submit(offline_provider: AiMemoryProvider) -> None:
    """`user-prompt` + {user, assistant} was accepted (202) then stored empty."""
    offline_provider.initialize("sess-A", hermes_home="")
    offline_provider.sync_turn("il fatto da ricordare", "risposta")
    _wait_for_hook(offline_provider)
    kwargs = offline_provider._client.send_hook.call_args.kwargs
    assert kwargs["event"] == "user-prompt-submit"
    assert kwargs["payload"]["prompt"] == "il fatto da ricordare"
    assert kwargs["payload"]["session_id"] == "sess-A"
    assert kwargs["session_id"] == "sess-A"


# ------------------------------------------------------------- 6/7. handoff
def test_fetch_handoff_reads_markdown_text(cfg: AiMemoryConfig) -> None:
    client = AiMemoryClient(cfg)
    body = "> **ai-memory: pending handoff**\n> next: finish the migration"
    _capture(client, httpx.Response(200, text=body))
    assert client.fetch_handoff() == body


def test_fetch_handoff_404_returns_none(cfg: AiMemoryConfig) -> None:
    client = AiMemoryClient(cfg)
    _capture(client, httpx.Response(404, text="not found"))
    assert client.fetch_handoff() is None


def test_fetch_handoff_blank_body_returns_none(cfg: AiMemoryConfig) -> None:
    client = AiMemoryClient(cfg)
    _capture(client, httpx.Response(200, text="   \n  "))
    assert client.fetch_handoff() is None


def test_initialize_fetches_handoff_once(offline_provider: AiMemoryProvider) -> None:
    offline_provider._client.fetch_handoff.return_value = "carry this over"
    offline_provider.initialize("sess-H", hermes_home="")
    assert offline_provider._client.fetch_handoff.call_count == 1
    assert "carry this over" in offline_provider.system_prompt_block()
    assert "Previous session handoff:" in offline_provider.system_prompt_block()


def test_system_prompt_block_without_handoff(offline_provider: AiMemoryProvider) -> None:
    offline_provider.initialize("sess-H", hermes_home="")
    assert offline_provider.system_prompt_block() == (
        "Long-term memory is backed by ai-memory wiki."
    )


def test_handoff_failure_is_not_fatal(offline_provider: AiMemoryProvider) -> None:
    offline_provider._client.fetch_handoff.side_effect = RuntimeError("server down")
    offline_provider.initialize("sess-H", hermes_home="")
    assert offline_provider.session_id == "sess-H"
    assert offline_provider._handoff_context is None


# --------------------------------------------------------- 8. session switch
def test_on_session_switch_updates_session_id(offline_provider: AiMemoryProvider) -> None:
    offline_provider.initialize("sess-old", hermes_home="")
    offline_provider.on_session_switch("sess-new")
    assert offline_provider.session_id == "sess-new"


def test_on_session_switch_reset_clears_handoff(offline_provider: AiMemoryProvider) -> None:
    offline_provider._client.fetch_handoff.return_value = "old context"
    offline_provider.initialize("sess-old", hermes_home="")
    offline_provider.on_session_switch("sess-new", reset=True)
    assert offline_provider._handoff_context is None
    assert offline_provider.system_prompt_block() == (
        "Long-term memory is backed by ai-memory wiki."
    )


def test_on_session_switch_without_reset_keeps_handoff(
    offline_provider: AiMemoryProvider,
) -> None:
    offline_provider._client.fetch_handoff.return_value = "old context"
    offline_provider.initialize("sess-old", hermes_home="")
    offline_provider.on_session_switch("sess-new")
    assert offline_provider._handoff_context == "old context"


def test_observations_after_switch_use_new_session_id(
    offline_provider: AiMemoryProvider,
) -> None:
    offline_provider.initialize("sess-old", hermes_home="")
    offline_provider.on_session_switch("sess-new")
    offline_provider.sync_turn("dopo lo switch", "ok")
    _wait_for_hook(offline_provider)
    kwargs = offline_provider._client.send_hook.call_args.kwargs
    assert kwargs["session_id"] == "sess-new"
    assert kwargs["payload"]["session_id"] == "sess-new"


# ------------------------------------------------------------- 9/10. scoping
def test_configured_project_is_not_overwritten(offline_provider: AiMemoryProvider) -> None:
    """Hermes sends no `project`; the old code overwrote ai-memory.json anyway."""
    offline_provider._config.project = "AI MULTI MODELLO"
    offline_provider.initialize(
        "sess-1", hermes_home="", agent_identity="default", agent_workspace="hermes"
    )
    assert offline_provider._config.project == "AI MULTI MODELLO"


def test_explicit_project_kwarg_wins(offline_provider: AiMemoryProvider) -> None:
    offline_provider.initialize("sess-1", hermes_home="", project="explicit-proj")
    assert offline_provider._config.project == "explicit-proj"


def test_agent_identity_fallback_when_project_empty(
    offline_provider: AiMemoryProvider,
) -> None:
    offline_provider._config.project = ""
    offline_provider.initialize("sess-1", hermes_home="", agent_identity="alice")
    assert offline_provider._config.project == "hermes-alice"


def test_project_falls_back_to_hermes_default(offline_provider: AiMemoryProvider) -> None:
    offline_provider._config.project = ""
    offline_provider.initialize("sess-1", hermes_home="")
    assert offline_provider._config.project == "hermes-default"


def test_agent_workspace_sets_workspace_name(offline_provider: AiMemoryProvider) -> None:
    """agent_workspace is a workspace NAME, never a filesystem path."""
    offline_provider.initialize("sess-1", hermes_home="", agent_workspace="team-ws")
    assert offline_provider._config.workspace == "team-ws"


def test_explicit_workspace_override_wins(offline_provider: AiMemoryProvider) -> None:
    offline_provider.initialize(
        "sess-1",
        hermes_home="",
        agent_workspace="team-ws",
        ai_memory_workspace="override-ws",
    )
    assert offline_provider._config.workspace == "override-ws"


# ------------------------------------------------------- 11. error tolerance
def test_send_hook_error_does_not_raise(
    cfg: AiMemoryConfig, caplog: pytest.LogCaptureFixture
) -> None:
    """A failing hook is logged, never propagated into the conversation."""
    client = AiMemoryClient(cfg)
    _capture(client, httpx.Response(500, text="boom"))
    client.send_hook(event="user-prompt-submit", session_id="s1", payload={"prompt": "x"})
    assert any("hook failed" in r.message for r in caplog.records)


def test_sync_turn_survives_client_failure(offline_provider: AiMemoryProvider) -> None:
    offline_provider._client.send_hook.side_effect = RuntimeError("network gone")
    offline_provider.initialize("sess-1", hermes_home="")
    offline_provider.sync_turn("testo", "risposta")  # must not raise


def test_on_session_end_survives_client_failure(offline_provider: AiMemoryProvider) -> None:
    offline_provider._client.send_hook.side_effect = RuntimeError("network gone")
    offline_provider.initialize("sess-1", hermes_home="")
    offline_provider.on_session_end([{"role": "user", "content": "hi"}])  # must not raise
