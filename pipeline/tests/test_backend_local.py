"""Unit + integration tests for ``signalpipe.llm.backend_local``.

The local backend talks to Ollama over ``urllib.request.urlopen``. respx/httpx
mocks do NOT intercept urllib, so every ``run``/``_ollama_chat`` test installs a
callable stand-in for ``urllib.request.urlopen`` (a small scripted fake whose
``resp.read()`` returns JSON bytes and that records each ``Request`` for body
inspection). Nothing here touches a real socket.

The pure surface — ``_extract_json``, ``_validate`` and the ``consensus`` arena
combiner — is exercised directly with hand-built inputs whose expected values
are derived from the real code path (see the majority-vote math in ``consensus``).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from types import SimpleNamespace
from typing import Any, List

import pytest

import signalpipe.llm.backend_cli as backend_cli
import signalpipe.llm.backend_local as backend_local
from signalpipe.llm import LLMError


# --------------------------------------------------------------------------- #
# Test helpers: a scripted urlopen fake + small builders
# --------------------------------------------------------------------------- #
class _FakeResp:
    """Context-manager response whose ``.read()`` yields the scripted payload."""

    def __init__(self, payload: Any):
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *exc) -> bool:
        return False


class _FakeUrlopen:
    """Callable stand-in for ``urllib.request.urlopen``.

    ``script`` is consumed one directive per call:
      * ``bytes``/``str`` -> becomes ``resp.read()``
      * an ``Exception`` instance -> raised
    Each ``Request`` is recorded on ``.requests`` for body/url assertions.
    """

    def __init__(self, script: List[Any]):
        self._script = list(script)
        self.requests: List[Any] = []
        self.timeouts: List[Any] = []

    def __call__(self, req: Any, timeout: Any = None) -> _FakeResp:
        self.requests.append(req)
        self.timeouts.append(timeout)
        if not self._script:
            raise AssertionError("urlopen called more times than scripted")
        directive = self._script.pop(0)
        if isinstance(directive, BaseException):
            raise directive
        return _FakeResp(directive)

    @property
    def call_count(self) -> int:
        return len(self.requests)


@pytest.fixture
def install_urlopen(monkeypatch):
    """Install a scripted ``_FakeUrlopen`` and return it for inspection."""

    def _install(script: List[Any]) -> _FakeUrlopen:
        fake = _FakeUrlopen(script)
        monkeypatch.setattr(urllib.request, "urlopen", fake)
        return fake

    return _install


def _envelope(content: Any) -> str:
    """An Ollama /api/chat success envelope wrapping model ``content``."""
    return json.dumps({"message": {"content": content}})


def _cfg(local=None):
    """A minimal stub cfg exposing ``.backend`` the way ``run`` reads it."""
    return SimpleNamespace(backend={"local": local} if local is not None else {})


def _req_body(req: Any) -> Any:
    return json.loads(req.data.decode("utf-8"))


# The schema used by the run()/integration tests.
RUN_SCHEMA = {
    "type": "object",
    "required": ["relevant", "score"],
    "properties": {
        "relevant": {"type": "boolean"},
        "score": {"type": "integer"},
        "summary": {"type": "string"},
    },
}
VALID_CONTENT = json.dumps({"relevant": True, "score": 8, "summary": "ok"})
VALID_OBJ = {"relevant": True, "score": 8, "summary": "ok"}


# =========================================================================== #
# _extract_json
# =========================================================================== #
class TestExtractJson:
    def test_fenced_json_block(self):
        text = "Sure!\n```json\n{\"a\": 1, \"b\": [2, 3]}\n```\nDone."
        assert backend_local._extract_json(text) == {"a": 1, "b": [2, 3]}

    def test_fenced_without_language_tag(self):
        text = "```\n{\"x\": true}\n```"
        assert backend_local._extract_json(text) == {"x": True}

    def test_bare_object_with_surrounding_prose(self):
        text = 'Here is the answer: {"score": 9, "why": "good"} — hope that helps.'
        assert backend_local._extract_json(text) == {"score": 9, "why": "good"}

    def test_empty_string_returns_none(self):
        assert backend_local._extract_json("") is None

    def test_none_input_returns_none(self):
        # ``if not text`` also absorbs a None that slips through.
        assert backend_local._extract_json(None) is None  # type: ignore[arg-type]

    def test_no_braces_returns_none(self):
        assert backend_local._extract_json("no json object here at all") is None

    def test_top_level_array_returns_none(self):
        # A bare JSON array has no braces -> find('{') == -1 -> None.
        assert backend_local._extract_json("[1, 2, 3]") is None

    def test_reversed_braces_returns_none(self):
        # end <= start -> None (rfind('}') precedes find('{')).
        assert backend_local._extract_json("} then {") is None

    def test_invalid_json_between_braces_returns_none(self):
        assert backend_local._extract_json("{not: valid, json}") is None

    def test_object_amid_prose_is_extracted(self):
        assert backend_local._extract_json('prefix {"only": "obj"} suffix') == {"only": "obj"}

    def test_object_wrapped_in_list_slices_to_inner_object(self):
        # The brace slice runs first-{ .. last-}, so a list wrapping one object
        # yields that object (not None); a genuine non-dict needs no braces.
        assert backend_local._extract_json('[{"a": 1}]') == {"a": 1}

    def test_fence_takes_precedence_over_trailing_braces(self):
        # The fenced group is chosen first, then brace-matched inside it.
        text = "```json\n{\"in\": 1}\n```\nand a stray {\"out\": 2}"
        assert backend_local._extract_json(text) == {"in": 1}

    def test_nested_object_brace_matching(self):
        text = 'noise {"a": {"b": {"c": 1}}} noise'
        assert backend_local._extract_json(text) == {"a": {"b": {"c": 1}}}


# =========================================================================== #
# _validate
# =========================================================================== #
class TestValidate:
    def test_valid_object_returns_none(self):
        obj = {"relevant": True, "score": 8, "summary": "ok"}
        assert backend_local._validate(obj, RUN_SCHEMA) is None

    def test_non_dict_object(self):
        assert backend_local._validate([1, 2], RUN_SCHEMA) == "not an object"
        assert backend_local._validate("nope", RUN_SCHEMA) == "not an object"
        assert backend_local._validate(None, RUN_SCHEMA) == "not an object"

    def test_missing_required_key(self):
        err = backend_local._validate({"relevant": True}, RUN_SCHEMA)
        assert err == "missing required key 'score'"

    def test_wrong_type_string_prop(self):
        # summary declared string, given an int.
        err = backend_local._validate(
            {"relevant": True, "score": 1, "summary": 5}, RUN_SCHEMA
        )
        assert err == "key 'summary' has wrong type"

    def test_none_value_is_skipped(self):
        # summary present but None -> not type-checked; object is otherwise valid.
        obj = {"relevant": True, "score": 1, "summary": None}
        assert backend_local._validate(obj, RUN_SCHEMA) is None

    def test_absent_optional_prop_is_skipped(self):
        obj = {"relevant": False, "score": 0}
        assert backend_local._validate(obj, RUN_SCHEMA) is None

    @pytest.mark.parametrize(
        "value, ok",
        [
            (True, True),
            (False, True),
            (1, False),      # int is NOT a bool
            ("yes", False),
        ],
    )
    def test_boolean_type_check(self, value, ok):
        schema = {"properties": {"flag": {"type": "boolean"}}}
        err = backend_local._validate({"flag": value}, schema)
        assert err == (None if ok else "key 'flag' has wrong type")

    @pytest.mark.parametrize(
        "value, ok",
        [
            (3, True),
            (True, True),    # bool is a subtype of int -> accepted
            (3.0, False),    # a float is not an integer
            ("3", False),
        ],
    )
    def test_integer_type_check(self, value, ok):
        schema = {"properties": {"n": {"type": "integer"}}}
        err = backend_local._validate({"n": value}, schema)
        assert err == (None if ok else "key 'n' has wrong type")

    @pytest.mark.parametrize(
        "value, ok",
        [
            (3, True),       # int accepted for number
            (3.5, True),
            ("3.5", False),
        ],
    )
    def test_number_type_check(self, value, ok):
        schema = {"properties": {"x": {"type": "number"}}}
        err = backend_local._validate({"x": value}, schema)
        assert err == (None if ok else "key 'x' has wrong type")

    @pytest.mark.parametrize(
        "value, ok",
        [
            ([], True),
            (["a"], True),
            ("notalist", False),
            ({}, False),
        ],
    )
    def test_array_type_check(self, value, ok):
        schema = {"properties": {"tags": {"type": "array"}}}
        err = backend_local._validate({"tags": value}, schema)
        assert err == (None if ok else "key 'tags' has wrong type")

    def test_unknown_or_missing_type_is_not_enforced(self):
        # An unrecognised type (or a property with no 'type') maps to no checker.
        schema = {"properties": {"a": {"type": "weird"}, "b": {}}}
        assert backend_local._validate({"a": 123, "b": object()}, schema) is None

    def test_no_required_and_no_properties(self):
        assert backend_local._validate({"anything": 1}, {}) is None


# =========================================================================== #
# _extract_json / _validate parity with backend_cli (verbatim duplicates)
# =========================================================================== #
class TestCliParity:
    # (input, concrete expected) — pinning the literal makes the test catch a
    # regression that hits BOTH copies identically (bare parity would not).
    PARSE_CASES = [
        ("", None),
        ("no json", None),
        ("[1, 2, 3]", None),
        ('{"a": 1}', {"a": 1}),
        ("```json\n{\"k\": [1, 2]}\n```", {"k": [1, 2]}),
        ('prose {"x": {"y": 2}} more', {"x": {"y": 2}}),
        ("} {", None),
        ("{invalid}", None),
    ]

    @pytest.mark.parametrize("text, expected", PARSE_CASES)
    def test_extract_json_parity(self, text, expected):
        local = backend_local._extract_json(text)
        cli = backend_cli._extract_json(text)
        assert local == expected      # pins the real value...
        assert cli == expected        # ...for BOTH copies (also proves parity)

    VALIDATE_CASES = [
        ({"relevant": True, "score": 1}, RUN_SCHEMA, None),
        ({"relevant": True}, RUN_SCHEMA, "missing required key 'score'"),
        ({"relevant": "no", "score": 1}, RUN_SCHEMA, "key 'relevant' has wrong type"),
        ({"relevant": True, "score": None}, RUN_SCHEMA, None),
        ("not a dict", RUN_SCHEMA, "not an object"),
        ({"n": 3.5}, {"properties": {"n": {"type": "integer"}}}, "key 'n' has wrong type"),
    ]

    @pytest.mark.parametrize("obj, schema, expected", VALIDATE_CASES)
    def test_validate_parity(self, obj, schema, expected):
        assert backend_local._validate(obj, schema) == expected
        assert backend_cli._validate(obj, schema) == expected


# =========================================================================== #
# _ollama_chat — direct unit tests (via the urlopen fake)
# =========================================================================== #
class TestOllamaChat:
    def test_returns_message_content(self, install_urlopen):
        install_urlopen([_envelope("hello world")])
        out = backend_local._ollama_chat(
            "http://127.0.0.1:11434", "m", "sys", "prompt",
            RUN_SCHEMA, 10, 16384, "15m",
        )
        assert out == "hello world"

    def test_missing_message_yields_empty_string(self, install_urlopen):
        install_urlopen([json.dumps({})])
        out = backend_local._ollama_chat(
            "http://127.0.0.1:11434", "m", "sys", "p", RUN_SCHEMA, 10, 16384, "15m",
        )
        assert out == ""

    def test_none_content_yields_empty_string(self, install_urlopen):
        install_urlopen([json.dumps({"message": {"content": None}})])
        out = backend_local._ollama_chat(
            "http://127.0.0.1:11434", "m", "sys", "p", RUN_SCHEMA, 10, 16384, "15m",
        )
        assert out == ""

    def test_request_body_and_url(self, install_urlopen):
        fake = install_urlopen([_envelope("{}")])
        backend_local._ollama_chat(
            "http://127.0.0.1:11434", "llama3", "SYSTEM", "PROMPT",
            RUN_SCHEMA, 42, 8192, "30m",
        )
        req = fake.requests[0]
        assert req.full_url == "http://127.0.0.1:11434/api/chat"
        assert req.get_header("Content-type") == "application/json"
        # The `timeout` arg is plumbed straight through to urlopen(timeout=...).
        assert fake.timeouts[0] == 42
        body = _req_body(req)
        assert body["model"] == "llama3"
        assert body["stream"] is False
        assert body["keep_alive"] == "30m"
        assert body["format"] == RUN_SCHEMA
        assert body["options"] == {"temperature": 0.2, "num_ctx": 8192}
        assert body["messages"] == [
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "PROMPT"},
        ]

    def test_base_url_trailing_slash_is_stripped(self, install_urlopen):
        fake = install_urlopen([_envelope("{}")])
        backend_local._ollama_chat(
            "http://localhost:11434/", "m", "s", "p", RUN_SCHEMA, 5, 1024, "1m",
        )
        assert fake.requests[0].full_url == "http://localhost:11434/api/chat"

    def test_urlerror_raises_unreachable(self, install_urlopen):
        install_urlopen([urllib.error.URLError("connection refused")])
        with pytest.raises(LLMError) as ei:
            backend_local._ollama_chat(
                "http://127.0.0.1:11434", "m", "s", "p", RUN_SCHEMA, 5, 1024, "1m",
            )
        assert "unreachable" in str(ei.value)
        assert ei.value.cost_usd == 0.0

    def test_httperror_is_treated_as_unreachable(self, install_urlopen):
        # HTTPError is a subclass of URLError -> the first except branch wins.
        err = urllib.error.HTTPError("http://x/api/chat", 500, "boom", {}, None)
        install_urlopen([err])
        with pytest.raises(LLMError) as ei:
            backend_local._ollama_chat(
                "http://x", "m", "s", "p", RUN_SCHEMA, 5, 1024, "1m",
            )
        assert "unreachable" in str(ei.value)
        assert ei.value.cost_usd == 0.0

    def test_non_json_body_raises_bad_response(self, install_urlopen):
        install_urlopen([b"<html>not json</html>"])
        with pytest.raises(LLMError) as ei:
            backend_local._ollama_chat(
                "http://127.0.0.1:11434", "m", "s", "p", RUN_SCHEMA, 5, 1024, "1m",
            )
        assert "bad response" in str(ei.value)
        assert ei.value.cost_usd == 0.0

    def test_oserror_raises_bad_response(self, install_urlopen):
        install_urlopen([OSError("socket exploded")])
        with pytest.raises(LLMError) as ei:
            backend_local._ollama_chat(
                "http://127.0.0.1:11434", "m", "s", "p", RUN_SCHEMA, 5, 1024, "1m",
            )
        assert "bad response" in str(ei.value)
        assert ei.value.cost_usd == 0.0

    def test_error_field_raises_ollama_error(self, install_urlopen):
        install_urlopen([json.dumps({"error": "model 'x' not found"})])
        with pytest.raises(LLMError) as ei:
            backend_local._ollama_chat(
                "http://127.0.0.1:11434", "m", "s", "p", RUN_SCHEMA, 5, 1024, "1m",
            )
        assert "ollama error" in str(ei.value)
        assert "model 'x' not found" in str(ei.value)
        assert ei.value.cost_usd == 0.0


# =========================================================================== #
# run() — happy path, repair retry, and terminal failures
# =========================================================================== #
class TestRun:
    def test_happy_path_returns_obj_and_zero_cost(self, install_urlopen):
        fake = install_urlopen([_envelope(VALID_CONTENT)])
        obj, cost = backend_local.run("m", "sys", "prompt", RUN_SCHEMA, _cfg())
        assert obj == VALID_OBJ
        assert cost == 0.0
        assert fake.call_count == 1
        # First (and only) attempt carries the original prompt verbatim.
        assert _req_body(fake.requests[0])["messages"][1]["content"] == "prompt"

    def test_defaults_used_when_local_config_absent(self, install_urlopen):
        # cfg.backend has no "local" key -> all defaults apply.
        fake = install_urlopen([_envelope(VALID_CONTENT)])
        obj, cost = backend_local.run("m", "s", "p", RUN_SCHEMA, _cfg())
        assert (obj, cost) == (VALID_OBJ, 0.0)
        req = fake.requests[0]
        assert req.full_url == "http://127.0.0.1:11434/api/chat"
        body = _req_body(req)
        assert body["keep_alive"] == "15m"
        assert body["options"]["num_ctx"] == 16384
        # Default timeout_sec (600) reaches urlopen unchanged.
        assert fake.timeouts[0] == 600

    def test_backend_none_falls_back_to_defaults(self, install_urlopen):
        fake = install_urlopen([_envelope(VALID_CONTENT)])
        cfg = SimpleNamespace(backend=None)  # (cfg.backend or {}) -> {}
        obj, cost = backend_local.run("m", "s", "p", RUN_SCHEMA, cfg)
        assert (obj, cost) == (VALID_OBJ, 0.0)
        assert fake.requests[0].full_url == "http://127.0.0.1:11434/api/chat"

    def test_custom_local_config_is_honoured(self, install_urlopen):
        fake = install_urlopen([_envelope(VALID_CONTENT)])
        local = {
            "base_url": "http://gpu.local:9999",
            "timeout_sec": 30,
            "num_ctx": 4096,
            "keep_alive": "5m",
        }
        backend_local.run("m", "s", "p", RUN_SCHEMA, _cfg(local))
        req = fake.requests[0]
        assert req.full_url == "http://gpu.local:9999/api/chat"
        body = _req_body(req)
        assert body["keep_alive"] == "5m"
        assert body["options"]["num_ctx"] == 4096
        # Custom timeout_sec (30) is honoured, not the 600 default.
        assert fake.timeouts[0] == 30

    def test_repair_retry_after_unparseable_first_response(self, install_urlopen):
        fake = install_urlopen([_envelope("no json here at all"), _envelope(VALID_CONTENT)])
        obj, cost = backend_local.run("m", "sys", "prompt", RUN_SCHEMA, _cfg())
        assert (obj, cost) == (VALID_OBJ, 0.0)
        assert fake.call_count == 2
        # 1st attempt: original prompt. 2nd attempt: repair addendum.
        assert _req_body(fake.requests[0])["messages"][1]["content"] == "prompt"
        repair = _req_body(fake.requests[1])["messages"][1]["content"]
        assert repair.startswith("prompt")
        assert "previous output was invalid" in repair
        assert "no parseable JSON" in repair

    def test_repair_retry_after_schema_validation_failure(self, install_urlopen):
        bad = json.dumps({"relevant": "yes", "score": 8})  # relevant wrong type
        fake = install_urlopen([_envelope(bad), _envelope(VALID_CONTENT)])
        obj, cost = backend_local.run("m", "sys", "prompt", RUN_SCHEMA, _cfg())
        assert (obj, cost) == (VALID_OBJ, 0.0)
        assert fake.call_count == 2
        repair = _req_body(fake.requests[1])["messages"][1]["content"]
        assert "previous output was invalid" in repair
        assert "schema validation" in repair
        assert "relevant" in repair  # the failing key surfaced in the addendum

    def test_invalid_both_attempts_raises_after_retry(self, install_urlopen):
        fake = install_urlopen([_envelope("garbage one"), _envelope("garbage two")])
        with pytest.raises(LLMError) as ei:
            backend_local.run("mymodel", "s", "p", RUN_SCHEMA, _cfg())
        assert "invalid after retry" in str(ei.value)
        assert "no parseable JSON" in str(ei.value)
        assert "mymodel" in str(ei.value)
        assert ei.value.cost_usd == 0.0
        assert fake.call_count == 2

    def test_invalid_both_attempts_reports_validation_error(self, install_urlopen):
        bad = json.dumps({"relevant": "no", "score": 1})
        fake = install_urlopen([_envelope(bad), _envelope(bad)])
        with pytest.raises(LLMError) as ei:
            backend_local.run("m", "s", "p", RUN_SCHEMA, _cfg())
        assert "schema validation" in str(ei.value)
        assert ei.value.cost_usd == 0.0
        assert fake.call_count == 2

    def test_transport_urlerror_propagates_unreachable(self, install_urlopen):
        fake = install_urlopen([urllib.error.URLError("refused")])
        with pytest.raises(LLMError) as ei:
            backend_local.run("m", "s", "p", RUN_SCHEMA, _cfg())
        assert "unreachable" in str(ei.value)
        assert ei.value.cost_usd == 0.0
        # Transport failure is terminal: NO repair retry.
        assert fake.call_count == 1

    def test_error_envelope_propagates_ollama_error(self, install_urlopen):
        fake = install_urlopen([json.dumps({"error": "model not found"})])
        with pytest.raises(LLMError) as ei:
            backend_local.run("m", "s", "p", RUN_SCHEMA, _cfg())
        assert "ollama error" in str(ei.value)
        assert ei.value.cost_usd == 0.0
        assert fake.call_count == 1

    def test_non_json_body_propagates_bad_response(self, install_urlopen):
        fake = install_urlopen([b"<html/>"])
        with pytest.raises(LLMError) as ei:
            backend_local.run("m", "s", "p", RUN_SCHEMA, _cfg())
        assert "bad response" in str(ei.value)
        assert ei.value.cost_usd == 0.0
        assert fake.call_count == 1


# =========================================================================== #
# consensus() — the pure arena combiner
# =========================================================================== #
# Schema exercising every fusion branch at once.
FUSE_SCHEMA = {
    "type": "object",
    "required": ["flag", "score", "conf", "channels", "facts", "label"],
    "properties": {
        "flag": {"type": "boolean"},
        "score": {"type": "integer"},
        "conf": {"type": "number"},
        "channels": {"type": "array", "maxItems": 3},
        "facts": {"type": "array", "maxItems": 5},
        "label": {"type": "string"},
    },
}


class TestConsensusCore:
    def test_core_type_fusion(self):
        objs = [
            {  # primary
                "flag": True, "score": 7, "conf": 0.5,
                "channels": ["ai", "robotics"], "facts": ["a", "b"],
                "label": "primary-label",
            },
            {
                "flag": True, "score": 9, "conf": 0.9,
                "channels": ["ai", "policy"], "facts": ["b", "c"],
                "label": "second",
            },
            {
                "flag": False, "score": 5, "conf": 0.1,
                "channels": ["ai"], "facts": ["c", "d"],
                "label": "third",
            },
        ]
        out = backend_local.consensus(objs, FUSE_SCHEMA)
        # boolean: 2 true / 1 false -> True
        assert out["flag"] is True
        # integer: median(7, 9, 5) = 7
        assert out["score"] == 7
        # number: median(0.5, 0.9, 0.1) = 0.5
        assert out["conf"] == 0.5
        # channels (majority): only "ai" is picked by > half of voters
        assert out["channels"] == ["ai"]
        # facts (union): order-preserving dedup across all voters
        assert out["facts"] == ["a", "b", "c", "d"]
        # string: the primary model's wording
        assert out["label"] == "primary-label"

    def test_integer_and_number_even_count_median(self):
        schema = {
            "properties": {"n": {"type": "integer"}, "x": {"type": "number"}},
        }
        out = backend_local.consensus(
            [{"n": 7, "x": 1.0}, {"n": 9, "x": 3.0}], schema
        )
        assert out["n"] == 8            # median(7, 9) = 8.0 -> int 8
        assert out["x"] == 2.0          # median(1.0, 3.0) = 2.0
        assert isinstance(out["x"], float)

    def test_union_array_respects_maxitems_cap(self):
        schema = {"properties": {"facts": {"type": "array", "maxItems": 2}}}
        out = backend_local.consensus(
            [{"facts": ["a", "b"]}, {"facts": ["c", "d"]}], schema
        )
        assert out["facts"] == ["a", "b"]

    def test_union_array_preserves_first_seen_order(self):
        schema = {"properties": {"facts": {"type": "array"}}}
        out = backend_local.consensus(
            [{"facts": ["b", "a"]}, {"facts": ["a", "c"]}, {"facts": ["b"]}], schema
        )
        assert out["facts"] == ["b", "a", "c"]

    def test_union_array_skips_non_list_voter_values(self):
        schema = {"properties": {"facts": {"type": "array"}}}
        out = backend_local.consensus(
            [{"facts": ["a"]}, {"facts": "oops"}, {"facts": ["b"]}], schema
        )
        assert out["facts"] == ["a", "b"]

    def test_majority_arrays_param_controls_branch(self):
        # With channels REMOVED from majority_arrays it falls to union semantics.
        schema = {"properties": {"channels": {"type": "array"}}}
        objs = [{"channels": ["ai"]}, {"channels": ["policy"]}, {"channels": ["ai"]}]
        out = backend_local.consensus(objs, schema, majority_arrays=())
        assert out["channels"] == ["ai", "policy"]

    def test_majority_arrays_param_can_add_keys(self):
        # Promote "tags" to majority semantics: only the >half item survives.
        schema = {"properties": {"tags": {"type": "array"}}}
        objs = [
            {"tags": ["x", "y"]},
            {"tags": ["x", "z"]},
            {"tags": ["x"]},
        ]
        out = backend_local.consensus(objs, schema, majority_arrays=("tags",))
        assert out["tags"] == ["x"]

    def test_majority_array_skips_non_list_voter_values(self):
        schema = {"properties": {"channels": {"type": "array"}}}
        objs = [{"channels": ["ai"]}, {"channels": "junk"}, {"channels": ["ai"]}]
        out = backend_local.consensus(objs, schema)
        assert out["channels"] == ["ai"]

    def test_string_and_object_take_primary_value(self):
        schema = {
            "properties": {
                "label": {"type": "string"},
                "meta": {"type": "object"},
            }
        }
        objs = [
            {"label": "first", "meta": {"a": 1}},
            {"label": "second", "meta": {"b": 2}},
        ]
        out = backend_local.consensus(objs, schema)
        assert out["label"] == "first"
        assert out["meta"] == {"a": 1}

    def test_string_falls_back_to_first_val_when_primary_lacks_key(self):
        schema = {"properties": {"flag": {"type": "boolean"}, "label": {"type": "string"}}}
        objs = [{"flag": True}, {"flag": True, "label": "z"}]
        out = backend_local.consensus(objs, schema)
        assert out["label"] == "z"


class TestConsensusEdges:
    def test_empty_input_returns_empty_dict(self):
        assert backend_local.consensus([], FUSE_SCHEMA) == {}

    def test_all_non_dict_input_returns_empty_dict(self):
        assert backend_local.consensus(["x", 3, None], FUSE_SCHEMA) == {}

    def test_single_object_is_returned_as_identity(self):
        obj = {"flag": True, "score": 3}
        result = backend_local.consensus([obj], FUSE_SCHEMA)
        assert result is obj

    def test_non_dict_entries_are_dropped_before_fusion(self):
        schema = {"properties": {"flag": {"type": "boolean"}}}
        objs = [{"flag": True}, "junk", 42, None, {"flag": True}]
        out = backend_local.consensus(objs, schema)
        # Only the two real dicts vote; both True -> True.
        assert out == {"flag": True}

    def test_junk_reducing_to_single_dict_is_identity(self):
        obj = {"flag": False, "note": "solo"}
        schema = {"properties": {"flag": {"type": "boolean"}}}
        # After filtering, only one dict remains -> identity fast-path.
        assert backend_local.consensus([obj, "junk", None], schema) is obj

    @pytest.mark.parametrize("primary_flag", [True, False])
    def test_boolean_tie_breaks_to_primary(self, primary_flag):
        schema = {"properties": {"flag": {"type": "boolean"}}}
        objs = [{"flag": primary_flag}, {"flag": not primary_flag}]
        out = backend_local.consensus(objs, schema)
        assert out["flag"] is primary_flag

    def test_boolean_two_voters_both_agree(self):
        schema = {"properties": {"flag": {"type": "boolean"}}}
        # n=2 requires BOTH to be true (trues*2 > len -> 4 > 2).
        assert backend_local.consensus(
            [{"flag": True}, {"flag": True}], schema
        )["flag"] is True
        assert backend_local.consensus(
            [{"flag": False}, {"flag": False}], schema
        )["flag"] is False

    def test_channels_no_majority_falls_back_to_primary_list(self):
        schema = {"properties": {"channels": {"type": "array", "maxItems": 3}}}
        objs = [
            {"channels": ["ai", "robotics"]},
            {"channels": ["policy", "science"]},
        ]
        # n=2, no item exceeds half -> the primary's picks are used.
        out = backend_local.consensus(objs, schema)
        assert out["channels"] == ["ai", "robotics"]

    def test_channels_no_majority_and_primary_not_a_list_yields_empty(self):
        schema = {"properties": {"channels": {"type": "array"}}}
        objs = [{"channels": "scalar"}, {"channels": ["policy"]}]
        out = backend_local.consensus(objs, schema)
        assert out["channels"] == []

    def test_required_key_only_on_primary_is_carried_through(self):
        # "id" is required but not in properties -> carried from the primary.
        schema = {
            "required": ["flag", "id"],
            "properties": {"flag": {"type": "boolean"}},
        }
        objs = [{"flag": True, "id": "story-1"}, {"flag": True}]
        out = backend_local.consensus(objs, schema)
        assert out["flag"] is True
        assert out["id"] == "story-1"

    def test_required_key_absent_on_primary_is_not_invented(self):
        schema = {
            "required": ["flag", "id"],
            "properties": {"flag": {"type": "boolean"}},
        }
        objs = [{"flag": True}, {"flag": True, "id": "only-secondary"}]
        out = backend_local.consensus(objs, schema)
        assert "id" not in out  # only carried from the PRIMARY

    def test_prop_with_no_values_carries_primary_none(self):
        # summary present-but-None on primary, absent elsewhere -> vals empty ->
        # the primary's (None) value is carried.
        schema = {
            "properties": {
                "flag": {"type": "boolean"},
                "summary": {"type": "string"},
            }
        }
        objs = [{"flag": True, "summary": None}, {"flag": True}]
        out = backend_local.consensus(objs, schema)
        assert "summary" in out
        assert out["summary"] is None

    def test_prop_absent_everywhere_is_omitted(self):
        schema = {
            "properties": {
                "flag": {"type": "boolean"},
                "summary": {"type": "string"},
            }
        }
        objs = [{"flag": True}, {"flag": True}]
        out = backend_local.consensus(objs, schema)
        assert "summary" not in out

    def test_no_maxitems_returns_full_union(self):
        schema = {"properties": {"facts": {"type": "array"}}}  # no cap
        out = backend_local.consensus(
            [{"facts": ["a", "b", "c"]}, {"facts": ["d"]}], schema
        )
        assert out["facts"] == ["a", "b", "c", "d"]

    def test_output_only_contains_schema_keys(self):
        # Extra keys present on the objects but absent from properties/required
        # never appear in the fused output.
        schema = {"properties": {"flag": {"type": "boolean"}}}
        objs = [{"flag": True, "stray": 1}, {"flag": True, "stray": 2}]
        out = backend_local.consensus(objs, schema)
        assert set(out) == {"flag"}


# =========================================================================== #
# consensus() — property-based containment / idempotence
# =========================================================================== #
class TestConsensusProperties:
    def test_output_keys_subset_and_single_idempotent(self):
        hypothesis = pytest.importorskip("hypothesis")
        from hypothesis import given, settings
        from hypothesis import strategies as st

        schema = FUSE_SCHEMA
        prop_keys = set(schema["properties"])
        required = set(schema["required"])
        allowed = prop_keys | required

        channel_vocab = ["ai", "policy", "robotics", "science"]

        @st.composite
        def _obj(draw):
            d = {}
            if draw(st.booleans()):
                d["flag"] = draw(st.booleans())
            if draw(st.booleans()):
                d["score"] = draw(st.integers(min_value=-1000, max_value=1000))
            if draw(st.booleans()):
                d["conf"] = draw(
                    st.floats(min_value=-1e6, max_value=1e6,
                              allow_nan=False, allow_infinity=False)
                )
            if draw(st.booleans()):
                d["channels"] = draw(
                    st.lists(st.sampled_from(channel_vocab), max_size=4)
                )
            if draw(st.booleans()):
                d["facts"] = draw(st.lists(st.text(max_size=4), max_size=4))
            if draw(st.booleans()):
                d["label"] = draw(st.text(max_size=8))
            return d

        @given(objs=st.lists(_obj(), min_size=1, max_size=5))
        @settings(max_examples=150, deadline=None)
        def _check(objs):
            out = backend_local.consensus(objs, schema)
            assert isinstance(out, dict)
            # Output never invents keys outside the schema surface.
            assert set(out).issubset(allowed)

        _check()

    def test_single_model_is_idempotent(self):
        hypothesis = pytest.importorskip("hypothesis")
        from hypothesis import given, settings
        from hypothesis import strategies as st

        @given(
            d=st.dictionaries(
                keys=st.sampled_from(list(FUSE_SCHEMA["properties"])),
                values=st.one_of(st.booleans(), st.integers(), st.text(max_size=5)),
                max_size=6,
            )
        )
        @settings(max_examples=100, deadline=None)
        def _check(d):
            # A single-model ensemble is passed through unchanged (identity).
            assert backend_local.consensus([d], FUSE_SCHEMA) == d

        _check()


# =========================================================================== #
# Live smoke test — real Ollama round-trip (opt-in only)
# =========================================================================== #
@pytest.mark.live
def test_live_ollama_round_trip():
    import os

    if not os.environ.get("SIGNAL_LIVE"):
        pytest.skip("live Ollama round-trip: set SIGNAL_LIVE=1 (and run a local 11434)")
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    model = os.environ.get("OLLAMA_TEST_MODEL", "llama3.2:1b")
    cfg = _cfg({"base_url": base_url, "timeout_sec": 120})
    schema = {
        "type": "object",
        "required": ["ok"],
        "properties": {"ok": {"type": "boolean"}},
    }
    obj, cost = backend_local.run(
        model, "Reply with a JSON object.", "Return {\"ok\": true}.", schema, cfg
    )
    assert isinstance(obj, dict) and obj.get("ok") in (True, False)
    assert cost == 0.0
