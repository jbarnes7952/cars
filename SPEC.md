# SPEC: Peer Session Ledger

An MCP server that acts as a discovery directory ("DNS") for dynamic Claude Code peer sessions. Sessions register their identity, role, and capabilities; other sessions query the ledger to decide **who** to message, then use Claude Code's native cross-session `SendMessage` to do the messaging. The ledger never transports messages.

## Design principles (do not violate)

1. **Directory only, never transport.** No message queuing, routing, or delivery. Native `ListAgents`/`SendMessage` is the transport layer. If a feature request implies delivery, it is out of scope.
2. **Ledger-as-DNS.** The Claude Code session name is a stable, opaque *address* assigned at launch and never changed mid-session. All meaning (role, capabilities, status, project) lives in the ledger and is mutable via tool calls. `/rename` is never used on registered sessions.
3. **Boring and always up.** Localhost only. SQLite only. No network dependencies, no external services, no auth. Must survive and function with nothing else on the machine running.
4. **One-way seam.** An append-only events table records every mutation for future consumers (a separate personal-knowledge system will eventually read it). The ledger never calls out to anything. Dependency direction: consumers read ledger; ledger reads nothing.
5. **Graceful degradation.** If the ledger is down or an entry is stale, sessions fall back to native `ListAgents`. Nothing in Claude Code's own operation may depend on the ledger.

## Deliverables

1. `ledger-mcp` — the MCP server (stdio transport), Python or TypeScript (implementer's choice; prefer whichever yields the smallest dependency footprint).
2. SQLite schema + migrations (a single `schema.sql` applied idempotently on startup is sufficient).
3. Hook scripts:
   - `hooks/session-start-register.sh` (SessionStart) — registers the session.
   - `hooks/session-end-deregister.sh` (SessionEnd) — deregisters.
   - `hooks/tmux-relabel.sh` (PostToolUse, matcher on the ledger's update tool) — relabels the tmux window/pane from the registered project.
4. `claude-md-snippet.md` — a block to paste into project/user CLAUDE.md instructing Claude how to participate in the registry (see Protocol section).
5. Example `settings.json` hook wiring and example `.mcp.json` / `claude mcp add` registration.
6. `launch-session.sh` — wrapper script that mints a session name, exports it, and launches `claude --name <name>`.
7. README covering install, wiring, and the fallback behavior.

## Storage

SQLite database at `~/.claude-ledger/ledger.db`. WAL mode. Two tables:

### `agents` (live state, one row per registered session)

| column | type | notes |
|---|---|---|
| session_name | TEXT PRIMARY KEY | the Claude Code messaging name (the address) |
| session_id | TEXT | Claude Code session UUID, for self-heal/audit |
| pid | INTEGER NULL | process id if known |
| cwd | TEXT | working directory at registration |
| project | TEXT NULL | inferred project name (drives tmux label) |
| role | TEXT | short role label, e.g. `schema-owner` |
| capabilities | TEXT | JSON array of strings |
| query_me_when | TEXT | free text: routing guidance for other agents |
| status | TEXT | free text: current focus, updated as work shifts |
| tmux_pane | TEXT NULL | `$TMUX_PANE` at registration, if in tmux |
| machine | TEXT | hostname |
| registered_at | TEXT | ISO 8601 UTC |
| last_seen | TEXT | ISO 8601 UTC, bumped on every write from that session |

### `events` (append-only, never updated or deleted)

| column | type |
|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT |
| ts | TEXT (ISO 8601 UTC) |
| session_name | TEXT |
| session_id | TEXT |
| event | TEXT — `register` \| `update` \| `heartbeat` \| `deregister` \| `evicted` |
| payload | TEXT — JSON snapshot of the fields written |

Every mutation of `agents` writes a corresponding `events` row in the same transaction. Heartbeat events may be sampled (write at most one heartbeat event per session per 5 minutes) to keep the table small; all other event types are always written.

## Staleness

- `last_seen` older than **10 minutes** ⇒ entry is *stale*. Query tools still return stale entries but flag them (`"stale": true`).
- `last_seen` older than **24 hours** ⇒ evict: delete from `agents`, write an `evicted` event. Eviction runs lazily on any tool call (no background daemon).
- Any tool call from a session (identified by `session_name` argument) bumps its `last_seen`.

## MCP tools

All tools return JSON. Keep descriptions short — these load into every session's context.

### `register`
Args: `session_name` (required), `session_id`, `cwd`, `role`, `capabilities` (array), `query_me_when`, `status`, `project`, `tmux_pane`, `pid`.
Upsert into `agents` (re-registration of an existing name replaces the row — supports session restart with the same name). Write `register` event. Returns the stored record.

### `update_registration`
Args: `session_name` (required), plus any subset of `role`, `capabilities`, `query_me_when`, `status`, `project`.
Partial update; untouched fields keep their values. Bumps `last_seen`. Write `update` event. Returns the updated record. Error if `session_name` is not registered (tell the caller to `register` first).

### `heartbeat`
Args: `session_name`. Bumps `last_seen` only. (Optional to wire; exists so a Stop hook can keep long-idle sessions fresh.)

### `find_agents`
Args: `query` (free text, required), `include_stale` (bool, default false).
Match against `role`, `capabilities`, `query_me_when`, `status`, `project` — case-insensitive substring/LIKE matching across those fields is sufficient; do **not** add embeddings or FTS in v1. Returns matching records ordered by freshness, each including `session_name` (the address to pass to `SendMessage`), `stale` flag, and all descriptive fields.

### `list_agents_detailed`
Args: `include_stale` (bool, default false). Returns all registered agents with full records.

### `deregister`
Args: `session_name`. Delete row, write `deregister` event. Idempotent (deregistering an unknown name succeeds silently).

## Hooks

### SessionStart → register
Reads hook JSON from stdin (`session_id`, `cwd`). Determines the session name in this priority order:
1. `$CLAUDE_LEDGER_NAME` env var (set by `launch-session.sh`).
2. `session_title` field from hook stdin, if present and non-empty. **Verify empirically during implementation whether `session_title` reflects `--name`; if it does not, document that and rely on (1) and (3).**
3. Fallback: derive `basename(cwd)-<first 4 chars of session_id>` and register that, flagging the record with `"name_source": "derived"` in the register event payload — a derived name may not match the ListAgents name, so the CLAUDE.md protocol (below) instructs Claude to correct it on first turn.

Registers with `role: "unassigned"`, `status: "starting"`, `project` inferred as `basename` of the git repo root if `cwd` is inside a git repo, else `basename(cwd)`. Captures `$TMUX_PANE` if set. Must exit 0 fast (<200ms) and exit 0 even if the ledger is unreachable — registration failure is never allowed to disturb session start.

### SessionEnd → deregister
Calls `deregister` with the same name resolution. Exit 0 unconditionally.

### PostToolUse (matcher: the ledger `update_registration` and `register` tool names, i.e. `mcp__ledger__register|mcp__ledger__update_registration`) → tmux relabel
- No-op unless `$TMUX` is set.
- Parse `project` from `tool_input` on stdin; if absent, no-op.
- `tmux rename-window -t "$TMUX_PANE" "<project>"` and `tmux set-option -w -t "$TMUX_PANE" automatic-rename off`.
- If multiple panes per window is detected as the user's layout (leave a config flag `LEDGER_TMUX_MODE=window|pane`, default `window`), `pane` mode uses `tmux select-pane -t "$TMUX_PANE" -T "<project>"` instead.
- Exit 0 unconditionally.

## Launch wrapper

`launch-session.sh [name] [-- claude args...]`:
- If no name given, generate `<project>-<short random>` from cwd.
- `export CLAUDE_LEDGER_NAME=<name>`, then `exec claude --name "<name>" "$@"`.
- This is the recommended path; hooks make direct `claude` launches work too, just with weaker name guarantees.

## CLAUDE.md protocol snippet

The snippet must instruct Claude to:
1. On first turn, verify its registration: compare its own name (as it appears to peers) against the ledger record; if the SessionStart hook registered a derived name that doesn't match, call `register` with the correct name and `deregister` the derived one.
2. Set a real `role`, `capabilities`, and `query_me_when` as soon as the session's purpose is clear (usually first user prompt).
3. Call `update_registration` whenever focus meaningfully shifts (new project, new major task) — the tmux label updates as a side effect; never use `/rename`.
4. Before messaging a peer about a topic, call `find_agents` first; address `SendMessage` using the returned `session_name`. If delivery fails or the entry is stale, fall back to native `ListAgents`.
5. Never treat the ledger as a message channel — no "leave a note in the ledger" patterns.

## Out of scope (v1)

- Cross-machine registry (single machine only; `machine` column exists for future use).
- Postgres sink / exocortex integration (the events table **is** the integration surface; nothing ships that reads it).
- Embedding/semantic search in `find_agents`.
- Any message delivery, queuing, or inbox semantics.
- Web UI (a `ledger` CLI subcommand that pretty-prints `list_agents_detailed` is a nice-to-have, low priority).

## Acceptance tests

1. Launch two sessions via `launch-session.sh` in different repos; each appears in `list_agents_detailed` with correct name, cwd, project.
2. In session A, ask Claude "who should I ask about <session B's domain>?" — Claude calls `find_agents`, gets B's `session_name`, and `SendMessage` to that name succeeds.
3. Update B's registration with a new `project` — B's tmux window label changes within one turn; `events` table shows the update.
4. Kill session B without SessionEnd firing (kill -9 the process); after TTL, B is flagged stale, then evicted, with an `evicted` event.
5. Stop the ledger MCP server entirely; sessions still start normally (hook exits 0) and native `ListAgents`/`SendMessage` still work.
6. Re-launch B with the same name; `register` upserts cleanly, no duplicate rows.
7. `sqlite3 ledger.db 'select * from events'` shows a complete, ordered history of the above.
