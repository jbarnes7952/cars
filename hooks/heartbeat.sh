#!/usr/bin/env bash
# Activity hook (UserPromptSubmit / PostToolUse / Stop): bump this session's
# last_seen in the peer ledger so its entry never drifts stale while active.
# Throttled to once per LEDGER_HEARTBEAT_EVERY seconds (default 60) per CLI
# process. Exit 0 unconditionally — a dead ledger must never disturb the session.

STATE="${TMPDIR:-/tmp}/claude-ledger-hb-$PPID"
NOW=$(date +%s)
LAST=$(stat -c %Y "$STATE" 2>/dev/null || echo 0)
if [ $((NOW - LAST)) -lt "${LEDGER_HEARTBEAT_EVERY:-60}" ]; then
    exit 0
fi
touch "$STATE" 2>/dev/null || true

LEDGER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
timeout 5 python3 "$LEDGER_DIR/ledger_mcp.py" hook-heartbeat >/dev/null 2>&1 || true
exit 0
