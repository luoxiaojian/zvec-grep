from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from zg_bench.reports.search_traces import classify_tool, export_traces, main


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def make_trial(root: Path, *, profile: str = "baseline", name: str = "task__trial",
               archived: bool = False, failed: bool = False) -> Path:
    job = root / f"job-{profile}"
    trial = job / ".retry-history" / name / "attempt-1" if archived else job / name
    write_json(trial / "result.json", {
        "trial_name": name, "task_name": "task", "finished_at": "2026-10-28T01:00:00Z",
        "exception_info": {"exception_type": "RuntimeError"} if failed else None,
    })
    write_json(trial / "agent" / "trajectory.json", {
        "session_id": "root", "steps": [
            {"step_id": 1, "source": "user", "message": "Find the computation function."},
            {"step_id": 2, "source": "agent", "message": "The answer."},
        ],
    })
    return trial


def complete_trace(trial: Path) -> Path:
    from zg_bench.agents.opencode_trace import TRACE_SCOPE, validate_trace_directory

    directory = trial / "agent" / "session-trace"
    write_json(directory / "trace.json", {
        "schema_version": 1, "scope": TRACE_SCOPE, "root_session_id": "root",
        "sessions": [{"id": "root", "parent_id": None}],
        "messages": [
            {"id": "user", "session_id": "root", "data": {"role": "user"}},
            {"id": "assistant", "session_id": "root", "data": {"role": "assistant", "time": {"completed": 1}}},
        ], "parts": [
            {"id": "question", "session_id": "root", "message_id": "user", "data": {"type": "text", "text": "Question"}},
            {"id": "answer", "session_id": "root", "message_id": "assistant", "data": {"type": "text", "text": "Answer"}},
        ], "errors": [], "overflow": [],
    })
    (directory / "instruction.txt").write_text("Question", encoding="utf-8")
    manifest = validate_trace_directory(directory, execution_complete=True)
    assert manifest["complete"], manifest["errors"]
    write_json(directory / "manifest.json", manifest)
    return directory


class SearchTraceTests(unittest.TestCase):
    def test_matches_multiple_outputs_and_preserves_long_empty_and_missing_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "runs"
            trial = make_trial(root)
            long_output = "完整返回\n```\n" + "abcdefghij" * 20_000 + "\nEND"
            write_json(trial / "agent" / "trajectory.json", {
                "session_id": "root", "steps": [
                    {"source": "user", "message": "Question"},
                    {"step_id": 2, "source": "agent", "timestamp": "now", "tool_calls": [
                        {"tool_call_id": "many", "function_name": "grep", "arguments": {"pattern": "target"}},
                        {"tool_call_id": "empty", "function_name": "read", "arguments": {}},
                        {"tool_call_id": "missing", "function_name": "read", "arguments": {}},
                    ], "observation": {"results": [
                        {"source_call_id": "many", "content": long_output},
                        {"source_call_id": "many", "content": "second result"},
                        {"source_call_id": "empty", "content": ""},
                        {"source_call_id": "orphan", "content": "unmatched result"},
                    ]}},
                    {"source": "agent", "message": "Final answer"},
                ],
            })
            output = Path(temp) / "output"
            index = export_traces(runs_dir=root, output_dir=output)
            events = [json.loads(line) for line in (output / "events.jsonl").read_text().splitlines()]
            self.assertEqual(len(events), 3)
            self.assertEqual(events[0]["observations"], [long_output, "second result"])
            self.assertTrue(events[1]["output_present"])
            self.assertEqual(events[1]["observations"], [""])
            self.assertFalse(events[2]["output_present"])
            markdown = (output / index["trials"][0]["markdown"]).read_text()
            for text in ["Question", long_output, "second result", "unmatched result", "Final answer"]:
                self.assertIn(text, markdown)
            self.assertIn("Output missing", markdown)
            self.assertIn("````\n" + long_output, markdown)

    def test_classification_recognizes_executables_not_rg_substrings_or_arguments(self) -> None:
        for command in ["rg -n needle .", "/usr/bin/rg needle", "cd /app && rg needle | head", "cd /app\nrg -n needle .", "sudo -u root rg needle", "env LC_ALL=C rg needle", "command rg needle", "bash -lc 'rg needle'"]:
            with self.subTest(command=command):
                self.assertEqual(classify_tool("bash", {"command": command}), "shell_rg")
        for command in ["echo rg", "grep rg file", "cargo test", "rgignore needle", "curl https://example.test/rg", "printf '%s' 'rg -n needle'", "echo 'x; rg needle'", "echo '|' rg", "echo 'x\nrg needle'", "echo x # ; rg needle", "sudo -u rg echo needle", "env -u rg echo needle", "command -v rg", "cat target"]:
            with self.subTest(command=command):
                self.assertEqual(classify_tool("bash", {"command": command}), "other")
        self.assertEqual(classify_tool("grep", {"pattern": "rg"}), "builtin_grep")
        self.assertEqual(classify_tool("zvec_grep_zvec_grep_search", {}), "zg_mcp")
        self.assertEqual(classify_tool("mcp__zvec_grep__zvec_grep_search", {}), "zg_mcp")
        self.assertEqual(classify_tool("read", {"path": "/rg"}), "other")

    def test_retry_history_is_separate_from_success_and_terminal_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "runs"
            make_trial(root, name="task__recovered")
            make_trial(root, name="task__recovered", archived=True, failed=True)
            make_trial(root, name="task__failed", profile="zvec-grep", failed=True)
            index = export_traces(runs_dir=root, output_dir=root / "search-traces")
            self.assertEqual(index["counts"], {"retry-history:failed": 1, "current:success": 1, "current:failed": 1})
            history = next(record for record in index["trials"] if record["kind"] == "retry-history")
            self.assertTrue(history["markdown"].startswith("retry-history/"))
            self.assertEqual(history["attempt"], "attempt-1")
            self.assertEqual(history["trial"], "task__recovered")
            # Re-exporting into the runs directory must not ingest its own index.
            self.assertEqual(len(export_traces(runs_dir=root, output_dir=root / "search-traces")["trials"]), 3)

    def test_raw_sessions_include_children_and_do_not_duplicate_root_trajectory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "runs"
            trial = make_trial(root)
            raw = trial / "agent" / "session-trace"
            write_json(raw / "manifest.json", {"complete": True})
            write_json(raw / "trace.json", {
                "root_session_id": "root", "sessions": [
                    {"id": "root", "parent_id": None}, {"id": "child", "parent_id": "root"},
                ], "messages": [
                    {"id": "m1", "session_id": "root", "time_created": 1, "data": {"role": "user"}},
                    {"id": "m2", "session_id": "root", "time_created": 2, "data": {"role": "assistant"}},
                    {"id": "m3", "session_id": "child", "time_created": 3, "data": {"role": "assistant"}},
                    {"id": "m4", "session_id": "root", "time_created": 4, "data": {"role": "assistant"}},
                ], "parts": [
                    {"id": "p1", "message_id": "m1", "data": {"type": "text", "text": "Raw question"}},
                    {"id": "p2", "message_id": "m2", "data": {"type": "tool", "tool": "grep", "callID": "r", "state": {"input": {"pattern": "root"}, "output": "root match", "status": "completed"}}},
                    {"id": "p3", "message_id": "m3", "data": {"type": "tool", "tool": "zvec_grep_zvec_grep_search", "callID": "c", "state": {"input": {"query": "child"}, "output": "child match"}}},
                    {"id": "p4", "message_id": "m4", "data": {"type": "text", "text": "Raw final answer"}},
                ], "overflow": [{"relative_path": "overflow/full.txt", "status": "captured"}],
            })
            (raw / "overflow").mkdir()
            (raw / "overflow" / "full.txt").write_text("full overflow content")
            output = Path(temp) / "output"
            index = export_traces(runs_dir=root, output_dir=output)
            record = index["trials"][0]
            self.assertEqual(record["trace_scope"], "session-tree")
            self.assertTrue(record["trace_complete"])
            self.assertEqual(record["session_count"], 2)
            events = [json.loads(line) for line in (output / "events.jsonl").read_text().splitlines()]
            self.assertEqual(len(events), 2)
            self.assertEqual(events[1]["session_id"], "child")
            self.assertEqual(events[1]["parent_session_id"], "root")
            markdown = (output / record["markdown"]).read_text()
            self.assertIn("Raw question", markdown)
            self.assertIn("Raw final answer", markdown)
            self.assertIn("full overflow content", markdown)
            self.assertNotIn("Find the computation function.", markdown)

    def test_invalid_raw_root_falls_back_explicitly_and_legacy_is_not_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "runs"
            trial = make_trial(root)
            write_json(trial / "agent" / "session-trace" / "trace.json", {
                "root_session_id": "root", "sessions": [{"id": "child"}],
            })
            index = export_traces(runs_dir=root, output_dir=Path(temp) / "output")
            record = index["trials"][0]
            self.assertEqual(record["trace_scope"], "root-only")
            self.assertFalse(record["trace_complete"])
            self.assertTrue(any("fell back" in warning for warning in record["warnings"]))

    def test_cli_reports_missing_runs_without_creating_success_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "output"
            self.assertEqual(main(["--runs-dir", str(Path(temp) / "missing"), "--output-dir", str(output)]), 1)
            self.assertFalse((output / "index.json").exists())

    def test_strict_gate_checks_both_profiles_and_does_not_count_retry_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "runs"
            for profile in ("baseline", "zvec-grep"):
                complete_trace(make_trial(root, profile=profile))
            make_trial(root, archived=True, failed=True)
            output = root / "search-traces"
            args = ["--runs-dir", str(root), "--output-dir", str(output), "--expected-trials", "1", "--require-complete"]
            self.assertEqual(main(args), 0)
            index = json.loads((output / "index.json").read_text())
            self.assertTrue(index["validation"]["passed"])
            self.assertEqual(len(index["trials"]), 3)
            self.assertTrue(all(record["trace_complete"] for record in index["trials"] if record["kind"] == "current"))
            args[args.index("--expected-trials") + 1] = "2"
            self.assertEqual(main(args), 1)
            self.assertTrue((output / "index.md").exists())
            errors = json.loads((output / "index.json").read_text())["validation"]["errors"]
            self.assertEqual(len(errors), 2)

    def test_strict_gate_recomputes_manifest_hashes_and_rejects_legacy_trace(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "runs"
            baseline = make_trial(root)
            directory = complete_trace(baseline)
            complete_trace(make_trial(root, profile="zvec-grep"))
            trace = json.loads((directory / "trace.json").read_text())
            trace["parts"][0]["data"]["text"] = "Changed after capture"
            write_json(directory / "trace.json", trace)
            index = export_traces(runs_dir=root, output_dir=Path(temp) / "output", require_complete=True, expected_trials=1)
            self.assertFalse(index["validation"]["passed"])
            record = next(record for record in index["trials"] if record["profile"] == "baseline")
            self.assertIn("stored_manifest_file_hashes_do_not_match", record["warnings"])
            (directory / "trace.json").unlink()
            index = export_traces(runs_dir=root, output_dir=Path(temp) / "legacy", require_complete=True)
            self.assertFalse(index["validation"]["passed"])
            record = next(record for record in index["trials"] if record["profile"] == "baseline")
            self.assertEqual(record["trace_scope"], "root-only")


if __name__ == "__main__":
    unittest.main()
