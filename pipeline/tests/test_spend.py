"""Tests for ``signalpipe.llm.spend`` (daily spend ledger + cap breaker) and the
``signalpipe.llm`` exception taxonomy (LLMError / SpendCapExceeded / UsageLimitExhausted).

Hermetic: the only I/O boundaries are sqlite (the shared ``conn`` fixture — a real
tmp-file writer connection with the v5 schema, ``sqlite3.Row`` factory and
``isolation_level=None`` autocommit, exactly what the module needs) and the clock
(``spend._today`` is monkeypatched to a fixed day so ledger rows are deterministic and
the midnight-rollover contract is exercised without racing real UTC time).
"""

from __future__ import annotations

import types

import pytest

from signalpipe.llm import LLMError, SpendCapExceeded, UsageLimitExhausted, spend


# --------------------------------------------------------------------------- #
# Local helpers
# --------------------------------------------------------------------------- #
class _Cfg:
    """Minimal stand-in for ``config.Config`` — ``assert_under_cap`` only ever
    touches ``cfg.spend.get(...)``."""

    def __init__(self, spend_conf):
        self.spend = spend_conf


def _row(conn, day):
    return conn.execute("SELECT * FROM spend WHERE day=?", (day,)).fetchone()


@pytest.fixture
def freeze_today(monkeypatch):
    """Monkeypatch ``spend._today`` to a fixed day string.

    Returns a setter so a single test can advance the clock (midnight rollover).
    """

    def _freeze(day="2026-07-04"):
        monkeypatch.setattr(spend, "_today", lambda: day)
        return day

    return _freeze


# --------------------------------------------------------------------------- #
# Exception taxonomy (signalpipe/llm/__init__.py) — pure, no fakes
# --------------------------------------------------------------------------- #
class TestExceptionTaxonomy:
    def test_llm_error_cost_default_none(self):
        e = LLMError("boom")
        assert e.cost_usd is None
        assert str(e) == "boom"
        assert isinstance(e, Exception)

    def test_llm_error_cost_explicit(self):
        e = LLMError("boom", cost_usd=0.02)
        assert e.cost_usd == 0.02

    def test_llm_error_cost_explicit_zero(self):
        # An explicit 0.0 must survive (not be coerced to the None default).
        e = LLMError("free", cost_usd=0.0)
        assert e.cost_usd == 0.0

    def test_spend_cap_exceeded_cost_forced_zero(self):
        e = SpendCapExceeded("cap hit")
        assert e.cost_usd == 0.0
        assert str(e) == "cap hit"

    def test_spend_cap_exceeded_is_llm_error(self):
        e = SpendCapExceeded("cap hit")
        assert isinstance(e, LLMError)
        assert isinstance(e, Exception)
        assert issubclass(SpendCapExceeded, LLMError)

    def test_usage_limit_defaults(self):
        e = UsageLimitExhausted("quota")
        assert e.retry_at is None
        assert e.cost_usd == 0.0
        assert str(e) == "quota"

    def test_usage_limit_stores_both(self):
        e = UsageLimitExhausted("quota", retry_at=123.0, cost_usd=0.5)
        assert e.retry_at == 123.0
        assert e.cost_usd == 0.5

    def test_usage_limit_is_llm_error(self):
        e = UsageLimitExhausted("quota")
        assert isinstance(e, LLMError)
        assert issubclass(UsageLimitExhausted, LLMError)

    def test_subclass_ordering_contract(self):
        # Pin the hierarchy that the adapter's except-ordering relies on: both
        # cap/quota errors are LLMError subclasses, but neither is the other's
        # subclass (so `except UsageLimitExhausted` before `except LLMError`
        # is a real, distinct branch and can't silently collapse).
        assert issubclass(SpendCapExceeded, LLMError)
        assert issubclass(UsageLimitExhausted, LLMError)
        assert not issubclass(SpendCapExceeded, UsageLimitExhausted)
        assert not issubclass(UsageLimitExhausted, SpendCapExceeded)

    def test_caught_as_llm_error(self):
        # The adapter does `except LLMError`; confirm both flavours are caught.
        for exc in (SpendCapExceeded("a"), UsageLimitExhausted("b")):
            try:
                raise exc
            except LLMError as caught:
                assert caught is exc
            else:  # pragma: no cover - defensive
                pytest.fail("LLMError did not catch subclass")


# --------------------------------------------------------------------------- #
# _today() — clock boundary
# --------------------------------------------------------------------------- #
class TestToday:
    def test_today_format(self):
        s = spend._today()
        assert isinstance(s, str)
        # YYYY-MM-DD, ten chars, dash-separated.
        parts = s.split("-")
        assert len(s) == 10
        assert len(parts) == 3
        assert parts[0].isdigit() and len(parts[0]) == 4
        assert parts[1].isdigit() and len(parts[1]) == 2
        assert parts[2].isdigit() and len(parts[2]) == 2

    def test_today_is_utc_date(self, monkeypatch):
        # Feed a fixed instant late in the UTC day and confirm the UTC calendar
        # date is used (not local time). Replaces the whole datetime module ref
        # the function closes over.
        import datetime as real_dt

        fake = types.SimpleNamespace(
            datetime=types.SimpleNamespace(
                now=lambda tz: real_dt.datetime(2026, 3, 15, 23, 59, tzinfo=tz)
            ),
            timezone=real_dt.timezone,
        )
        monkeypatch.setattr(spend, "datetime", fake)
        assert spend._today() == "2026-03-15"


# --------------------------------------------------------------------------- #
# record() — backend/kind routing + upsert accumulation
# --------------------------------------------------------------------------- #
@pytest.mark.integration
class TestRecord:
    def test_subscription_routes_to_cli(self, conn, freeze_today):
        day = freeze_today()
        spend.record(conn, "subscription", 0.1)
        row = _row(conn, day)
        assert row["cli_usd"] == pytest.approx(0.1)
        assert row["api_usd"] == pytest.approx(0.0)
        assert row["digest_usd"] == pytest.approx(0.0)
        assert row["calls"] == 1

    def test_api_routes_to_api(self, conn, freeze_today):
        day = freeze_today()
        spend.record(conn, "api", 0.2)
        row = _row(conn, day)
        assert row["api_usd"] == pytest.approx(0.2)
        assert row["cli_usd"] == pytest.approx(0.0)
        assert row["calls"] == 1

    def test_non_subscription_backend_routes_to_api(self, conn, freeze_today):
        # Anything that isn't the literal "subscription" lands in api_usd.
        day = freeze_today()
        spend.record(conn, "local", 0.15)
        row = _row(conn, day)
        assert row["api_usd"] == pytest.approx(0.15)
        assert row["cli_usd"] == pytest.approx(0.0)

    def test_digest_kind_adds_to_digest_and_cli(self, conn, freeze_today):
        day = freeze_today()
        spend.record(conn, "subscription", 0.3, kind="digest")
        row = _row(conn, day)
        assert row["cli_usd"] == pytest.approx(0.3)
        assert row["digest_usd"] == pytest.approx(0.3)
        assert row["api_usd"] == pytest.approx(0.0)
        assert row["calls"] == 1

    def test_digest_kind_adds_to_digest_and_api(self, conn, freeze_today):
        day = freeze_today()
        spend.record(conn, "api", 0.4, kind="digest")
        row = _row(conn, day)
        assert row["api_usd"] == pytest.approx(0.4)
        assert row["digest_usd"] == pytest.approx(0.4)
        assert row["cli_usd"] == pytest.approx(0.0)

    def test_daily_kind_leaves_digest_zero(self, conn, freeze_today):
        day = freeze_today()
        spend.record(conn, "subscription", 0.9)  # kind defaults to "daily"
        row = _row(conn, day)
        # The spend still lands in cli_usd; only digest_usd stays zero.
        assert row["cli_usd"] == pytest.approx(0.9)
        assert row["digest_usd"] == pytest.approx(0.0)

    def test_repeated_same_day_accumulates(self, conn, freeze_today):
        day = freeze_today()
        spend.record(conn, "subscription", 0.1)
        spend.record(conn, "subscription", 0.25)
        row = _row(conn, day)
        assert row["cli_usd"] == pytest.approx(0.35)
        assert row["calls"] == 2

    def test_mixed_backends_and_kinds(self, conn, freeze_today):
        day = freeze_today()
        spend.record(conn, "subscription", 0.1)
        spend.record(conn, "api", 0.2)
        spend.record(conn, "subscription", 0.3, kind="digest")
        row = _row(conn, day)
        assert row["cli_usd"] == pytest.approx(0.4)  # 0.1 + 0.3
        assert row["api_usd"] == pytest.approx(0.2)
        assert row["digest_usd"] == pytest.approx(0.3)
        assert row["calls"] == 3

    def test_none_cost_is_treated_as_zero(self, conn, freeze_today):
        day = freeze_today()
        spend.record(conn, "subscription", None)
        row = _row(conn, day)
        assert row["cli_usd"] == pytest.approx(0.0)
        assert row["calls"] == 1

    def test_none_cost_digest_is_zero(self, conn, freeze_today):
        day = freeze_today()
        spend.record(conn, "api", None, kind="digest")
        row = _row(conn, day)
        assert row["digest_usd"] == pytest.approx(0.0)
        assert row["api_usd"] == pytest.approx(0.0)
        assert row["calls"] == 1

    def test_record_persists_without_explicit_commit(self, conn, freeze_today):
        # Autocommit (isolation_level=None): the row is durable even though the
        # module never calls conn.commit(). Read it back through today_total.
        freeze_today()
        spend.record(conn, "subscription", 0.5)
        assert spend.today_total(conn) == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# today_total() / today_digest()
# --------------------------------------------------------------------------- #
@pytest.mark.integration
class TestTotals:
    def test_empty_db_returns_zero(self, conn, freeze_today):
        freeze_today()
        assert spend.today_total(conn) == 0.0
        assert spend.today_digest(conn) == 0.0

    def test_total_sums_cli_and_api(self, conn, freeze_today):
        freeze_today()
        spend.record(conn, "subscription", 0.1)
        spend.record(conn, "api", 0.2)
        assert spend.today_total(conn) == pytest.approx(0.3)

    def test_digest_sums_only_digest_kind(self, conn, freeze_today):
        freeze_today()
        spend.record(conn, "subscription", 1.0)  # non-digest
        spend.record(conn, "subscription", 0.4, kind="digest")
        spend.record(conn, "api", 0.6, kind="digest")
        assert spend.today_digest(conn) == pytest.approx(1.0)  # 0.4 + 0.6
        assert spend.today_total(conn) == pytest.approx(2.0)  # 1.0 + 0.4 + 0.6

    def test_seeded_zero_row_returns_zero(self, conn, seed, freeze_today):
        # A present row whose columns are all 0 must read as 0.0 (the `and
        # row["total"]` / `and row["digest_usd"]` falsy-zero branch).
        freeze_today("2026-07-04")
        seed.spend(day="2026-07-04", cli_usd=0.0, api_usd=0.0, digest_usd=0.0)
        assert spend.today_total(conn) == 0.0
        assert spend.today_digest(conn) == 0.0

    def test_seeded_values_read_back(self, conn, seed, freeze_today):
        freeze_today("2026-07-04")
        seed.spend(day="2026-07-04", cli_usd=1.5, api_usd=0.25, digest_usd=0.75)
        assert spend.today_total(conn) == pytest.approx(1.75)
        assert spend.today_digest(conn) == pytest.approx(0.75)

    def test_other_day_row_ignored(self, conn, seed, freeze_today):
        # A row for a different day must not leak into today's totals.
        seed.spend(day="2026-01-01", cli_usd=9.0, api_usd=9.0, digest_usd=9.0)
        freeze_today("2026-07-04")
        assert spend.today_total(conn) == 0.0
        assert spend.today_digest(conn) == 0.0


# --------------------------------------------------------------------------- #
# assert_under_cap() — daily gate
# --------------------------------------------------------------------------- #
@pytest.mark.integration
class TestDailyCap:
    def test_under_cap_passes(self, conn, freeze_today):
        freeze_today()
        cfg = _Cfg({"daily_cap_usd": 1.0})
        spend.record(conn, "subscription", 0.5)
        # Guard against a vacuous pass: the record must actually be on the books
        # (0.5 < 1.0). If record() silently wrote nothing this would read 0.0 and
        # the no-raise below would prove nothing.
        assert spend.today_total(conn) == pytest.approx(0.5)
        # Under the cap: must not raise.
        spend.assert_under_cap(conn, cfg)

    def test_at_or_over_cap_raises(self, conn, freeze_today):
        day = freeze_today()
        cfg = _Cfg({"daily_cap_usd": 1.0})
        spend.record(conn, "subscription", 0.6)
        spend.record(conn, "api", 0.6)  # total 1.2 >= 1.0
        with pytest.raises(SpendCapExceeded) as ei:
            spend.assert_under_cap(conn, cfg)
        msg = str(ei.value)
        assert "daily spend cap hit" in msg
        assert day in msg
        # Pin the exact breach formatting: spent 1.2000 (%.4f) >= cap 1.00 (%.2f).
        assert "$1.2000 >= $1.00" in msg
        # SpendCapExceeded is defined to carry cost 0.0 (never recorded).
        assert ei.value.cost_usd == 0.0

    def test_exactly_equal_cap_raises(self, conn, freeze_today):
        # The gate is `>=`, so spent == cap must raise.
        freeze_today()
        cfg = _Cfg({"daily_cap_usd": 0.5})
        spend.record(conn, "subscription", 0.5)
        with pytest.raises(SpendCapExceeded):
            spend.assert_under_cap(conn, cfg)

    def test_default_cap_five_when_missing(self, conn, freeze_today):
        freeze_today()
        cfg = _Cfg({})  # no daily_cap_usd -> defaults to 5.0
        spend.record(conn, "subscription", 4.99)
        spend.assert_under_cap(conn, cfg)  # under 5.0, passes
        spend.record(conn, "api", 0.01)  # total 5.0 -> at cap
        with pytest.raises(SpendCapExceeded) as ei:
            spend.assert_under_cap(conn, cfg)
        # The breach message must name the *default* cap of $5.00 (not some other
        # fallback), and the accumulated total 5.0000 that tripped it.
        assert "$5.0000 >= $5.00" in str(ei.value)

    def test_default_kind_is_daily(self, conn, freeze_today):
        # Calling without kind uses the daily gate.
        freeze_today()
        cfg = _Cfg({"daily_cap_usd": 1.0, "digest_cap_usd": 100.0})
        spend.record(conn, "subscription", 2.0)  # over daily, under digest
        with pytest.raises(SpendCapExceeded):
            spend.assert_under_cap(conn, cfg)  # kind defaults to "daily"


# --------------------------------------------------------------------------- #
# assert_under_cap() — digest sub-cap decoupling
# --------------------------------------------------------------------------- #
@pytest.mark.integration
class TestDigestCap:
    def test_digest_decoupled_from_heavy_day(self, conn, freeze_today):
        # A busy curation morning (heavy cli_usd, zero digest_usd) must NOT
        # block the digest: digest is gated only by digest_usd vs digest_cap.
        freeze_today()
        cfg = _Cfg({"daily_cap_usd": 5.0, "digest_cap_usd": 5.0})
        spend.record(conn, "subscription", 10.0)  # day total 10, digest 0
        # digest gate passes despite the day being far over the daily cap.
        spend.assert_under_cap(conn, cfg, kind="digest")
        # ...and the daily gate on the very same state DOES raise (contrast).
        with pytest.raises(SpendCapExceeded):
            spend.assert_under_cap(conn, cfg, kind="daily")

    def test_digest_over_its_own_cap_raises(self, conn, freeze_today):
        day = freeze_today()
        cfg = _Cfg({"daily_cap_usd": 100.0, "digest_cap_usd": 5.0})
        spend.record(conn, "subscription", 5.0, kind="digest")  # digest_usd 5.0
        with pytest.raises(SpendCapExceeded) as ei:
            spend.assert_under_cap(conn, cfg, kind="digest")
        msg = str(ei.value)
        assert "digest spend cap hit" in msg
        assert day in msg
        # Digest breach names the digest sub-cap, not the daily cap ($100).
        assert "$5.0000 >= $5.00" in msg
        assert ei.value.cost_usd == 0.0

    def test_digest_under_its_cap_passes(self, conn, freeze_today):
        freeze_today()
        cfg = _Cfg({"digest_cap_usd": 5.0})
        spend.record(conn, "api", 4.9, kind="digest")
        # Non-vacuous: the 4.9 must be booked as digest spend (< 5.0 cap) for the
        # no-raise below to mean anything.
        assert spend.today_digest(conn) == pytest.approx(4.9)
        spend.assert_under_cap(conn, cfg, kind="digest")  # 4.9 < 5.0, passes

    def test_digest_default_cap_five_when_missing(self, conn, freeze_today):
        freeze_today()
        cfg = _Cfg({})  # no digest_cap_usd -> defaults to 5.0
        spend.record(conn, "subscription", 5.0, kind="digest")
        with pytest.raises(SpendCapExceeded) as ei:
            spend.assert_under_cap(conn, cfg, kind="digest")
        # Confirms the default digest cap is exactly $5.00 (not silently some
        # other value that 5.0 also happens to trip).
        assert "$5.0000 >= $5.00" in str(ei.value)

    def test_digest_exactly_equal_cap_raises(self, conn, freeze_today):
        freeze_today()
        cfg = _Cfg({"digest_cap_usd": 2.0})
        spend.record(conn, "subscription", 2.0, kind="digest")
        with pytest.raises(SpendCapExceeded):
            spend.assert_under_cap(conn, cfg, kind="digest")


# --------------------------------------------------------------------------- #
# Midnight rollover / per-day keying
# --------------------------------------------------------------------------- #
@pytest.mark.integration
class TestMidnightRollover:
    def test_rollover_isolates_totals(self, conn, freeze_today):
        # Day A gets spend; advancing the clock to day B shows a clean slate.
        freeze_today("2026-07-04")
        spend.record(conn, "subscription", 1.0)
        spend.record(conn, "subscription", 0.5, kind="digest")
        assert spend.today_total(conn) == pytest.approx(1.5)
        assert spend.today_digest(conn) == pytest.approx(0.5)

        freeze_today("2026-07-05")  # rolled past midnight UTC
        assert spend.today_total(conn) == 0.0
        assert spend.today_digest(conn) == 0.0

        # Day A's row is untouched: rolling back reveals the original figures.
        freeze_today("2026-07-04")
        assert spend.today_total(conn) == pytest.approx(1.5)
        assert spend.today_digest(conn) == pytest.approx(0.5)

    def test_cap_resets_next_day(self, conn, freeze_today):
        cfg = _Cfg({"daily_cap_usd": 1.0})
        freeze_today("2026-07-04")
        spend.record(conn, "subscription", 2.0)  # over cap on day A
        with pytest.raises(SpendCapExceeded):
            spend.assert_under_cap(conn, cfg)

        freeze_today("2026-07-05")  # new day: fresh budget
        spend.assert_under_cap(conn, cfg)  # does not raise
