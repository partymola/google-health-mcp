"""Sync tool: fetch data from the Google Health API and store it in the cache."""

import logging
import sqlite3
from datetime import date, timedelta

import anyio

from .. import api, auth, config, db
from ..errors import LiveRefreshFailed
from ..helpers import format_response, require_auth
from ..mcp_instance import mcp
from . import google_sync
from .google_sync import GOOGLE_SYNC_HANDLERS, HANDLER_SCOPES

logger = logging.getLogger(__name__)

#: How far behind the incremental cursor a resumed sync starts.
#:
#: The property it buys, stated so the value can be re-derived rather than
#: taken on trust: a day D is asked for again on every run from D through
#: D+INCREMENTAL_OVERLAP_DAYS, so the overlap tolerates a publication lag of
#: up to that many days. The lag is at least one day: a `daily-*` summary was
#: absent from a sync run late in its own local day and present the next
#: morning. Three is margin on top.
#:
#: A host being off needs none of this: `last_date_attempted` is written only
#: on success, so the cursor does not advance while nothing runs and the first
#: run back covers the whole gap by itself.
#:
#: Bounded rather than open-ended because that cursor exists to stop a type
#: nobody records any more - food_log, say - being re-queried from its last
#: reading on every run forever. The upsert makes the repeated days free of
#: consequence; they cost a few requests.
INCREMENTAL_OVERLAP_DAYS = 3


def run_sync(
    data_types: list[str],
    days: int = 30,
    since: str | None = None,
    until: str | None = None,
    handlers: dict | None = None,
) -> dict:
    """Run sync outside MCP context (for CLI use). Returns results dict.

    Args:
        data_types: data type names to sync.
        days: history window for a cold sync (no prior data for that type).
        since: optional "YYYY-MM-DD". When set, every type is backfilled from
            this date, overriding the incremental resume-from-last-sync cursor
            (and `days`). Use to pull history older than what is already cached.
        until: optional "YYYY-MM-DD" inclusive end date; requires `since`.
            Together they re-fetch and upsert exactly the [since, until]
            window - use to repair a hole in the middle of the cache without
            re-pulling everything from the hole to today. Capped at today.
        handlers: which function fetches each type. Defaults to the Google map;
            taken as an argument rather than read inside the loop so a caller
            can sync a subset into a copy of the database without the loop
            knowing anything about providers.
    """
    today = date.today()

    def _all_error(message: str) -> dict:
        return {dtype: {"status": "error", "message": message} for dtype in data_types}

    since_date = None
    if since:
        try:
            since_date = date.fromisoformat(since)
        except ValueError:
            return _all_error(f"Invalid since date '{since}'. Use YYYY-MM-DD.")

    until_date = None
    if until:
        try:
            until_date = date.fromisoformat(until)
        except ValueError:
            return _all_error(f"Invalid until date '{until}'. Use YYYY-MM-DD.")
        if since_date is None:
            return _all_error("'until' requires 'since' to define the backfill window.")
        if until_date < since_date:
            return _all_error(f"'until' ({until}) is before 'since' ({since}).")
        until_date = min(until_date, today)

    handlers = GOOGLE_SYNC_HANDLERS if handlers is None else handlers
    conn = db.get_db()
    results = {}

    try:
        return _run_sync_types(
            conn, data_types, results, since_date, until_date, today, days, handlers
        )
    finally:
        conn.close()


def _run_sync_types(
    conn, data_types, results, since_date, until_date, today, days, handlers, compact=True
):
    # A token from before scopes were recorded says nothing until its next
    # refresh, so one is asked for now; otherwise the first sync after an
    # upgrade fetches what the grant lacks and fails on the 403. If it cannot
    # be had, unknown is treated as held and everything is fetched as before.
    # A scope holding nothing requested may only be junk, and every type would
    # be skipped with no request left to correct it, so it gets one refresh too.
    lacking = config.missing_scopes(auth.granted_scopes())
    if not config.OFFLINE_MODE and (
        lacking is None or len(lacking) == len(config.GOOGLE_SCOPE_READERS)
    ):
        try:
            auth.refresh_google_token(force=True)
        except (auth.TokenRefused, auth.RefreshNetworkError):
            pass
    missing = set(config.missing_scopes(auth.granted_scopes()) or ())
    for dtype in data_types:
        try:
            if since_date is not None:
                # Explicit backfill: ignore the incremental cursor entirely.
                start_date = since_date
            else:
                # Use the later of (most-recent row in table) and (most-recent
                # successful sync's end-date). The second matters for sparse
                # types like food_log: if the user stops logging, the data
                # table's MAX(date) freezes and we'd otherwise re-query every
                # day from then on, burning quota on confirmed-empty days.
                candidates = [
                    d
                    for d in (
                        db.get_last_synced_date(conn, dtype),
                        db.get_last_attempted_date(conn, dtype),
                    )
                    if d
                ]
                if candidates:
                    # Behind the cursor, not at it. Google publishes a day's
                    # `daily-*` summary after the day has ended, so a run early
                    # on the next day can still find nothing for yesterday,
                    # and records that it attempted through today. Resuming
                    # from that cursor would never ask for yesterday again,
                    # leaving a permanent one-day hole behind a sync_log full
                    # of `ok`.
                    # Never subtract past date.min: a cursor within the
                    # overlap of it - which only a corrupt sync_log produces -
                    # would raise OverflowError instead of syncing.
                    cursor = date.fromisoformat(max(candidates))
                    start_date = cursor - timedelta(
                        days=min(INCREMENTAL_OVERLAP_DAYS, (cursor - date.min).days)
                    )
                else:
                    start_date = today - timedelta(days=days)
            end_date = until_date if until_date is not None else today

            handler = handlers.get(dtype)
            if handler is None:
                results[dtype] = {"status": "error", "message": f"Unknown type: {dtype}"}
                continue
            # A scope trimmed out of the request is never granted either.
            lacking = [
                s
                for s in HANDLER_SCOPES.get(dtype, ())
                if s in missing or s not in config.GOOGLE_SCOPE_READERS
            ]
            if lacking:
                # Neither a failure nor a success: `sync` exits 0 and doctor's
                # sync-log check passes, while the missing-scope note warns.
                ungranted = [s for s in lacking if s in config.GOOGLE_SCOPE_READERS]
                unrequested = [s for s in lacking if s not in config.GOOGLE_SCOPE_READERS]
                reasons = []
                if ungranted:
                    reasons.append(
                        f"permission not granted ({', '.join(ungranted)}); "
                        "run google-health-mcp auth"
                    )
                if unrequested:
                    reasons.append(
                        f"permission not requested by this install ({', '.join(unrequested)})"
                    )
                message = "; ".join(reasons)
                db.log_sync(conn, dtype, "skipped", notes=message)
                results[dtype] = {"status": "skipped", "message": message}
                continue
            count = handler(conn, start_date, end_date)

            db.log_sync(conn, dtype, "ok", count, last_date_attempted=end_date.isoformat())
            results[dtype] = {
                "status": "ok",
                "records": count,
                "range": f"{start_date} to {end_date}",
            }

        except api.HealthRateLimitError as e:
            db.log_sync(conn, dtype, "partial", notes="rate limited")
            results[dtype] = {"status": "rate_limited", "message": str(e)}
        except api.HealthAuthError as e:
            db.log_sync(conn, dtype, "auth_error", notes=str(e))
            results[dtype] = {"status": "auth_error", "message": str(e)}
        except api.HealthAPIError as e:
            db.log_sync(conn, dtype, "error", notes=str(e))
            results[dtype] = {"status": "error", "message": str(e)}
        except api.HealthOfflineError:
            raise
        except Exception as e:
            # Type only: these paths carry API responses.
            try:
                db.log_sync(conn, dtype, "error", notes=f"unexpected {type(e).__name__}")
            except Exception:
                logger.error("Could not record a failed sync of %s", dtype)
            results[dtype] = {"status": "error", "message": "Unexpected error during sync."}

    # After the types rather than before: a refresh during them is what brings
    # the token's record of its scopes up to date. An unreadable token leaves
    # the record alone rather than erasing a shortfall every host reports.
    try:
        found = config.missing_scopes(auth.granted_scopes())
        if found is not None:
            db.record_missing_scopes(conn, found)
    except Exception:
        logger.error("Could not record the grant's missing scopes")

    # Here rather than on open: every host opens the cache, and only the one
    # that syncs may rewrite it.
    if compact and not config.OFFLINE_MODE:
        _compact(conn)
    return results


def _compact(conn) -> None:
    try:
        packed = db.compact(conn)
        if packed:
            logger.info("Compressed %d stored value(s)", packed)
    except Exception as e:
        logger.warning("Could not compact the cache: %s", type(e).__name__)


def auto_sync_if_stale(*data_types: str) -> None:
    """Sync the types if any has never been synced or last synced before today.

    Several types given together are synced together whenever any is stale,
    so a reply reading a total beside its parts does not get a morning total
    beside afternoon entries from this gate. A refresh of one of them alone,
    by `live` or `health_sync`, can still leave them apart until the next day.
    The cost is deliberate: while one of them keeps failing, every query
    re-syncs the others with it.

    Failures are silently suppressed - the caller should still query the cache.
    This ensures tools work on first use without requiring an explicit health_sync call.

    No-op in offline mode (GOOGLE_HEALTH_MCP_OFFLINE): a cache-only host never syncs.
    """
    if config.OFFLINE_MODE:
        return

    try:
        conn = db.get_db()
        try:
            last_syncs = [db.get_last_sync_time(conn, t) for t in data_types]
        finally:
            conn.close()

        # `synced_at` is stored in UTC and "today" is local, so compare in local
        # time: otherwise, east of UTC, a sync made after local midnight reads
        # as yesterday's and every query re-syncs until UTC catches up.
        if all(
            last is not None and last.astimezone().date() >= date.today() for last in last_syncs
        ):
            return

        run_sync(list(data_types))
    except Exception as e:
        logger.debug("Auto-sync failed for %s: %s", ",".join(data_types), type(e).__name__)


def refresh_before_query(
    data_type: str | tuple[str, ...], start: date, end: date, live: bool
) -> None:
    """Bring the cache up to date for one type, or several together, then let the caller read it.

    `live` skips the once-a-day gate and syncs exactly the window asked for.
    It raises where auto-sync would swallow: run_sync reports failure in its
    return value rather than by raising, so a caller that ignores the result
    answers a live request out of the cache without saying so.
    """
    data_types = (data_type,) if isinstance(data_type, str) else tuple(data_type)
    if not live:
        auto_sync_if_stale(*data_types)
        return

    if config.OFFLINE_MODE:
        # The offline contract is a tagged message, not an error reaching the
        # client: require_auth answers this type with one instead of converting.
        raise api.HealthOfflineError(
            "Offline mode (GOOGLE_HEALTH_MCP_OFFLINE): live refresh is unavailable; "
            "the cache is served as-is."
        )

    result = run_sync(list(data_types), since=start.isoformat(), until=end.isoformat())
    for name in data_types:
        status = result.get(name, {}).get("status")
        if status != "ok":
            # The status word only. These paths carry API responses, and the
            # accompanying notes are not this function's to widen.
            raise LiveRefreshFailed(f"live refresh of {name} failed: {status or 'no result'}")


#: Bookkeeping columns, which a report leaves out: they hold no reading.
_BOOKKEEPING = frozenset({"provider", "google_columns"})

#: The tables whose sync already removes what Google no longer holds, under
#: its own guards: a point Google still lists is kept even when it cannot be
#: stored. A rebuild leaves removal there to the sync. A route leaves with its
#: workout, and one already held is not fetched again, so a rebuild never
#: removes one on its own.
_SWEPT_TABLES = frozenset(
    {
        "food_entries",
        "water_entries",
        "food_log",
        "core_temperature",
        "weight_readings",
        "body_fat_readings",
        "weight",
        "exercises",
        "exercise_routes",
    }
)


def _shown(row: dict, packed: str | None) -> dict:
    """A row as a report carries it: compressed text named rather than sent."""
    return {
        column: "(stored compressed)" if column == packed and value is not None else value
        for column, value in row.items()
        if column not in _BOOKKEEPING
    }


def _differences(table: str, before: list[dict], after: list[dict]) -> dict:
    """What a fetch changed among one provider's rows: gone, changed, new, the same."""
    keys, packed = db.key_columns(table), db.packed_column(table)
    after_by_key = {tuple(row[k] for k in keys): row for row in after}
    removed, changed, kept = [], [], set()
    for row in before:
        key = tuple(row[k] for k in keys)
        now = after_by_key.get(key)
        if now is None:
            removed.append(_shown(row, packed))
            continue
        kept.add(key)
        columns = {
            column: {"before": _shown(row, packed)[column], "after": _shown(now, packed)[column]}
            for column in row
            if column not in _BOOKKEEPING and row[column] != now.get(column)
        }
        if columns:
            changed.append({**{k: row[k] for k in keys}, "date": row["date"], "columns": columns})
    return {
        "removed": removed,
        "changed": changed,
        "added": len(set(after_by_key) - kept),
        "unchanged": len(kept) - len(changed),
    }


def rebuild(
    data_types: list[str],
    since: str,
    until: str | None = None,
    apply: bool = False,
    handlers: dict | None = None,
) -> dict:
    """Fetch [since, until] again from Google for each type, and say what changed.

    The dates are synced again over the rows already held, with a day either
    side, so a value an import put beside Google's stays wherever Google
    still holds the row. Then a row this provider wrote for [since, until],
    held before the fetch, that the fetch did not write again is one Google
    no longer holds, and is removed, with any imported value on it. The day
    either side is there because several types are filtered by Google on one
    clock and dated here on another, so a row on the first or last day can
    fall outside an answer for exactly those dates. On the tables whose sync
    removes what Google dropped, that sync's own removal is left to stand,
    the day either side included. A type whose fetch wrote nothing for
    [since, until] has nothing removed: an answer holding nothing this
    package could store is no evidence that every row went. Rows an import
    wrote and Google never did are not touched. The result lists each row
    that went and each that changed, with its values before and after.
    Without `apply` the work runs on an in-memory copy and the cache is left
    as it was, so the result is a preview. A type whose fetch fails has
    nothing removed. A rebuild stopped part-way leaves its dates as far as
    it got, and running it again completes them.
    """
    today = date.today()

    def _all_error(message: str) -> dict:
        types = {dtype: {"status": "error", "message": message} for dtype in data_types}
        return {"applied": False, "types": types}

    try:
        since_date = date.fromisoformat(since)
        until_date = min(date.fromisoformat(until), today) if until else today
    except (TypeError, ValueError):
        return _all_error("Dates must be YYYY-MM-DD, and since is required.")
    if until_date < since_date:
        return _all_error(f"'until' ({until_date}) is before 'since' ({since_date}).")

    handlers = GOOGLE_SYNC_HANDLERS if handlers is None else handlers
    conn = db.get_db()
    target = conn
    try:
        if not apply:
            target = sqlite3.connect(":memory:")
            conn.backup(target)
            target.row_factory = sqlite3.Row
        first, last = since_date.isoformat(), until_date.isoformat()
        fetch_start = max(since_date, date.min + timedelta(days=1)) - timedelta(days=1)
        fetch_end = min(until_date + timedelta(days=1), today)
        # The report covers what the fetch could touch, edge days included.
        seen_first, seen_last = fetch_start.isoformat(), fetch_end.isoformat()
        provider = google_sync.PROVIDER
        results, before, notes = {}, {}, {}
        for dtype in data_types:
            table = db.table_for(dtype)
            if table is None or dtype not in handlers:
                results[dtype] = {"status": "error", "message": f"Cannot rebuild {dtype}."}
                continue
            before[dtype] = db.rows_written_by(target, table, provider, seen_first, seen_last)
            db.watch_writes(target, table)
        outcome: dict = {}
        _run_sync_types(
            target,
            list(before),
            outcome,
            fetch_start,
            fetch_end,
            today,
            30,
            handlers,
            compact=False,
        )
        succeeded = [d for d in before if outcome.get(d, {}).get("status") == "ok"]
        for dtype in succeeded:
            table = db.table_for(dtype)
            if table in _SWEPT_TABLES:
                continue
            # Only rows held before the fetch: another process may have written since.
            keys = db.key_columns(table)
            held = {tuple(row[k] for k in keys) for row in before[dtype]}
            not_written = db.rows_not_written(target, table, provider, first, last)
            unwritten = [row for row in not_written if tuple(row[k] for k in keys) in held]
            stored = db.rows_written_by(target, table, provider, first, last)
            if len(stored) == len(not_written):
                # The fetch wrote nothing for these dates.
                if unwritten:
                    notes[dtype] = (
                        "Google sent nothing this type could store for these dates, "
                        "so nothing was removed."
                    )
                continue
            db.remove_rows(target, table, unwritten)
        target.commit()
        # Reported once every removal is made, so no type's report predates another's.
        for dtype in before:
            table = db.table_for(dtype)
            if dtype not in succeeded:
                results[dtype] = outcome.get(dtype, {"status": "error", "message": "No result."})
                continue
            after = db.rows_written_by(target, table, provider, seen_first, seen_last)
            results[dtype] = {
                "status": "ok",
                "range": f"{seen_first} to {seen_last}",
                **_differences(table, before[dtype], after),
                **({"note": notes[dtype]} if dtype in notes else {}),
            }
        # After the removals, so nothing rewrites a row between noting the
        # fetch's writes and reading them, and after the report, which then
        # shows no value as changed by being packed.
        if apply:
            _compact(target)
        return {"applied": apply, "types": {dtype: results[dtype] for dtype in data_types}}
    finally:
        if target is not conn:
            target.close()
        conn.close()


@mcp.tool(structured_output=False)
@require_auth
async def health_rebuild(
    since: str,
    until: str | None = None,
    data_types: str = "all",
    apply: bool = False,
) -> str:
    """Fetch a date range again from Google and report every row that would change.

    For when the cache may hold something Google no longer does, or holds it
    differently. These dates are fetched again, with a day either side, and
    a row Google sent before and no longer holds is removed. A removed row
    loses any imported value it carried, and the reply lists it with every
    value. Where Google still holds a row, an imported value beside Google's
    stays. History only an import wrote is never touched. A type whose fetch
    brings back nothing it can store keeps its rows, and says so in a note.
    A daily weight or body-fat figure synced before release 1.13.0 stays
    even after its readings are deleted. Without apply nothing is changed:
    the reply is a preview. Show the person what it lists before running it
    again with apply=True. A preview copies the cache into memory and makes
    the same requests to Google as applying does.

    Args:
        since: First date to rebuild, "YYYY-MM-DD". Required.
        until: Last date, "YYYY-MM-DD". Default: today.
        data_types: "all", or comma-separated names as health_sync takes
            them. "account" has no dates and is never rebuilt.
        apply: If true, change the cache. Default: false, a preview.

    Returns, per type, removed (each row Google no longer holds, with its
    values), changed (each row whose values differ, before and after),
    added and unchanged (counts), and the range the fetch covered. A type
    whose fetch fails has nothing removed.
    """
    if config.OFFLINE_MODE:
        return format_response(
            {
                "error": (
                    "Offline mode is on (GOOGLE_HEALTH_MCP_OFFLINE); a rebuild fetches from "
                    "Google. Run it on the host that owns the cache."
                ),
                "offline_mode": True,
            }
        )
    types = [t.strip() for t in data_types.split(",")]
    if "all" in types:
        types = [t for t in GOOGLE_SYNC_HANDLERS if db.table_for(t) is not None]
    result = await anyio.to_thread.run_sync(lambda: rebuild(types, since, until, apply=apply))
    return format_response(result)


@mcp.tool(structured_output=False)
@require_auth
async def health_sync(
    data_types: str = "all",
    days: int = 30,
    since: str | None = None,
    until: str | None = None,
) -> str:
    """Sync health data to the local cache.

    Fetches data from the Google Health API and stores it in SQLite for fast
    offline queries. Run this before using other health_get_* tools.

    Syncs incrementally: only fetches data newer than the most recent
    entry in each table. First sync fetches the specified number of days.

    Args:
        data_types: What to sync. Options: "all", "heart_rate",
            "heart_rate_zones", "activity", "exercises", "sleep", "weight",
            "spo2", "hrv", "azm",
            "breathing_rate", "skin_temperature", "core_temperature",
            "cardio_fitness", "food_log", "ecg", "irn", "account", "height",
            "exercise_routes", "sleep_sessions", "weight_readings",
            "body_fat_readings", "sedentary_periods", "altitude_gains",
            "swim_lengths", "respiratory_rate_sleep", "food_entries",
            "water_entries".
            Comma-separated for multiple, e.g. "sleep,hrv". Default: "all".
        days: Days of history for first sync (default: 30). Ignored
            on subsequent syncs (uses last synced date).
        since: Optional "YYYY-MM-DD" backfill date. When set, fetches from this
            date regardless of what is already cached - use to pull history
            older than the current cache. Overrides incremental resume and days.
        until: Optional "YYYY-MM-DD" inclusive end date; requires since.
            Together they re-fetch and upsert exactly the since..until window -
            use to repair a gap in the middle of the cache without re-pulling
            everything from the gap to today.

    Returns summary of records synced per data type; for the day-at-a-time
    types (sedentary_periods, altitude_gains, swim_lengths,
    respiratory_rate_sleep, food_entries, water_entries) the count is of days.
    Not for querying data - use health_get_heart_rate, health_get_activity,
    health_get_sleep, etc. instead.
    """
    if config.OFFLINE_MODE:
        return format_response(
            {
                "error": (
                    "Offline mode is on (GOOGLE_HEALTH_MCP_OFFLINE); syncing is disabled. "
                    "Run the sync on the host that owns the cache, or unset "
                    "GOOGLE_HEALTH_MCP_OFFLINE."
                ),
                "offline_mode": True,
            }
        )

    types = [t.strip() for t in data_types.split(",")]
    if "all" in types:
        types = list(GOOGLE_SYNC_HANDLERS)

    results = await anyio.to_thread.run_sync(
        lambda: run_sync(types, days, since=since, until=until)
    )
    return format_response(results)
