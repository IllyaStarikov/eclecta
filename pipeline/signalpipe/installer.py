"""Install / sync the TCC-safe runtime + launchd agents.

The repo lives in iCloud Drive (`~/Library/Mobile Documents/...`); launchd-
spawned processes may be TCC-blocked from reading it and iCloud is hostile
to SQLite. So the daemons run from a runtime COPY at
~/.local/state/signal/app/ containing:

    app/signalpipe/   (package code + templates + static + sources files)
    app/config/signal.json        (+ signal.topics.json, bulk_sources.json)
    app/doc/ask-me-scope.md       (topic extraction input)
    app/doc/digest-style.md       (digest style guide; baked-in fallback)

`install` copies the runtime, writes two LaunchAgents, and bootstraps them:
    io.starikov.signal.server  — uvicorn, pure reader, KeepAlive
    io.starikov.signal.worker  — caffeinate -i + APScheduler, sole writer
`sync` refreshes the runtime after repo edits (and optionally restarts).

launchd choices per doc/signal_research.md Part F: user LaunchAgent (keychain
reachable for `claude -p` OAuth), KeepAlive={SuccessfulExit:false},
ThrottleInterval=30, ProcessType=Interactive, absolute binary paths,
logs in ~/Library/Logs/signal/.
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import plistlib
import shutil
import subprocess
import sys

from .config import STATE_DIR

APP_DIR = STATE_DIR / "app"
LOGS_DIR = pathlib.Path(os.path.expanduser("~/Library/Logs/signal"))
AGENTS_DIR = pathlib.Path(os.path.expanduser("~/Library/LaunchAgents"))
LABEL_SERVER = "io.starikov.signal.server"
LABEL_WORKER = "io.starikov.signal.worker"
LABEL_WATCHDOG = "io.starikov.signal.watchdog"
WRAPPER = APP_DIR / "run-worker.sh"
WATCHDOG = APP_DIR / "signal-watchdog.sh"
SIGNAL_SHIM = pathlib.Path(os.path.expanduser("~/.local/bin/signal"))
WATCHDOG_INTERVAL = 300  # seconds between heartbeat-staleness checks
HEARTBEAT_STALE_SEC = 900  # restart the worker if the heartbeat is older than this

_COPY_IGNORE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", ".DS_Store", "*.egg-info"
)


def _copy_runtime(cfg) -> None:
    src_pkg = pathlib.Path(__file__).resolve().parent
    repo = cfg.blog_repo

    APP_DIR.mkdir(parents=True, exist_ok=True)
    dst_pkg = APP_DIR / "signalpipe"
    if dst_pkg.exists():
        shutil.rmtree(dst_pkg)
    shutil.copytree(src_pkg, dst_pkg, ignore=_COPY_IGNORE)

    (APP_DIR / "config").mkdir(exist_ok=True)
    shutil.copy2(repo / "config" / "signal.json", APP_DIR / "config" / "signal.json")
    for name in ("signal.topics.json", "bulk_sources.json"):
        src = repo / "config" / name
        if src.exists():
            shutil.copy2(src, APP_DIR / "config" / name)

    (APP_DIR / "doc").mkdir(exist_ok=True)
    for name in ("ask-me-scope.md", "digest-style.md"):
        src = repo / "doc" / name
        if src.exists():
            shutil.copy2(src, APP_DIR / "doc" / name)

    _write_sync_manifest(repo)
    print("runtime synced -> %s" % APP_DIR)


def _git_rev(repo: pathlib.Path):
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        return proc.stdout.strip() or None if proc.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _write_sync_manifest(repo: pathlib.Path) -> None:
    """Record when (and from which repo state) the runtime copy was last
    synced, making staleness visible: the worker compares the repo config
    against the runtime copy at startup and warns when the copy is older
    (live incident: a stale runtime spend cap silently blocked curation)."""
    manifest = {
        "synced_at": datetime.datetime.now(
            datetime.timezone.utc).isoformat(),
        "git_rev": _git_rev(repo),
        "repo": str(repo),
    }
    try:
        (APP_DIR / "sync_manifest.json").write_text(
            json.dumps(manifest, indent=1) + "\n")
    except OSError as e:
        print("could not write sync manifest: %s" % e, file=sys.stderr)


def _plist(label: str, program_args, extra_env=None) -> dict:
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin:%s:%s"
        % (
            os.path.dirname(sys.executable),
            os.path.expanduser("~/.local/bin"),
        ),
        "LANG": "en_US.UTF-8",
    }
    if extra_env:
        env.update(extra_env)
    return {
        "Label": label,
        "ProgramArguments": program_args,
        "WorkingDirectory": str(APP_DIR),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "ProcessType": "Interactive",
        "StandardOutPath": str(LOGS_DIR / ("%s.out.log" % label.split(".")[-1])),
        "StandardErrorPath": str(LOGS_DIR / ("%s.err.log" % label.split(".")[-1])),
        "EnvironmentVariables": env,
    }


def _periodic_plist(label: str, program_args, interval: int) -> dict:
    """A short-lived periodic agent (StartInterval), not a KeepAlive daemon —
    used for the watchdog, which runs, checks the heartbeat, and exits."""
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin:%s:%s"
        % (os.path.dirname(sys.executable), os.path.expanduser("~/.local/bin")),
        "LANG": "en_US.UTF-8",
    }
    return {
        "Label": label,
        "ProgramArguments": program_args,
        "WorkingDirectory": str(APP_DIR),
        "StartInterval": interval,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "StandardOutPath": str(LOGS_DIR / ("%s.out.log" % label.split(".")[-1])),
        "StandardErrorPath": str(LOGS_DIR / ("%s.err.log" % label.split(".")[-1])),
        "EnvironmentVariables": env,
    }


# Worker launch wrapper: load the API key for the metered (api-tier) curation
# calls, then exec the scheduler under caffeinate. The subscription digest path
# (backend_cli.py / claude -p) strips ANTHROPIC_API_KEY itself, so digests stay
# on the Max plan regardless of the key being present here.
_WRAPPER_SH = """#!/bin/sh
set -a
[ -f "$HOME/.config/signal/worker.env" ] && . "$HOME/.config/signal/worker.env"
set +a
exec /usr/bin/caffeinate -i %(py)s -m signalpipe worker
"""

# Watchdog: restart the worker if its heartbeat goes stale — a hang the worker's
# live pid hides from launchd KeepAlive (which only fires on process exit).
_WATCHDOG_SH = """#!/bin/sh
HB="$HOME/.local/state/signal/heartbeat"
STALE=%(stale)d
LABEL="%(label)s"
UID_N=$(id -u)
ts() { date '+%%Y-%%m-%%dT%%H:%%M:%%S'; }
restart() {
    echo "$(ts) $1 - kickstarting $LABEL"
    launchctl kickstart -k "gui/$UID_N/$LABEL"
    osascript -e "display notification \\"$1 - restarted worker\\" with title \\"signal watchdog\\"" 2>/dev/null || true
}
if [ ! -f "$HB" ]; then
    restart "heartbeat missing"
    exit 0
fi
now=$(date +%%s)
mtime=$(stat -f %%m "$HB" 2>/dev/null || echo 0)
age=$(( now - mtime ))
if [ "$age" -gt "$STALE" ]; then
    restart "heartbeat stale ${age}s"
fi
"""


# Global CLI shim on PATH so `signal pause 2h` / `signal resume` / `signal
# downtime` / `signal status` work from anywhere (~/.local/bin is already on the
# agents' PATH and the user's interactive shell).
_SIGNAL_SHIM_SH = """#!/bin/sh
cd "%(app)s" 2>/dev/null || true
exec %(py)s -m signalpipe "$@"
"""


def _write_scripts() -> None:
    """(Re)write the worker wrapper + watchdog scripts into the runtime dir, plus
    the global `signal` CLI shim. The secret (~/.config/signal/worker.env) is
    created out-of-band, never by the installer."""
    APP_DIR.mkdir(parents=True, exist_ok=True)
    WRAPPER.write_text(_WRAPPER_SH % {"py": sys.executable})
    WRAPPER.chmod(0o755)
    WATCHDOG.write_text(
        _WATCHDOG_SH % {"stale": HEARTBEAT_STALE_SEC, "label": LABEL_WORKER})
    WATCHDOG.chmod(0o755)
    SIGNAL_SHIM.parent.mkdir(parents=True, exist_ok=True)
    SIGNAL_SHIM.write_text(
        _SIGNAL_SHIM_SH % {"py": sys.executable, "app": str(APP_DIR)})
    SIGNAL_SHIM.chmod(0o755)
    print("wrote %s" % WRAPPER)
    print("wrote %s" % WATCHDOG)
    print("wrote %s" % SIGNAL_SHIM)


def _write_plists() -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    py = sys.executable
    server = _plist(
        LABEL_SERVER, [py, "-m", "signalpipe", "serve"]
    )
    # The worker runs through the wrapper so ANTHROPIC_API_KEY is loaded for the
    # api-tier curation calls; the wrapper handles caffeinate. KeepAlive still
    # restarts it on crash.
    worker = _plist(LABEL_WORKER, ["/bin/sh", str(WRAPPER)])
    watchdog = _periodic_plist(
        LABEL_WATCHDOG, ["/bin/sh", str(WATCHDOG)], WATCHDOG_INTERVAL)
    for label, data in ((LABEL_SERVER, server), (LABEL_WORKER, worker),
                        (LABEL_WATCHDOG, watchdog)):
        path = AGENTS_DIR / ("%s.plist" % label)
        with open(path, "wb") as f:
            plistlib.dump(data, f)
        print("wrote %s" % path)


def _launchctl(*args) -> int:
    proc = subprocess.run(["launchctl", *args], capture_output=True, text=True)
    if proc.returncode != 0 and proc.stderr.strip():
        print("  launchctl %s: %s" % (" ".join(args[:2]), proc.stderr.strip()))
    return proc.returncode


def _bootstrap() -> None:
    uid = os.getuid()
    domain = "gui/%d" % uid
    for label in (LABEL_SERVER, LABEL_WORKER, LABEL_WATCHDOG):
        path = AGENTS_DIR / ("%s.plist" % label)
        _launchctl("bootout", "%s/%s" % (domain, label))  # ok if not loaded
        rc = _launchctl("bootstrap", domain, str(path))
        print("%s: %s" % (label, "started" if rc == 0 else "FAILED (rc %d)" % rc))


def _warn_if_no_secret() -> None:
    """The api-tier curation calls need ANTHROPIC_API_KEY; the worker loads it
    from ~/.config/signal/worker.env (chmod 600, never committed)."""
    env_file = pathlib.Path(os.path.expanduser("~/.config/signal/worker.env"))
    if not env_file.exists():
        print("WARN: %s missing — the worker has no ANTHROPIC_API_KEY, so "
              "api-tier curation will fail auth. Create it with a single line:\n"
              "      ANTHROPIC_API_KEY=sk-ant-...   (then chmod 600)" % env_file)


def install(cfg, start: bool = True) -> int:
    _copy_runtime(cfg)
    _write_scripts()
    _write_plists()
    _warn_if_no_secret()
    if start:
        _bootstrap()
        print()
        print("check:  launchctl print gui/%d/%s | head -20"
              % (os.getuid(), LABEL_WORKER))
        print("logs:   tail -f %s/worker.err.log" % LOGS_DIR)
        print("server: curl -s http://127.0.0.1:%d/healthz"
              % int(cfg.server.get("port", 8765)))
    else:
        print("plists written; start with: python3 -m signalpipe install")
    return 0


def sync(cfg, restart: bool = False) -> int:
    _copy_runtime(cfg)
    _write_scripts()
    if restart:
        uid = os.getuid()
        for label in (LABEL_SERVER, LABEL_WORKER, LABEL_WATCHDOG):
            _launchctl("kickstart", "-k", "gui/%d/%s" % (uid, label))
            print("restarted %s" % label)
    else:
        print("runtime synced; restart agents with: "
              "python3 -m signalpipe sync --restart")
    return 0
