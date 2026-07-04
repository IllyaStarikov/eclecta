"""Downtime gate for fully-local operation.

The pipeline runs its local Ollama models (curation + digest) ONLY when the Mac
is genuinely idle, so background inference never competes with the user's work.
`is_open(cfg, need_gb)` is the single gate the worker consults before a heavy
local stage; it is True only when ALL hold:

  - not manually paused      (`signal pause 2h` writes downtime.json)
  - on AC power              (pmset -g ac)
  - user idle >= idle_min    (ioreg HIDIdleTime — keyboard/mouse)
  - not thermally throttled  (pmset -g therm; absent == healthy on Apple Silicon)
  - enough free RAM          (vm_stat, real 16 KB pages) + no swap pressure
  - no digest mid-flight      (digest.lock — keeps a 14B curate off a 47B digest)
  - Ollama reachable         (GET /api/tags)

Every check fails SAFE: anything we cannot determine reads as "closed" (the one
exception is thermal, which fails open because a healthy Apple-Silicon box simply
omits the warning line). All probes use absolute binary paths — the user's `ps`
is shadowed by the `procs` rust tool, and `sysctl` lives in /usr/sbin.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from typing import List, Optional, Tuple

from .config import STATE_DIR

PAUSE_FILE = STATE_DIR / "downtime.json"
DIGEST_LOCK = STATE_DIR / "digest.lock"
DIGEST_LOCK_MAX_AGE_SEC = 1800  # a stale lock (crash mid-digest) is ignored after 30m

PMSET = "/usr/bin/pmset"
IOREG = "/usr/sbin/ioreg"
VM_STAT = "/usr/bin/vm_stat"
SYSCTL = "/usr/sbin/sysctl"


# ---- low-level probes --------------------------------------------------------

def _run(argv: List[str], timeout: int = 5) -> str:
    """Run a read-only probe; return stdout (empty string on any failure)."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return proc.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


def on_ac() -> bool:
    """True when an AC adapter is attached. Fails SAFE (battery) on no output."""
    out = _run([PMSET, "-g", "ac"])
    if not out:
        return False
    return "No adapter" not in out


def idle_seconds() -> float:
    """Seconds since the last keyboard/mouse event (HIDIdleTime, in ns). Reading
    ioreg does NOT reset it. Fails SAFE (0.0 == 'just active') on no output."""
    out = _run([IOREG, "-c", "IOHIDSystem"])
    for line in out.splitlines():
        if "HIDIdleTime" in line:
            try:
                ns = int(line.split("=")[-1].strip())
                return ns / 1_000_000_000.0
            except (ValueError, IndexError):
                return 0.0
    return 0.0


def thermal_ok() -> bool:
    """True when not thermally throttled. Apple Silicon has no thermal sysctls and
    OMITS the warning line when healthy, so an absent/empty signal fails OPEN."""
    out = _run([PMSET, "-g", "therm"])
    if not out:
        return True
    for line in out.splitlines():
        if "CPU_Speed_Limit" in line:
            try:
                if int(line.split("=")[-1].strip().rstrip("%")) < 100:
                    return False
            except (ValueError, IndexError):
                return False
        if "warning level" in line.lower() and "No " not in line:
            return False
    return True


def mem_available_gb() -> float:
    """Reclaimable RAM in GB (free + inactive + speculative pages). Parses the
    REAL page size from vm_stat line 1 (16384 on Apple Silicon, not 4096). Fails
    SAFE (0.0) on no output."""
    out = _run([VM_STAT])
    if not out:
        return 0.0
    lines = out.splitlines()
    page = 4096
    if "page size of" in lines[0]:
        try:
            page = int(lines[0].split("page size of")[1].split("bytes")[0].strip())
        except (ValueError, IndexError):
            page = 4096
    pages = 0
    for line in lines:
        for label in ("Pages free:", "Pages inactive:", "Pages speculative:"):
            if line.startswith(label):
                try:
                    pages += int(line.split(":")[1].strip().rstrip("."))
                except (ValueError, IndexError):
                    pass
    return pages * page / 1_000_000_000.0


def swap_used_gb() -> float:
    """Swap currently in use, in GB. 0.0 if undeterminable (does not block)."""
    out = _run([SYSCTL, "-n", "vm.swapusage"])  # 'total = X  used = Y  free = Z'
    try:
        parts = out.split("used =")[1].split()[0]  # e.g. '1024.50M'
        val = float(parts[:-1])
        unit = parts[-1].upper()
        return val / 1024.0 if unit == "M" else (val if unit == "G" else val / 1024.0 / 1024.0)
    except (ValueError, IndexError):
        return 0.0


# ---- ollama ------------------------------------------------------------------

def _base_url(cfg) -> str:
    return (cfg.backend.get("local") or {}).get(
        "base_url", "http://127.0.0.1:11434").rstrip("/")


def ollama_up(cfg) -> bool:
    try:
        req = urllib.request.Request(_base_url(cfg) + "/api/tags")
        with urllib.request.urlopen(req, timeout=4) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def ollama_unload(cfg) -> List[str]:
    """Evict every resident model immediately (keep_alive=0) so its 9-47 GB frees
    the instant the user reclaims the machine. Returns the models unloaded."""
    base = _base_url(cfg)
    unloaded: List[str] = []
    try:
        req = urllib.request.Request(base + "/api/ps")
        with urllib.request.urlopen(req, timeout=4) as resp:
            running = json.loads(resp.read()).get("models") or []
    except (urllib.error.URLError, OSError, ValueError):
        running = []
    for m in running:
        name = m.get("name") or m.get("model")
        if not name:
            continue
        try:
            body = json.dumps({"model": name, "keep_alive": 0}).encode("utf-8")
            req = urllib.request.Request(
                base + "/api/generate", data=body,
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
            unloaded.append(name)
        except (urllib.error.URLError, OSError, ValueError):
            pass
    return unloaded


# ---- digest lock (avoid a 14B curate landing on a 47B digest) ----------------

def set_digest_lock() -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        DIGEST_LOCK.write_text(str(int(time.time())) + "\n")
    except OSError:
        pass


def clear_digest_lock() -> None:
    try:
        DIGEST_LOCK.unlink()
    except OSError:
        pass


def digest_in_flight() -> bool:
    try:
        age = time.time() - DIGEST_LOCK.stat().st_mtime
    except OSError:
        return False
    return age < DIGEST_LOCK_MAX_AGE_SEC


# ---- manual pause ------------------------------------------------------------

def paused_until() -> float:
    try:
        return float(json.loads(PAUSE_FILE.read_text()).get("paused_until", 0))
    except (OSError, ValueError):
        return 0.0


def pause(seconds: int, reason: str = "manual") -> float:
    until = time.time() + max(0, int(seconds))
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        PAUSE_FILE.write_text(json.dumps({
            "paused_until": until, "reason": reason,
            "set_at": time.time(),
        }) + "\n")
    except OSError as e:
        print("could not write pause file: %s" % e)
        return 1
    return until


def resume() -> None:
    try:
        PAUSE_FILE.unlink()
    except OSError:
        pass


# ---- the gate ----------------------------------------------------------------

def is_open(cfg, need_gb: float = 0.0) -> Tuple[bool, str]:
    """Whether a heavy local stage may run right now. Returns (open?, reason)."""
    dt = cfg.data.get("downtime", {})
    if not dt.get("enabled", True):
        return True, "gating disabled"

    pu = paused_until()
    if pu > time.time():
        return False, "paused (%dm left)" % round((pu - time.time()) / 60)

    if dt.get("require_ac", True) and not on_ac():
        return False, "on battery"

    idle_need = float(dt.get("idle_min", 5)) * 60.0
    idle = idle_seconds()
    if idle < idle_need:
        return False, "user active (idle %ds < %ds)" % (int(idle), int(idle_need))

    if dt.get("thermal_guard", True) and not thermal_ok():
        return False, "thermal throttling"

    if digest_in_flight():
        return False, "digest in flight"

    if need_gb:
        avail = mem_available_gb()
        if avail < float(need_gb):
            return False, "low RAM (%.0f < %.0f GB free)" % (avail, float(need_gb))

    if dt.get("swap_thrash_guard", True):
        if swap_used_gb() > float(dt.get("swap_used_max_gb", 6)):
            return False, "swap pressure (%.1f GB)" % swap_used_gb()

    if dt.get("ollama_preflight", True) and not ollama_up(cfg):
        return False, "ollama unreachable"

    return True, "open"


def parse_duration(s: Optional[str], default_sec: int = 7200) -> int:
    """'2h' / '30m' / '90s' / '45' (bare == minutes) -> seconds."""
    if not s:
        return default_sec
    s = s.strip().lower()
    try:
        if s.endswith("h"):
            return int(float(s[:-1]) * 3600)
        if s.endswith("m"):
            return int(float(s[:-1]) * 60)
        if s.endswith("s"):
            return int(float(s[:-1]))
        return int(float(s) * 60)  # bare number == minutes
    except ValueError:
        return default_sec


def status(cfg) -> str:
    """Human-readable gate state — every check, for `signal downtime`."""
    ok, reason = is_open(cfg)
    pu = paused_until()
    lines = ["downtime gate: %s%s" % (
        "OPEN — local stages may run" if ok else "CLOSED",
        "" if ok else "  (%s)" % reason)]
    if pu > time.time():
        lines.append("  paused: %d min remaining" % round((pu - time.time()) / 60))
    lines.append("  on AC:        %s" % on_ac())
    lines.append("  user idle:    %ds" % int(idle_seconds()))
    lines.append("  thermal ok:   %s" % thermal_ok())
    lines.append("  RAM free:     %.0f GB" % mem_available_gb())
    lines.append("  swap used:    %.1f GB" % swap_used_gb())
    lines.append("  ollama up:    %s" % ollama_up(cfg))
    lines.append("  digest lock:  %s" % digest_in_flight())
    return "\n".join(lines)
