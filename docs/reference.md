# Reference

## Configuration

### `AiMemoryConfig`

Dataclass in `config.py`. Fields:

| Field | Type | Default | Description |
|---|---|---|---|
| `server_url` | `str` | `http://127.0.0.1:49374` | ai-memory HTTP API endpoint |
| `api_key` | `str` | `""` | API key (Bearer token) |
| `auth_token` | `str` | `""` | Auth token alias |
| `workspace` | `str` | `"hermes"` | ai-memory workspace name |
| `project` | `str` | `"hermes-default"` | ai-memory project name |
| `search_scope` | `str` | `"global"` | `global` (legacy recall) or `project` (isolated recall); other values are rejected |

Precedence: **env vars > file config > defaults**

### Config File Location

`$HERMES_HOME/ai-memory.json`

### Scope Resolution

`initialize()` resolves `workspace` / `project` in this order:

1. Explicit kwargs — `ai_memory_workspace`, `project`
2. Explicit config — `workspace` / `project` in `ai-memory.json`, or `AI_MEMORY_WORKSPACE` / `AI_MEMORY_PROJECT`
3. The nearest `.ai-memory.toml` at or above the session's `cwd`, read with ai-memory's own line-based parser (a marker that pins no scope key is skipped)
4. What Hermes reports — `agent_workspace`, then `agent_identity` as `hermes-<identity>` — and finally the defaults (`hermes` / `hermes-default`)

Only an absolute, existing directory is eligible for marker lookup. Missing, relative and file paths are not anchored to process cwd and do not inherit a parent marker. A missing marker or an incomplete scope cannot authorize writes through implicit profile/default project labels. Explicit project configuration (including `hermes-default`) remains supported; custom programmatic project values remain a configured fallback. Each initialization starts from configuration, never from the previously resolved marker scope.

`search_scope=global` preserves legacy global search; `search_scope=project` applies the resolved scope to every provider search/prefetch. An unknown scope in project mode returns an error/empty recall, never global results. Writes, lifecycle capture, mirrors and handoff consumption require a known workspace/project and a primary context in either search mode.

`mirror_memory` (default `false`, or `AI_MEMORY_MIRROR_MEMORY`) mirrors built-in `MEMORY.md`/`USER.md` writes into the wiki. Off by default: Hermes memory is already injected every session, so the wiki copy is a second, staler source of truth.

A failed tool call returns a final answer rather than raising, so the agent does not retry a store that is down: `{"ok": false, "unavailable": true, "retry": false, "error": "ai-memory store unavailable (ConnectError); do not retry — carry on with skills, Hermes memory and session_search."}`. A rejected request (`HTTPStatusError`) reports the status code with `retry: false`; an empty `ai_memory_search` query is refused locally without touching the server.

**No dynamic directory-scope switching:** the memory-provider contract supplies the construction cwd at `initialize()`, not the updated live agent cwd. A new initialized provider with the target directory is required; `on_session_switch()` updates the session ID and invalidates recall, but does not re-resolve cwd. See the [Hermes contract](https://hermes-agent.nousresearch.com/docs/developer-guide/memory-provider-plugin).

Legacy-generated default pairs are exempt from explicit file precedence only in project mode and only for the narrow format documented in [Legacy scope defaults](../README.md#legacy-scope-defaults-and-explicit-overrides). `scope_origin=explicit` preserves an intentional default pair; saving nonblank scope values records that metadata. Install/update defaults and blank setup answers do not pin projects. UTF-8 files with a BOM (PowerShell 5.1) are supported. Loading never writes migration changes to the profile.

### Config Schema (for `hermes memory setup`)

| Key | Secret | Env Only | Env Var | Default |
|---|---|---|---|---|
| `server_url` | no | no | — | `http://127.0.0.1:49374` |
| `api_key` | yes | yes | `AI_MEMORY_API_KEY` | `""` |
| `auth_token` | yes | yes | `AI_MEMORY_AUTH_TOKEN` | `""` |
| `workspace` | no | no | — | `""` (directory routing) |
| `project` | no | no | — | `""` (directory routing) |
| `search_scope` | no | no | `AI_MEMORY_SEARCH_SCOPE` | `"global"` |

**Env Only** fields are never written to `ai-memory.json`. They must be set via environment variables.

## `AiMemoryClient`

Typed HTTP wrapper in `client.py`. Accepts `AiMemoryConfig`. Persistent `httpx.Client` with connection pooling.

### Methods

#### `search(query, workspace=None, project=None, limit=3) → list[dict]`

**HTTP:** `GET /admin/search?q=<query>&limit=<n>&workspace=<ws>&project=<proj>`  
**Timeout:** 10s  
**Raises:** `httpx.HTTPStatusError` on non-2xx

Returns list of result dicts with keys like `path`, `snippet`, `score`.

#### `read_page(path, workspace=None, project=None) → dict | None`

**HTTP:** `GET /admin/read-page?path=<path>&workspace=<ws>&project=<proj>`  
**Timeout:** 5s (`READ_TIMEOUT`)  
**Returns:** the page (`path`, `title`, `body`, `frontmatter`), or `None` on 404

The server requires an explicit `workspace`; a call without one cannot verify
anything, which is why `write_page` reports `verified: false` for it.

#### `write_page(path, body, tags=None, tier=None, pinned=False, workspace=None, project=None) → dict`

**HTTP:** `POST /admin/write-page`, then `GET /admin/read-page` to verify  
**Timeout:** 10s write, 5s read-back  
**Raises:** `ValueError` for an empty path, a path that escapes the wiki, or an empty body; `httpx.HTTPStatusError` on non-2xx

The path is normalised first (`normalise_page_path()`: strip `./` and a leading
`/`, collapse `//`, force the `.md` suffix), because ai-memory keys a page by its
literal path — `_rules/x` and `_rules/x.md` used to be two live pages for one
fact and never superseded each other.

The returned dict reports what actually happened:

| key | meaning |
|---|---|
| `path` | the canonical page path that now exists |
| `verified` | a read-back returned the body that was written |
| `unchanged` | the page already held this exact body; nothing was written |
| `recovered_after_timeout` | the write timed out but landed; the read-back proves it |
| `existed` / `previous_chars` / `previous_body_excerpt` | the page was replaced (a new version) |
| `near_duplicate` | another live page already covers this topic |

A timeout is resolved by reading the page back: if the body matches, the write
is reported as landed instead of raising; if it did not land, the whole payload
is retried once — never a partial one. A pin already on the page, or declared in
the body's YAML frontmatter, is sent even when the caller does not mention it.

#### `status() → dict`

**HTTP:** `GET /admin/status`  
**Timeout:** 10s

Returns the server's status report. The lifetime counters are nested under `counts` (`pages_latest`, `pages_all`, `sessions`, `observations`, `evidence_rows`), alongside `version` and `data_dir`.

#### `send_hook(event, session_id, payload=None, workspace=None, project=None, cwd=None, timeout=None) → None`

**HTTP:** `POST /hook?event=<event>&session_id=<sid>&workspace=<ws>&project=<proj>&cwd=<cwd>`  
**Timeout:** `timeout` when given, else `HOOK_TIMEOUT` (0.5s); the provider passes `SESSION_END_TIMEOUT` (10s) for `session-end`  
**Errors:** Swallowed (logged at exception level)

Dropped without a request while the client is paused: after `FAILURE_THRESHOLD` (3) consecutive transport failures it stops calling the server for `COOLDOWN_SECONDS` (60s), and `last_failure()` explains why. A successful request clears that state.

#### `fetch_handoff(agent="hermes", cwd=None, workspace=None, project=None) → str | None`

**HTTP:** `GET /handoff?agent=<agent>&cwd=<cwd>`  
**Timeout:** 10s  
**Returns:** `None` on 404, raises on other errors

## `AiMemoryProvider`

Implements `MemoryProvider` ABC in `provider.py`.

### Properties

- `name → "ai-memory"`

### Methods

- `is_available() → bool` — checks `server_url` is non-empty
- `unavailable_reason() → str` — the missing `server_url`, or the recorded transport failure; `""` while healthy
- `initialize(session_id, **kwargs)` — resolves scope, reloads config, fetches the handoff once (skipped in non-primary contexts)
- `get_config_schema() → list[dict]`
- `save_config(values, hermes_home) → list[str]` — returns list of skipped secret keys
- `get_tool_schemas() → list[dict]` — OpenAI function schemas (the `parameters` key)
- `handle_tool_call(name, args) → str` (returns JSON string)
- `system_prompt_block() → str`
- `prefetch(query, *, session_id="") → str` — cached hits from `queue_prefetch`, else a search; each hit is labelled `title (path): snippet`
- `queue_prefetch(query, *, session_id="")` — daemon thread; captures client/scope and caches by `(session_id, search_scope, workspace, project, query)`; initialization/session switches discard stale results
- `recall_status() → RecallStatus | None` — hit count of the last `prefetch`, `None` when it injected nothing
- `sync_turn(user, assistant, *, session_id="", **kwargs)` — daemon thread
- `on_session_end(messages, **kwargs)` — daemon thread; sends the session id, never the transcript
- `on_memory_write(action, target, content, metadata=None)` — mirrors `add` / `replace` only when `mirror_memory` is on (default off); `remove` is skipped
- `shutdown()`

## CLI (`register_cli`)

Registered via `register_cli(subparsers)` in `cli.py`.

### Subcommands

#### `hermes ai-memory status`

Checks ai-memory server reachability. Prints the server version and the page / session / observation counts read from `counts`.

#### `hermes ai-memory config`

Displays active config values (secrets show their source: env var or not set).

#### `hermes ai-memory config-set <key> <value>`

Sets a config value. Secrets (`api_key`, `auth_token`) are rejected with instructions to use the corresponding environment variable instead.

#### `hermes ai-memory link`

Creates a symlink `$HERMES_HOME/plugins/ai-memory → <plugin_dir>`.

## `plugin.yaml`

```yaml
name: ai-memory
version: 0.1.0
description: "ai-memory wiki-backed long-term memory provider"
kind: exclusive
python_dependencies:
  - httpx
```

## `register(ctx)`

Entry point in `__init__.py`:

1. Resolves config: file (`$HERMES_HOME/ai-memory.json`) → env var overrides (env wins for secrets)
2. Creates `AiMemoryConfig` with merged values (secrets env-only, never persisted)
3. Instantiates `AiMemoryProvider(config=config)`
4. Calls `ctx.register_memory_provider(provider)`

## Install Scripts

| Platform | Script | Requirements | Behavior |
|---|---|---|---|
| Linux/macOS | `scripts/install.sh` | `curl`, `tar` | Symlinks plugin from local repo; downloads from GitHub when run via `bash <(curl ...)` |
| Windows | `scripts/install.ps1` | PowerShell 5.1+ with .NET | Creates junction from local repo; downloads from GitHub when run via `iex` |
| Linux/macOS | `scripts/uninstall.sh` | `bash` | Removes plugin directory/symlink; disables plugin in Hermes if CLI exists |
| Windows | `scripts/uninstall.ps1` | PowerShell 5.1+ | Removes plugin directory/junction; disables plugin in Hermes if CLI exists |
| Linux/macOS | `scripts/update.sh` | `curl`, `tar` | Downloads latest plugin from GitHub, backs up old install, preserves config |
| Windows | `scripts/update.ps1` | PowerShell 5.1+ with .NET | Downloads latest plugin from GitHub, backs up old install, preserves config |

All scripts run **pre-flight checks** before making changes:

1. Hermes CLI availability (warn if missing)
2. Hermes process status (warn if running — restart needed)
3. ai-memory server reachability (try `/admin/status` endpoint)
4. Existing plugin state (installed? symlink/copy? empty?)
5. Write permissions to `$HERMES_HOME/plugins/`
6. Config file existence and contents
7. Wrong nested path detection (`plugins/memory/ai-memory/`)

### Script Flags

| Flag | Bash | PowerShell | Env var | Description |
|---|---|---|---|---|
| Dry run | `--dry-run` | `-DryRun` | — | Show what would happen without making changes |
| Skip prompts | `--yes` | `-Yes` | `FORCE=true` | Skip confirmation prompts (for CI/automation) |
| Help | `--help` | — | — | Show usage information |

When piped (non-interactive), scripts detect the missing TTY, print a warning, and proceed. Set `FORCE=true` or pass `--yes`/`-Yes` to silence the warning.

### Environment Variables

| Variable | Used By | Description |
|---|---|---|
| `HERMES_HOME` | install/uninstall | Hermes profile directory (default: `~/.hermes` / `%USERPROFILE%\.hermes`) |
| `AI_MEMORY_SERVER_URL` | install | Initial `server_url` written to `ai-memory.json` |
| `REPO_TARBALL_URL` | install | Override the GitHub tarball/zip URL used by the one-liner fallback |
| `REMOVE_CONFIG` | uninstall (bash) | Set to `true` to delete `$HERMES_HOME/ai-memory.json` |
| `FORCE` | all scripts | Set to `true` to skip confirmation prompts (same as `--yes` / `-Yes`) |
| `DRY_RUN` | all scripts | Set to `true` to enable dry-run mode (same as `--dry-run` / `-DryRun`) |

## Quality Gates

| Check | Command | Target |
|---|---|---|
| Lint | `ruff check .` | 0 errors |
| Types | `mypy .` | 0 issues |
| Tests | `pytest --cov` | 91 tests, ≥89% coverage |
