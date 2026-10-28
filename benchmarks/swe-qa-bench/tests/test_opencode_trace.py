"""Run the production Node collector against a real, isolated SQLite fixture."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from zg_bench.agents.opencode_trace import (
    TRACE_SCOPE, trace_capture_script, validate_trace_directory,
)


def trace_fixture() -> dict:
    sessions = [
        {"id": "root", "parent_id": None},
        {"id": "child", "parent_id": "root"},
        {"id": "grandchild", "parent_id": "child"},
        {"id": "unrelated", "parent_id": None},
    ]
    messages, parts = [], []
    for sid in ("root", "child", "grandchild", "unrelated"):
        for role in ("user", "assistant"):
            mid = f"{sid}-{role}"
            messages.append({"id": mid, "session_id": sid, "time_created": 100,
                "data": {"role": role, "time": {"created": 100, "completed": 200}}})
            parts.append({"id": mid + "-text", "message_id": mid, "session_id": sid,
                "time_created": 100, "data": {"type": "text",
                    "text": ("UNRELATED_PRIVATE_CONTENT" if sid == "unrelated"
                             else f"{sid} {role} complete original text")}})
    for sid, child in (("root", "child"), ("child", "grandchild")):
        parts.append({"id": sid + "-task", "message_id": sid + "-assistant", "session_id": sid,
            "time_created": 101, "data": {"type": "tool", "tool": "task", "callID": sid + "-call",
                "state": {"status": "completed", "input": {"prompt": "Find implementation"},
                    "output": "delegated answer", "metadata": {"sessionId": child}}}})
    parts.append({"id": "grandchild-search", "message_id": "grandchild-assistant",
        "session_id": "grandchild", "time_created": 102,
        "data": {"type": "tool", "tool": "zvec_grep_zvec_grep_search", "callID": "search-1",
            "state": {"status": "completed", "input": {"root": "/app", "query": "resolve lifecycle"},
                "output": "result.py:42: actual matching source", "metadata": {}}}})
    parts.append({"id": "grandchild-reasoning", "message_id": "grandchild-assistant",
        "session_id": "grandchild", "time_created": 103,
        "data": {"type": "reasoning", "text": "Agent-visible reasoning"}})
    return {"schema_version": 1, "scope": TRACE_SCOPE, "root_session_id": "root",
        "sessions": sessions, "messages": messages, "parts": parts, "errors": [], "overflow": []}


def selected_fixture() -> dict:
    trace = trace_fixture()
    trace["sessions"] = [row for row in trace["sessions"] if row["id"] != "unrelated"]
    for name in ("messages", "parts"):
        trace[name] = [row for row in trace[name] if row["session_id"] != "unrelated"]
    return trace


class TraceValidationTests(unittest.TestCase):
    def validate(self, value: dict, *, execution_complete=True, usage=None) -> dict:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "instruction.txt").write_text("Original question")
            (directory / "trace.json").write_text(json.dumps(value))
            return validate_trace_directory(directory, execution_complete=execution_complete, usage=usage)

    def test_complete_tree_preserves_tool_bodies_and_final_answers(self):
        trace = selected_fixture()
        manifest = self.validate(trace)
        self.assertTrue(manifest["complete"], manifest["errors"])
        self.assertEqual(manifest["counts"], {"sessions": 3, "messages": 6, "parts": 10,
            "tool_calls": 3, "overflow_files": 0})
        self.assertEqual(len(manifest["files"]), 2)
        for item in manifest["files"]:
            self.assertEqual(len(item["sha256"]), 64)

    def test_missing_child_tool_body_or_message_parts_fail_closed(self):
        cases = {}
        missing_child = selected_fixture()
        missing_child["sessions"] = missing_child["sessions"][:2]
        cases["missing_task_child"] = missing_child
        for field in ("input", "output"):
            missing = selected_fixture()
            del next(p for p in missing["parts"] if p["id"] == "grandchild-search")["data"]["state"][field]
            cases["missing_tool_" + field] = missing
        missing_parts = selected_fixture()
        missing_parts["parts"] = [p for p in missing_parts["parts"] if p["id"] != "child-user-text"]
        cases["missing_message_parts"] = missing_parts
        for prefix, trace in cases.items():
            with self.subTest(prefix=prefix):
                manifest = self.validate(trace)
                self.assertFalse(manifest["complete"])
                self.assertTrue(any(e.startswith(prefix) for e in manifest["errors"]), manifest)

    def test_unrelated_sessions_and_usage_mismatch_are_not_certified(self):
        manifest = self.validate(trace_fixture())
        self.assertIn("sessions_outside_selected_tree", manifest["errors"])
        manifest = self.validate(selected_fixture(), usage={"sessions": [], "root_session_id": "root", "complete": True})
        self.assertIn("usage_session_mismatch", manifest["errors"])

    def test_failed_execution_retains_valid_snapshot_without_passing(self):
        manifest = self.validate(selected_fixture(), execution_complete=False)
        self.assertTrue(manifest["capture_complete"])
        self.assertFalse(manifest["complete"])
        self.assertIn("execution_incomplete", manifest["errors"])

    def test_missing_root_final_answer_cannot_pass_even_with_finished_tools(self):
        trace = selected_fixture()
        trace["parts"] = [p for p in trace["parts"] if p["id"] != "root-assistant-text"]
        manifest = self.validate(trace)
        self.assertIn("missing_root_answer", manifest["errors"])
        self.assertFalse(manifest["complete"])


@unittest.skipUnless(shutil.which("node"), "production collector requires Node")
class TraceCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.logs = self.root / "logs"
        self.logs.mkdir()
        self.directory = self.logs / "session-trace"
        self.directory.mkdir()
        (self.directory / "instruction.txt").write_text("Original question")
        (self.logs / "opencode.txt").write_text('diagnostic text\n{"sessionID":"root","type":"text"}\n')
        self.db = self.root / "fixture.sqlite"
        self.binary = self.root / "opencode"
        self.binary.write_text(f"#!{sys.executable}\n" + """
import json, os, sqlite3, sys
query = sys.argv[2]
if os.environ.get('TRACE_TEST_FAIL_TABLE') and ('SELECT * FROM ' + os.environ['TRACE_TEST_FAIL_TABLE']) in query:
    sys.exit(1)
db = sqlite3.connect(os.environ['TRACE_TEST_DATABASE'])
db.row_factory = sqlite3.Row
print(json.dumps([dict(row) for row in db.execute(query)]))
""")
        self.binary.chmod(0o755)

    def capture(self, trace=None, *, fail_table=""):
        trace = trace or trace_fixture()
        db = sqlite3.connect(self.db)
        db.executescript("""
            CREATE TABLE session(id TEXT, parent_id TEXT);
            CREATE TABLE message(id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
            CREATE TABLE part(id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
        """)
        for row in trace["sessions"]:
            db.execute("INSERT INTO session VALUES (?,?)", (row["id"], row["parent_id"]))
        for row in trace["messages"]:
            db.execute("INSERT INTO message VALUES (?,?,?,?)", (row["id"], row["session_id"], row["time_created"], json.dumps(row["data"])))
        for row in trace["parts"]:
            db.execute("INSERT INTO part VALUES (?,?,?,?,?)", (row["id"], row["message_id"], row["session_id"], row["time_created"], json.dumps(row["data"])))
        db.commit()
        db.close()
        env = {**os.environ, "HOME": str(self.root), "XDG_DATA_HOME": str(self.root / "data"),
            "TRACE_TEST_DATABASE": str(self.db), "TRACE_TEST_FAIL_TABLE": fail_table}
        subprocess.run(["node", "-e", trace_capture_script(str(self.logs), opencode=str(self.binary))],
            env=env, check=True, capture_output=True, text=True, timeout=20)
        return json.loads((self.directory / "trace.json").read_text())

    def test_recursive_sql_exports_only_current_tree_and_all_original_bodies(self):
        trace = self.capture()
        self.assertEqual({s["id"] for s in trace["sessions"]}, {"root", "child", "grandchild"})
        raw = json.dumps(trace)
        self.assertNotIn("UNRELATED_PRIVATE_CONTENT", raw)
        self.assertIn("resolve lifecycle", raw)
        self.assertIn("result.py:42: actual matching source", raw)
        self.assertIn("Agent-visible reasoning", raw)
        self.assertIn("grandchild assistant complete original text", raw)
        manifest = validate_trace_directory(self.directory, execution_complete=True)
        self.assertTrue(manifest["complete"], manifest)

    def test_later_db_failure_keeps_earlier_rows_but_marks_incomplete(self):
        trace = self.capture(fail_table="part")
        self.assertEqual(len(trace["sessions"]), 3)
        self.assertEqual(len(trace["messages"]), 6)
        self.assertEqual(trace["parts"], [])
        manifest = validate_trace_directory(self.directory, execution_complete=True)
        self.assertFalse(manifest["complete"])
        self.assertIn("db_export_failed:part", manifest["errors"])

    def test_ambiguous_root_never_exports_any_sessions(self):
        (self.logs / "opencode.txt").write_text('{"sessionID":"root"}\n{"sessionID":"unrelated"}\n')
        trace = self.capture()
        self.assertEqual(trace["sessions"], [])
        self.assertIn("missing_or_ambiguous_root_session", trace["errors"])

    def test_overflow_is_preserved_but_outside_paths_and_symlinks_are_rejected(self):
        base = self.root / "data/opencode/tool-output"
        base.mkdir(parents=True)
        output = base / "tool_valid"
        output.write_text("FULL TOOL RESPONSE\n" * 100)
        outside = self.root / "auth.json"
        outside.write_text("DO_NOT_EXPORT_CREDENTIALS")
        symlink = base / "tool_link"
        symlink.symlink_to(outside)
        trace = trace_fixture()
        search = next(p for p in trace["parts"] if p["id"] == "grandchild-search")
        search["data"]["state"]["metadata"] = {"outputPath": str(output)}
        search["data"]["state"]["output"] = f"Full output saved to: {outside}\nFull output saved to: {symlink}\nFull output saved to: {base / 'tool_missing'}"
        captured = self.capture(trace)
        by_path = {entry["source_path"]: entry for entry in captured["overflow"]}
        self.assertEqual(by_path[str(output)]["status"], "saved")
        self.assertEqual((self.directory / by_path[str(output)]["relative_path"]).read_text(), output.read_text())
        self.assertEqual(by_path[str(outside)]["status"], "rejected")
        self.assertEqual(by_path[str(symlink)]["status"], "rejected")
        self.assertEqual(by_path[str(base / 'tool_missing')]["status"], "missing")
        self.assertNotIn("DO_NOT_EXPORT_CREDENTIALS", json.dumps(captured))
        self.assertEqual(len(list((self.directory / "overflow").iterdir())), 1)
        manifest = validate_trace_directory(self.directory, execution_complete=True)
        self.assertFalse(manifest["complete"])
        self.assertTrue(any(error.startswith("overflow_not_saved") for error in manifest["errors"]))


if __name__ == "__main__":
    unittest.main()
