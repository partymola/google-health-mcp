"""Lifetime activity totals and personal bests, computed from the cache."""

import anyio

from .. import db
from ..helpers import format_response, require_auth
from ..mcp_instance import mcp

_SUMMED = ("steps", "floors", "distance_km", "calories_out")
_RANKED = ("steps", "floors", "distance_km")


def lifetime_from_cache(conn) -> dict:
    """All-time totals and best days over every activity row held."""
    totals = dict(
        zip(
            _SUMMED,
            conn.execute(
                "SELECT COALESCE(SUM(steps), 0), COALESCE(SUM(floors), 0), "
                "COALESCE(SUM(distance_km), 0), COALESCE(SUM(calories_out), 0) FROM activity"
            ).fetchone(),
        )
    )

    best = {}
    for column in _RANKED:
        # NULL sorts last under DESC, so the filter only matters when a column
        # is entirely unmeasured - without it that answers with a real date and
        # a null value, which reads as a record rather than as no record.
        # The date tiebreak keeps one cache from answering two ways.
        row = conn.execute(
            f"SELECT {column}, date FROM activity WHERE {column} IS NOT NULL "
            f"ORDER BY {column} DESC, date ASC LIMIT 1"
        ).fetchone()
        best[column] = {"value": row[0], "date": row[1]} if row else None

    days, first_date, last_date = conn.execute(
        "SELECT COUNT(*), MIN(date), MAX(date) FROM activity"
    ).fetchone()
    return {
        "totals": totals,
        "best": best,
        "coverage": {"days": days, "first_date": first_date, "last_date": last_date},
    }


@mcp.tool()
@require_auth
async def health_get_lifetime_stats() -> str:
    """Get all-time activity totals and personal best records.

    Totals for steps, floors, distance and calories over every day held in the
    cache, plus the best single day for steps, floors and distance with the
    date each record was set.

    The answer is bounded by what has been synced, so it is returned with a
    `coverage` block giving the first and last dates and the number of days
    counted - read it before quoting a total as all-time. There is no
    tracker-versus-total split and no "active score"; neither has a source.
    """

    def _read():
        conn = db.get_db()
        try:
            return lifetime_from_cache(conn)
        finally:
            conn.close()

    return format_response(await anyio.to_thread.run_sync(_read))
