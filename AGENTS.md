# google-health-mcp - agent guide

`CLAUDE.md` symlinks to this file. It orients AI agents and contributors working *in* the code, and deliberately does not repeat the user-facing docs:

- **What it is, install, auth, tools, config, CLI, usage** -> [README.md](README.md)
- **Dev environment, running tests, pre-commit hook, PR & security process** -> [CONTRIBUTING.md](CONTRIBUTING.md)

**This is a public open-source repository and health data is sensitive PII.** Read the Data Safety Rules before committing.

## Data Safety Rules

Before committing ANY change, verify:

- **No real health measurements** in code, tests, commits, or docs - no real heart rates, sleep data, step counts, weight, SpO2, or HRV values
- **No personal identifiers** - no real names, account or user ids, or dates of birth
- **No credentials** - no OAuth tokens, client IDs, API keys, or token files
- **Test fixtures must use fictional data** - obviously fake values and fixed past dates (e.g. 2026-03-10)
- **Error messages and logs**: status codes and operation names only - never measurement values or API response bodies. Filesystem paths are governed separately, under What may leave the process: a CLI subcommand may print one, shared code may not
- **`config/` and `*.db` are gitignored for a reason** - never override this

The `scripts/check-no-data.sh` pre-commit hook refuses `*.db` files with their journals and copies, `*.bak` and `*.backup` files, anything under the top-level `config/` except `.gitkeep` and paths ending in `.example` or containing `.example.`, any added, changed or renamed file over 100KB except the root `uv.lock`, and any path containing a newline. Install it per [CONTRIBUTING.md](CONTRIBUTING.md#install-the-pre-commit-hook); CI does not run it. **It matches paths and sizes and never reads contents**, so a secret in code passes, and so does a credential file outside `config/` or named like an example. None of the rules above is fully enforced by it; check them yourself.

## What may leave the process

**Nothing built from an API response or an exception's own text reaches a user, a log or `sync_log`.** `run_sync` writes `str(e)` straight into `sync_log.notes` and returns it to the client, so every exception the client raises carries fixed text plus at most a status code and the operation, a data type and method. Never a resource name, which carries the account's user id, pinned by `test_an_error_names_the_operation_and_never_a_resource_name`. Not the response body - an error body can quote the measurement that caused it - and not the underlying exception, whose text is not this package's to vet: a filesystem error's text can name an absolute path. The same rule governs `auth.py`'s callback page and `auto_sync_if_stale`'s debug line, which report `type(e).__name__` for that reason - do not restore `exc_info=True` there. Pinned by `test_no_part_of_the_error_body_reaches_the_message` and `test_a_transport_failure_carries_no_path` in `tests/test_google_api.py`, by `test_auto_sync_logs_a_type_rather_than_a_traceback` in `tests/test_sync.py`, and for the interactive setup flow by the two source-reading assertions in `TestTheCallbackPage`: the exception may travel only as `type(e).__name__`, and the live exception may not be reached without binding it at all (`traceback.format_exc`, `sys.exc_info`, `locals`, a bare `except:`).

**Filesystem paths are a narrower rule than the one above, because two subcommands print them deliberately.** `doctor` exists to report which paths it resolved, and `import` names the directory it was given and the database it wrote to. Both are commands a person typed, and their output - stdout or stderr - is that person's terminal. **`doctor --json` widens that destination without widening the rule**: the payload carries the same resolved paths, and it is meant to be read by a program that may forward it off the host, so a consumer that does so owns that disclosure. What must stay true either way is the harder half - no credential value and no measurement reaches either format, pinned for both by the parametrised `test_never_prints_token_or_secret_values`, which drives the credential files the code actually opens - fictional ones, under `_no_real_credentials` - rather than hand-built findings. A leak test that builds its own findings never puts a secret anywhere and passes over code that dumps the whole token file.

Everything else must not carry one, and that includes every `logger.*` call. Logging is shared by both modes: run with no subcommand, the process speaks JSON-RPC on stdout and its log lines go to stderr, where the MCP client collects them, so a log line is closer to a tool response than to a terminal. The test for a new path is therefore **which code prints it, not which stream**: a CLI subcommand may, shared code may not. `sync_log.notes` and every tool response may not either, because both are stored or handed to the model. That is why `importer` prints the directory it was given and logs only a file's name.

## Architecture

- **Entry point**: `src/google_health_mcp/cli.py` - routes `auth`/`sync`/`rebuild`/`import`/`doctor` subcommands or starts the MCP stdio server. `sync` takes `--since`/`--until` to backfill or re-fetch a date range; `rebuild` is described in [sync.md](docs/agents/sync.md); `import` (backed by `importer.py`) bulk-loads exported data
- **MCP server**: `mcp_instance.py` creates the shared `MCPServer("google-health-mcp")` instance
- **Auth**: `auth.py` - Google OAuth setup and token refresh
- **API**: `api.py` - the Google Health client
- **DB**: `db.py` - SQLite schema (one table per cached data type, plus `authorisation` and `sync_log`), save/query helpers
- **Google normalisers**: `tools/google_sync.py` - one function per table, all writing through the same upsert
- **Tools**: `tools/` - domain-grouped modules; `sync_tools.py` also exports `auto_sync_if_stale(*data_types)`
- **Helpers**: `helpers.py` - `require_auth` (auth-gate decorator on every tool), plus `format_response`/`parse_date`
- **Errors**: `errors.py` - `GoogleHealthError`, the base every exception this package defines derives from
- **Import**: `importer.py` - bulk-loads exported data through this package's own writers
- **Config**: `config.py` - paths overridable via `GOOGLE_HEALTH_MCP_CONFIG_DIR` and `GOOGLE_HEALTH_MCP_DB_PATH`; `GOOGLE_HEALTH_MCP_OFFLINE` for cache-only mode
- **Doctor**: `doctor.py` - the `doctor` subcommand

## Before changing anything

The rest of the guide is in `docs/agents/`, one file per area. Most of each file is invariants a change can undo, most with the test that pins it.

**Before changing anything under `src/`, `tests/` or `scripts/`, read [seams.md](docs/agents/seams.md) in full.** Then read in full every file the table lists for what you are changing. Module paths in the first column are under `src/google_health_mcp/`. A test file takes the rows of the modules it exercises; if unsure, read the guide files that cite it by name.

| Before changing | Read |
|---|---|
| `auth.py` | [auth.md](docs/agents/auth.md), [tools.md](docs/agents/tools.md) |
| `api.py` | [auth.md](docs/agents/auth.md), [sync.md](docs/agents/sync.md), [tools.md](docs/agents/tools.md), [schema.md](docs/agents/schema.md) |
| `config.py` | [auth.md](docs/agents/auth.md), [schema.md](docs/agents/schema.md) |
| `errors.py`, `mcp_instance.py` | [tools.md](docs/agents/tools.md) |
| `helpers.py` | [tools.md](docs/agents/tools.md), [auth.md](docs/agents/auth.md) |
| `cli.py` | [tools.md](docs/agents/tools.md), [doctor.md](docs/agents/doctor.md), [sync.md](docs/agents/sync.md), [auth.md](docs/agents/auth.md) |
| `db.py` | [sync.md](docs/agents/sync.md), [schema.md](docs/agents/schema.md), [tools.md](docs/agents/tools.md) |
| `importer.py` | [sync.md](docs/agents/sync.md), [schema.md](docs/agents/schema.md) |
| `doctor.py` | [doctor.md](docs/agents/doctor.md), [auth.md](docs/agents/auth.md), [schema.md](docs/agents/schema.md) |
| `tools/google_sync.py` | [sync.md](docs/agents/sync.md), [schema.md](docs/agents/schema.md), [auth.md](docs/agents/auth.md) |
| `tools/sync_tools.py` | [sync.md](docs/agents/sync.md), [tools.md](docs/agents/tools.md), [auth.md](docs/agents/auth.md), [schema.md](docs/agents/schema.md) |
| Every other `tools/*_tools.py` | [tools.md](docs/agents/tools.md), [schema.md](docs/agents/schema.md) |
| A table or column in `SCHEMA` | [schema.md](docs/agents/schema.md), [sync.md](docs/agents/sync.md) |
| `README.md`, `CHANGELOG.md`, `CONTRIBUTING.md`, `pyproject.toml`, `uv.lock` | [seams.md](docs/agents/seams.md), [doctor.md](docs/agents/doctor.md), [sync.md](docs/agents/sync.md), [tools.md](docs/agents/tools.md) |

## Auto-sync behaviour

`get_*` reach `auto_sync_if_stale(*data_types)` through `refresh_before_query`, and `health_trends` calls it directly, before querying: it triggers an incremental sync if the last successful sync for that data type was before today (checked via `sync_log`, whose UTC `synced_at` is converted to local time before comparing, pinned by `test_a_sync_after_local_midnight_counts_for_the_local_day`), at most once per data type per day. Types a tool reads together are synced together when any of them is stale, so one of them can sync a second time that day. A `skipped` row counts as a sync here, or each query of a type the grant cannot read would re-run a sync that can only skip it again. Failures are swallowed silently - the cache query proceeds regardless.

That silence is why `run_sync` ends its per-type loop with a catch-all writing an `error` row: `sync_log` is the only record any failure leaves, and anything escaping the named handlers escapes into that bare `except`, leaving `doctor` reporting a clean log while the cache ages. **Keep the catch-all, and keep the row's notes to the exception type** - these paths carry API responses, and a message built from one could hold anything. The row is best-effort: writing it is itself a database write, and on a read-only database it fails too, so it is wrapped and the connection is closed in a `finally` around the whole loop rather than per handler. Pinned by `test_an_unnamed_failure_still_leaves_a_sync_log_row`, `test_the_unnamed_failure_message_carries_no_response_content` (both assert the note by equality - a substring check passes with the exception interpolated) and `test_a_database_that_cannot_record_the_failure_does_not_escape`, in `tests/test_sync.py`.

**A resumed sync starts `INCREMENTAL_OVERLAP_DAYS` behind its cursor, not at it, and that is load-bearing.** Google publishes a day's `daily-*` summary after the day has ended, so a sync running late in the local day finds nothing for yesterday and still records that it attempted through today; resuming from that cursor would never ask for yesterday again. Nothing reports the loss, because `sync_log` records `ok`, which is what `doctor` grades. A re-fetch of a day the cursor has passed returned the rows the incremental sync never stored, so the day was lost to the cursor, not missing upstream. With an hourly sync the cursor is already today after the first run of a day, so without the overlap every later run that day asks only for today, and a day the first run missed is never asked for again. The property the constant buys, stated so the value can be re-derived: **a day is asked for again on every run from that day through `INCREMENTAL_OVERLAP_DAYS` later, so the overlap tolerates a publication lag of up to that many days** - measured at one, with the rest as margin. It is not about a host being off, which needs none of it: `last_date_attempted` is written only on success, so the cursor does not advance while nothing runs. The overlap is bounded because that cursor exists to stop a type nobody records any more being re-queried from its last reading on every run forever, and the upsert makes the repeated days free of consequence. Pinned by `TestTheIncrementalWindowLooksBack` in `tests/test_sync.py`.

Three ordering rules in `run_sync` are load-bearing and easy to undo. `HealthOfflineError` must keep a clause of its own before the trailing catch-all, or offline mode turns into per-type error rows instead of one clean message. The clause need not close the connection; the `finally` in `run_sync` does. A failure is logged `auth_error` rather than `error` because `doctor` grades the two differently and a dead token is the one that will not clear itself. And the wait a 429 asks for is clamped to `api.MAX_RATE_LIMIT_WAIT` where the header is read, so a caller that ever sleeps on it cannot be handed an unbounded or unparseable value - nothing sleeps on it today. Pinned by `test_offline_error_propagates_and_closes` and the `TestTheRateLimitWait` cases.

## Running tests

Run the suite as [CONTRIBUTING.md](CONTRIBUTING.md) describes - all of it uses temporary SQLite and fictional data. What that file does not cover:

**No test sees the machine's own credentials, and that is what makes the suite mean the same thing everywhere.** `_no_real_credentials` in `tests/conftest.py` points `GOOGLE_TOKENS_PATH` and `GOOGLE_CLIENT_PATH` at an empty directory for every test. It is a data-safety mechanism, not tidiness: every query tool opens with `refresh_before_query` (and `health_trends` with `auto_sync_if_stale` directly), which syncs whenever the cache holds no `sync_log` row for the type, and a temp database never has one. Without `_no_network`, a usable token would let that sync write the real account into the test's database and into the windows the test asserts over. This fixture keeps the token from being found at all. **Both `config` and `helpers` are patched, and the second is the one to keep**: `helpers` imports the two paths by value, so it holds its own copies, and a fixture patching `config` alone leaves `require_auth` - worn by every tool - reading the real files while everything else looks isolated. `auth` and `doctor` reach them through `config` at call time. Pinned by `test_the_suite_cannot_see_real_credentials` in `tests/test_seams.py`, whose equality half fires anywhere and whose existence half fires only where a credential is present.

**`_no_network` is the backstop, and `pytest.fail` rather than `assert` is load-bearing.** It replaces `urllib.request.urlopen`, which is measured to be the only call this package makes a request through (`api.py`, `auth.py`) and is not pinned as such - a route opened through `socket`, `http.client`, a subprocess or a new dependency would pass it. `refresh_google_token` and `auto_sync_if_stale` both end in `except Exception`, so an `AssertionError` raised in that fixture is caught by the code under test and reported as an ordinary network failure, which is the silence the fixture exists to break; `Failed` derives from `BaseException` and neither catch-all can absorb it. Pinned by `test_the_suite_cannot_reach_the_network` in `tests/test_seams.py`, which asserts the type as well as the message. A test that means to exercise a request patches `urlopen` itself, which replaces the fixture for its duration.

**`_no_real_cache` points `db.DB_PATH` at a fresh temporary file for every test**, because two calls resolve it implicitly. `auto_sync_if_stale` calls `db.get_db()` with no argument, and `get_db` creates the file and the whole schema before any request is made, so without the fixture a refused sync still writes an `auth_error` row into whatever cache that is. And every tool response asks `db.recorded_missing_scopes()` which permissions the grant lacks, which would make the suite's answers depend on what the machine's own cache records. A test wanting a particular database still patches `db.DB_PATH` itself, which replaces the fixture's value. Only `db` is patched: `config.DB_PATH` is what `doctor` reads and what `test_default_paths_exist` checks, and `doctor`'s tests set it through `setup_paths`. Note `GOOGLE_HEALTH_MCP_DB_PATH` does work from outside, but only exported before pytest starts - `config` resolves it at import, so `_isolate_from_ambient_env`'s `delenv` cannot move a path that is already bound.

**Do not isolate by setting `GOOGLE_HEALTH_MCP_CONFIG_DIR` to an empty directory**: `test_default_paths_exist` asserts the *default* config directory is named `google-health-mcp`, and the override makes it the temp directory's name instead, so that test fails for a reason that has nothing to do with the change under test.
