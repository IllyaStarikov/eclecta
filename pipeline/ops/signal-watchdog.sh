#!/bin/sh
# signal watchdog — runs every 5 min from launchd (StartInterval), stateless.
# v2: worker heartbeat (v1) + server healthz probe + copy-truncate log
# rotation + publish-freshness notice. Restarts are per-service kickstarts;
# notifications are best-effort and never fail the script.
HB="$HOME/.local/state/signal/heartbeat"
STALE=900
WORKER="io.starikov.signal.worker"
SERVER="io.starikov.signal.server"
LOGDIR="$HOME/Library/Logs/signal"
SITE_REPO="$HOME/Library/Mobile Documents/com~apple~CloudDocs/Documents/development/eclecta"
MAX_LOG=52428800   # 50 MB
PUBLISH_STALE=21600 # 6 h
UID_N=$(id -u)
ts() { date '+%Y-%m-%dT%H:%M:%S'; }
notify() {
    osascript -e "display notification \"$1\" with title \"signal watchdog\"" 2>/dev/null || true
}
restart() { # $1 message, $2 label
    echo "$(ts) $1 - kickstarting $2"
    launchctl kickstart -k "gui/$UID_N/$2"
    notify "$1 - restarted ${2##*.}"
}

# 1. worker heartbeat (the worker touches it every few minutes while alive)
if [ ! -f "$HB" ]; then
    restart "heartbeat missing" "$WORKER"
else
    now=$(date +%s)
    mtime=$(stat -f %m "$HB" 2>/dev/null || echo 0)
    age=$(( now - mtime ))
    if [ "$age" -gt "$STALE" ]; then
        restart "heartbeat stale ${age}s" "$WORKER"
    fi
fi

# 2. server liveness (the dashboard/API on :8765)
if ! curl -fsS -m 5 http://127.0.0.1:8765/healthz >/dev/null 2>&1; then
    restart "server healthz failed" "$SERVER"
fi

# 3. log rotation, copy-truncate, keep 3 — newsyslog needs root; this doesn't
for log in "$LOGDIR"/*.log; do
    [ -f "$log" ] || continue
    size=$(stat -f %z "$log" 2>/dev/null || echo 0)
    if [ "$size" -gt "$MAX_LOG" ]; then
        [ -f "$log.2" ] && mv "$log.2" "$log.3"
        [ -f "$log.1" ] && mv "$log.1" "$log.2"
        cp "$log" "$log.1" && : > "$log"
        echo "$(ts) rotated $log (${size} bytes)"
    fi
done

# 4. publish freshness — notice only, and only while the site checkout is on
#    main (the publisher refuses other branches by design, so a stale wire on
#    a WIP branch is expected, not an incident)
if [ -d "$SITE_REPO/.git" ]; then
    branch=$(git -C "$SITE_REPO" branch --show-current 2>/dev/null)
    if [ "$branch" = "main" ] && [ -f "$SITE_REPO/src/data/stats.json" ]; then
        now=$(date +%s)
        pub=$(stat -f %m "$SITE_REPO/src/data/stats.json" 2>/dev/null || echo 0)
        page=$(( now - pub ))
        if [ "$page" -gt "$PUBLISH_STALE" ]; then
            echo "$(ts) publish stale $(( page / 3600 ))h (stats.json)"
            notify "publish stale $(( page / 3600 ))h - check worker/publish logs"
        fi
    fi
fi
