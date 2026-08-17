# Peer Session Ledger — paste this block into your user or project CLAUDE.md

## Peer session ledger

This machine runs a session directory (the `ledger` MCP server). It tells you
**who** to message; native `SendMessage` does the messaging.

1. **Register early.** Once this session's purpose is clear (usually the first
   real prompt): if the user is actively present, ask them once — briefly —
   whether to add this session to the registry, proposing the `role` and a
   `query_me_when` written as a **trigger condition** ("message me
   when/before ...", naming specific files or operations — it is rendered
   verbatim as your tool description for peers); register on approval and
   never re-ask after a decline. If running unattended, register without
   asking. Pass `session_name` only if you actually know it (user-given or a
   rename notice in context — never guess one); otherwise omit it and you are
   registered under this session's self-derived transport address. Learn the
   real name later → re-register under it (the old entry is superseded).
2. **Keep it current.** Call `mcp__ledger__update_registration` when focus
   meaningfully shifts; the tmux label follows `project`. Never `/rename` a
   registered session — the name is its address.
3. **Route before messaging.** Check the injected roster, `peer_*` tools, or
   `find_agents` before messaging a peer about a topic; address `SendMessage`
   with the returned `session_name`. Ledger tools may be deferred (name-only)
   — load them via ToolSearch first; `/cars:register` is the manual fallback.
   If the ledger is down or an entry is stale, fall back to native
   `ListAgents`.
4. **Directory only.** Never use the ledger to leave notes or deliver
   messages — no "note in the ledger" patterns, ever.
