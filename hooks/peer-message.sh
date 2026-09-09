#!/usr/bin/env bash
# UserPromptSubmit hook: record that a cross-session message arrived here.
# Writes one `peer_message` event holding the two endpoints and a timestamp.
# Never the body, never its length, never a subject. Exit 0 unconditionally.
#
# Almost every prompt is an ordinary one, so this is gated in shell: the cheap
# substring test below is a necessary condition, and python starts only when it
# passes. The authoritative check -- the wrapper must sit at offset 0, and the
# sender must resolve in the directory -- is done in python, which can parse
# the payload properly. This test is a filter, never the decision.

PAYLOAD=$(cat)

case "$PAYLOAD" in
    *'<cross-session-message'*) ;;
    *) exit 0 ;;
esac

LEDGER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
printf '%s' "$PAYLOAD" \
    | timeout 5 python3 "$LEDGER_DIR/ledger_mcp.py" hook-peer-message >/dev/null 2>&1 \
    || true
exit 0
