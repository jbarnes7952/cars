#!/usr/bin/env bash
# launch-session.sh [name] [-- claude args...]
#
# Mints a session name, exports it for the SessionStart/SessionEnd hooks,
# and launches `claude --name <name>`. This is the recommended launch path:
# it guarantees the ledger entry matches the name peers see in ListAgents.

NAME=""
if [ $# -gt 0 ] && [ "$1" != "--" ]; then
    NAME="$1"
    shift
fi
[ "${1:-}" = "--" ] && shift

if [ -z "$NAME" ]; then
    ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
    PROJECT=$(basename "$ROOT")
    RAND=$(LC_ALL=C tr -dc 'a-z0-9' </dev/urandom | head -c 4)
    NAME="${PROJECT}-${RAND}"
fi

export CLAUDE_LEDGER_NAME="$NAME"
exec claude --name "$NAME" "$@"
