"""Tests for :mod:`signalpipe.installer`.

The autouse ``redirect_state_dirs`` conftest fixture repoints every module-level
path constant (``APP_DIR``, ``LOGS_DIR``, ``AGENTS_DIR``, ``WRAPPER``,
``WATCHDOG``, ``SIGNAL_SHIM``) at ``tmp_path`` before each test, so nothing here
can scribble on the real ``~/Library/LaunchAgents`` or ``~/.local/state``. We
still fake every subprocess boundary (launchctl / git) and ``shutil.copytree``
(to avoid the ~1.3 MB sources.json copy) so no test shells out for real.
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import plistlib
import subprocess
import sys
from types import SimpleNamespace

import pytest

import signalpipe.installer as installer


PY = sys.executable
BINDIR = os.path.dirname(sys.executable)
LOCAL_BIN = os.path.expanduser("~/.local/bin")


# --------------------------------------------------------------------------- #
# Local fakes
# --------------------------------------------------------------------------- #
class _FakeProc:
    """Stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_repo(tmp_path, with_optional=True):
    """Build a minimal source repo tree that ``_copy_runtime`` reads from."""
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / "config" / "signal.json").write_text('{"ok": 1}')
    if with_optional:
        (repo / "config" / "bulk_sources.json").write_text('{"bulk": 2}')
        (repo / "doc").mkdir()
        (repo / "doc" / "digest-style.md").write_text("# style\n")
    return repo


# --------------------------------------------------------------------------- #
# _plist  /  _periodic_plist
# --------------------------------------------------------------------------- #
def test_plist_server_shape():
    d = installer._plist(installer.LABEL_SERVER, [PY, "-m", "signalpipe", "serve"])

    assert d["Label"] == installer.LABEL_SERVER
    assert d["ProgramArguments"] == [PY, "-m", "signalpipe", "serve"]
    assert d["WorkingDirectory"] == str(installer.APP_DIR)
    assert d["RunAtLoad"] is True
    assert d["KeepAlive"] == {"SuccessfulExit": False}
    assert d["ThrottleInterval"] == 30
    assert d["ProcessType"] == "Interactive"
    # Std*Path derive from the last label segment -> "server".
    assert d["StandardOutPath"] == str(installer.LOGS_DIR / "server.out.log")
    assert d["StandardErrorPath"] == str(installer.LOGS_DIR / "server.err.log")

    env = d["EnvironmentVariables"]
    assert env["LANG"] == "en_US.UTF-8"
    assert env["PATH"].startswith("/usr/local/bin:/usr/bin:/bin:")
    assert BINDIR in env["PATH"]
    assert LOCAL_BIN in env["PATH"]


def test_plist_worker_label_and_program_args():
    d = installer._plist(installer.LABEL_WORKER, ["/bin/sh", str(installer.WRAPPER)])
    assert d["ProgramArguments"] == ["/bin/sh", str(installer.WRAPPER)]
    assert d["StandardOutPath"].endswith("worker.out.log")
    assert d["StandardErrorPath"].endswith("worker.err.log")


def test_plist_extra_env_merges_over_base():
    d = installer._plist(
        installer.LABEL_SERVER,
        [PY],
        extra_env={"ANTHROPIC_API_KEY": "sk-test", "LANG": "C"},
    )
    env = d["EnvironmentVariables"]
    assert env["ANTHROPIC_API_KEY"] == "sk-test"
    # extra_env overrides the base LANG.
    assert env["LANG"] == "C"
    # PATH untouched by the override.
    assert env["PATH"].startswith("/usr/local/bin:/usr/bin:/bin:")


def test_periodic_plist_watchdog_shape():
    d = installer._periodic_plist(
        installer.LABEL_WATCHDOG,
        ["/bin/sh", str(installer.WATCHDOG)],
        installer.WATCHDOG_INTERVAL,
    )
    assert d["StartInterval"] == installer.WATCHDOG_INTERVAL
    assert d["ProcessType"] == "Background"
    assert d["RunAtLoad"] is True
    # A periodic agent is NOT a KeepAlive daemon.
    assert "KeepAlive" not in d
    assert d["ProgramArguments"] == ["/bin/sh", str(installer.WATCHDOG)]
    assert d["WorkingDirectory"] == str(installer.APP_DIR)
    assert d["StandardOutPath"] == str(installer.LOGS_DIR / "watchdog.out.log")
    assert d["StandardErrorPath"] == str(installer.LOGS_DIR / "watchdog.err.log")
    env = d["EnvironmentVariables"]
    assert env["LANG"] == "en_US.UTF-8"
    assert BINDIR in env["PATH"]


# --------------------------------------------------------------------------- #
# Shell template rendering (guards against %-escaping regressions)
# --------------------------------------------------------------------------- #
def test_wrapper_sh_renders():
    out = installer._WRAPPER_SH % {"py": PY}
    assert out.startswith("#!/bin/sh")
    assert "/usr/bin/caffeinate -i" in out
    assert PY in out
    assert "-m signalpipe worker" in out
    # Loads the API key from the out-of-band secret file.
    assert "$HOME/.config/signal/worker.env" in out


def test_watchdog_sh_collapses_double_percent():
    out = installer._WATCHDOG_SH % {
        "stale": installer.HEARTBEAT_STALE_SEC,
        "label": installer.LABEL_WORKER,
    }
    # %% must collapse to a single % in the rendered shell.
    assert "date +%s" in out
    assert "stat -f %m" in out
    assert "date '+%Y-%m-%dT%H:%M:%S'" in out
    assert "%%" not in out
    assert "STALE=%d" % installer.HEARTBEAT_STALE_SEC in out
    assert 'LABEL="%s"' % installer.LABEL_WORKER in out
    assert "kickstart -k" in out


def test_signal_shim_sh_renders():
    out = installer._SIGNAL_SHIM_SH % {"py": PY, "app": str(installer.APP_DIR)}
    assert 'cd "%s"' % str(installer.APP_DIR) in out
    assert 'exec %s -m signalpipe "$@"' % PY in out


# --------------------------------------------------------------------------- #
# _git_rev
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "rc,stdout,expected",
    [
        (0, "abc123\n", "abc123"),
        (0, "abc123", "abc123"),
        (0, "", None),
        (0, "   \n", None),
        (1, "abc123\n", None),
        (128, "", None),
    ],
)
def test_git_rev_returncode_table(monkeypatch, rc, stdout, expected):
    def fake_run(cmd, **kw):
        # It shells out to git rev-parse; assert the shape we asked for.
        assert cmd[0] == "git" and "rev-parse" in cmd
        return _FakeProc(returncode=rc, stdout=stdout)

    monkeypatch.setattr(installer.subprocess, "run", fake_run)
    assert installer._git_rev(pathlib.Path("/tmp/repo")) == expected


@pytest.mark.parametrize(
    "exc", [OSError("no git"), subprocess.SubprocessError("boom")]
)
def test_git_rev_swallows_exceptions(monkeypatch, exc):
    def fake_run(cmd, **kw):
        raise exc

    monkeypatch.setattr(installer.subprocess, "run", fake_run)
    assert installer._git_rev(pathlib.Path("/tmp/repo")) is None


# --------------------------------------------------------------------------- #
# _write_sync_manifest
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_write_sync_manifest_writes_expected_json(monkeypatch, tmp_path):
    monkeypatch.setattr(installer, "_git_rev", lambda repo: "deadbee")
    repo = tmp_path / "repo"
    repo.mkdir()

    installer._write_sync_manifest(repo)

    data = json.loads((installer.APP_DIR / "sync_manifest.json").read_text())
    assert set(data) == {"synced_at", "git_rev", "repo"}
    assert data["git_rev"] == "deadbee"
    assert data["repo"] == str(repo)
    dt = datetime.datetime.fromisoformat(data["synced_at"])
    assert dt.tzinfo is not None  # UTC-aware timestamp


@pytest.mark.integration
def test_write_sync_manifest_oserror_warns_no_raise(monkeypatch, capsys):
    monkeypatch.setattr(installer, "_git_rev", lambda repo: None)
    # Make the write target a directory so write_text raises IsADirectoryError
    # (a subclass of OSError) — the warning branch must swallow it.
    (installer.APP_DIR / "sync_manifest.json").mkdir()

    installer._write_sync_manifest(pathlib.Path("/repo"))  # must not raise

    err = capsys.readouterr().err
    assert "could not write sync manifest" in err


# --------------------------------------------------------------------------- #
# _write_scripts
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_write_scripts_writes_three_executable_scripts(capsys):
    installer._write_scripts()

    assert installer.WRAPPER.read_text() == installer._WRAPPER_SH % {"py": PY}
    assert installer.WATCHDOG.read_text() == installer._WATCHDOG_SH % {
        "stale": installer.HEARTBEAT_STALE_SEC,
        "label": installer.LABEL_WORKER,
    }
    assert installer.SIGNAL_SHIM.read_text() == installer._SIGNAL_SHIM_SH % {
        "py": PY,
        "app": str(installer.APP_DIR),
    }
    # ~/.local/bin analogue was created for the CLI shim.
    assert installer.SIGNAL_SHIM.parent.is_dir()

    for p in (installer.WRAPPER, installer.WATCHDOG, installer.SIGNAL_SHIM):
        # chmod(0o755) -> exactly rwxr-xr-x, no group/other write bits.
        assert p.stat().st_mode & 0o777 == 0o755

    out = capsys.readouterr().out
    assert "wrote %s" % installer.WRAPPER in out
    assert "wrote %s" % installer.WATCHDOG in out
    assert "wrote %s" % installer.SIGNAL_SHIM in out


# --------------------------------------------------------------------------- #
# _write_plists
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_write_plists_round_trips_via_plistlib(capsys):
    installer._write_plists()

    def load(label):
        with open(installer.AGENTS_DIR / ("%s.plist" % label), "rb") as f:
            return plistlib.load(f)

    server = load(installer.LABEL_SERVER)
    worker = load(installer.LABEL_WORKER)
    watchdog = load(installer.LABEL_WATCHDOG)

    assert server["ProgramArguments"] == [PY, "-m", "signalpipe", "serve"]
    assert server["KeepAlive"] == {"SuccessfulExit": False}
    assert server["ProcessType"] == "Interactive"

    assert worker["ProgramArguments"] == ["/bin/sh", str(installer.WRAPPER)]
    assert worker["KeepAlive"] == {"SuccessfulExit": False}

    assert watchdog["ProgramArguments"] == ["/bin/sh", str(installer.WATCHDOG)]
    assert watchdog["StartInterval"] == installer.WATCHDOG_INTERVAL
    assert watchdog["ProcessType"] == "Background"
    assert "KeepAlive" not in watchdog

    out = capsys.readouterr().out
    for label in (installer.LABEL_SERVER, installer.LABEL_WORKER,
                  installer.LABEL_WATCHDOG):
        assert "wrote %s" % (installer.AGENTS_DIR / ("%s.plist" % label)) in out


# --------------------------------------------------------------------------- #
# _copy_runtime
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_copy_runtime_copies_config_doc_and_writes_manifest(
    monkeypatch, tmp_path, capsys
):
    repo = _make_repo(tmp_path, with_optional=True)
    monkeypatch.setattr(installer, "_git_rev", lambda r: "cafef00")

    copytree_calls = []

    def fake_copytree(src, dst, ignore=None):
        copytree_calls.append((pathlib.Path(src), pathlib.Path(dst), ignore))

    monkeypatch.setattr(installer.shutil, "copytree", fake_copytree)

    # Pre-create the destination package so the rmtree-then-copytree branch runs.
    dst_pkg = installer.APP_DIR / "signalpipe"
    dst_pkg.mkdir(parents=True, exist_ok=True)
    (dst_pkg / "marker.txt").write_text("stale")

    installer._copy_runtime(SimpleNamespace(blog_repo=repo))

    # copytree invoked exactly once, real package -> runtime dst, with ignore set.
    assert len(copytree_calls) == 1
    src, dst, ignore = copytree_calls[0]
    assert src == pathlib.Path(installer.__file__).resolve().parent
    assert dst == dst_pkg
    assert ignore is installer._COPY_IGNORE
    # rmtree removed the pre-existing dir (the copytree spy did not recreate it).
    assert not dst_pkg.exists()

    # config + optional + doc copied via copy2.
    assert (installer.APP_DIR / "config" / "signal.json").read_text() == '{"ok": 1}'
    assert (installer.APP_DIR / "config" / "bulk_sources.json").read_text() == '{"bulk": 2}'
    assert (installer.APP_DIR / "doc" / "digest-style.md").read_text() == "# style\n"

    data = json.loads((installer.APP_DIR / "sync_manifest.json").read_text())
    assert data["git_rev"] == "cafef00"
    assert data["repo"] == str(repo)

    assert "runtime synced" in capsys.readouterr().out


@pytest.mark.integration
def test_copy_runtime_skips_absent_optional_files(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path, with_optional=False)
    monkeypatch.setattr(installer, "_git_rev", lambda r: None)
    monkeypatch.setattr(installer.shutil, "copytree", lambda src, dst, ignore=None: None)

    # No pre-existing dst_pkg -> exercises the `if dst_pkg.exists()` False branch.
    installer._copy_runtime(SimpleNamespace(blog_repo=repo))

    assert (installer.APP_DIR / "config" / "signal.json").read_text() == '{"ok": 1}'
    assert not (installer.APP_DIR / "config" / "bulk_sources.json").exists()
    assert not (installer.APP_DIR / "doc" / "digest-style.md").exists()
    # _git_rev returned None -> the manifest records a null git_rev, not a crash.
    manifest = json.loads((installer.APP_DIR / "sync_manifest.json").read_text())
    assert manifest["git_rev"] is None
    assert manifest["repo"] == str(repo)


# --------------------------------------------------------------------------- #
# _launchctl
# --------------------------------------------------------------------------- #
def test_launchctl_builds_command_and_returns_zero_silently(monkeypatch, capsys):
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _FakeProc(returncode=0, stderr="")

    monkeypatch.setattr(installer.subprocess, "run", fake_run)

    rc = installer._launchctl("bootstrap", "gui/501", "/path/x.plist")

    assert rc == 0
    assert captured["cmd"] == ["launchctl", "bootstrap", "gui/501", "/path/x.plist"]
    assert capsys.readouterr().out == ""


def test_launchctl_prints_stderr_on_failure(monkeypatch, capsys):
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda cmd, **kw: _FakeProc(returncode=1, stderr="kaboom"),
    )
    rc = installer._launchctl("bootout", "gui/501/io.starikov.signal.worker")
    assert rc == 1
    out = capsys.readouterr().out
    assert "launchctl bootout gui/501/io.starikov.signal.worker: kaboom" in out


def test_launchctl_nonzero_but_empty_stderr_is_silent(monkeypatch, capsys):
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda cmd, **kw: _FakeProc(returncode=5, stderr="   "),
    )
    rc = installer._launchctl("kickstart", "-k", "gui/501/x")
    assert rc == 5
    assert capsys.readouterr().out == ""


# --------------------------------------------------------------------------- #
# _bootstrap
# --------------------------------------------------------------------------- #
def test_bootstrap_bootout_then_bootstrap_per_label_started(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(installer, "_launchctl", lambda *a: calls.append(a) or 0)

    installer._bootstrap()

    uid = os.getuid()
    domain = "gui/%d" % uid
    expected = []
    for label in (installer.LABEL_SERVER, installer.LABEL_WORKER,
                  installer.LABEL_WATCHDOG):
        path = installer.AGENTS_DIR / ("%s.plist" % label)
        expected.append(("bootout", "%s/%s" % (domain, label)))
        expected.append(("bootstrap", domain, str(path)))
    assert calls == expected

    out = capsys.readouterr().out
    for label in (installer.LABEL_SERVER, installer.LABEL_WORKER,
                  installer.LABEL_WATCHDOG):
        assert "%s: started" % label in out


def test_bootstrap_reports_failure_on_nonzero_bootstrap(monkeypatch, capsys):
    monkeypatch.setattr(installer, "_launchctl", lambda *a: 3)

    installer._bootstrap()

    out = capsys.readouterr().out
    # Every label bootstraps independently -> each reports its own rc-3 failure.
    for label in (installer.LABEL_SERVER, installer.LABEL_WORKER,
                  installer.LABEL_WATCHDOG):
        assert "%s: FAILED (rc 3)" % label in out
    assert out.count("FAILED (rc 3)") == 3
    assert "started" not in out


# --------------------------------------------------------------------------- #
# _warn_if_no_secret
# --------------------------------------------------------------------------- #
def test_warn_if_no_secret_warns_when_missing_and_silent_when_present(
    monkeypatch, tmp_path, capsys
):
    env_file = tmp_path / "worker.env"
    orig = os.path.expanduser
    monkeypatch.setattr(
        os.path,
        "expanduser",
        lambda p: str(env_file) if "worker.env" in p else orig(p),
    )

    # Missing -> warns.
    installer._warn_if_no_secret()
    out = capsys.readouterr().out
    assert "WARN" in out
    assert "worker.env" in out
    assert "ANTHROPIC_API_KEY" in out

    # Present -> silent.
    env_file.write_text("ANTHROPIC_API_KEY=sk-ant-x\n")
    installer._warn_if_no_secret()
    assert capsys.readouterr().out == ""


# --------------------------------------------------------------------------- #
# install() / sync() orchestration
# --------------------------------------------------------------------------- #
def test_install_start_false_order_no_bootstrap(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(installer, "_copy_runtime", lambda cfg: calls.append("copy"))
    monkeypatch.setattr(installer, "_write_scripts", lambda: calls.append("scripts"))
    monkeypatch.setattr(installer, "_write_plists", lambda: calls.append("plists"))
    monkeypatch.setattr(installer, "_warn_if_no_secret", lambda: calls.append("warn"))
    boot = []
    monkeypatch.setattr(installer, "_bootstrap", lambda: boot.append(1))

    rc = installer.install(SimpleNamespace(), start=False)

    assert rc == 0
    assert calls == ["copy", "scripts", "plists", "warn"]
    assert boot == []  # launchd untouched
    assert "plists written" in capsys.readouterr().out


def test_install_start_true_bootstraps_last(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(installer, "_copy_runtime", lambda cfg: calls.append("copy"))
    monkeypatch.setattr(installer, "_write_scripts", lambda: calls.append("scripts"))
    monkeypatch.setattr(installer, "_write_plists", lambda: calls.append("plists"))
    monkeypatch.setattr(installer, "_warn_if_no_secret", lambda: calls.append("warn"))
    monkeypatch.setattr(installer, "_bootstrap", lambda: calls.append("boot"))

    # Non-default port so the assertion proves the port is read from cfg
    # (not that the code happened to fall back to the 8765 default).
    cfg = SimpleNamespace(server={"port": 9099})
    rc = installer.install(cfg, start=True)

    assert rc == 0
    assert calls == ["copy", "scripts", "plists", "warn", "boot"]
    out = capsys.readouterr().out
    assert "check:  launchctl print gui/%d/%s" % (os.getuid(), installer.LABEL_WORKER) in out
    assert "logs:   tail -f %s/worker.err.log" % installer.LOGS_DIR in out
    assert "server: curl -s http://127.0.0.1:9099/healthz" in out


def test_install_start_true_uses_default_port(monkeypatch, capsys):
    for name in ("_copy_runtime",):
        monkeypatch.setattr(installer, name, lambda cfg: None)
    for name in ("_write_scripts", "_write_plists", "_warn_if_no_secret", "_bootstrap"):
        monkeypatch.setattr(installer, name, lambda: None)

    # server dict without a "port" key -> falls back to 8765.
    cfg = SimpleNamespace(server={})
    rc = installer.install(cfg, start=True)
    assert rc == 0
    assert "8765" in capsys.readouterr().out


@pytest.mark.integration
def test_install_end_to_end_writes_files_without_launchd(
    monkeypatch, tmp_path, capsys
):
    repo = _make_repo(tmp_path, with_optional=True)
    monkeypatch.setattr(installer, "_git_rev", lambda r: "e2e1234")
    monkeypatch.setattr(installer.shutil, "copytree", lambda src, dst, ignore=None: None)
    boot = []
    monkeypatch.setattr(installer, "_bootstrap", lambda: boot.append(1))
    warn = []
    monkeypatch.setattr(installer, "_warn_if_no_secret", lambda: warn.append(1))

    rc = installer.install(SimpleNamespace(blog_repo=repo), start=False)

    assert rc == 0
    assert boot == []
    assert warn == [1]

    assert installer.WRAPPER.exists()
    assert installer.WATCHDOG.exists()
    assert installer.SIGNAL_SHIM.exists()
    for label in (installer.LABEL_SERVER, installer.LABEL_WORKER,
                  installer.LABEL_WATCHDOG):
        assert (installer.AGENTS_DIR / ("%s.plist" % label)).exists()
    assert (installer.APP_DIR / "sync_manifest.json").exists()
    assert "plists written" in capsys.readouterr().out


def test_sync_restart_kickstarts_each_label(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(installer, "_copy_runtime", lambda cfg: calls.append("copy"))
    monkeypatch.setattr(installer, "_write_scripts", lambda: calls.append("scripts"))
    lc = []
    monkeypatch.setattr(installer, "_launchctl", lambda *a: lc.append(a) or 0)

    rc = installer.sync(SimpleNamespace(), restart=True)

    assert rc == 0
    assert calls == ["copy", "scripts"]
    uid = os.getuid()
    assert lc == [
        ("kickstart", "-k", "gui/%d/%s" % (uid, installer.LABEL_SERVER)),
        ("kickstart", "-k", "gui/%d/%s" % (uid, installer.LABEL_WORKER)),
        ("kickstart", "-k", "gui/%d/%s" % (uid, installer.LABEL_WATCHDOG)),
    ]
    out = capsys.readouterr().out
    assert "restarted %s" % installer.LABEL_WORKER in out


def test_sync_no_restart_prints_hint(monkeypatch, capsys):
    monkeypatch.setattr(installer, "_copy_runtime", lambda cfg: None)
    monkeypatch.setattr(installer, "_write_scripts", lambda: None)
    lc = []
    monkeypatch.setattr(installer, "_launchctl", lambda *a: lc.append(a) or 0)

    rc = installer.sync(SimpleNamespace(), restart=False)

    assert rc == 0
    assert lc == []  # no launchctl call
    assert "runtime synced; restart agents" in capsys.readouterr().out
