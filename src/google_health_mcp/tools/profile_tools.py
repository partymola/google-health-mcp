"""Account profile and settings query tool."""

from datetime import date

import anyio

from .. import db
from ..helpers import format_response, require_auth
from ..mcp_instance import mcp
from .sync_tools import refresh_before_query


@mcp.tool()
@require_auth
async def health_get_profile(live: bool = False) -> str:
    """Get the account's profile, settings and irregular-rhythm enrolment.

    Each is the record Google returns, kept whole: the profile carries age,
    membership start and stride lengths, the settings carry units, time zone
    and locale, and the enrolment says whether irregular-rhythm notifications
    are on. Returns data from the local cache by default.

    Args:
        live: If true, re-fetch the records from the API before reading the cache.

    Returns `profile`, `settings` and `irn_profile`, each null if never fetched,
    and when each was fetched.
    """
    today = date.today()
    await anyio.to_thread.run_sync(lambda: refresh_before_query("account", today, today, live))

    def _query():
        conn = db.get_db()
        try:
            return db.query_account(conn)
        finally:
            conn.close()

    stored = await anyio.to_thread.run_sync(_query)
    if not stored:
        return format_response(
            {
                "message": "No profile or settings have been fetched yet.",
                "hint": "Try live=True to re-fetch them from the API.",
            }
        )
    response = {}
    for resource in ("profile", "settings", "irn_profile"):
        record = stored.get(resource) or {}
        response[resource] = record.get("body")
        response[f"{resource}_fetched_at"] = record.get("fetched_at")
    return format_response(response)
