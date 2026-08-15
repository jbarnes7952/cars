#!/usr/bin/env python3
"""Smoke tests for ledger-mcp. Stdlib only. Run: python3 test_ledger.py"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "ledger_mcp.py")


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "ledger.db")
        os.environ["CLAUDE_LEDGER_DB"] = self.db
        sys.path.insert(0, HERE)
        for mod in ("ledger_mcp",):
            sys.modules.pop(mod, None)
        import ledger_mcp
        ledger_mcp.DB_PATH = self.db
        self.ledger = ledger_mcp

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("CLAUDE_LEDGER_DB", None)
        os.environ.pop("CLAUDE_LEDGER_NAME", None)

    def call(self, tool, **args):
        return self.ledger.call_tool(tool, args)

    def events(self, **where):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        clause = " AND ".join(f"{k} = ?" for k in where) or "1=1"
        rows = conn.execute(
            f"SELECT * FROM events WHERE {clause} ORDER BY id", tuple(where.values())
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # -------------------------------------------------------------- basics

    def test_register_and_list(self):
        rec = self.call(
            "register", session_name="alpha", session_id="u-1", cwd="/tmp/x",
            role="schema-owner", capabilities=["sql", "migrations"],
            query_me_when="schema questions", status="working", project="projx",
        )
        self.assertEqual(rec["session_name"], "alpha")
        self.assertEqual(rec["capabilities"], ["sql", "migrations"])
        self.assertFalse(rec["stale"])
        out = self.call("list_agents_detailed")
        self.assertEqual(out["count"], 1)
        self.assertEqual(self.events(event="register")[0]["session_name"], "alpha")

    def test_reregister_upserts_no_duplicates(self):
        self.call("register", session_name="alpha", session_id="u-1", role="a")
        self.call("register", session_name="alpha", session_id="u-2", role="b")
        out = self.call("list_agents_detailed")
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["agents"][0]["role"], "b")
        self.assertEqual(len(self.events(event="register")), 2)

    def test_update_partial_keeps_untouched_fields(self):
        self.call(
            "register", session_name="alpha", role="dev",
            capabilities=["x"], query_me_when="stuff", status="starting",
        )
        rec = self.call("update_registration", session_name="alpha", status="reviewing")
        self.assertEqual(rec["status"], "reviewing")
        self.assertEqual(rec["role"], "dev")
        self.assertEqual(rec["capabilities"], ["x"])
        payload = json.loads(self.events(event="update")[0]["payload"])
        self.assertIn("status", payload)
        self.assertNotIn("role", payload)

    def test_update_unregistered_errors(self):
        with self.assertRaises(self.ledger.ToolError) as ctx:
            self.call("update_registration", session_name="ghost", status="x")
        self.assertIn("register first", str(ctx.exception))

    def test_deregister_idempotent(self):
        self.call("register", session_name="alpha")
        self.call("deregister", session_name="alpha")
        self.call("deregister", session_name="alpha")  # unknown: succeeds silently
        self.call("deregister", session_name="never-existed")
        self.assertEqual(self.call("list_agents_detailed")["count"], 0)
        self.assertEqual(len(self.events(event="deregister")), 1)

    # -------------------------------------------------------------- find

    def test_find_agents_matches_descriptive_fields(self):
        self.call("register", session_name="a", role="schema-owner",
                  capabilities=["postgres"], query_me_when="db migrations")
        self.call("register", session_name="b", role="frontend",
                  status="building dashboard", project="webapp")
        hit = self.call("find_agents", query="MIGRATION")
        self.assertEqual([a["session_name"] for a in hit["agents"]], ["a"])
        hit = self.call("find_agents", query="webapp")
        self.assertEqual([a["session_name"] for a in hit["agents"]], ["b"])
        self.assertEqual(self.call("find_agents", query="nothing-matches")["count"], 0)

    # -------------------------------------------------------------- staleness

    def _age(self, name, seconds):
        from datetime import datetime, timedelta, timezone
        old = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE agents SET last_seen = ? WHERE session_name = ?",
                     (old, name))
        conn.commit()
        conn.close()

    def test_stale_flag_and_filtering(self):
        self.call("register", session_name="old")
        self.call("register", session_name="fresh")
        self._age("old", 11 * 60)
        self.assertEqual(self.call("list_agents_detailed")["count"], 1)
        both = self.call("list_agents_detailed", include_stale=True)
        self.assertEqual(both["count"], 2)
        flags = {a["session_name"]: a["stale"] for a in both["agents"]}
        self.assertTrue(flags["old"])
        self.assertFalse(flags["fresh"])

    def test_eviction_after_24h(self):
        self.call("register", session_name="dead")
        self._age("dead", 25 * 3600)
        self.call("list_agents_detailed", include_stale=True)  # any call triggers
        self.assertEqual(
            self.call("list_agents_detailed", include_stale=True)["count"], 0)
        self.assertEqual(len(self.events(event="evicted", session_name="dead")), 1)

    def test_heartbeat_bumps_and_samples(self):
        self.call("register", session_name="hb")
        r1 = self.call("heartbeat", session_name="hb")
        r2 = self.call("heartbeat", session_name="hb")
        self.assertTrue(r1["registered"] and r2["registered"])
        # register wrote no heartbeat; two rapid heartbeats -> one sampled event
        self.assertEqual(len(self.events(event="heartbeat", session_name="hb")), 1)
        unknown = self.call("heartbeat", session_name="ghost")
        self.assertFalse(unknown["registered"])

    # -------------------------------------------------------------- hooks

    def test_hook_register_env_name_priority(self):
        os.environ["CLAUDE_LEDGER_NAME"] = "minted-name"
        proc = subprocess.run(
            [sys.executable, SERVER, "hook-register"],
            input=json.dumps({"session_id": "abcd1234", "cwd": self.tmp.name}),
            capture_output=True, text=True, env=os.environ.copy(),
        )
        self.assertEqual(proc.returncode, 0)
        rec = self.call("list_agents_detailed")["agents"][0]
        self.assertEqual(rec["session_name"], "minted-name")
        self.assertEqual(rec["role"], "unassigned")
        self.assertEqual(rec["status"], "starting")
        payload = json.loads(self.events(event="register")[0]["payload"])
        self.assertEqual(payload["name_source"], "env")

    def test_hook_register_derived_name(self):
        os.environ.pop("CLAUDE_LEDGER_NAME", None)
        cwd = os.path.join(self.tmp.name, "myproj")
        os.makedirs(cwd)
        proc = subprocess.run(
            [sys.executable, SERVER, "hook-register"],
            input=json.dumps({"session_id": "abcd1234-x", "cwd": cwd}),
            capture_output=True, text=True, env=os.environ.copy(),
        )
        self.assertEqual(proc.returncode, 0)
        rec = self.call("list_agents_detailed")["agents"][0]
        self.assertEqual(rec["session_name"], "myproj-abcd")
        payload = json.loads(self.events(event="register")[0]["payload"])
        self.assertEqual(payload["name_source"], "derived")

    def test_hook_deregister(self):
        os.environ["CLAUDE_LEDGER_NAME"] = "minted-name"
        self.call("register", session_name="minted-name")
        proc = subprocess.run(
            [sys.executable, SERVER, "hook-deregister"],
            input="{}", capture_output=True, text=True, env=os.environ.copy(),
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(self.call("list_agents_detailed")["count"], 0)

    def test_hook_register_survives_bad_input(self):
        os.environ["CLAUDE_LEDGER_NAME"] = "resilient"
        proc = subprocess.run(
            [sys.executable, SERVER, "hook-register"],
            input="this is not json", capture_output=True, text=True,
            env=os.environ.copy(),
        )
        self.assertEqual(proc.returncode, 0)

    def test_infer_project_git_root(self):
        repo = os.path.join(self.tmp.name, "repo")
        sub = os.path.join(repo, "src", "deep")
        os.makedirs(os.path.join(repo, ".git"))
        os.makedirs(sub)
        self.assertEqual(self.ledger.infer_project(sub), "repo")
        bare = os.path.join(self.tmp.name, "norepo")
        os.makedirs(bare)
        self.assertEqual(self.ledger.infer_project(bare), "norepo")

    # -------------------------------------------------------------- MCP stdio

    def test_mcp_stdio_end_to_end(self):
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "register",
                        "arguments": {"session_name": "stdio-agent",
                                      "role": "tester",
                                      "capabilities": ["testing"]}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "find_agents", "arguments": {"query": "test"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "update_registration",
                        "arguments": {"session_name": "nope", "status": "x"}}},
            {"jsonrpc": "2.0", "id": 6, "method": "no/such/method"},
        ]
        proc = subprocess.run(
            [sys.executable, SERVER, "serve"],
            input="".join(json.dumps(m) + "\n" for m in msgs),
            capture_output=True, text=True, env=os.environ.copy(), timeout=30,
        )
        responses = {r["id"]: r for r in
                     (json.loads(l) for l in proc.stdout.splitlines() if l.strip())}
        self.assertEqual(len(responses), 6)  # notification got no response
        init = responses[1]["result"]
        self.assertEqual(init["serverInfo"]["name"], "ledger")
        self.assertEqual(init["protocolVersion"], "2025-06-18")
        tools = {t["name"] for t in responses[2]["result"]["tools"]}
        self.assertEqual(tools, {"register", "update_registration", "heartbeat",
                                 "find_agents", "list_agents_detailed", "deregister"})
        reg = json.loads(responses[3]["result"]["content"][0]["text"])
        self.assertEqual(reg["session_name"], "stdio-agent")
        found = json.loads(responses[4]["result"]["content"][0]["text"])
        self.assertEqual(found["agents"][0]["session_name"], "stdio-agent")
        self.assertTrue(responses[5]["result"]["isError"])
        self.assertEqual(responses[6]["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main(verbosity=2)
