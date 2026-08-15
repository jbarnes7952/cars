-- Peer Session Ledger schema. Applied idempotently on every startup.

CREATE TABLE IF NOT EXISTS agents (
    session_name  TEXT PRIMARY KEY,   -- the Claude Code messaging name (the address)
    session_id    TEXT,               -- Claude Code session UUID, for self-heal/audit
    pid           INTEGER,            -- process id if known
    cwd           TEXT,               -- working directory at registration
    project       TEXT,               -- inferred project name (drives tmux label)
    role          TEXT,               -- short role label, e.g. schema-owner
    capabilities  TEXT,               -- JSON array of strings
    query_me_when TEXT,               -- routing guidance for other agents
    status        TEXT,               -- current focus, updated as work shifts
    tmux_pane     TEXT,               -- $TMUX_PANE at registration, if in tmux
    machine       TEXT,               -- hostname
    registered_at TEXT,               -- ISO 8601 UTC
    last_seen     TEXT                -- ISO 8601 UTC, bumped on every write from that session
);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT,                -- ISO 8601 UTC
    session_name TEXT,
    session_id   TEXT,
    event        TEXT,                -- register | update | heartbeat | deregister | evicted
    payload      TEXT                 -- JSON snapshot of the fields written
);

CREATE INDEX IF NOT EXISTS idx_events_session_event_ts
    ON events (session_name, event, ts);
