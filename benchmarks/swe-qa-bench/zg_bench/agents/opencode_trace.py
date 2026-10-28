"""Capture the selected OpenCode session tree separately from usage accounting.

Only task sessions and their message/part rows are read. Never copy OpenCode's
database, auth files, or configuration. The existing artifact secret scan must
run on these uncompressed files before publishing them.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shlex
from typing import Any

TRACE_DIRECTORY = "session-trace"
TRACE_SCOPE = "opencode-session-tree-trace-v1"

# Node is already installed by the pinned OpenCode adapter. Query through its
# supported database CLI instead of adding a sqlite executable or Python module
# to each task container. Each query is restricted to the stdout root's tree.
_CAPTURE_SCRIPT = r"""
const fs = require('fs'), path = require('path'), cp = require('child_process'), os = require('os');
const crypto = require('crypto');
const directory = path.join(config.logsDir, 'session-trace');
fs.mkdirSync(directory, {recursive: true});
const trace = {schema_version: 1, scope: 'opencode-session-tree-trace-v1',
  root_session_id: null, sessions: [], messages: [], parts: [], overflow: [], errors: [],
  export_diagnostics: []};
function save() {
  const target = path.join(directory, 'trace.json');
  fs.writeFileSync(target + '.tmp', JSON.stringify(trace, null, 2) + '\n');
  fs.renameSync(target + '.tmp', target);
}
function decode(row) {
  if (typeof row.data === 'string') return {...row, data: JSON.parse(row.data)};
  return row;
}
function query(table) {
  const root = trace.root_session_id.replaceAll("'", "''");
  const column = table === 'session' ? 'id' : 'session_id';
  const sql = `WITH RECURSIVE selected(id) AS (
    SELECT id FROM session WHERE id = '${root}'
    UNION SELECT s.id FROM session s JOIN selected p ON s.parent_id = p.id
  ) SELECT * FROM ${table} WHERE ${column} IN (SELECT id FROM selected) ORDER BY id`;
  const diagnostic = {table, exit_code: null, error_code: null, signal: null,
    stdout_bytes: 0, stderr_bytes: 0, duration_ms: 0, stage: 'create_files'};
  trace.export_diagnostics.push(diagnostic);
  const started = Date.now();
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'zg-opencode-db-export-'));
  const stdoutPath = path.join(temporary, 'stdout.json'), stderrPath = path.join(temporary, 'stderr');
  let stdoutFd, stderrFd;
  try {
    stdoutFd = fs.openSync(stdoutPath, 'wx', 0o600);
    stderrFd = fs.openSync(stderrPath, 'wx', 0o600);
    diagnostic.stage = 'process';
    // OpenCode 1.18.4 may exit before a large piped console.log is flushed,
    // returning status 0 with truncated JSON. A regular-file stdout is written
    // synchronously by the CLI and also avoids spawnSync's maxBuffer limit.
    // Do not publish stderr: it can contain arbitrary provider/config details.
    const result = cp.spawnSync(config.opencode, ['db', sql, '--format', 'json'],
      {stdio: ['ignore', stdoutFd, stderrFd], timeout: 20000});
    diagnostic.exit_code = result.status;
    diagnostic.error_code = /^[A-Z0-9_]+$/.test(result.error?.code || '') ? result.error.code : null;
    diagnostic.signal = /^SIG[A-Z0-9]+$/.test(result.signal || '') ? result.signal : null;
    fs.closeSync(stdoutFd); stdoutFd = undefined;
    fs.closeSync(stderrFd); stderrFd = undefined;
    diagnostic.stdout_bytes = fs.statSync(stdoutPath).size;
    diagnostic.stderr_bytes = fs.statSync(stderrPath).size;
    if (result.error || result.status !== 0) throw new Error('db_export_failed:' + table);
    diagnostic.stage = 'parse_json';
    const rows = JSON.parse(fs.readFileSync(stdoutPath, 'utf8'));
    diagnostic.stage = 'validate_rows';
    if (!Array.isArray(rows)) throw new Error('invalid_db_rows:' + table);
    diagnostic.stage = 'decode_rows';
    const decoded = rows.map(decode);
    diagnostic.stage = 'complete';
    diagnostic.row_count = decoded.length;
    return decoded;
  } finally {
    diagnostic.duration_ms = Date.now() - started;
    if (stdoutFd !== undefined) fs.closeSync(stdoutFd);
    if (stderrFd !== undefined) fs.closeSync(stderrFd);
    // Unparsed provider stderr and temporary exports never enter artifacts.
    fs.rmSync(temporary, {recursive: true, force: true});
  }
}
function overflowPaths() {
  const found = new Set();
  function visit(value) {
    if (!value || typeof value !== 'object') return;
    for (const [key, item] of Object.entries(value)) {
      if (key === 'outputPath' && typeof item === 'string') found.add(item);
      else if (item && typeof item === 'object') visit(item);
    }
  }
  for (const row of trace.parts) {
    if (row.data?.type !== 'tool') continue;
    visit(row.data.state?.metadata);
    const output = row.data.state?.output;
    if (typeof output === 'string') {
      for (const match of output.matchAll(/Full output saved to: ([^\r\n]+)/g)) found.add(match[1].trim());
    }
  }
  return [...found].sort();
}
function copyOverflow(source) {
  const entry = {source_path: source, status: 'rejected'};
  trace.overflow.push(entry);
  // OpenCode 1.18.4 stores overflow under Global.Path.data/tool-output.
  const data = process.env.XDG_DATA_HOME || path.join(process.env.HOME, '.local', 'share');
  const base = path.resolve(data, 'opencode', 'tool-output');
  const relative = path.relative(base, path.resolve(source));
  if (!path.isAbsolute(source) || !relative || relative.startsWith('..') || path.isAbsolute(relative)) return;
  try {
    const stat = fs.lstatSync(source);
    if (!stat.isFile() || stat.isSymbolicLink()) return;
    const realBase = fs.realpathSync(base), realSource = fs.realpathSync(source);
    const realRelative = path.relative(realBase, realSource);
    if (!realRelative || realRelative.startsWith('..') || path.isAbsolute(realRelative)) return;
    const content = fs.readFileSync(realSource);
    const digest = crypto.createHash('sha256').update(content).digest('hex');
    const destination = 'overflow/' + digest + '.txt';
    fs.mkdirSync(path.join(directory, 'overflow'), {recursive: true});
    fs.writeFileSync(path.join(directory, destination), content);
    Object.assign(entry, {status: 'saved', relative_path: destination,
      sha256: digest, size_bytes: content.length});
  } catch (error) {
    entry.status = error.code === 'ENOENT' ? 'missing' : 'read_failed';
  }
}
try {
  const roots = new Set();
  const stdout = fs.readFileSync(path.join(config.logsDir, 'opencode.txt'), 'utf8');
  for (const line of stdout.split('\n')) {
    try { const event = JSON.parse(line); if (typeof event.sessionID === 'string' && event.sessionID) roots.add(event.sessionID); }
    catch (_) { /* OpenCode may interleave non-JSON diagnostics. */ }
  }
  if (roots.size !== 1) throw new Error('expected_one_root_session');
  trace.root_session_id = [...roots][0];
  for (const [table, name] of [['session', 'sessions'], ['message', 'messages'], ['part', 'parts']]) {
    try { trace[name] = query(table); }
    catch (_) { trace.errors.push('db_export_failed:' + table); }
    save(); // Preserve prior exports if a later query is interrupted.
  }
  for (const source of overflowPaths()) copyOverflow(source);
} catch (_) {
  trace.errors.push(trace.root_session_id ? 'trace_capture_failed' : 'missing_or_ambiguous_root_session');
} finally { save(); }
"""


def trace_capture_script(logs_dir: str = "/logs/agent", *, opencode: str = "opencode") -> str:
    """Return the production collector, also executable with a fixture CLI."""
    return "const config = " + json.dumps({"logsDir": logs_dir, "opencode": opencode}) + ";\n" + _CAPTURE_SCRIPT


def trace_capture_command() -> str:
    return "set -eo pipefail; . ~/.nvm/nvm.sh; node -e " + shlex.quote(trace_capture_script())


def _file_record(path: Path, relative: str) -> dict[str, Any]:
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    return {"path": relative, "sha256": digest, "size_bytes": path.stat().st_size}


def validate_trace_directory(
    directory: Path, *, execution_complete: bool, usage: dict[str, Any] | None = None,
    capture_error: str | None = None,
) -> dict[str, Any]:
    """Validate raw evidence and return a manifest; never repair missing rows.

    `complete` requires a completed execution, all referenced task sessions and
    tools, and any overflow files. Failed executions retain partial evidence but
    cannot pass the successful-trial gate. Usage, when supplied, cross-checks the
    independently collected session/message/tool identities.
    """
    errors: list[str] = []
    files: list[dict[str, Any]] = []
    if type(execution_complete) is not bool:
        execution_complete = False
        errors.append("invalid_execution_complete")
    if capture_error:
        errors.append("trace_capture_failed:" + capture_error)
    snapshot: dict[str, Any] = {}
    for filename in ("instruction.txt", "trace.json"):
        path = directory / filename
        try:
            if path.is_symlink() or not path.is_file():
                raise ValueError("missing regular file")
            files.append(_file_record(path, filename))
            if filename == "trace.json":
                snapshot = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(snapshot, dict):
                    snapshot = {}
                    raise ValueError("not an object")
            elif not path.read_text(encoding="utf-8").strip():
                errors.append("empty_instruction")
        except (OSError, ValueError):
            errors.append("missing_or_invalid_file:" + filename)
    if snapshot.get("schema_version") != 1 or snapshot.get("scope") != TRACE_SCOPE:
        errors.append("invalid_trace_schema")
    capture_errors = snapshot.get("errors")
    if not isinstance(capture_errors, list) or any(not isinstance(x, str) for x in capture_errors):
        errors.append("invalid_capture_errors")
    else:
        errors.extend(capture_errors)
    root = snapshot.get("root_session_id")
    tables: dict[str, dict[str, dict[str, Any]]] = {}
    for name in ("sessions", "messages", "parts"):
        rows = snapshot.get(name)
        tables[name] = {}
        if not isinstance(rows, list):
            errors.append("missing_rows:" + name)
            continue
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
                errors.append("invalid_row:" + name)
                continue
            if row["id"] in tables[name]:
                errors.append("duplicate_row:" + name + ":" + row["id"])
            tables[name][row["id"]] = row
            if name != "sessions" and not isinstance(row.get("data"), dict):
                errors.append("invalid_data:" + name + ":" + row["id"])
    sessions, messages, parts = (tables[name] for name in ("sessions", "messages", "parts"))
    selected: set[str] = set()
    if not isinstance(root, str) or root not in sessions:
        errors.append("missing_root_session")
    else:
        pending = [root]
        while pending:
            sid = pending.pop()
            if sid in selected:
                errors.append("session_cycle:" + sid)
                continue
            selected.add(sid)
            pending.extend(s["id"] for s in sessions.values() if s.get("parent_id") == sid)
        if sessions[root].get("parent_id"):
            errors.append("root_has_parent")
    if set(sessions) != selected:
        errors.append("sessions_outside_selected_tree")
    for mid, message in messages.items():
        if message.get("session_id") not in selected:
            errors.append("message_outside_tree:" + mid)
    for pid, part in parts.items():
        message = messages.get(part.get("message_id"), {})
        if not message or message.get("session_id") != part.get("session_id"):
            errors.append("missing_or_mismatched_part_message:" + pid)
        if part.get("session_id") not in selected:
            errors.append("part_outside_tree:" + pid)
        data = part.get("data")
        if not isinstance(data, dict):
            continue
        if data.get("type") == "tool":
            state = data.get("state")
            if not isinstance(state, dict):
                errors.append("missing_tool_state:" + pid)
                continue
            if "input" not in state:
                errors.append("missing_tool_input:" + pid)
            if state.get("status") not in ("completed", "error"):
                errors.append("unfinished_tool:" + pid)
            if state.get("status") == "completed" and "output" not in state:
                errors.append("missing_tool_output:" + pid)
            metadata = state.get("metadata") or {}
            if data.get("tool") == "task" and state.get("status") == "completed":
                child = metadata.get("sessionId") if isinstance(metadata, dict) else None
                if not child or child not in selected:
                    errors.append("missing_task_child:" + pid)
    session_details = []
    for sid in sorted(selected):
        own_messages = {mid: row for mid, row in messages.items() if row.get("session_id") == sid}
        own_parts = {pid: row for pid, row in parts.items() if row.get("session_id") == sid}
        assistants = {mid for mid, row in own_messages.items() if isinstance(row.get("data"), dict) and row["data"].get("role") == "assistant"}
        tools = {pid for pid, row in own_parts.items() if isinstance(row.get("data"), dict) and row["data"].get("type") == "tool"}
        if not assistants:
            errors.append("missing_session_messages:" + sid)
        for mid in own_messages:
            if not any(row.get("message_id") == mid for row in own_parts.values()):
                errors.append("missing_message_parts:" + mid)
        for mid in assistants:
            data = own_messages[mid]["data"]
            if not isinstance(data.get("time"), dict) or not data["time"].get("completed"):
                errors.append("unfinished_message:" + mid)
            if data.get("error"):
                errors.append("message_error:" + mid)
        session_details.append({"session_id": sid, "parent_session_id": sessions[sid].get("parent_id"),
            "message_ids": sorted(own_messages), "assistant_message_ids": sorted(assistants),
            "part_ids": sorted(own_parts), "tool_ids": sorted(tools)})
    for role, missing in (("user", "missing_root_question"), ("assistant", "missing_root_answer")):
        if not any(
            row.get("session_id") == root
            and isinstance(row.get("data"), dict)
            and row["data"].get("type") == "text"
            and isinstance(row["data"].get("text"), str)
            and row["data"]["text"].strip()
            and messages.get(row.get("message_id"), {}).get("data", {}).get("role") == role
            for row in parts.values()
        ):
            errors.append(missing)
    if usage is not None:
        expected = {row["session_id"]: row for row in usage.get("sessions", []) if isinstance(row, dict) and isinstance(row.get("session_id"), str)}
        if set(expected) != selected or usage.get("root_session_id") != root:
            errors.append("usage_session_mismatch")
        for session in session_details:
            other = expected.get(session["session_id"], {})
            if set(session["assistant_message_ids"]) != set(other.get("message_ids", [])):
                errors.append("usage_message_mismatch:" + session["session_id"])
            if set(session["tool_ids"]) != set(other.get("tool_ids", [])):
                errors.append("usage_tool_mismatch:" + session["session_id"])
        if usage.get("complete") is not True:
            errors.append("session_usage_incomplete")
    overflow = snapshot.get("overflow", [])
    if not isinstance(overflow, list):
        overflow = []
        errors.append("invalid_overflow_manifest")
    for index, item in enumerate(overflow):
        if not isinstance(item, dict) or item.get("status") != "saved":
            errors.append(f"overflow_not_saved:{index}")
            continue
        relative = item.get("relative_path")
        if not isinstance(relative, str) or not relative.startswith("overflow/"):
            errors.append(f"invalid_overflow_path:{index}")
            continue
        target = directory / relative
        try:
            if target.is_symlink() or not target.is_file() or not target.resolve().is_relative_to((directory / "overflow").resolve()):
                raise ValueError("invalid overflow file")
            record = _file_record(target, relative)
            if record["sha256"] != item.get("sha256") or record["size_bytes"] != item.get("size_bytes"):
                raise ValueError("overflow checksum mismatch")
            if record not in files:
                files.append(record)
        except (OSError, ValueError):
            errors.append(f"missing_or_invalid_overflow:{index}")
    capture_complete = not errors
    if not execution_complete:
        errors.append("execution_incomplete")
    return {"schema_version": 1, "scope": TRACE_SCOPE, "root_session_id": root,
        "capture_error": capture_error,
        "execution_complete": execution_complete, "capture_complete": capture_complete,
        "complete": not errors, "errors": sorted(set(errors)), "sessions": session_details,
        "counts": {"sessions": len(sessions), "messages": len(messages), "parts": len(parts),
            "tool_calls": sum(len(s["tool_ids"]) for s in session_details), "overflow_files": len(overflow)},
        "files": sorted(files, key=lambda item: item["path"])}
