# ai-memory Hermes Memory Provider

Connects Hermes Agent to an ai-memory server for long-term wiki memory.

## Quick Start

```bash
mkdir -p "$HERMES_HOME/plugins/ai-memory"
cp -r * "$HERMES_HOME/plugins/ai-memory/"
hermes plugins enable ai-memory
hermes memory setup
hermes memory status
```

## Tools

- `ai_memory_search` — search the wiki
- `ai_memory_write` — write a new page
- `ai_memory_status` — health check

## Docs

Full documentation is at the [project root](../../../README.md), with detailed [usage guide](../../../docs/guide.md), [API reference](../../../docs/reference.md), and [troubleshooting](../../../docs/common-problems.md).

## Search and directory scope

`search_scope` is optional in `$HERMES_HOME/ai-memory.json`:

- `global` (default): preserves legacy global search and recall across projects.
- `project`: restricts tools, synchronous recall and background prefetch to the resolved workspace/project. Unknown scope returns no project results, never a global fallback.

For per-folder routing with one Hermes profile, use `{"search_scope": "project"}` and omit fixed `workspace`/`project` overrides. `AI_MEMORY_SEARCH_SCOPE` also accepts `global` or `project`. Explicit scope kwargs, file config and `AI_MEMORY_WORKSPACE` / `AI_MEMORY_PROJECT` take precedence over TOML.

At initialization, the plugin reads the nearest applicable `.ai-memory.toml` at or above the supplied **absolute, existing session directory**. For example, `imperio_2026/apps/socorro_financeiro/src` selects the application's marker rather than `imperio_2026/.ai-memory.toml`. No valid cwd or no project marker means unknown scope unless a project is explicitly configured. In that case writes, lifecycle capture, mirrors and handoff consumption are blocked; an explicitly configured `hermes-default` project remains supported. A custom programmatic `AiMemoryConfig.project` is also retained as a fallback. Implicit profile/default project labels alone do not authorize writes.

Legacy-generated default pairs no longer pin project-mode sessions; an intentional pair is preserved with `scope_origin=explicit`. New installations omit fixed scope, and setup scope prompts default to blank. See the [exact compatibility rule](../../../README.md#legacy-scope-defaults-and-explicit-overrides); indistinguishable hand-written legacy defaults require an explicit opt-out before enabling project mode.

Background workers snapshot the client, session and scope before dispatch. Cache keys include session, search mode, workspace, project and query, and initialization/session switches invalidate pending recall results.

**No dynamic folder-scope switching is supported in an existing session.** Hermes may update its live agent cwd, but the initialized memory provider is not given the updated cwd. Start a new session/provider initialized with the target folder; an ID-only `/new` or `/reset` on the same provider is not sufficient. The plugin never substitutes `os.getcwd()`. See the [Hermes memory-provider contract](https://hermes-agent.nousresearch.com/docs/developer-guide/memory-provider-plugin).

## Requirements

- Python 3.10+
- Hermes Agent with plugin support
- ai-memory server running locally or on LAN
- `httpx`
