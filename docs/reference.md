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

Precedence: **env vars > file config > defaults**

### Config File Location

`$HERMES_HOME/ai-memory.json`

### Scope Resolution

`initialize()` resolves `workspace` / `project` in this order:

1. Explicit kwargs — `ai_memory_workspace`, `project`
2. Explicit config — `workspace` / `project` in `ai-memory.json`, or `AI_MEMORY_WORKSPACE` / `AI_MEMORY_PROJECT`
3. The nearest `.ai-memory.toml` at or above the session's `cwd`, read with ai-memory's own line-based parser (a marker that pins no scope key is skipped)
4. What Hermes reports — `agent_workspace`, then `agent_identity` as `hermes-<identity>` — and finally the defaults (`hermes` / `hermes-default`)

A caller that reports no `cwd` never consults a marker, so it keeps its configured scope. The marker deliberately outranks Hermes's `agent_workspace` (the literal `"hermes"`): letting that value win pinned every session to a project no other agent in the same repository wrote to, which is what broke cross-agent handoff.

### Config Schema (for `hermes memory setup`)

| Key | Secret | Env Only | Env Var | Default |
|---|---|---|---|---|
| `server_url` | no | no | — | `http://127.0.0.1:49374` |
| `api_key` | yes | yes | `AI_MEMORY_API_KEY` | `""` |
| `auth_token` | yes | yes | `AI_MEMORY_AUTH_TOKEN` | `""` |
| `workspace` | no | no | — | `"hermes"` |
| `project` | no | no | — | `"hermes-default"` |

**Env Only** fields are never written to `ai-memory.json`. They must be set via environment variables.

## `AiMemoryClient`

Typed HTTP wrapper in `client.py`. Accepts `AiMemoryConfig`. Persistent `httpx.Client` with connection pooling.

### Methods

#### `search(query, workspace=None, project=None, limit=3) → list[dict]`

**HTTP:** `GET /admin/search?q=<query>&limit=<n>&workspace=<ws>&project=<proj>`  
**Timeout:** 10s  
**Raises:** `httpx.HTTPStatusError` on non-2xx

Returns list of result dicts with keys like `path`, `snippet`, `score`.

#### `write_page(path, body, tags=None, tier=None, pinned=False, workspace=None, project=None) → dict`

**HTTP:** `POST /admin/write-page`  
**Timeout:** 10s  
**Raises:** `httpx.HTTPStatusError` on non-2xx

Returns `{"ok": true, "path": "..."}` or error dict.

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
- `queue_prefetch(query, *, session_id="")` — daemon thread; caches results per `(session_id, query)` for the next turn
- `recall_status() → RecallStatus | None` — hit count of the last `prefetch`, `None` when it injected nothing
- `sync_turn(user, assistant, *, session_id="", **kwargs)` — daemon thread
- `on_session_end(messages, **kwargs)` — daemon thread; sends the session id, never the transcript
- `on_memory_write(action, target, content, metadata=None)` — mirrors `add` / `replace`; `remove` is skipped
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
