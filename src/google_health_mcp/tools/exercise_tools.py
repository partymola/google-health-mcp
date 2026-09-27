"""Exercise log and route query tools."""

from datetime import date

import anyio

from .. import db
from ..errors import UnknownExerciseType
from ..helpers import LIVE_HINT, format_response, parse_date, require_auth
from ..mcp_instance import mcp
from .sync_tools import refresh_before_query


@mcp.tool()
@require_auth
async def health_get_exercises(
    start_date: str | None = None,
    end_date: str | None = None,
    exercise_type: str | None = None,
    live: bool = False,
) -> str:
    """Get exercise log entries (individual tracked activities).

    Returns exercise sessions from the local cache by default. Use live=True
    to fetch from the API. Run health_sync first to populate the cache.

    Args:
        start_date: Start date as "YYYY-MM-DD", "YYYY-MM", or "30d". Default: last 30 days.
        end_date: End date as "YYYY-MM-DD". Default: today.
        exercise_type: Filter by activity name (case-insensitive substring match),
            e.g. "cycling", "walk", "run". Default: all types. A value matching
            no workout name the cache holds is refused, naming those, rather
            than answered as a period with no workouts.
        live: If true, re-fetch this window from the API before reading the cache.

    Returns exercise entries with name, duration, calories, avg heart rate,
    distance, and source (auto-detect vs manual).
    Note: HR data from cycling may be unreliable (optical sensor vs handlebar grip).
    """
    start, end = parse_date(start_date, end_date, default_days=30)

    await anyio.to_thread.run_sync(lambda: refresh_before_query("exercises", start, end, live))

    def _query():
        conn = db.get_db()
        try:
            if exercise_type is None:
                return db.query_exercises(conn, start.isoformat(), end.isoformat())
            # Folded here rather than in SQL, whose LOWER covers ASCII alone.
            cached = db.exercise_names(conn)
            matched = [n for n in cached if exercise_type.casefold() in n.casefold()]
            if cached and not matched:
                raise UnknownExerciseType(
                    f"No workout named like '{exercise_type}' in the cache. "
                    f"Names cached: {', '.join(cached)}."
                )
            return db.query_exercises(conn, start.isoformat(), end.isoformat(), matched)
        finally:
            conn.close()

    entries = await anyio.to_thread.run_sync(_query)

    if not entries:
        return format_response(
            {
                "message": "No exercise entries found for this period.",
                "hint": LIVE_HINT,
            }
        )

    return format_response({"exercises": entries, "count": len(entries)})


@mcp.tool()
@require_auth
async def health_get_exercise_route(log_id: str, include_tcx: bool = False) -> str:
    """Get the GPS route of one exercise, as the TCX file Google exports.

    Only exercises recorded with GPS have a route, and a sync fetches routes
    for the workouts in its own window. The TCX text carries every trackpoint
    (time, position, altitude, distance, heart rate) and can run to hundreds of
    kilobytes, so it is returned only when asked for.

    Args:
        log_id: The exercise's `log_id`, as health_get_exercises returns it.
        include_tcx: If true, include the TCX text itself.

    Returns the exercise's log_id, date and provider, and the TCX text when
    asked for.
    """
    today = date.today()
    await anyio.to_thread.run_sync(
        lambda: refresh_before_query("exercise_routes", today, today, False)
    )

    def _query():
        conn = db.get_db()
        try:
            return db.query_exercise_route(conn, log_id)
        finally:
            conn.close()

    route = await anyio.to_thread.run_sync(_query)
    if route is None:
        return format_response(
            {
                "message": f"No route for exercise '{log_id}' is in the cache.",
                "hint": (
                    "Only exercises recorded with GPS have one, and none is fetched for "
                    "a day whose workouts came from an import. A sync fetches routes "
                    "from its own window, so an older one needs a sync of "
                    "exercise_routes with since set before its date, on the host "
                    "that syncs."
                ),
            }
        )
    tcx = route.pop("tcx")
    if include_tcx:
        route["tcx"] = tcx
    return format_response(route)
