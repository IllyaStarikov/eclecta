#!/bin/sh
HB="$HOME/.local/state/signal/heartbeat"
STALE=900
LABEL="io.starikov.signal.worker"
UID_N=$(id -u)
ts() { date '+%Y-%m-%dT%H:%M:%S'; }
restart() {
    echo "$(ts) $1 - kickstarting $LABEL"
    launchctl kickstart -k "gui/$UID_N/$LABEL"
    osascript -e "display notification \"$1 - restarted worker\" with title \"signal watchdog\"" 2>/dev/null || true
}
if [ ! -f "$HB" ]; then
    restart "heartbeat missing"
    exit 0
fi
now=$(date +%s)
mtime=$(stat -f %m "$HB" 2>/dev/null || echo 0)
age=$(( now - mtime ))
if [ "$age" -gt "$STALE" ]; then
    restart "heartbeat stale ${age}s"
fi
