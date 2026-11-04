#!/usr/bin/env bash
# Manual gates G1 to G3 for Phase 2. Usage: scripts/gate.sh <task-id>
#
# Prints one line of JSON. Exit codes: 0 passed, 1 a gate failed, 64 bad usage or
# task setup, 70 internal error. Full tool output goes to logs/<id>.gate.log.
#
# ORCH_RUNTIME overrides the runtime directory (default ~/.orch/orchestrator).
#
# The system Python may lack tomllib, so uv supplies the interpreter.
set -euo pipefail
exec uv run --no-project --python 3.14 python -I - "$@" <<'PY'
import json
import os
import re
import signal
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any, NoReturn

RESULT_FILE = ".orch-result.json"
COMMAND_TIMEOUT_SEC = 15 * 60
DETAIL_LIMIT = 500

# Each gate's result, reported in the output JSON.
GATES = {"G1": "not_run", "G2": "not_run", "G3": "not_run"}

# Files that sub-agents may never change, whatever the task's modify list says.
TEST_GLOBS = ("tests/**",)
SHARED_GLOBS = (
    "pyproject.toml",
    "uv.lock",
    ".gitignore",
    "AGENTS.md",
    "CLAUDE.md",
    ".claude/**",
    ".github/**",
    "docs/**",
    "schemas/**",
    "scripts/**",
    "templates/**",
)


def emit(payload: dict[str, Any], code: int) -> NoReturn:
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(code)


def usage_error(error: str, message: str) -> NoReturn:
    emit({"ok": False, "error": error, "message": message}, 64)


def gate_failure(gate: str, detail: str, log_path: Path | None = None) -> NoReturn:
    GATES[gate] = "failed"
    payload: dict[str, Any] = {
        "ok": True,
        "passed": False,
        "gates": GATES,
        "failed_gate": gate,
        "detail": detail[:DETAIL_LIMIT],
    }
    if log_path is not None:
        payload["log_path"] = str(log_path)
    emit(payload, 1)


def glob_to_regex(glob: str) -> re.Pattern[str]:
    """Translate a path glob: ** crosses directories, * and ? do not."""
    out = ""
    i = 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif glob.startswith("**", i):
            out += ".*"
            i += 2
        elif glob[i] == "*":
            out += "[^/]*"
            i += 1
        elif glob[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(glob[i])
            i += 1
    return re.compile(out)


def matches_any(path: str, globs: tuple[str, ...] | list[str]) -> bool:
    return any(glob_to_regex(g).fullmatch(path) for g in globs)


def git(worktree: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(worktree), *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout


def z_paths(output: str) -> set[str]:
    return {p for p in output.split("\0") if p}


def load_task(runtime: Path, task_id: str) -> dict[str, Any]:
    path = runtime / "tasks" / f"{task_id}.md"
    if not path.is_file():
        usage_error("task_not_found", f"지시서가 없습니다: {path}")
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "+++" or "+++" not in map(str.strip, lines[1:]):
        usage_error("bad_front_matter", "지시서 맨 앞에 +++ 로 감싼 TOML 블록이 없습니다.")
    end = [line.strip() for line in lines[1:]].index("+++") + 1
    try:
        task = tomllib.loads("\n".join(lines[1:end]))
    except tomllib.TOMLDecodeError as exc:
        usage_error("bad_front_matter", f"TOML 파싱 실패: {exc}")
    if task.get("id") != task_id:
        usage_error("bad_front_matter", "앞머리의 id가 인자와 다릅니다.")
    modify = task.get("modify")
    if (
        not isinstance(modify, list)
        or not modify
        or not all(isinstance(g, str) and g for g in modify)
    ):
        usage_error("bad_front_matter", "modify는 비어 있지 않은 문자열 목록이어야 합니다.")
    if not isinstance(task.get("verify"), str) or not task["verify"].strip():
        usage_error("bad_front_matter", "verify 명령이 없습니다.")
    tests = task.get("tests")
    if tests is not None and (tests != "none" or task.get("grade") != "L1"):
        usage_error("bad_front_matter", 'tests는 L1 작업에서 "none"만 허용됩니다.')
    return task


def task_tip(worktree: Path) -> str:
    """Return the task's last commit, looking through a temporary merge of origin/main."""
    parents = git(worktree, "rev-list", "--parents", "-n", "1", "HEAD").split()
    main = git(worktree, "rev-parse", "origin/main").strip()
    if len(parents) == 3 and parents[2] == main:
        return parents[1]
    return "HEAD"


def check_scope(worktree: Path, task: dict[str, Any]) -> None:
    """G1: compare everything since the test commit with the allowed paths."""
    tip = task_tip(worktree)
    base = git(worktree, "merge-base", "origin/main", tip).strip()
    commits = git(worktree, "rev-list", "--reverse", f"{base}..{tip}").split()
    if task.get("tests") == "none":
        compare = base
    elif commits:
        compare = commits[0]
    else:
        gate_failure("G1", "테스트 커밋이 없습니다.")

    # A temporary merge brings in main's files, which are not the task's changes.
    if tip == "HEAD":
        changed = z_paths(git(worktree, "diff", "--name-only", "--no-renames", "-z", compare))
    else:
        changed = z_paths(
            git(worktree, "diff", "--name-only", "--no-renames", "-z", compare, tip)
        )
        changed |= z_paths(git(worktree, "diff", "--name-only", "--no-renames", "-z", "HEAD"))
    changed |= z_paths(git(worktree, "ls-files", "--others", "--exclude-standard", "-z"))
    changed.discard(RESULT_FILE)

    violations = {
        "test_file_changed": sorted(p for p in changed if matches_any(p, TEST_GLOBS)),
        "shared_file_changed": sorted(p for p in changed if matches_any(p, SHARED_GLOBS)),
        "out_of_scope": sorted(p for p in changed if not matches_any(p, task["modify"])),
    }
    found = {name: paths for name, paths in violations.items() if paths}
    if found:
        gate_failure("G1", json.dumps(found, ensure_ascii=False))


def run_logged(
    command: list[str], worktree: Path, log_path: Path, env: dict[str, str]
) -> int | None:
    """Run a command in its own process group. Returns None on timeout."""
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n$ {' '.join(command)}\n")
        log.flush()
        proc = subprocess.Popen(
            command,
            cwd=worktree,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            return proc.wait(timeout=COMMAND_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            return None


def run_gate(
    gate: str,
    command: list[str],
    worktree: Path,
    log_path: Path,
    env: dict[str, str],
    allowed: tuple[int, ...] = (0,),
) -> None:
    code = run_logged(command, worktree, log_path, env)
    if code is None:
        gate_failure(gate, f"시간 초과: {' '.join(command)}", log_path)
    if code not in allowed:
        gate_failure(gate, f"종료 코드 {code}: {' '.join(command)}", log_path)


def main() -> None:
    if len(sys.argv) != 2 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", sys.argv[1]):
        usage_error("usage", "사용법: scripts/gate.sh <task-id>")
    task_id = sys.argv[1]
    runtime = Path(os.environ.get("ORCH_RUNTIME", "~/.orch/orchestrator")).expanduser()
    task = load_task(runtime, task_id)
    worktree = runtime / "worktrees" / task_id
    if not worktree.is_dir():
        usage_error("worktree_not_found", f"워크트리가 없습니다: {worktree}")

    log_dir = runtime / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{task_id}.gate.log"
    log_path.write_text("", encoding="utf-8")

    # Do not let the interpreter uv chose for this script leak into the worktree's tools.
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}

    check_scope(worktree, task)
    GATES["G1"] = "passed"

    # pytest exits 5 when it collects nothing, which only tests = "none" may do.
    pytest_ok = (0, 5) if task.get("tests") == "none" else (0,)
    run_gate("G2", ["uv", "run", "--locked", "pytest"], worktree, log_path, env, pytest_ok)
    run_gate("G2", ["bash", "-c", task["verify"]], worktree, log_path, env)
    GATES["G2"] = "passed"

    run_gate("G3", ["uv", "run", "--locked", "ruff", "check", "."], worktree, log_path, env)
    run_gate(
        "G3", ["uv", "run", "--locked", "ruff", "format", "--check", "."], worktree, log_path, env
    )

    GATES["G3"] = "passed"

    emit({"ok": True, "passed": True, "gates": GATES, "log_path": str(log_path)}, 0)


try:
    main()
except RuntimeError as exc:
    emit({"ok": False, "error": "internal", "message": str(exc)[:DETAIL_LIMIT]}, 70)
PY
