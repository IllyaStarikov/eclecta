"""Tests for signalpipe.downtime — the macOS downtime gate for local inference.

Every probe shells out via ``_run`` (subprocess) or ``urllib.request.urlopen``;
all of those boundaries are faked here, so the suite is fully hermetic (NO real
network, NO real ``pmset``/``ioreg``/``vm_stat``/``sysctl`` unless a test is
explicitly ``@pytest.mark.integration``/``live``).

``conftest.redirect_state_dirs`` (autouse) already repoints ``PAUSE_FILE`` and
``DIGEST_LOCK`` at a tmp ``state`` dir. ``set_digest_lock``/``pause`` also call
``STATE_DIR.mkdir(...)`` (a *separate* module reference the conftest does not
patch), so the ``state_dir`` fixture below patches that too to keep writes off
the real ``~/.local/state``.
"""

from __future__ import annotations

import json
import os
import sys
import types
import urllib.error

import pytest

import signalpipe.downtime as downtime


# --------------------------------------------------------------------------- #
# Local helpers / fixtures
# --------------------------------------------------------------------------- #
def make_cfg(downtime_cfg=None, backend=None):
    """A minimal stand-in for ``config.Config`` exposing only what the gate reads:
    ``.data`` (dict, holds the ``downtime`` block) and ``.backend`` (dict)."""
    cfg = types.SimpleNamespace()
    cfg.data = {"downtime": dict(downtime_cfg or {})}
    cfg.backend = {} if backend is None else backend
    return cfg


class _Resp:
    """Fake urllib response usable as a context manager (like ``urlopen``'s)."""

    def __init__(self, status=200, body=b""):
        self.status = status
        self._body = body if isinstance(body, bytes) else body.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def state_dir(monkeypatch):
    """Point ``downtime.STATE_DIR`` at the tmp state dir the conftest already made
    (``DIGEST_LOCK.parent``), so ``set_digest_lock``/``pause`` mkdir into tmp."""
    sd = downtime.DIGEST_LOCK.parent
    monkeypatch.setattr(downtime, "STATE_DIR", sd)
    return sd


@pytest.fixture
def patch_run(monkeypatch):
    """Return a setter that installs canned ``_run`` stdout."""

    def _set(output):
        monkeypatch.setattr(downtime, "_run", lambda argv, timeout=5: output)

    return _set


@pytest.fixture
def all_open(monkeypatch):
    """Monkeypatch every probe so ``is_open`` would otherwise pass; return the
    frozen ``time.time()`` value tests can build ``paused_until`` deltas against."""
    T = 1_000_000.0
    monkeypatch.setattr(downtime.time, "time", lambda: T)
    monkeypatch.setattr(downtime, "paused_until", lambda: 0.0)
    monkeypatch.setattr(downtime, "on_ac", lambda: True)
    monkeypatch.setattr(downtime, "idle_seconds", lambda: 100_000.0)
    monkeypatch.setattr(downtime, "thermal_ok", lambda: True)
    monkeypatch.setattr(downtime, "digest_in_flight", lambda: False)
    monkeypatch.setattr(downtime, "mem_available_gb", lambda: 128.0)
    monkeypatch.setattr(downtime, "swap_used_gb", lambda: 0.0)
    monkeypatch.setattr(downtime, "ollama_up", lambda cfg: True)
    return T


# --------------------------------------------------------------------------- #
# parse_duration
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text,expected",
    [
        ("2h", 7200),
        ("30m", 1800),
        ("90s", 90),
        ("45", 2700),  # bare number == minutes
        ("2.5h", 9000),
        ("1.5m", 90),
        ("0.5s", 0),  # int(float(0.5)) == 0
        ("2H", 7200),  # case-insensitive
        ("  2h ", 7200),  # trimmed
        ("0", 0),
    ],
)
def test_parse_duration_valid(text, expected):
    assert downtime.parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", None, "garbage", "  ", "abc h", "h", "m"])
def test_parse_duration_falls_back_to_default(text):
    assert downtime.parse_duration(text) == 7200
    assert downtime.parse_duration(text, default_sec=42) == 42


def test_parse_duration_custom_default_only_on_bad_input():
    # good input ignores default; bad input uses it
    assert downtime.parse_duration("10m", default_sec=999) == 600
    assert downtime.parse_duration("nope", default_sec=999) == 999


@pytest.mark.property
def test_parse_duration_numeric_suffixed_never_raises():
    hypothesis = pytest.importorskip("hypothesis")
    from hypothesis import given
    from hypothesis import strategies as st

    @given(
        st.integers(min_value=0, max_value=10**7),
        st.sampled_from(["h", "m", "s", ""]),
    )
    def _check(n, suffix):
        result = downtime.parse_duration("%d%s" % (n, suffix))
        assert isinstance(result, int)
        # non-negative input can never yield a negative duration, and a bare/`s`
        # suffix is a lossless pass-through (int(float(n)) == n)
        assert result >= 0
        if suffix in ("s", ""):
            assert result == (n if suffix == "s" else n * 60)

    _check()


# --------------------------------------------------------------------------- #
# mem_available_gb
# --------------------------------------------------------------------------- #
def _vm_stat(page_line, free, inactive, speculative, active=999999):
    return "\n".join(
        [
            page_line,
            "Pages free:                            %d." % free,
            "Pages active:                          %d." % active,
            "Pages inactive:                        %d." % inactive,
            "Pages speculative:                     %d." % speculative,
            "Pages wired down:                      12345.",
        ]
    )


def test_mem_available_gb_apple_silicon_16k_pages(patch_run):
    # free+inactive+speculative = 160000 pages @ 16384 bytes
    out = _vm_stat(
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)",
        free=100_000,
        inactive=50_000,
        speculative=10_000,
    )
    patch_run(out)
    expected = 160_000 * 16384 / 1_000_000_000.0  # 2.62144
    assert downtime.mem_available_gb() == pytest.approx(expected)


def test_mem_available_gb_defaults_to_4096_when_page_line_absent(patch_run):
    out = _vm_stat(
        "Mach Virtual Memory Statistics:",  # no 'page size of'
        free=100_000,
        inactive=50_000,
        speculative=10_000,
    )
    patch_run(out)
    expected = 160_000 * 4096 / 1_000_000_000.0  # 0.65536
    assert downtime.mem_available_gb() == pytest.approx(expected)


def test_mem_available_gb_malformed_page_size_falls_back_to_4096(patch_run):
    out = _vm_stat(
        "Mach Virtual Memory Statistics: (page size of NOPE bytes)",
        free=1000,
        inactive=0,
        speculative=0,
    )
    patch_run(out)
    assert downtime.mem_available_gb() == pytest.approx(1000 * 4096 / 1e9)


def test_mem_available_gb_empty_is_fail_safe_zero(patch_run):
    patch_run("")
    assert downtime.mem_available_gb() == 0.0


def test_mem_available_gb_skips_unparseable_count_lines(patch_run):
    out = "\n".join(
        [
            "Mach Virtual Memory Statistics: (page size of 4096 bytes)",
            "Pages free:                            notanumber.",
            "Pages inactive:                        2000.",
            "Pages speculative:                     0.",
        ]
    )
    patch_run(out)
    # free line unparseable -> contributes 0; only inactive counts
    assert downtime.mem_available_gb() == pytest.approx(2000 * 4096 / 1e9)


# --------------------------------------------------------------------------- #
# swap_used_gb
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "sysctl,expected",
    [
        ("total = 2048.00M  used = 1024.50M  free = 1023.50M", 1024.5 / 1024.0),
        ("total = 4.00G  used = 2.00G  free = 2.00G", 2.0),
        ("total = 2048.00M  used = 100.00K  free = 1948.00M", 100.0 / 1024.0 / 1024.0),
        ("total = 2048.00M  used = 0.00M  free = 2048.00M", 0.0),
    ],
)
def test_swap_used_gb_unit_conversion(patch_run, sysctl, expected):
    patch_run(sysctl)
    assert downtime.swap_used_gb() == pytest.approx(expected)


@pytest.mark.parametrize("sysctl", ["", "garbage with no marker", "total = 1M  used = abcM  x"])
def test_swap_used_gb_fail_safe_zero(patch_run, sysctl):
    patch_run(sysctl)
    assert downtime.swap_used_gb() == 0.0


# --------------------------------------------------------------------------- #
# on_ac
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "out,expected",
    [
        (" Now drawing from 'AC Power'\n", True),
        ("Currently drawing from 'Battery Power'\nNo adapter attached.\n", False),
        ("", False),  # fail-safe: unknown -> on battery
    ],
)
def test_on_ac(patch_run, out, expected):
    patch_run(out)
    assert downtime.on_ac() is expected


# --------------------------------------------------------------------------- #
# idle_seconds
# --------------------------------------------------------------------------- #
def test_idle_seconds_parses_hid_idle_time(patch_run):
    patch_run('  |   "HIDIdleTime" = 300000000000\n')  # 300s in ns
    assert downtime.idle_seconds() == pytest.approx(300.0)


def test_idle_seconds_missing_line_is_zero(patch_run):
    patch_run("some ioreg output\nwith no idle field\n")
    assert downtime.idle_seconds() == 0.0


def test_idle_seconds_empty_is_zero(patch_run):
    patch_run("")
    assert downtime.idle_seconds() == 0.0


def test_idle_seconds_malformed_value_is_zero(patch_run):
    patch_run('    "HIDIdleTime" = not_a_number\n')
    assert downtime.idle_seconds() == 0.0


# --------------------------------------------------------------------------- #
# thermal_ok
# --------------------------------------------------------------------------- #
def test_thermal_ok_empty_fails_open(patch_run):
    patch_run("")  # Apple Silicon healthy box emits nothing
    assert downtime.thermal_ok() is True


def test_thermal_ok_healthy_no_warning_line(patch_run):
    patch_run("Note: No thermal warning level has been recorded\n")
    assert downtime.thermal_ok() is True


def test_thermal_ok_full_speed_limit_is_ok(patch_run):
    patch_run("CPU_Speed_Limit = 100\n")
    assert downtime.thermal_ok() is True


@pytest.mark.parametrize("line", ["CPU_Speed_Limit = 80\n", "CPU_Speed_Limit = 70%\n"])
def test_thermal_ok_throttled_speed_limit(patch_run, line):
    patch_run(line)
    assert downtime.thermal_ok() is False


def test_thermal_ok_malformed_speed_limit_reads_throttled(patch_run):
    patch_run("CPU_Speed_Limit = ??\n")
    assert downtime.thermal_ok() is False


def test_thermal_ok_active_warning_level_is_throttled(patch_run):
    patch_run("CPU thermal warning level 2\n")
    assert downtime.thermal_ok() is False


# --------------------------------------------------------------------------- #
# _run
# --------------------------------------------------------------------------- #
def test_run_returns_stdout(monkeypatch):
    monkeypatch.setattr(
        downtime.subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout="hello")
    )
    assert downtime._run(["/bin/true"]) == "hello"


def test_run_none_stdout_becomes_empty(monkeypatch):
    monkeypatch.setattr(
        downtime.subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout=None)
    )
    assert downtime._run(["/bin/true"]) == ""


def test_run_oserror_returns_empty(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("no such binary")

    monkeypatch.setattr(downtime.subprocess, "run", boom)
    assert downtime._run(["/nope"]) == ""


def test_run_subprocess_error_returns_empty(monkeypatch):
    def boom(*a, **k):
        raise downtime.subprocess.TimeoutExpired(cmd=["x"], timeout=5)

    monkeypatch.setattr(downtime.subprocess, "run", boom)
    assert downtime._run(["/bin/sleep", "10"], timeout=1) == ""


@pytest.mark.integration
def test_run_real_echo():
    # real subprocess, no network; cross-platform binary
    assert "hi" in downtime._run(["/bin/echo", "hi"])


# --------------------------------------------------------------------------- #
# _base_url
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "backend,expected",
    [
        ({}, "http://127.0.0.1:11434"),
        ({"local": None}, "http://127.0.0.1:11434"),
        ({"local": {}}, "http://127.0.0.1:11434"),
        ({"local": {"base_url": "http://host:1234/"}}, "http://host:1234"),
        ({"local": {"base_url": "http://host:1234///"}}, "http://host:1234"),
    ],
)
def test_base_url(backend, expected):
    assert downtime._base_url(make_cfg(backend=backend)) == expected


# --------------------------------------------------------------------------- #
# ollama_up
# --------------------------------------------------------------------------- #
def test_ollama_up_true_and_hits_api_tags(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        return _Resp(200)

    monkeypatch.setattr(downtime.urllib.request, "urlopen", fake_urlopen)
    cfg = make_cfg(backend={"local": {"base_url": "http://127.0.0.1:11434/"}})
    assert downtime.ollama_up(cfg) is True
    assert calls == ["http://127.0.0.1:11434/api/tags"]


def test_ollama_up_non_200_is_false(monkeypatch):
    monkeypatch.setattr(downtime.urllib.request, "urlopen", lambda req, timeout=None: _Resp(503))
    assert downtime.ollama_up(make_cfg()) is False


@pytest.mark.parametrize("exc", [urllib.error.URLError("down"), OSError("boom"), ValueError("bad")])
def test_ollama_up_errors_are_false(monkeypatch, exc):
    def raiser(req, timeout=None):
        raise exc

    monkeypatch.setattr(downtime.urllib.request, "urlopen", raiser)
    assert downtime.ollama_up(make_cfg()) is False


# --------------------------------------------------------------------------- #
# ollama_unload
# --------------------------------------------------------------------------- #
def test_ollama_unload_unloads_all_running_models(monkeypatch):
    ps_body = json.dumps({"models": [{"name": "llama3.1:70b"}, {"model": "qwen2.5:14b"}]}).encode(
        "utf-8"
    )
    gen_calls = []

    def fake_urlopen(req, timeout=None):
        if req.full_url.endswith("/api/ps"):
            return _Resp(200, ps_body)
        if req.full_url.endswith("/api/generate"):
            gen_calls.append(json.loads(req.data.decode("utf-8")))
            return _Resp(200, b'{"done":true}')
        raise AssertionError("unexpected url %s" % req.full_url)

    monkeypatch.setattr(downtime.urllib.request, "urlopen", fake_urlopen)
    cfg = make_cfg(backend={"local": {"base_url": "http://127.0.0.1:11434"}})
    assert downtime.ollama_unload(cfg) == ["llama3.1:70b", "qwen2.5:14b"]
    assert gen_calls == [
        {"model": "llama3.1:70b", "keep_alive": 0},
        {"model": "qwen2.5:14b", "keep_alive": 0},
    ]


def test_ollama_unload_skips_nameless_and_failed_models(monkeypatch):
    ps_body = json.dumps({"models": [{"foo": "bar"}, {"name": "good"}, {"name": "bad"}]}).encode(
        "utf-8"
    )

    def fake_urlopen(req, timeout=None):
        if req.full_url.endswith("/api/ps"):
            return _Resp(200, ps_body)
        payload = json.loads(req.data.decode("utf-8"))
        if payload["model"] == "bad":
            raise urllib.error.URLError("generate failed")
        return _Resp(200, b"{}")

    monkeypatch.setattr(downtime.urllib.request, "urlopen", fake_urlopen)
    assert downtime.ollama_unload(make_cfg()) == ["good"]


def test_ollama_unload_ps_unreachable_returns_empty(monkeypatch):
    def raiser(req, timeout=None):
        raise urllib.error.URLError("ps down")

    monkeypatch.setattr(downtime.urllib.request, "urlopen", raiser)
    assert downtime.ollama_unload(make_cfg()) == []


def test_ollama_unload_null_models_returns_empty(monkeypatch):
    monkeypatch.setattr(
        downtime.urllib.request,
        "urlopen",
        lambda req, timeout=None: _Resp(200, json.dumps({"models": None}).encode("utf-8")),
    )
    assert downtime.ollama_unload(make_cfg()) == []


# --------------------------------------------------------------------------- #
# digest lock
# --------------------------------------------------------------------------- #
def test_digest_lock_roundtrip(state_dir):
    assert downtime.digest_in_flight() is False
    downtime.set_digest_lock()
    assert downtime.DIGEST_LOCK.exists()
    assert downtime.digest_in_flight() is True
    downtime.clear_digest_lock()
    assert not downtime.DIGEST_LOCK.exists()
    assert downtime.digest_in_flight() is False


def test_digest_in_flight_boundary(state_dir, monkeypatch):
    downtime.set_digest_lock()
    mtime = downtime.DIGEST_LOCK.stat().st_mtime
    monkeypatch.setattr(downtime.time, "time", lambda: mtime + 1799)
    assert downtime.digest_in_flight() is True  # under 1800s
    monkeypatch.setattr(downtime.time, "time", lambda: mtime + 1801)
    assert downtime.digest_in_flight() is False  # stale


def test_digest_in_flight_missing_file_is_false(state_dir):
    assert not downtime.DIGEST_LOCK.exists()
    assert downtime.digest_in_flight() is False


def test_clear_digest_lock_missing_is_noop(state_dir):
    # no file present; must not raise
    downtime.clear_digest_lock()
    assert downtime.digest_in_flight() is False


def test_set_digest_lock_writes_int_timestamp(state_dir, monkeypatch):
    monkeypatch.setattr(downtime.time, "time", lambda: 1_700_000_000.9)
    downtime.set_digest_lock()
    assert downtime.DIGEST_LOCK.read_text().strip() == "1700000000"


def test_set_digest_lock_swallows_oserror(state_dir, monkeypatch):
    class _Boom:
        def write_text(self, *a, **k):
            raise OSError("read-only fs")

    monkeypatch.setattr(downtime, "DIGEST_LOCK", _Boom())
    downtime.set_digest_lock()  # must not raise


# --------------------------------------------------------------------------- #
# manual pause
# --------------------------------------------------------------------------- #
def test_pause_paused_until_resume_roundtrip(state_dir, monkeypatch):
    T = 2_000_000.0
    monkeypatch.setattr(downtime.time, "time", lambda: T)
    until = downtime.pause(3600, reason="vacation")
    assert until == T + 3600
    assert downtime.paused_until() == T + 3600
    stored = json.loads(downtime.PAUSE_FILE.read_text())
    assert stored["reason"] == "vacation"
    assert stored["set_at"] == T
    downtime.resume()
    assert not downtime.PAUSE_FILE.exists()
    assert downtime.paused_until() == 0.0


def test_pause_negative_seconds_clamped_to_zero(state_dir, monkeypatch):
    T = 3_000_000.0
    monkeypatch.setattr(downtime.time, "time", lambda: T)
    assert downtime.pause(-500) == T  # max(0, -500) == 0
    # the clamped instant must also be what got persisted (not T - 500)
    assert downtime.paused_until() == T
    assert json.loads(downtime.PAUSE_FILE.read_text())["paused_until"] == T


def test_paused_until_missing_file_is_zero(state_dir):
    assert not downtime.PAUSE_FILE.exists()
    assert downtime.paused_until() == 0.0


def test_paused_until_corrupt_json_is_zero(state_dir):
    downtime.PAUSE_FILE.write_text("{ this is not json")
    assert downtime.paused_until() == 0.0


def test_resume_missing_file_is_noop(state_dir):
    downtime.resume()  # must not raise
    assert downtime.paused_until() == 0.0


def test_pause_write_failure_prints_and_returns_one(monkeypatch, capsys):
    class _Boom:
        def write_text(self, *a, **k):
            raise OSError("disk full")

    # STATE_DIR.mkdir must succeed; PAUSE_FILE.write_text must fail
    monkeypatch.setattr(downtime, "STATE_DIR", downtime.DIGEST_LOCK.parent)
    monkeypatch.setattr(downtime, "PAUSE_FILE", _Boom())
    assert downtime.pause(60) == 1
    assert "could not write pause file" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# is_open composite gate
# --------------------------------------------------------------------------- #
def test_is_open_gating_disabled_short_circuits(all_open):
    ok, reason = downtime.is_open(make_cfg({"enabled": False}))
    assert (ok, reason) == (True, "gating disabled")


def test_is_open_all_pass(all_open):
    ok, reason = downtime.is_open(make_cfg())
    assert (ok, reason) == (True, "open")


def test_is_open_paused(all_open, monkeypatch):
    T = all_open
    monkeypatch.setattr(downtime, "paused_until", lambda: T + 3600)
    ok, reason = downtime.is_open(make_cfg())
    assert ok is False
    assert reason == "paused (60m left)"


def test_is_open_on_battery(all_open, monkeypatch):
    monkeypatch.setattr(downtime, "on_ac", lambda: False)
    assert downtime.is_open(make_cfg()) == (False, "on battery")


def test_is_open_require_ac_false_skips_battery(all_open, monkeypatch):
    monkeypatch.setattr(downtime, "on_ac", lambda: False)
    assert downtime.is_open(make_cfg({"require_ac": False})) == (True, "open")


def test_is_open_user_active(all_open, monkeypatch):
    monkeypatch.setattr(downtime, "idle_seconds", lambda: 10.0)
    # default idle_min == 5 -> need 300s
    assert downtime.is_open(make_cfg()) == (False, "user active (idle 10s < 300s)")


def test_is_open_custom_idle_min(all_open, monkeypatch):
    monkeypatch.setattr(downtime, "idle_seconds", lambda: 30.0)
    assert downtime.is_open(make_cfg({"idle_min": 1})) == (
        False,
        "user active (idle 30s < 60s)",
    )


def test_is_open_thermal_throttling(all_open, monkeypatch):
    monkeypatch.setattr(downtime, "thermal_ok", lambda: False)
    assert downtime.is_open(make_cfg()) == (False, "thermal throttling")


def test_is_open_thermal_guard_false_skips(all_open, monkeypatch):
    monkeypatch.setattr(downtime, "thermal_ok", lambda: False)
    assert downtime.is_open(make_cfg({"thermal_guard": False})) == (True, "open")


def test_is_open_digest_in_flight(all_open, monkeypatch):
    monkeypatch.setattr(downtime, "digest_in_flight", lambda: True)
    assert downtime.is_open(make_cfg()) == (False, "digest in flight")


def test_is_open_low_ram_when_need_gb_set(all_open, monkeypatch):
    monkeypatch.setattr(downtime, "mem_available_gb", lambda: 12.3)
    assert downtime.is_open(make_cfg(), need_gb=50) == (
        False,
        "low RAM (12 < 50 GB free)",
    )


def test_is_open_sufficient_ram_continues(all_open, monkeypatch):
    monkeypatch.setattr(downtime, "mem_available_gb", lambda: 128.0)
    # need_gb set but plenty free -> RAM check passes, gate stays open
    assert downtime.is_open(make_cfg(), need_gb=16) == (True, "open")


def test_is_open_need_gb_zero_skips_ram_check(all_open, monkeypatch):
    monkeypatch.setattr(downtime, "mem_available_gb", lambda: 0.0)
    # need_gb default 0.0 is falsy -> RAM never checked
    assert downtime.is_open(make_cfg()) == (True, "open")


def test_is_open_swap_pressure(all_open, monkeypatch):
    monkeypatch.setattr(downtime, "swap_used_gb", lambda: 8.5)
    assert downtime.is_open(make_cfg()) == (False, "swap pressure (8.5 GB)")


def test_is_open_swap_guard_false_skips(all_open, monkeypatch):
    monkeypatch.setattr(downtime, "swap_used_gb", lambda: 100.0)
    assert downtime.is_open(make_cfg({"swap_thrash_guard": False})) == (True, "open")


def test_is_open_custom_swap_max(all_open, monkeypatch):
    monkeypatch.setattr(downtime, "swap_used_gb", lambda: 10.0)
    # under a raised ceiling -> open
    assert downtime.is_open(make_cfg({"swap_used_max_gb": 20})) == (True, "open")
    # over a lowered ceiling -> closed
    assert downtime.is_open(make_cfg({"swap_used_max_gb": 5})) == (
        False,
        "swap pressure (10.0 GB)",
    )


def test_is_open_ollama_unreachable(all_open, monkeypatch):
    monkeypatch.setattr(downtime, "ollama_up", lambda cfg: False)
    assert downtime.is_open(make_cfg()) == (False, "ollama unreachable")


def test_is_open_ollama_preflight_false_skips(all_open, monkeypatch):
    monkeypatch.setattr(downtime, "ollama_up", lambda cfg: False)
    assert downtime.is_open(make_cfg({"ollama_preflight": False})) == (True, "open")


def test_is_open_reason_ordering_paused_beats_battery(all_open, monkeypatch):
    T = all_open
    monkeypatch.setattr(downtime, "paused_until", lambda: T + 60)
    monkeypatch.setattr(downtime, "on_ac", lambda: False)
    ok, reason = downtime.is_open(make_cfg())
    # paused is checked before AC, so the reason is the pause message (with its
    # 60s -> "1m" rounding), never "on battery"
    assert (ok, reason) == (False, "paused (1m left)")


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
def test_status_open(all_open):
    text = downtime.status(make_cfg())
    assert text.splitlines()[0] == "downtime gate: OPEN — local stages may run"
    assert "  on AC:        True" in text
    assert "  user idle:    100000s" in text
    assert "  thermal ok:   True" in text
    assert "  RAM free:     128 GB" in text
    assert "  swap used:    0.0 GB" in text
    assert "  ollama up:    True" in text
    assert "  digest lock:  False" in text


def test_status_closed_shows_reason(all_open, monkeypatch):
    monkeypatch.setattr(downtime, "on_ac", lambda: False)
    text = downtime.status(make_cfg())
    assert text.splitlines()[0] == "downtime gate: CLOSED  (on battery)"


def test_status_paused_line(all_open, monkeypatch):
    T = all_open
    monkeypatch.setattr(downtime, "paused_until", lambda: T + 600)  # 10 min
    text = downtime.status(make_cfg())
    assert "downtime gate: CLOSED" in text.splitlines()[0]
    assert "  paused: 10 min remaining" in text


# --------------------------------------------------------------------------- #
# live smoke (deselected by default; needs a real macOS box + SIGNAL_LIVE)
# --------------------------------------------------------------------------- #
@pytest.mark.live
def test_probes_live_smoke():
    if not os.environ.get("SIGNAL_LIVE"):
        pytest.skip("live probes: set SIGNAL_LIVE=1 to run against real binaries")
    if sys.platform != "darwin":
        pytest.skip("darwin-only probes")
    assert isinstance(downtime.on_ac(), bool)
    assert isinstance(downtime.idle_seconds(), float)
    assert isinstance(downtime.mem_available_gb(), float)
    assert isinstance(downtime.swap_used_gb(), float)
    assert isinstance(downtime.thermal_ok(), bool)
