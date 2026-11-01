"""Same-named pages across projects must be distinguishable in a recall block.

Audit REPORT.md defect 2: one global ``/admin/search`` returns ``index.md`` four
times and ``hermes-notifier`` twice, and the provider labelled every hit
``title (path)`` -- so four different pages rendered as four identical lines.

The ai-memory 3.3.0 server already answers the first half of the bead's option
(a): every hit carries ``workspace`` and ``project`` (verified against the live
``/admin/search``). What is left is local -- the label dropped those fields.

Collapsing the rows client-side was measured and rejected: across 24 queries
and 781 live hits *no* two rows shared a path leaf and a snippet, so the four
``index.md`` pages are four different project indexes and dropping three would
delete facts to save slots.
"""

from __future__ import annotations

import httpx
from client import AiMemoryClient, hit_scope
from aimemory_config import AiMemoryConfig
from provider import AiMemoryProvider

INDEX_CLONE_SNIPPET = "\n# Bundle <mark>index</mark>\n\nConcept files live in `concepts/`.\n"


def _hit(path: str, snippet: str, workspace: str, project: str) -> dict[str, object]:
    return {
        "path": path,
        "title": path.rsplit("/", 1)[-1].removesuffix(".md"),
        "snippet": snippet,
        "workspace": workspace,
        "project": project,
    }


def test_hit_scope_reads_the_fields_the_live_server_sends() -> None:
    live_hit = {
        "id": "019fa053-ebb4-7703-a65b-70d9b1782ed0",
        "path": "hermes-notifier",
        "title": "hermes-notifier",
        "snippet": "Desktop notifications + sounds for Hermes Agent when idle.",
        "rank": -9.13,
        "workspace": "default",
        "project": "hermes-notifier",
    }

    assert hit_scope(live_hit) == "default/hermes-notifier"


def test_hit_scope_degrades_to_whichever_field_exists() -> None:
    assert hit_scope({"project": "hermes-default"}) == "hermes-default"
    assert hit_scope({"workspace": "hermes"}) == "hermes"
    assert hit_scope({}) == ""
    assert hit_scope({"workspace": None, "project": ""}) == ""


def test_prefetch_block_distinguishes_same_path_in_two_projects() -> None:
    hits = [
        _hit("hermes-notifier", "Desktop notifications + sounds.", "default", "hermes-notifier"),
        _hit("hermes-notifier", "Plugin for desktop notifications.", "hermes", "hermes-default"),
    ]

    block = AiMemoryProvider._format_recall(hits)

    lines = block.split("\n\n")
    assert len(lines) == 2
    assert lines[0] != lines[1]
    assert lines[0].startswith("hermes-notifier (hermes-notifier) [default/hermes-notifier]:")
    assert lines[1].startswith("hermes-notifier (hermes-notifier) [hermes/hermes-default]:")


def test_four_project_index_pages_render_as_four_labelled_blocks() -> None:
    """The live defect, as the server actually returns it: same name, own body."""
    scopes = [
        ("default", "hermes-notifier", "Concept files live in these directories:"),
        ("default", "scratch", "Concept files live in these directories:\n- [sessions/](sessions/)"),
        ("hermes", "quadros", "- [projects/](projects/)\n- [sessions/](sessions/)"),
    ]
    hits = [_hit("index.md", body, workspace, project) for workspace, project, body in scopes]

    lines = AiMemoryProvider._format_recall(hits).split("\n\n")

    assert len(lines) == 3
    assert len({line.split(":")[0] for line in lines}) == 3
    assert "[default/hermes-notifier]" in lines[0]
    assert "[default/scratch]" in lines[1]
    assert "[hermes/quadros]" in lines[2]


def test_prefetch_block_without_scope_fields_keeps_the_old_label() -> None:
    """A server that sends no workspace/project must not grow an empty bracket."""
    assert (
        AiMemoryProvider._format_recall([{"title": "Rules", "path": "rules/x.md", "snippet": "body"}])
        == "Rules (rules/x.md): body"
    )


def test_prefetch_block_without_title_still_carries_the_scope() -> None:
    hit = {"path": "index.md", "snippet": "body", "workspace": "hermes", "project": "quadros"}

    assert AiMemoryProvider._format_recall([hit]) == "index.md [hermes/quadros]: body"


def test_client_search_passes_the_scope_fields_through_untouched() -> None:
    payload = [
        _hit("index.md", INDEX_CLONE_SNIPPET, "default", "hermes-notifier"),
        _hit("index.md", "other body", "hermes", "quadros"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = AiMemoryClient(AiMemoryConfig(server_url="http://localhost:49374"))
    client._transport = httpx.MockTransport(handler)

    assert client.search("index", limit=3) == payload
