"""Unit tests for ``signalpipe.llm.schemas``.

The module is pure data (JSON schemas + system-prompt strings) plus one pure
prompt composer, ``system_digest``. Everything here is hermetic: no network, no
filesystem, no clock. jsonschema is not a test dependency, so schema
well-formedness is checked with a small self-contained recursive validator that
mirrors what ``Draft7Validator.check_schema`` would assert for these schemas
(``required`` is a subset of ``properties``; every object closes with
``additionalProperties: False``).
"""

from __future__ import annotations

import pytest

import signalpipe.llm.schemas as schemas

# The exact trailing clause every reader-facing system prompt ends with.
JSON_CLAUSE = " Respond ONLY with JSON matching the provided schema."

ALL_SCHEMAS = {
    "TRIAGE_SCHEMA": schemas.TRIAGE_SCHEMA,
    "CURATION_SCHEMA": schemas.CURATION_SCHEMA,
    "JUDGE_SCHEMA": schemas.JUDGE_SCHEMA,
    "WRITE_SCHEMA": schemas.WRITE_SCHEMA,
    "DIGEST_SCHEMA": schemas.DIGEST_SCHEMA,
    "GLOSSARY_EXTRACT_SCHEMA": schemas.GLOSSARY_EXTRACT_SCHEMA,
    "GLOSSARY_DEFINE_SCHEMA": schemas.GLOSSARY_DEFINE_SCHEMA,
}

EXPECTED_KINDS = {"daily", "weekly", "monthly", "quarterly", "yearly"}

# Concrete, order-sensitive snapshots of the enum domains. The schemas embed
# these lists BY REFERENCE (``enum is CHANNELS``), so a bare ``enum == CHANNELS``
# is a same-object tautology that can never catch an edit to the domain itself.
# Pinning the literal set here makes an accidental rename/reorder/drop fail loudly.
EXPECTED_CHANNELS = [
    "ai",
    "ml-research",
    "devtools",
    "security",
    "hardware",
    "startups",
    "science",
]
EXPECTED_GLOSSARY_CATEGORIES = [
    "ai-ml",
    "computer-science",
    "software-engineering",
    "mathematics",
    "systems",
    "security",
    "hardware",
    "other",
]

# A distinctive substring that appears ONLY in that kind's block text.
KIND_MARKER = {
    "daily": "This is the DAILY digest",
    "weekly": "This is the WEEKLY digest",
    "monthly": "This is the MONTHLY digest",
    "quarterly": "This is the QUARTERLY digest",
    "yearly": "This is the YEARLY digest",
}


def _iter_object_nodes(node):
    """Yield every JSON-schema sub-node whose ``type`` is ``object``.

    Walks ``properties`` values and array ``items`` recursively — enough to
    cover the nested shapes used in this module (glossary schemas nest an
    object inside an array).
    """
    if not isinstance(node, dict):
        return
    if node.get("type") == "object":
        yield node
    for value in node.get("properties", {}).values():
        for found in _iter_object_nodes(value):
            yield found
    items = node.get("items")
    if items is not None:
        for found in _iter_object_nodes(items):
            yield found


# --------------------------------------------------------------------------- #
# system_digest — per-kind blocks
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind", sorted(EXPECTED_KINDS))
def test_system_digest_includes_kind_block_and_core(kind):
    out = schemas.system_digest(kind, "House style.")
    # The kind's own distinctive block text is present ...
    assert KIND_MARKER[kind] in out
    assert schemas._DIGEST_KIND_BLOCKS[kind] in out
    # ... and no *other* kind's marker leaks in.
    for other, marker in KIND_MARKER.items():
        if other != kind:
            assert marker not in out
    # The shared editorial core is always present, at the very start.
    assert schemas._DIGEST_CORE in out
    assert out.startswith(schemas._DIGEST_CORE)


@pytest.mark.parametrize("kind", sorted(EXPECTED_KINDS))
def test_system_digest_always_ends_with_reader_profile_and_json_clause(kind):
    out = schemas.system_digest(kind, "House style.", "Some policy.")
    assert out.endswith(schemas.READER_PROFILE + JSON_CLAUSE)


def test_system_digest_contains_style_guide_header():
    out = schemas.system_digest("weekly", "House style.")
    assert "STYLE GUIDE:\n" in out


# --------------------------------------------------------------------------- #
# system_digest — unknown-kind fallback (silently maps to 'weekly')
# --------------------------------------------------------------------------- #
def test_system_digest_unknown_kind_falls_back_to_weekly():
    out = schemas.system_digest("bogus-not-a-kind", "style")
    assert KIND_MARKER["weekly"] in out
    assert schemas._DIGEST_KIND_BLOCKS["weekly"] in out
    assert KIND_MARKER["daily"] not in out


def test_system_digest_unknown_kind_does_not_raise():
    # Documented behavior: fall back to weekly (never raise) for an unknown kind.
    # isinstance(str) alone is near-vacuous — the fn always concatenates strings;
    # assert the fallback actually fired and the lookalike block did NOT.
    empty_out = schemas.system_digest("", "style")
    assert KIND_MARKER["weekly"] in empty_out

    # "YEARLY" is a real kind name but uppercase; keys are lowercase, so it is
    # unknown and must fall back to weekly rather than select the yearly block.
    upper_out = schemas.system_digest("YEARLY", "style")
    assert KIND_MARKER["weekly"] in upper_out
    assert KIND_MARKER["yearly"] not in upper_out


def test_system_digest_uppercase_kind_is_unknown_and_falls_back():
    # Keys are lowercase; an uppercase kind is not found -> weekly fallback.
    out = schemas.system_digest("DAILY", "style")
    assert KIND_MARKER["weekly"] in out
    assert KIND_MARKER["daily"] not in out


# --------------------------------------------------------------------------- #
# system_digest — style branch
# --------------------------------------------------------------------------- #
def test_system_digest_empty_style_uses_fallback():
    out = schemas.system_digest("weekly", "")
    assert schemas.STYLE_FALLBACK in out


def test_system_digest_none_style_uses_fallback():
    # ``style_text or STYLE_FALLBACK`` also absorbs a None slipping through.
    out = schemas.system_digest("weekly", None)  # type: ignore[arg-type]
    assert schemas.STYLE_FALLBACK in out


def test_system_digest_provided_style_is_used_and_stripped():
    out = schemas.system_digest("weekly", "  My bespoke style rules.  ")
    assert "My bespoke style rules." in out
    # The fallback is NOT substituted when a real style is given.
    assert schemas.STYLE_FALLBACK not in out
    # Surrounding whitespace was stripped: the style sits directly after header.
    assert "STYLE GUIDE:\nMy bespoke style rules.\n\n" in out


# --------------------------------------------------------------------------- #
# system_digest — policy branch
# --------------------------------------------------------------------------- #
POLICY_HEADER = "EDITORIAL POLICY (what to publish and emphasize):"


def test_system_digest_default_policy_omits_header():
    out = schemas.system_digest("weekly", "style")
    assert POLICY_HEADER not in out


def test_system_digest_whitespace_policy_omits_header():
    out = schemas.system_digest("weekly", "style", "   \n\t  ")
    assert POLICY_HEADER not in out


def test_system_digest_nonempty_policy_emits_stripped_header_block():
    out = schemas.system_digest("weekly", "style", "  Favor primary sources.  ")
    assert POLICY_HEADER in out
    # Header is immediately followed by the stripped policy text + blank line.
    assert POLICY_HEADER + "\nFavor primary sources.\n\n" in out
    # And the policy block sits before the kind block.
    assert out.index(POLICY_HEADER) < out.index(KIND_MARKER["weekly"])


def test_system_digest_full_ordering():
    # core -> style guide -> policy -> kind block -> reader profile + clause.
    out = schemas.system_digest("daily", "STYLEX", "POLICYY")
    i_core = out.index(schemas._DIGEST_CORE)
    i_style = out.index("STYLE GUIDE:\nSTYLEX")
    i_policy = out.index(POLICY_HEADER)
    i_block = out.index(KIND_MARKER["daily"])
    i_reader = out.index(schemas.READER_PROFILE)
    assert i_core < i_style < i_policy < i_block < i_reader
    assert out.endswith(schemas.READER_PROFILE + JSON_CLAUSE)


# --------------------------------------------------------------------------- #
# Schema well-formedness (self-contained; no jsonschema dependency)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", sorted(ALL_SCHEMAS))
def test_schema_required_is_subset_of_properties(name):
    schema = ALL_SCHEMAS[name]
    nodes = list(_iter_object_nodes(schema))
    assert nodes, "expected at least one object node in %s" % name
    for node in nodes:
        props = set(node.get("properties", {}).keys())
        required = set(node.get("required", []))
        missing = required - props
        assert not missing, "%s: required keys not in properties: %s" % (name, missing)


@pytest.mark.parametrize("name", sorted(ALL_SCHEMAS))
def test_schema_objects_close_additional_properties(name):
    schema = ALL_SCHEMAS[name]
    for node in _iter_object_nodes(schema):
        assert node.get("additionalProperties") is False, (
            "%s: object node does not set additionalProperties: False" % name
        )


@pytest.mark.parametrize("name", sorted(ALL_SCHEMAS))
def test_schema_top_level_is_object(name):
    assert ALL_SCHEMAS[name].get("type") == "object"


def test_schema_property_types_are_declared():
    # Every leaf property declares a JSON type (guards against a bare/typo'd node).
    for name, schema in ALL_SCHEMAS.items():
        for node in _iter_object_nodes(schema):
            for prop_name, prop in node.get("properties", {}).items():
                assert "type" in prop, "%s.%s missing type" % (name, prop_name)


# --------------------------------------------------------------------------- #
# Enum parity — channels + glossary categories
# --------------------------------------------------------------------------- #
def test_curation_channels_enum_matches_channels():
    enum = schemas.CURATION_SCHEMA["properties"]["channels"]["items"]["enum"]
    # Pin the concrete domain (order included) — not just `== schemas.CHANNELS`,
    # which is a same-object tautology since the schema embeds CHANNELS directly.
    assert enum == EXPECTED_CHANNELS


def test_judge_channels_enum_matches_channels():
    enum = schemas.JUDGE_SCHEMA["properties"]["channels"]["items"]["enum"]
    assert enum == EXPECTED_CHANNELS


def test_glossary_extract_category_enum_matches_categories():
    term_props = schemas.GLOSSARY_EXTRACT_SCHEMA["properties"]["terms"]["items"]["properties"]
    assert term_props["category"]["enum"] == EXPECTED_GLOSSARY_CATEGORIES


def test_glossary_define_category_enum_matches_categories():
    def_props = schemas.GLOSSARY_DEFINE_SCHEMA["properties"]["definitions"]["items"]["properties"]
    assert def_props["category"]["enum"] == EXPECTED_GLOSSARY_CATEGORIES


def test_channels_and_categories_are_nonempty_unique():
    # Exact-list pin: catches an added/removed/renamed/reordered domain value.
    assert schemas.CHANNELS == EXPECTED_CHANNELS
    assert schemas.GLOSSARY_CATEGORIES == EXPECTED_GLOSSARY_CATEGORIES
    # ... and the invariants those lists must satisfy.
    assert len(schemas.CHANNELS) == len(set(schemas.CHANNELS))
    assert len(schemas.GLOSSARY_CATEGORIES) == len(set(schemas.GLOSSARY_CATEGORIES))
    assert "other" in schemas.GLOSSARY_CATEGORIES  # the technical catch-all
    assert "other" not in schemas.CHANNELS  # feed channels have no catch-all


# --------------------------------------------------------------------------- #
# System-prompt string constants
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "prompt",
    [
        schemas.SYSTEM_TRIAGE,
        schemas.SYSTEM_CURATE,
        schemas.SYSTEM_JUDGE,
        schemas.SYSTEM_WRITE,
    ],
)
def test_reader_facing_prompts_embed_profile_and_json_clause(prompt):
    assert isinstance(prompt, str) and prompt
    assert schemas.READER_PROFILE in prompt
    assert prompt.endswith(JSON_CLAUSE)


@pytest.mark.parametrize(
    "prompt",
    [
        schemas.SYSTEM_GLOSSARY_EXTRACT,
        schemas.SYSTEM_GLOSSARY_DEFINE,
    ],
)
def test_glossary_prompts_end_with_json_clause(prompt):
    # Glossary prompts are independent of the feed pipeline: no READER_PROFILE,
    # but they still close with the JSON-only instruction.
    assert isinstance(prompt, str) and prompt
    assert prompt.rstrip().endswith(JSON_CLAUSE.strip())
    # Pin the documented independence: the feed reader profile must NOT leak in.
    assert schemas.READER_PROFILE not in prompt


def test_digest_kind_blocks_key_set():
    assert set(schemas._DIGEST_KIND_BLOCKS) == EXPECTED_KINDS
    for block in schemas._DIGEST_KIND_BLOCKS.values():
        assert isinstance(block, str) and block


def test_style_fallback_is_plain_stripped_text():
    assert schemas.STYLE_FALLBACK
    assert schemas.STYLE_FALLBACK == schemas.STYLE_FALLBACK.strip()
