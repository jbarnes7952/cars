#!/usr/bin/env python3
"""ledger-mcp: MCP directory server for Claude Code peer sessions.

Directory only, never transport. Sessions register identity/role/capabilities;
peers query to decide WHO to message, then use native SendMessage.

Zero dependencies: Python stdlib only (sqlite3, json). Localhost only.

Usage:
    ledger_mcp.py [serve]          MCP server on stdio (default)
    ledger_mcp.py hook-register    SessionStart hook helper (hook JSON on stdin)
    ledger_mcp.py hook-deregister  SessionEnd hook helper (hook JSON on stdin)
    ledger_mcp.py list [--stale]   pretty-print registered agents
"""

import json
import os
import socket
import sqlite3
import sys
from datetime import datetime, timezone

DB_PATH = os.environ.get(
    "CLAUDE_LEDGER_DB", os.path.expanduser("~/.claude-ledger/ledger.db")
)
SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")

STALE_SECONDS = 10 * 60          # older than this => flagged stale
EVICT_SECONDS = 24 * 60 * 60     # older than this => evicted (lazily)
HEARTBEAT_SAMPLE_SECONDS = 5 * 60  # at most one heartbeat event per session per 5 min

AGENT_COLUMNS = [
    "session_name", "session_id", "pid", "cwd", "project", "role",
    "capabilities", "query_me_when", "status", "tmux_pane", "machine",
    "registered_at", "last_seen",
]
UPDATABLE_FIELDS = ["role", "capabilities", "query_me_when", "status", "project"]
SEARCH_FIELDS = ["role", "capabilities", "query_me_when", "status", "project"]


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def parse_iso(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def age_seconds(ts):
    dt = parse_iso(ts)
    if dt is None:
        return float("inf")
    return (datetime.now(timezone.utc) - dt).total_seconds()


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    with open(SCHEMA_PATH) as f:
        conn.executescript(f.read())
    return conn


def write_event(conn, session_name, session_id, event, payload):
    conn.execute(
        "INSERT INTO events (ts, session_name, session_id, event, payload)"
        " VALUES (?, ?, ?, ?, ?)",
        (now_iso(), session_name, session_id or "", event, json.dumps(payload)),
    )


def evict_stale(conn):
    """Lazy eviction, run at the start of every tool call."""
    rows = conn.execute("SELECT * FROM agents").fetchall()
    for row in rows:
        if age_seconds(row["last_seen"]) > EVICT_SECONDS:
            conn.execute(
                "DELETE FROM agents WHERE session_name = ?", (row["session_name"],)
            )
            write_event(
                conn, row["session_name"], row["session_id"], "evicted",
                {"last_seen": row["last_seen"]},
            )
    conn.commit()


def row_to_record(row):
    rec = {k: row[k] for k in AGENT_COLUMNS}
    try:
        rec["capabilities"] = json.loads(rec["capabilities"] or "[]")
    except (ValueError, TypeError):
        rec["capabilities"] = []
    rec["stale"] = age_seconds(row["last_seen"]) > STALE_SECONDS
    return rec


def get_record(conn, session_name):
    row = conn.execute(
        "SELECT * FROM agents WHERE session_name = ?", (session_name,)
    ).fetchone()
    return row_to_record(row) if row else None


# ---------------------------------------------------------------- operations

class ToolError(Exception):
    pass


def op_register(conn, args):
    name = args["session_name"]
    caps = args.get("capabilities") or []
    if not isinstance(caps, list):
        caps = [str(caps)]
    now = now_iso()
    fields = {
        "session_name": name,
        "session_id": args.get("session_id") or "",
        "pid": args.get("pid"),
        "cwd": args.get("cwd") or "",
        "project": args.get("project") or "",
        "role": args.get("role") or "unassigned",
        "capabilities": json.dumps(caps),
        "query_me_when": args.get("query_me_when") or "",
        "status": args.get("status") or "",
        "tmux_pane": args.get("tmux_pane") or "",
        "machine": socket.gethostname(),
        "registered_at": now,
        "last_seen": now,
    }
    cols = ", ".join(fields)
    ph = ", ".join("?" for _ in fields)
    # Re-registration of an existing name replaces the row (session restart).
    conn.execute(
        f"INSERT OR REPLACE INTO agents ({cols}) VALUES ({ph})",
        tuple(fields.values()),
    )
    payload = dict(fields, capabilities=caps)
    if args.get("name_source"):
        payload["name_source"] = args["name_source"]
    write_event(conn, name, fields["session_id"], "register", payload)
    conn.commit()
    return get_record(conn, name)


def op_update_registration(conn, args):
    name = args["session_name"]
    row = conn.execute(
        "SELECT * FROM agents WHERE session_name = ?", (name,)
    ).fetchone()
    if row is None:
        raise ToolError(
            f"'{name}' is not registered; call register first."
        )
    changed = {}
    for field in UPDATABLE_FIELDS:
        if field in args and args[field] is not None:
            value = args[field]
            if field == "capabilities":
                if not isinstance(value, list):
                    value = [str(value)]
                changed[field] = json.dumps(value)
            else:
                changed[field] = value
    changed["last_seen"] = now_iso()
    sets = ", ".join(f"{k} = ?" for k in changed)
    conn.execute(
        f"UPDATE agents SET {sets} WHERE session_name = ?",
        tuple(changed.values()) + (name,),
    )
    payload = {
        k: (json.loads(v) if k == "capabilities" else v) for k, v in changed.items()
    }
    write_event(conn, name, row["session_id"], "update", payload)
    conn.commit()
    return get_record(conn, name)


def op_heartbeat(conn, args):
    name = args["session_name"]
    now = now_iso()
    cur = conn.execute(
        "UPDATE agents SET last_seen = ? WHERE session_name = ?", (now, name)
    )
    if cur.rowcount:
        # Sample heartbeat events: at most one per session per 5 minutes.
        last = conn.execute(
            "SELECT MAX(ts) AS ts FROM events"
            " WHERE session_name = ? AND event = 'heartbeat'",
            (name,),
        ).fetchone()["ts"]
        if last is None or age_seconds(last) > HEARTBEAT_SAMPLE_SECONDS:
            row = conn.execute(
                "SELECT session_id FROM agents WHERE session_name = ?", (name,)
            ).fetchone()
            write_event(conn, name, row["session_id"], "heartbeat", {"last_seen": now})
    conn.commit()
    return {"session_name": name, "registered": bool(cur.rowcount), "last_seen": now}


def op_find_agents(conn, args):
    query = args["query"]
    include_stale = bool(args.get("include_stale", False))
    like = f"%{query}%"
    where = " OR ".join(f"{f} LIKE ? COLLATE NOCASE" for f in SEARCH_FIELDS)
    rows = conn.execute(
        f"SELECT * FROM agents WHERE {where} ORDER BY last_seen DESC",
        tuple(like for _ in SEARCH_FIELDS),
    ).fetchall()
    records = [row_to_record(r) for r in rows]
    if not include_stale:
        records = [r for r in records if not r["stale"]]
    return {"agents": records, "count": len(records)}


def op_list_agents_detailed(conn, args):
    include_stale = bool(args.get("include_stale", False))
    rows = conn.execute("SELECT * FROM agents ORDER BY last_seen DESC").fetchall()
    records = [row_to_record(r) for r in rows]
    if not include_stale:
        records = [r for r in records if not r["stale"]]
    return {"agents": records, "count": len(records)}


def op_deregister(conn, args):
    name = args["session_name"]
    row = conn.execute(
        "SELECT session_id FROM agents WHERE session_name = ?", (name,)
    ).fetchone()
    # Idempotent: deregistering an unknown name succeeds silently.
    if row is not None:
        conn.execute("DELETE FROM agents WHERE session_name = ?", (name,))
        write_event(conn, name, row["session_id"], "deregister", {})
        conn.commit()
    return {"session_name": name, "deregistered": True}


# ---------------------------------------------------------------- MCP server

STR = {"type": "string"}
BOOL_FALSE = {"type": "boolean", "default": False}
CAPS = {"type": "array", "items": {"type": "string"}}

TOOLS = [
    {
        "name": "register",
        "description": "Register this session in the peer directory. Upserts by session_name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_name": {**STR, "description": "Claude Code messaging name (the address peers use with SendMessage)"},
                "session_id": STR, "cwd": STR, "role": STR,
                "capabilities": CAPS, "query_me_when": STR, "status": STR,
                "project": STR, "tmux_pane": STR,
                "pid": {"type": "integer"}, "name_source": STR,
            },
            "required": ["session_name"],
        },
        "handler": op_register,
    },
    {
        "name": "update_registration",
        "description": "Partially update this session's directory entry (role, capabilities, query_me_when, status, project).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_name": STR, "role": STR, "capabilities": CAPS,
                "query_me_when": STR, "status": STR, "project": STR,
            },
            "required": ["session_name"],
        },
        "handler": op_update_registration,
    },
    {
        "name": "heartbeat",
        "description": "Mark this session as still alive (bumps last_seen).",
        "inputSchema": {
            "type": "object",
            "properties": {"session_name": STR},
            "required": ["session_name"],
        },
        "handler": op_heartbeat,
    },
    {
        "name": "find_agents",
        "description": "Find peer sessions to message. Free-text match on role/capabilities/query_me_when/status/project; returns each match's session_name for SendMessage.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": STR, "include_stale": BOOL_FALSE},
            "required": ["query"],
        },
        "handler": op_find_agents,
    },
    {
        "name": "list_agents_detailed",
        "description": "List all registered peer sessions with full records.",
        "inputSchema": {
            "type": "object",
            "properties": {"include_stale": BOOL_FALSE},
        },
        "handler": op_list_agents_detailed,
    },
    {
        "name": "deregister",
        "description": "Remove a session from the peer directory. Idempotent.",
        "inputSchema": {
            "type": "object",
            "properties": {"session_name": STR},
            "required": ["session_name"],
        },
        "handler": op_deregister,
    },
]
TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}


def call_tool(name, args):
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        raise ToolError(f"unknown tool: {name}")
    for req in tool["inputSchema"].get("required", []):
        if not args.get(req):
            raise ToolError(f"missing required argument: {req}")
    conn = connect()
    try:
        evict_stale(conn)
        return tool["handler"](conn, args)
    finally:
        conn.close()


def rpc_response(msg_id, result=None, error=None):
    resp = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        resp["error"] = error
    else:
        resp["result"] = result
    sys.stdout.write(json.dumps(resp) + "\n")
    sys.stdout.flush()


def serve():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method = msg.get("method")
        msg_id = msg.get("id")
        if msg_id is None:  # notification — nothing to answer
            continue
        if method == "initialize":
            rpc_response(msg_id, {
                "protocolVersion": msg.get("params", {}).get(
                    "protocolVersion", "2025-06-18"
                ),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "ledger", "version": "1.0.0"},
            })
        elif method == "ping":
            rpc_response(msg_id, {})
        elif method == "tools/list":
            rpc_response(msg_id, {
                "tools": [
                    {k: t[k] for k in ("name", "description", "inputSchema")}
                    for t in TOOLS
                ]
            })
        elif method == "tools/call":
            params = msg.get("params", {})
            try:
                result = call_tool(params.get("name"), params.get("arguments") or {})
                rpc_response(msg_id, {
                    "content": [{"type": "text", "text": json.dumps(result)}],
                    "isError": False,
                })
            except ToolError as exc:
                rpc_response(msg_id, {
                    "content": [{"type": "text", "text": json.dumps({"error": str(exc)})}],
                    "isError": True,
                })
            except Exception as exc:  # never crash the server on one bad call
                rpc_response(msg_id, {
                    "content": [{"type": "text", "text": json.dumps({"error": f"internal: {exc}"})}],
                    "isError": True,
                })
        else:
            rpc_response(msg_id, error={"code": -32601, "message": f"method not found: {method}"})


# ---------------------------------------------------------------- hook helpers

def infer_project(cwd):
    """basename of the git repo root if cwd is inside a git repo, else basename(cwd)."""
    if not cwd:
        return ""
    path = os.path.abspath(cwd)
    probe = path
    while True:
        if os.path.exists(os.path.join(probe, ".git")):
            return os.path.basename(probe)
        parent = os.path.dirname(probe)
        if parent == probe:
            return os.path.basename(path)
        probe = parent


def read_hook_input():
    try:
        data = json.loads(sys.stdin.read() or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def resolve_name(hook):
    """Name resolution priority: env var, session_title from hook stdin, derived."""
    env_name = os.environ.get("CLAUDE_LEDGER_NAME", "").strip()
    if env_name:
        return env_name, "env"
    title = (hook.get("session_title") or "").strip()
    if title:
        return title, "session_title"
    cwd = hook.get("cwd") or os.getcwd()
    sid = hook.get("session_id") or ""
    return f"{os.path.basename(os.path.abspath(cwd))}-{sid[:4]}", "derived"


def hook_register():
    hook = read_hook_input()
    name, source = resolve_name(hook)
    cwd = hook.get("cwd") or os.getcwd()
    args = {
        "session_name": name,
        "session_id": hook.get("session_id") or "",
        "cwd": cwd,
        "project": infer_project(cwd),
        "role": "unassigned",
        "status": "starting",
        "tmux_pane": os.environ.get("TMUX_PANE", ""),
        "pid": os.getppid() or None,
        "name_source": source,
    }
    call_tool("register", args)


def hook_deregister():
    hook = read_hook_input()
    name, _ = resolve_name(hook)
    call_tool("deregister", {"session_name": name})


def cli_list(include_stale):
    result = call_tool("list_agents_detailed", {"include_stale": include_stale})
    agents = result["agents"]
    if not agents:
        print("no registered agents")
        return
    cols = ["session_name", "role", "project", "status", "last_seen", "stale"]
    widths = {
        c: max(len(c), max(len(str(a[c])) for a in agents)) for c in cols
    }
    print("  ".join(c.ljust(widths[c]) for c in cols))
    for a in agents:
        print("  ".join(str(a[c]).ljust(widths[c]) for c in cols))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "serve":
        serve()
    elif cmd == "hook-register":
        try:
            hook_register()
        except Exception:
            pass  # registration failure must never disturb session start
    elif cmd == "hook-deregister":
        try:
            hook_deregister()
        except Exception:
            pass
    elif cmd == "list":
        cli_list("--stale" in sys.argv[2:])
    else:
        sys.stderr.write(__doc__)
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
