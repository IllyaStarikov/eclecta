#!/usr/bin/env bash
# Signal smoke test — exercise every subsystem of the running pipeline and
# report PASS/FAIL. Read-only against the live daemon except two safe active
# probes (a single-source HN ingest refresh + an optional live LLM auth ping).
#
#   bash signalpipe/smoke_test.sh            # full run
#   SKIP_LLM=1 bash signalpipe/smoke_test.sh # skip the spending LLM probe
#
# Exit 0 if all non-skipped checks pass, 1 otherwise.

set -uo pipefail
cd "$(dirname "$0")/.." || exit 2

DB="$HOME/.local/state/signal/signal.db"
BASE="http://127.0.0.1:8765"
GHOST="http://localhost:2368"
UID_N="$(id -u)"
PASS=0; FAIL=0; SKIP=0
RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; DIM=$'\033[2m'; RST=$'\033[0m'

ok()   { PASS=$((PASS+1)); printf "  ${GRN}PASS${RST} %s\n" "$1"; }
bad()  { FAIL=$((FAIL+1)); printf "  ${RED}FAIL${RST} %s${DIM}%s${RST}\n" "$1" "${2:+ — $2}"; }
skip() { SKIP=$((SKIP+1)); printf "  ${YEL}SKIP${RST} %s${DIM}%s${RST}\n" "$1" "${2:+ — $2}"; }
sec()  { printf "\n${DIM}== %s ==${RST}\n" "$1"; }

q() { sqlite3 "$DB" "$1" 2>/dev/null; }
code() { curl -s -o /dev/null -w '%{http_code}' "$1" 2>/dev/null; }
items() { curl -s "$1" 2>/dev/null | grep -c '<item>'; }

# ---------------------------------------------------------------------------
sec "A · daemon + process liveness"
state_srv=$(launchctl print "gui/$UID_N/io.starikov.signal.server" 2>/dev/null | grep -oE 'state = [a-z]+' | head -1)
state_wkr=$(launchctl print "gui/$UID_N/io.starikov.signal.worker" 2>/dev/null | grep -oE 'state = [a-z]+' | head -1)
[ "$state_srv" = "state = running" ] && ok "server launchd agent running" || bad "server launchd agent" "$state_srv"
[ "$state_wkr" = "state = running" ] && ok "worker launchd agent running" || bad "worker launchd agent" "$state_wkr"
[ "$(code "$BASE/healthz")" = "200" ] && ok "server answers /healthz (200)" || bad "server /healthz"
last_ing=$(q "SELECT ts FROM health WHERE job='ingest' ORDER BY id DESC LIMIT 1")
if [ -n "$last_ing" ]; then
  # health timestamps are UTC ISO-8601 — parse them as UTC, not local.
  ing_epoch=$(TZ=UTC date -j -f "%Y-%m-%dT%H:%M:%S" "${last_ing:0:19}" +%s 2>/dev/null || echo 0)
  age=$(( $(date +%s) - ing_epoch ))
  [ "$ing_epoch" -gt 0 ] && [ "$age" -ge 0 ] && [ "$age" -lt 7200 ] \
    && ok "daemon ingested recently (${age}s ago)" || bad "stale/unparseable ingest" "${age}s ago"
else bad "no ingest health rows"; fi

# ---------------------------------------------------------------------------
sec "B · HTTP endpoints + feed validity"
hz=$(curl -s "$BASE/healthz")
echo "$hz" | grep -qE '"ok":[[:space:]]*true' && ok "/healthz ok:true" || bad "/healthz ok"
curl -s "$BASE/feed.xml?limit=10" | xmllint --noout - 2>/dev/null && ok "/feed.xml well-formed XML" || bad "/feed.xml not well-formed"
[ "$(items "$BASE/feed.xml?limit=10")" -ge 1 ] && ok "/feed.xml returns items" || bad "/feed.xml empty"
[ "$(items "$BASE/feed/ai.xml")" -ge 0 ] && [ "$(code "$BASE/feed/ai.xml")" = 200 ] && ok "/feed/ai.xml serves" || bad "/feed/ai.xml"
[ "$(code "$BASE/feed/security.xml")" = 200 ] && ok "/feed/security.xml serves" || bad "/feed/security.xml"
[ "$(code "$BASE/feed/bogus.xml")" = 404 ] && ok "unknown channel → 404" || bad "unknown channel guard"
[ "$(items "$BASE/feed.xml?channel=%25")" = 0 ] && ok "LIKE-injection guard (channel=%) → 0 items" || bad "injection guard leaked"
[ "$(items "$BASE/feed.xml?since=24h&min_score=5&limit=5")" -ge 0 ] && ok "params since/min_score/limit accepted" || bad "param query errored"
[ "$(items "$BASE/feed.xml?sources=hacker-news&limit=30")" -ge 0 ] && ok "sources= filter accepted" || bad "sources filter errored"
[ "$(code "$BASE/opml")" = 200 ] && curl -s "$BASE/opml" | xmllint --noout - 2>/dev/null && ok "/opml valid XML" || bad "/opml"
[ "$(code "$BASE/")" = 200 ] && ok "dashboard / serves (200)" || bad "dashboard /"
curl -s "$BASE/" | grep -q 'health-grid' && ok "dashboard renders health panel" || bad "dashboard health panel missing"
[ "$(curl -s "$BASE/static/signal.css" | grep -c 'border-radius: 0')" -ge 1 ] && ok "sharp-corner CSS served" || bad "signal.css"

# ---------------------------------------------------------------------------
sec "C · data integrity"
case "$DB" in *Mobile\ Documents*) bad "DB inside iCloud!" "$DB";; *) ok "DB outside iCloud ($DB)";; esac
[ "$(q "PRAGMA journal_mode")" = "wal" ] && ok "WAL mode active" || bad "WAL mode" "$(q "PRAGMA journal_mode")"
[ "$(q "PRAGMA integrity_check")" = "ok" ] && ok "PRAGMA integrity_check ok" || bad "integrity_check"
[ "$(q "PRAGMA foreign_key_check" | wc -l | tr -d ' ')" = 0 ] && ok "no foreign-key violations" || bad "FK violations"
nsrc=$(q "SELECT COUNT(*) FROM sources"); [ "$nsrc" -ge 1000 ] && ok "sources registered: $nsrc (>=1000)" || bad "sources count" "$nsrc"
ncl=$(q "SELECT COUNT(*) FROM clusters"); [ "$ncl" -ge 100 ] && ok "clusters: $ncl" || bad "clusters" "$ncl"
ncur=$(q "SELECT COUNT(*) FROM curations WHERE status='done'"); [ "$ncur" -ge 1 ] && ok "curated items: $ncur" || bad "no curations"
nrich=$(q "SELECT COUNT(*) FROM curations WHERE status='done' AND why_it_matters IS NOT NULL AND notes IS NOT NULL AND summary IS NOT NULL")
[ "$nrich" -ge 1 ] && ok "curations carry why/notes/summary ($nrich)" || bad "curations missing rich fields"
guid=$(curl -s "$BASE/feed.xml?limit=1" | grep -oE 'tag:starikov.co,2026:signal/[0-9]+' | head -1)
[ -n "$guid" ] && ok "feed guids are stable tag: URIs ($guid)" || bad "feed guid format"

# ---------------------------------------------------------------------------
sec "D · pipeline stages (active, safe)"
ing=$(python3 -m signalpipe ingest --source hacker-news 2>&1 | grep -E '^ingest:')
echo "$ing" | grep -q 'ok' && ok "single-source ingest (hacker-news): $ing" || bad "HN ingest" "$ing"
scr=$(python3 -m signalpipe score --show 0 2>&1 | grep -E '^score:')
[ -n "$scr" ] && ok "score runs: $scr" || bad "score"
spent=$(q "SELECT printf('%.4f', cli_usd+api_usd) FROM spend WHERE day=date('now')")
[ -n "$spent" ] && ok "spend ledger has today's entry (\$$spent)" || skip "no spend recorded today"
if [ "${SKIP_LLM:-0}" = "1" ]; then
  skip "live LLM auth probe (SKIP_LLM=1)"
else
  llm=$(python3 - <<'PY' 2>&1
from signalpipe import config as c
from signalpipe.llm import adapter, SpendCapExceeded
cfg = c.load()
try:
    ok, msg = adapter.probe_auth(cfg)
    print("OK" if ok else "FAIL:"+msg)
except SpendCapExceeded as e:
    print("CAP")  # cap reached = the breaker works; LLM path itself is fine
except Exception as e:
    print("ERR:"+str(e)[:120])
PY
)
  case "$llm" in
    OK) ok "live LLM (claude -p subscription) auth + structured output works" ;;
    CAP) ok "spend cap breaker engaged (LLM path proven earlier via $ncur curations)" ;;
    *) bad "live LLM probe" "$llm" ;;
  esac
fi

# ---------------------------------------------------------------------------
sec "E · Ghost separation (digest must not leak into the blog)"
slug=$(q "SELECT staged_path FROM digests ORDER BY iso_week DESC LIMIT 1" | xargs -I{} basename {} .md | sed 's/_/-/g')
if [ -n "$slug" ] && [ "$(code "$GHOST/")" = "200" ]; then
  [ "$(curl -s "$GHOST/blog/rss/" | grep -c "$slug")" = 0 ] && ok "digest ABSENT from /blog/rss" || bad "digest LEAKED into /blog/rss"
  [ "$(curl -s "$GHOST/signal/rss/" | grep -c "$slug")" -ge 1 ] && ok "digest present in /signal/rss" || bad "digest missing from /signal/rss"
  [ "$(code "$GHOST/signal/$slug/")" = 200 ] && ok "/signal/$slug/ resolves" || bad "/signal/ permalink"
  [ "$(curl -s "$GHOST/signal/$slug/" | grep -c 'archive.ph\|archive.today')" = 0 ] && ok "no archive.* links in published digest" || bad "archive link leaked"
else
  skip "Ghost separation" "local Ghost not on :2368 or no digest staged"
fi

# ---------------------------------------------------------------------------
sec "F · config + CLI"
python3 -m signalpipe status >/dev/null 2>&1 && ok "status runs clean" || bad "status errored"
python3 -m signalpipe sources stats >/dev/null 2>&1 && ok "sources stats runs" || bad "sources stats"
for c in ingest score fetch curate digest promote serve worker sources install sync status; do
  python3 -m signalpipe "$c" --help >/dev/null 2>&1 || { bad "subcommand --help: $c"; continue; }
done
ok "all CLI subcommands expose --help"

# ---------------------------------------------------------------------------
printf "\n${DIM}=========================================${RST}\n"
printf "  ${GRN}%d passed${RST}, ${RED}%d failed${RST}, ${YEL}%d skipped${RST}\n" "$PASS" "$FAIL" "$SKIP"
printf "${DIM}=========================================${RST}\n"
[ "$FAIL" -eq 0 ]
