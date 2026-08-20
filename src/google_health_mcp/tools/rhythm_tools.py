"""Heart-rhythm query tools: electrocardiograms and irregular-rhythm alerts."""

import anyio

from .. import db
from ..helpers import format_response, parse_date, require_auth
from ..mcp_instance import mcp
from .sync_tools import refresh_before_query


@mcp.tool()
@require_auth
async def health_get_ecg(
    start_date: str | None = None,
    end_date: str | None = None,
    live: bool = False,
    include_waveform: bool = False,
) -> str:
    """Get electrocardiogram (ECG) readings taken on the watch.

    Each reading is a single-lead trace the user started by hand, with a
    rhythm classification - NORMAL_SINUS_RHYTHM, ATRIAL_FIBRILLATION, or one
    of several inconclusive results (low or high heart rate, poor reading,
    unclassified). Use it for questions about heart rhythm or AFib checks;
    for resting rate over time use health_get_heart_rate instead.

    Args:
        start_date: Start date as "YYYY-MM-DD", "YYYY-MM", or "30d". Default: last 30 days.
        end_date: End date as "YYYY-MM-DD". Default: today.
        live: If true, re-fetch this window from the API before reading the cache.
        include_waveform: If true, include the raw voltage samples. These are
            thousands of integers per reading - ask for them only to analyse
            the trace itself. Multiply by scaling_factor for millivolts, and
            read them at sampling_hz samples per second.

    Returns one entry per reading with classification, avg_bpm, duration_sec,
    sampling_hz, scaling_factor and waveform_samples (null where no trace was
    stored, or where the stored one cannot be read).
    """
    start, end = parse_date(start_date, end_date, default_days=30)

    await anyio.to_thread.run_sync(lambda: refresh_before_query("ecg", start, end, live))

    def _query():
        conn = db.get_db()
        rows = db.query_ecg(conn, start.isoformat(), end.isoformat(), include_waveform)
        conn.close()
        return rows

    rows = await anyio.to_thread.run_sync(_query)

    if not rows:
        return format_response(
            {
                "message": "No ECG readings found for this period.",
                "hint": (
                    "ECG readings are taken by hand on a supported watch. "
                    "Try live=True to re-fetch this window."
                ),
            }
        )

    return format_response({"ecg": rows, "count": len(rows)})


@mcp.tool()
@require_auth
async def health_get_irregular_rhythm(
    start_date: str | None = None,
    end_date: str | None = None,
    live: bool = False,
) -> str:
    """Get irregular heart rhythm notifications raised by the watch.

    These are background checks the watch runs while the user is still - an
    alert means it saw a rhythm consistent with atrial fibrillation over one
    or more windows, not a diagnosis. Most accounts never have one. For a
    deliberate reading with a trace behind it, use health_get_ecg.

    Args:
        start_date: Start date as "YYYY-MM-DD", "YYYY-MM", or "30d". Default: last 30 days.
        end_date: End date as "YYYY-MM-DD". Default: today.
        live: If true, re-fetch this window from the API before reading the cache.

    Returns one entry per notification with start_time, end_time and
    alert_windows (the periods that triggered it).
    """
    start, end = parse_date(start_date, end_date, default_days=30)

    await anyio.to_thread.run_sync(lambda: refresh_before_query("irn", start, end, live))

    def _query():
        conn = db.get_db()
        rows = db.query_irn(conn, start.isoformat(), end.isoformat())
        conn.close()
        return rows

    rows = await anyio.to_thread.run_sync(_query)

    if not rows:
        return format_response(
            {
                "message": "No irregular rhythm notifications found for this period.",
                "hint": (
                    "These are raised by the watch and are rare. "
                    "Try live=True to re-fetch this window."
                ),
            }
        )

    return format_response({"irregular_rhythm": rows, "count": len(rows)})
