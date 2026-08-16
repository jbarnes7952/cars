---
name: deregister
description: Remove this session's entry from the peer ledger directory.
argument-hint: "[session name, if not already known]"
disable-model-invocation: true
---

Remove this session's entry from the `ledger` MCP directory.

1. Resolve the session name the same way `/register` does: `$ARGUMENTS` if
   given, else `$CLAUDE_LEDGER_NAME`, else a name established in this
   conversation, else ask the user.
2. Call the ledger server's `deregister` tool (`mcp__ledger__deregister`, or
   `mcp__plugin_cars_ledger__deregister` when installed as the cars plugin)
   with that `session_name`. It is idempotent — deregistering an unknown name
   succeeds silently.
3. Confirm in one sentence.

Note: forgetting to deregister is harmless — entries go stale after 10 minutes
and are evicted after 24 hours automatically.
