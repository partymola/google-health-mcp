"""Temperature query tools (skin and core)."""

import anyio

from .. import db
from ..helpers import format_response, parse_date, require_auth
from ..mcp_instance import mcp
from .sync_tools import refresh_before_query


@mcp.tool()
@require_auth
async def health_get_skin_temperature(
    start_date: str | None = None,
    end_date: str | None = None,
    live: bool = False,
) -> str:
    """Get nightly skin temperature variation (degrees Celsius from personal baseline).

    This is the device-derived RELATIVE deviation recorded during sleep, NOT an
    absolute body temperature - for fever / body-temperature readings use
    health_get_core_temperature instead. A baseline takes about three nights to
    establish before values appear. Useful as an illness/cycle/recovery signal.

    Args:
        start_date: Start date as "YYYY-MM-DD", "YYYY-MM", or "30d". Default: last 30 days.
        end_date: End date as "YYYY-MM-DD". Default: today.
        live: If true, re-fetch this window from the API before reading the cache.

    Returns one entry per night with nightly_relative (degrees C, can be negative)
    and log_type (e.g. "dermal").
    """
    start, end = parse_date(start_date, end_date, default_days=30)

    await anyio.to_thread.run_sync(
        lambda: refresh_before_query("skin_temperature", start, end, live)
    )

    def _query():
        conn = db.get_db()
        rows = db.query_skin_temperature(conn, start.isoformat(), end.isoformat())
        conn.close()
        return rows

    entries = await anyio.to_thread.run_sync(_query)

    if not entries:
        return format_response(
            {
                "message": "No skin temperature data found for this period.",
                "hint": (
                    "Try live=True to re-fetch this window. Requires sleep tracking and baseline."
                ),
            }
        )

    return format_response({"skin_temperature": entries, "count": len(entries)})


@mcp.tool()
@require_auth
async def health_get_core_temperature(
    start_date: str | None = None,
    end_date: str | None = None,
    live: bool = False,
) -> str:
    """Get manually-logged core (body) temperature readings (degrees Celsius).

    These are absolute body temperatures the user enters by hand - a
    thermometer reading logged in an app - and are the right source for
    fever / body-temperature questions. They are NOT the device-derived nightly
    skin-temperature variation from health_get_skin_temperature. A single day can
    hold several readings (each timestamped), useful for tracking a fever over time.

    Args:
        start_date: Start date as "YYYY-MM-DD", "YYYY-MM", or "30d". Default: last 30 days.
        end_date: End date as "YYYY-MM-DD". Default: today.
        live: If true, re-fetch this window from the API before reading the cache.

    Returns one entry per logged reading with datetime (YYYY-MM-DDThh:mm:ss)
    and temp_celsius.
    """
    start, end = parse_date(start_date, end_date, default_days=30)

    await anyio.to_thread.run_sync(
        lambda: refresh_before_query("core_temperature", start, end, live)
    )

    def _query():
        conn = db.get_db()
        rows = db.query_core_temperature(conn, start.isoformat(), end.isoformat())
        conn.close()
        return rows

    entries = await anyio.to_thread.run_sync(_query)

    if not entries:
        return format_response(
            {
                "message": "No core temperature data found for this period.",
                "hint": (
                    "Core temperature is only present when readings are logged "
                    "manually in the app. Try live=True to re-fetch this window."
                ),
            }
        )

    return format_response({"core_temperature": entries, "count": len(entries)})
