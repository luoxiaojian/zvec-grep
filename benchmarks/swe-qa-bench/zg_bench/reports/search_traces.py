"""Export offline Harbor traces and searchable tool events without pairing calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import sys
from collections import Counter, defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

PROFILES = ("baseline", "zvec-grep")


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _object(path: Path) -> dict[str, Any]:
    value = _read(path)
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _rows(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError(f"expected a list of objects: {label}")
    return value


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _block(value: Any, language: str = "") -> str:
    text = value if isinstance(value, str) else _json(value)
    fence = "`" * max(3, 1 + max((len(m[0]) for m in re.finditer(r"`+", text)), default=0))
    return f"{fence}{language}\n{text}\n{fence}\n"


def _shell_segments(command: str) -> list[list[str]]:
    # Preserve quoted separators; shlex's normalized tokens alone cannot
    # distinguish a real pipe from the literal argument '|'.
    pieces, chunk = [], []
    quote_char: str | None = None
    escaped, comment = False, False
    for char in command:
        if comment and char != "\n":
            continue
        if comment:
            comment = False
        if escaped:
            chunk.append(char)
            escaped = False
        elif char == "\\" and quote_char != "'":
            chunk.append(char)
            escaped = True
        elif quote_char:
            chunk.append(char)
            if char == quote_char:
                quote_char = None
        elif char in "\"'":
            chunk.append(char)
            quote_char = char
        elif char == "#" and (not chunk or chunk[-1].isspace()):
            comment = True
        elif char in ";&|()\n":
            pieces.append("".join(chunk))
            chunk = []
        else:
            chunk.append(char)
    pieces.append("".join(chunk))
    return [shlex.split(piece, comments=True) for piece in pieces]


def _shell_rg(command: str) -> bool:
    """Recognize literal executable invocations, without evaluating shell code."""
    try:
        segments = _shell_segments(command)
    except ValueError:
        return False
    option_arguments = {
        "sudo": {"-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt", "-C", "--close-from", "-T", "--command-timeout", "-R", "--chroot", "-D", "--chdir"},
        "env": {"-u", "--unset", "-C", "--chdir", "-a", "--argv0"},
        "exec": {"-a"}, "command": set(),
    }
    for segment in segments:
        while segment and re.match(r"^[A-Za-z_][A-Za-z_0-9]*=", segment[0]):
            segment = segment[1:]
        while segment and Path(segment[0]).name in {"command", "exec", "env", "sudo"}:
            wrapper = Path(segment[0]).name
            segment = segment[1:]
            if wrapper == "command" and any(flag in segment[:1] for flag in ("-v", "-V")):
                segment = []
            while segment:
                option = segment[0]
                if option == "--":
                    segment = segment[1:]
                    break
                if option in option_arguments[wrapper]:
                    segment = segment[2:]
                elif option.startswith("-") or re.match(r"^[A-Za-z_][A-Za-z_0-9]*=", option):
                    segment = segment[1:]
                else:
                    break
        if not segment:
            continue
        executable = Path(segment[0]).name
        if executable == "rg":
            return True
        if executable in {"bash", "sh", "zsh"} and len(segment) >= 3:
            if segment[1].startswith("-") and "c" in segment[1] and _shell_rg(segment[2]):
                return True
    return False


def classify_tool(name: str, arguments: Any) -> str:
    if name == "grep":
        return "builtin_grep"
    if name in {"zvec_grep_search", "zvec_grep_zvec_grep_search"} or name.endswith("__zvec_grep_search"):
        return "zg_mcp"
    if name in {"bash", "shell", "exec_command"} and isinstance(arguments, dict):
        command = arguments.get("command", arguments.get("cmd"))
        if isinstance(command, str) and _shell_rg(command):
            return "shell_rg"
    return "other"


def _event(*, session: Any, parent: Any, step: Any, timestamp: Any, call_id: Any,
           name: str, arguments: Any, observations: list[Any], output_present: bool,
           source_format: str, state: Any = None) -> dict[str, Any]:
    return {
        "session_id": session, "parent_session_id": parent, "step_id": step,
        "timestamp": timestamp, "call_id": call_id, "tool_name": name,
        "category": classify_tool(name, arguments), "arguments": arguments,
        "observations": observations, "output_present": output_present,
        "source_format": source_format, "state": state,
    }


def _render_event(event: dict[str, Any]) -> list[str]:
    lines = [f"#### Tool: {event['tool_name']} ({event['category']})", "",
             f"Call ID: `{event['call_id']}`", "", "Arguments:", "",
             _block(event["arguments"], "json")]
    if not event["output_present"]:
        lines.extend(["**Output missing from the captured trace.**", ""])
    for number, observation in enumerate(event["observations"], 1):
        lines.extend([f"Output {number}:", "", _block(observation)])
    if event["state"] is not None:
        lines.extend(["Tool state:", "", _block(event["state"], "json")])
    return lines


def _trajectory(path: Path) -> tuple[list[str], list[dict[str, Any]]]:
    data = _object(path)
    lines: list[str] = []
    events: list[dict[str, Any]] = []
    session = data.get("session_id")
    for index, step in enumerate(_rows(data.get("steps", []), "trajectory steps"), 1):
        step_id = step.get("step_id", index)
        lines.extend([f"### Step {step_id} · {step.get('source', 'unknown')}", "",
                      f"Session: `{session}`; time: `{step.get('timestamp', 'not captured')}`", ""])
        if "message" in step:
            lines.extend(["Message:", "", _block(step["message"])])
        if "reasoning_content" in step:
            lines.extend(["Captured model reasoning:", "", _block(step["reasoning_content"])])
        results = step.get("observation", {}).get("results", [])
        by_call: dict[Any, list[dict[str, Any]]] = defaultdict(list)
        for result in results:
            by_call[result.get("source_call_id")].append(result)
        call_ids = set()
        for call in step.get("tool_calls", []):
            call_id = call.get("tool_call_id")
            call_ids.add(call_id)
            matched = by_call.get(call_id, [])
            event = _event(
                session=session, parent=None, step=step_id, timestamp=step.get("timestamp"),
                call_id=call_id, name=call.get("function_name", "unknown"),
                arguments=call.get("arguments"),
                observations=[result.get("content", result) for result in matched],
                output_present=bool(matched), source_format="trajectory",
            )
            events.append(event)
            lines.extend(_render_event(event))
        for result in results:
            if result.get("source_call_id") not in call_ids:
                lines.extend(["Unmatched observation (no captured call):", "", _block(result, "json")])
    return lines, events


def _row_data(row: dict[str, Any]) -> dict[str, Any]:
    data = row.get("data", {})
    if isinstance(data, str):
        data = json.loads(data)
    return data if isinstance(data, dict) else {}


def _row_order(row: dict[str, Any]) -> tuple[str, str]:
    value = row.get("time_created", "")
    if isinstance(value, (float, int)):
        return f"{value:020.3f}", str(row.get("id", ""))
    return str(value), str(row.get("id", ""))


def _session_trace(directory: Path) -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
    trace = _object(directory / "trace.json")
    manifest = _object(directory / "manifest.json") if (directory / "manifest.json").is_file() else {}
    root = trace.get("root_session_id")
    sessions = _rows(trace.get("sessions", []), "sessions")
    if not root or not any(row.get("id") == root for row in sessions):
        raise ValueError("raw session trace is missing its root session")
    parents = {row.get("id"): row.get("parent_id") for row in sessions}
    lines = ["## Captured session tree", "", _block(sessions, "json")]
    instruction = directory / "instruction.txt"
    if instruction.is_file():
        lines.extend(["## Task instruction", "", _block(instruction.read_text(encoding="utf-8"))])
    parts_by_message: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for part in _rows(trace.get("parts", []), "parts"):
        parts_by_message[part.get("message_id")].append(part)
    events: list[dict[str, Any]] = []
    messages = sorted(_rows(trace.get("messages", []), "messages"), key=_row_order)
    for message in messages:
        message_id, session = message.get("id"), message.get("session_id")
        data = _row_data(message)
        lines.extend([f"### Message {message_id} · {data.get('role', 'unknown')}", "",
                      f"Session: `{session}`; parent: `{parents.get(session)}`; time: `{message.get('time_created')}`", ""])
        for part in sorted(parts_by_message.pop(message_id, []), key=_row_order):
            payload = _row_data(part)
            kind = payload.get("type", "unknown")
            if kind == "tool":
                state = payload.get("state", {})
                if not isinstance(state, dict):
                    state = {"captured_state": state}
                event = _event(
                    session=session, parent=parents.get(session), step=message_id,
                    timestamp=part.get("time_created", message.get("time_created")),
                    call_id=payload.get("callID", part.get("id")),
                    name=payload.get("tool", "unknown"), arguments=state.get("input"),
                    observations=[state["output"]] if "output" in state else [],
                    output_present="output" in state, source_format="session-trace",
                    state={key: value for key, value in state.items() if key not in {"input", "output"}},
                )
                events.append(event)
                lines.extend(_render_event(event))
            else:
                lines.extend([f"Part {part.get('id')} · {kind}:", "", _block(payload.get("text", payload))])
    for message_id, parts in parts_by_message.items():
        lines.extend([f"### Orphan parts for missing message {message_id}", "", _block(parts, "json")])
    overflow = _rows(trace.get("overflow", []), "overflow")
    if overflow:
        lines.extend(["## Captured overflow files", ""])
    for item in overflow:
        lines.extend([_block(item, "json")])
        relative = item.get("relative_path")
        target = (directory / relative).resolve() if isinstance(relative, str) else None
        if target is not None and target.is_relative_to(directory.resolve()) and target.is_file():
            lines.append(_block(target.read_text(encoding="utf-8", errors="replace")))
        else:
            lines.append("Overflow content unavailable in this artifact.\n")
    complete = manifest.get("complete") is True
    return lines, events, {
        "trace_scope": "session-tree", "trace_complete": complete,
        "session_count": len(sessions), "trace_manifest": manifest,
        "warnings": ([] if complete else ["Raw trace is incomplete or has no completeness manifest."]),
    }


def _trial_directories(root: Path, output: Path) -> list[Path]:
    found = {path.parent.parent for path in root.rglob("trajectory.json") if path.parent.name == "agent"}
    found.update(path.parent.parent.parent for path in root.rglob("trace.json")
                 if path.parent.name == "session-trace" and path.parent.parent.name == "agent")
    found.update(path.parent for path in root.rglob("result.json") if (path.parent / "agent").is_dir())
    return sorted(path for path in found if not path.is_relative_to(output))


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)[:100] or "unknown"


def _verify_raw_trace(directory: Path, stored: dict[str, Any]) -> dict[str, Any]:
    from zg_bench.agents.opencode_trace import validate_trace_directory

    usage_path = directory.parent / "session-usage.json"
    usage = _object(usage_path) if usage_path.is_file() else None
    checked = validate_trace_directory(
        directory, execution_complete=stored.get("execution_complete") is True,
        usage=usage, capture_error=stored.get("capture_error"),
    )
    errors = list(checked.get("errors", []))
    if stored.get("complete") is not True:
        errors.append("stored_manifest_not_complete")
    if sorted(stored.get("files", []), key=lambda item: item.get("path", "")) != checked.get("files", []):
        errors.append("stored_manifest_file_hashes_do_not_match")
    return {**checked, "errors": errors, "complete": not errors}


def export_traces(*, runs_dir: Path, output_dir: Path, expected_trials: int | None = None,
                  require_complete: bool = False) -> dict[str, Any]:
    runs_dir, output_dir = runs_dir.resolve(), output_dir.resolve()
    if not runs_dir.is_dir():
        raise ValueError(f"runs directory does not exist: {runs_dir}")
    if runs_dir == output_dir or runs_dir.is_relative_to(output_dir):
        raise ValueError("output directory must not be the runs directory or its ancestor")
    if expected_trials is not None and expected_trials < 1:
        raise ValueError("expected trials must be positive")
    trials = _trial_directories(runs_dir, output_dir)
    records: list[dict[str, Any]] = []
    validation_errors = [] if trials else [f"no Harbor trial traces found in {runs_dir}"]
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "events.jsonl").open("w", encoding="utf-8") as stream:
        for trial_dir in trials:
            result_path = trial_dir / "result.json"
            result = _object(result_path) if result_path.is_file() else {}
            history = ".retry-history" in trial_dir.parts
            job = next((parent for parent in trial_dir.parents
                        if any(parent.name.endswith("-" + profile) for profile in PROFILES)), trial_dir.parent)
            profile = next((profile for profile in PROFILES if job.name.endswith("-" + profile)), "unknown")
            trial_name = str(result.get("trial_name") or (trial_dir.parent.name if history else trial_dir.name))
            task = str(result.get("task_name") or trial_name.split("__", 1)[0])
            status = "failed" if result.get("exception_info") else "success" if result.get("finished_at") else "unknown"
            identity = {"task": task, "profile": profile, "trial": trial_name, "job": job.name,
                        "kind": "retry-history" if history else "current", "status": status,
                        "attempt": trial_dir.name if history else None}
            raw_dir = trial_dir / "agent" / "session-trace"
            trajectory_path = trial_dir / "agent" / "trajectory.json"
            details = {"trace_scope": "root-only", "trace_complete": False, "session_count": 1,
                       "warnings": ["Only the root trajectory is available; descendant content is not captured here."]}
            try:
                if not (raw_dir / "trace.json").is_file():
                    raise ValueError("raw session trace unavailable")
                body, events, details = _session_trace(raw_dir)
            except (ValueError, OSError, TypeError, KeyError) as error:
                if trajectory_path.is_file():
                    body, events = _trajectory(trajectory_path)
                    if (raw_dir / "trace.json").is_file():
                        details["warnings"].append(f"Raw trace could not be used; fell back to root trajectory: {error}")
                else:
                    body, events = ["No readable trajectory was captured.\n"], []
                    details.update(trace_scope="missing", session_count=0, warnings=[str(error)])
            if require_complete and not history:
                if details["trace_scope"] == "session-tree":
                    try:
                        verified = _verify_raw_trace(raw_dir, details["trace_manifest"])
                        details["trace_validation"] = verified
                        details["trace_complete"] = verified["complete"]
                        details["warnings"].extend(verified["errors"])
                    except (OSError, ValueError, TypeError, KeyError) as error:
                        details["trace_complete"] = False
                        details["warnings"].append(f"Trace validation failed: {error}")
                if status != "success" or not details["trace_complete"]:
                    validation_errors.append(f"{task}/{profile}/{trial_name}: successful execution and complete verified session trace required")
            counts = dict(Counter(event["category"] for event in events))
            key = hashlib.sha256(str(trial_dir.relative_to(runs_dir)).encode()).hexdigest()[:10]
            relative = Path(identity["kind"]) / _slug(task) / profile / f"{_slug(trial_name)}-{key}.md"
            target = output_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            record = {**identity, **details, "source": str(trial_dir), "markdown": relative.as_posix(),
                      "tool_calls": len(events), "categories": counts}
            records.append(record)
            preamble = [f"# {task} · {profile} · {trial_name}", "", _block({**identity, **details}, "json"),
                        "Outputs below preserve the content visible in the captured trace. Tool-level truncation is not reversed.", ""]
            target.write_text("\n".join(preamble + body), encoding="utf-8")
            for event in events:
                stream.write(json.dumps({"schema_version": 1, **identity, **event}, ensure_ascii=False) + "\n")
    counts = dict(Counter(f"{record['kind']}:{record['status']}" for record in records))
    current = [record for record in records if record["kind"] == "current"]
    if require_complete and not current:
        validation_errors.append("no current trials; archived attempts cannot satisfy the trace gate")
    if expected_trials is not None:
        tasks = {record["task"] for record in current}
        if not tasks:
            validation_errors.append("no current tasks found for expected trial count")
        for task in sorted(tasks):
            for profile in PROFILES:
                actual = sum(record["task"] == task and record["profile"] == profile for record in current)
                if actual != expected_trials:
                    validation_errors.append(f"{task}/{profile}: expected {expected_trials} current trials, found {actual}")
        for record in current:
            if record["profile"] not in PROFILES:
                validation_errors.append(f"{record['task']}/{record['trial']}: unrecognized profile")
    validation = {"require_complete": require_complete, "expected_trials_per_task_profile": expected_trials,
                  "passed": not validation_errors, "errors": validation_errors}
    index = {"schema_version": 1, "runs_dir": str(runs_dir), "counts": counts, "trials": records,
             "validation": validation}
    (output_dir / "index.json").write_text(_json(index) + "\n", encoding="utf-8")
    lines = ["# Search trace index", "", "Calls are recorded independently; no baseline/treatment call pairing is implied.",
             "Current trials and archived failed attempts are listed separately. Unknown or failed trials are not successful samples.", ""]
    lines.extend(["## Validation", "", _block(validation, "json")])
    for kind in ("current", "retry-history"):
        lines.extend([f"## {kind}", "", "| Task | Profile | Trial | Status | Scope | Complete | Tool calls |",
                      "|---|---|---|---|---|---|---:|"])
        for record in records:
            if record["kind"] != kind:
                continue
            label = str(record["trial"]).replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {_slug(record['task'])} | {record['profile']} | [{label}]({quote(record['markdown'])}) | "
                         f"{record['status']} | {record['trace_scope']} | {record['trace_complete']} | {record['tool_calls']} |")
        lines.append("")
    (output_dir / "index.md").write_text("\n".join(lines), encoding="utf-8")
    return index


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected-trials", type=int, help="Expected current trials per task and profile, excluding retry history")
    parser.add_argument("--require-complete", action="store_true", help="Require successful trials and revalidate complete raw session traces")
    args = parser.parse_args(argv)
    try:
        index = export_traces(runs_dir=args.runs_dir, output_dir=args.output_dir,
                              expected_trials=args.expected_trials, require_complete=args.require_complete)
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"output_dir": str(args.output_dir.resolve()), "counts": index["counts"],
                      "validation": index["validation"]}))
    return 0 if index["validation"]["passed"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
