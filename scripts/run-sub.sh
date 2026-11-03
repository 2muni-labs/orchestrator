#!/usr/bin/env bash
# Temporary stand-in for `orch run` until M6. Usage: scripts/run-sub.sh <task-id>
#
# Runs lane B (Codex) from the task spec's front matter, enforces timeout_min, and
# records the result in results/<id>.json. Start it in a background shell.
#
# Prints one line of JSON. Exit codes follow the status: 0 DONE, 10 PARTIAL,
# 20 FAILED, 30 BLOCKED, 40 BLOCKED_QUOTA, 64 bad usage or task setup.
#
# A missing or invalid result becomes BLOCKED_QUOTA if the end of the log shows a
# usage limit message, and FAILED/report_missing otherwise.
#
# ORCH_RUNTIME overrides the runtime directory (default ~/.orch/orchestrator).
# ORCH_CODEX overrides the Codex binary, for tests only.
set -euo pipefail
exec uv run --no-project --python 3.14 python -I - "$@" <<'PY'
import json
import os
import re
import signal
import subprocess
import sys
import time
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn

CODEX = "~/.local/share/mise/installs/codex/0.160.0/bin/codex"
RESULT_FILE = ".orch-result.json"
REMOVED_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "VIRTUAL_ENV")
TERM_GRACE_SEC = 10
SUMMARY_LIMIT = 700

# Best guesses: the real `codex exec` message is unconfirmed. Add it here when first seen.
USAGE_LIMIT = re.compile(
    r"usage limit|rate limit|hit your .*limit|limit reached|quota|out of credits", re.IGNORECASE
)
LOG_TAIL_LINES = 20

EFFORTS = {"low", "medium", "high", "xhigh"}
STATUS_EXIT = {"DONE": 0, "PARTIAL": 10, "FAILED": 20, "BLOCKED": 30, "BLOCKED_QUOTA": 40}
FAILURE_REASONS = {
    None,
    "report_missing",
    "timeout",
    "killed",
    "aborted",
    "spec_mismatch",
    "out_of_scope",
}
RESULT_KEYS = {
    "task_id",
    "status",
    "failure_reason",
    "summary",
    "changed_paths",
    "out_of_scope_changes",
    "verification",
    "unresolved",
    "log_path",
}


def usage_error(error: str, message: str) -> NoReturn:
    print(json.dumps({"ok": False, "error": error, "message": message}, ensure_ascii=False))
    sys.exit(64)


def load_task(path: Path, task_id: str) -> dict[str, Any]:
    if not path.is_file():
        usage_error("task_not_found", f"지시서가 없습니다: {path}")
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    if not lines or lines[0] != "+++" or "+++" not in lines[1:]:
        usage_error("bad_front_matter", "지시서 맨 앞에 +++ 로 감싼 TOML 블록이 없습니다.")
    end = lines[1:].index("+++") + 1
    raw = path.read_text(encoding="utf-8").splitlines()[1:end]
    try:
        task = tomllib.loads("\n".join(raw))
    except tomllib.TOMLDecodeError as exc:
        usage_error("bad_front_matter", f"TOML 파싱 실패: {exc}")
    if task.get("id") != task_id:
        usage_error("bad_front_matter", "앞머리의 id가 인자와 다릅니다.")
    if task.get("lane") != "B":
        usage_error("unsupported_lane", "run-sub.sh는 B 레인만 실행합니다.")
    if not isinstance(task.get("model"), str) or not task["model"]:
        usage_error("bad_front_matter", "model이 없습니다.")
    if task.get("effort") not in EFFORTS:
        usage_error("bad_front_matter", "effort는 low, medium, high, xhigh 중 하나여야 합니다.")
    minutes = task.get("timeout_min")
    if isinstance(minutes, bool) or not isinstance(minutes, int | float) or minutes <= 0:
        usage_error("bad_front_matter", "timeout_min은 양수여야 합니다.")
    return task


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def descendants(root: int) -> set[int]:
    """Find a process's descendants, which may have left its process group."""
    table = subprocess.run(
        ["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, stdin=subprocess.DEVNULL
    ).stdout
    children: dict[int, list[int]] = {}
    for line in table.splitlines():
        pid, ppid = map(int, line.split())
        children.setdefault(ppid, []).append(pid)
    found: set[int] = set()
    pending = [root]
    while pending:
        for child in children.get(pending.pop(), []):
            found.add(child)
            pending.append(child)
    return found


def send(pids: set[int], pgid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass
    for pid in pids:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


def stop(proc: subprocess.Popen[bytes]) -> None:
    """Stop the sub-agent and its tool commands. Codex runs those in their own sessions."""
    pids = descendants(proc.pid)  # Taken first: orphans cannot be found once Codex is gone.
    send(pids, proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=TERM_GRACE_SEC)
    except subprocess.TimeoutExpired:
        pass
    time.sleep(1)  # Let tool commands act on SIGTERM before checking what is left.
    send(pids, proc.pid, signal.SIGKILL)


def run_codex(task: dict[str, Any], spec: Path, worktree: Path, log_path: Path) -> tuple[str, int]:
    """Run Codex in its own process group. Returns the end reason and exit code."""
    codex = os.environ.get("ORCH_CODEX") or str(Path(CODEX).expanduser())
    command = [
        codex,
        "exec",
        "-C",
        str(worktree),
        "-m",
        task["model"],
        "-c",
        f"model_reasoning_effort={task['effort']}",
        "--sandbox",
        "workspace-write",
        "-",
    ]
    env = {k: v for k, v in os.environ.items() if k not in REMOVED_ENV}
    with spec.open("rb") as stdin, log_path.open("ab") as log:
        proc = subprocess.Popen(
            command,
            cwd=worktree,
            env=env,
            stdin=stdin,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    # The child has its own session, so a signal to this script does not reach it.
    def forward(_signum: int, _frame: object) -> None:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        stop(proc)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)

    try:
        code = proc.wait(timeout=task["timeout_min"] * 60)
    except subprocess.TimeoutExpired:
        stop(proc)
        return "timeout", proc.wait()
    return ("signal" if code < 0 else "normal"), code


def is_str_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def result_error(data: object, task_id: str) -> str | None:
    """Return why a result report is invalid, or None if it is valid."""
    if not isinstance(data, dict):
        return "JSON 객체가 아닙니다"
    if set(data) != RESULT_KEYS:
        return f"키가 규격과 다릅니다: {sorted(set(data) ^ RESULT_KEYS)}"
    if data["task_id"] != task_id:
        return "task_id가 다릅니다"
    status, reason = data["status"], data["failure_reason"]
    if not isinstance(status, str) or status not in STATUS_EXIT:
        return "status 값이 틀립니다"
    if not (reason is None or isinstance(reason, str)) or reason not in FAILURE_REASONS:
        return "failure_reason 값이 틀립니다"
    if (status in ("FAILED", "BLOCKED")) != (reason is not None):
        return "failure_reason은 FAILED와 BLOCKED에서만 채웁니다"
    if not isinstance(data["summary"], str) or not is_str_list(data["unresolved"]):
        return "summary 또는 unresolved 형식이 틀립니다"
    if len(data["summary"]) + sum(map(len, data["unresolved"])) > SUMMARY_LIMIT:
        return f"summary와 unresolved 합계가 {SUMMARY_LIMIT}자를 넘습니다"
    if not is_str_list(data["changed_paths"]) or not is_str_list(data["out_of_scope_changes"]):
        return "changed_paths 또는 out_of_scope_changes 형식이 틀립니다"
    verification = data["verification"]
    if (
        not isinstance(verification, dict)
        or set(verification) != {"command", "passed"}
        or not isinstance(verification["command"], str)
        or not isinstance(verification["passed"], bool)
    ):
        return "verification 형식이 틀립니다"
    if not isinstance(data["log_path"], str):
        return "log_path 형식이 틀립니다"
    return None


def hit_usage_limit(log_path: Path, spec: Path) -> bool:
    """Look for a limit message at the end of the log, ignoring the echoed task spec."""
    echoed = set(spec.read_text(encoding="utf-8").splitlines())
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return any(
        USAGE_LIMIT.search(line) for line in lines[-LOG_TAIL_LINES:] if line not in echoed
    )


def write_atomic(path: Path, data: dict[str, Any]) -> None:
    temp = path.with_name(f"{path.name}.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def main() -> None:
    if len(sys.argv) != 2 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", sys.argv[1]):
        usage_error("usage", "사용법: scripts/run-sub.sh <task-id>")
    task_id = sys.argv[1]
    runtime = Path(os.environ.get("ORCH_RUNTIME", "~/.orch/orchestrator")).expanduser()
    spec = runtime / "tasks" / f"{task_id}.md"
    task = load_task(spec, task_id)
    worktree = runtime / "worktrees" / task_id
    if not worktree.is_dir():
        usage_error("worktree_not_found", f"워크트리가 없습니다: {worktree}")

    logs = runtime / "logs"
    results = runtime / "results"
    logs.mkdir(parents=True, exist_ok=True)
    results.mkdir(parents=True, exist_ok=True)
    log_path = logs / f"{task_id}.log"
    result_path = results / f"{task_id}.json"
    worktree_result = worktree / RESULT_FILE
    worktree_result.unlink(missing_ok=True)  # A leftover must not count as this run's report.

    def log_line(text: str) -> None:
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"[run-sub] {text}\n")

    log_line(f"start {now()} model={task['model']} effort={task['effort']}")
    started = time.monotonic()
    reason, code = run_codex(task, spec, worktree, log_path)
    elapsed = round(time.monotonic() - started)
    log_line(f"end {now()} elapsed_sec={elapsed} reason={reason} exit={code}")

    failure = {"timeout": "timeout", "signal": "killed"}.get(reason)
    summary = f"실행이 {reason}로 끝났습니다."
    status = "FAILED"
    report_valid = False
    if failure is None:
        try:
            error = result_error(json.loads(worktree_result.read_text(encoding="utf-8")), task_id)
        except (OSError, ValueError) as exc:
            error = f"결과 파일을 읽지 못했습니다: {type(exc).__name__}"
        if error is None:
            report_valid = True
        elif hit_usage_limit(log_path, spec):
            status, summary = "BLOCKED_QUOTA", "로그에서 사용량 한도 문구를 감지했습니다."
        else:
            failure, summary = "report_missing", f"결과 보고가 없거나 형식이 틀립니다: {error}"

    if report_valid:
        os.replace(worktree_result, result_path)
    else:
        if worktree_result.exists():
            os.replace(worktree_result, logs / f"{task_id}.rejected-result.json")
        write_atomic(
            result_path,
            {
                "task_id": task_id,
                "status": status,
                "failure_reason": failure,
                "summary": summary[:SUMMARY_LIMIT],
                "changed_paths": [],
                "out_of_scope_changes": [],
                "verification": {"command": str(task.get("verify", "")), "passed": False},
                "unresolved": [],
                "log_path": str(log_path),
            },
        )

    status = json.loads(result_path.read_text(encoding="utf-8"))["status"]
    log_line(f"result status={status} failure_reason={failure}")
    print(
        json.dumps(
            {
                "ok": True,
                "status": status,
                "failure_reason": failure,
                "end_reason": reason,
                "elapsed_sec": elapsed,
                "result_path": str(result_path),
                "log_path": str(log_path),
            },
            ensure_ascii=False,
        )
    )
    sys.exit(STATUS_EXIT[status])


main()
PY
