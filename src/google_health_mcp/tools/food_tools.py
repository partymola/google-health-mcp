"""Food and water log query tool."""

import anyio

from .. import db
from ..helpers import empty_answer, format_response, parse_date, require_auth
from ..mcp_instance import mcp
from .sync_tools import refresh_before_query

#: Each entry type the tool serves, and the field its points carry it under.
_ENTRY_TYPES = (
    ("food_entries", "nutritionLog"),
    ("water_entries", "hydrationLog"),
)


@mcp.tool(structured_output=False)
@require_auth
async def health_get_food_log(
    start_date: str | None = None,
    end_date: str | None = None,
    live: bool = False,
    include_entries: bool = False,
) -> str:
    """Get daily food and water totals, and each entry on request.

    Returns calories consumed and water intake (in mL) per day. Only populated
    where the user logs food or water by hand in a connected app. Returns from
    cache by default, syncing once a day from a few days before the last sync.
    An entry logged since today's first sync, or for an older date, needs
    live=True over a window covering it.

    Args:
        start_date: Start date as "YYYY-MM-DD", "YYYY-MM", or "30d". Default: 30
            days back from the end date or today, whichever is earlier; 7
            with include_entries.
        end_date: End date as "YYYY-MM-DD", "YYYY-MM" (its last day), or "Nd".
            Default: today.
        live: If true, re-fetch this window from the API before reading the cache.
        include_entries: If true, also return each food and drink logged,
            with Google's fields for it: for food, foodDisplayName, mealType,
            serving, energy, energyFromFat, totalCarbohydrate, totalFat,
            nutrients and food (Google's reference to the food record); for a
            drink, amountConsumed. Integers may arrive as strings. The first
            sync of entries covers 30 days, so a day before that can have a
            total and no entries until health_sync is run with since.

    Returns food_log, one row per logged day with calories_in and water_ml,
    and count, the number of rows. Days with no logging are omitted. With
    include_entries, also food_entries and water_entries. Each entry carries
    date, the local date it was logged for, and data_source. It also carries
    start_time and end_time in UTC, and start_utc_offset for the local time;
    on entries logged in the app, end_time falls seconds after start_time.
    entry_count is how many entries both lists hold.
    """
    start, end = parse_date(start_date, end_date, default_days=7 if include_entries else 30)

    # Together, so the daily gate syncs the totals and the entries at once.
    refreshed = ("food_log", *(t for t, _f in _ENTRY_TYPES)) if include_entries else "food_log"
    await anyio.to_thread.run_sync(lambda: refresh_before_query(refreshed, start, end, live))

    def _query():
        conn = db.get_db()
        try:
            totals = db.query_food_log(conn, start.isoformat(), end.isoformat())
            days = {
                table: (
                    db.query_day_lists(conn, table, start.isoformat(), end.isoformat())
                    if include_entries
                    else []
                )
                for table, _field in _ENTRY_TYPES
            }
            return totals, days
        finally:
            conn.close()

    entries, days = await anyio.to_thread.run_sync(_query)

    logged = {}
    for table, field in _ENTRY_TYPES:
        items = []
        for day in days[table]:
            for point in day["points"] or []:
                payload = point.get(field) if isinstance(point, dict) else None
                if not isinstance(payload, dict):
                    continue
                interval = payload.get("interval") or {}
                items.append(
                    {
                        **{k: v for k, v in payload.items() if k != "interval"},
                        "date": day["date"],
                        "start_time": interval.get("startTime"),
                        "end_time": interval.get("endTime"),
                        "start_utc_offset": interval.get("startUtcOffset"),
                        "data_source": point.get("dataSource"),
                    }
                )
        logged[table] = items
    logged_count = sum(len(items) for items in logged.values())

    if not entries and not logged_count:
        return empty_answer(
            "No food log data found for this period.",
            live,
            "Food and water are logged by hand, so most days have none.",
        )

    body = {"food_log": entries, "count": len(entries)}
    if include_entries:
        body.update(logged)
        body["entry_count"] = logged_count
    return format_response(body)
