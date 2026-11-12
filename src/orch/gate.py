"""Gates G1 scope, G2 tests and G3 static checks, and the orch gate flow."""

import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

from orch import gitops, store
from orch import sandbox as gate_sandbox
from orch.config import GATE_TIMEOUT_DEFAULT_MIN, Target
from orch.lanes.base import child_env
from orch.models import Gate, Grade, Outcome, TaskState
from orch.taskfile import TaskSpec

RESULT_FILE = ".orch-result.json"
COMMAND_TIMEOUT_SEC = GATE_TIMEOUT_DEFAULT_MIN * 60
DETAIL_LIMIT = 500
TEST_GLOBS = ("tests/**",)


class GateError(Exception):
    """The gates cannot run for this task as it stands."""


class GateStatus(StrEnum):
    """What happened to one gate."""

    NOT_RUN = "not_run"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True, kw_only=True)
class ScopeViolations:
    """Changed paths that a task may not touch, sorted."""

    test_file_changed: tuple[str, ...] = ()
    shared_file_changed: tuple[str, ...] = ()
    out_of_scope: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(
            self.test_file_changed or self.shared_file_changed or self.out_of_scope
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class GateReport:
    """The outcome of running the gates."""

    passed: bool
    gates: Mapping[str, GateStatus] = field(default_factory=dict)
    failed_gate: str | None = None
    detail: str | None = None
    log_path: str | None = None


def glob_matches(path: str, glob: str) -> bool:
    """Match a path: ** crosses directories, * and ? do not. The whole path counts."""
    if "\n" in path:
        return False
    pattern = ""
    index = 0
    while index < len(glob):
        if glob.startswith("**/", index):
            pattern += "(?:.*/)?"
            index += 3
        elif glob.startswith("**", index):
            pattern += ".*"
            index += 2
        elif glob[index] == "*":
            pattern += "[^/]*"
            index += 1
        elif glob[index] == "?":
            pattern += "[^/]"
            index += 1
        else:
            pattern += re.escape(glob[index])
            index += 1
    return re.fullmatch(pattern, path) is not None


def scope_violations(
    changed: Iterable[str],
    *,
    modify: Iterable[str],
    shared_paths: Iterable[str],
) -> ScopeViolations:
    """Compare changed paths with the allowed paths. The result file does not count."""
    paths = sorted(path for path in changed if path != RESULT_FILE)
    allowed = tuple(modify)
    shared = tuple(s.removesuffix("/**").removesuffix("/") for s in shared_paths)
    return ScopeViolations(
        test_file_changed=tuple(
            p for p in paths if any(glob_matches(p, g) for g in TEST_GLOBS)
        ),
        shared_file_changed=tuple(
            p
            for p in paths
            if any(glob_matches(p, s) or glob_matches(p, s + "/**") for s in shared)
        ),
        out_of_scope=tuple(
            p for p in paths if not any(glob_matches(p, g) for g in allowed)
        ),
    )


def check_scope(
    worktree: Path,
    *,
    since: str,
    main_ref: str,
    modify: Iterable[str],
    shared_paths: Iterable[str],
) -> ScopeViolations:
    """Run the scope comparison on a worktree, looking through a temporary merge."""
    return scope_violations(
        gitops.task_changed_paths(worktree, since, main_ref),
        modify=modify,
        shared_paths=shared_paths,
    )


def run_command(
    command: str,
    worktree: Path,
    log_path: Path,
    *,
    timeout_sec: int | None = None,
    sandbox: bool = True,
) -> int | None:
    """Run a shell command in its own group, logging output. None means a timeout.

    With sandbox (the default) the command runs under sandbox.PROFILE with a new
    TMPDIR, after a start check that raises sandbox.SandboxError on failure.
    """
    tmpdir = None
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"$ {command}\n")
            log.flush()
            argv = ["bash", "-c", command]
            env = child_env(os.environ)
            if sandbox:
                tmpdir = Path(tempfile.mkdtemp(prefix="orch-gate-")).resolve()
                home = Path.home().resolve()
                exe = gate_sandbox.executable()
                try:
                    gate_sandbox.check(
                        executable=exe, worktree=worktree, tmpdir=tmpdir, home=home
                    )
                except gate_sandbox.SandboxError as exc:
                    log.write(f"샌드박스 실패: {exc}\n")
                    raise
                env["TMPDIR"] = str(tmpdir)
                argv = gate_sandbox.wrap(
                    argv, executable=exe, worktree=worktree, tmpdir=tmpdir, home=home
                )
            process = subprocess.Popen(
                argv,
                cwd=worktree,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                code = process.wait(
                    timeout=COMMAND_TIMEOUT_SEC if timeout_sec is None else timeout_sec
                )
            except subprocess.TimeoutExpired:
                code = None
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError, PermissionError:
                    pass
                process.wait()
            return code
    finally:
        if tmpdir is not None:
            shutil.rmtree(tmpdir)


def _command_failure(command: str, code: int | None) -> str | None:
    if code is None:
        return f"시간 초과: {command}"[:DETAIL_LIMIT]
    if code != 0:
        return f"종료 코드 {code}: {command}"[:DETAIL_LIMIT]
    return None


def run_tests(
    spec: TaskSpec,
    target: Target,
    worktree: Path,
    log_path: Path,
    *,
    timeout_sec: int | None = None,
) -> str | None:
    """Run the G2 commands and return a failure summary, or None if they pass."""
    for command, allow_empty in (
        (target.test_command, spec.tests == "none"),
        (spec.verify, False),
    ):
        try:
            code = run_command(command, worktree, log_path, timeout_sec=timeout_sec)
        except gate_sandbox.SandboxError as exc:
            return f"샌드박스 실패: {exc}"[:DETAIL_LIMIT]
        if code == 5 and allow_empty:
            continue
        failure = _command_failure(command, code)
        if failure is not None:
            return failure
    return None


def run_gates(
    spec: TaskSpec,
    target: Target,
    worktree: Path,
    *,
    since: str,
    main_ref: str,
    log_path: Path,
    timeout_sec: int | None = None,
) -> GateReport:
    """Run G1, G2 and G3, stopping at a failure, then check the scope again."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("", encoding="utf-8")
    gates = dict.fromkeys(("G1", "G2", "G3"), GateStatus.NOT_RUN)

    def scope_failure() -> str | None:
        violations = check_scope(
            worktree,
            since=since,
            main_ref=main_ref,
            modify=spec.modify,
            shared_paths=target.shared_paths,
        )
        if violations:
            found = {key: paths for key, paths in asdict(violations).items() if paths}
            return json.dumps(found, ensure_ascii=False)[:DETAIL_LIMIT]
        return None

    def failed(name: str, detail: str) -> GateReport:
        gates[name] = GateStatus.FAILED
        return GateReport(
            passed=False,
            gates=gates,
            failed_gate=name,
            detail=detail,
            log_path=str(log_path),
        )

    detail = scope_failure()
    if detail is not None:
        return failed("G1", detail)
    gates["G1"] = GateStatus.PASSED

    detail = run_tests(spec, target, worktree, log_path, timeout_sec=timeout_sec)
    if detail is not None:
        return failed("G2", detail)
    gates["G2"] = GateStatus.PASSED

    if not target.lint_command.strip():
        gates["G3"] = GateStatus.SKIPPED
    else:
        try:
            code = run_command(
                target.lint_command, worktree, log_path, timeout_sec=timeout_sec
            )
        except gate_sandbox.SandboxError as exc:
            return failed("G3", f"샌드박스 실패: {exc}"[:DETAIL_LIMIT])
        detail = _command_failure(target.lint_command, code)
        if detail is not None:
            return failed("G3", detail)
        gates["G3"] = GateStatus.PASSED

    detail = scope_failure()
    if detail is not None:
        return failed("G1", detail)
    return GateReport(passed=True, gates=gates, log_path=str(log_path))


def review_required(grade: Grade, *, flag: bool = False) -> bool:
    """Return whether a passing task of this grade goes to cross-review."""
    return grade in (Grade.L3, Grade.L3R) or flag


def gate_task(
    conn: sqlite3.Connection,
    spec: TaskSpec,
    target: Target,
    *,
    main_ref: str,
    log_path: Path,
    home: Path,
    orch_home: Path,
    timeout_sec: int | None = None,
) -> GateReport:
    """Run the gates on a judged task, record them, and commit on success."""
    task = store.get_task(conn, spec.id)
    if task is None:
        raise GateError(f"Task not found: {spec.id}")
    if task.state != TaskState.JUDGING:
        raise GateError(f"Task must be judging: {spec.id}")
    run = store.latest_run(conn, spec.id)
    if run is None or run.outcome != Outcome.DONE:
        raise GateError(f"Task must have a DONE run: {spec.id}")
    if not task.worktree or not task.test_sha:
        raise GateError(f"Task needs a worktree and test commit: {spec.id}")

    store.transition(conn, spec.id, TaskState.JUDGING, TaskState.GATING, actor="orch")
    try:
        store.clear_gate_results(
            conn, spec.id, task.attempt, (Gate.G1, Gate.G2, Gate.G3)
        )
        worktree = Path(task.worktree)
        report = run_gates(
            spec,
            target,
            worktree,
            since=task.test_sha,
            main_ref=main_ref,
            log_path=log_path,
            timeout_sec=timeout_sec,
        )
        for name in ("G1", "G2", "G3"):
            status = report.gates[name]
            if status == GateStatus.NOT_RUN:
                continue
            detail = None
            if status == GateStatus.SKIPPED:
                detail = "skipped"
            elif status == GateStatus.FAILED:
                detail = report.detail
            store.record_gate_result(
                conn,
                spec.id,
                task.attempt,
                Gate(name),
                passed=status != GateStatus.FAILED,
                detail=detail,
            )
        if not report.passed:
            store.transition(
                conn, spec.id, TaskState.GATING, TaskState.JUDGING, actor="orch"
            )
            return report

        paths = sorted(gitops.changed_paths(worktree, "HEAD") - {RESULT_FILE})
        if paths:
            gitops.commit(
                worktree, task.title, paths=paths, home=home, orch_home=orch_home
            )
        store.update_task(conn, spec.id, gated_sha=gitops.rev_parse(worktree, "HEAD"))
        next_state = (
            TaskState.REVIEWING
            if review_required(task.grade, flag=spec.review)
            else TaskState.READY
        )
        store.transition(conn, spec.id, TaskState.GATING, next_state, actor="orch")
        return report
    except BaseException as exc:
        try:
            store.transition(
                conn, spec.id, TaskState.GATING, TaskState.JUDGING, actor="orch"
            )
        except Exception as recovery_exc:
            exc.add_note(f"Failed to recover gating -> judging: {recovery_exc}")
        raise
