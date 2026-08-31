# google-health-mcp

<!-- mcp-name: io.github.partymola/google-health-mcp -->

[![CI](https://github.com/partymola/google-health-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/partymola/google-health-mcp/actions/workflows/ci.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/downloads/)
[![PyPI](https://img.shields.io/pypi/v/google-health-mcp)](https://pypi.org/project/google-health-mcp/)
[![Glama MCP Server](https://glama.ai/mcp/servers/partymola/google-health-mcp/badges/score.svg)](https://glama.ai/mcp/servers/partymola/google-health-mcp)

MCP server for the [Google Health API](https://developers.google.com/health), with a local SQLite cache and trend analysis.

Designed for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) and other [MCP](https://modelcontextprotocol.io/) clients. Your data syncs to a database on your own machine, so queries are fast, work offline, and cost no API quota.

## Features

- **Local SQLite cache** - sync once, query instantly
- **Incremental sync** - each run fetches only what is new, resuming from where the last one stopped
- **Offline mode** - serve the cache with no credentials and no network at all
- **Trends** - weekly, monthly or quarterly aggregates, and two-period comparisons
- **ECG** - readings stored whole, waveform included, returned only when asked for
- **`doctor`** - diagnoses a setup offline and read-only, without spending quota

## Data types

| Tool | Data |
|------|------|
| `health_get_heart_rate` | Resting heart rate |
| `health_get_activity` | Steps, calories, distance, floors |
| `health_get_exercises` | Workouts (name, duration, heart rate, calories) |
| `health_get_sleep` | Duration, stages, sleep period |
| `health_get_weight` | Weight, body fat % |
| `health_get_spo2` | Nightly blood oxygen saturation |
| `health_get_hrv` | Heart rate variability (RMSSD) |
| `health_get_azm` | Active zone minutes, with the per-zone breakdown |
| `health_get_breathing_rate` | Nightly breaths per minute |
| `health_get_skin_temperature` | Nightly variation from your baseline, and the absolutes behind it |
| `health_get_core_temperature` | Body temperature readings you logged by hand |
| `health_get_cardio_fitness` | VO2 max, where the device reports it |
| `health_get_food_log` | Food calories and water, where logged |
| `health_get_ecg` | Electrocardiograms: classification, average rate, duration, waveform on request |
| `health_get_irregular_rhythm` | Irregular-rhythm notifications and the windows that triggered them |
| `health_get_devices` | Paired devices, battery level, last sync |
| `health_get_lifetime_stats` | Totals and best days over the cached history, with its coverage |
| `health_trends` | Aggregated averages and period comparisons |

## Requirements

- Python 3.13+ (tested on 3.13 and 3.14, on Linux, macOS and Windows, in CI)
- A Google account with health data, and a Google Cloud project to authorise against. **No billing account is needed** - the console offers a free trial throughout setup and you can decline all of it.

## Setup

### 1. Install

```bash
pip install google-health-mcp
```

Or run it without installing, in which case every `google-health-mcp ...` command you run below becomes `uvx google-health-mcp ...`:

```bash
uvx google-health-mcp --version
```

### 2. Create the Google Cloud project

Every user registers their own OAuth client. This is seven console steps, and the page names are Google's as of August 2026.

**Google's own [setup page](https://developers.google.com/health/setup) will send you somewhere else - follow the steps below instead.** Its quick-start builds a *Web* client with `https://www.google.com` as the redirect URI, which suits the OAuth Playground rather than a program running on your machine; this server refuses that file and says so. Use that page only to check whether one of the pages below has been renamed.

1. **Project.** Create a project at [console.cloud.google.com/projectcreate](https://console.cloud.google.com/projectcreate) and select it.
2. **API.** Enable **Google Health API** on the [API Enablement page](https://console.cloud.google.com/apis/library/health.googleapis.com).
3. **Get started.** Open **Google Auth Platform** and complete **Get started** - app name, support email, **External** audience, contact email. A new project has no Audience, Data Access or Clients page until this is done.
4. **Audience.** Under **Test users**, add your own Google account. Skipping this fails sign-in with `403: access_denied`.
5. **Data Access.** Click **Add or remove scopes**, search for "Google Health API", and tick the read-only scopes listed under [OAuth scopes](#oauth-scopes) below.
6. **Clients.** Create an OAuth client of type **Desktop app** and download its JSON. A Desktop client permits the loopback redirect automatically, so there is nothing to register; a Web client does not, and fails at consent instead.
7. **Publish.** Back on the Audience page, click **Publish app**.

**Step 7 is the one that bites, and it is worth checking rather than assuming.** While an app's publishing status is Testing, Google issues refresh tokens that expire seven days after consent - so everything works, and then syncing stops a week later with nothing pointing back to this moment. The Audience page can read "In production" while the token server disagrees. Two readings that do not: the verification-status line on the **Branding** page, and `google-health-mcp doctor`, which fails loudly when the stored token records a short expiry.

### 3. Authorise

Put the downloaded client JSON where the server looks for it, unedited:

```bash
mkdir -p ~/.config/google-health-mcp
cp ~/Downloads/client_secret_*.json ~/.config/google-health-mcp/google_client.json
google-health-mcp auth
```

Your browser will warn that **Google hasn't verified this app**. That is expected, and the app is your own: these health scopes are classified restricted, and verification only matters above 100 users. Click **Advanced**, then **Go to google-health-mcp (unsafe)**, and grant the scopes.

The flow listens on `localhost:8081` for the callback, so that port must be free. It saves tokens to `~/.config/google-health-mcp/google_tokens.json`, created 0600 on POSIX. Windows keeps only the owner-write bit, as its read-only attribute, and governs access by ACLs - so there the file is not restricted to your account, and what it grants is whatever its directory's ACLs pass down. Access tokens last an hour and refresh automatically. Refresh tokens do not rotate, so a token minted on a machine with a browser can be copied to a headless one.

**If you authorised before publishing the app**, re-run `google-health-mcp auth` afterwards: publishing does not extend a token already granted, and that one still expires after seven days.

### 4. Register with your MCP client

```bash
claude mcp add -s user google-health -- google-health-mcp
```

Running it with `uvx` instead: `claude mcp add -s user google-health -- uvx google-health-mcp`.

### 5. Check it

```bash
google-health-mcp doctor
```

Worth running before step 3 (Authorise) as well as after: it reports whether port 8081 can be bound and whether this host can open a browser, which are the two ways `auth` fails before it starts.

Offline and read-only: it reports which paths resolved where, whether the credential files are the right shape, whether the token is short-lived, and whether the cache is being kept up to date.

`doctor --json` reports the same findings for a monitor to act on:

```json
{
  "version": "1.2.0",
  "findings": [
    {
      "check": "stopped-series",
      "name": "hrv series",
      "severity": "warn",
      "detail": "No hrv since 2026-03-30, after rows on 28 of the 30 days before that.",
      "fix": "Re-sync that type alone (...)"
    }
  ],
  "counts": {"ok": 7, "warn": 1, "fail": 0}
}
```

The payload goes to stdout; logging goes to stderr, so a `subprocess` consumer should read the two separately.

Match on `check`, never on `name` or `detail`: the first is a stable identifier, the other two are prose and carry the data type. `check` is `null` for findings nothing consumes programmatically yet.

**The exit code is 1 only when something is graded `fail`**, in both formats - a warning never changes it, which is the reason this flag exists: a stopped data series is a warning, so the exit code alone cannot tell you about the one failure most worth watching for.

**Check `version` before trusting an absent `check`.** A release older than this one omits the field entirely and an older one still rejects `--json` and exits 2, so "no `stopped-series` finding" and "this build cannot report one" look identical without it. `version` is itself `null` when the package is run from a source tree with no installed distribution metadata - the payload is still emitted, since a diagnostic that dies on a half-configured install is worthless exactly when it is needed.

The payload names the resolved config, database and credential paths, the same way the text report does. That is deliberate - it is what makes a wrong-path setup diagnosable - but a consumer that forwards the payload off the machine is disclosing them. No credential *values* appear in either format.

### 6. First sync (optional)

Query tools sync on first use each day, so you can skip this. To pre-populate the cache, or to pull history older than it:

```bash
google-health-mcp sync --days 30
google-health-mcp sync --since 2023-10-01     # backfill
```

## CLI usage

```
google-health-mcp                Start the MCP server (stdio transport)
google-health-mcp -V, --version  Print the installed package version
google-health-mcp auth           Interactive OAuth setup
google-health-mcp doctor         Check the setup and report what needs fixing
  --json                Emit the findings as JSON, for a monitor rather than
                        a person. Each finding carries a stable `check` name
                        to match on; the exit code is the same either way.
google-health-mcp sync           Sync data to the local cache
  --days N              Days of history for a first sync (default: 30)
  --types TYPE,...      Data types to sync (default: all). One or more of:
                        heart_rate, activity, exercises, sleep, weight, spo2,
                        hrv, azm, breathing_rate, skin_temperature,
                        core_temperature, cardio_fitness, food_log, ecg, irn
  --since YYYY-MM-DD    Fetch from this date, ignoring the incremental cursor
  --until YYYY-MM-DD    Inclusive end date for a --since window; together they
                        re-fetch exactly that window, to repair a gap in the
                        middle of the cache
google-health-mcp import         Import exported JSON data files
  --data-dir PATH       Directory containing the JSON files
```

## MCP tool reference

Query tools sync on the first query of each day per data type, then read the cache.

All query tools except `health_get_devices` and `health_get_lifetime_stats`, which take no arguments, accept:

- `start_date` - `YYYY-MM-DD`, `YYYY-MM`, or `30d` (relative). Default: last 30 days.
- `end_date` - `YYYY-MM-DD`. Default: today.
- `live` - if true, re-fetch this window from the API before reading the cache. A failed refresh is reported rather than silently answered from the cache.

`health_get_exercises` also takes `exercise_type`, a case-insensitive substring match on the workout name. `health_get_ecg` also takes `include_waveform`: a trace is thousands of voltages, so the default response carries the classification, average rate, duration and a sample count instead.

### health_sync

- `data_types` - `all`, or a comma-separated subset of the names listed under [CLI usage](#cli-usage) above (`irn` is the irregular-rhythm notifications). Default: `all`.
- `days` - days of history for a first sync (default: 30). Later syncs are incremental.
- `since` / `until` - fetch an exact window regardless of what is cached.

### health_trends

- `data_type` - any cached type with a daily series; ECG readings and rhythm alerts are episodes and have no trend. Default: `activity`.
- `period` - `weekly`, `monthly`, `quarterly`. Default: `monthly`.
- `start_date` / `end_date` - default: the last 12 months.
- `compare` - two periods, e.g. `last_30d vs previous_30d`, `2026-03 vs 2026-02`, `2026-Q1 vs 2025-Q4`. When set, `period`, `start_date` and `end_date` are ignored.

## OAuth scopes

Tick these read-only scopes on the Data Access page. All are under `https://www.googleapis.com/auth/googlehealth.`:

| Scope | Data accessed |
|-------|--------------|
| `activity_and_fitness.readonly` | Steps, distance, floors, calories, workouts, active zone minutes |
| `health_metrics_and_measurements.readonly` | Heart rate, HRV, SpO2, breathing rate, weight, body fat, temperature, VO2 max |
| `sleep.readonly` | Sleep sessions and stages |
| `nutrition.readonly` | Food and water logs |
| `ecg.readonly` | Electrocardiograms |
| `irn.readonly` | Irregular-rhythm notifications |
| `settings.readonly` | Paired devices |

`location.readonly` and `profile.readonly` are two the console offers that this package deliberately does not request, because nothing here reads either - the first is the GPS track recorded during an exercise.

**Read the list off the console, not off the published scope page** - read-only scopes exist that appear in neither Google's documentation nor the API's own discovery document, and the discovery document omits `nutrition.readonly` outright. To request fewer, tick fewer on the Data Access page and edit `GOOGLE_SCOPES` in `config.py` before authorising, which needs a source checkout rather than a `pip` or `uvx` install. A grant does not gain scopes on refresh, so widening the list later means running `auth` again.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `GOOGLE_HEALTH_MCP_CONFIG_DIR` | `~/.config/google-health-mcp/` | Directory holding the OAuth client and tokens |
| `GOOGLE_HEALTH_MCP_DB_PATH` | `~/.local/share/google-health-mcp/google_health.db` | SQLite cache |
| `GOOGLE_HEALTH_MCP_OFFLINE` | unset | If truthy (`1`, `true`, `yes`, `on`), run as a cache-only reader |

### Offline / cache-only mode

By default the server syncs on demand, so no cron job is needed. Set `GOOGLE_HEALTH_MCP_OFFLINE=1` to run as a pure reader instead:

- No credentials are required - the server never opens the token file.
- No network call is made. Auto-sync is off, and `live=True`, `health_get_devices` and `health_sync` return a clear "offline mode" message rather than reaching the API.
- Query tools serve the cache, tagged `"offline_mode": true`.

Typical uses:

- **Several machines, one cache** - one host runs `google-health-mcp sync` from cron or systemd against a shared database; the others set `GOOGLE_HEALTH_MCP_OFFLINE=1`, point `GOOGLE_HEALTH_MCP_DB_PATH` at the same file, and only read.
- **CI and privacy** - run queries with no network access and no credentials.

## Rate limits

Google applies a per-user request quota, documented at [developers.google.com/health/rate-limits](https://developers.google.com/health/rate-limits). Ordinary syncing is nowhere near it: a day's update is a handful of requests, and a measured three-year backfill of every data type was around 250. If a sync is cut short, that data type is recorded as a partial sync and the next run resumes from its cursor rather than starting over.

Querying from the cache - the default - costs no quota at all.

## Data safety

Your health data stays on your machine: this server has no backend, sends nothing anywhere, and talks only to Google's API with your own credentials.

The repository ships a pre-commit hook that refuses to commit database files, anything under `config/`, and large files; [CONTRIBUTING.md](https://github.com/partymola/google-health-mcp/blob/main/CONTRIBUTING.md) says how to install it.

## Importing existing data

If you already have health data as JSON files, from an export or a script of your own:

```bash
google-health-mcp import --data-dir /path/to/json/files/
```

Expected file names: `heart_rate.json`, `activity.json`, `exercises.json`, `sleep.json`, `weight.json`, `spo2.json`, `hrv.json`. See `src/google_health_mcp/importer.py` for the shape each one expects. Import covers those seven types; everything else arrives by `sync`.

## Contributing

See [CONTRIBUTING.md](https://github.com/partymola/google-health-mcp/blob/main/CONTRIBUTING.md) for development setup, the test workflow, and the pre-commit hook. Changes are tracked in [CHANGELOG.md](https://github.com/partymola/google-health-mcp/blob/main/CHANGELOG.md).

## License

[GPL-3.0-or-later](https://github.com/partymola/google-health-mcp/blob/main/LICENSE)
