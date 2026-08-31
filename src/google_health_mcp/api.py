"""Google Health API client with automatic token refresh and paging.

One data type is spelled three different ways and each mistake fails
differently, so `GOOGLE_TYPES` carries all three rather than deriving them:
the request path is kebab-case, the filter field is snake_case, and the field
carrying the value in the response is camelCase. A wrong path is a 404 and a
wrong filter is a malformed request, but a wrong response field is a request
that succeeds while every page parses to nothing.

Quotas are per user: 300 requests a minute for a verified app, and 2.5 a
second for an unverified one - which is every install of this, since the
100-user cap makes verification unnecessary. Measured against that: a full
three-year backfill of every type is around 250 requests.
"""

import json
import logging
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from typing import NamedTuple
from urllib.parse import urlencode

from . import config
from .auth import RefreshNetworkError, TokenRefused, refresh_google_token
from .errors import GoogleHealthError

logger = logging.getLogger(__name__)


class HealthAuthError(GoogleHealthError):
    """Token expired or invalid, re-auth needed."""


class HealthOfflineError(GoogleHealthError):
    """A live API call was attempted while offline/cache-only mode is on.

    Deliberately a sibling of HealthAPIError / HealthAuthError /
    HealthRateLimitError rather than one of them: run_sync() catches those per
    data type, which would swallow this and write spurious error rows to the
    cache. It is meant to propagate up to require_auth (and the CLI sync
    handler), which translate it into a single clean "offline mode" message.
    The shared GoogleHealthError base is not one of those catches.
    """


#: Longest a 429 may ask us to wait. The value is reported rather than slept
#: on today, and the bound is what makes it safe for anything that ever does:
#: a wait would happen on the thread serving an MCP tool call.
MAX_RATE_LIMIT_WAIT = 900


class HealthRateLimitError(GoogleHealthError):
    """Rate limited (429). Retry after reset seconds."""

    def __init__(self, reset_seconds: int = MAX_RATE_LIMIT_WAIT):
        self.reset_seconds = reset_seconds
        super().__init__(f"Rate limited. Retry in {reset_seconds}s.")


def _reset_seconds(error) -> int:
    """How long a 429 asks us to wait, bounded and never unparseable.

    `Retry-After` is the standard header and Google does not document whether
    it sends one, so its absence is ordinary rather than a fault. Its other
    documented form is an HTTP date, which is not read here - anything that is
    not a number of seconds falls back to the cap.
    """
    try:
        raw = error.headers.get("Retry-After", MAX_RATE_LIMIT_WAIT)
    except AttributeError:
        return MAX_RATE_LIMIT_WAIT
    try:
        # float first, so a fractional header keeps its value. OverflowError
        # is not a ValueError: int(float("inf")) raises it, where int("inf")
        # did not.
        seconds = int(float(raw))
    except (TypeError, ValueError, OverflowError):
        return MAX_RATE_LIMIT_WAIT
    if seconds < 0:
        return 0
    return min(seconds, MAX_RATE_LIMIT_WAIT)


class HealthAPIError(GoogleHealthError):
    """General API error."""


# --- Google Health API ---

#: Server maximum. Larger values are truncated rather than refused, so asking
#: for more than this silently gets you this.
GOOGLE_MAX_PAGE_SIZE = 10000

#: Sleep and exercise cap at 25, for both the default and the maximum.
GOOGLE_SESSION_PAGE_SIZE = 25

#: A page loop with no ceiling hangs the thread serving a tool call if the
#: server keeps handing back tokens. Generous enough that no real window
#: reaches it: three years of intraday points at the maximum page size.
GOOGLE_MAX_PAGES = 500

#: The page count alone does not bound the wall clock - 500 requests at the
#: 30-second socket timeout is hours on the thread serving a tool call, which
#: is the same argument that bounds MAX_RATE_LIMIT_WAIT.
GOOGLE_WALK_DEADLINE = 600


class GoogleType(NamedTuple):
    """How one data type is spelled, filtered and paged.

    The three names differ and fail differently: `path` wrong is a 404,
    `filter_on` wrong is a malformed request, and `field` wrong is a request
    that succeeds while every page parses to nothing.
    """

    path: str
    field: str
    filter_on: str | None
    literal: str  # "date" or "timestamp"
    page_size: int = GOOGLE_MAX_PAGE_SIZE
    closed_range: bool = True
    #: Three types answer a list request with "List is not supported for data
    #: type X" and must be rolled up instead. They carry no filter, because
    #: sending one is always an error.
    listable: bool = True
    #: Longest range a single dailyRollUp accepts. 14 days for the calorie and
    #: heart-rate family, 90 for everything else.
    rollup_cap_days: int = 90


def _interval(path: str, field: str, **kw) -> GoogleType:
    return GoogleType(
        path, field, f"{path.replace('-', '_')}.interval.civil_start_time", "date", **kw
    )


def _daily(path: str, field: str, **kw) -> GoogleType:
    return GoogleType(path, field, f"{path.replace('-', '_')}.date", "date", **kw)


def _sample(path: str, field: str, **kw) -> GoogleType:
    return GoogleType(path, field, f"{path.replace('-', '_')}.sample_time.civil_time", "date", **kw)


def _rollup_only(path: str, field: str, cap_days: int) -> GoogleType:
    return GoogleType(path, field, None, "date", listable=False, rollup_cap_days=cap_days)


#: Every data type this package can read. Keyed by the request path, which is
#: the API's own identity for a type.
#:
#: Wider than what the sync uses: a dozen entries here are catalogued rather
#: than read, either superseded by a `daily-*` twin or measured and rejected
#: (`activity-level`, whose reasons are in `google_sync.sync_activity`). A
#: type appearing here is not evidence that anything fetches it - the handler
#: map in `google_sync` is.
GOOGLE_TYPES: dict[str, GoogleType] = {
    t.path: t
    for t in (
        _interval("steps", "steps"),
        _interval("distance", "distance"),
        # Measured: these three refuse `list` outright. total-calories is the
        # only source for a day's total energy, BMR included - summing active
        # and basal instead is wrong, because basal returns no points at all
        # on some accounts and it is most of the number.
        _rollup_only("floors", "floors", 90),
        _rollup_only("total-calories", "totalCalories", 14),
        _rollup_only("calories-in-heart-rate-zone", "caloriesInHeartRateZone", 14),
        _interval("active-minutes", "activeMinutes", rollup_cap_days=14),
        _interval("activity-level", "activityLevel"),
        _interval("sedentary-period", "sedentaryPeriod"),
        _interval("active-energy-burned", "activeEnergyBurned"),
        _interval("basal-energy-burned", "basalEnergyBurned"),
        _sample("heart-rate", "heartRate", rollup_cap_days=14),
        _interval("active-zone-minutes", "activeZoneMinutes"),
        _interval("time-in-heart-rate-zone", "timeInHeartRateZone"),
        _sample("heart-rate-variability", "heartRateVariability"),
        _sample("oxygen-saturation", "oxygenSaturation"),
        _daily("daily-resting-heart-rate", "dailyRestingHeartRate"),
        _daily("daily-heart-rate-zones", "dailyHeartRateZones"),
        _daily("daily-heart-rate-variability", "dailyHeartRateVariability"),
        _daily("daily-oxygen-saturation", "dailyOxygenSaturation"),
        _daily("daily-respiratory-rate", "dailyRespiratoryRate"),
        _daily("daily-sleep-temperature-derivations", "dailySleepTemperatureDerivations"),
        _daily("daily-vo2-max", "dailyVo2Max"),
        _sample("weight", "weight"),
        _sample("body-fat", "bodyFat"),
        _sample("core-body-temperature", "coreBodyTemperature"),
        _sample("vo2-max", "vo2Max"),
        _interval("nutrition-log", "nutritionLog"),
        _interval("hydration-log", "hydrationLog"),
        # Sessions. Sleep bins on the end of the night, not its start, so a
        # night beginning on the 1st belongs to the 2nd.
        _interval("exercise", "exercise", page_size=GOOGLE_SESSION_PAGE_SIZE),
        GoogleType(
            "sleep",
            "sleep",
            "sleep.interval.civil_end_time",
            "date",
            page_size=GOOGLE_SESSION_PAGE_SIZE,
        ),
        # ECG accepts >= only; filtering on end time is unsupported, so the
        # window is open at the top and the caller drops what overruns it.
        GoogleType(
            "electrocardiogram",
            "electrocardiogram",
            "electrocardiogram.interval.start_time",
            "timestamp",
            closed_range=False,
        ),
        _interval("irregular-rhythm-notification", "irregularRhythmNotification"),
    )
}


def _google_filter(spec: GoogleType, start: date, end: date) -> str:
    """Build the range filter, in the literal form this type's field expects."""
    if spec.literal == "timestamp":
        low, high = f"{start.isoformat()}T00:00:00Z", f"{end.isoformat()}T00:00:00Z"
    else:
        low, high = start.isoformat(), end.isoformat()
    clause = f'{spec.filter_on} >= "{low}"'
    if spec.closed_range:
        clause += f' AND {spec.filter_on} < "{high}"'
    return clause


def _classify_google_api_error(error: urllib.error.HTTPError):
    """Map a refused data request to the cause a user can act on.

    A 403 is either a scope never granted or an app that never left Testing,
    and the remedies differ. The body is read to tell them apart; nothing
    from it reaches the message.
    """
    if error.code == 401:
        # Not a permissions question: the token itself was rejected, so the
        # publish-or-tester advice below would send the user somewhere useless.
        return HealthAuthError("Google rejected the access token. Run: google-health-mcp auth")
    reason = ""
    try:
        body = json.loads(error.read().decode())
        detail = (body or {}).get("error") or {}
        reason = f"{detail.get('status', '')} {detail.get('message', '')}".lower()
    except Exception:
        pass
    if "scope" in reason or "insufficient" in reason:
        return HealthAuthError(
            "Google refused the request for a scope this grant does not carry. "
            "Re-run: google-health-mcp auth"
        )
    return HealthAuthError(
        "Google refused the request. Either the OAuth app has not been published "
        "or the account is not an approved tester. Run: google-health-mcp auth"
    )


def google_get(path: str, params: dict, body: dict | None = None) -> dict:
    """One authenticated request against the Google Health API.

    A body makes it the POST the rollup methods require; without one it is a
    GET with the params in the query string.

    Mirrors `get` above: the same two-type classification out of the token
    layer, the same read-then-parse split so an unreadable body is reported
    rather than escaping, and no response content in any message.
    """
    if config.OFFLINE_MODE:
        raise HealthOfflineError(
            "Offline mode is on (GOOGLE_HEALTH_MCP_OFFLINE); live API calls are disabled. "
            "Query the local cache instead, or unset GOOGLE_HEALTH_MCP_OFFLINE."
        )
    try:
        token = refresh_google_token()
    except TokenRefused as e:
        raise HealthAuthError(
            "Could not obtain an access token. Run: google-health-mcp auth"
        ) from e
    except RefreshNetworkError as e:
        raise HealthAPIError("Network error. Check your connection.") from e

    url = f"{config.GOOGLE_API_BASE}/{path}"
    if params:
        url += f"?{urlencode(params)}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    payload = None
    if body is not None:
        payload = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=payload, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise HealthRateLimitError(_reset_seconds(e)) from e
        if e.code in (401, 403):
            raise _classify_google_api_error(e) from e
        if e.code == 504:
            raise GoogleGatewayTimeout("Google timed out serving the request.") from e
        raise HealthAPIError(f"API error {e.code} for {path}") from e
    except (TimeoutError, urllib.error.URLError) as e:
        raise HealthAPIError("Network error. Check your connection.") from e

    try:
        body = json.loads(raw)
    except ValueError as e:
        raise HealthAPIError("Google returned an unreadable response.") from e
    if not isinstance(body, dict):
        raise HealthAPIError("Google returned an unexpected response shape.")
    return body


class GoogleGatewayTimeout(GoogleHealthError):
    """A 504, which Google's own guidance says to retry with a smaller page.

    Internal to the paging loop: callers see whatever the retry ends up
    raising, so this never reaches run_sync.
    """


def list_google_data_points(data_type: str, start: date, end: date) -> list[dict]:
    """Every data point of one type in [start, end), following pages to exhaustion.

    An empty page carrying a `nextPageToken` is not an empty window: measured
    against the real API, a month of steps returned nothing on its first page
    and 6025 points behind it. Only an absent or empty token ends the walk.

    Resuming *across runs* re-issues a narrowed request rather than storing a
    token: one may only be replayed with every other field unchanged, and it
    carries no documented lifetime. Within a single walk a token is replayed,
    which is why a 504 restarts rather than shrinking the page under one.
    """
    spec = GOOGLE_TYPES.get(data_type)
    if spec is None:
        raise ValueError(f"{data_type} is not a data type this package reads")
    if not spec.listable:
        raise ValueError(f"{data_type} cannot be listed; read it with daily_roll_up")
    if start >= end:
        # Measured: an empty closed-open window is refused with "Invalid data
        # point filter: INVALID_TIME_RANGE", not answered with no points. An
        # incremental sync produces exactly that whenever it is already up to
        # date, so asking would turn a no-op into an error row every run.
        return []

    points: list[dict] = []
    page_size = spec.page_size
    token = None
    deadline = time.monotonic() + GOOGLE_WALK_DEADLINE
    for _ in range(GOOGLE_MAX_PAGES):
        params = {
            "filter": _google_filter(spec, start, end),
            "pageSize": page_size,
        }
        if token:
            params["pageToken"] = token
        if time.monotonic() > deadline:
            raise HealthAPIError(f"gave up walking {data_type} after {GOOGLE_WALK_DEADLINE}s")
        try:
            body = google_get(f"users/me/dataTypes/{spec.path}/dataPoints", params)
        except GoogleGatewayTimeout:
            if page_size <= 1:
                raise HealthAPIError("Google timed out even on a single-item page") from None
            # Halving rather than repeating: Google's guidance is explicitly
            # against re-sending the same large payload straight away. The walk
            # restarts, because a token may only be replayed with every other
            # field unchanged - so the smaller page and the old token cannot be
            # sent together.
            page_size //= 2
            token = None
            points = []
            continue
        page = body.get("dataPoints")
        if page is not None and not isinstance(page, list):
            raise HealthAPIError("Google returned an unexpected response shape.")
        points.extend(page or [])
        token = body.get("nextPageToken")
        if not token:
            return points
    raise HealthAPIError(f"too many pages for {data_type}")


#: Types with no daily rollup at all. Measured: each answers dailyRollUp with
#: "DailyRollup is not supported for data type X".
#:
#: These are facts about Google, not about this code, so no test can tell you
#: whether a name belongs here - only a live call can. Adding one costs a
#: refused request; taking one out grants a capability the API may not have,
#: so remove a name only after a real dailyRollUp for that type succeeds.
_NO_DAILY_ROLLUP = frozenset(
    {
        "basal-energy-burned",
        "activity-level",
        "heart-rate-variability",
        "oxygen-saturation",
        "vo2-max",
        "daily-vo2-max",
        "daily-resting-heart-rate",
        "daily-heart-rate-zones",
        "daily-heart-rate-variability",
        "daily-oxygen-saturation",
        "daily-respiratory-rate",
        "daily-sleep-temperature-derivations",
        "exercise",
        "sleep",
        "electrocardiogram",
        "irregular-rhythm-notification",
    }
)


def _civil(day: date) -> dict:
    return {"date": {"year": day.year, "month": day.month, "day": day.day}}


def daily_roll_up(data_type: str, start: date, end: date) -> list[dict]:
    """One point per day for a type, over [start, end), in cap-sized chunks.

    The read path for the three types that refuse `list`, and the cheaper one
    for any daily aggregate. Chunking is done here so a caller never has to
    know that the cap is 14 days for the calorie family and 90 for the rest.

    Days the response omits are omitted from the result: whether a missing day
    is off-wrist or a genuine zero is not something this layer can tell, and
    filling it in either direction would invent data.
    """
    spec = GOOGLE_TYPES.get(data_type)
    if spec is None:
        raise ValueError(f"{data_type} is not a data type this package reads")
    if data_type in _NO_DAILY_ROLLUP:
        raise ValueError(f"{data_type} has no daily rollup; read it with list_google_data_points")

    points: list[dict] = []
    window_start = start
    while window_start < end:
        window_end = min(window_start + timedelta(days=spec.rollup_cap_days), end)
        # No pageSize: measured, dailyRollUp answers total-calories with 400
        # when it is present, and a capped window carries no page token.
        try:
            body = google_get(
                f"users/me/dataTypes/{spec.path}/dataPoints:dailyRollUp",
                {},
                {
                    "range": {"start": _civil(window_start), "end": _civil(window_end)},
                    "windowSizeDays": 1,
                },
            )
        except GoogleGatewayTimeout as e:
            # Reported rather than retried smaller, and not for the reason the
            # list path halves a page. A page size is a transport parameter and
            # shrinking it returns the same data; the only shrinkable dimension
            # here is the window, and moving that changes which days land in
            # which request - a semantic change wearing a retry's clothes.
            raise HealthAPIError(f"Google timed out rolling up {data_type}") from e
        if body.get("nextPageToken"):
            raise HealthAPIError(f"{data_type} rollup returned a page token, which it never has")
        page = body.get("rollupDataPoints")
        if page is not None and not isinstance(page, list):
            raise HealthAPIError("Google returned an unexpected response shape.")
        points.extend(page or [])
        window_start = window_end
    return points


def list_paired_devices() -> list[dict]:
    """Every device paired with the account, following the page token.

    Paged like every other list here, and for the same reason: a response
    carrying a token is not the whole answer however short it looks, and a
    second watch missing from the list is not something a reader would query
    twice to check.
    """
    devices: list[dict] = []
    token = None
    for _ in range(GOOGLE_MAX_PAGES):
        params = {"pageToken": token} if token else {}
        body = google_get("users/me/pairedDevices", params)
        page = body.get("pairedDevices")
        if page is not None and not isinstance(page, list):
            raise HealthAPIError("Google returned an unexpected response shape.")
        devices.extend(page or [])
        token = body.get("nextPageToken")
        if not token:
            return devices
    raise HealthAPIError("Google returned more pages of devices than expected.")
