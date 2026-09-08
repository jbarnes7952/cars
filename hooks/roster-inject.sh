#!/usr/bin/env bash
# UserPromptSubmit / PostToolUse hook: every LEDGER_ROSTER_EVERY prompts or
# LEDGER_ROSTER_TOOLS_EVERY tool calls (0 disables a channel), inject the live
# peer roster into context. stdout carries the hook JSON payload — do not
# redirect it. Exit 0 unconditionally.
#
# $1 selects the cadence channel: "prompt" (UserPromptSubmit) or "tool"
# (PostToolUse). It is passed on the command line rather than read from the
# payload so the off-cycle path needs no JSON parsing at all.
#
# The cadence is gated here, in shell, so python3 only starts on a firing
# tick. This hook runs on every tool call of every session, and an off-cycle
# start cost ~60ms just to decide "not yet". Gating in shell makes the common
# path a couple of file reads. On the firing tick we hand the counting result
# to python via LEDGER_ROSTER_GATED so it does not count a second time.
#
# Every failure path falls through to invoking python, i.e. to the previous
# behaviour: this may cost time, but it must never drop a roster injection.

LEDGER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PAYLOAD=$(cat)

# Older wiring invoked this script with no argument; fall back to the event
# name in the payload so an un-updated settings.json keeps its cadence.
CHANNEL="$1"
if [ -z "$CHANNEL" ]; then
    case "$PAYLOAD" in
        *'"hook_event_name"'*'"PostToolUse"'*) CHANNEL=tool ;;
        *) CHANNEL=prompt ;;
    esac
fi

fire() {
    printf '%s' "$PAYLOAD" | LEDGER_ROSTER_GATED=1 \
        timeout 5 python3 "$LEDGER_DIR/ledger_mcp.py" hook-roster 2>/dev/null || true
    exit 0
}

if [ "$CHANNEL" = "tool" ]; then
    EVERY="${LEDGER_ROSTER_TOOLS_EVERY:-25}"
else
    EVERY="${LEDGER_ROSTER_EVERY:-5}"
fi
# Non-numeric config is treated as "off", matching _env_int's fallback.
case "$EVERY" in ''|*[!0-9]*) exit 0 ;; esac
[ "$EVERY" -gt 0 ] || exit 0

# A stable per-session key. $PPID is NOT one: hooks are spawned under a fresh
# parent each time, so keying on it silently disables the gate and leaks a
# state file per invocation.
# The payload's session_id is authoritative and is what python keys its own
# state file on; $CLAUDE_CODE_SESSION_ID is only a fallback, since it reflects
# whoever spawned the hook. Extracted with parameter expansion so the
# off-cycle path spawns no processes at all.
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
# No stable key means no safe gate; behave exactly as before.
[ -n "$SID" ] || fire
case "$SID" in */*|.|..) fire ;; esac

DB="${CLAUDE_LEDGER_DB:-$HOME/.claude-ledger/ledger.db}"
STATE_DIR="$(dirname "$DB")/roster-state"
mkdir -p "$STATE_DIR" 2>/dev/null || fire

COUNT_FILE="$STATE_DIR/$SID.$CHANNEL"
OTHER=$([ "$CHANNEL" = "tool" ] && echo prompt || echo tool)

# Absent counter => first event of this session on this channel => fire.
N=$(cat "$COUNT_FILE" 2>/dev/null) || N=""
# Zero, never remove: an absent counter means "first event on this channel"
# and would fire, so removing the other channel's file would grant it an
# extra injection instead of resetting it.
case "$N" in ''|*[!0-9]*) echo 0 > "$COUNT_FILE" 2>/dev/null
                          echo 0 > "$STATE_DIR/$SID.$OTHER" 2>/dev/null
                          fire ;;
esac

N=$((N + 1))
if [ "$N" -ge "$EVERY" ]; then
    # Firing resets both channels so the two never double-inject.
    echo 0 > "$COUNT_FILE" 2>/dev/null
    echo 0 > "$STATE_DIR/$SID.$OTHER" 2>/dev/null
    fire
fi
echo "$N" > "$COUNT_FILE" 2>/dev/null
exit 0
