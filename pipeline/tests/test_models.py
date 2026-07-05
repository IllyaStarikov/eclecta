"""Unit tests for ``signalpipe.models`` (pure row dataclasses) and the
``signalpipe.__version__`` package constant.

The module has no IO and no import-time side effects, so every test here is a
plain unit test with no fakes. Expected values are derived from the real code
path in ``models.py`` (not from docstrings or config parity).
"""

from __future__ import annotations

import dataclasses
import pathlib
import re
import subprocess
import sys

import pytest

import signalpipe
from signalpipe.models import (
    VALID_ACCESS_TYPES,
    VALID_CHANNELS,
    ProbeResult,
    SourceSpec,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _valid_spec(**over):
    """A minimally-valid SourceSpec; override any field via kwargs."""
    base = dict(
        slug="example",
        name="Example Source",
        type="rss",
        url="https://example.com/feed.xml",
    )
    base.update(over)
    return SourceSpec(**base)


# --------------------------------------------------------------------------- #
# Module-level constants (guard against silent drift)
# --------------------------------------------------------------------------- #
def test_valid_access_types_exact_membership():
    # Documented set — assert the real tuple, not config parity.
    assert VALID_ACCESS_TYPES == ("rss", "atom", "json", "api", "scrape")


def test_valid_channels_exact_membership():
    assert VALID_CHANNELS == (
        "ai",
        "ml-research",
        "devtools",
        "security",
        "hardware",
        "startups",
        "science",
        "news",
    )


def test_valid_channels_divergence_from_config():
    """Latent-divergence guard (see briefing): models' channel set includes
    'news' and omits 'everything', unlike the config channel list."""
    assert "news" in VALID_CHANNELS
    assert "everything" not in VALID_CHANNELS


# --------------------------------------------------------------------------- #
# SourceSpec.validate() — happy paths
# --------------------------------------------------------------------------- #
def test_validate_happy_path_returns_none():
    spec = _valid_spec(topics=["ai", "ml-research"])
    assert spec.validate() is None


@pytest.mark.parametrize("access_type", list(VALID_ACCESS_TYPES))
def test_validate_accepts_every_valid_access_type(access_type):
    assert _valid_spec(type=access_type).validate() is None


@pytest.mark.parametrize("tier", [1, 2, 3])
def test_validate_accepts_every_valid_tier(tier):
    assert _valid_spec(tier=tier).validate() is None


@pytest.mark.parametrize("channel", list(VALID_CHANNELS))
def test_validate_accepts_every_valid_channel(channel):
    assert _valid_spec(topics=[channel]).validate() is None


def test_validate_empty_topics_is_valid():
    assert _valid_spec(topics=[]).validate() is None


# --------------------------------------------------------------------------- #
# SourceSpec.validate() — failure branches (exact messages)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "over, expected",
    [
        (dict(slug=""), "missing slug/name/url"),
        (dict(name=""), "missing slug/name/url"),
        (dict(url=""), "missing slug/name/url"),
        (dict(type="bad"), "bad type 'bad'"),
        (dict(type=""), "bad type ''"),
        (dict(tier=0), "bad tier 0"),
        (dict(tier=4), "bad tier 4"),
        (dict(topics=["bogus"]), "unknown topics ['bogus']"),
        (dict(topics=["ai", "bogus"]), "unknown topics ['bogus']"),
        (dict(topics=["everything"]), "unknown topics ['everything']"),
    ],
)
def test_validate_failure_messages(over, expected):
    assert _valid_spec(**over).validate() == expected


def test_validate_unknown_topics_reports_only_bad_ones():
    msg = _valid_spec(topics=["ai", "bogus", "news", "nope"]).validate()
    assert msg == "unknown topics ['bogus', 'nope']"


def test_validate_none_url_treated_as_missing():
    # url is typed Optional in construction terms; a falsy value trips the guard.
    assert _valid_spec(url=None).validate() == "missing slug/name/url"


# --------------------------------------------------------------------------- #
# SourceSpec.validate() — branch precedence / short-circuit order
# --------------------------------------------------------------------------- #
def test_validate_missing_field_takes_precedence_over_bad_type():
    # url check runs before the type check.
    assert _valid_spec(url="", type="bad").validate() == "missing slug/name/url"


def test_validate_bad_type_takes_precedence_over_bad_tier():
    assert _valid_spec(type="bad", tier=9).validate() == "bad type 'bad'"


def test_validate_bad_tier_takes_precedence_over_bad_topics():
    assert _valid_spec(tier=7, topics=["bogus"]).validate() == "bad tier 7"


# --------------------------------------------------------------------------- #
# SourceSpec dataclass construction / defaults
# --------------------------------------------------------------------------- #
def test_sourcespec_defaults():
    spec = SourceSpec(slug="s", name="N", type="rss", url="https://x/y")
    assert spec.homepage is None
    assert spec.category == "uncategorized"
    assert spec.topics == []
    assert spec.reputation == 1.0
    assert spec.tier == 2
    assert spec.cadence_min == 60
    assert spec.paywalled is False
    assert spec.enabled is True
    assert spec.mode is None
    assert spec.why is None
    assert spec.api_notes is None


def test_sourcespec_topics_default_factory_is_per_instance():
    a = SourceSpec(slug="a", name="A", type="rss", url="https://a")
    b = SourceSpec(slug="b", name="B", type="rss", url="https://b")
    a.topics.append("ai")
    assert a.topics == ["ai"]
    assert b.topics == []  # fresh list, not a shared class-level default
    assert a.topics is not b.topics


def test_sourcespec_field_metadata_uses_default_factory_for_topics():
    fields = {f.name: f for f in dataclasses.fields(SourceSpec)}
    topics_field = fields["topics"]
    assert topics_field.default is dataclasses.MISSING
    assert topics_field.default_factory is list


# --------------------------------------------------------------------------- #
# ProbeResult construction / defaults
# --------------------------------------------------------------------------- #
def test_proberesult_defaults():
    pr = ProbeResult(candidate_url="https://example.com")
    assert pr.candidate_url == "https://example.com"
    assert pr.feed_url is None
    assert pr.ok is False
    assert pr.kind is None
    assert pr.title is None
    assert pr.latest_entry is None
    assert pr.entries == 0
    assert pr.error is None


def test_proberesult_full_construction():
    pr = ProbeResult(
        candidate_url="https://example.com",
        feed_url="https://example.com/feed.xml",
        ok=True,
        kind="atom",
        title="Example Feed",
        latest_entry="2026-07-04",
        entries=17,
        error=None,
    )
    assert pr.candidate_url == "https://example.com"
    assert pr.ok is True
    assert pr.kind == "atom"
    assert pr.feed_url == "https://example.com/feed.xml"
    assert pr.title == "Example Feed"
    assert pr.latest_entry == "2026-07-04"
    assert pr.entries == 17
    assert pr.error is None


# --------------------------------------------------------------------------- #
# Property-based coverage of validate() membership rules
# --------------------------------------------------------------------------- #
def test_validate_property_membership():
    hypothesis = pytest.importorskip("hypothesis")
    from hypothesis import given, settings
    from hypothesis import strategies as st

    identifier = st.text(min_size=1, max_size=12)

    @given(
        access_type=st.sampled_from(VALID_ACCESS_TYPES),
        tier=st.sampled_from((1, 2, 3)),
        topics=st.lists(st.sampled_from(VALID_CHANNELS), max_size=5),
        slug=identifier,
        name=identifier,
        url=identifier,
    )
    @settings(max_examples=75)
    def valid_specs_pass(access_type, tier, topics, slug, name, url):
        spec = SourceSpec(
            slug=slug, name=name, type=access_type, url=url, tier=tier, topics=topics
        )
        assert spec.validate() is None

    @given(
        bad_type=st.text(min_size=1, max_size=12).filter(
            lambda t: t not in VALID_ACCESS_TYPES
        ),
    )
    @settings(max_examples=50)
    def bad_type_specs_fail(bad_type):
        spec = SourceSpec(slug="s", name="N", type=bad_type, url="https://x")
        result = spec.validate()
        assert result is not None
        assert result == "bad type %r" % bad_type

    valid_specs_pass()
    bad_type_specs_fail()


# --------------------------------------------------------------------------- #
# signalpipe.__version__
# --------------------------------------------------------------------------- #
def test_version_is_nonempty_string():
    assert isinstance(signalpipe.__version__, str)
    assert signalpipe.__version__.strip() != ""


def test_version_is_semver_ish():
    # server /healthz + CLI status echo this; guard its shape.
    assert re.match(r"^\d+\.\d+(\.\d+)?", signalpipe.__version__)


def test_importing_signalpipe_is_side_effect_free_in_fresh_interpreter():
    """A clean interpreter can import the package and read __version__ with no
    crash/hang — documents that ``__init__`` performs no import-time IO."""
    pipeline_dir = pathlib.Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [sys.executable, "-c", "import signalpipe; print(signalpipe.__version__)"],
        cwd=str(pipeline_dir),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == signalpipe.__version__
