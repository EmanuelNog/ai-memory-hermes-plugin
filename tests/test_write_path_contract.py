from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from aimemory_config import AiMemoryConfig
from client import AiMemoryClient, normalise_page_path
from provider import AiMemoryProvider

SCOPE = {"workspace": "hermes", "project": "hermes-test"}


def _client(
    config: AiMemoryConfig, handler: Callable[[httpx.Request], httpx.Response]
) -> AiMemoryClient:
    client = AiMemoryClient(config)
    client._transport = httpx.MockTransport(handler)
    return client


def _page(body: str, pinned: bool = False) -> dict[str, Any]:
    return {
        "path": "notes/x.md",
        "workspace": "hermes",
        "project": "hermes-test",
        "title": None,
        "body": body,
        "frontmatter": {"pinned": pinned},
    }


# --- one page per fact: the path form is canonical -------------------------


def test_bare_and_md_path_forms_are_the_same_page(config: AiMemoryConfig) -> None:
    """ai-memory keys a page by its literal path, so `x` and `x.md` coexisted.

    Both stayed is_latest=1 with different bodies and neither superseded the
    other. The client normalises the path so a rewrite supersedes the page.
    """
    posted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/write-page":
            body = json.loads(request.content)
            posted.append(body["path"])
            return httpx.Response(200, json={"page_id": "p1", "path": body["path"]})
        return httpx.Response(404)

    client = _client(config, handler)
    client.write_page("_rules/no-ubuntu-fleet.md", "# Rule\n\nv1")
    client.write_page("_rules/no-ubuntu-fleet", "# Rule\n\nv2")

    assert posted == ["_rules/no-ubuntu-fleet.md", "_rules/no-ubuntu-fleet.md"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("notes/a.md", "notes/a.md"),
        ("notes/a", "notes/a.md"),
        ("  notes/a.md  ", "notes/a.md"),
        ("./notes/a.md", "notes/a.md"),
        ("/notes/a.md", "notes/a.md"),
        ("notes//a", "notes/a.md"),
        ("notes\\a", "notes/a.md"),
        ("notes/a.MD", "notes/a.MD"),
    ],
)
def test_normalise_page_path(raw: str, expected: str) -> None:
    assert normalise_page_path(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", ".", "..", "notes/../escape", "/"])
def test_normalise_page_path_rejects_unusable_values(raw: str) -> None:
    with pytest.raises(ValueError):
        normalise_page_path(raw)


def test_write_page_rejects_a_body_the_server_would_422(config: AiMemoryConfig) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        calls.append(request.url.path)
        return httpx.Response(500)

    client = _client(config, handler)
    with pytest.raises(ValueError):
        client.write_page("notes/x.md", "   ")
    assert calls == []


# --- a write the caller can trust: read-back, retry, curation flags --------


def test_write_page_verifies_the_page_it_wrote(config: AiMemoryConfig) -> None:
    reads: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/write-page":
            return httpx.Response(200, json={"page_id": "p1", "path": "notes/x.md"})
        if request.url.path == "/admin/read-page":
            reads.append(dict(request.url.params))
            return httpx.Response(200, json=_page("# Rule\n\nv1"))
        return httpx.Response(404)

    client = _client(config, handler)
    result = client.write_page("notes/x", "# Rule\n\nv1", **SCOPE)

    assert result["ok"] is True
    assert result["path"] == "notes/x.md"
    assert result["verified"] is True
    assert reads == [{"path": "notes/x.md", **SCOPE}]


def test_write_page_reports_a_readback_mismatch(config: AiMemoryConfig) -> None:
    """A mismatch is surfaced, never reported as a clean write."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/write-page":
            return httpx.Response(200, json={"page_id": "p1"})
        if request.url.path == "/admin/read-page":
            return httpx.Response(200, json=_page("something else entirely"))
        return httpx.Response(404)

    result = _client(config, handler).write_page("notes/x", "# Rule\n\nv1", **SCOPE)
    assert result["ok"] is True
    assert result["verified"] is False


def test_write_page_recovers_when_the_timeout_landed(config: AiMemoryConfig) -> None:
    """occ#298: the write timed out AFTER landing, so the caller retried a
    mutation that had already applied. Read back first and say so."""
    posts: list[dict[str, Any]] = []
    reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal reads
        if request.url.path == "/admin/write-page":
            posts.append(json.loads(request.content))
            raise httpx.ReadTimeout("write timed out")
        if request.url.path == "/admin/read-page":
            reads += 1
            if reads == 1:
                return httpx.Response(404)  # the page does not exist yet
            return httpx.Response(200, json=_page("# Rule\n\nv1"))
        return httpx.Response(404)

    result = _client(config, handler).write_page("notes/x", "# Rule\n\nv1", **SCOPE)

    assert result["ok"] is True
    assert result["recovered_after_timeout"] is True
    assert result["verified"] is True
    assert len(posts) == 1


def test_write_page_retries_the_same_full_payload(
    config: AiMemoryConfig,
) -> None:
    """The retry must carry everything the first attempt had — path, body,
    tags and curation flags — never a partial payload."""
    posts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/write-page":
            posts.append(json.loads(request.content))
            if len(posts) == 1:
                raise httpx.ReadTimeout("write timed out")
            return httpx.Response(200, json={"page_id": "p2", "path": "notes/x.md"})
        if request.url.path == "/admin/read-page":
            return httpx.Response(404)
        return httpx.Response(404)

    result = _client(config, handler).write_page(
        "notes/x", "# Rule\n\nv1", tags=["rule"], pinned=True, **SCOPE
    )

    assert result["ok"] is True
    assert len(posts) == 2
    assert posts[1] == posts[0]
    assert posts[1]["pinned"] is True
    assert posts[1]["tags"] == ["rule"]
    assert posts[1]["body"] == "# Rule\n\nv1"


def test_write_page_does_not_retry_when_the_server_is_down(config: AiMemoryConfig) -> None:
    posts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/write-page":
            posts.append(json.loads(request.content))
            raise httpx.ConnectError("[Errno 111] connection refused")
        return httpx.Response(404)

    with pytest.raises(httpx.ConnectError):
        _client(config, handler).write_page("notes/x", "# Rule", **SCOPE)
    assert len(posts) == 1


def test_write_page_keeps_an_existing_pin_when_the_caller_omits_it(config: AiMemoryConfig) -> None:
    """A curation flag belongs to the page, not to the call: a write that does
    not mention `pinned` must not unpin it."""
    posts: list[dict[str, Any]] = []
    reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal reads
        if request.url.path == "/admin/write-page":
            posts.append(json.loads(request.content))
            return httpx.Response(200, json={"page_id": "p3"})
        if request.url.path == "/admin/read-page":
            reads += 1
            if reads == 1:
                return httpx.Response(200, json=_page("old body", pinned=True))
            return httpx.Response(200, json=_page("# Rule\n\nnew", pinned=True))
        return httpx.Response(404)

    _client(config, handler).write_page("notes/x", "# Rule\n\nnew", **SCOPE)

    assert posts[0]["pinned"] is True


def test_write_page_sends_the_body_frontmatter_pin(config: AiMemoryConfig) -> None:
    """The server only reads `pinned` from the payload, so a body that declares
    it must not silently lose the flag (occ#298)."""
    posts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/write-page":
            posts.append(json.loads(request.content))
            return httpx.Response(200, json={"page_id": "p4"})
        return httpx.Response(404)

    _client(config, handler).write_page(
        "notes/x", "---\npinned: true\n---\n\n# Rule\n", workspace="hermes", project="hermes-test"
    )
    assert posts[0]["pinned"] is True


def test_write_page_does_not_version_an_identical_rewrite(config: AiMemoryConfig) -> None:
    """The 4-5x rewrite pattern: an identical body already live is a no-op."""
    posts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/write-page":
            posts.append(json.loads(request.content))
            return httpx.Response(200, json={"page_id": "p5"})
        if request.url.path == "/admin/read-page":
            return httpx.Response(200, json=_page("# Rule\n\nv1"))
        return httpx.Response(404)

    result = _client(config, handler).write_page("notes/x", "# Rule\n\nv1", **SCOPE)

    assert posts == []
    assert result["ok"] is True
    assert result["unchanged"] is True
    assert result["path"] == "notes/x.md"


def test_write_page_names_a_near_duplicate_on_another_path(config: AiMemoryConfig) -> None:
    """Rewriting the same gotcha under a new name is the other half of the
    duplicate problem; the write says which page already covers it."""
    body = (
        "Windows Terminal steals the tmux alt keybindings. tmux option M-a prefix is "
        "swallowed by Windows Terminal menu mnemonics, so bind the prefix to C-Space "
        "instead and keep the alacritty profile as the outer terminal."
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/write-page":
            return httpx.Response(200, json={"page_id": "p6"})
        if request.url.path == "/admin/read-page":
            wanted = request.url.params["path"]
            if wanted == "gotchas/terminal-steals-alt-keys.md":
                return httpx.Response(404)  # the page being written is new
            page = _page(body)
            page["path"] = wanted
            return httpx.Response(200, json=page)
        if request.url.path == "/admin/search":
            return httpx.Response(
                200,
                json=[
                    {
                        "path": "gotchas/tmux-alt-keys-swallowed.md",
                        "title": "tmux alt keys",
                        "snippet": body,
                    }
                ],
            )
        return httpx.Response(404)

    result = _client(config, handler).write_page("gotchas/terminal-steals-alt-keys", body, **SCOPE)

    assert result["near_duplicate"] == "gotchas/tmux-alt-keys-swallowed.md"


# --- the provider's write tool --------------------------------------------


def test_provider_write_returns_the_canonical_path(provider: AiMemoryProvider) -> None:
    provider._client.write_page = lambda **kwargs: {"ok": True, "path": "notes/x.md"}
    result = provider._write({"path": "notes/x", "body": "# Rule"})
    # the provider forwards the raw path; the client canonicalises and the
    # reported path must be the page that now exists.
    assert result["ok"] is True
    assert result["written"] == "notes/x.md"


def test_provider_write_warns_about_the_page_it_replaced(provider: AiMemoryProvider) -> None:
    provider._client.write_page = lambda **kwargs: {
        "ok": True,
        "path": "notes/x.md",
        "existed": True,
        "previous_chars": 480,
        "previous_body_excerpt": "old text",
    }
    result = provider._write({"path": "notes/x", "body": "# Rule"})
    assert result["ok"] is True
    assert "already existed" in result["warning"]
    assert "old text" in result["warning"]


def test_provider_write_warns_about_a_near_duplicate(provider: AiMemoryProvider) -> None:
    provider._client.write_page = lambda **kwargs: {
        "ok": True,
        "path": "notes/x.md",
        "near_duplicate": "gotchas/older-name.md",
    }
    result = provider._write({"path": "notes/x", "body": "# Rule"})
    assert "gotchas/older-name.md" in result["warning"]
