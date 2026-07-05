"""Tests for :mod:`signalpipe.period` — pure UTC period math, no I/O.

The module is fully deterministic given an injected ``run_date``/``today`` so
everything here is a plain unit test (plus a hypothesis round-trip property when
hypothesis is installed). Expected ISO strings are derived from the real code
path, not from the docstrings.
"""

from __future__ import annotations

import datetime

import pytest

import signalpipe.period as period

D = datetime.date


def iso(y: int, m: int, d: int) -> str:
    """The exact UTC-midnight ISO string ``period._day_iso`` emits for a date.

    Reimplemented independently of the SUT so expected values never re-derive
    through ``period._day_iso`` — a broken serializer must make asserts fail,
    not silently match garbage against garbage.
    """
    return datetime.datetime(y, m, d, tzinfo=datetime.timezone.utc).isoformat()


def iso_d(d: datetime.date) -> str:
    """``iso`` for a ``date`` object — independent of ``period._day_iso``."""
    return iso(d.year, d.month, d.day)


# --------------------------------------------------------------------------- #
# Module surface / constants
# --------------------------------------------------------------------------- #
def test_kinds_tuple_exact():
    assert period.KINDS == ("daily", "weekly", "monthly", "quarterly", "yearly")


def test_period_error_is_valueerror():
    assert issubclass(period.PeriodError, ValueError)


# --------------------------------------------------------------------------- #
# _check_kind
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind", ["daily", "weekly", "monthly", "quarterly", "yearly"])
def test_check_kind_accepts_valid(kind):
    # Should not raise for any known kind.
    assert period._check_kind(kind) is None


@pytest.mark.parametrize("kind", ["", "Daily", "hourly", "week", "junk", "DAILY"])
def test_check_kind_rejects_junk(kind):
    with pytest.raises(period.PeriodError):
        period._check_kind(kind)


def test_check_kind_message_lists_kinds():
    with pytest.raises(period.PeriodError) as ei:
        period._check_kind("nope")
    msg = str(ei.value)
    assert "nope" in msg
    for k in period.KINDS:
        assert k in msg


# --------------------------------------------------------------------------- #
# _day_iso
# --------------------------------------------------------------------------- #
def test_day_iso_format():
    assert period._day_iso(D(2026, 7, 4)) == "2026-07-04T00:00:00+00:00"


@pytest.mark.parametrize(
    "d",
    [D(2000, 1, 1), D(2024, 2, 29), D(2026, 12, 31), D(2100, 6, 15)],
)
def test_day_iso_always_utc_midnight(d):
    s = period._day_iso(d)
    assert s.endswith("T00:00:00+00:00")
    assert s.startswith(d.isoformat())


# --------------------------------------------------------------------------- #
# _first_weekday
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "y,m,expected",
    [
        (2023, 1, D(2023, 1, 2)),   # Jan 1 = Sunday -> Jan 2 (Mon)
        (2024, 1, D(2024, 1, 1)),   # Jan 1 = Monday -> itself
        (2026, 2, D(2026, 2, 2)),   # Feb 1 = Sunday -> Feb 2 (Mon)
        (2023, 4, D(2023, 4, 3)),   # Apr 1 = Saturday -> Apr 3 (Mon)
        (2023, 7, D(2023, 7, 3)),   # Jul 1 = Saturday -> Jul 3 (Mon)
        (2026, 7, D(2026, 7, 1)),   # Jul 1 = Wednesday -> itself
        (2026, 1, D(2026, 1, 1)),   # Jan 1 = Thursday -> itself
    ],
)
def test_first_weekday_table(y, m, expected):
    assert period._first_weekday(y, m) == expected


@pytest.mark.parametrize("y", [2000, 2019, 2020, 2024, 2100])
@pytest.mark.parametrize("m", range(1, 13))
def test_first_weekday_invariants(y, m):
    fw = period._first_weekday(y, m)
    assert fw.year == y and fw.month == m
    assert fw.weekday() < 5          # never a Sat/Sun
    assert 1 <= fw.day <= 3          # first weekday can only be the 1st..3rd


# --------------------------------------------------------------------------- #
# _quarter_start_month
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "month,expected",
    [
        (1, 1), (2, 1), (3, 1),
        (4, 4), (5, 4), (6, 4),
        (7, 7), (8, 7), (9, 7),
        (10, 10), (11, 10), (12, 10),
    ],
)
def test_quarter_start_month(month, expected):
    assert period._quarter_start_month(month) == expected


# --------------------------------------------------------------------------- #
# period_key
# --------------------------------------------------------------------------- #
def test_period_key_daily():
    assert period.period_key("daily", D(2026, 7, 6)) == "2026-07-06"


@pytest.mark.parametrize(
    "run,expected",
    [
        (D(2026, 7, 10), "2026-W28"),   # Friday, ISO week 28
        (D(2026, 7, 4), "2026-W27"),    # Saturday, ISO week 27
        (D(2021, 1, 1), "2020-W53"),    # ISO year rolls back to 2020
        (D(2020, 12, 31), "2020-W53"),
    ],
)
def test_period_key_weekly(run, expected):
    assert period.period_key("weekly", run) == expected


@pytest.mark.parametrize(
    "run,expected",
    [
        (D(2026, 7, 6), "2026-06"),    # previous month
        (D(2026, 1, 5), "2025-12"),    # crosses year boundary
        (D(2024, 3, 10), "2024-02"),   # previous month is leap Feb
        (D(2026, 12, 20), "2026-11"),
    ],
)
def test_period_key_monthly(run, expected):
    assert period.period_key("monthly", run) == expected


@pytest.mark.parametrize(
    "run,expected",
    [
        (D(2026, 1, 5), "2025-Q4"),    # Q1 run -> previous Q4 of last year
        (D(2026, 4, 6), "2026-Q1"),
        (D(2026, 7, 6), "2026-Q2"),
        (D(2026, 10, 6), "2026-Q3"),
        (D(2026, 8, 15), "2026-Q2"),   # mid-Q3 still names the previous full quarter (Q2)
    ],
)
def test_period_key_quarterly(run, expected):
    # Quarterly keys the *previous full quarter* relative to run_date's quarter.
    assert period.period_key("quarterly", run) == expected


@pytest.mark.parametrize(
    "run,expected",
    [
        (D(2026, 3, 15), "2025"),
        (D(2000, 1, 1), "1999"),
        (D(2100, 12, 31), "2099"),
    ],
)
def test_period_key_yearly(run, expected):
    assert period.period_key("yearly", run) == expected


def test_period_key_unknown_kind():
    with pytest.raises(period.PeriodError):
        period.period_key("hourly", D(2026, 7, 6))


# --------------------------------------------------------------------------- #
# window
# --------------------------------------------------------------------------- #
def test_window_daily_monday_covers_fri_sat_sun():
    # 2026-07-06 is a Monday -> since = run - 3 days (Fri 00:00), until = run 00:00.
    since, until = period.window("daily", D(2026, 7, 6))
    assert since == iso(2026, 7, 3)
    assert until == iso(2026, 7, 6)


@pytest.mark.parametrize(
    "run,since_d",
    [
        (D(2026, 7, 7), D(2026, 7, 6)),    # Tuesday
        (D(2026, 7, 8), D(2026, 7, 7)),    # Wednesday
        (D(2026, 7, 9), D(2026, 7, 8)),    # Thursday
        (D(2026, 7, 10), D(2026, 7, 9)),   # Friday
    ],
)
def test_window_daily_tue_to_fri_is_prev_day(run, since_d):
    since, until = period.window("daily", run)
    assert since == iso_d(since_d)
    assert until == iso_d(run)


def test_window_daily_weekend_still_prev_day():
    # Sat/Sun aren't "due" but window() is defined for any date: not Monday -> prev day.
    since, until = period.window("daily", D(2026, 7, 4))  # Saturday
    assert since == iso(2026, 7, 3)
    assert until == iso(2026, 7, 4)


def test_window_weekly_trailing_seven_days():
    since, until = period.window("weekly", D(2026, 7, 10))
    assert since == iso(2026, 7, 3)
    assert until == iso(2026, 7, 10)


@pytest.mark.parametrize(
    "run,since_d,until_d",
    [
        (D(2026, 7, 6), D(2026, 6, 1), D(2026, 7, 1)),     # covers June
        (D(2026, 1, 5), D(2025, 12, 1), D(2026, 1, 1)),    # Jan run -> Dec, cross year
        (D(2024, 3, 10), D(2024, 2, 1), D(2024, 3, 1)),    # leap Feb boundary
    ],
)
def test_window_monthly(run, since_d, until_d):
    since, until = period.window("monthly", run)
    assert since == iso_d(since_d)
    assert until == iso_d(until_d)


@pytest.mark.parametrize(
    "run,since_d,until_d",
    [
        (D(2026, 1, 5), D(2025, 10, 1), D(2026, 1, 1)),    # Q1 run -> prev Q4, cross year
        (D(2026, 4, 6), D(2026, 1, 1), D(2026, 4, 1)),     # Q2 run -> Q1
        (D(2026, 7, 6), D(2026, 4, 1), D(2026, 7, 1)),     # Q3 run -> Q2
        (D(2026, 10, 6), D(2026, 7, 1), D(2026, 10, 1)),   # Q4 run -> Q3
        (D(2026, 8, 15), D(2026, 4, 1), D(2026, 7, 1)),    # mid-Q3 still full prev quarter
    ],
)
def test_window_quarterly(run, since_d, until_d):
    since, until = period.window("quarterly", run)
    assert since == iso_d(since_d)
    assert until == iso_d(until_d)


@pytest.mark.parametrize(
    "run,py",
    [
        (D(2026, 3, 15), 2025),
        (D(2026, 1, 1), 2025),
        (D(2000, 6, 1), 1999),
    ],
)
def test_window_yearly(run, py):
    since, until = period.window("yearly", run)
    assert since == iso(py, 1, 1)
    assert until == iso(py + 1, 1, 1)


def test_window_unknown_kind():
    with pytest.raises(period.PeriodError):
        period.window("bogus", D(2026, 7, 6))


@pytest.mark.parametrize(
    "kind,since_d,until_d",
    [
        ("daily", D(2026, 7, 9), D(2026, 7, 10)),     # Friday -> prev day
        ("weekly", D(2026, 7, 3), D(2026, 7, 10)),    # trailing 7 days
        ("monthly", D(2026, 6, 1), D(2026, 7, 1)),    # full previous month
        ("quarterly", D(2026, 4, 1), D(2026, 7, 1)),  # full previous quarter (Q2)
        ("yearly", D(2025, 1, 1), D(2026, 1, 1)),     # full previous year
    ],
)
def test_window_is_half_open_and_utc(kind, since_d, until_d):
    # Pin the exact window for every kind on one Friday, plus the half-open /
    # UTC invariants. 2026-07-10 is a Friday, safe (non-error) for all kinds.
    since, until = period.window(kind, D(2026, 7, 10))
    assert (since, until) == (iso_d(since_d), iso_d(until_d))
    assert since < until
    assert since.endswith("+00:00")
    assert until.endswith("+00:00")


# --------------------------------------------------------------------------- #
# is_due
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "run,expected",
    [
        (D(2026, 7, 6), True),    # Mon
        (D(2026, 7, 7), True),    # Tue
        (D(2026, 7, 8), True),    # Wed
        (D(2026, 7, 9), True),    # Thu
        (D(2026, 7, 10), True),   # Fri
        (D(2026, 7, 4), False),   # Sat
        (D(2026, 7, 5), False),   # Sun
    ],
)
def test_is_due_daily(run, expected):
    assert period.is_due("daily", run) is expected


@pytest.mark.parametrize(
    "run,expected",
    [
        (D(2026, 7, 10), True),   # Friday
        (D(2026, 7, 9), False),   # Thursday
        (D(2026, 7, 11), False),  # Saturday
        (D(2026, 7, 6), False),   # Monday
    ],
)
def test_is_due_weekly(run, expected):
    assert period.is_due("weekly", run) is expected


@pytest.mark.parametrize(
    "run,expected",
    [
        (D(2026, 2, 2), True),    # first weekday of Feb 2026
        (D(2026, 2, 1), False),   # Sunday, the 1st but a weekend
        (D(2026, 2, 3), False),   # not the anchor
        (D(2026, 7, 1), True),    # Jul 1 2026 = Wednesday = first weekday
        (D(2026, 7, 2), False),
    ],
)
def test_is_due_monthly(run, expected):
    assert period.is_due("monthly", run) is expected


@pytest.mark.parametrize(
    "run,expected",
    [
        (D(2023, 1, 2), True),    # first weekday Jan (Q1 start month)
        (D(2023, 4, 3), True),    # first weekday Apr
        (D(2023, 7, 3), True),    # first weekday Jul
        (D(2023, 10, 2), True),   # first weekday Oct
        (D(2023, 4, 4), False),   # right month, not the anchor day
        (D(2026, 2, 2), False),   # first weekday but month not a quarter start
        (D(2026, 5, 1), False),   # month 5 not in {1,4,7,10}
    ],
)
def test_is_due_quarterly(run, expected):
    assert period.is_due("quarterly", run) is expected


@pytest.mark.parametrize(
    "run,expected",
    [
        (D(2023, 1, 2), True),    # first weekday of Jan 2023
        (D(2024, 1, 1), True),    # Jan 1 2024 = Monday = first weekday
        (D(2023, 1, 3), False),   # not the anchor
        (D(2026, 2, 2), False),   # first weekday but not January
        (D(2026, 7, 1), False),   # first weekday of July, not January
    ],
)
def test_is_due_yearly(run, expected):
    assert period.is_due("yearly", run) is expected


def test_is_due_unknown_kind():
    with pytest.raises(period.PeriodError):
        period.is_due("nope", D(2026, 7, 6))


# --------------------------------------------------------------------------- #
# due_run_date
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind", ["daily", "weekly"])
@pytest.mark.parametrize("day", [1, 6, 15, 28])
def test_due_run_date_daily_weekly_always_none(kind, day):
    assert period.due_run_date(kind, D(2026, 7, day)) is None


@pytest.mark.parametrize("day", [1, 6, 15, 31])
def test_due_run_date_monthly_any_day(day):
    # Monthly is always applicable; returns the first weekday of today's month
    # regardless of which day `today` is.
    assert period.due_run_date("monthly", D(2026, 7, day)) == D(2026, 7, 1)


def test_due_run_date_monthly_cross_month_first_weekday():
    # Feb 2026: 1st is a Sunday -> first weekday is Feb 2.
    assert period.due_run_date("monthly", D(2026, 2, 20)) == D(2026, 2, 2)


@pytest.mark.parametrize(
    "today,expected",
    [
        (D(2026, 1, 20), D(2026, 1, 1)),    # Jan is a quarter start
        (D(2026, 4, 20), D(2026, 4, 1)),    # Apr 1 2026 = Wednesday
        (D(2026, 7, 20), D(2026, 7, 1)),
        (D(2026, 10, 20), D(2026, 10, 1)),
        (D(2026, 2, 10), None),             # not a quarter-start month
        (D(2026, 8, 15), None),
        (D(2026, 12, 31), None),
    ],
)
def test_due_run_date_quarterly(today, expected):
    assert period.due_run_date("quarterly", today) == expected


@pytest.mark.parametrize(
    "today,expected",
    [
        (D(2026, 1, 20), D(2026, 1, 1)),    # Jan -> first weekday
        (D(2023, 1, 31), D(2023, 1, 2)),    # Jan 1 2023 = Sunday -> Jan 2
        (D(2026, 3, 5), None),              # not January
        (D(2026, 12, 1), None),
    ],
)
def test_due_run_date_yearly(today, expected):
    assert period.due_run_date("yearly", today) == expected


def test_due_run_date_unknown_kind():
    with pytest.raises(period.PeriodError):
        period.due_run_date("weekend", D(2026, 7, 6))


def test_due_run_date_matches_is_due_on_first_weekday():
    # By construction, the returned run-date is exactly the day is_due() fires.
    # Jan 1 2026 is a Thursday, so the first weekday is Jan 1 for all three
    # month-anchored kinds (Jan is both a quarter-start and the yearly month).
    for kind in ("monthly", "quarterly", "yearly"):
        rd = period.due_run_date(kind, D(2026, 1, 15))
        assert rd == D(2026, 1, 1)
        assert period.is_due(kind, rd) is True


# --------------------------------------------------------------------------- #
# parse_period — happy paths (inverse of window)
# --------------------------------------------------------------------------- #
def test_parse_period_daily_roundtrip_monday():
    # Monday key -> the three-day window, same as window("daily", Monday).
    assert period.parse_period("daily", "2026-07-06") == (
        iso(2026, 7, 3),
        iso(2026, 7, 6),
    )


def test_parse_period_daily_roundtrip_midweek():
    assert period.parse_period("daily", "2026-07-08") == (
        iso(2026, 7, 7),
        iso(2026, 7, 8),
    )


def test_parse_period_weekly():
    assert period.parse_period("weekly", "2026-W28") == (
        iso(2026, 7, 3),
        iso(2026, 7, 10),
    )


def test_parse_period_weekly_iso_year_rollover():
    # 2020-W53 Friday is 2021-01-01; trailing week starts 2020-12-25.
    since, until = period.parse_period("weekly", "2020-W53")
    assert since == iso(2020, 12, 25)
    assert until == iso(2021, 1, 1)


@pytest.mark.parametrize(
    "key,since_d,until_d",
    [
        ("2026-06", D(2026, 6, 1), D(2026, 7, 1)),
        ("2025-12", D(2025, 12, 1), D(2026, 1, 1)),    # cross-year
        ("2024-02", D(2024, 2, 1), D(2024, 3, 1)),     # leap Feb -> Mar
        ("2026-01", D(2026, 1, 1), D(2026, 2, 1)),
    ],
)
def test_parse_period_monthly(key, since_d, until_d):
    assert period.parse_period("monthly", key) == (
        iso_d(since_d),
        iso_d(until_d),
    )


@pytest.mark.parametrize(
    "key,since_d,until_d",
    [
        ("2026-Q1", D(2026, 1, 1), D(2026, 4, 1)),
        ("2026-Q2", D(2026, 4, 1), D(2026, 7, 1)),
        ("2026-Q3", D(2026, 7, 1), D(2026, 10, 1)),
        ("2026-Q4", D(2026, 10, 1), D(2027, 1, 1)),   # Q4 rolls into next year
    ],
)
def test_parse_period_quarterly(key, since_d, until_d):
    assert period.parse_period("quarterly", key) == (
        iso_d(since_d),
        iso_d(until_d),
    )


@pytest.mark.parametrize(
    "key,y",
    [("2025", 2025), ("1999", 1999), ("2100", 2100)],
)
def test_parse_period_yearly(key, y):
    assert period.parse_period("yearly", key) == (iso(y, 1, 1), iso(y + 1, 1, 1))


# --------------------------------------------------------------------------- #
# parse_period — error paths
# --------------------------------------------------------------------------- #
def test_parse_period_unknown_kind():
    with pytest.raises(period.PeriodError):
        period.parse_period("hourly", "2026-07-06")


@pytest.mark.parametrize(
    "key",
    ["2026-13-40", "not-a-date", "2026/07/06", ""],
)
def test_parse_period_daily_bad_key(key):
    with pytest.raises(period.PeriodError):
        period.parse_period("daily", key)


@pytest.mark.parametrize(
    "key",
    ["2026-W99", "not-a-week", "2026-Wxx", "2026"],
)
def test_parse_period_weekly_bad_key(key):
    with pytest.raises(period.PeriodError):
        period.parse_period("weekly", key)


@pytest.mark.parametrize(
    "key",
    ["2026-13", "2026", "abcd-01", "2026-1x"],
)
def test_parse_period_monthly_bad_key(key):
    with pytest.raises(period.PeriodError):
        period.parse_period("monthly", key)


@pytest.mark.parametrize(
    "key",
    ["2025-Q5", "2025-Q0", "2025-Qx", "2025", "abcd-Q1"],
)
def test_parse_period_quarterly_bad_key(key):
    with pytest.raises(period.PeriodError):
        period.parse_period("quarterly", key)


def test_parse_period_quarterly_q5_message():
    # q>4 raises the explicit "bad quarter" PeriodError (not the generic wrapper).
    with pytest.raises(period.PeriodError) as ei:
        period.parse_period("quarterly", "2025-Q5")
    assert "bad quarter" in str(ei.value)


@pytest.mark.parametrize(
    "key",
    ["abcd", "20x5", "", "2025.0"],
)
def test_parse_period_yearly_bad_key(key):
    with pytest.raises(period.PeriodError):
        period.parse_period("yearly", key)


def test_parse_period_error_message_carries_context():
    with pytest.raises(period.PeriodError) as ei:
        period.parse_period("yearly", "abcd")
    msg = str(ei.value)
    assert "yearly" in msg and "abcd" in msg


# --------------------------------------------------------------------------- #
# Cross-function round-trip invariants (explicit, non-hypothesis)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "kind,run",
    [
        ("daily", D(2026, 7, 6)),      # Monday (3-day window)
        ("daily", D(2026, 7, 8)),      # midweek
        ("monthly", D(2026, 7, 15)),
        ("monthly", D(2026, 1, 15)),   # cross-year
        ("quarterly", D(2026, 8, 15)),
        ("quarterly", D(2026, 1, 15)), # cross-year
        ("yearly", D(2026, 6, 1)),
    ],
)
def test_parse_period_inverts_window_for_nonweekly(kind, run):
    key = period.period_key(kind, run)
    assert period.parse_period(kind, key) == period.window(kind, run)


@pytest.mark.parametrize(
    "run",
    [D(2026, 7, 6), D(2026, 7, 10), D(2026, 7, 4), D(2021, 1, 1)],
)
def test_parse_period_inverts_window_for_weekly_at_anchor(run):
    # Weekly keys anchor on the ISO week's Friday, so the round-trip reproduces
    # window() evaluated at that Friday (not necessarily at `run`).
    key = period.period_key("weekly", run)
    iy, iw, _ = run.isocalendar()
    friday = datetime.date.fromisocalendar(iy, iw, 5)
    assert period.parse_period("weekly", key) == period.window("weekly", friday)


# --------------------------------------------------------------------------- #
# Property-based round-trip (hypothesis optional)
# --------------------------------------------------------------------------- #
@pytest.mark.property
def test_property_key_window_parse_roundtrip():
    hypothesis = pytest.importorskip("hypothesis")
    from hypothesis import given
    from hypothesis import strategies as st

    dates = st.dates(min_value=D(2000, 1, 1), max_value=D(2100, 12, 31))

    @given(kind=st.sampled_from(period.KINDS), d=dates)
    def check(kind, d):
        since, until = period.window(kind, d)
        # Half-open, UTC, string-comparable.
        assert since < until
        assert since.endswith("+00:00") and until.endswith("+00:00")

        key = period.period_key(kind, d)
        parsed = period.parse_period(kind, key)
        if kind == "weekly":
            iy, iw, _ = d.isocalendar()
            friday = datetime.date.fromisocalendar(iy, iw, 5)
            assert parsed == period.window("weekly", friday)
        else:
            assert parsed == (since, until)

    check()


@pytest.mark.property
def test_property_first_weekday_and_is_due_consistency():
    hypothesis = pytest.importorskip("hypothesis")
    from hypothesis import given
    from hypothesis import strategies as st

    dates = st.dates(min_value=D(2000, 1, 1), max_value=D(2100, 12, 31))

    @given(d=dates)
    def check(d):
        fw = period._first_weekday(d.year, d.month)
        assert fw.weekday() < 5
        assert fw.month == d.month and fw.year == d.year
        # monthly is_due fires iff d IS the first weekday of its month.
        assert period.is_due("monthly", d) is (d == fw)
        # daily is_due fires iff a weekday.
        assert period.is_due("daily", d) is (d.weekday() < 5)
        # weekly is_due fires iff Friday.
        assert period.is_due("weekly", d) is (d.weekday() == 4)

    check()
