# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
