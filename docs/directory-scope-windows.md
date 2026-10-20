# Windows directory scope: diagnosis without memory writes

## Confirmed host contract

The locally installed Hermes source was inspected read-only:

- `hermes_cli/main.py:_apply_in_dir` converts `--in` through
  `tools.environments.local._msys_to_windows_path`, validates the directory,
  changes the launch directory, refreshes an inherited `TERMINAL_CWD`, and
  disables restoring the previous session directory.
- `agent/agent_init.py:_memory_provider_init_kwargs` does **not** supply `cwd`.
- `_init_memory` passes those kwargs to `MemoryManager.initialize_all`.
- `agent/memory_manager.py:initialize_all` forwards kwargs unchanged, except
  for supplying a missing `hermes_home`.

Isolated execution of the installed host functions confirmed that
`/c/imperio_2026/apps/socorro_financeiro` converts to the native Windows path,
while the provider initialization kwargs have no `cwd` key. This is evidence
for this installed version and initialization path, not a captured argument
from the user's earlier session or a guarantee about every Hermes version.

Consequently, converting MSYS inside this plugin would not repair that host
contract. No MSYS converter was added. Direct `marker_scope('/c/...')` on
native Windows remains unsupported and fails closed. The host needs to pass
an absolute, existing **session** directory to `initialize(cwd=...)`; changing
that host contract is outside this plugin-only change. Do not substitute the
process cwd, environment terminal representation, or `agent_workspace`
(which is a workspace name, not a path).

## Read the actual provider instance

`provider.scope_diagnostics()` returns an allowlisted snapshot, under the
provider lock, without contacting the server or reading/writing memory:

- `cwd_received`: the string received at initialization, or empty if absent.
- `cwd_normalized`: native `Path` spelling after validation, or empty if invalid.
  This is not MSYS conversion, symlink canonicalization, or a cwd fallback.
- `search_scope`, `workspace`, `project`.
- `scope_known`, `writes_allowed`, `project_search_allowed`.

Inspect this method on the **already initialized instance** in a debugger or
host integration. Do not initialize another live provider just to inspect it:
normal initialization may fetch/consume a handoff when scope is known.
`ai_memory_status` checks the server; it is not this local diagnostic.

Initialization also emits an INFO record on the plugin's `provider` logger,
with the same fields. Path and scope values use `%r`, escaping newlines;
no configuration dump, URL, session transcript, handoff, token or API key is
included. Paths and project names can still identify a checkout, so treat
these logs as local operational metadata. Read the `ai-memory scope:` record
from a normal session startup; no memory-writing prompt is needed. Logging
visibility depends on the host's configured handlers and levels. This change
is only in the repository; it is not installed into the active Hermes profile.

An unknown scope still reports the configured default project name, but
`scope_known=False` and both authorization flags are false. That default name
is **not** evidence of a resolved project or permission to access it.

## Offline regression check

From this repository:

```bash
uv run pytest tests/test_cwd_diagnostics.py tests/test_directory_scope.py -q
```

Tests use temporary monorepos and fake clients/HTTP transports; the suite
blocks real socket connections and isolates profile/environment scope.
The Windows case uses the native filesystem and confirms that the closest
application marker (`socorro-financeiro`) wins over `imperio_2026` when the
provider actually receives the application directory. Other regressions cover
missing/relative/drive-relative/root-relative directories, files, absent cwd,
secret-free diagnostics, and reinitialization. Existing directory-scope tests
cover cache isolation, captured asynchronous context and obsolete workers.

Explicit workspace/project overrides continue to outrank directory markers.
Changing a terminal directory or passing cwd to `on_session_switch` does not
reinitialize directory scope: a new instance/session initialization with the
correct cwd is required. No user config or host installation is changed here.
