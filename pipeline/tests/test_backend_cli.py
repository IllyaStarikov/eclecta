"""Tests for ``signalpipe.llm.backend_cli`` — the subscription backend that spawns
headless ``claude -p --json-schema`` over STDIN, parses the JSON envelope, detects
Max-plan usage limits (arming the quota hold), and does one repair retry.

Hermetic strategy:
* The only external boundary is ``subprocess.run``. It is replaced module-locally
  (``backend_cli.subprocess`` -> a ``SimpleNamespace`` whose ``run`` is a recorder and
  whose ``TimeoutExpired`` is the real class so the ``except`` clause still matches). No
  real process is ever spawned.
* ``quota.set_hold`` writes the hold file and ``quota.clear`` unlinks it. The autouse
  ``redirect_state_dirs`` conftest fixture already repoints ``quota.HOLD_PATH`` / STATE_DIR
  at tmp, so the REAL quota functions are safe to exercise; where an exact call needs to be
  asserted we patch ``backend_cli.quota.set_hold`` / ``.clear`` with recorders instead.
* ``run`` copies ``os.environ`` and pops ANTHROPIC_API_KEY from the *copy* — we set the key
  in the environment and assert the child env kwarg dropped it while the process env kept it.
"""

from __future__ import annotations

import json
import os
import subprocess
import types
from typing import Any, Dict, List, Optional

import pytest

import signalpipe.llm.backend_cli as bc
from signalpipe.llm import LLMError, UsageLimitExhausted


# --------------------------------------------------------------------------- #
# Local helpers / stubs
# --------------------------------------------------------------------------- #
SCHEMA: Dict[str, Any] = {
    "required": ["relevance"],
    "properties": {"relevance": {"type": "integer"}},
}


class _Cfg:
    """Minimal stand-in for ``config.Config``.

    ``run`` and ``quota.set_hold`` only ever touch ``cfg.backend.get(...)``; a plain dict
    supplies both the reads and the documented defaults (via ``dict.get``)."""

    def __init__(self, **backend: Any):
        self.backend: Dict[str, Any] = dict(backend)


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> types.SimpleNamespace:
    """A fake ``subprocess.CompletedProcess`` — ``run`` only reads these three attrs."""
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _envelope(**kw: Any) -> str:
    """Serialize a ``claude -p --output-format json`` envelope."""
    return json.dumps(kw)


class _FakeRun:
    """Records every call and yields queued results (a proc to return, or an exception
    instance/class to raise) in order."""

    def __init__(self, results: List[Any]):
        self.results = list(results)
        self.calls: List[types.SimpleNamespace] = []

    def __call__(
        self,
        argv,
        input=None,
        capture_output=None,
        text=None,
        timeout=None,
        env=None,
        cwd=None,
    ):
        self.calls.append(
            types.SimpleNamespace(
                argv=list(argv),
                input=input,
                timeout=timeout,
                env=dict(env) if env is not None else None,
                cwd=cwd,
            )
        )
        item = self.results.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, type) and issubclass(item, BaseException):
            raise item()
        return item


class _HoldRecorder:
    """Stand-in for ``quota.set_hold`` that records args and returns a fixed epoch."""

    def __init__(self, retry_at: float = 4102444800.0):
        self.calls: List[tuple] = []
        self.retry_at = retry_at

    def __call__(self, cfg, reason, retry_at=None):
        self.calls.append((cfg, reason, retry_at))
        return self.retry_at


class _ClearRecorder:
    def __init__(self):
        self.count = 0

    def __call__(self):
        self.count += 1


@pytest.fixture
def patch_run(monkeypatch):
    """Install a :class:`_FakeRun` as ``backend_cli.subprocess.run`` and return it.

    Patches a module-local ``SimpleNamespace`` so the real ``subprocess.run`` is never
    clobbered process-wide; ``TimeoutExpired`` is preserved as the real class."""

    def _install(*results: Any) -> _FakeRun:
        fake = _FakeRun(list(results))
        monkeypatch.setattr(
            bc,
            "subprocess",
            types.SimpleNamespace(run=fake, TimeoutExpired=subprocess.TimeoutExpired),
        )
        return fake

    return _install


# --------------------------------------------------------------------------- #
# _parse_reset_epoch — pure
# --------------------------------------------------------------------------- #
class TestParseResetEpoch:
    def test_epoch_seconds_tail(self):
        assert bc._parse_reset_epoch("usage limit reached|1751652000") == 1751652000.0

    def test_epoch_millis_tail_divided(self):
        # 13-digit tail (> 1e12) is treated as milliseconds and scaled to seconds.
        assert bc._parse_reset_epoch("limit|1751652000000") == 1751652000.0

    def test_epoch_with_whitespace_after_pipe(self):
        assert bc._parse_reset_epoch("blah |  1751652000 more") == 1751652000.0

    def test_iso_reset_offset_qualified(self):
        # Offset-qualified ISO => TZ-independent timestamp.
        assert bc._parse_reset_epoch("resets at 2026-07-04T03:00:00+00:00") == 1783134000.0

    def test_iso_reset_without_at(self):
        assert bc._parse_reset_epoch("resets 2026-07-04T03:00:00+00:00") == 1783134000.0

    def test_iso_reset_z_suffix_normalized(self):
        # The 'Z' is rewritten to '+00:00' before fromisoformat.
        assert bc._parse_reset_epoch("resets at 2026-07-04T03:00:00Z") == 1783134000.0

    def test_epoch_wins_over_iso_when_both_present(self):
        got = bc._parse_reset_epoch(
            "usage limit|1751652000 resets at 2026-07-04T03:00:00+00:00"
        )
        assert got == 1751652000.0

    def test_malformed_iso_returns_none(self):
        assert bc._parse_reset_epoch("resets at 2026-13-45T99:99:99Z") is None

    def test_no_hint_returns_none(self):
        assert bc._parse_reset_epoch("some unrelated error text") is None

    def test_short_number_not_matched_as_epoch(self):
        # < 10 digits after a pipe is not a reset epoch and there is no ISO hint.
        assert bc._parse_reset_epoch("foo|12345 bar") is None


# --------------------------------------------------------------------------- #
# _LIMIT_RE — pure regex include/exclude
# --------------------------------------------------------------------------- #
class TestLimitRe:
    @pytest.mark.parametrize(
        "text",
        [
            "Claude AI usage limit reached",
            "5-hour limit reached",
            "rate_limit_error",
            "rate limit exceeded",
            "RATE-LIMIT hit",  # case-insensitive + optional separator
            "out of usage",
            "out of extra usage",
            "quota exceeded",
        ],
    )
    def test_matches_documented_limit_spellings(self, text):
        assert bc._LIMIT_RE.search(text) is not None

    @pytest.mark.parametrize(
        "text",
        [
            "Reached maximum number of turns (1)",
            "Your credit balance is too low",
            "connection reset by peer",
            "generic api error",
        ],
    )
    def test_does_not_match_real_failures(self, text):
        assert bc._LIMIT_RE.search(text) is None


# --------------------------------------------------------------------------- #
# _raise_if_usage_limited
# --------------------------------------------------------------------------- #
class TestRaiseIfUsageLimited:
    def test_non_limit_detail_is_noop(self, monkeypatch):
        rec = _HoldRecorder()
        monkeypatch.setattr(bc.quota, "set_hold", rec)
        assert bc._raise_if_usage_limited("credit balance too low", _Cfg(), 0.05) is None
        assert rec.calls == []

    def test_limit_detail_arms_hold_and_raises(self, monkeypatch):
        rec = _HoldRecorder(retry_at=4102444800.0)
        monkeypatch.setattr(bc.quota, "set_hold", rec)
        cfg = _Cfg()
        detail = "Claude AI usage limit reached|1751652000"
        with pytest.raises(UsageLimitExhausted) as ei:
            bc._raise_if_usage_limited(detail, cfg, 0.07)
        exc = ei.value
        assert exc.retry_at == 4102444800.0
        assert exc.cost_usd == 0.07
        assert "subscription usage limit" in str(exc)
        # set_hold received (cfg, detail, parsed_epoch)
        assert len(rec.calls) == 1
        got_cfg, got_reason, got_epoch = rec.calls[0]
        assert got_cfg is cfg
        assert got_reason == detail
        assert got_epoch == 1751652000.0

    def test_zero_cost_coerced_to_float_zero(self, monkeypatch):
        monkeypatch.setattr(bc.quota, "set_hold", _HoldRecorder())
        with pytest.raises(UsageLimitExhausted) as ei:
            bc._raise_if_usage_limited("usage limit", _Cfg(), 0.0)
        assert ei.value.cost_usd == 0.0

    def test_real_quota_writes_hold_file(self, monkeypatch):
        # Uses the REAL quota.set_hold; HOLD_PATH is redirected to tmp by conftest.
        assert not bc.quota.HOLD_PATH.exists()
        detail = "Claude AI usage limit reached|4102444800"  # far-future => kept as-is
        with pytest.raises(UsageLimitExhausted) as ei:
            bc._raise_if_usage_limited(detail, _Cfg(quota_recheck_min=30), 0.5)
        assert ei.value.retry_at == 4102444800.0
        assert bc.quota.HOLD_PATH.exists()
        data = json.loads(bc.quota.HOLD_PATH.read_text())
        assert data["retry_at"] == 4102444800.0
        assert "usage limit" in data["reason"]


# --------------------------------------------------------------------------- #
# _extract_json — pure
# --------------------------------------------------------------------------- #
class TestExtractJson:
    def test_empty_text_returns_none(self):
        assert bc._extract_json("") is None

    def test_fenced_json_block(self):
        text = "Here you go:\n```json\n{\"a\": 1, \"b\": 2}\n```\nthanks"
        assert bc._extract_json(text) == {"a": 1, "b": 2}

    def test_fenced_without_lang_tag(self):
        text = "```\n{\"a\": 1}\n```"
        assert bc._extract_json(text) == {"a": 1}

    def test_brace_match_without_fence(self):
        text = 'prose before {"x": true} prose after'
        assert bc._extract_json(text) == {"x": True}

    def test_no_braces_returns_none(self):
        assert bc._extract_json("no json object here") is None

    def test_close_before_open_returns_none(self):
        # rfind('}') <= find('{') -> no object.
        assert bc._extract_json("} then {") is None

    def test_invalid_json_returns_none(self):
        assert bc._extract_json("{not valid json at all}") is None


# --------------------------------------------------------------------------- #
# _validate — pure
# --------------------------------------------------------------------------- #
class TestValidate:
    def test_non_dict_object(self):
        assert bc._validate("nope", {}) == "not an object"

    def test_missing_required_key(self):
        assert bc._validate({}, {"required": ["relevance"]}) == "missing required key 'relevance'"

    def test_wrong_type(self):
        err = bc._validate({"relevance": "eight"}, SCHEMA)
        assert err == "key 'relevance' has wrong type"

    def test_valid_returns_none(self):
        assert bc._validate({"relevance": 8}, SCHEMA) is None

    def test_none_value_skipped(self):
        # A present-but-None property is not type-checked.
        schema = {"properties": {"relevance": {"type": "integer"}}}
        assert bc._validate({"relevance": None}, schema) is None

    def test_absent_optional_property_skipped(self):
        schema = {"properties": {"relevance": {"type": "integer"}}}
        assert bc._validate({}, schema) is None

    def test_number_accepts_int_and_float(self):
        schema = {"properties": {"score": {"type": "number"}}}
        assert bc._validate({"score": 3}, schema) is None
        assert bc._validate({"score": 3.5}, schema) is None

    def test_boolean_true_ok(self):
        schema = {"properties": {"flag": {"type": "boolean"}}}
        assert bc._validate({"flag": True}, schema) is None

    def test_array_and_object_types(self):
        schema = {"properties": {"xs": {"type": "array"}, "o": {"type": "object"}}}
        assert bc._validate({"xs": [1], "o": {"k": 1}}, schema) is None
        assert bc._validate({"xs": "nope", "o": {}}, schema) == "key 'xs' has wrong type"

    def test_unknown_type_not_enforced(self):
        # A type absent from the map (expected is None) is skipped, not an error.
        schema = {"properties": {"whatever": {"type": "null"}}}
        assert bc._validate({"whatever": 123}, schema) is None


# --------------------------------------------------------------------------- #
# run() — argv / env / cwd construction
# --------------------------------------------------------------------------- #
@pytest.mark.integration
class TestRunArgv:
    def _ok_envelope(self, cost: float = 0.0) -> str:
        return _envelope(structured_output={"relevance": 8}, total_cost_usd=cost)

    def test_argv_default_shape(self, patch_run, monkeypatch):
        monkeypatch.setattr(bc.quota, "clear", _ClearRecorder())
        fake = patch_run(_proc(0, self._ok_envelope(0.3)))
        bc.run("claude-haiku-4-5", "SYS", "PROMPT", SCHEMA, _Cfg())
        argv = fake.calls[0].argv
        assert argv == [
            "claude",
            "-p",
            "--model", "claude-haiku-4-5",
            "--output-format", "json",
            "--json-schema", json.dumps(SCHEMA),
            "--max-turns", "4",
            "--permission-mode", "dontAsk",
            "--disallowedTools", "Bash Edit Write WebFetch WebSearch Task",
            "--system-prompt", "SYS",
        ]
        # STDIN carries the prompt (never argv).
        assert fake.calls[0].input == "PROMPT"
        assert fake.calls[0].cwd is None

    def test_effort_appended_when_given(self, patch_run, monkeypatch):
        monkeypatch.setattr(bc.quota, "clear", _ClearRecorder())
        fake = patch_run(_proc(0, self._ok_envelope()))
        bc.run("m", "s", "p", SCHEMA, _Cfg(), effort="max")
        argv = fake.calls[0].argv
        assert argv[-2:] == ["--effort", "max"]

    def test_no_effort_flag_by_default(self, patch_run, monkeypatch):
        monkeypatch.setattr(bc.quota, "clear", _ClearRecorder())
        fake = patch_run(_proc(0, self._ok_envelope()))
        bc.run("m", "s", "p", SCHEMA, _Cfg())
        assert "--effort" not in fake.calls[0].argv

    def test_custom_cli_bin_timeout_maxturns(self, patch_run, monkeypatch):
        monkeypatch.setattr(bc.quota, "clear", _ClearRecorder())
        fake = patch_run(_proc(0, self._ok_envelope()))
        cfg = _Cfg(cli_bin="/opt/claude", cli_timeout_sec=120, cli_max_turns=6)
        bc.run("m", "s", "p", SCHEMA, cfg)
        call = fake.calls[0]
        assert call.argv[0] == "/opt/claude"
        assert call.timeout == 120
        assert call.argv[call.argv.index("--max-turns") + 1] == "6"

    def test_extra_args_and_cwd_threaded(self, patch_run, monkeypatch):
        monkeypatch.setattr(bc.quota, "clear", _ClearRecorder())
        fake = patch_run(_proc(0, self._ok_envelope()))
        cfg = _Cfg(
            cli_extra_args=["--strict-mcp-config", "--setting-sources", "project"],
            cli_cwd="/some/dir",
        )
        bc.run("m", "s", "p", SCHEMA, cfg, effort="low")
        call = fake.calls[0]
        assert call.argv[-3:] == ["--strict-mcp-config", "--setting-sources", "project"]
        # extras come AFTER --effort
        assert call.argv.index("--effort") < len(call.argv) - 3
        assert call.cwd == "/some/dir"

    def test_extra_args_coerced_to_str(self, patch_run, monkeypatch):
        monkeypatch.setattr(bc.quota, "clear", _ClearRecorder())
        fake = patch_run(_proc(0, self._ok_envelope()))
        bc.run("m", "s", "p", SCHEMA, _Cfg(cli_extra_args=[123, True]))
        assert fake.calls[0].argv[-2:] == ["123", "True"]

    def test_empty_cli_cwd_becomes_none(self, patch_run, monkeypatch):
        monkeypatch.setattr(bc.quota, "clear", _ClearRecorder())
        fake = patch_run(_proc(0, self._ok_envelope()))
        bc.run("m", "s", "p", SCHEMA, _Cfg(cli_cwd=""))
        assert fake.calls[0].cwd is None

    def test_env_drops_api_key_but_keeps_others(self, patch_run, monkeypatch):
        monkeypatch.setattr(bc.quota, "clear", _ClearRecorder())
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
        monkeypatch.setenv("SIG_TEST_SENTINEL", "keepme")
        fake = patch_run(_proc(0, self._ok_envelope()))
        bc.run("m", "s", "p", SCHEMA, _Cfg())
        child_env = fake.calls[0].env
        assert "ANTHROPIC_API_KEY" not in child_env
        assert child_env["SIG_TEST_SENTINEL"] == "keepme"
        # The pop was on a copy: the process environment is untouched.
        assert os.environ.get("ANTHROPIC_API_KEY") == "sk-secret"


# --------------------------------------------------------------------------- #
# run() — success paths
# --------------------------------------------------------------------------- #
@pytest.mark.integration
class TestRunSuccess:
    def test_structured_output_happy(self, patch_run, monkeypatch):
        clear = _ClearRecorder()
        monkeypatch.setattr(bc.quota, "clear", clear)
        fake = patch_run(
            _proc(0, _envelope(structured_output={"relevance": 8}, total_cost_usd=0.3))
        )
        obj, cost = bc.run("m", "s", "p", SCHEMA, _Cfg())
        assert obj == {"relevance": 8}
        assert cost == 0.3
        assert clear.count == 1  # success clears the hold
        assert len(fake.calls) == 1  # no retry needed

    def test_result_fallback_when_structured_output_missing(self, patch_run, monkeypatch):
        monkeypatch.setattr(bc.quota, "clear", _ClearRecorder())
        result_text = "Sure:\n```json\n{\"relevance\": 5}\n```"
        fake = patch_run(_proc(0, _envelope(result=result_text, total_cost_usd=0.1)))
        obj, cost = bc.run("m", "s", "p", SCHEMA, _Cfg())
        assert obj == {"relevance": 5}
        assert cost == 0.1
        assert len(fake.calls) == 1

    def test_nonzero_exit_but_json_on_stdout_still_parsed(self, patch_run, monkeypatch):
        # returncode!=0 with a non-empty stdout skips the early-failure branch.
        monkeypatch.setattr(bc.quota, "clear", _ClearRecorder())
        env = _envelope(structured_output={"relevance": 2}, total_cost_usd=0.0)
        fake = patch_run(_proc(1, env, stderr="warning"))
        obj, cost = bc.run("m", "s", "p", SCHEMA, _Cfg())
        assert obj == {"relevance": 2}
        assert cost == 0.0
        assert len(fake.calls) == 1

    def test_missing_total_cost_defaults_zero(self, patch_run, monkeypatch):
        monkeypatch.setattr(bc.quota, "clear", _ClearRecorder())
        fake = patch_run(_proc(0, _envelope(structured_output={"relevance": 1})))
        obj, cost = bc.run("m", "s", "p", SCHEMA, _Cfg())
        assert obj == {"relevance": 1}
        assert cost == 0.0


# --------------------------------------------------------------------------- #
# run() — repair retry
# --------------------------------------------------------------------------- #
@pytest.mark.integration
class TestRunRepair:
    def test_repair_retry_then_success(self, patch_run, monkeypatch):
        clear = _ClearRecorder()
        monkeypatch.setattr(bc.quota, "clear", clear)
        # 1st: fenced JSON with wrong type -> validation fails.
        bad_result = "```json\n{\"relevance\": \"nope\"}\n```"
        first = _proc(0, _envelope(result=bad_result, total_cost_usd=0.1))
        second = _proc(0, _envelope(structured_output={"relevance": 9}, total_cost_usd=0.2))
        fake = patch_run(first, second)

        obj, cost = bc.run("m", "s", "PROMPT", SCHEMA, _Cfg())
        assert obj == {"relevance": 9}
        assert cost == pytest.approx(0.3)  # accumulated across both attempts
        assert clear.count == 1
        assert len(fake.calls) == 2
        # Second STDIN carries the repair suffix keyed off the first validation error.
        repair_input = fake.calls[1].input
        assert repair_input.startswith("PROMPT")
        assert "failed validation" in repair_input
        assert "wrong type" in repair_input
        # First attempt used the bare prompt.
        assert fake.calls[0].input == "PROMPT"

    def test_repair_from_unparseable_first_attempt(self, patch_run, monkeypatch):
        monkeypatch.setattr(bc.quota, "clear", _ClearRecorder())
        first = _proc(0, _envelope(result="no json here at all", total_cost_usd=0.0))
        second = _proc(0, _envelope(structured_output={"relevance": 4}, total_cost_usd=0.0))
        fake = patch_run(first, second)
        obj, _cost = bc.run("m", "s", "P", SCHEMA, _Cfg())
        assert obj == {"relevance": 4}
        assert "no parseable JSON" in fake.calls[1].input

    def test_invalid_after_retry_raises_llmerror(self, patch_run, monkeypatch):
        monkeypatch.setattr(bc.quota, "clear", _ClearRecorder())
        bad = _proc(0, _envelope(structured_output={"relevance": "x"}, total_cost_usd=0.2))
        fake = patch_run(bad, _proc(0, _envelope(structured_output={"relevance": "y"},
                                                 total_cost_usd=0.2)))
        with pytest.raises(LLMError) as ei:
            bc.run("m", "s", "p", SCHEMA, _Cfg())
        assert "invalid after retry" in str(ei.value)
        assert ei.value.cost_usd == pytest.approx(0.4)
        assert not isinstance(ei.value, UsageLimitExhausted)
        assert len(fake.calls) == 2


# --------------------------------------------------------------------------- #
# run() — is_error envelope handling
# --------------------------------------------------------------------------- #
@pytest.mark.integration
class TestRunIsError:
    def test_is_error_usage_limit_arms_hold(self, patch_run, monkeypatch):
        rec = _HoldRecorder(retry_at=4102444800.0)
        monkeypatch.setattr(bc.quota, "set_hold", rec)
        env = _envelope(
            is_error=True,
            errors="Claude AI usage limit reached|1751652000",
            total_cost_usd=0.05,
        )
        fake = patch_run(_proc(0, env))
        with pytest.raises(UsageLimitExhausted) as ei:
            bc.run("m", "s", "p", SCHEMA, _Cfg())
        assert ei.value.retry_at == 4102444800.0
        assert ei.value.cost_usd == 0.05  # envelope cost carried through
        assert len(rec.calls) == 1
        assert len(fake.calls) == 1  # no repair retry for infra errors

    def test_is_error_generic_raises_llmerror_with_status(self, patch_run, monkeypatch):
        rec = _HoldRecorder()
        monkeypatch.setattr(bc.quota, "set_hold", rec)
        env = _envelope(
            is_error=True,
            errors="Your credit balance is too low",
            api_error_status=400,
            total_cost_usd=0.0,
        )
        fake = patch_run(_proc(0, env))
        with pytest.raises(LLMError) as ei:
            bc.run("m", "s", "p", SCHEMA, _Cfg())
        exc = ei.value
        assert not isinstance(exc, UsageLimitExhausted)
        assert "400" in str(exc)
        assert "credit balance" in str(exc)
        assert exc.cost_usd is None  # 0.0 -> None
        assert rec.calls == []  # not a limit -> hold not armed
        assert len(fake.calls) == 1

    def test_is_error_falls_back_to_result_field(self, patch_run, monkeypatch):
        # No 'errors' key -> detail comes from 'result'.
        monkeypatch.setattr(bc.quota, "set_hold", _HoldRecorder())
        env = _envelope(is_error=True, result="quota exceeded", total_cost_usd=0.02)
        patch_run(_proc(0, env))
        with pytest.raises(UsageLimitExhausted) as ei:
            bc.run("m", "s", "p", SCHEMA, _Cfg())
        assert ei.value.cost_usd == 0.02


# --------------------------------------------------------------------------- #
# run() — process failure paths
# --------------------------------------------------------------------------- #
@pytest.mark.integration
class TestRunProcessFailures:
    def test_nonzero_exit_empty_stdout_raises_llmerror(self, patch_run, monkeypatch):
        monkeypatch.setattr(bc.quota, "set_hold", _HoldRecorder())
        fake = patch_run(_proc(2, stdout="   ", stderr="fatal boom"))
        with pytest.raises(LLMError) as ei:
            bc.run("m", "s", "p", SCHEMA, _Cfg())
        exc = ei.value
        assert not isinstance(exc, UsageLimitExhausted)
        assert "fatal boom" in str(exc)
        assert exc.cost_usd is None
        assert len(fake.calls) == 1

    def test_nonzero_exit_empty_stderr_uses_exit_code(self, patch_run, monkeypatch):
        monkeypatch.setattr(bc.quota, "set_hold", _HoldRecorder())
        patch_run(_proc(3, stdout="", stderr=""))
        with pytest.raises(LLMError) as ei:
            bc.run("m", "s", "p", SCHEMA, _Cfg())
        assert "exit 3" in str(ei.value)

    def test_nonzero_exit_stderr_usage_limit_raises_usage_exhausted(self, patch_run, monkeypatch):
        rec = _HoldRecorder(retry_at=4102444800.0)
        monkeypatch.setattr(bc.quota, "set_hold", rec)
        patch_run(_proc(1, stdout="", stderr="Claude AI usage limit reached"))
        with pytest.raises(UsageLimitExhausted) as ei:
            bc.run("m", "s", "p", SCHEMA, _Cfg())
        assert ei.value.retry_at == 4102444800.0
        assert len(rec.calls) == 1

    def test_timeout_raises_llmerror_cost_none(self, patch_run):
        fake = patch_run(subprocess.TimeoutExpired(cmd="claude", timeout=240))
        with pytest.raises(LLMError) as ei:
            bc.run("m", "s", "p", SCHEMA, _Cfg())
        exc = ei.value
        assert "timed out" in str(exc)
        assert exc.cost_usd is None
        assert not isinstance(exc, UsageLimitExhausted)
        assert len(fake.calls) == 1

    def test_oserror_raises_llmerror_cost_zero(self, patch_run):
        fake = patch_run(FileNotFoundError("no such binary"))
        with pytest.raises(LLMError) as ei:
            bc.run("m", "s", "p", SCHEMA, _Cfg(cli_bin="/nope/claude"))
        exc = ei.value
        assert "cannot exec" in str(exc)
        assert exc.cost_usd == 0.0  # never executed => definitively free
        assert len(fake.calls) == 1

    def test_non_json_stdout_raises_llmerror(self, patch_run):
        fake = patch_run(_proc(0, stdout="this is not json"))
        with pytest.raises(LLMError) as ei:
            bc.run("m", "s", "p", SCHEMA, _Cfg())
        exc = ei.value
        assert "non-JSON envelope" in str(exc)
        assert exc.cost_usd is None
        assert len(fake.calls) == 1


# --------------------------------------------------------------------------- #
# Live smoke — opt-in only (real `claude` CLI, real subscription spend)
# --------------------------------------------------------------------------- #
@pytest.mark.live
def test_live_claude_p_schema_roundtrip():
    if not os.environ.get("SIGNAL_LIVE"):
        pytest.skip("live: set SIGNAL_LIVE=1 with an authenticated `claude` CLI to run")
    import shutil

    if shutil.which("claude") is None:
        pytest.skip("live: `claude` CLI not on PATH")
    cfg = _Cfg(cli_bin="claude", cli_timeout_sec=120)
    schema = {"required": ["word"], "properties": {"word": {"type": "string"}}}
    obj, cost = bc.run(
        "claude-haiku-4-5",
        "You reply only with the requested JSON.",
        "Return a JSON object with a single key 'word' whose value is 'ping'.",
        schema,
        cfg,
    )
    assert isinstance(obj, dict) and isinstance(obj.get("word"), str)
    assert cost >= 0.0
