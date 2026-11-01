from __future__ import annotations

import logging
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx

# Ensure sibling modules are findable when this file is loaded standalone
# (Hermes pre-loads submodules before executing __init__.py).
_PLUGIN_DIR = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from aimemory_config import AiMemoryConfig, marker_briefing  # noqa: E402

log = logging.getLogger("ai-memory")

SEARCH_TIMEOUT = 10.0
HOOK_TIMEOUT = 0.5
WRITE_TIMEOUT = 10.0
# Read-back budget for the post-write verification: the same loopback server,
# so a slow read is a failure signal, not a reason to block the turn.
READ_TIMEOUT = 5.0
# Handoff is fetched once, synchronously, on session start against a
# loopback server. Keep it short so a stalled server cannot delay startup.
HANDOFF_TIMEOUT = 2.0
# Session end carries no transcript (ai-memory stores only the lifecycle row),
# so it gets the write budget instead of the 0.5s turn-hook budget.
SESSION_END_TIMEOUT = 10.0
# A local server that is down fails every call immediately; after this many
# consecutive transport failures the client pauses instead of paying a timeout
# on every turn. ``last_failure()`` explains the pause.
FAILURE_THRESHOLD = 3
COOLDOWN_SECONDS = 60.0
# A write is reported back with how much of the page it replaced, so the
# caller can merge instead of rewriting blind. Bounded: a tool result.
PREVIOUS_BODY_EXCERPT = 1200
# Near-duplicate detection compares the new body against search hits for the
# path's own words. Below this many tokens a body carries no topic to match.
NEAR_DUPLICATE_MIN_TOKENS = 15
NEAR_DUPLICATE_THRESHOLD = 0.6
# Search hits whose bodies get read and compared; bounded so a write stays one
# search plus a few reads.
_NEAR_DUPLICATE_CANDIDATES = 3

_WORD_RE = re.compile(r"[a-z0-9]+")
_PATH_SPLIT_RE = re.compile(r"[-_./]+")
_PINNED_RE = re.compile(r"^\s*pinned\s*:\s*(?:true|yes|1|on)\s*$", re.IGNORECASE | re.MULTILINE)


def normalise_page_path(path: str) -> str:
    """Canonical wiki path: one page per fact, always with the ``.md`` suffix.

    ai-memory keys a page by its literal path and its uniqueness index is
    ``(workspace, project, path) WHERE is_latest``, so ``_rules/x`` and
    ``_rules/x.md`` were two live pages for one rule and a write with the other
    form never superseded anything (audit defect 1). Normalising here is the
    only end of that contract this plugin owns: the server keeps its literal
    key, and every write from the agent lands on the same row.
    """
    text = (path or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    text = re.sub(r"/{2,}", "/", text).lstrip("/")
    segments = [segment for segment in text.split("/") if segment and segment != "."]
    if any(segment == ".." for segment in segments):
        raise ValueError(f"page path must stay inside the wiki: {path!r}")
    text = "/".join(segments)
    if not text:
        raise ValueError("page path is empty")
    if not text.lower().endswith(".md"):
        text += ".md"
    return text


def _same_body(left: Any, right: Any) -> bool:
    return str(left or "").strip() == str(right or "").strip()


def _declares_pinned(body: str) -> bool:
    """Whether the body's own YAML frontmatter asks for the page to be pinned.

    The server only reads ``pinned`` from the write payload; a body-level flag
    is otherwise inert, which is how occ#298 lost its curation flag.
    """
    head = (body or "").lstrip()
    if not head.startswith("---"):
        return False
    end = head.find("\n---", 3)
    return bool(_PINNED_RE.search(head[3:end] if end >= 0 else head[3:]))


def _is_pinned(page: dict[str, Any] | None) -> bool:
    frontmatter = (page or {}).get("frontmatter")
    return bool(frontmatter.get("pinned")) if isinstance(frontmatter, dict) else False


def _tokens(text: str) -> set[str]:
    return {word for word in _WORD_RE.findall(str(text or "").lower()) if len(word) > 2}


def _topic_query(canonical: str, body: str) -> str:
    """Search terms for "does a page about this already exist?".

    The body's own H1 names the topic; the path leaf is the fallback for a body
    without a heading. Bounded so the query stays a query.
    """
    words = _path_query(canonical).split()
    heading = re.search(r"^\s{0,3}#\s+(.+)$", body or "", re.MULTILINE)
    if heading:
        words += [word for word in _WORD_RE.findall(heading.group(1).lower()) if len(word) > 2]
    unique: list[str] = []
    for word in words:
        if word not in unique:
            unique.append(word)
    return " ".join(unique[:12])


def _path_query(path: str) -> str:
    leaf = path.rsplit("/", 1)[-1]
    if leaf.lower().endswith(".md"):
        leaf = leaf[:-3]
    return " ".join(part for part in _PATH_SPLIT_RE.split(leaf) if len(part) > 2)


def hit_scope(hit: dict[str, Any]) -> str:
    """``workspace/project`` the server attributed a hit to, or ``""``.

    ai-memory 3.3.0 puts both on every ``/admin/search`` hit, and a page name
    alone cannot tell two projects apart -- four projects each hold their own
    ``index.md`` (audit defect 2). A hit without the fields predates that, and
    is simply unlabelled.
    """
    workspace = str(hit.get("workspace") or "")
    project = str(hit.get("project") or "")
    if workspace and project:
        return f"{workspace}/{project}"
    return project or workspace


class AiMemoryClient:
    def __init__(self, config: AiMemoryConfig) -> None:
        self.config = config
        self._base = config.server_url.rstrip("/")
        headers: dict[str, str] = {"Content-Type": "application/json"}
        token = config.auth_token or config.api_key
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._transport: Any = None
        self._client = httpx.Client(headers=headers)
        self._consecutive_failures = 0
        self._blocked_until = 0.0
        self._last_failure = ""

    def last_failure(self) -> str:
        """Transport failure pausing this client, or ``""`` while it is healthy."""
        return self._last_failure if self._is_blocked() else ""

    def _is_blocked(self) -> bool:
        """Whether the pause window is still open; expires into a fresh attempt."""
        if not self._blocked_until:
            return False
        if time.monotonic() >= self._blocked_until:
            self._blocked_until = 0.0
            self._consecutive_failures = 0
            self._last_failure = ""
            return False
        return True

    def _record_failure(self, exc: Exception) -> None:
        self._consecutive_failures += 1
        self._last_failure = f"{type(exc).__name__}: {exc}"
        if self._consecutive_failures >= FAILURE_THRESHOLD:
            self._blocked_until = time.monotonic() + COOLDOWN_SECONDS
            log.warning(
                "ai-memory unreachable (%d consecutive failures, %s); pausing requests for %.0fs",
                self._consecutive_failures,
                self._last_failure,
                COOLDOWN_SECONDS,
            )

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._blocked_until = 0.0
        self._last_failure = ""

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if self._is_blocked():
            raise RuntimeError(f"ai-memory requests paused: {self._last_failure}")
        url = f"{self._base}{path}"
        extra_headers = kwargs.pop("headers", {})
        headers = {**self._client.headers, **extra_headers}
        timeout = kwargs.pop("timeout", SEARCH_TIMEOUT)
        try:
            if self._transport:
                with httpx.Client(transport=self._transport, timeout=timeout) as c:
                    response = c.request(method, url, headers=headers, **kwargs)
            else:
                response = self._client.request(
                    method, url, headers=headers, timeout=timeout, **kwargs
                )
        except Exception as exc:
            self._record_failure(exc)
            raise
        self._record_success()
        return response

    def search(
        self,
        query: str,
        workspace: str | None = None,
        project: str | None = None,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """Full-text search the ai-memory wiki.

        ai-memory 1.28.1 exposes ``GET /admin/search?q=&limit=``; the
        ``POST /api/v1/search`` this used to call does not exist and 404s.

        Scope is all-or-nothing. ai-memory resolves a project *within* a
        workspace, so a lone ``project=`` has no workspace to resolve
        against and a lone ``workspace=`` silently widens the scope past
        what the caller asked for. Passing neither searches globally,
        across every project — which is what cross-agent recall needs.
        """
        params: dict[str, Any] = {"q": query, "limit": limit}
        if workspace and project:
            params["workspace"] = workspace
            params["project"] = project
        r = self._request("GET", "/admin/search", params=params, timeout=SEARCH_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            data = data.get("results", data.get("pages", []))
        if not isinstance(data, list):
            log.warning("search response has unexpected type: %s", type(data).__name__)
            return []
        return [item for item in data if isinstance(item, dict)][:limit]

    def read_page(
        self,
        path: str,
        workspace: str | None = None,
        project: str | None = None,
    ) -> dict[str, Any] | None:
        """Fetch one page, or ``None`` when it does not exist.

        ``/admin/read-page`` requires an explicit workspace, so a call without
        a scope cannot be verified and its caller must say so.
        """
        params: dict[str, Any] = {"path": path}
        if workspace:
            params["workspace"] = workspace
        if project:
            params["project"] = project
        r = self._request("GET", "/admin/read-page", params=params, timeout=READ_TIMEOUT)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, dict) else None

    def _read_quietly(
        self,
        path: str,
        workspace: str | None,
        project: str | None,
    ) -> dict[str, Any] | None:
        """Read-back that can never turn a landed write into an exception."""
        try:
            return self.read_page(path, workspace, project)
        except Exception:
            log.debug("ai-memory read-back failed for %s", path, exc_info=True)
            return None

    def write_page(
        self,
        path: str,
        body: str,
        tags: list[str] | None = None,
        tier: str | None = None,
        pinned: bool = False,
        workspace: str | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Write or update one page, and say honestly what happened.

        Four things the raw POST does not give a caller:

        * the path is normalised, so ``x`` and ``x.md`` cannot become two live
          pages for one fact (audit defect 1);
        * an identical body already live is a no-op instead of a new version
          (defect 4: the same page written five times in one session);
        * a pin already on the page or declared in the body survives a write
          that does not mention it (defect 5: occ#298's lost ``pinned``);
        * a timeout is resolved by reading the page back, so a mutation that
          landed is reported as landed and a lost one is retried with the same
          full payload — never a partial one.
        """
        canonical = normalise_page_path(path)
        body_text = body or ""
        if not body_text.strip():
            raise ValueError("page body is empty")

        scoped = bool(workspace and project)
        scoped_workspace = workspace or ""
        scoped_project = project or ""
        existing = self._read_quietly(canonical, workspace, project) if scoped else None
        if existing is not None and _same_body(existing.get("body"), body_text):
            return {
                "ok": True,
                "path": canonical,
                "page_id": existing.get("page_id"),
                "verified": True,
                "unchanged": True,
            }

        payload: dict[str, Any] = {"path": canonical, "body": body_text}
        if tags:
            payload["tags"] = list(tags)
        if tier:
            payload["tier"] = tier
        if pinned or _declares_pinned(body_text) or _is_pinned(existing):
            payload["pinned"] = True
        if workspace:
            payload["workspace"] = workspace
        if project:
            payload["project"] = project

        result = dict(self._post_write(payload, canonical, workspace, project))
        result["ok"] = True
        result["path"] = canonical
        if "verified" not in result:
            landed = self._read_quietly(canonical, workspace, project) if scoped else None
            result["verified"] = bool(landed) and _same_body(landed.get("body"), body_text)
        if existing is not None:
            previous = str(existing.get("body") or "")
            result["existed"] = True
            result["previous_chars"] = len(previous)
            result["previous_body_excerpt"] = previous[:PREVIOUS_BODY_EXCERPT]
        elif scoped:
            near = self._near_duplicate(canonical, body_text, scoped_workspace, scoped_project)
            if near:
                result["near_duplicate"] = near
        return result

    def _post_write(
        self,
        payload: dict[str, Any],
        canonical: str,
        workspace: str | None,
        project: str | None,
    ) -> dict[str, Any]:
        """POST the write, resolving a timeout by read-back before retrying."""
        try:
            r = self._request("POST", "/admin/write-page", json=payload, timeout=WRITE_TIMEOUT)
            r.raise_for_status()
        except httpx.TimeoutException:
            landed = self._read_quietly(canonical, workspace, project)
            if landed is not None and _same_body(landed.get("body"), payload["body"]):
                log.warning("ai-memory write timed out after landing: %s", canonical)
                return {
                    "page_id": landed.get("page_id"),
                    "verified": True,
                    "recovered_after_timeout": True,
                }
            log.warning("ai-memory write timed out without landing; retrying %s", canonical)
            r = self._request("POST", "/admin/write-page", json=payload, timeout=WRITE_TIMEOUT)
            r.raise_for_status()
        data = r.json()
        return data if isinstance(data, dict) else {}

    def _near_duplicate(
        self,
        canonical: str,
        body: str,
        workspace: str,
        project: str,
    ) -> str | None:
        """Path of a live page already covering this body, if there is one.

        The server has no duplicate detection on write, so a second page for
        the same gotcha lands silently. One search on the path's own words,
        compared by token overlap, names the page to update instead.
        """
        tokens = _tokens(body)
        if len(tokens) < NEAR_DUPLICATE_MIN_TOKENS:
            return None
        query = _topic_query(canonical, body)
        if not query:
            return None
        try:
            hits = self.search(query, workspace=workspace, project=project, limit=5)
        except Exception:
            log.debug("ai-memory duplicate probe failed for %s", canonical, exc_info=True)
            return None
        for hit in hits[:_NEAR_DUPLICATE_CANDIDATES]:
            other = str(hit.get("path") or "")
            if not other:
                continue
            try:
                if normalise_page_path(other) == canonical:
                    continue
            except ValueError:
                continue
            # A search snippet is a query-term excerpt, not the page: compare
            # against the page body or a real duplicate looks unrelated.
            page = self._read_quietly(other, workspace, project)
            body_of_hit = str((page or {}).get("body") or hit.get("snippet") or "")
            overlap = len(tokens & _tokens(body_of_hit)) / len(tokens)
            if overlap >= NEAR_DUPLICATE_THRESHOLD:
                return other
        return None

    def status(self) -> dict[str, Any]:
        r = self._request("GET", "/admin/status", timeout=SEARCH_TIMEOUT)
        r.raise_for_status()
        return r.json()

    def send_hook(
        self,
        event: str,
        session_id: str,
        payload: dict[str, Any] | None = None,
        workspace: str | None = None,
        project: str | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
    ) -> None:
        if self._is_blocked():
            log.debug("ai-memory hook skipped for event=%s: %s", event, self._last_failure)
            return
        params: dict[str, str] = {
            "event": event,
            "agent": "hermes",
        }
        if workspace:
            params["workspace"] = workspace
        if project:
            params["project"] = project
        # cwd travels with the event so the server can resolve the same scope a
        # shell-hook-driven agent would, and refresh the active project.
        if cwd:
            params["cwd"] = cwd
        if session_id:
            params["session_id"] = session_id
        body: dict[str, Any] = {}
        if payload:
            body = payload
        try:
            r = self._request(
                "POST", "/hook", params=params, json=body, timeout=timeout or HOOK_TIMEOUT
            )
            # ai-memory answers 202 for anything it queues, including a
            # payload whose fields it does not recognise, so a bad shape
            # used to look identical to a good one. raise_for_status at
            # least surfaces transport- and route-level failures.
            r.raise_for_status()
        except Exception:
            log.warning("ai-memory hook failed for event=%s", event, exc_info=True)

    def fetch_handoff(
        self,
        agent: str = "hermes",
        cwd: str | None = None,
        workspace: str | None = None,
        project: str | None = None,
    ) -> str | None:
        params: dict[str, str] = {"agent": agent}
        if cwd:
            params["cwd"] = cwd
            briefing = marker_briefing(cwd)
            if briefing.get("inject_on_session_start", "").lower() in ("1", "true", "yes", "on"):
                # ai-memory only appends the project brief (pinned/_rules_/
                # _slots_ pages) when the client forwards the marker's opt-in;
                # the hook clients do, this provider predates it.
                params["briefing"] = briefing["inject_on_session_start"]
                if briefing.get("max_chars"):
                    params["briefing_budget"] = briefing["max_chars"]
        if workspace:
            params["workspace"] = workspace
        if project:
            params["project"] = project
        r = self._request("GET", "/handoff", params=params, timeout=HANDOFF_TIMEOUT)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        # ai-memory 1.28.1 returns the handoff as a markdown block, not a
        # JSON envelope: r.json() raised here on every call.
        text = (r.text or "").strip()
        return text or None
