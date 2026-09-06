# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 1.5.0 - 2026-09-06

### Fixed

- `doctor` no longer reports a data type as a stopped series when no successful sync has ever recorded a row for it. Such a series cannot resume, so the warning was permanent, and a warning nothing can clear is the state the report then spends its life in. Where a monitor matches the `stopped-series` slug it also held that monitor down, which on a monitor carrying other signals silenced those too.
- The exempted type is still reported, graded `ok`, carrying the new `series-never-filled` check name. A check that simply goes quiet about a type reads as one that has gone blind, and a monitor watching `stopped-series` cannot otherwise tell a series that recovered from one this build stopped judging.

### Changed

- The exemption is on positive evidence only, and says only what it observed: that no successful sync wrote those rows, never where they came from. A type absent from `sync_log`, or whose `records_added` total is NULL, is unknown and judged exactly as before, so an existing cache and one this package did not write both keep today's behaviour. Only `ok` rows count, because a run that errored records no rows either. And the total must sum to an integer: SQLite sums a text column to a real `0.0`, so a foreign writer storing something other than a count would otherwise exempt a type that genuinely stopped.

## 1.4.0 - 2026-09-06

### Fixed

- `health_get_exercises` refuses an `exercise_type` that matches no workout name in your cache, instead of reporting the period as empty. A typo came back as `No exercise entries found for this period`, which says you did not train rather than that the filter could not be used, and a caller has no way to tell one from the other. The refusal names the workouts the cache holds and keeps the `live=True` hint, since a name absent from the cache may be a window that was never synced rather than a workout you have never done. Google names the workouts, so those names are read from your own data rather than declared in the tool's schema, and a workout type recorded for the first time needs no change here.
- In offline mode the refusal says live fetch is disabled and that the host owning the cache must sync the period, rather than suggesting `live=True`. An empty answer already said that, but a refusal is raised rather than returned, so it bypassed the correction.
- A filter is matched against stored names in one place, so a name outside ASCII is found whatever case you ask in. The match ran in SQL, whose `LOWER` folds ASCII alone, so `LÄUFEN` found nothing while `CYCLING` worked.

### Changed

- `exercise_type` now refuses two things it used to answer. A value matching no cached name is refused rather than reported as an empty period, and `%` and `_` are ordinary characters rather than SQL wildcards, so `%` no longer matches every workout. It is still a substring match rather than an exact one.

## 1.3.0 - 2026-09-02

### Fixed

- `health_trends` reads the aggregation period it was asked for. Any value other than `weekly` or `quarterly` fell through to monthly, so a period the tool did not recognise returned twelve months of correct figures labelled with whatever was sent. `"aggregation": "not-a-period"` sat beside a full year of real data. Nothing was empty and nothing failed, which made it worse than an error: the numbers were right and the label describing them was not.

### Changed

- `data_type` and `period` accept only their listed values, and those values are declared in the tool's schema rather than checked inside it. A caller sending anything else now gets a validation error naming the accepted values, where an unrecognised `data_type` previously returned `{"error": ...}` after the tool had already run a sync for it. The `data_type` values a caller is offered are exactly the ones the tool can answer, because the schema is built from the dispatch it reads.

## 1.2.0 - 2026-08-31

### Fixed

- `health_trends` no longer abandons its database connection when it is given a date it cannot parse. It opened the cache before validating the argument and closed it only on the way out, so a rejected call left the handle for the cyclic garbage collector to reclaim instead of closing it at the point of failure.

### Changed

- `mcp` 2.1.1, up from 2.0.0. That release keeps a `ToolError`'s text and replaces every other exception's with `Error executing tool <name>`, which on its own would have left a query tool answering a date it cannot parse, or an expired token, with nothing but that line. The errors this package raises as its own types now travel as `ToolError`, so a caller still gets the text that says what to do: which date formats parse, that the token needs re-authorising, that the grant is missing a scope. Anything unplanned keeps the new behaviour and stays in the server's log, which is where the absolute path in a failure to read the token file or the cache belongs.

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
