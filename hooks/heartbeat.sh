#!/usr/bin/env bash
# Activity hook (UserPromptSubmit / PostToolUse / Stop): bump this session's
# last_seen in the peer ledger so its entry never drifts stale while active.
# Throttled to once per LEDGER_HEARTBEAT_EVERY seconds (default 60) per
# SESSION. Exit 0 unconditionally — a dead ledger must never disturb a session.
#
# The throttle is keyed on the session_id in the hook payload. It must not be
# keyed on $PPID: hooks are spawned under a fresh parent every invocation, so a
# PPID key is unique per call, the throttle never engages, and each call leaves
# another state file behind. Keeping the key in the payload also means the
# throttle survives anything that re-parents the hook.
#
# State lives beside the roster counters under roster-state/, so the 7-day
# prune in hook_roster reaps it and nothing accumulates.

PAYLOAD=$(cat)
LEDGER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

beat() {
    printf '%s' "$PAYLOAD" \
        | timeout 5 python3 "$LEDGER_DIR/ledger_mcp.py" hook-heartbeat >/dev/null 2>&1 \
        || true
    exit 0
}

EVERY="${LEDGER_HEARTBEAT_EVERY:-60}"
case "$EVERY" in ''|*[!0-9]*) EVERY=60 ;; esac

# Same extraction as hooks/roster-inject.sh, and the same key, so both hooks
# agree on what identifies a session. Parameter expansion only: no subprocess.
SID=""
case "$PAYLOAD" in
    *'"session_id"'*)
        SID=${PAYLOAD#*'"session_id"'}
        SID=${SID#*:}
        SID=${SID#*'"'}
        SID=${SID%%'"'*}
        ;;
esac
[ -n "$SID" ] || SID="$CLAUDE_CODE_SESSION_ID"
# No stable key means no safe throttle; heartbeat rather than drop it.
[ -n "$SID" ] || beat
case "$SID" in */*|.|..) beat ;; esac

DB="${CLAUDE_LEDGER_DB:-$HOME/.claude-ledger/ledger.db}"
STATE_DIR="$(dirname "$DB")/roster-state"
mkdir -p "$STATE_DIR" 2>/dev/null || beat
STATE="$STATE_DIR/$SID.hb"

# Timestamp stored in the file, read with a builtin: the throttled path -- the
# common one, on every tool call -- spawns nothing at all.
printf -v NOW '%(%s)T' -1 2>/dev/null || NOW=$(date +%s)
LAST=0
[ -r "$STATE" ] && read -r LAST < "$STATE" 2>/dev/null
case "$LAST" in ''|*[!0-9]*) LAST=0 ;; esac

[ $((NOW - LAST)) -lt "$EVERY" ] && exit 0

printf '%s\n' "$NOW" > "$STATE" 2>/dev/null || true
beat
