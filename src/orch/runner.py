"""orch run: start a sub-agent, stop it on time, and classify how it ended."""

import os
import re
import signal
import sqlite3
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from orch import gate, lanes, results, store
from orch.config import Config, Target, TargetPaths
from orch.models import FailureReason, Outcome, Run, TaskState
from orch.results import TaskResult
from orch.taskfile import TaskFileError, parse_task

TERM_GRACE_SEC = 10


class RunError(Exception):
    """A task cannot start. Its state has not changed."""


def next_log_path(logs: Path, task_id: str) -> Path:
    """Return the path for a task's next numbered log, without creating it."""
    pattern = re.compile(rf"{re.escape(task_id)}\.(\d+)\.log")
    highest = 0
    try:
        with os.scandir(logs) as entries:
            for entry in entries:
                if (match := pattern.fullmatch(entry.name)) and entry.is_file():
                    highest = max(highest, int(match[1]))
    except FileNotFoundError:
        pass
    return logs / f"{task_id}.{highest + 1}.log"


def log_label(log_path: Path, environ: Mapping[str, str]) -> str:
    """Return the log path as a sub-agent reports it, with the home as a tilde."""
    if home := environ.get("HOME"):
        try:
            return "~/" + str(log_path.relative_to(home))
        except ValueError:
            pass
    return str(log_path)


def process_started_at(pid: int) -> str:
    """Return a process's start time from ps, or an empty string if ps cannot tell."""
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
    except OSError:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def descendants(root: int) -> set[int]:
    """Return a process's descendants from ps, or an empty set if ps cannot tell."""
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid="],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
    except OSError:
        return set()
    if result.returncode != 0:
        return set()
    children: dict[int, list[int]] = {}
    for line in result.stdout.splitlines():
        pid, ppid = map(int, line.split())
        children.setdefault(ppid, []).append(pid)
    found = {root}
    pending = [root]
    while pending:
        for child in children.get(pending.pop(), ()):
            if child not in found:
                found.add(child)
                pending.append(child)
    return found - {root}


def _send(pgid: int, pids: set[int], sig: signal.Signals) -> None:
    """Signal the group and any descendants that started separate sessions."""
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError, PermissionError:
        pass
    for pid in pids:
        try:
            os.kill(pid, sig)
        except ProcessLookupError, PermissionError:
            pass


def stop_process(process: subprocess.Popen[bytes]) -> None:
    """Stop a process, its group and its descendants: SIGTERM, then SIGKILL."""
    pids = descendants(process.pid)
    _send(process.pid, pids, signal.SIGTERM)
    try:
        process.wait(timeout=TERM_GRACE_SEC)
    except subprocess.TimeoutExpired:
        pass
    _send(process.pid, pids, signal.SIGKILL)
    process.wait()


def _reject_report(source: Path, rejected: Path) -> None:
    """Preserve a report that cannot be used for this run, including symlinks."""
    if os.path.lexists(source):
        rejected.parent.mkdir(parents=True, exist_ok=True)
        source.replace(rejected)


def _finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    task_id: str,
    destination: Path,
    result: TaskResult,
    exit_code: int | None,
    substitute: Callable[[FailureReason | None, str], TaskResult],
) -> TaskResult:
    """Persist the report, then finish the run together with its state change."""
    try:
        results.write_result(destination, result)
    except Exception:
        try:
            destination.unlink(missing_ok=True)
        except Exception:
            pass
        result = substitute(
            FailureReason.REPORT_MISSING, "결과 파일을 저장하지 못했습니다."
        )
    ended_at = datetime.now(UTC).isoformat()
    with store.transaction(conn):
        store.finish_run(
            conn,
            run_id,
            ended_at=ended_at,
            exit_code=exit_code,
            outcome=result.status,
            failure_reason=result.failure_reason,
        )
        store.transition(
            conn,
            task_id,
            TaskState.RUNNING,
            TaskState.JUDGING,
            actor="orch",
            at=ended_at,
        )
    return result


def run_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    target: Target,
    config: Config,
    paths: TargetPaths,
    environ: Mapping[str, str],
    on_started: Callable[[], None] | None = None,
) -> TaskResult:
    """Run a reserved task to its end, record the result, and move it to judging."""
    task = store.get_task(conn, task_id)
    if (
        task is None
        or task.state is not TaskState.RESERVED
        or not task.worktree
        or not task.test_sha
    ):
        raise RunError("Task must be reserved with a worktree and test commit")
    worktree = Path(task.worktree)
    if not worktree.is_dir():
        raise RunError("Task worktree is not a directory")
    try:
        log_path = next_log_path(paths.logs, task_id)
        label = log_label(log_path, environ)
        spec_text = (paths.tasks / f"{task_id}.md").read_text(encoding="utf-8")
        spec = parse_task(spec_text)
        if spec.id != task_id:
            raise RunError("Task spec id does not match the task")
        command = lanes.build_command(
            spec=spec,
            spec_text=spec_text,
            worktree=worktree,
            target=target,
            config=config,
            environ=environ,
            log_label=label,
        )
    except (OSError, UnicodeError, TaskFileError, lanes.LaneError) as exc:
        raise RunError("Cannot read or start the task spec") from exc

    def substitute(
        reason: FailureReason | None,
        summary: str,
        status: Outcome = Outcome.FAILED,
    ) -> TaskResult:
        return results.substitute_result(
            task_id=task_id,
            status=status,
            failure_reason=reason,
            summary=summary,
            verification_command=spec.verify,
            log_path=label,
        )

    source = worktree / ".orch-result.json"
    destination = paths.results / f"{task_id}.json"
    rejected = paths.logs / f"{task_id}.rejected-result.json"
    started_at = datetime.now(UTC).isoformat()
    attempt = max(task.attempt, 1)
    with store.transaction(conn):
        store.update_task(conn, task_id, attempt=attempt, at=started_at)
        store.transition(
            conn,
            task_id,
            TaskState.RESERVED,
            TaskState.RUNNING,
            actor="orch",
            at=started_at,
        )
        run_id = store.insert_run(
            conn,
            Run(
                task_id=task_id,
                attempt=attempt,
                lane=spec.lane,
                model=spec.model,
                effort=spec.effort,
                started_at=started_at,
            ),
        )
    process = None
    try:
        if on_started is not None:
            try:
                on_started()
            except Exception:
                pass
        try:
            paths.logs.mkdir(parents=True, exist_ok=True)
            source.unlink(missing_ok=True)
            with (
                tempfile.TemporaryFile(dir=worktree) as stdin,
                log_path.open("xb") as log,
            ):
                stdin.write(command.stdin_text.encode("utf-8"))
                stdin.seek(0)
                process = subprocess.Popen(
                    list(command.argv),
                    cwd=command.cwd,
                    env=dict(command.env),
                    stdin=stdin,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except Exception:
            if process is not None:
                stop_process(process)
            result = substitute(
                FailureReason.REPORT_MISSING, "에이전트 실행을 시작하지 못했습니다."
            )
            return _finish_run(
                conn, run_id, task_id, destination, result, None, substitute
            )

        timed_out = False
        store.set_run_process(
            conn,
            run_id,
            pid=process.pid,
            pid_started_at=process_started_at(process.pid),
        )
        try:
            process.wait(timeout=task.timeout_sec)
        except subprocess.TimeoutExpired:
            stop_process(process)
            timed_out = True
        else:
            _send(process.pid, set(), signal.SIGKILL)

        try:
            if timed_out:
                _reject_report(source, rejected)
                result = substitute(
                    FailureReason.TIMEOUT, "작업 시간 상한을 초과했습니다."
                )
            elif process.returncode < 0:
                _reject_report(source, rejected)
                result = substitute(
                    FailureReason.KILLED, "에이전트가 신호로 종료되었습니다."
                )
            else:
                try:
                    result = results.collect_result(
                        source, destination, rejected, task_id
                    )
                except results.ReportError:
                    if lanes.hit_usage_limit(
                        spec.lane,
                        log_path,
                        spec_text=spec_text,
                        config=config,
                        environ=environ,
                    ):
                        result = substitute(
                            None,
                            "에이전트 사용량 한도에 도달했습니다.",
                            Outcome.BLOCKED_QUOTA,
                        )
                    else:
                        result = substitute(
                            FailureReason.REPORT_MISSING,
                            "유효한 결과 보고서를 받지 못했습니다.",
                        )
                else:
                    if result.status is Outcome.DONE:
                        violations = gate.check_scope(
                            worktree,
                            since=task.test_sha,
                            main_ref=f"origin/{target.default_branch}",
                            modify=spec.modify,
                            shared_paths=target.shared_paths,
                        )
                        if violations:
                            result = replace(
                                result,
                                status=Outcome.FAILED,
                                failure_reason=FailureReason.OUT_OF_SCOPE,
                                out_of_scope_changes=tuple(
                                    sorted(
                                        set(violations.test_file_changed)
                                        | set(violations.shared_file_changed)
                                        | set(violations.out_of_scope)
                                    )
                                ),
                            )
        except Exception:
            result = substitute(
                FailureReason.REPORT_MISSING, "실행 결과를 분류하지 못했습니다."
            )
        return _finish_run(
            conn, run_id, task_id, destination, result, process.returncode, substitute
        )
    except BaseException as exc:
        try:
            if process is not None:
                stop_process(process)
        except Exception as cleanup_error:
            exc.add_note(f"Failed to stop the agent: {cleanup_error}")
        try:
            _reject_report(source, rejected)
        except Exception as cleanup_error:
            exc.add_note(f"Failed to reject the report: {cleanup_error}")
        try:
            result = substitute(
                FailureReason.ABORTED, "에이전트 실행이 중단되었습니다."
            )
            _finish_run(
                conn,
                run_id,
                task_id,
                destination,
                result,
                process.returncode if process is not None else None,
                substitute,
            )
        except Exception as cleanup_error:
            exc.add_note(f"Failed to record the abort: {cleanup_error}")
        raise
