"""Pure period math for the multi-cadence digest engine. No I/O.

Five kinds, one authority for "when does which digest run and what window
does it cover":

  daily      Mon-Fri. Tue-Fri cover [yesterday 00:00, today 00:00);
             Monday covers [Friday 00:00, Monday 00:00) — Fri+Sat+Sun,
             gapless with Friday's run (which covered Thursday).
  weekly     Fridays, trailing [run-7d 00:00, run 00:00).
  monthly    First weekday of the month, full previous calendar month.
  quarterly  First weekday of Jan/Apr/Jul/Oct, full previous quarter.
  yearly     First weekday of January, full previous calendar year.

All boundaries are UTC midnights serialized as ISO 8601 with +00:00, which
compares correctly as strings against the pipeline's stored timestamps.
"""

from __future__ import annotations

import datetime
from typing import Optional, Tuple

KINDS = ("daily", "weekly", "monthly", "quarterly", "yearly")


class PeriodError(ValueError):
    pass


def _check_kind(kind: str) -> None:
    if kind not in KINDS:
        raise PeriodError("unknown digest kind %r (expected one of %s)"
                          % (kind, ", ".join(KINDS)))


def _day_iso(d: datetime.date) -> str:
    """UTC midnight of a date as an ISO timestamp."""
    return datetime.datetime(
        d.year, d.month, d.day, tzinfo=datetime.timezone.utc
    ).isoformat()


def _first_weekday(year: int, month: int) -> datetime.date:
    d = datetime.date(year, month, 1)
    while d.weekday() >= 5:  # 5=Sat, 6=Sun
        d += datetime.timedelta(days=1)
    return d


def _quarter_start_month(month: int) -> int:
    return ((month - 1) // 3) * 3 + 1


def period_key(kind: str, run_date: datetime.date) -> str:
    """The stable identifier for the period a run on `run_date` covers."""
    _check_kind(kind)
    if kind == "daily":
        return run_date.isoformat()
    if kind == "weekly":
        y, w, _ = run_date.isocalendar()
        return "%d-W%02d" % (y, w)
    if kind == "monthly":
        prev_last = run_date.replace(day=1) - datetime.timedelta(days=1)
        return "%04d-%02d" % (prev_last.year, prev_last.month)
    if kind == "quarterly":
        qstart = run_date.replace(
            month=_quarter_start_month(run_date.month), day=1
        )
        prev_last = qstart - datetime.timedelta(days=1)
        return "%d-Q%d" % (prev_last.year, (prev_last.month - 1) // 3 + 1)
    # yearly
    return "%d" % (run_date.year - 1)


def window(kind: str, run_date: datetime.date) -> Tuple[str, str]:
    """(since_iso, until_iso) — the half-open UTC window a run covers."""
    _check_kind(kind)
    if kind == "daily":
        if run_date.weekday() == 0:  # Monday: cover Fri+Sat+Sun
            since = run_date - datetime.timedelta(days=3)
        else:
            since = run_date - datetime.timedelta(days=1)
        return _day_iso(since), _day_iso(run_date)
    if kind == "weekly":
        return (_day_iso(run_date - datetime.timedelta(days=7)),
                _day_iso(run_date))
    if kind == "monthly":
        until = run_date.replace(day=1)
        prev_last = until - datetime.timedelta(days=1)
        since = prev_last.replace(day=1)
        return _day_iso(since), _day_iso(until)
    if kind == "quarterly":
        until = run_date.replace(
            month=_quarter_start_month(run_date.month), day=1
        )
        prev_last = until - datetime.timedelta(days=1)
        since = prev_last.replace(
            month=_quarter_start_month(prev_last.month), day=1
        )
        return _day_iso(since), _day_iso(until)
    # yearly: full previous calendar year
    return (_day_iso(datetime.date(run_date.year - 1, 1, 1)),
            _day_iso(datetime.date(run_date.year, 1, 1)))


def is_due(kind: str, run_date: datetime.date) -> bool:
    """Weekday-shift authority: should a `kind` digest run on this date?"""
    _check_kind(kind)
    wd = run_date.weekday()
    if kind == "daily":
        return wd < 5
    if kind == "weekly":
        return wd == 4  # Friday
    if kind == "monthly":
        return run_date == _first_weekday(run_date.year, run_date.month)
    if kind == "quarterly":
        return (run_date.month in (1, 4, 7, 10)
                and run_date == _first_weekday(run_date.year, run_date.month))
    # yearly
    return (run_date.month == 1
            and run_date == _first_weekday(run_date.year, 1))


def due_run_date(kind: str, today: datetime.date) -> Optional[datetime.date]:
    """Month-anchored kinds (monthly/quarterly/yearly): the scheduled
    run-date (first weekday) in `today`'s month, or None when the kind has
    no run this month. daily/weekly return None — they are cron-gated, not
    catch-up-gated. Used by the worker's editions dispatcher to retry a
    missed/failed first-weekday run on later days of the month."""
    _check_kind(kind)
    if kind == "monthly":
        applicable = True
    elif kind == "quarterly":
        applicable = today.month in (1, 4, 7, 10)
    elif kind == "yearly":
        applicable = today.month == 1
    else:
        return None
    if not applicable:
        return None
    return _first_weekday(today.year, today.month)


def parse_period(kind: str, key: str) -> Tuple[str, str]:
    """period key -> (since_iso, until_iso), for backfill.

    Mirrors window(): a daily key is the run date (Monday keys cover three
    days); a weekly key covers the trailing week ending on that ISO week's
    Friday; monthly/quarterly/yearly keys cover the named calendar period.
    """
    _check_kind(kind)
    try:
        if kind == "daily":
            return window("daily", datetime.date.fromisoformat(key))
        if kind == "weekly":
            year, wnum = key.split("-W")
            friday = datetime.date.fromisocalendar(int(year), int(wnum), 5)
            return window("weekly", friday)
        if kind == "monthly":
            year, month = (int(x) for x in key.split("-"))
            since = datetime.date(year, month, 1)
            until = (since + datetime.timedelta(days=32)).replace(day=1)
            return _day_iso(since), _day_iso(until)
        if kind == "quarterly":
            year, qn = key.split("-Q")
            q = int(qn)
            if q not in (1, 2, 3, 4):
                raise PeriodError("bad quarter in %r" % key)
            since = datetime.date(int(year), (q - 1) * 3 + 1, 1)
            if q == 4:
                until = datetime.date(int(year) + 1, 1, 1)
            else:
                until = datetime.date(int(year), q * 3 + 1, 1)
            return _day_iso(since), _day_iso(until)
        # yearly
        year = int(key)
        return (_day_iso(datetime.date(year, 1, 1)),
                _day_iso(datetime.date(year + 1, 1, 1)))
    except (ValueError, IndexError) as e:
        if isinstance(e, PeriodError):
            raise
        raise PeriodError("cannot parse %s period key %r: %s" % (kind, key, e))
