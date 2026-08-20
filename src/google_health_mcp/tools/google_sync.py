"""Normalise Google Health data points into the cache's tables.

Three things about this API make a wrong number easy and a loud failure rare,
and every helper here exists for one of them: integers arrive as JSON strings,
a quantity that was not measured is absent rather than zero, and several
values carry a different definition from the one the same column holds for a
row that arrived by import.
"""

import json
import logging
from datetime import datetime, timedelta

from .. import api, db

logger = logging.getLogger(__name__)

#: Written into every row this module stores. The provider of the most recent
#: write, not of the row - the upsert keeps columns a writer does not name.
PROVIDER = "google"


def _number(value, cast):
    """A JSON value as a number, or None if it is not one.

    int64 arrives as a string, so every integer needs converting and a
    forgotten conversion concatenates instead of adding. Anything unparseable
    is absent rather than zero: a value we cannot read is not a measurement
    of nothing.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return cast(value)
    except (TypeError, ValueError):
        return None


def _date(payload: dict) -> str | None:
    """The day a daily point belongs to, from its own `date` field.

    Taken from the point rather than the requested window: binning a month's
    points onto the day we asked for would be silent and wrong.
    """
    parts = (payload or {}).get("date") or {}
    try:
        return f"{parts['year']:04d}-{parts['month']:02d}-{parts['day']:02d}"
    except (KeyError, TypeError, ValueError):
        return None


def _window(start, end):
    """The half-open window a Google filter needs, from the loop's inclusive one.

    The sync loop hands every handler an inclusive end date; Google's ranges
    are closed-open, so passing it through would silently drop the most recent
    day - every run, invisibly, since the day arrives eventually anyway once
    it stops being the last one.
    """
    return start, end + timedelta(days=1)


def _daily_rows(data_type: str, start, end, field: str, build):
    """Every daily point in the window, as (date, row) pairs ready to store.

    `build` returns the columns for one point, or None to skip it. A point
    with no date of its own is skipped rather than guessed at.
    """
    for point in api.list_google_data_points(data_type, *_window(start, end)):
        payload = point.get(field)
        if not isinstance(payload, dict):
            continue
        day = _date(payload)
        if day is None:
            logger.debug("%s point carried no date", data_type)
            continue
        columns = build(payload)
        if columns is None:
            continue
        # A column Google did not report for this day is omitted, not written
        # as None. The upsert treats an explicit None as "withdraw the stored
        # value", so naming every column unconditionally would clear whatever
        # an import left on days Google is quieter about.
        measured = {name: value for name, value in columns.items() if value is not None}
        if not measured:
            continue
        yield {"date": day, "provider": PROVIDER, **measured}


def sync_heart_rate(conn, start, end) -> int:
    """Resting heart rate.

    Written through the upsert rather than through `save_heart_rate`, which
    always names `zones`: that helper would put an empty list over whatever
    zones the day already holds. Zones are stitched from three separate data
    types and are not here yet.
    """
    count = 0
    for row in _daily_rows(
        "daily-resting-heart-rate",
        start,
        end,
        "dailyRestingHeartRate",
        lambda p: {"resting_hr": _number(p.get("beatsPerMinute"), int)},
    ):
        db.save_heart_rate_row(conn, row)
        count += 1
    conn.commit()
    return count


def sync_spo2(conn, start, end) -> int:
    """Nightly oxygen saturation.

    The bounds are a confidence interval on the average, not the night's
    observed extremes, so they go to their own columns. `min`/`max` hold the
    extremes an imported row carries and are left untouched - a trend
    averaging the two definitions together reports the change as physiology.
    """
    count = 0
    for row in _daily_rows(
        "daily-oxygen-saturation",
        start,
        end,
        "dailyOxygenSaturation",
        lambda p: {
            "avg": _number(p.get("averagePercentage"), float),
            "avg_ci_low": _number(p.get("lowerBoundPercentage"), float),
            "avg_ci_high": _number(p.get("upperBoundPercentage"), float),
        },
    ):
        db.save_spo2(conn, row)
        count += 1
    conn.commit()
    return count


def sync_hrv(conn, start, end) -> int:
    count = 0
    for row in _daily_rows(
        "daily-heart-rate-variability",
        start,
        end,
        "dailyHeartRateVariability",
        lambda p: {
            "daily_rmssd": _number(p.get("averageHeartRateVariabilityMilliseconds"), float),
            "deep_rmssd": _number(
                p.get("deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds"), float
            ),
        },
    ):
        db.save_hrv(conn, row)
        count += 1
    conn.commit()
    return count


def sync_breathing_rate(conn, start, end) -> int:
    count = 0
    for row in _daily_rows(
        "daily-respiratory-rate",
        start,
        end,
        "dailyRespiratoryRate",
        lambda p: {"breaths_per_min": _number(p.get("breathsPerMinute"), float)},
    ):
        db.save_breathing_rate(conn, row)
        count += 1
    conn.commit()
    return count


def sync_skin_temperature(conn, start, end) -> int:
    """Nightly skin temperature, as a delta and as the two absolutes.

    `nightly_relative` is the delta from the personal baseline, which is the
    quantity an imported row holds; Google reports the two absolutes instead,
    so one subtraction keeps the column meaning one thing. Both absolutes are
    stored beside it because they are strictly more information. Without a
    baseline there is no delta - and no delta is not a drift of zero.
    """

    def build(payload):
        nightly = _number(payload.get("nightlyTemperatureCelsius"), float)
        baseline = _number(payload.get("baselineTemperatureCelsius"), float)
        # Rounded to the precision the column already holds: 41.5 - 41.4 is
        # 0.10000000000000142 in binary, a precision neither reading had.
        relative = None if nightly is None or baseline is None else round(nightly - baseline, 4)
        return {
            "nightly_absolute": nightly,
            "baseline": baseline,
            "nightly_relative": relative,
        }

    count = 0
    for row in _daily_rows(
        "daily-sleep-temperature-derivations", start, end, "dailySleepTemperatureDerivations", build
    ):
        db.save_skin_temperature(conn, row)
        count += 1
    conn.commit()
    return count


def _rollup_date(point: dict) -> str | None:
    """The day a rollup point covers, from `civilStartTime`.

    A rollup answers under a different key from a daily list point, and its
    date sits one level further in - the same shape of difference that makes
    `rollupDataPoints` easy to read as `dataPoints`.
    """
    return _date((point or {}).get("civilStartTime") or {})


#: The only quantities where nothing measured and zero are the same thing.
#: A day can genuinely have no steps and no floors climbed; it cannot
#: genuinely have no heart rate. Naming them means adding a type forces the
#: decision rather than inheriting whichever default was nearest.
#:
#: Google's own list, from
#: developers.google.com/health/data-presence-and-true-zeros: altitude,
#: distance, floors, steps, total calories.
_TRUE_ZERO_TYPES = frozenset({"steps", "distance", "floors", "total-calories", "altitude"})


def _rollup_values(data_type: str, start, end, field: str, extract) -> dict:
    """Map of day to extracted value for one rollup type.

    Three states, not two. No point for a day means the day is unknown and it
    is absent from the map. A point present whose value field is omitted means
    a measured zero - but only for the quantities where zero is a real reading;
    for anything else it is still absence, because a heart rate of nothing is
    not a heart rate of zero.

    Google documents the omitted field for `list`, and says a rollup reports a
    true zero as an explicit 0 instead - so the last branch covers a shape the
    reference does not describe, and reads it the way `list` would.
    """
    values = {}
    for point in api.daily_roll_up(data_type, *_window(start, end)):
        day = _rollup_date(point)
        payload = point.get(field)
        if day is None or not isinstance(payload, dict):
            continue
        extracted = extract(payload)
        if extracted is not None:
            values[day] = extracted
        elif data_type in _TRUE_ZERO_TYPES:
            values[day] = 0
    return values


#: The four activity levels, kept for the day a derivation exists. Google's
#: own vocabulary matches the columns exactly - SEDENTARY / LIGHTLY_ACTIVE /
#: MODERATELY_ACTIVE / VERY_ACTIVE - which is what made this look easy.
_ACTIVITY_LEVEL_COLUMNS = {
    "VERY_ACTIVE": "very_active_minutes",
    "MODERATELY_ACTIVE": "fairly_active_minutes",
    "LIGHTLY_ACTIVE": "lightly_active_minutes",
    "SEDENTARY": "sedentary_minutes",
}


def sync_activity(conn, start, end) -> int:
    """A day of activity, assembled from four rollups.

    Each source is independent, so a day present in one and missing from
    another still gets a row carrying what is known - the upsert leaves the
    columns this writer does not name.

    `calories_out` comes from `total-calories` and never from adding active
    and basal energy: basal returns nothing at all on a real account, so that
    sum would silently drop the larger half of the number.

    **The five minute columns are deliberately not written**, `active_minutes`
    among them: it was a derivation of two of the other four, so it goes with
    them. Google's
    `activity-level` vocabulary matches them exactly, which is what made it
    look straightforward, but no reading of it tiles a day. Measured on one
    real day: listed raw it returns 1565 minute-long segments for a
    1440-minute day, because `list` is the per-source view and the sources
    overlap; reconciled it returns 591, which covers under ten hours. The type
    has no daily rollup - the API says so plainly, whatever the discovery
    document's value union implies - so there is nothing left that carries
    Google's own reconciliation. De-duplicating the raw stream ourselves would
    mean inventing a precedence rule between sources and presenting it as
    Google's number, and deriving sedentary as the day minus the rest would
    rest on the four levels partitioning a day, which they measurably do not.

    So they stay NULL, as `sleep.efficiency` and `weight.bmi` already do, and
    an imported row's values beside them are left untouched.
    """
    steps = _rollup_values("steps", start, end, "steps", lambda p: _number(p.get("countSum"), int))
    distance = _rollup_values(
        "distance",
        start,
        end,
        "distance",
        lambda p: (
            None
            if (mm := _number(p.get("millimetersSum"), float)) is None
            else round(mm / 1_000_000, 6)
        ),
    )
    floors = _rollup_values(
        "floors", start, end, "floors", lambda p: _number(p.get("countSum"), int)
    )
    calories = _rollup_values(
        "total-calories",
        start,
        end,
        "totalCalories",
        lambda p: None if (k := _number(p.get("kcalSum"), float)) is None else int(k),
    )
    count = 0
    for day in sorted(set(steps) | set(distance) | set(floors) | set(calories)):
        row = {"date": day, "provider": PROVIDER}
        for source, column in (
            (steps, "steps"),
            (distance, "distance_km"),
            (floors, "floors"),
            (calories, "calories_out"),
        ):
            if day in source:
                row[column] = source[day]
        db.save_activity(conn, row)
        count += 1
    conn.commit()
    return count


def sync_azm(conn, start, end) -> int:
    """Active zone minutes. The total is the sum of the three zones."""
    zones = (
        ("sumInFatBurnHeartZone", "fat_burn_minutes"),
        ("sumInCardioHeartZone", "cardio_minutes"),
        ("sumInPeakHeartZone", "peak_minutes"),
    )
    count = 0
    for point in api.daily_roll_up("active-zone-minutes", *_window(start, end)):
        day = _rollup_date(point)
        payload = point.get("activeZoneMinutes")
        if day is None or not isinstance(payload, dict):
            continue
        measured = {
            column: value
            for field, column in zones
            if (value := _number(payload.get(field), int)) is not None
        }
        # A point carrying no zone at all is not a day of no active minutes.
        # Writing the row anyway inserts a day of NULLs that health_get_azm
        # returns as an entry and compare mode counts, and stamps `provider`
        # over whatever wrote the day's values.
        if not measured:
            continue
        row = {
            "date": day,
            "provider": PROVIDER,
            **measured,
            "total_minutes": sum(measured.values()),
        }
        db.save_azm(conn, row)
        count += 1
    conn.commit()
    return count


_SLEEP_STAGE_COLUMNS = {
    "DEEP": "deep_minutes",
    "LIGHT": "light_minutes",
    "REM": "rem_minutes",
    "AWAKE": "wake_minutes",
}


def _local_date(timestamp: str | None, offset: str | None) -> str | None:
    """The calendar date a UTC timestamp falls on in the user's own time.

    Sleep is binned by when a night ended locally, and the response carries no
    civil end time - only the UTC instant and an offset like "3600s". Ignoring
    the offset moves every night that ends just after local midnight back a
    day, which is a whole night in the wrong row.
    """
    if not timestamp:
        return None
    try:
        moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    # An unreadable offset is not zero: defaulting to UTC moves a whole night
    # into the wrong day, silently. Absent is treated as zero because that is
    # what the field means when the instant is already UTC.
    seconds = 0 if offset is None else _number(str(offset).rstrip("s"), int)
    if seconds is None:
        return None
    return (moment + timedelta(seconds=seconds)).date().isoformat()


def sync_sleep(conn, start, end) -> int:
    """One row a night, aggregating every session that night was recorded as.

    A fragmented night - a wake-and-resume, or a main sleep plus a nap -
    arrives as several sessions sharing an end date. The table is keyed by
    date, so without aggregation the last session written replaces the night
    with its shortest piece. Summing them is also what an imported row holds,
    which keeps a series comparable across the two.

    `efficiency` is never written: nothing Google reports corresponds to it,
    and a lookalike computed here would sit in a column beside values that
    mean something else. The column keeps whatever it already holds.
    """
    nights: dict[str, dict] = {}
    for point in api.list_google_data_points("sleep", *_window(start, end)):
        payload = point.get("sleep")
        if not isinstance(payload, dict):
            continue
        interval = payload.get("interval") or {}
        night = _local_date(interval.get("endTime"), interval.get("endUtcOffset"))
        if night is None:
            continue
        summary = payload.get("summary") or {}

        accumulated = nights.setdefault(night, {"date": night, "provider": PROVIDER, "sessions": 0})
        accumulated["sessions"] += 1
        for field, column in (
            ("minutesAsleep", "total_minutes"),
            ("minutesInSleepPeriod", "sleep_period_minutes"),
        ):
            value = _number(summary.get(field), int)
            if value is not None:
                accumulated[column] = accumulated.get(column, 0) + value
        for stage in summary.get("stagesSummary") or []:
            column = _SLEEP_STAGE_COLUMNS.get(stage.get("type"))
            minutes = _number(stage.get("minutes"), int)
            if column and minutes is not None:
                accumulated[column] = accumulated.get(column, 0) + minutes

        started, ended = interval.get("startTime"), interval.get("endTime")
        if started and (accumulated.get("start_time") or started) >= started:
            accumulated["start_time"] = started
        if ended and (accumulated.get("end_time") or ended) <= ended:
            accumulated["end_time"] = ended

    for row in nights.values():
        db.save_sleep(conn, row)
    conn.commit()
    return len(nights)


def _civil(payload: dict) -> tuple[str | None, str | None]:
    """The local date and timestamp a sample was taken at.

    A sample carries the moment rather than a day, and `civilTime` is already
    the user's own clock - so the date comes from there rather than from the
    UTC `physicalTime`, which would move an early-morning reading to the day
    before.
    """
    civil = (payload.get("sampleTime") or {}).get("civilTime") or {}
    day = _date(civil)
    if day is None:
        return None, None
    clock = civil.get("time") or {}
    stamp = "{}T{:02d}:{:02d}:{:02d}".format(
        day,
        clock.get("hours", 0) or 0,
        clock.get("minutes", 0) or 0,
        clock.get("seconds", 0) or 0,
    )
    return day, stamp


def _samples(data_type: str, field: str, start, end):
    for point in api.list_google_data_points(data_type, *_window(start, end)):
        payload = point.get(field)
        if not isinstance(payload, dict):
            continue
        day, stamp = _civil(payload)
        if day is not None:
            yield day, stamp, payload


def sync_weight(conn, start, end) -> int:
    """Weight and body fat: two data types, one row a day.

    They are recorded by the same event on the scales, so a reading of each
    belongs in one row. `bmi` is never written - Google reports height and
    weight but no BMI, and computing one would put a derived value in a
    column holding measured ones.
    """
    days: dict[str, dict] = {}
    for day, _stamp, payload in _samples("weight", "weight", start, end):
        grams = _number(payload.get("weightGrams"), float)
        if grams is not None:
            days.setdefault(day, {"date": day, "provider": PROVIDER})["weight_kg"] = grams / 1000
    for day, _stamp, payload in _samples("body-fat", "bodyFat", start, end):
        percentage = _number(payload.get("percentage"), float)
        if percentage is not None:
            days.setdefault(day, {"date": day, "provider": PROVIDER})["fat_pct"] = percentage

    for row in days.values():
        db.save_weight(conn, row)
    conn.commit()
    return len(days)


def sync_core_temperature(conn, start, end) -> int:
    """Manually logged body temperature, keyed by timestamp and value.

    Google gives these an `id`, which the table does not key on: keying on
    (timestamp, value) keeps a re-synced day idempotent against rows that
    arrived without one. Adopting the id would be a migration rather than a
    normaliser decision.
    """
    count = 0
    for day, stamp, payload in _samples("core-body-temperature", "coreBodyTemperature", start, end):
        celsius = _number(payload.get("temperatureCelsius"), float)
        if celsius is None:
            continue
        count += db.save_core_temperature(
            conn, {"datetime": stamp, "date": day, "temp_celsius": celsius}
        )
    conn.commit()
    return count


def _seconds(duration: str | None) -> float | None:
    """A protobuf duration like "1576.800s" as a number of seconds."""
    if not isinstance(duration, str) or not duration.endswith("s"):
        return None
    return _number(duration[:-1], float)


def _days_another_provider_recorded(conn) -> set[str]:
    """Days whose workouts belong to an earlier provider, and are left to it.

    An imported workout carries whatever identifier its source used, and
    Google keys a workout by a resource name, so the same session arrives
    under a key that inserts rather than corrects - and a backfill over an
    imported history would hold every workout twice. Nothing stored can tell
    the pair apart either: (start_time, duration, name) collides across the
    Strava mirrors of watch-tracked workouts, measured on real data, so
    deduping on it would delete rows that are genuinely distinct.

    Per day rather than a cut-off date, so a day the earlier provider never
    recorded is still filled. A day this provider wrote is not in the set, so
    re-syncing a window still corrects its own rows.
    """
    return {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT date FROM exercises WHERE provider IS NULL OR provider != ?",
            (PROVIDER,),
        )
    }


def sync_exercises(conn, start, end) -> int:
    """Workouts, keyed by the resource name Google gives each one.

    A workout with no identifier is skipped rather than stored under an empty
    key, where each one would overwrite the last.
    """
    covered = _days_another_provider_recorded(conn)
    skipped = 0
    count = 0
    for point in api.list_google_data_points("exercise", *_window(start, end)):
        payload = point.get("exercise")
        identifier = point.get("name")
        if not isinstance(payload, dict) or not identifier:
            continue
        interval = payload.get("interval") or {}
        day = _local_date(interval.get("startTime"), interval.get("startUtcOffset"))
        if day is None:
            continue
        if day in covered:
            skipped += 1
            continue

        metrics = payload.get("metricsSummary") or {}
        seconds = _seconds(payload.get("activeDuration"))
        millimetres = _number(metrics.get("distanceMillimeters"), float)
        row = {
            "date": day,
            "name": payload.get("displayName") or payload.get("exerciseType"),
            "duration_min": None if seconds is None else round(seconds / 60),
            "calories": _number(metrics.get("caloriesKcal"), int),
            "avg_hr": _number(metrics.get("averageHeartRateBeatsPerMinute"), int),
            "steps": _number(metrics.get("steps"), int),
            "distance_km": None if millimetres is None else round(millimetres / 1_000_000, 6),
            "start_time": interval.get("startTime"),
            "source": ((point.get("dataSource") or {}).get("device") or {}).get("displayName"),
            "log_type": payload.get("exerciseType"),
            "provider": PROVIDER,
        }
        db.save_exercise(conn, identifier, row)
        count += 1
    conn.commit()
    if skipped:
        logger.info("left %d workout(s) to the provider that already recorded their day", skipped)
    return count


def sync_ecg(conn, start, end) -> int:
    """Electrocardiograms, stored whole.

    The waveform is kept with the two numbers that make it readable - the
    sampling frequency and the millivolt scaling factor - because without
    either it is an array of meaningless integers.

    Duration is stored rather than derived on read: the interval carries only
    a start time, so a reading's length is samples over frequency and nothing
    downstream should have to know that. Without a frequency there is no
    duration, and dividing by an absent rate would invent one.
    """
    count = 0
    for point in api.list_google_data_points("electrocardiogram", *_window(start, end)):
        reading = point.get("electrocardiogram")
        identifier = point.get("name")
        if not isinstance(reading, dict) or not identifier:
            continue
        interval = reading.get("interval") or {}
        day = _local_date(interval.get("startTime"), interval.get("startUtcOffset"))
        if day is None:
            continue

        samples = reading.get("waveformSamples")
        hertz = _number(reading.get("samplingFrequencyHertz"), int)
        duration = None
        if isinstance(samples, list) and hertz:
            duration = round(len(samples) / hertz, 3)

        db.save_ecg(
            conn,
            {
                "reading_id": identifier,
                "date": day,
                "start_time": interval.get("startTime"),
                "avg_bpm": _number(reading.get("beatsPerMinuteAvg"), int),
                "classification": reading.get("resultClassification"),
                "lead_number": _number(reading.get("leadNumber"), int),
                "sampling_hz": hertz,
                "scaling_factor": _number(reading.get("millivoltsScalingFactor"), int),
                "duration_sec": duration,
                "waveform": None if samples is None else json.dumps(list(samples)),
                "device_model": (reading.get("medicalDeviceInfo") or {}).get("deviceModel"),
                "provider": PROVIDER,
            },
        )
        count += 1
    conn.commit()
    return count


def sync_irn(conn, start, end) -> int:
    """Irregular-rhythm notifications.

    The only mapping here never checked against real data: the account this
    was built against has never had an alert, so the shape follows the
    published schema alone.
    """
    count = 0
    for point in api.list_google_data_points("irregular-rhythm-notification", *_window(start, end)):
        alert = point.get("irregularRhythmNotification")
        identifier = point.get("name")
        if not isinstance(alert, dict) or not identifier:
            continue
        interval = alert.get("interval") or {}
        day = _local_date(interval.get("startTime"), interval.get("startUtcOffset"))
        if day is None:
            continue
        windows = alert.get("alertWindows")
        db.save_irn(
            conn,
            {
                "alert_id": identifier,
                "date": day,
                "start_time": interval.get("startTime"),
                "end_time": interval.get("endTime"),
                "alert_windows": None if windows is None else json.dumps(windows),
                "device_model": (alert.get("medicalDeviceInfo") or {}).get("deviceModel"),
                "provider": PROVIDER,
            },
        )
        count += 1
    conn.commit()
    return count


def sync_cardio_fitness(conn, start, end) -> int:
    """Cardio fitness, as one value rather than the range the column also holds.

    `vo2_max_low` and `vo2_max_high` are never written. Google reports a single
    figure where the band columns hold a reported range, so writing it into
    both ends would collapse a real four-wide range to a point and store the
    difference between two definitions as though it were a measurement.

    Unverified against live data: this type returns nothing on the account the
    normalisers were built against, so the field name follows `DailyVO2Max` in
    the discovery document rather than a reading.
    """
    count = 0
    for row in _daily_rows(
        "daily-vo2-max",
        start,
        end,
        "dailyVo2Max",
        lambda p: {"vo2_max": _number(p.get("vo2Max"), float)},
    ):
        db.save_cardio_fitness(conn, row)
        count += 1
    conn.commit()
    return count


def sync_food_log(conn, start, end) -> int:
    """A day's intake and hydration, from two independent rollups.

    Rolled up rather than listed, though both types answer either way: a list
    returns one point per logged entry, so a day's figure would end up being
    whichever apple was logged last.

    The two sources are independent, so a day carrying only one of them writes
    only that column and leaves the other as it stands.
    """
    calories = _rollup_values(
        "nutrition-log",
        start,
        end,
        "nutritionLog",
        lambda p: (
            None
            if (kcal := _number((p.get("energy") or {}).get("kcalSum"), float)) is None
            else int(kcal)
        ),
    )
    water = _rollup_values(
        "hydration-log",
        start,
        end,
        "hydrationLog",
        lambda p: (
            None
            if (ml := _number((p.get("amountConsumed") or {}).get("millilitersSum"), float)) is None
            else int(ml)
        ),
    )
    count = 0
    for day in sorted(set(calories) | set(water)):
        row = {"date": day, "provider": PROVIDER}
        for source, column in ((calories, "calories_in"), (water, "water_ml")):
            if day in source:
                row[column] = source[day]
        db.save_food_log(conn, row)
        count += 1
    conn.commit()
    return count


#: What the sync loop calls for each cached type. Taken as an argument by
#: `run_sync` rather than read inside it, so a caller can sync a subset into a
#: copy of the database without the loop knowing anything about providers.
GOOGLE_SYNC_HANDLERS = {
    "heart_rate": sync_heart_rate,
    "spo2": sync_spo2,
    "hrv": sync_hrv,
    "breathing_rate": sync_breathing_rate,
    "skin_temperature": sync_skin_temperature,
    "activity": sync_activity,
    "azm": sync_azm,
    "sleep": sync_sleep,
    "weight": sync_weight,
    "core_temperature": sync_core_temperature,
    "exercises": sync_exercises,
    "ecg": sync_ecg,
    "irn": sync_irn,
    "cardio_fitness": sync_cardio_fitness,
    "food_log": sync_food_log,
}
