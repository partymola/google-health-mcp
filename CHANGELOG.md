# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 1.1.1 - 2026-08-21

### Fixed

- On Windows, `google-health-mcp doctor` no longer reports the OAuth callback port free while an earlier `google-health-mcp auth` still holds it. The check binds the port to see whether anything is there, and it asked for address reuse so that a socket left closing by the previous `auth` run would not read as a conflict. Windows grants that request against a socket that asked for reuse itself - which the callback server did until this release - so the one holder the check most needed to see was the one it could bind straight over. A port held by an unrelated process was reported correctly throughout. Reuse is now asked for only where it means what it is for, which on Windows trades that for the port reading as busy for a few minutes after authorising.
- `google-health-mcp auth` reports a busy callback port instead of ending in a traceback, and binds it before opening the browser rather than after, so a run that cannot receive the callback does not send you to an authorisation page first.

### Packaging

- The package declares `Operating System :: OS Independent`, and CI runs the suite on Linux, macOS and Windows. It had only ever been tested on Linux while claiming every platform.

### Security

- On Windows, `google-health-mcp auth` no longer lets another process take over the port the authorisation code arrives on. The callback listener inherited `allow_reuse_address` from `http.server`, which on POSIX only waives the wait after a previous run. Windows reads it as consent to be displaced, so another process could bind over the live listener and receive the callback - and the code it carries is enough to obtain the tokens. The listener no longer asks to share the address, which is what closes it, and asks for exclusive use as well. The cost is Windows-only: after a run the port stays held until the previous connection has finished closing, normally a couple of minutes, and `auth` has to be retried until it has. Nothing changes on Linux or macOS.

## 1.1.0 - 2026-08-20

### Added

- `doctor --json` emits the findings as JSON on stdout, for a monitor rather than a person. Each finding carries a `check` field: a stable identifier to match on, since `name` is prose and interpolates the data type. `stopped-series` is the first, because a stopped data series is graded a warning and `doctor` exits non-zero only on a failure, so the exit code cannot carry the one condition an external monitor most needs. The exit code is unchanged in both formats.
- The JSON payload names the package version that produced it. Without it a consumer cannot tell "no stopped series" from "this build has no such check": an earlier release omits the `check` field entirely, and one earlier still rejects `--json` and exits 2.

## 1.0.0 - 2026-08-20

### Added

- First release. Reads the Google Health API into a local SQLite cache and serves it over MCP: resting heart rate, activity, exercises, sleep, weight and body fat, SpO2, HRV, active zone minutes, breathing rate, skin and core temperature, cardio fitness, food and water, electrocardiograms, and irregular-rhythm notifications.
- `health_trends` aggregates any cached daily series by week, month or quarter, and compares two periods.
- `health_get_ecg` returns a reading's classification, average rate and duration; the waveform itself travels only when asked for, since a single trace is thousands of voltages.
- Offline mode (`GOOGLE_HEALTH_MCP_OFFLINE`) serves the cache with no credentials and makes no network call, so one host can sync a shared database and the rest only read.
- `doctor` diagnoses a setup offline and read-only, without spending quota or touching a token another host owns. It reports which paths resolved where, whether the client file is the Desktop-app JSON the console downloads, and whether the stored refresh token records a short expiry - Google grants a 7-day one while an OAuth app's publishing status is Testing, which otherwise stops syncing about a week after setup with nothing to point at.
- `doctor` also reports a single data type that filled most days and now fills none while the others carry on. That is the shape a renamed upstream field takes: every page parses to nothing, the request still succeeds, and the table stops filling with the sync log recording `ok`. Graded a warning, since a series can also stop because the device stopped being worn.
- `sync --since/--until` re-fetches an exact window, for backfilling history older than the cache or repairing a gap in the middle of it.
- `import --data-dir` bulk-loads exported JSON files into the cache.
