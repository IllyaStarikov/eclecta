"""Tests for :mod:`signalpipe.config`.

Covers load()/Config construction + validation, typed accessors (paths, tiers,
backends, digests, sections), the config fingerprint, file-hash change tracking,
last-run stamping, save() round-trips, and the iCloud-DB safety guard.

All tests are hermetic: no network, no real ``$HOME`` state (the autouse
``redirect_state_dirs`` fixture repoints ``config.STATE_DIR`` at tmp), and every
filesystem write lands under pytest ``tmp_path``.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib
from typing import Any, Dict

import pytest

import signalpipe.config as config_mod
from signalpipe.config import DIGEST_DEFAULTS, Config, ConfigError


# --------------------------------------------------------------------------- #
# Local helpers — a fresh, fully-valid config dict per call (all fresh literals
# so tests can mutate one field without leaking into other tests).
# --------------------------------------------------------------------------- #
def _base_data() -> Dict[str, Any]:
    return {
        "db_path": "~/.local/state/signal/signal.db",
        "user_agent": "signalpipe-test/0.1 (+https://eclecta.co; test)",
        "server": {"host": "127.0.0.1", "port": 8765},
        "backend": {
            "selector": "subscription",
            "tier_overrides": {},
            "local": {"models": ["qwen2.5:14b"]},
        },
        "tiers": {
            "triage": {
                "subscription": "claude-haiku-4-5",
                "api": "claude-haiku-4-5",
                "local": "qwen2.5:14b",
            },
            "deep": {
                "subscription": "claude-sonnet-4-6",
                "api": "claude-sonnet-4-6",
            },
            "judge": {
                "subscription": "claude-haiku-4-5",
                "api": "claude-haiku-4-5",
                "local": "qwen2.5:14b",
            },
            "write": {
                "subscription": "claude-sonnet-4-6",
                "api": "claude-sonnet-4-6",
                "local": "qwen2.5:14b",
            },
            "digest": {
                "subscription": "claude-opus-4-8",
                "api": "claude-opus-4-8",
            },
        },
        "cadences": {"ingest_min": 45},
        "funnel": {"score_window_hours": 72},
        "spend": {"daily_cap_usd": 10.0},
        "channels": ["ai", "ml-research"],
        "score_weights": {"consensus": 0.3},
        "paywall": {"paywall_domains": []},
        "dedup": {"near_dup_window_hours": 48},
        "digests": {"daily": {"max_items": 99}},
    }


def _cfg(tmp_path: pathlib.Path, data: Dict[str, Any] = None) -> Config:
    """Construct a Config against a writable (not-yet-created) tmp path."""
    return Config(_base_data() if data is None else data, tmp_path / "signal.json")


# --------------------------------------------------------------------------- #
# Module-level constants + error type
# --------------------------------------------------------------------------- #
def test_constants_exposed():
    assert config_mod.VALID_BACKENDS == ("subscription", "api")
    assert config_mod.VALID_TIERS == ("triage", "deep", "judge", "write", "digest")
    assert set(DIGEST_DEFAULTS) == {"daily", "weekly", "monthly", "quarterly", "yearly"}
    assert issubclass(ConfigError, Exception)


# --------------------------------------------------------------------------- #
# sha256_file
# --------------------------------------------------------------------------- #
@pytest.mark.integration
@pytest.mark.parametrize(
    "payload",
    [b"", b"small", b"hello world\n" * 10000],  # last one exceeds the 64 KiB chunk
)
def test_sha256_file_matches_hashlib(tmp_path, payload):
    f = tmp_path / "f.bin"
    f.write_bytes(payload)
    assert config_mod.sha256_file(f) == hashlib.sha256(payload).hexdigest()


def test_sha256_file_missing_returns_none(tmp_path):
    assert config_mod.sha256_file(tmp_path / "nope") is None


def test_sha256_file_directory_returns_none(tmp_path):
    # open()ing a directory raises IsADirectoryError (an OSError) -> swallowed -> None.
    assert config_mod.sha256_file(tmp_path) is None


# --------------------------------------------------------------------------- #
# load()
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_load_valid_roundtrips_into_config(tmp_path):
    data = _base_data()
    p = tmp_path / "signal.json"
    p.write_text(json.dumps(data))
    cfg = config_mod.load(p)
    assert isinstance(cfg, Config)
    assert cfg.path == p
    assert cfg.data["channels"] == ["ai", "ml-research"]
    assert cfg.model_for("triage") == "claude-haiku-4-5"


@pytest.mark.integration
def test_example_config_roundtrips(tmp_path):
    example = pathlib.Path(__file__).resolve().parent.parent / "config" / "signal.example.json"
    dst = tmp_path / "signal.json"
    dst.write_text(example.read_text())
    cfg = config_mod.load(dst)
    assert cfg.model_for("digest", "api") == "claude-opus-4-8"
    # example ships all tiers on the subscription (empty tier_overrides):
    # triage + judge on Sonnet, write + digest on Opus.
    assert cfg.backend_for("triage") == "subscription"
    assert cfg.model_for("triage") == "claude-sonnet-5"
    assert cfg.model_for("judge") == "claude-sonnet-5"
    assert cfg.model_for("write") == "claude-opus-4-8"
    assert cfg.backend_for("write") == "subscription"
    assert set(cfg.digests) == {"daily", "weekly", "monthly", "quarterly", "yearly"}
    assert "Mobile Documents" not in str(cfg.db_path)
    assert cfg.blog_repo == pathlib.Path("/path/to/starikov-dot-co")
    assert cfg.channels[0] == "ai"


def test_load_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError, match="cannot read config"):
        config_mod.load(tmp_path / "does-not-exist.json")


def test_load_invalid_json_raises(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{bad json")
    with pytest.raises(ConfigError, match="is not valid JSON"):
        config_mod.load(p)


def test_load_default_path_missing_raises(tmp_path, monkeypatch):
    # No explicit path -> DEFAULT_CONFIG_PATH (signal.json), which is not committed.
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", tmp_path / "absent.json")
    with pytest.raises(ConfigError, match="cannot read config"):
        config_mod.load()


# --------------------------------------------------------------------------- #
# _validate (runs eagerly at construction)
# --------------------------------------------------------------------------- #
def test_valid_config_constructs(tmp_path):
    # A fully-valid dict constructs without raising and yields a usable config:
    # the eager _validate() passed (incl. the iCloud db_path guard) and accessors
    # resolve real values, not just "an object came back".
    cfg = _cfg(tmp_path)
    assert isinstance(cfg, Config)
    assert cfg.model_for("triage", "api") == "claude-haiku-4-5"
    assert cfg.db_path == pathlib.Path(os.path.expanduser("~/.local/state/signal/signal.db"))


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda d: d.pop("server"), "missing keys"),
        (lambda d: d.pop("dedup"), "missing keys"),
        (lambda d: d["backend"].__setitem__("selector", "bogus"), "selector must be one of"),
        (lambda d: d["backend"].pop("selector"), "selector must be one of"),
        (lambda d: d["tiers"]["write"].pop("api"), "must map both backends"),
        (lambda d: d["tiers"].pop("write"), "must map both backends"),
        (lambda d: d["tiers"].update({"deep": {"subscription": "x"}}), "must map both backends"),
        (lambda d: d.__setitem__("channels", []), "channels must be non-empty"),
    ],
)
def test_validate_rejects_defects(tmp_path, mutate, match):
    data = _base_data()
    mutate(data)
    with pytest.raises(ConfigError, match=match):
        _cfg(tmp_path, data)


def test_db_path_icloud_guard_raises_at_construction(tmp_path):
    data = _base_data()
    data["db_path"] = "~/Mobile Documents/x/signal.db"
    with pytest.raises(ConfigError, match="iCloud"):
        _cfg(tmp_path, data)


# --------------------------------------------------------------------------- #
# db_path
# --------------------------------------------------------------------------- #
def test_db_path_default_when_absent(tmp_path):
    data = _base_data()
    del data["db_path"]
    cfg = _cfg(tmp_path, data)
    assert cfg.db_path == pathlib.Path(os.path.expanduser("~/.local/state/signal/signal.db"))


def test_db_path_custom_absolute(tmp_path):
    data = _base_data()
    data["db_path"] = "/var/data/signal.db"
    cfg = _cfg(tmp_path, data)
    assert cfg.db_path == pathlib.Path("/var/data/signal.db")


# --------------------------------------------------------------------------- #
# path accessors: repo_root, blog_repo, repo_path, sources_*, staging_dir
# --------------------------------------------------------------------------- #
def test_repo_root_property_tracks_module_constant(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "REPO_ROOT", tmp_path / "repo")
    cfg = _cfg(tmp_path)
    assert cfg.repo_root == tmp_path / "repo"


def test_blog_repo_defaults_to_repo_root(tmp_path):
    cfg = _cfg(tmp_path)  # base has no blog_repo
    assert cfg.blog_repo == config_mod.REPO_ROOT


def test_blog_repo_custom_expanduser(tmp_path):
    data = _base_data()
    data["blog_repo"] = "~/myblog"
    cfg = _cfg(tmp_path, data)
    assert cfg.blog_repo == pathlib.Path(os.path.expanduser("~/myblog"))


def test_repo_path_absolute_passthrough(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.repo_path("/abs/some/file.json") == pathlib.Path("/abs/some/file.json")


def test_repo_path_relative_joins_repo_root(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "REPO_ROOT", tmp_path / "repo")
    cfg = _cfg(tmp_path)
    assert (
        cfg.repo_path("signalpipe/sources.json")
        == tmp_path / "repo" / "signalpipe" / "sources.json"
    )


def test_repo_path_home_expands_to_absolute(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.repo_path("~/foo") == pathlib.Path(os.path.expanduser("~/foo"))


def test_sources_json_and_opml_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "REPO_ROOT", tmp_path / "repo")
    cfg = _cfg(tmp_path)  # base has no 'sources' block
    assert cfg.sources_json == tmp_path / "repo" / "signalpipe" / "sources.json"
    assert cfg.sources_opml == tmp_path / "repo" / "signalpipe" / "sources.opml"


def test_sources_json_and_opml_custom_absolute(tmp_path):
    data = _base_data()
    data["sources"] = {"registry": "/custom/reg.json", "opml": "/custom/reg.opml"}
    cfg = _cfg(tmp_path, data)
    assert cfg.sources_json == pathlib.Path("/custom/reg.json")
    assert cfg.sources_opml == pathlib.Path("/custom/reg.opml")


@pytest.mark.integration
def test_staging_dir_is_created_on_read(cfg, tmp_path, monkeypatch):
    # Documents the read-time mkdir side effect against config.STATE_DIR.
    fresh = tmp_path / "freshstate"
    monkeypatch.setattr(config_mod, "STATE_DIR", fresh)
    assert not fresh.exists()
    result = cfg.staging_dir
    assert result == fresh / "staging"
    assert result.is_dir()


# --------------------------------------------------------------------------- #
# section passthrough accessors
# --------------------------------------------------------------------------- #
def test_section_accessors(cfg):
    # Each accessor is a passthrough to a specific data block; pin a concrete
    # value from that block so a swapped/misnamed accessor is caught (membership
    # or isinstance-dict would pass even if the wrong block came back).
    assert cfg.server["port"] == 8765
    assert cfg.backend["selector"] == "subscription"
    assert cfg.tiers["triage"]["subscription"] == "claude-haiku-4-5"
    assert cfg.cadences["ingest_min"] == 45
    assert cfg.funnel["score_window_hours"] == 72
    assert cfg.spend["daily_cap_usd"] == 10.0
    assert cfg.channels[0] == "ai"
    assert cfg.score_weights["consensus"] == 0.3
    assert cfg.paywall["paywall_domains"] == ["nytimes.com", "wsj.com", "ft.com"]
    assert cfg.dedup["near_dup_window_hours"] == 48
    assert cfg.ingest["reddit_mode"] == "public_json"
    assert cfg.site["url"] == "https://eclecta.co"


def test_channels_returns_fresh_list(cfg):
    # Content mirrors the underlying data (pin the ends to a concrete literal)...
    assert cfg.channels == cfg.data["channels"]
    assert cfg.channels[0] == "ai" and cfg.channels[-1] == "everything"
    # ...but each read is a fresh copy, so callers can't mutate cfg.data via it.
    fresh = cfg.channels
    assert fresh is not cfg.data["channels"]
    fresh.append("MUTATED")
    assert "MUTATED" not in cfg.data["channels"]


def test_ingest_and_site_default_empty(tmp_path):
    cfg = _cfg(tmp_path)  # base has neither ingest nor site
    assert cfg.ingest == {}
    assert cfg.site == {}


def test_user_agent_custom(cfg):
    assert cfg.user_agent == "signalpipe-test/0.1 (+https://eclecta.co; test)"


def test_user_agent_default(tmp_path):
    data = _base_data()
    del data["user_agent"]
    cfg = _cfg(tmp_path, data)
    assert cfg.user_agent == "signalpipe/0.1 (+https://eclecta.co; feed curator)"


# --------------------------------------------------------------------------- #
# digests merge
# --------------------------------------------------------------------------- #
def test_digests_merge_config_over_defaults(tmp_path):
    data = _base_data()
    data["digests"] = {
        "daily": {"max_items": 99, "min_relevance": 3},
        "unknownkind": {"max_items": 1},  # not in DIGEST_DEFAULTS -> ignored
    }
    cfg = _cfg(tmp_path, data)
    d = cfg.digests
    assert set(d) == {"daily", "weekly", "monthly", "quarterly", "yearly"}
    # config wins on overlapping keys...
    assert d["daily"]["max_items"] == 99
    assert d["daily"]["min_relevance"] == 3
    # ...but defaults are preserved for non-overridden keys.
    assert d["daily"]["cron"] == DIGEST_DEFAULTS["daily"]["cron"]
    # kinds absent from config come straight from defaults.
    assert d["weekly"] == DIGEST_DEFAULTS["weekly"]
    assert "unknownkind" not in d


def test_digests_all_defaults_when_config_absent(tmp_path):
    data = _base_data()
    del data["digests"]
    cfg = _cfg(tmp_path, data)
    assert cfg.digests == {k: dict(v) for k, v in DIGEST_DEFAULTS.items()}


def test_digests_returns_copies_not_default_refs(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.digests["weekly"]["max_items"] = -1
    assert DIGEST_DEFAULTS["weekly"]["max_items"] != -1


# --------------------------------------------------------------------------- #
# model_for
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "tier, backend, expected",
    [
        ("digest", "api", "claude-opus-4-8"),
        ("digest", "subscription", "claude-opus-4-8"),
        ("deep", "local", "claude-sonnet-4-6"),  # local remaps to subscription
        ("triage", "local", "claude-haiku-4-5"),
        ("triage", None, "claude-haiku-4-5"),  # default selector = subscription
        ("triage", "api", "claude-haiku-4-5"),
    ],
)
def test_model_for_resolution(tmp_path, tier, backend, expected):
    cfg = _cfg(tmp_path)
    assert cfg.model_for(tier, backend) == expected


def test_model_for_uses_selector_when_backend_none(tmp_path):
    data = _base_data()
    data["backend"]["selector"] = "api"
    data["tiers"]["digest"]["api"] = "api-only-model"
    cfg = _cfg(tmp_path, data)
    assert cfg.model_for("digest") == "api-only-model"  # selector picked
    assert cfg.model_for("digest", "subscription") == "claude-opus-4-8"


def test_model_for_unknown_tier_raises(tmp_path):
    cfg = _cfg(tmp_path)
    with pytest.raises(ConfigError, match="unknown tier"):
        cfg.model_for("bogus")


# --------------------------------------------------------------------------- #
# backend_for
# --------------------------------------------------------------------------- #
def test_backend_for_uses_selector_when_no_override(tmp_path):
    cfg = _cfg(tmp_path)  # tier_overrides == {}
    assert cfg.backend_for("triage") == "subscription"
    assert cfg.backend_for("digest") == "subscription"


def test_backend_for_honors_overrides(tmp_path):
    data = _base_data()
    data["backend"]["tier_overrides"] = {"triage": "local", "judge": "local"}
    cfg = _cfg(tmp_path, data)
    assert cfg.backend_for("triage") == "local"
    assert cfg.backend_for("judge") == "local"
    assert cfg.backend_for("write") == "subscription"  # falls through to selector


def test_backend_for_no_overrides_key(tmp_path):
    data = _base_data()
    data["backend"].pop("tier_overrides")  # overrides absent -> {} default
    data["backend"]["selector"] = "api"
    cfg = _cfg(tmp_path, data)
    assert cfg.backend_for("deep") == "api"


def test_backend_for_unknown_tier_raises(tmp_path):
    cfg = _cfg(tmp_path)
    with pytest.raises(ConfigError, match="unknown tier"):
        cfg.backend_for("nope")


# --------------------------------------------------------------------------- #
# local_models_for
# --------------------------------------------------------------------------- #
def test_local_models_for_per_tier_string_normalized(tmp_path):
    # base tiers.triage.local is a bare string -> wrapped into a 1-list.
    cfg = _cfg(tmp_path)
    assert cfg.local_models_for("triage") == ["qwen2.5:14b"]


def test_local_models_for_falls_back_to_backend_local_models(tmp_path):
    cfg = _cfg(tmp_path)  # 'deep' has no per-tier local
    assert cfg.local_models_for("deep") == ["qwen2.5:14b"]


def test_local_models_for_backend_models_string_normalized(tmp_path):
    data = _base_data()
    data["backend"]["local"]["models"] = "solo-model"  # bare string fallback
    cfg = _cfg(tmp_path, data)
    assert cfg.local_models_for("deep") == ["solo-model"]


def test_local_models_for_list_passthrough_is_copy(tmp_path):
    data = _base_data()
    data["backend"]["local"]["models"] = ["a", "b"]
    cfg = _cfg(tmp_path, data)
    got = cfg.local_models_for("deep")
    assert got == ["a", "b"]
    assert got is not data["backend"]["local"]["models"]


def test_local_models_for_empty_when_no_local_anywhere(tmp_path):
    data = _base_data()
    data["backend"].pop("local")  # no backend.local at all
    cfg = _cfg(tmp_path, data)
    assert cfg.local_models_for("deep") == []


# --------------------------------------------------------------------------- #
# config_fingerprint
# --------------------------------------------------------------------------- #
def test_config_fingerprint_shape_and_stability(cfg):
    fp = cfg.config_fingerprint()
    assert set(fp) == {"hash", "tunables"}
    assert isinstance(fp["hash"], str) and len(fp["hash"]) == 12
    int(fp["hash"], 16)  # 12-char hex slice
    assert cfg.config_fingerprint()["hash"] == fp["hash"]  # deterministic
    assert "funnel" in fp["tunables"] and "digests" in fp["tunables"]


def test_config_fingerprint_changes_on_tunable(cfg):
    before = cfg.config_fingerprint()["hash"]
    cfg.data["funnel"]["score_window_hours"] = 999
    assert cfg.config_fingerprint()["hash"] != before


def test_config_fingerprint_ignores_cosmetic_keys(cfg):
    before = cfg.config_fingerprint()["hash"]
    cfg.data["server"]["port"] = 9999
    cfg.data["user_agent"] = "totally-different"
    assert cfg.config_fingerprint()["hash"] == before


# --------------------------------------------------------------------------- #
# tracking + last_run + save  (filesystem round-trips)
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_tracked_changes_detects_changed_and_unchanged(tmp_path):
    f = tmp_path / "tracked.txt"
    f.write_text("original")
    old = config_mod.sha256_file(f)
    data = _base_data()
    data["tracking"] = {str(f): old}  # absolute key -> repo_path passthrough
    cfg = _cfg(tmp_path, data)
    assert cfg.tracked_changes() == []  # hash matches record
    f.write_text("MUTATED CONTENT")
    assert cfg.tracked_changes() == [str(f)]


@pytest.mark.integration
def test_tracked_changes_missing_file_reads_as_changed(tmp_path):
    gone = tmp_path / "gone.txt"  # never created
    data = _base_data()
    data["tracking"] = {str(gone): "some-old-hash"}
    cfg = _cfg(tmp_path, data)
    assert cfg.tracked_changes() == [str(gone)]  # None != old -> changed


@pytest.mark.integration
def test_tracked_changes_missing_file_matching_none_record(tmp_path):
    gone = tmp_path / "still-gone.txt"
    data = _base_data()
    data["tracking"] = {str(gone): None}  # never hashed before
    cfg = _cfg(tmp_path, data)
    assert cfg.tracked_changes() == []  # None == None -> unchanged


@pytest.mark.integration
def test_tracked_changes_empty_when_no_tracking(tmp_path):
    cfg = _cfg(tmp_path)  # base has no 'tracking'
    assert cfg.tracked_changes() == []


@pytest.mark.integration
def test_update_tracking_hashes_and_persists(tmp_path):
    f = tmp_path / "in.txt"
    f.write_text("data-v1")
    cfg = _cfg(tmp_path)
    cfg.update_tracking([str(f)])
    expected = config_mod.sha256_file(f)
    assert cfg.data["tracking"][str(f)] == expected
    # save() persisted the whole config to cfg.path
    on_disk = json.loads(cfg.path.read_text())
    assert on_disk["tracking"][str(f)] == expected


@pytest.mark.integration
def test_update_tracking_no_args_rehashes_existing_keys(tmp_path):
    f = tmp_path / "in.txt"
    f.write_text("data-v1")
    cfg = _cfg(tmp_path)
    cfg.update_tracking([str(f)])
    f.write_text("data-v2")
    cfg.update_tracking()  # rels=None -> iterate existing tracking keys
    assert cfg.data["tracking"][str(f)] == config_mod.sha256_file(f)


@pytest.mark.integration
def test_write_last_run_stamps_and_persists(cfg):
    cfg.write_last_run("ingest", {"added": 5, "skipped": 2})
    lr = cfg.data["last_run"]
    assert lr["job"] == "ingest"
    assert lr["stats"] == {"added": 5, "skipped": 2}
    parsed = datetime.datetime.fromisoformat(lr["timestamp"])
    assert parsed.tzinfo is not None  # timezone-aware UTC stamp
    on_disk = json.loads(cfg.path.read_text())
    assert on_disk["last_run"]["job"] == "ingest"
    assert on_disk["last_run"]["stats"]["added"] == 5


@pytest.mark.integration
def test_save_round_trips_json_with_trailing_newline(cfg):
    cfg.data["marker"] = {"nested": [1, 2, 3]}
    cfg.save()
    text = cfg.path.read_text()
    assert text.endswith("\n")
    assert "\n  " in text  # indent=2 pretty-print
    reloaded = json.loads(text)
    assert reloaded["marker"] == {"nested": [1, 2, 3]}
    assert reloaded == cfg.data


@pytest.mark.integration
def test_save_raises_on_non_serializable_data(cfg):
    cfg.data["bad"] = {1, 2, 3}  # a set is not JSON-serializable
    with pytest.raises(TypeError):
        cfg.save()
