"""Tests for ``signalpipe.llm.quota`` — the subscription usage-limit hold file.

``HOLD_PATH`` and ``STATE_DIR`` are module-level constants derived from ``$HOME`` at
import; the autouse ``redirect_state_dirs`` fixture in conftest already repoints both
at ``tmp_path``, so nothing here touches the real ``~/.local/state``.

Time is the other seam: ``set_hold``/``status`` call ``time.time()`` and ``status``
renders an HH:MM via ``datetime.fromtimestamp`` (LOCAL tz). We freeze the clock by
monkeypatching ``quota.time.time`` with a mutable :class:`Clock`, and assert on the
active/expired boolean plus reason substrings rather than the exact HH:MM (which is
TZ/DST dependent).
"""

from __future__ import annotations

import datetime
import json
import re

import pytest

import signalpipe.llm.quota as quota

# A fixed epoch, comfortably < 2**53 so every float add below is exact.
T0 = 1_700_000_000.0


class Clock:
    """A callable, mutable stand-in for ``time.time``."""

    def __init__(self, t: float = T0) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t


class FakeCfg:
    """Minimal config: ``set_hold`` only reads ``cfg.backend.get('quota_recheck_min')``."""

    def __init__(self, quota_recheck_min=None) -> None:
        self.backend = {}
        if quota_recheck_min is not None:
            self.backend["quota_recheck_min"] = quota_recheck_min


@pytest.fixture
def clock(monkeypatch):
    """Freeze ``quota``'s clock; return the mutable Clock so tests can advance it."""
    c = Clock()
    monkeypatch.setattr(quota.time, "time", c)
    return c


def _read_hold():
    return json.loads(quota.HOLD_PATH.read_text())


# --------------------------------------------------------------------------- #
# set_hold — effective retry_at selection
# --------------------------------------------------------------------------- #
def test_set_hold_future_retry_at_written_verbatim(clock):
    cfg = FakeCfg(quota_recheck_min=30)
    future = clock.t + 5000
    result = quota.set_hold(cfg, "rate limited", future)

    assert result == future
    data = _read_hold()
    assert data["retry_at"] == future
    assert data["reason"] == "rate limited"
    assert data["set_at"] == clock.t


def test_set_hold_none_defaults_to_recheck_window(clock):
    cfg = FakeCfg(quota_recheck_min=30)
    result = quota.set_hold(cfg, "hit limit", None)

    assert result == clock.t + 30 * 60
    assert _read_hold()["retry_at"] == clock.t + 1800


def test_set_hold_past_retry_at_defaults(clock):
    cfg = FakeCfg(quota_recheck_min=30)
    # retry_at strictly in the past -> replaced by now + window
    assert quota.set_hold(cfg, "x", clock.t - 500) == clock.t + 1800


def test_set_hold_retry_at_equal_now_defaults(clock):
    cfg = FakeCfg(quota_recheck_min=10)
    # boundary: retry_at == now is `<= now` -> defaulted
    assert quota.set_hold(cfg, "x", clock.t) == clock.t + 600


def test_set_hold_custom_recheck_min(clock):
    cfg = FakeCfg(quota_recheck_min=15)
    assert quota.set_hold(cfg, "x", None) == clock.t + 15 * 60


def test_set_hold_default_recheck_min_when_unset(clock):
    cfg = FakeCfg()  # no quota_recheck_min key -> get(..., 30)
    assert quota.set_hold(cfg, "x", None) == clock.t + 30 * 60


# --------------------------------------------------------------------------- #
# set_hold — payload shape
# --------------------------------------------------------------------------- #
def test_set_hold_writes_exactly_three_keys(clock):
    quota.set_hold(FakeCfg(), "x", clock.t + 100)
    assert set(_read_hold().keys()) == {"retry_at", "reason", "set_at"}


def test_set_hold_truncates_reason_to_300(clock):
    quota.set_hold(FakeCfg(), "z" * 500, clock.t + 1000)
    reason = _read_hold()["reason"]
    assert reason == "z" * 300
    assert len(reason) == 300


def test_set_hold_coerces_non_str_reason(clock):
    quota.set_hold(FakeCfg(), 42, clock.t + 1000)
    assert _read_hold()["reason"] == "42"


# --------------------------------------------------------------------------- #
# set_hold — OSError is swallowed (a hold-file hiccup must never mask the LLM error)
# --------------------------------------------------------------------------- #
def test_set_hold_swallows_write_text_oserror(clock, monkeypatch):
    class ExplodingPath:
        def write_text(self, *a, **k):
            raise OSError("disk full")

        def exists(self):
            return False

    monkeypatch.setattr(quota, "HOLD_PATH", ExplodingPath())
    future = clock.t + 1000

    # returns the effective retry_at despite the failed write; no exception, no file.
    assert quota.set_hold(FakeCfg(), "x", future) == future
    assert quota.exists() is False


def test_set_hold_swallows_mkdir_oserror(clock, monkeypatch):
    class ExplodingDir:
        def mkdir(self, *a, **k):
            raise OSError("read-only fs")

    monkeypatch.setattr(quota, "STATE_DIR", ExplodingDir())
    future = clock.t + 1000

    # mkdir raises before write is attempted -> real tmp HOLD_PATH stays absent.
    assert quota.set_hold(FakeCfg(), "x", future) == future
    assert quota.HOLD_PATH.exists() is False


# --------------------------------------------------------------------------- #
# status — active
# --------------------------------------------------------------------------- #
def test_status_active_after_set_hold(clock):
    retry_at = clock.t + 3600
    quota.set_hold(FakeCfg(), "over the limit", retry_at)
    active, msg = quota.status()

    assert active is True
    assert "usage limit" in msg
    assert "retrying after" in msg
    assert "over the limit" in msg
    # The rendered HH:MM must come from retry_at (not set_at/now). We can't pin the
    # literal (LOCAL-tz dependent) but we CAN pin *which* timestamp it reflects: the
    # shown time equals retry_at's local render and differs from now's (they are an
    # hour apart, so HH always differs regardless of the runner's timezone).
    m = re.search(r"retrying after (\d{2}:\d{2}) \(", msg)
    assert m, "message should carry an HH:MM retry time: %r" % msg
    shown = m.group(1)
    assert shown == datetime.datetime.fromtimestamp(retry_at).strftime("%H:%M")
    assert shown != datetime.datetime.fromtimestamp(clock.t).strftime("%H:%M")


def test_status_message_truncates_reason_to_120(clock):
    quota.set_hold(FakeCfg(), "y" * 200, clock.t + 3600)
    active, msg = quota.status()

    assert active is True
    assert "y" * 120 in msg
    assert "y" * 121 not in msg


def test_status_active_with_missing_reason_key(clock):
    # A hand-written hold with retry_at but no reason -> `.get("reason", "")` default.
    quota.HOLD_PATH.write_text(json.dumps({"retry_at": clock.t + 100}))
    active, msg = quota.status()

    assert active is True
    assert "usage limit" in msg
    assert msg.endswith("()")


# --------------------------------------------------------------------------- #
# status — expired (inactive, but the file is deliberately left in place)
# --------------------------------------------------------------------------- #
def test_status_expired_at_boundary_leaves_file(clock):
    retry_at = quota.set_hold(FakeCfg(), "limited", clock.t + 100)
    assert quota.HOLD_PATH.exists()

    clock.t = retry_at  # now == retry_at -> `now >= retry_at` is True -> expired
    assert quota.status() == (False, "")
    assert quota.HOLD_PATH.exists()  # NOT unlinked; the probe job re-checks it


def test_status_expired_strictly_past(clock):
    retry_at = quota.set_hold(FakeCfg(), "limited", clock.t + 100)
    clock.t = retry_at + 50
    assert quota.status() == (False, "")


# --------------------------------------------------------------------------- #
# status — missing / corrupt guards all read as inactive
# --------------------------------------------------------------------------- #
def test_status_missing_file(clock):
    assert not quota.HOLD_PATH.exists()
    assert quota.status() == (False, "")  # OSError (FileNotFoundError) branch


def test_status_corrupt_json(clock):
    quota.HOLD_PATH.write_text("{not valid json")
    assert quota.status() == (False, "")  # ValueError from json.loads


def test_status_missing_retry_at_key(clock):
    quota.HOLD_PATH.write_text("{}")
    assert quota.status() == (False, "")  # KeyError


def test_status_non_float_retry_at(clock):
    quota.HOLD_PATH.write_text(json.dumps({"retry_at": "abc"}))
    assert quota.status() == (False, "")  # ValueError from float("abc")


def test_status_null_retry_at(clock):
    quota.HOLD_PATH.write_text(json.dumps({"retry_at": None}))
    assert quota.status() == (False, "")  # TypeError from float(None)


# --------------------------------------------------------------------------- #
# exists / clear
# --------------------------------------------------------------------------- #
def test_exists_and_clear_roundtrip(clock):
    assert quota.exists() is False
    quota.set_hold(FakeCfg(), "x", clock.t + 1000)
    assert quota.exists() is True
    quota.clear()
    assert quota.exists() is False


def test_clear_absent_file_is_noop():
    assert not quota.HOLD_PATH.exists()
    quota.clear()  # unlink raises FileNotFoundError -> swallowed
    quota.clear()  # idempotent
    assert quota.exists() is False


# --------------------------------------------------------------------------- #
# round trip: an armed hold reads back active, then expired once the clock passes it
# --------------------------------------------------------------------------- #
def test_set_hold_status_full_lifecycle(clock):
    retry_at = quota.set_hold(FakeCfg(quota_recheck_min=30), "quota reset soon", None)
    assert retry_at == clock.t + 1800

    active, msg = quota.status()
    assert active is True
    assert "quota reset soon" in msg

    clock.t = retry_at  # window elapsed
    assert quota.status() == (False, "")
    assert quota.exists() is True  # still on disk for the probe job

    quota.clear()
    assert quota.exists() is False
