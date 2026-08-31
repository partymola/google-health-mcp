"""Trend analysis tool - works from local cache only."""

import re
from collections import defaultdict
from datetime import date, timedelta

import anyio

from .. import db
from ..helpers import format_duration, format_response, parse_date, require_auth
from ..mcp_instance import mcp
from .sync_tools import auto_sync_if_stale


def _get_period_key(ds: str, period: str) -> str:
    """Map a YYYY-MM-DD date string to a period bucket key."""
    year = ds[:4]
    month = int(ds[5:7])
    if period == "weekly":
        d = date.fromisoformat(ds)
        iso_year, iso_week, _ = d.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    elif period == "quarterly":
        q = (month - 1) // 3 + 1
        return f"{year}-Q{q}"
    else:  # monthly
        return ds[:7]


def _avg(values: list) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def _count_row(bucket: dict, name: str, row: dict, fields: tuple[str, ...]) -> None:
    """Count a row once if it carries any of these fields.

    Counting a column's values instead answers a different question, and the
    two only agree while every row carries the same columns.
    """
    if any(row.get(f) is not None for f in fields):
        bucket[name].append(1)


def _trend_heart_rate(conn, start_date: str, end_date: str, period: str) -> dict:
    rows = db.query_heart_rate(conn, start_date, end_date)
    if not rows:
        return {"message": "No heart rate data in cache. No data recorded for this period."}

    buckets = defaultdict(list)
    for r in rows:
        hr = r.get("resting_hr")
        if hr is not None:
            buckets[_get_period_key(r["date"], period)].append(hr)

    periods = []
    for key in sorted(buckets.keys()):
        vals = buckets[key]
        periods.append(
            {
                "period": key,
                "days": len(vals),
                "avg_resting_hr": _avg(vals),
                "min_resting_hr": min(vals) if vals else None,
                "max_resting_hr": max(vals) if vals else None,
            }
        )
    return {"periods": periods, "data_type": "heart_rate", "aggregation": period}


def _trend_activity(conn, start_date: str, end_date: str, period: str) -> dict:
    rows = db.query_activity(conn, start_date, end_date)
    if not rows:
        return {"message": "No activity data in cache. No data recorded for this period."}

    buckets = defaultdict(lambda: defaultdict(list))
    for r in rows:
        key = _get_period_key(r["date"], period)
        for f in ["steps", "calories_out", "active_minutes", "distance_km"]:
            v = r.get(f)
            if v is not None:
                buckets[key][f].append(v)

    periods = []
    for key in sorted(buckets.keys()):
        b = buckets[key]
        dist = b.get("distance_km", [])
        periods.append(
            {
                "period": key,
                "days": len(b.get("steps", [])),
                "avg_steps": _avg(b.get("steps", [])),
                "avg_active_minutes": _avg(b.get("active_minutes", [])),
                "total_distance_km": round(sum(dist), 1) if dist else None,
                "avg_calories_out": _avg(b.get("calories_out", [])),
            }
        )
    return {"periods": periods, "data_type": "activity", "aggregation": period}


def _trend_sleep(conn, start_date: str, end_date: str, period: str) -> dict:
    rows = db.query_sleep(conn, start_date, end_date)
    if not rows:
        return {"message": "No sleep data in cache. No data recorded for this period."}

    buckets = defaultdict(lambda: defaultdict(list))
    for r in rows:
        key = _get_period_key(r["date"], period)
        for f in ["total_minutes", "efficiency", "deep_minutes", "rem_minutes"]:
            v = r.get(f)
            if v is not None:
                buckets[key][f].append(v)

    periods = []
    for key in sorted(buckets.keys()):
        b = buckets[key]
        total = b.get("total_minutes", [])
        periods.append(
            {
                "period": key,
                "nights": len(total),
                "avg_total_sleep": format_duration(_avg(total)),
                "avg_deep_sleep": format_duration(_avg(b.get("deep_minutes", []))),
                "avg_rem_sleep": format_duration(_avg(b.get("rem_minutes", []))),
                "avg_efficiency": _avg(b.get("efficiency", [])),
            }
        )
    return {"periods": periods, "data_type": "sleep", "aggregation": period}


def _trend_weight(conn, start_date: str, end_date: str, period: str) -> dict:
    rows = db.query_weight(conn, start_date, end_date)
    if not rows:
        return {"message": "No weight data in cache. No data recorded for this period."}

    buckets = defaultdict(lambda: defaultdict(list))
    for r in rows:
        key = _get_period_key(r["date"], period)
        for f in ["weight_kg", "fat_pct", "bmi"]:
            v = r.get(f)
            if v is not None:
                buckets[key][f].append(v)

    periods = []
    for key in sorted(buckets.keys()):
        b = buckets[key]
        periods.append(
            {
                "period": key,
                "count": len(b.get("weight_kg", [])),
                "avg_weight_kg": _avg(b.get("weight_kg", [])),
                "avg_fat_pct": _avg(b.get("fat_pct", [])),
                "avg_bmi": _avg(b.get("bmi", [])),
            }
        )
    return {"periods": periods, "data_type": "weight", "aggregation": period}


def _trend_spo2(conn, start_date: str, end_date: str, period: str) -> dict:
    """Both definitions of a night's bounds, reported side by side.

    `min`/`max` are observed nightly extremes; `avg_ci_low`/`avg_ci_high` are
    a confidence interval on the average. Averaging the two into one series
    makes the change from one definition to the other read as
    physiology, so each period says how many nights of each it holds.
    """
    rows = db.query_spo2(conn, start_date, end_date)
    if not rows:
        return {"message": "No SpO2 data in cache. No data recorded for this period."}

    buckets = defaultdict(lambda: defaultdict(list))
    for r in rows:
        key = _get_period_key(r["date"], period)
        for f in ["avg", "min", "max", "avg_ci_low", "avg_ci_high"]:
            v = r.get(f)
            if v is not None:
                buckets[key][f].append(v)
        # Counted per row over both ends of a pair. Taking the longer of the
        # two lists instead undercounts whenever the missing end varies -
        # three nights with a minimum and two with a maximum are five nights
        # carrying an extreme, not three.
        _count_row(buckets[key], "_extremes", r, ("min", "max"))
        _count_row(buckets[key], "_ci", r, ("avg_ci_low", "avg_ci_high"))

    periods = []
    for key in sorted(buckets.keys()):
        b = buckets[key]
        mins, maxes = b.get("min", []), b.get("max", [])
        ci_lows, ci_highs = b.get("avg_ci_low", []), b.get("avg_ci_high", [])
        periods.append(
            {
                "period": key,
                "nights": len(b.get("avg", [])),
                "avg_spo2": _avg(b.get("avg", [])),
                "nights_observed_extremes": len(b.get("_extremes", [])),
                "min_spo2": min(mins) if mins else None,
                "max_spo2": max(maxes) if maxes else None,
                "nights_confidence_interval": len(b.get("_ci", [])),
                # The mean of the nightly bounds, not a confidence interval on
                # the period's own average - named so a reader cannot take an
                # aggregate for the stored column it came from.
                "avg_nightly_ci_low": _avg(ci_lows),
                "avg_nightly_ci_high": _avg(ci_highs),
            }
        )
    return {"periods": periods, "data_type": "spo2", "aggregation": period}


def _trend_exercises(conn, start_date: str, end_date: str, period: str) -> dict:
    rows = db.query_exercises(conn, start_date, end_date)
    if not rows:
        return {"message": "No exercise data in cache. No data recorded for this period."}

    buckets = defaultdict(lambda: defaultdict(list))
    for r in rows:
        key = _get_period_key(r["date"], period)
        for f in ["duration_min", "calories"]:
            v = r.get(f)
            if v is not None:
                buckets[key][f].append(v)
        buckets[key]["_count"].append(1)

    periods = []
    for key in sorted(buckets.keys()):
        b = buckets[key]
        dur = b.get("duration_min", [])
        periods.append(
            {
                "period": key,
                "sessions": len(b.get("_count", [])),
                "total_duration": format_duration(sum(dur)) if dur else None,
                "avg_duration": format_duration(_avg(dur)),
                "total_calories": sum(b.get("calories", [])) if b.get("calories") else None,
            }
        )
    return {"periods": periods, "data_type": "exercises", "aggregation": period}


def _trend_hrv(conn, start_date: str, end_date: str, period: str) -> dict:
    rows = db.query_hrv(conn, start_date, end_date)
    if not rows:
        return {"message": "No HRV data in cache. No data recorded for this period."}

    buckets = defaultdict(lambda: defaultdict(list))
    for r in rows:
        key = _get_period_key(r["date"], period)
        for f in ["daily_rmssd", "deep_rmssd"]:
            v = r.get(f)
            if v is not None:
                buckets[key][f].append(v)

    periods = []
    for key in sorted(buckets.keys()):
        b = buckets[key]
        periods.append(
            {
                "period": key,
                "nights": len(b.get("daily_rmssd", [])),
                "avg_daily_rmssd": _avg(b.get("daily_rmssd", [])),
                "avg_deep_rmssd": _avg(b.get("deep_rmssd", [])),
            }
        )
    return {"periods": periods, "data_type": "hrv", "aggregation": period}


def _trend_azm(conn, start_date: str, end_date: str, period: str) -> dict:
    rows = db.query_azm(conn, start_date, end_date)
    if not rows:
        return {"message": "No AZM data in cache. No data recorded for this period."}

    buckets = defaultdict(lambda: defaultdict(list))
    for r in rows:
        key = _get_period_key(r["date"], period)
        for f in ["total_minutes", "fat_burn_minutes", "cardio_minutes", "peak_minutes"]:
            v = r.get(f)
            if v is not None:
                buckets[key][f].append(v)

    periods = []
    for key in sorted(buckets.keys()):
        b = buckets[key]
        total = b.get("total_minutes", [])
        periods.append(
            {
                "period": key,
                "days": len(total),
                "avg_total_azm": _avg(total),
                "total_azm": sum(total) if total else None,
                "avg_fat_burn_minutes": _avg(b.get("fat_burn_minutes", [])),
                "avg_cardio_minutes": _avg(b.get("cardio_minutes", [])),
                "avg_peak_minutes": _avg(b.get("peak_minutes", [])),
            }
        )
    return {"periods": periods, "data_type": "azm", "aggregation": period}


def _trend_breathing_rate(conn, start_date: str, end_date: str, period: str) -> dict:
    rows = db.query_breathing_rate(conn, start_date, end_date)
    if not rows:
        return {"message": "No breathing rate data in cache. No data recorded for this period."}

    buckets = defaultdict(list)
    for r in rows:
        v = r.get("breaths_per_min")
        if v is not None:
            buckets[_get_period_key(r["date"], period)].append(v)

    periods = []
    for key in sorted(buckets.keys()):
        vals = buckets[key]
        periods.append(
            {
                "period": key,
                "nights": len(vals),
                "avg_breaths_per_min": _avg(vals),
                "min_breaths_per_min": min(vals) if vals else None,
                "max_breaths_per_min": max(vals) if vals else None,
            }
        )
    return {"periods": periods, "data_type": "breathing_rate", "aggregation": period}


def _trend_skin_temperature(conn, start_date: str, end_date: str, period: str) -> dict:
    rows = db.query_skin_temperature(conn, start_date, end_date)
    if not rows:
        return {"message": "No skin temperature data in cache. No data recorded for this period."}

    buckets = defaultdict(list)
    for r in rows:
        v = r.get("nightly_relative")
        if v is not None:
            buckets[_get_period_key(r["date"], period)].append(v)

    periods = []
    for key in sorted(buckets.keys()):
        vals = buckets[key]
        periods.append(
            {
                "period": key,
                "nights": len(vals),
                "avg_nightly_relative": _avg(vals),
                "min_nightly_relative": min(vals) if vals else None,
                "max_nightly_relative": max(vals) if vals else None,
            }
        )
    return {"periods": periods, "data_type": "skin_temperature", "aggregation": period}


def _trend_core_temperature(conn, start_date: str, end_date: str, period: str) -> dict:
    rows = db.query_core_temperature(conn, start_date, end_date)
    if not rows:
        return {"message": "No core temperature data in cache. No data recorded for this period."}

    buckets = defaultdict(list)
    for r in rows:
        v = r.get("temp_celsius")
        if v is not None:
            buckets[_get_period_key(r["date"], period)].append(v)

    periods = []
    for key in sorted(buckets.keys()):
        vals = buckets[key]
        periods.append(
            {
                "period": key,
                "readings": len(vals),
                # Peak and fever-reading counts are the meaningful signals here:
                # core temps are logged by hand (usually only when ill), so the
                # sample is sparse and biased high - avg is included but should
                # not be read as a sustained baseline.
                "max_temp_celsius": max(vals) if vals else None,
                "readings_ge_38c": sum(1 for v in vals if v >= 38.0),
                "min_temp_celsius": min(vals) if vals else None,
                "avg_temp_celsius": _avg(vals),
            }
        )
    return {"periods": periods, "data_type": "core_temperature", "aggregation": period}


def _trend_cardio_fitness(conn, start_date: str, end_date: str, period: str) -> dict:
    """A reported band and a single value, counted separately.

    A period can hold both - an imported row carries the band, a synced one
    the single value. Averaging them into one series would report the change
    of definition as a change in the person, so each is counted and averaged
    on its own.
    """
    rows = db.query_cardio_fitness(conn, start_date, end_date)
    if not rows:
        return {"message": "No cardio fitness data in cache. No data recorded for this period."}

    buckets = defaultdict(lambda: defaultdict(list))
    for r in rows:
        key = _get_period_key(r["date"], period)
        for f in ["vo2_max_low", "vo2_max_high", "vo2_max"]:
            v = r.get(f)
            if v is not None:
                buckets[key][f].append(v)
        _count_row(buckets[key], "_count", r, ("vo2_max_low", "vo2_max_high", "vo2_max"))
        _count_row(buckets[key], "_range", r, ("vo2_max_low", "vo2_max_high"))

    periods = []
    for key in sorted(buckets.keys()):
        b = buckets[key]
        lows, highs = b.get("vo2_max_low", []), b.get("vo2_max_high", [])
        singles = b.get("vo2_max", [])
        periods.append(
            {
                "period": key,
                "readings": len(b.get("_count", [])),
                "readings_range": len(b.get("_range", [])),
                "avg_vo2_max_low": _avg(lows),
                "avg_vo2_max_high": _avg(highs),
                "readings_single_value": len(singles),
                "avg_vo2_max": _avg(singles),
            }
        )
    return {"periods": periods, "data_type": "cardio_fitness", "aggregation": period}


def _trend_food_log(conn, start_date: str, end_date: str, period: str) -> dict:
    rows = db.query_food_log(conn, start_date, end_date)
    if not rows:
        return {"message": "No food log data in cache. No data recorded for this period."}

    buckets = defaultdict(lambda: defaultdict(list))
    for r in rows:
        key = _get_period_key(r["date"], period)
        present = False
        for f in ["calories_in", "water_ml"]:
            v = r.get(f)
            if v is not None:
                buckets[key][f].append(v)
                present = True
        if present:
            buckets[key]["_count"].append(1)

    periods = []
    for key in sorted(buckets.keys()):
        b = buckets[key]
        cals = b.get("calories_in", [])
        water = b.get("water_ml", [])
        periods.append(
            {
                "period": key,
                # Days with either, not whichever list is longer: water with
                # no meal logged is the common shape, and a month holding
                # both would otherwise report only the meals.
                "days_logged": len(b.get("_count", [])),
                "avg_calories_in": _avg(cals),
                "avg_water_ml": _avg(water),
            }
        )
    return {"periods": periods, "data_type": "food_log", "aggregation": period}


#: What a trend can be asked for. Not every cached type has a daily series to
#: aggregate - ECG readings and rhythm alerts are episodes - and a message
#: built from the cache's own list offers types the dispatch below refuses.
_TREND_FNS = {
    "heart_rate": _trend_heart_rate,
    "activity": _trend_activity,
    "exercises": _trend_exercises,
    "sleep": _trend_sleep,
    "weight": _trend_weight,
    "spo2": _trend_spo2,
    "hrv": _trend_hrv,
    "azm": _trend_azm,
    "breathing_rate": _trend_breathing_rate,
    "skin_temperature": _trend_skin_temperature,
    "core_temperature": _trend_core_temperature,
    "cardio_fitness": _trend_cardio_fitness,
    "food_log": _trend_food_log,
}
#: Compare mode's own dispatch. It must hold exactly the same types as
#: _TREND_FNS, since one message offers both: dropping an entry here refuses a
#: type in the same sentence that recommends it.
_COMPARE_QUERY_FNS = {
    "heart_rate": db.query_heart_rate,
    "activity": db.query_activity,
    "exercises": db.query_exercises,
    "sleep": db.query_sleep,
    "weight": db.query_weight,
    "spo2": db.query_spo2,
    "hrv": db.query_hrv,
    "azm": db.query_azm,
    "breathing_rate": db.query_breathing_rate,
    "skin_temperature": db.query_skin_temperature,
    "core_temperature": db.query_core_temperature,
    "cardio_fitness": db.query_cardio_fitness,
    "food_log": db.query_food_log,
}
_VALID_TYPES = ", ".join(_TREND_FNS)


def _parse_compare_range(part: str) -> tuple[date, date] | None:
    today = date.today()
    m = re.match(r"last_(\d+)d", part)
    if m:
        days = int(m.group(1))
        return today - timedelta(days=days - 1), today
    m = re.match(r"previous_(\d+)d", part)
    if m:
        days = int(m.group(1))
        return today - timedelta(days=days * 2 - 1), today - timedelta(days=days)
    if re.match(r"^\d{4}-\d{2}$", part):
        year, month = int(part[:4]), int(part[5:7])
        start = date(year, month, 1)
        end = (
            date(year + 1, 1, 1) - timedelta(days=1)
            if month == 12
            else date(year, month + 1, 1) - timedelta(days=1)
        )
        return start, end
    m = re.match(r"^(\d{4})-Q([1-4])$", part)
    if m:
        year, q = int(m.group(1)), int(m.group(2))
        start = date(year, (q - 1) * 3 + 1, 1)
        end_month = q * 3
        end = (
            date(year + 1, 1, 1) - timedelta(days=1)
            if end_month == 12
            else date(year, end_month + 1, 1) - timedelta(days=1)
        )
        return start, end
    return None


def _compare_periods(conn, data_type: str, compare_str: str) -> dict:
    parts = re.split(r"\s+vs\s+", compare_str.strip(), maxsplit=1)
    if len(parts) != 2:
        return {
            "error": (
                "Invalid compare format. Use: 'last_30d vs previous_30d' or '2026-03 vs 2026-02'"
            )
        }

    ranges = []
    for part in parts:
        r = _parse_compare_range(part.strip())
        if r is None:
            return {
                "error": (
                    f"Cannot parse period '{part}'. Use: last_30d, "
                    "previous_30d, 2026-03, or 2026-Q1"
                )
            }
        ranges.append(r)

    query_fn = _COMPARE_QUERY_FNS.get(data_type)
    if not query_fn:
        return {"error": f"Cannot compare data_type '{data_type}'. Use: {_VALID_TYPES}."}

    def summarize(rows, dtype):
        if not rows:
            return {"count": 0}

        # Every column is read through one helper, so the filter is written
        # once. Thirteen copies of it needed only one to say `if r.get(f)` -
        # a rest day is 0 steps, and dropping it while still counting the row
        # averages a smaller total over the same number of days.
        def values(field):
            return [r[field] for r in rows if r.get(field) is not None]

        if dtype == "heart_rate":
            return {"count": len(rows), "avg_resting_hr": _avg(values("resting_hr"))}
        elif dtype == "activity":
            return {"count": len(rows), "avg_steps": _avg(values("steps"))}
        elif dtype == "exercises":
            return {
                "count": len(rows),
                "avg_duration": format_duration(_avg(values("duration_min"))),
            }
        elif dtype == "sleep":
            return {
                "count": len(rows),
                "avg_total_sleep": format_duration(_avg(values("total_minutes"))),
            }
        elif dtype == "weight":
            return {"count": len(rows), "avg_weight_kg": _avg(values("weight_kg"))}
        elif dtype == "spo2":
            return {"count": len(rows), "avg_spo2": _avg(values("avg"))}
        elif dtype == "hrv":
            return {"count": len(rows), "avg_daily_rmssd": _avg(values("daily_rmssd"))}
        elif dtype == "azm":
            azm = values("total_minutes")
            return {
                "count": len(rows),
                "avg_total_azm": _avg(azm),
                "total_azm": sum(azm) if azm else None,
            }
        elif dtype == "breathing_rate":
            return {"count": len(rows), "avg_breaths_per_min": _avg(values("breaths_per_min"))}
        elif dtype == "skin_temperature":
            return {"count": len(rows), "avg_nightly_relative": _avg(values("nightly_relative"))}
        elif dtype == "core_temperature":
            temps = values("temp_celsius")
            return {
                "count": len(rows),
                "avg_temp_celsius": _avg(temps),
                "max_temp_celsius": max(temps) if temps else None,
            }
        elif dtype == "cardio_fitness":
            return {
                "count": len(rows),
                "avg_vo2_max_low": _avg(values("vo2_max_low")),
                "avg_vo2_max_high": _avg(values("vo2_max_high")),
                "avg_vo2_max": _avg(values("vo2_max")),
            }
        elif dtype == "food_log":
            return {"count": len(rows), "avg_calories_in": _avg(values("calories_in"))}
        return {"count": len(rows)}

    period_a = query_fn(conn, ranges[0][0].isoformat(), ranges[0][1].isoformat())
    period_b = query_fn(conn, ranges[1][0].isoformat(), ranges[1][1].isoformat())
    result_a = summarize(period_a, data_type)
    result_b = summarize(period_b, data_type)
    result_a["period"] = f"{ranges[0][0]} to {ranges[0][1]}"
    result_b["period"] = f"{ranges[1][0]} to {ranges[1][1]}"
    return {"period_1": result_a, "period_2": result_b, "data_type": data_type}


@mcp.tool()
@require_auth
async def health_trends(
    data_type: str = "activity",
    period: str = "monthly",
    start_date: str | None = None,
    end_date: str | None = None,
    compare: str | None = None,
) -> str:
    """Analyse trends in cached health data.

    Computes averages and totals over time from the local cache,
    auto-syncing if stale.

    Args:
        data_type: What to analyse. Options: "heart_rate", "activity",
            "exercises", "sleep", "weight", "spo2", "hrv", "azm",
            "breathing_rate", "skin_temperature", "core_temperature",
            "cardio_fitness", "food_log". Default: "activity".
        period: Aggregation period. Options: "weekly", "monthly",
            "quarterly". Default: "monthly".
        start_date: Start date as "YYYY-MM-DD" or "365d". Default: last 12 months.
        end_date: End date as "YYYY-MM-DD". Default: today.
        compare: Compare two periods. Format: "last_30d vs previous_30d",
            "2026-03 vs 2026-02", "2026-Q1 vs 2025-Q4".
            When set, period/start_date/end_date are ignored.

    Returns aggregated averages per period. For activity: steps, distance,
    active minutes. For exercises: sessions, duration, calories.
    For sleep: duration, efficiency, stage breakdown.
    For heart_rate: resting HR min/avg/max. For weight: weight, fat%, BMI.
    For hrv: daily and deep RMSSD.

    Two data types report their history under two definitions, each with the
    count of readings behind it - never merge them into one series. For spo2,
    min_spo2/max_spo2 are observed nightly extremes, while
    avg_nightly_ci_low/avg_nightly_ci_high average the nightly bounds of a
    confidence interval on each night's own average. For cardio_fitness,
    avg_vo2_max_low/high average a reported band while avg_vo2_max averages a
    single value.

    Those per-definition fields and their counts appear in the period form
    only. `compare=` reports spo2 as avg_spo2 alone, which is deliberate and
    not a gap: that column means the same thing under both definitions, so it
    is the one figure two windows either side of the switchover can be
    compared on. Ask for the period form when you need to know which
    definition is behind a number. cardio_fitness has no such neutral column,
    so `compare=` carries both definitions there.
    Not for raw data - use health_get_* tools instead.
    """

    def _analyse():
        auto_sync_if_stale(data_type)
        conn = db.get_db()
        try:
            if compare:
                return _compare_periods(conn, data_type, compare)

            start, end = parse_date(start_date, end_date, default_days=365)
            s, e = start.isoformat(), end.isoformat()

            fn = _TREND_FNS.get(data_type)
            if fn:
                return fn(conn, s, e, period)
            return {"error": f"Unknown data_type '{data_type}'. Use: {_VALID_TYPES}."}
        finally:
            conn.close()

    result = await anyio.to_thread.run_sync(_analyse)
    return format_response(result)
