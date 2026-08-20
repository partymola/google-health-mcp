"""Preflight diagnostic: report what is wrong with a setup, and how to fix it.

Every check here is offline and read-only. Two consequences are load-bearing
rather than incidental:

- Nothing is opened through `db.get_db()`, which creates the database if it
  is absent. Doing so would manufacture an empty DB at the resolved path and
  destroy the evidence for the misconfigured-path and stale-cache checks - the
  two this command exists for. The database is opened read-only throughout.
- No credential value is ever placed in a finding. Output is meant to be
  pasted into a bug report, so files are described by shape - present, parseable,
  which fields are set - and never by content.

Live validation (token introspection, scope diffing) is deliberately absent: it
spends API quota, and on a shared credential file a refresh triggered by a
diagnostic can rotate the token and break the host that legitimately owns it.
"""

import json
import os
import socket
import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import quote

from . import config, db

OK = "ok"
WARN = "warn"
FAIL = "fail"

_SEVERITY_MARK = {OK: "ok  ", WARN: "warn", FAIL: "FAIL"}

# Machine-stable check identifiers. A monitor outside this package matches on
# these, so they are part of the interface: adding one is free, changing one is
# a breaking change that fails silently at the consumer.
STOPPED_SERIES = "stopped-series"

# Google refresh tokens expire after six months of disuse. The file is
# rewritten on every access-token refresh, so an untouched one means nothing
# has authorised here since - worth saying before the user finds out mid-sync.
_REFRESH_TOKEN_LIFETIME = timedelta(days=180)

# Every Google OAuth client id ends this way, so a client id that does not is
# not one - and a token exchange with it cannot succeed.
_GOOGLE_CLIENT_ID_SUFFIX = ".apps.googleusercontent.com"

# Columns db._migrate() adds to an older database on the next open. Keep in
# step with it: listing one here that it does not add hides a real fault.
# Derived, not restated: a column db._migrate() repairs is one this check must
# excuse, and two hand-kept lists would drift. The lockstep test still probes
# what _migrate actually does rather than trusting the declaration.
_SELF_HEALING_COLUMNS = {(table, column) for table, column, _ in db.MIGRATIONS}

# A cache older than this has stopped being updated rather than merely lagging.
# Auto-sync refreshes each type at most once a day, so three days without a new
# row is past what a missed run or a day off the wrist explains.
_CACHE_STALE_AFTER = timedelta(days=3)


@dataclass
class Finding:
    name: str
    severity: str
    detail: str
    fix: str | None = None
    # A stable identifier for machine consumers, emitted by `doctor --json`.
    # `name` is prose meant for a person and carries the data type where there
    # is one, so it is not something a script can match on; this is. Set it on
    # any finding a program needs to recognise, and never change one that has a
    # consumer - renaming it silently breaks them, since a missing key reads as
    # "that condition is absent" rather than as an error.
    check: str | None = None


def _open_db_readonly(path: Path) -> closing:
    """Open the database with no possibility of creating or altering it.

    The path is percent-encoded because this is a URI, not a filename: an
    unescaped `#` would start a fragment, so `?mode=ro` would land inside it
    and be silently ignored - handing back a writable connection to a
    truncated path. `quote` leaves `/` alone and escapes `#` and `?`.

    Encoding via `os.fsencode` rather than passing the str: a filename holding
    non-UTF-8 bytes arrives as surrogate escapes, which `quote` refuses. Going
    through bytes round-trips those unchanged. `Path.as_uri()` is not usable
    at all here - it rejects relative paths, and GOOGLE_HEALTH_MCP_DB_PATH may be one.
    """
    conn = sqlite3.connect(f"file:{quote(os.fsencode(path))}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return closing(conn)


def _reference_schema() -> dict[str, set[str]]:
    """Tables and columns this version expects, read from db.SCHEMA itself.

    Built by running the real schema into an in-memory database rather than
    parsing SQL or restating a column list, so it cannot drift from the code.
    """
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(db.SCHEMA)
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {t: {r[1] for r in conn.execute(f"PRAGMA table_info('{t}')")} for t in tables}
    finally:
        conn.close()


def _describe_path_source(env_var: str) -> str:
    """Say where a path came from, matching how config.py actually resolves it.

    config.py reads the variable with a plain `os.environ.get`, so an empty
    value is still an override - it resolves to a path relative to the working
    directory. Testing truthiness here would report that as the default and
    send the reader looking for the wrong fault.
    """
    if env_var not in os.environ:
        return "default"
    if not os.environ[env_var]:
        return f"${env_var} is set but empty, so the path resolves under the working directory"
    return f"from ${env_var}"


def _timestamp_or_none(value) -> datetime | None:
    """Convert an epoch-seconds value, or None if it is not one.

    Out-of-range values raise rather than clamp, and they are not exotic - a
    token written with milliseconds instead of seconds lands tens of thousands
    of years out. A diagnostic must report that, not die on it.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value)
    except (ValueError, OverflowError, OSError):
        return None


def check_environment() -> list[Finding]:
    findings = [
        Finding(
            "config path",
            OK,
            f"{config.CONFIG_DIR} ({_describe_path_source('GOOGLE_HEALTH_MCP_CONFIG_DIR')})",
        ),
        Finding(
            "database path",
            OK,
            f"{config.DB_PATH} ({_describe_path_source('GOOGLE_HEALTH_MCP_DB_PATH')})",
        ),
    ]

    # Warn only on a value that is neither on nor off - "0" and "false" mean
    # off and are not mistakes, and an empty value is the systemd idiom for
    # blanking a variable. Flagging those would report correct usage as broken.
    raw_offline = (os.environ.get("GOOGLE_HEALTH_MCP_OFFLINE") or "").strip().lower()
    recognised = config.OFFLINE_TRUTHY_VALUES + config.OFFLINE_FALSY_VALUES
    if raw_offline and raw_offline not in recognised:
        findings.append(
            Finding(
                "offline mode",
                WARN,
                "GOOGLE_HEALTH_MCP_OFFLINE is set to an unrecognised value and so parses "
                "as OFF; this host will try live API calls. Accepted values: "
                f"{', '.join(recognised)}.",
                "Correct the value, or unset it if live access is intended.",
            )
        )
    else:
        findings.append(
            Finding("offline mode", OK, "on (cache only)" if config.OFFLINE_MODE else "off (live)")
        )

    return findings


def _check_client_file() -> list[Finding]:
    path = config.GOOGLE_CLIENT_PATH
    if not path.exists():
        return [
            Finding(
                "app config",
                FAIL,
                f"No google_client.json at {path}.",
                "Create an OAuth client of type Desktop app in the Google Cloud console, "
                "save its downloaded JSON there unedited, then run `google-health-mcp auth`. "
                "If you set the install up with GOOGLE_HEALTH_MCP_CONFIG_DIR set - a systemd "
                "unit does this - export the same value before running this command.",
            )
        ]

    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return [
            Finding(
                "app config",
                FAIL,
                "google_client.json is unreadable or malformed.",
                "Replace it with the JSON downloaded from the Google Cloud console, unedited.",
            )
        ]

    installed = data.get("installed") if isinstance(data, dict) else None
    client_id = installed.get("client_id") if isinstance(installed, dict) else None
    if not client_id or not isinstance(client_id, str):
        # A bare top-level client_id is the shape of a hand-written file, and
        # of every other OAuth client's JSON: worth naming, since the reader
        # is looking at something that does contain a client id.
        bare = isinstance(data, dict) and data.get("client_id")
        return [
            Finding(
                "app config",
                FAIL,
                "google_client.json is not a Google OAuth Desktop client file."
                + (
                    " It carries a client id at the top level and no `installed` object, "
                    "so it is not the file the console downloads."
                    if bare
                    else ""
                ),
                "Create an OAuth client of type Desktop app in the Google Cloud console "
                "and save its downloaded JSON there unedited. A Web client only works if "
                "you register the redirect URI yourself, so it fails at consent with the "
                "cause out of sight.",
            )
        ]

    if not client_id.endswith(_GOOGLE_CLIENT_ID_SUFFIX):
        return [
            Finding(
                "app config",
                FAIL,
                f"client_id does not end in {_GOOGLE_CLIENT_ID_SUFFIX}, so it is not a "
                "Google OAuth client id and no token exchange can succeed.",
                "Use the client id from a Google Cloud OAuth client of type Desktop app.",
            )
        ]

    if not installed.get("client_secret"):
        return [
            Finding(
                "app config",
                FAIL,
                "google_client.json has a client_id and no client_secret, so every "
                "token exchange will be refused as an unknown client.",
                "Re-download the client JSON from the Google Cloud console and save it unedited.",
            )
        ]

    return [Finding("app config", OK, "Google OAuth Desktop client id present")]


def _check_token_file() -> list[Finding]:
    path = config.GOOGLE_TOKENS_PATH
    if not path.exists():
        return [
            Finding(
                "credentials",
                FAIL,
                f"No google_tokens.json at {path}.",
                "Run `google-health-mcp auth`.",
            )
        ]

    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return [
            Finding(
                "credentials",
                FAIL,
                "google_tokens.json is unreadable or malformed.",
                "Re-run `google-health-mcp auth`.",
            )
        ]

    if not isinstance(data, dict):
        return [
            Finding(
                "credentials",
                FAIL,
                "google_tokens.json is malformed: expected an object.",
                "Re-run `google-health-mcp auth`.",
            )
        ]

    findings = []
    missing = [k for k in ("access_token", "refresh_token") if not data.get(k)]
    if missing:
        findings.append(
            Finding(
                "credentials",
                FAIL,
                f"google_tokens.json is missing: {', '.join(missing)}.",
                "Re-run `google-health-mcp auth`. If it still reports no refresh token, remove "
                "the app's access at myaccount.google.com/permissions first and "
                "authorise again.",
            )
        )
    else:
        findings.append(Finding("credentials", OK, "access and refresh tokens present"))

    expires_at = data.get("expires_at")
    when = _timestamp_or_none(expires_at)
    if when is not None:
        if expires_at < time.time():
            findings.append(
                Finding(
                    "access token",
                    OK,
                    f"expired at {when:%Y-%m-%d %H:%M} - normal between syncs; "
                    "it is refreshed on the next call.",
                )
            )
        else:
            findings.append(Finding("access token", OK, f"valid until {when:%Y-%m-%d %H:%M}"))
    else:
        findings.append(
            Finding(
                "access token",
                WARN,
                "expires_at is missing, not a number, or not a usable timestamp, "
                "so expiry cannot be checked. A value in milliseconds rather than "
                "seconds does this.",
                "Re-run `google-health-mcp auth`.",
            )
        )

    findings.extend(_check_token_file_writability(path))
    if not missing:
        # Both read the refresh token's state, and there is none to describe:
        # "no 7-day expiry marker" beside "missing: refresh_token" reassures
        # about a credential that is not there.
        findings.extend(_check_short_lived_refresh_token(data))
        findings.extend(_check_refresh_token_age(path))
    return findings


def _check_token_file_writability(path: Path) -> list[Finding]:
    """An unwritable token file fails every refresh after the network call.

    Google does not rotate the refresh token, so the credential itself
    survives - but the new access token cannot be stored, the save raises,
    and the sync that triggered it fails. Every subsequent one does too.

    Only the file is checked. auth.py rewrites it in place with O_WRONLY|
    O_TRUNC, which needs no write permission on the directory, so a read-only
    config dir holding a writable token file works and must not be reported.
    """
    if os.access(path, os.W_OK):
        return []
    return [
        Finding(
            "credentials",
            FAIL,
            "google_tokens.json is not writable by this user, so every access-token "
            "refresh will succeed at Google and then fail to save.",
            "Fix ownership/permissions before the next sync runs.",
        )
    ]


def _check_short_lived_refresh_token(data: dict) -> list[Finding]:
    """The failure that arrives a week later with nothing to point at.

    Google grants a refresh token expiring in 7 days while an OAuth app's
    publishing status is Testing. What is reported here is the expiry the
    grant itself recorded and what it means for syncing - not a diagnosis of
    the user's Cloud project, which no local file can establish: the field
    behind it appears in none of Google's published references.

    Anything shorter than the six-month disuse window is short-lived and
    worth saying; the boundary is Google's own rather than a chosen one.
    """
    when = _timestamp_or_none(data.get("refresh_token_expires_at"))
    if when is None or when > datetime.now() + _REFRESH_TOKEN_LIFETIME:
        return []

    fix = (
        "Check the publishing status in the Google Cloud console - the Audience page "
        "can read 'In production' while the token server disagrees, so trust the "
        "verification status on the Branding page. If it is in Testing, use Publish "
        "app, then run `google-health-mcp auth` to replace this token."
    )
    if when < datetime.now():
        return [
            Finding(
                "refresh token",
                FAIL,
                f"This refresh token recorded an expiry of {when:%Y-%m-%d %H:%M} and is "
                "past it, so nothing can refresh an access token any more.",
                fix,
            )
        ]
    return [
        Finding(
            "refresh token",
            FAIL,
            f"This refresh token records an expiry of {when:%Y-%m-%d %H:%M}, after which "
            "syncing stops with no other warning. Google grants a 7-day expiry while an "
            "app's publishing status is Testing.",
            fix,
        )
    ]


def _check_refresh_token_age(path: Path) -> list[Finding]:
    """How long the credential has gone unused, from the file's own mtime.

    Every access-token refresh rewrites the file, so an untouched one means
    nothing has authorised on this host since.
    """
    try:
        written = _timestamp_or_none(path.stat().st_mtime)
    except OSError:
        return []
    if written is None:
        return []

    age = datetime.now() - written
    if age < _REFRESH_TOKEN_LIFETIME:
        # What this check looked at, not a verdict on the token: the expiry
        # above is a separate finding under the same name, and a line summing
        # both would contradict it whenever one fires.
        return [Finding("refresh token", OK, f"file rewritten {age.days} day(s) ago")]
    return [
        Finding(
            "refresh token",
            WARN,
            f"Token file has not been rewritten for {age.days} days; Google refresh "
            "tokens expire after six months without use.",
            "Run `google-health-mcp auth` if syncs are failing.",
        )
    ]


def check_credentials() -> list[Finding]:
    if config.OFFLINE_MODE:
        return [Finding("credentials", OK, "not required in offline mode")]
    return _check_client_file() + _check_token_file()


def check_database() -> list[Finding]:
    path = config.DB_PATH
    if not path.exists():
        severity = FAIL if config.OFFLINE_MODE else WARN
        return [
            Finding(
                "database",
                severity,
                f"No database at {path}."
                + (
                    " Offline mode serves the cache only, so there is nothing to read."
                    if config.OFFLINE_MODE
                    else " It is created on the first sync."
                ),
                "Check GOOGLE_HEALTH_MCP_DB_PATH points at the database the syncing host writes."
                if config.OFFLINE_MODE
                else "Run `google-health-mcp sync` to populate it.",
            )
        ]

    # is_file() rather than not is_dir(): a FIFO passes the directory test and
    # then blocks the open forever, and a diagnostic that hangs is worse than
    # one that reports.
    if not path.is_file():
        return [
            Finding("database", FAIL, f"{path} is not a regular file, so it cannot be a database.")
        ]

    findings = []
    if not os.access(path, os.R_OK):
        # Reported before opening, because the open fails with the same error
        # SQLite raises for a corrupt file - and that path recommends deleting
        # the database, which would be catastrophic advice here.
        return [
            Finding(
                "database",
                FAIL,
                "Database exists but is not readable by this user. Nothing can be "
                "queried, and this says nothing about whether its contents are sound.",
                "Fix ownership/permissions - do not delete it.",
            )
        ]

    # A read-only cache is the documented multi-host arrangement, not a fault:
    # one host syncs and the rest read. Only worth reporting where this host is
    # expected to write. SQLite writes its rollback journal beside the database,
    # so a writable file in a read-only directory still fails every sync -
    # checking only the file would miss the case this finding describes.
    if not config.OFFLINE_MODE and (
        not os.access(path, os.W_OK) or not os.access(path.parent, os.W_OK)
    ):
        findings.append(
            Finding(
                "database",
                WARN,
                "Database cannot be written by this user; queries will work but "
                "every sync will fail. SQLite needs to write both the database file "
                "and a journal in its directory.",
                "Fix ownership/permissions on the file and its directory, or run syncs "
                "as the owning user.",
            )
        )

    try:
        with _open_db_readonly(path) as conn:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                return findings + [
                    Finding(
                        "database",
                        FAIL,
                        "Database fails its integrity check.",
                        "Restore from backup, or delete it and re-sync.",
                    )
                ]
            findings.extend(_check_schema(conn))
            # Read once and shared: both checks judge the same map, and
            # building it is a MAX(date) per cached type.
            newest = _newest_per_type(conn)
            findings.extend(_check_freshness(conn, newest))
            findings.extend(_check_stopped_series(conn, newest))
    except sqlite3.OperationalError:
        # Split from DatabaseError below on purpose. SQLite raises this when it
        # cannot open or read the file - locked by a sync running now, or a hot
        # journal from one that crashed - which says nothing about the contents.
        # Sharing the corruption branch would tell a user with a healthy
        # database to delete it.
        return findings + [
            Finding(
                "database",
                FAIL,
                f"{path} could not be opened. It may be locked by a sync running now, "
                "or left mid-recovery by one that crashed. Its contents are not "
                "implicated.",
                "Retry once any sync has finished, and check ownership/permissions.",
            )
        ]
    except sqlite3.DatabaseError:
        return findings + [
            Finding(
                "database",
                FAIL,
                f"{path} is not a readable SQLite database.",
                "Restore from backup, or delete it and re-sync.",
            )
        ]

    return findings


def _resync_advice(action: str) -> str:
    """Remediation for a cache problem, correct for this host's mode.

    cli.py refuses `sync` in offline mode, so advising it there sends the user
    to a command that exits 1. The fault belongs to whichever host does sync.
    """
    if config.OFFLINE_MODE:
        return (
            "This host is cache-only, so run the sync on the host that owns the "
            "database; nothing here will change it."
        )
    return action


def _check_schema(conn: sqlite3.Connection) -> list[Finding]:
    """Report only schema drift that the next ordinary command will NOT repair.

    A whole missing table is recreated by db.get_db()'s `CREATE TABLE IF NOT
    EXISTS`, and any column in _SELF_HEALING_COLUMNS is restored by
    db._migrate() on the next open. Neither is a problem for the user, and
    reporting one would attach the remediation below - destroying and
    rebuilding a cache that needed nothing. A column missing anywhere else
    has nothing to restore it and does break its sync.

    _SELF_HEALING_COLUMNS is empty while `db.MIGRATIONS` is, which is the
    state of a first release: every column gap is then a real fault.
    """
    expected = _reference_schema()
    present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    absent, problems = [], []
    for table, columns in sorted(expected.items()):
        if table not in present:
            absent.append(table)
            continue
        actual = {r[1] for r in conn.execute(f"PRAGMA table_info('{table}')")}
        missing = {c for c in columns - actual if (table, c) not in _SELF_HEALING_COLUMNS}
        if missing:
            problems.append(f"{table}.{'/'.join(sorted(missing))}")

    findings = []
    if problems:
        findings.append(
            Finding(
                "schema",
                FAIL,
                f"The database is missing columns nothing will restore: "
                f"{', '.join(problems)}. Syncing these types will fail.",
                "Back the database up, then re-create and re-import it.",
            )
        )
    if absent:
        # Recreated empty by get_db()'s CREATE TABLE IF NOT EXISTS, so this is
        # not a fault to remedy - but it is not "matches this version" either,
        # and any history those tables held is gone.
        findings.append(
            Finding(
                "schema",
                WARN,
                f"{len(absent)} table(s) absent: {', '.join(absent)}. They are "
                "recreated empty on the next open, so any history in them is lost.",
                _resync_advice("Re-sync to refill them."),
            )
        )
    if not findings:
        findings.append(Finding("schema", OK, "matches this version"))
    return findings


def _newest_per_type(conn: sqlite3.Connection) -> dict[str, str]:
    newest = {}
    for data_type in config.CACHED_DATA_TYPES:
        try:
            row = conn.execute(f"SELECT MAX(date) FROM '{data_type}'").fetchone()
        except sqlite3.DatabaseError:
            continue
        if row and row[0]:
            newest[data_type] = row[0]
    return newest


#: Windows for judging that one series has stopped, in days back from the
#: cache's own newest date - not from today, which keeps this orthogonal to
#: `_check_freshness`: a whole cache going stale is that check's job, and one
#: type dying while the rest continue is this one's.
#: Fourteen because a multi-day gap is a real pattern, not a fault, and a
#: window a normal gap can fill is a check people learn to ignore. Measured
#: over a real three-year cache: the longest gap in the last 400 days is 11
#: days (skin temperature and SpO2), so fourteen carries a three-day margin.
#: Weight's longest is 12, retained only as corroboration: it is excluded
#: below, so its gaps can never reach this check. Older history
#: on the same account holds 19 and 20-day gaps in HRV and breathing rate,
#: which this window would have reported; that is the accepted cost of
#: detecting a real stop within a fortnight rather than a month.
_STOPPED_RECENT_DAYS = 14
_STOPPED_PRIOR_DAYS = 30
#: What makes a series established enough to judge. Density, not age, is what
#: separates a series that stopped from one that was always sporadic - which
#: is the distinction `_check_freshness` declines to make and the reason it
#: judges every type together.
_ESTABLISHED_DAYS = 15

#: Types a person fills by hand rather than a device filling them. Density
#: tells a stopped machine series from a sporadic one, but for these it
#: measures a habit, and a habit ending is not a fault - a month of logged
#: runs followed by a month off would otherwise be reported. Measured: on a
#: real account `exercises` has rows on 483 of 1042 days, easily dense enough
#: to be judged. Naming them means adding a type forces the decision.
#:
#: `weight` is the arguable one and the trade is deliberate: at 65% density on
#: a real account it is a smart scale rather than hand entry, and the density
#: guard alone already protects a sporadic weigher. What the exclusion buys is
#: the daily weigher who goes on holiday; what it costs is that a broken
#: weight sync is invisible here. The false positive was judged worse.
_USER_LOGGED_TYPES = frozenset({"exercises", "food_log", "weight", "core_temperature"})


def _days_with_rows(conn: sqlite3.Connection, data_type: str, first: date, last: date) -> int:
    try:
        row = conn.execute(
            f"SELECT COUNT(DISTINCT date) FROM '{data_type}' WHERE date >= ? AND date <= ?",
            (first.isoformat(), last.isoformat()),
        ).fetchone()
    except sqlite3.DatabaseError:
        return 0
    return row[0] if row else 0


def _check_stopped_series(conn: sqlite3.Connection, newest: dict[str, str]) -> list[Finding]:
    """A type that filled most days and now fills none, while others carry on.

    This is the shape of the failure nothing else here can see. A response
    field renamed upstream makes every page parse to nothing while the request
    still succeeds, so the table stops filling, `run_sync` records `ok` with
    zero rows, and `_check_freshness` reads the other types' dates and calls
    the cache current. The same shape covers a scope narrowed for one family
    and a type withdrawn by the provider.

    Reported as a warning, never a failure: a series can also stop because the
    user stopped wearing the watch, and a diagnostic that exits non-zero on
    that is one people learn to ignore.

    Known limit, so nobody "fixes" it into something worse: a series that
    thins out before it dies is not reported, because the density window ends
    at the last row and a sparse tail masks the dense body. Every wider
    measure - the best 30-day window, density over the whole span - reopens
    the user-behaviour false positives `_USER_LOGGED_TYPES` exists to close.
    """
    if not newest:
        return []
    try:
        reference = datetime.strptime(max(newest.values()), "%Y-%m-%d").date()
    except ValueError:
        # A malformed date sorts high; the freshness check reports it, and
        # judging windows against it would be arithmetic on nonsense.
        return []

    recent_first = reference - timedelta(days=_STOPPED_RECENT_DAYS - 1)

    findings = []
    for data_type in sorted(newest):
        if data_type in _USER_LOGGED_TYPES:
            continue
        if _days_with_rows(conn, data_type, recent_first, reference):
            continue
        try:
            stopped_at = datetime.strptime(newest[data_type], "%Y-%m-%d").date()
        except ValueError:
            continue
        # Density is measured over the days before this type's OWN last row,
        # not before the recent window. Anchored to the shared reference, a
        # series dead for longer than the two windows together has no rows in
        # either, reads as never established, and is skipped - so the check
        # went blind about six weeks after the failure it exists to catch,
        # which is the state it would spend most of its life in.
        established = _days_with_rows(
            conn, data_type, stopped_at - timedelta(days=_STOPPED_PRIOR_DAYS - 1), stopped_at
        )
        if established < _ESTABLISHED_DAYS:
            continue
        findings.append(
            Finding(
                f"{data_type} series",
                WARN,
                f"No {data_type} since {newest[data_type]}, after rows on {established} "
                f"of the {_STOPPED_PRIOR_DAYS} days before that.",
                f"Re-sync that type alone (`google-health-mcp sync --types {data_type} "
                f"--since {newest[data_type]}`); if it stays empty, the data may have "
                "stopped at the source.",
                check=STOPPED_SERIES,
            )
        )
    return findings


def _check_freshness(
    conn: sqlite3.Connection, newest: dict[str, str] | None = None
) -> list[Finding]:
    if newest is None:
        newest = _newest_per_type(conn)

    if not newest:
        return [
            Finding(
                "cache",
                WARN,
                "Database has no data in any table.",
                _resync_advice("Run `google-health-mcp sync --days 30`."),
            )
        ]

    latest = max(newest.values())
    summary = f"{len(newest)} data types cached, newest date {latest}"

    # Staleness is judged on the newest row across all types, not per type.
    # Types only written when the user logs something - weight, food - lag by
    # design, and flagging those individually would cry wolf on a healthy cache.
    # Whole days between two dates. Measuring from `datetime.now()` instead
    # would fold in the time of day, so the same cache read stale at 23:00 and
    # fresh at 01:00.
    try:
        age = date.today() - datetime.strptime(latest, "%Y-%m-%d").date()
    except ValueError:
        # Dates are stored as the API returned them, so a malformed one sorts
        # high and would otherwise mark any cache current, however old.
        return [
            Finding(
                "cache",
                WARN,
                f"{summary} - that date is not a calendar date, so freshness cannot be judged.",
                _resync_advice("Re-sync to overwrite it."),
            )
        ]

    if age <= _CACHE_STALE_AFTER:
        return [Finding("cache", OK, summary)]
    return [
        Finding(
            "cache",
            WARN,
            f"{summary} - {age.days} days old, so syncing has stopped.",
            "Check the sync log below. In offline mode the cause is on the "
            "syncing host, not this one.",
        )
    ]


def check_sync_health() -> list[Finding]:
    """Read the sync log for failures that never surfaced anywhere else.

    Auto-sync suppresses its exceptions by design so that a read still serves
    the cache; the cost is that a dead token produces no visible error. The
    log is the only in-band record that this is happening.
    """
    path = config.DB_PATH
    if not path.exists():
        return []

    try:
        with _open_db_readonly(path) as conn:
            # The latest attempt per type is what says whether it is broken
            # NOW. Counting failures over a window instead would keep reporting
            # a problem that was fixed weeks ago, until enough rows aged out.
            rows = conn.execute(
                "SELECT data_type, status, notes, synced_at FROM sync_log s "
                "WHERE synced_at = (SELECT MAX(synced_at) FROM sync_log "
                "                   WHERE data_type = s.data_type)"
            ).fetchall()
    except sqlite3.DatabaseError:
        return []

    if not rows:
        return []

    failing = [r for r in rows if r["status"] in ("auth_error", "error")]
    if not failing:
        if any(r["status"] == "partial" and (r["notes"] or "") == "rate limited" for r in rows):
            return [
                Finding(
                    "sync log",
                    WARN,
                    "The last sync was cut short by Google's per-user request quota.",
                    "It resumes from its cursor on the next run; use --types to sync "
                    "fewer types at once.",
                )
            ]
        return [Finding("sync log", OK, "no failures recorded")]

    types = ", ".join(sorted(r["data_type"] for r in failing))
    auth = [r for r in failing if r["status"] == "auth_error"]
    detail = (
        f"Last sync failed for: {types} ({'auth' if auth else 'error'}, "
        f"most recent {max(r['synced_at'] for r in failing)}). Auto-sync "
        "suppresses these, so queries keep serving stale cache silently."
    )

    if config.OFFLINE_MODE:
        # This host does not sync; the log belongs to whichever host does. Only
        # `sync` is refused offline - `auth` runs - but neither helps from here.
        return [
            Finding(
                "sync log",
                WARN,
                detail + " This host is cache-only, so the fault is on the syncing host.",
                "Fix it there; nothing on this host will change it.",
            )
        ]

    return [
        Finding(
            "sync log",
            FAIL if auth else WARN,
            detail,
            "Run `google-health-mcp auth` if this is an auth failure, then "
            "`google-health-mcp sync --since <first missing date>` to backfill.",
        )
    ]


def check_auth_prerequisites() -> list[Finding]:
    """Things that break `google-health-mcp auth` itself, checked before it is needed."""
    if config.OFFLINE_MODE:
        return []

    findings = []
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        # Here SO_REUSEADDR only means "ignore a TIME_WAIT socket left by the
        # last auth run". Windows reads it as permission to bind over a live
        # listener that asked for reuse itself, so with it set the check could
        # never see one. Without it a recently closed auth run reads as busy
        # for a few minutes, which on a WARN is the better half of the trade -
        # the same trade `auth`'s own listener makes.
        if sys.platform != "win32":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("localhost", config.GOOGLE_CALLBACK_PORT))
        except OSError:
            findings.append(
                Finding(
                    "auth callback",
                    WARN,
                    f"Port {config.GOOGLE_CALLBACK_PORT} appears to be in use, so "
                    "`google-health-mcp auth` cannot receive the OAuth callback.",
                    "Free the port before authorising. A Desktop client registers no "
                    "redirect URI, so nothing at Google holds this number - it is fixed "
                    "by this package and the flow cannot use another. On Windows, if "
                    "nothing is listening, a socket from a recent `google-health-mcp "
                    "auth` may still be closing - retry in a few minutes.",
                )
            )

    # DISPLAY/WAYLAND_DISPLAY are X11/Wayland only; macOS and Windows open a
    # browser without either, so testing them alone flags every mac as headless.
    headless = sys.platform not in ("darwin", "win32") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    )
    if headless:
        findings.append(
            Finding(
                "auth browser",
                WARN,
                "No display detected, so `google-health-mcp auth` cannot open a browser and "
                "the callback must still reach this host.",
                f"Authorise over a tunnel: ssh -L {config.GOOGLE_CALLBACK_PORT}:"
                f"localhost:{config.GOOGLE_CALLBACK_PORT} <user>@<host>",
            )
        )

    return findings


def run_checks() -> list[Finding]:
    findings = []
    for check in (
        check_environment,
        check_credentials,
        check_database,
        check_sync_health,
        check_auth_prerequisites,
    ):
        try:
            findings.extend(check())
        except Exception as e:
            # A diagnostic that dies on a broken setup is worthless precisely
            # when it is needed, so no single check may end the run. Only the
            # exception type is reported: these checks read files containing
            # tokens, and a message built from file content could carry one.
            findings.append(
                Finding(
                    check.__name__,
                    FAIL,
                    f"This check could not run ({type(e).__name__}).",
                    "Please report this as a bug.",
                )
            )
    return findings


def format_report(findings: list[Finding]) -> str:
    lines = []
    for f in findings:
        lines.append(f"[{_SEVERITY_MARK[f.severity]}] {f.name}: {f.detail}")
        if f.fix and f.severity != OK:
            lines.append(f"         -> {f.fix}")

    failures = sum(1 for f in findings if f.severity == FAIL)
    warnings = sum(1 for f in findings if f.severity == WARN)
    lines.append("")
    if failures:
        lines.append(f"{failures} problem(s) need fixing, {warnings} warning(s).")
    elif warnings:
        lines.append(f"No blocking problems, {warnings} warning(s).")
    else:
        lines.append("All checks passed.")
    return "\n".join(lines)


def _package_version() -> str | None:
    """The installed distribution's version, or None when there is none.

    Run from a source tree with no installed metadata, `version()` raises. The
    text report survives that because `run_checks` catches broadly; this path
    sits outside it, so an unguarded lookup would make `--json` the one mode
    that dies on a half-broken install - which is the setup a diagnostic exists
    to describe.
    """
    try:
        return version("google-health-mcp")
    except PackageNotFoundError:
        return None


def format_json(findings: list[Finding]) -> str:
    """The same findings as machine-readable JSON.

    `check` is the field a program should match on; it is null for findings
    nothing consumes yet. Severities are the same three strings the text report
    grades on, so a consumer never has to parse prose.
    """
    return json.dumps(
        {
            # The build that produced this payload. A consumer cannot otherwise
            # tell "no stopped series" from "this build predates the slug": an
            # older release has no `check` field at all, and one older still
            # rejects `--json` outright. Absence of a key is not absence of a
            # condition, and only the version distinguishes them.
            "version": _package_version(),
            "findings": [
                {
                    "check": f.check,
                    "name": f.name,
                    "severity": f.severity,
                    "detail": f.detail,
                    "fix": f.fix,
                }
                for f in findings
            ],
            "counts": {
                severity: sum(1 for f in findings if f.severity == severity)
                for severity in (OK, WARN, FAIL)
            },
        },
        indent=2,
    )


def run_doctor(*, as_json: bool = False) -> int:
    """Report on the setup. Exit 1 on FAIL only, whichever format is asked for.

    A warning must not change the exit code: `--json` exists so a consumer can
    act on a WARN it cares about, and grading warnings as failures here would
    make every caller that only checks the status treat them as blocking.
    """
    findings = run_checks()
    print(format_json(findings) if as_json else format_report(findings))
    return 1 if any(f.severity == FAIL for f in findings) else 0
