"""Tests for helpers module: date parsing, formatting, response formatting."""

from datetime import date, timedelta

import pytest

from google_health_mcp import helpers
from google_health_mcp.errors import InvalidDateError
from google_health_mcp.helpers import format_duration, format_response, parse_date


class TestParseDate:
    """Tests for the flexible date parser."""

    def test_defaults_30_days(self):
        start, end = parse_date(None, None, default_days=30)
        assert end == date.today()
        assert start == date.today() - timedelta(days=30)

    def test_defaults_custom_days(self):
        start, end = parse_date(None, None, default_days=7)
        assert start == date.today() - timedelta(days=7)

    def test_iso_date(self):
        start, end = parse_date("2026-01-15", "2026-02-20")
        assert start == date(2026, 1, 15)
        assert end == date(2026, 2, 20)

    def test_relative_start(self):
        start, end = parse_date("7d", None)
        assert start == date.today() - timedelta(days=7)
        assert end == date.today()

    def test_relative_both(self):
        start, end = parse_date("30d", "7d")
        assert start == date.today() - timedelta(days=30)
        assert end == date.today() - timedelta(days=7)

    def test_month_start(self):
        start, _ = parse_date("2026-03", None)
        assert start == date(2026, 3, 1)

    def test_month_end(self):
        _, end = parse_date(None, "2026-03")
        assert end == date(2026, 3, 31)

    def test_month_end_february_leap(self):
        _, end = parse_date(None, "2028-02")
        assert end == date(2028, 2, 29)

    def test_month_end_february_non_leap(self):
        _, end = parse_date(None, "2027-02")
        assert end == date(2027, 2, 28)

    def test_month_end_december(self):
        _, end = parse_date(None, "2026-12")
        assert end == date(2026, 12, 31)

    def test_month_start_january(self):
        start, _ = parse_date("2026-01", None)
        assert start == date(2026, 1, 1)

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError, match="Invalid date"):
            parse_date("not-a-date", None)

    def test_invalid_relative_raises(self):
        with pytest.raises(ValueError, match="Invalid date"):
            parse_date("30x", None)

    def test_a_start_after_the_end_is_refused_naming_both(self):
        with pytest.raises(InvalidDateError) as caught:
            parse_date("2026-03-02", "2026-03-01")
        assert str(caught.value) == (
            "Start date 2026-03-02 is after end date 2026-03-01. "
            "Use a start on or before that date."
        )

    def test_a_start_after_a_defaulted_end_calls_it_today(self):
        tomorrow = date.today() + timedelta(days=1)
        with pytest.raises(InvalidDateError, match=rf"after today \({date.today()}\)"):
            parse_date(tomorrow.isoformat())

    def test_a_start_on_the_end_date_is_accepted(self):
        assert parse_date("2026-03-01", "2026-03-01") == (date(2026, 3, 1), date(2026, 3, 1))

    def test_a_missing_start_counts_back_from_a_past_end_date(self):
        # From today it would land after the end and be refused, though the
        # caller sent no start.
        assert parse_date(None, "2020-01", default_days=30) == (date(2020, 1, 1), date(2020, 1, 31))

    def test_a_missing_start_before_a_future_end_counts_back_from_today(self):
        # From the end it would land after today and answer as no data.
        end = date.today() + timedelta(days=40)
        assert parse_date(None, end.isoformat(), default_days=30) == (
            date.today() - timedelta(days=30),
            end,
        )

    def test_a_default_reaching_past_year_one_stops_there(self):
        assert parse_date(None, "0001-01-05", default_days=30) == (date(1, 1, 1), date(1, 1, 5))

    def test_the_last_month_the_calendar_holds_ends_on_its_last_day(self):
        assert parse_date("2026-01-01", "9999-12")[1] == date(9999, 12, 31)

    @pytest.mark.parametrize("value", ["2026-13", "2026-00", "2026-02-30", "99999999d"])
    @pytest.mark.parametrize("position", ["start", "end"])
    def test_a_date_of_the_right_shape_that_does_not_exist_is_refused(self, value, position):
        args = (value, None) if position == "start" else (None, value)
        with pytest.raises(InvalidDateError, match="YYYY-MM-DD"):
            parse_date(*args)

    @pytest.mark.parametrize("value", ["30d\n", "2026-03\n", "2026-03-01\n"])
    def test_a_trailing_newline_is_not_part_of_any_format(self, value):
        with pytest.raises(InvalidDateError):
            parse_date(value)

    @pytest.mark.parametrize(
        ("start", "end", "expected"),
        [
            ("0d", None, (date(2026, 3, 1), date(2026, 3, 1))),
            ("0d", "0d", (date(2026, 3, 1), date(2026, 3, 1))),
            (None, "2026-04-30", (date(2026, 1, 30), date(2026, 4, 30))),
            # A day ahead, so counting from today only past the span fails it.
            (None, "2026-03-02", (date(2026, 1, 30), date(2026, 3, 2))),
        ],
    )
    def test_today_is_read_once(self, monkeypatch, start, end, expected):
        # Two reads either side of midnight would put `0d` a day after the end
        # it defaults to, and refuse it. One day to give, so a second read
        # anywhere on these paths raises.
        days = iter([date(2026, 3, 1)])

        class _OneToday(date):
            @classmethod
            def today(cls):
                return next(days)

        monkeypatch.setattr(helpers, "date", _OneToday)
        assert parse_date(start, end, default_days=30) == expected

    def test_a_refusal_names_the_today_it_read(self, monkeypatch):
        days = iter([date(2026, 3, 1)])

        class _OneToday(date):
            @classmethod
            def today(cls):
                return next(days)

        monkeypatch.setattr(helpers, "date", _OneToday)
        with pytest.raises(InvalidDateError, match=r"after today \(2026-03-01\)"):
            parse_date("2026-03-02")


class TestFormatDuration:
    """Tests for the duration formatter."""

    def test_none_returns_empty(self):
        assert format_duration(None) == ""

    def test_zero(self):
        assert format_duration(0) == "0m"

    def test_minutes_only(self):
        assert format_duration(45) == "45m"

    def test_hours_and_minutes(self):
        assert format_duration(90) == "1h 30m"

    def test_exact_hours(self):
        assert format_duration(120) == "2h 0m"

    def test_float_rounds_correctly(self):
        assert format_duration(90.7) == "1h 31m"

    def test_float_rounds_down_at_half(self):
        # round() uses banker's rounding, 90.5 -> 90
        assert format_duration(90.5) == "1h 30m"

    def test_large_value(self):
        assert format_duration(1440) == "24h 0m"


class TestFormatResponse:
    """Tests for MCP response formatting."""

    def test_dict(self):
        result = format_response({"key": "value"})
        assert '"key": "value"' in result

    def test_list(self):
        result = format_response([1, 2, 3])
        assert "[" in result

    def test_none(self):
        assert format_response(None) == "null"

    def test_string(self):
        result = format_response("hello")
        assert '"result": "hello"' in result

    def test_nested_dict(self):
        result = format_response({"a": {"b": 1}})
        assert '"b": 1' in result

    def test_date_serialization(self):
        result = format_response({"d": date(2026, 3, 15)})
        assert "2026-03-15" in result
