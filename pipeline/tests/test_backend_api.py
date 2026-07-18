"""Tests for ``signalpipe.llm.backend_api`` — the metered anthropic-SDK backend.

Hermetic: the ``anthropic`` package is present (0.62.0), but every call boundary
is faked. We patch ``anthropic.Anthropic`` to a fake client whose
``messages.create`` returns a canned response or raises, so NO network happens
and NO real API key is needed. The constructed client's key read is bypassed
entirely because the constructor itself is replaced.

Covers:
- ``_cost`` math: pricing lookup, unknown-model default, missing/None cache
  fields, the ``or 0`` guard.
- ``run`` kwargs/tool construction (forced single tool, schema identity,
  max_tokens, messages, model threading, max_retries threading).
- ``run`` system prompt-cache branch (list-with-cache_control vs plain string).
- ``run`` happy extraction path (tool_use.input dict -> (data, cost)).
- ``run`` failure mapping to ``LLMError`` (non-dict input, no tool_use block,
  wrong tool name, APIError -> cost None) with cost computed BEFORE extraction.
- A golden recorded ``Message`` dump fed through the real anthropic types to
  lock tool_use parsing against SDK shape drift.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

import signalpipe.llm.backend_api as backend_api
from signalpipe.llm import LLMError

# --------------------------------------------------------------------------- #
# Local helpers
# --------------------------------------------------------------------------- #
SCHEMA = {
    "type": "object",
    "properties": {
        "relevance_score": {"type": "integer"},
        "summary": {"type": "string"},
    },
    "required": ["relevance_score", "summary"],
    "additionalProperties": False,
}


def _usage(**over):
    """Build a usage namespace with the four token fields defaulted to 0."""
    fields = dict(
        input_tokens=0,
        output_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    fields.update(over)
    return SimpleNamespace(**fields)


def _tool_block(name="emit_result", data=None):
    return SimpleNamespace(type="tool_use", name=name, input=data)


def _text_block(text="hello"):
    return SimpleNamespace(type="text", text=text)


def install_fake_anthropic(monkeypatch, *, response=None, create_exc=None):
    """Replace ``anthropic.Anthropic`` with a fake. Returns a ``captured`` dict
    holding the constructor kwargs (``init``) and the ``messages.create`` kwargs
    (``create``) so tests can assert on the exact call shape."""
    import anthropic

    captured = {"init": None, "create": None}

    class _Messages:
        def create(self, **kwargs):
            captured["create"] = kwargs
            if create_exc is not None:
                raise create_exc
            return response

    class _Client:
        def __init__(self, **kwargs):
            captured["init"] = kwargs
            self.messages = _Messages()

    # Defensive: never let a real client construct even if the patch is bypassed.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setattr(anthropic, "Anthropic", _Client)
    return captured


def _api_error(message="boom"):
    """A real ``anthropic.APIError`` instance (constructing it needs an httpx
    request), so the ``except anthropic.APIError`` clause in run() catches it."""
    import anthropic
    import httpx

    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIError(message, request=req, body=None)


# --------------------------------------------------------------------------- #
# _cost — pure math
# --------------------------------------------------------------------------- #
class TestCost:
    def test_haiku_all_fields(self):
        usage = _usage(
            input_tokens=1000,
            output_tokens=500,
            cache_read_input_tokens=2000,
            cache_creation_input_tokens=100,
        )
        # (1000*1.0 + 500*5.0 + 2000*1.0*0.1 + 100*1.0*1.25) / 1e6
        # = (1000 + 2500 + 200 + 125) / 1e6 = 0.003825
        assert backend_api._cost(usage, "claude-haiku-4-5") == pytest.approx(0.003825)

    def test_sonnet_pricing(self):
        usage = _usage(input_tokens=1000, output_tokens=1000)
        # (1000*3.0 + 1000*15.0) / 1e6 = 0.018
        assert backend_api._cost(usage, "claude-sonnet-4-6") == pytest.approx(0.018)

    def test_unknown_model_uses_default_pricing(self):
        usage = _usage(
            input_tokens=1000,
            output_tokens=500,
            cache_read_input_tokens=2000,
            cache_creation_input_tokens=100,
        )
        # default (5.0, 25.0):
        # (1000*5 + 500*25 + 2000*5*0.1 + 100*5*1.25)/1e6
        # = (5000 + 12500 + 1000 + 625)/1e6 = 0.019125
        assert backend_api._cost(usage, "gpt-9-turbo") == pytest.approx(0.019125)

    def test_default_matches_opus_entry(self):
        # The unknown-model default must equal the opus price so an opus id typo
        # never under/over-charges. Pin the concrete dollar figure so this can't
        # pass by both sides drifting together: opus input is 5.0/MTok, and on
        # exactly 1M input tokens (no output) that is a flat $5.00.
        usage = _usage(input_tokens=1_000_000, output_tokens=0)
        assert backend_api._cost(usage, "claude-opus-4-8") == pytest.approx(5.0)
        assert backend_api._cost(usage, "totally-unknown") == pytest.approx(5.0)

    def test_missing_cache_fields_are_zero(self):
        # A usage object with only the two required fields: cache terms drop out.
        usage = SimpleNamespace(input_tokens=1000, output_tokens=500)
        # (1000*1.0 + 500*5.0)/1e6 = 0.0035
        assert backend_api._cost(usage, "claude-haiku-4-5") == pytest.approx(0.0035)

    def test_none_field_treated_as_zero(self):
        # getattr returns None -> float(None or 0) == 0.0 (the `or 0` guard).
        usage = _usage(
            input_tokens=1000,
            output_tokens=None,
            cache_read_input_tokens=None,
            cache_creation_input_tokens=None,
        )
        # only input counts: 1000*1.0/1e6 = 0.001
        assert backend_api._cost(usage, "claude-haiku-4-5") == pytest.approx(0.001)

    def test_all_zero_is_free(self):
        assert backend_api._cost(_usage(), "claude-haiku-4-5") == 0.0

    def test_float_return_type(self):
        result = backend_api._cost(_usage(input_tokens=1), "claude-haiku-4-5")
        assert isinstance(result, float)
        # 1 input token at haiku 1.0/MTok -> 1e-6 (also pins it's not truncated to 0).
        assert result == pytest.approx(1e-6)

    @pytest.mark.parametrize(
        "model,pin,pout",
        [
            ("claude-haiku-4-5", 1.0, 5.0),
            ("claude-sonnet-4-6", 3.0, 15.0),
            ("claude-opus-4-8", 5.0, 25.0),
            ("claude-opus-4-7", 5.0, 25.0),
            ("claude-opus-4-6", 5.0, 25.0),
        ],
    )
    def test_pricing_table_entries(self, model, pin, pout):
        assert backend_api.PRICING[model] == (pin, pout)
        # One input token, one output token -> (pin + pout)/1e6.
        usage = _usage(input_tokens=1, output_tokens=1)
        assert backend_api._cost(usage, model) == pytest.approx((pin + pout) / 1e6)

    def test_pricing_table_shape(self):
        assert set(backend_api.PRICING) == {
            "claude-haiku-4-5",
            "claude-sonnet-5",
            "claude-sonnet-4-6",
            "claude-fable-5",
            "claude-opus-4-8",
            "claude-opus-4-7",
            "claude-opus-4-6",
        }
        for pair in backend_api.PRICING.values():
            assert isinstance(pair, tuple) and len(pair) == 2


# --------------------------------------------------------------------------- #
# run — happy extraction path
# --------------------------------------------------------------------------- #
class TestRunHappy:
    def test_returns_input_dict_and_cost(self, cfg, monkeypatch):
        usage = _usage(
            input_tokens=1000,
            output_tokens=500,
            cache_read_input_tokens=2000,
            cache_creation_input_tokens=100,
        )
        payload = {"relevance_score": 8, "summary": "ok"}
        resp = SimpleNamespace(usage=usage, content=[_tool_block(data=payload)])
        install_fake_anthropic(monkeypatch, response=resp)

        data, cost = backend_api.run("claude-haiku-4-5", "sys", "prompt", SCHEMA, cfg)
        assert data == payload
        assert cost == pytest.approx(0.003825)

    def test_skips_leading_text_block(self, cfg, monkeypatch):
        payload = {"relevance_score": 3, "summary": "later"}
        resp = SimpleNamespace(
            usage=_usage(input_tokens=10),
            content=[_text_block("thinking..."), _tool_block(data=payload)],
        )
        install_fake_anthropic(monkeypatch, response=resp)
        data, cost = backend_api.run("claude-haiku-4-5", "sys", "p", SCHEMA, cfg)
        assert data == payload
        # Cost is still computed off usage even though the tool_use block is not
        # first: 10 input tokens at haiku 1.0/MTok -> 1e-5.
        assert cost == pytest.approx(1e-5)

    def test_effort_arg_is_accepted_and_ignored(self, cfg, monkeypatch):
        payload = {"relevance_score": 1, "summary": "x"}
        resp = SimpleNamespace(usage=_usage(input_tokens=5), content=[_tool_block(data=payload)])
        cap = install_fake_anthropic(monkeypatch, response=resp)
        data, _ = backend_api.run("claude-haiku-4-5", "sys", "p", SCHEMA, cfg, effort="high")
        assert data == payload
        # "ignored" must mean it never leaks into the request. Pin the exact key
        # set so a regression that forwarded effort (or dropped/added any kwarg)
        # is caught here, not silently swallowed by the fake.
        assert "effort" not in cap["create"]
        assert set(cap["create"]) == {
            "model",
            "max_tokens",
            "system",
            "messages",
            "tools",
            "tool_choice",
        }


# --------------------------------------------------------------------------- #
# run — request construction (kwargs, tool, system branch, retries)
# --------------------------------------------------------------------------- #
class TestRunRequestShape:
    def _run_capture(
        self,
        cfg,
        monkeypatch,
        model="claude-haiku-4-5",
        system="the system rubric",
        prompt="the prompt",
    ):
        resp = SimpleNamespace(
            usage=_usage(input_tokens=1),
            content=[_tool_block(data={"relevance_score": 1, "summary": "s"})],
        )
        captured = install_fake_anthropic(monkeypatch, response=resp)
        backend_api.run(model, system, prompt, SCHEMA, cfg)
        return captured

    def test_core_kwargs(self, cfg, monkeypatch):
        cap = self._run_capture(cfg, monkeypatch, model="claude-sonnet-4-6", prompt="hello world")
        kw = cap["create"]
        assert kw["model"] == "claude-sonnet-4-6"
        assert kw["max_tokens"] == 16000
        assert kw["messages"] == [{"role": "user", "content": "hello world"}]

    def test_forced_single_tool_with_caller_schema(self, cfg, monkeypatch):
        cap = self._run_capture(cfg, monkeypatch)
        kw = cap["create"]
        tools = kw["tools"]
        assert len(tools) == 1
        assert tools[0]["name"] == "emit_result"
        assert tools[0]["description"] == "Emit the structured result for this item."
        # The tool's input_schema IS the caller's schema object (identity).
        assert tools[0]["input_schema"] is SCHEMA
        assert kw["tool_choice"] == {"type": "tool", "name": "emit_result"}

    def test_system_prompt_cache_default_is_list(self, cfg, monkeypatch):
        # signal.min.json has no api_use_prompt_cache -> default True.
        cap = self._run_capture(cfg, monkeypatch, system="RUBRIC-TEXT")
        system = cap["create"]["system"]
        assert isinstance(system, list) and len(system) == 1
        block = system[0]
        assert block["type"] == "text"
        assert block["text"] == "RUBRIC-TEXT"
        assert block["cache_control"] == {"type": "ephemeral"}

    def test_system_prompt_cache_disabled_is_plain_string(self, cfg, monkeypatch):
        cfg.data["backend"]["api_use_prompt_cache"] = False
        cap = self._run_capture(cfg, monkeypatch, system="PLAIN-RUBRIC")
        assert cap["create"]["system"] == "PLAIN-RUBRIC"

    def test_max_retries_default(self, cfg, monkeypatch):
        # No api_max_retries in config -> default 4.
        cap = self._run_capture(cfg, monkeypatch)
        assert cap["init"] == {"max_retries": 4}

    def test_max_retries_threaded_from_cfg(self, cfg, monkeypatch):
        cfg.data["backend"]["api_max_retries"] = 7
        cap = self._run_capture(cfg, monkeypatch)
        assert cap["init"]["max_retries"] == 7

    def test_max_retries_coerced_to_int(self, cfg, monkeypatch):
        cfg.data["backend"]["api_max_retries"] = "9"
        cap = self._run_capture(cfg, monkeypatch)
        assert cap["init"]["max_retries"] == 9
        # Must be a real int, not float("9")==9.0 (which == 9 would pass but is
        # the wrong type for the SDK). Pins the int() coercion, not just equality.
        assert type(cap["init"]["max_retries"]) is int


# --------------------------------------------------------------------------- #
# run — failure mapping to LLMError
# --------------------------------------------------------------------------- #
class TestRunFailures:
    def _run_expect_error(self, cfg, monkeypatch, content, usage=None):
        usage = usage or _usage(input_tokens=100, output_tokens=10)
        resp = SimpleNamespace(usage=usage, content=content)
        install_fake_anthropic(monkeypatch, response=resp)
        with pytest.raises(LLMError) as exc:
            backend_api.run("claude-haiku-4-5", "sys", "p", SCHEMA, cfg)
        return exc.value

    def test_non_dict_input_raises_with_cost(self, cfg, monkeypatch):
        err = self._run_expect_error(cfg, monkeypatch, [_tool_block(data="not-a-dict")])
        assert "not an object" in str(err)
        # cost computed BEFORE extraction: (100*1.0 + 10*5.0)/1e6 = 0.00015
        assert err.cost_usd == pytest.approx(0.00015)

    def test_none_input_is_non_dict(self, cfg, monkeypatch):
        err = self._run_expect_error(cfg, monkeypatch, [_tool_block(data=None)])
        assert "not an object" in str(err)
        assert err.cost_usd == pytest.approx(0.00015)

    def test_list_input_is_non_dict(self, cfg, monkeypatch):
        err = self._run_expect_error(cfg, monkeypatch, [_tool_block(data=[1, 2, 3])])
        assert "not an object" in str(err)
        assert err.cost_usd == pytest.approx(0.00015)

    def test_no_tool_use_block_raises_with_cost(self, cfg, monkeypatch):
        err = self._run_expect_error(cfg, monkeypatch, [_text_block("just text")])
        assert "no tool_use block" in str(err)
        assert err.cost_usd == pytest.approx(0.00015)

    def test_empty_content_raises_with_cost(self, cfg, monkeypatch):
        err = self._run_expect_error(cfg, monkeypatch, [])
        assert "no tool_use block" in str(err)
        assert err.cost_usd == pytest.approx(0.00015)

    def test_wrong_tool_name_falls_through(self, cfg, monkeypatch):
        # A tool_use block with a different name is not the forced tool.
        err = self._run_expect_error(
            cfg, monkeypatch, [_tool_block(name="other_tool", data={"x": 1})]
        )
        assert "no tool_use block" in str(err)
        assert err.cost_usd == pytest.approx(0.00015)

    def test_cost_reflects_pricing_on_failure(self, cfg, monkeypatch):
        # A failed extraction on an unknown model still carries the default price.
        usage = _usage(input_tokens=100, output_tokens=10)
        resp = SimpleNamespace(usage=usage, content=[_text_block("x")])
        install_fake_anthropic(monkeypatch, response=resp)
        with pytest.raises(LLMError) as exc:
            backend_api.run("mystery-model", "sys", "p", SCHEMA, cfg)
        # default (5.0, 25.0): (100*5 + 10*25)/1e6 = (500 + 250)/1e6 = 0.00075
        assert exc.value.cost_usd == pytest.approx(0.00075)

    def test_api_error_maps_to_llmerror_cost_none(self, cfg, monkeypatch):
        install_fake_anthropic(monkeypatch, create_exc=_api_error("overloaded"))
        with pytest.raises(LLMError) as exc:
            backend_api.run("claude-haiku-4-5", "sys", "p", SCHEMA, cfg)
        assert exc.value.cost_usd is None
        assert "anthropic API error" in str(exc.value)
        assert "overloaded" in str(exc.value)


# --------------------------------------------------------------------------- #
# Golden recorded response — locks parsing against real SDK object shape
# --------------------------------------------------------------------------- #
class TestGoldenRecordedResponse:
    def _validated_message(self, load_json):
        from anthropic.types import Message

        data = load_json("backend_api_message.json")
        return Message.model_validate(data)

    def test_golden_extraction_through_run(self, cfg, monkeypatch, load_json):
        msg = self._validated_message(load_json)
        install_fake_anthropic(monkeypatch, response=msg)

        data, cost = backend_api.run("claude-haiku-4-5", "sys", "p", SCHEMA, cfg)
        assert data == {
            "relevance_score": 8,
            "why_it_matters": "It advances the state of the art.",
            "summary": "A concise recorded summary.",
            "skip": False,
        }
        # usage: input=1200, output=60, cache_read=800, cache_creation=0, haiku:
        # (1200*1.0 + 60*5.0 + 800*1.0*0.1 + 0)/1e6 = (1200 + 300 + 80)/1e6 = 0.00158
        assert cost == pytest.approx(0.00158)

    def test_golden_cost_matches_cost_function(self, load_json):
        msg = self._validated_message(load_json)
        assert backend_api._cost(msg.usage, "claude-haiku-4-5") == pytest.approx(0.00158)

    def test_golden_message_has_real_tool_use_block(self, load_json):
        # The recorded content parses into real anthropic content-block types the
        # extraction path reads via getattr(type/name/input).
        msg = self._validated_message(load_json)
        tool_blocks = [b for b in msg.content if getattr(b, "type", None) == "tool_use"]
        assert len(tool_blocks) == 1
        assert getattr(tool_blocks[0], "name", None) == "emit_result"
        assert isinstance(getattr(tool_blocks[0], "input", None), dict)


# --------------------------------------------------------------------------- #
# Live smoke — env-gated, never runs in CI
# --------------------------------------------------------------------------- #
@pytest.mark.live
def test_live_metered_call():
    if not os.environ.get("SIGNAL_LIVE"):
        pytest.skip("live test: set SIGNAL_LIVE=1 (and ANTHROPIC_API_KEY) to run")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("live test: ANTHROPIC_API_KEY not set")

    cfg = SimpleNamespace(backend={"api_max_retries": 2, "api_use_prompt_cache": True})
    schema = {
        "type": "object",
        "properties": {"word": {"type": "string"}},
        "required": ["word"],
        "additionalProperties": False,
    }
    data, cost = backend_api.run(
        "claude-haiku-4-5",
        "Reply with exactly one lowercase english word in the 'word' field.",
        "Give me any single word.",
        schema,
        cfg,
    )
    assert isinstance(data, dict) and isinstance(data.get("word"), str)
    assert cost > 0
