from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from aimemory_config import AiMemoryConfig
from client import AiMemoryClient
from provider import AiMemoryProvider


def _provider(
    config: AiMemoryConfig, handler: Callable[[httpx.Request], httpx.Response]
) -> AiMemoryProvider:
    client = AiMemoryClient(config)
    client._transport = httpx.MockTransport(handler)
    return AiMemoryProvider(client=client, config=config)


# --- a query the tool can actually answer --------------------------------


def test_search_refuses_an_empty_query_without_touching_the_store(
    provider: AiMemoryProvider,
) -> None:
    """29 of 317 audited calls went out with `{}` and could only fail."""
    provider._client.search = lambda **kwargs: pytest.fail("search called without a query")

    result = provider._search({})

    assert result["ok"] is False
    assert result["results"] == []
    assert result["retry"] is False
    assert "query" in result["error"]


def test_search_tool_schema_requires_a_non_empty_query(provider: AiMemoryProvider) -> None:
    schema = {item["name"]: item for item in provider.get_tool_schemas()}
    query = schema["ai_memory_search"]["parameters"]["properties"]["query"]
    assert query.get("minLength") == 1
    assert "ai_memory_search" in schema


# --- an unavailable store is a state, not a reason to retry --------------


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("[Errno 111] connection refused"),
        httpx.ReadTimeout("timed out"),
        httpx.ConnectTimeout("connect timed out"),
    ],
)
def test_search_reports_unavailability_once(config: AiMemoryConfig, exc: Exception) -> None:
    """37 audited calls were connection-refused and the agent retried them
    instead of treating the store as absent for the turn."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    result = _provider(config, handler)._search({"query": "fleet"})

    assert result["ok"] is False
    assert result["unavailable"] is True
    assert result["retry"] is False
    assert result["results"] == []
    assert "do not retry" in result["error"]


def test_search_reports_a_rejected_request_as_final(config: AiMemoryConfig) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "bad query"})

    result = _provider(config, handler)._search({"query": "fleet"})

    assert result["ok"] is False
    assert result["retry"] is False
    assert "422" in result["error"]


def test_write_reports_unavailability_once(config: AiMemoryConfig) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 111] connection refused")

    provider = _provider(config, handler)
    provider._writes_enabled = True
    provider._scope_known = True

    result = provider._write({"path": "notes/x.md", "body": "# Rule"})

    assert result["ok"] is False
    assert result["unavailable"] is True
    assert result["retry"] is False


def test_status_reports_unavailability_once(config: AiMemoryConfig) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 111] connection refused")

    result = _provider(config, handler)._status()

    assert result["ok"] is False
    assert result["unavailable"] is True
    assert result["retry"] is False


def test_writes_stay_disabled_without_a_scope(provider: AiMemoryProvider) -> None:
    result = provider._write({"path": "notes/x.md", "body": "# Rule"})
    assert result["ok"] is False
    assert result["retry"] is False
