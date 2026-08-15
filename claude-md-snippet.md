# Peer Session Ledger — paste this block into your project or user CLAUDE.md

## Peer session registry (ledger)

This machine runs a session directory ("DNS for sessions") as the `ledger` MCP
server. It tells you **who** to message; native `SendMessage` does the messaging.

1. **First turn — verify your registration.** A SessionStart hook registered
   this session, possibly under a *derived* name that does not match your real
   messaging name. Compare the name peers see for you (your `--name` /
   ListAgents identity) against `mcp__ledger__list_agents_detailed`. If the
   hook registered a derived name (register event `name_source: "derived"`)
   that differs, call `register` with your correct name and `deregister` the
   derived one.
2. **Declare yourself early.** As soon as this session's purpose is clear
   (usually the first user prompt), call `update_registration` with a real
   `role`, `capabilities`, and `query_me_when` so peers can route to you.
3. **Keep it current.** Call `update_registration` whenever focus meaningfully
   shifts (new project, new major task). The tmux window label updates as a
   side effect of setting `project`. Never use `/rename` — the session name is
   a stable address; meaning lives in the ledger.
4. **Route before you message.** Before messaging a peer about a topic, call
   `find_agents` with the topic; address `SendMessage` using the returned
   `session_name`. If delivery fails or the entry is stale, fall back to
   native `ListAgents`.
5. **Never use the ledger as a message channel.** No "leave a note in the
   ledger" patterns — it is a directory, not transport. If the ledger is down,
   proceed normally with native `ListAgents`/`SendMessage`.
