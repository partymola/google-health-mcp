"""Sync tool: fetch data from the Google Health API and store it in the cache."""

import logging
from datetime import date, timedelta

import anyio

from .. import api, config, db
from ..errors import LiveRefreshFailed
from ..helpers import format_response, require_auth
from ..mcp_instance import mcp
from .google_sync import GOOGLE_SYNC_HANDLERS

logger = logging.getLogger(__name__)

#: How far behind the incremental cursor a resumed sync starts.
#:
#: The property it buys, stated so the value can be re-derived rather than
#: taken on trust: a day D is asked for again on every run from D through
#: D+INCREMENTAL_OVERLAP_DAYS, so the overlap tolerates a publication lag of
#: up to that many days. Measured 19 Aug 2026, the lag is at least one -
#: `daily-*` summaries for that day were absent from a sync run late in the
#: local day and present the next morning - and three is margin on top.
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


def _run_sync_types(conn, data_types, results, since_date, until_date, today, days, handlers):
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
                    # `daily-*` summary after the day has ended, so a sync
                    # running late in the local day gets nothing for
                    # yesterday, records that it attempted through today, and
                    # resuming from that cursor would never ask again - a
                    # permanent one-day hole behind a sync_log full of `ok`.
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

    return results


def auto_sync_if_stale(data_type: str) -> None:
    """Sync data_type if it has never been synced or last sync was before today.

    Failures are silently suppressed - the caller should still query the cache.
    This ensures tools work on first use without requiring an explicit health_sync call.

    No-op in offline mode (GOOGLE_HEALTH_MCP_OFFLINE): a cache-only host never syncs.
    """
    if config.OFFLINE_MODE:
        return

    try:
        conn = db.get_db()
        try:
            last_sync = db.get_last_sync_time(conn, data_type)
        finally:
            conn.close()

        if last_sync is not None and last_sync.date() >= date.today():
            return

        run_sync([data_type])
    except Exception as e:
        logger.debug("Auto-sync failed for %s: %s", data_type, type(e).__name__)


def refresh_before_query(data_type: str, start: date, end: date, live: bool) -> None:
    """Bring the cache up to date for one type, then let the caller read it.

    `live` skips the once-a-day gate and syncs exactly the window asked for.
    It raises where auto-sync would swallow: run_sync reports failure in its
    return value rather than by raising, so a caller that ignores the result
    answers a live request out of the cache without saying so.
    """
    if not live:
        auto_sync_if_stale(data_type)
        return

    if config.OFFLINE_MODE:
        # The offline contract is a tagged message, not an error reaching the
        # client: require_auth answers this type with one instead of converting.
        raise api.HealthOfflineError(
            "Offline mode (GOOGLE_HEALTH_MCP_OFFLINE): live refresh is unavailable; "
            "the cache is served as-is."
        )

    result = run_sync([data_type], since=start.isoformat(), until=end.isoformat())
    status = result.get(data_type, {}).get("status")
    if status != "ok":
        # The status word only. These paths carry API responses, and the
        # accompanying notes are not this function's to widen.
        raise LiveRefreshFailed(f"live refresh of {data_type} failed: {status or 'no result'}")


@mcp.tool()
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
        data_types: What to sync. Options: "all", "heart_rate", "activity",
            "exercises", "sleep", "weight", "spo2", "hrv", "azm",
            "breathing_rate", "skin_temperature", "core_temperature",
            "cardio_fitness", "food_log", "ecg", "irn".
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

    Returns summary of records synced per data type.
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
