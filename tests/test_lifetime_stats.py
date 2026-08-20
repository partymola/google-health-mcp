"""Lifetime totals and personal bests, read from the cache rather than fetched.

The API has no endpoint for it, so the question is answered from the
`activity` table - which means the answer is bounded by what has been synced,
and the coverage it reports is not decoration: without it a caller cannot tell
an all-time total from a total over whatever happens to be cached.
"""

from google_health_mcp import db
from google_health_mcp.tools.lifetime_stats_tools import lifetime_from_cache


def _day(conn, date, **columns):
    db.save_activity(conn, {"date": date, **columns})


class TestTheTotals:
    def test_totals_sum_every_cached_day(self, tmp_db):
        _day(tmp_db, "2026-03-10", steps=1000, floors=2, distance_km=0.8, calories_out=2000)
        _day(tmp_db, "2026-03-11", steps=1500, floors=3, distance_km=1.2, calories_out=2100)
        tmp_db.commit()
        result = lifetime_from_cache(tmp_db)
        assert result["totals"] == {
            "steps": 2500,
            "floors": 5,
            "distance_km": 2.0,
            "calories_out": 4100,
        }

    def test_a_missing_measurement_does_not_become_a_zero(self, tmp_db):
        """A day with no floors is not a day with no floors climbed.

        SUM ignores NULL, which is right, but the day must still count toward
        coverage - otherwise a sparse column silently shrinks the span.
        """
        _day(tmp_db, "2026-03-10", steps=1000, floors=2)
        _day(tmp_db, "2026-03-11", steps=1500)
        tmp_db.commit()
        result = lifetime_from_cache(tmp_db)
        assert result["totals"]["floors"] == 2
        assert result["coverage"]["days"] == 2

    def test_an_empty_cache_answers_rather_than_raising(self, tmp_db):
        result = lifetime_from_cache(tmp_db)
        assert result["totals"]["steps"] == 0
        assert result["coverage"]["days"] == 0
        assert result["best"]["steps"] is None


class TestThePersonalBests:
    def test_the_best_day_carries_its_date(self, tmp_db):
        _day(tmp_db, "2026-03-10", steps=1000)
        _day(tmp_db, "2026-03-11", steps=9000)
        _day(tmp_db, "2026-03-12", steps=1500)
        tmp_db.commit()
        best = lifetime_from_cache(tmp_db)["best"]
        assert best["steps"] == {"value": 9000, "date": "2026-03-11"}

    def test_a_null_day_is_never_the_best_day(self, tmp_db):
        _day(tmp_db, "2026-03-10", steps=1000, floors=None)
        _day(tmp_db, "2026-03-11", steps=2000, floors=4)
        tmp_db.commit()
        assert lifetime_from_cache(tmp_db)["best"]["floors"] == {"value": 4, "date": "2026-03-11"}

    def test_an_entirely_unmeasured_column_has_no_record(self, tmp_db):
        """The case the IS NOT NULL filter exists for, and the only one.

        NULL sorts last under DESC, so a mixed column needs no help. A column
        nothing ever measured returns its one row anyway, and reporting
        {"value": None, "date": "2026-03-10"} reads as a record that was set.
        """
        _day(tmp_db, "2026-03-10", steps=1000)
        tmp_db.commit()
        assert lifetime_from_cache(tmp_db)["best"]["floors"] is None

    def test_a_tie_resolves_to_the_earlier_day(self, tmp_db):
        """Deterministic, so the same cache never reports two different records."""
        _day(tmp_db, "2026-03-11", steps=5000)
        _day(tmp_db, "2026-03-10", steps=5000)
        tmp_db.commit()
        assert lifetime_from_cache(tmp_db)["best"]["steps"]["date"] == "2026-03-10"


class TestWhatTheAnswerIsBoundedBy:
    def test_coverage_reports_the_span_the_totals_cover(self, tmp_db):
        _day(tmp_db, "2026-03-10", steps=1000)
        _day(tmp_db, "2026-03-14", steps=1000)
        tmp_db.commit()
        coverage = lifetime_from_cache(tmp_db)["coverage"]
        assert coverage["first_date"] == "2026-03-10"
        assert coverage["last_date"] == "2026-03-14"
        assert coverage["days"] == 2, "days counts rows held, not the calendar span"
