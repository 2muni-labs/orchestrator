"""lifecycle.kill_task: stop a running task and record FAILED/aborted once."""
# ruff: noqa: F811 - fixtures imported from test_runner are used as parameters

import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orch import lifecycle, results, runner, store
from orch.config import TargetPaths
from orch.lifecycle import KillReport, LifecycleError, kill_task
from orch.models import FailureReason, Lane, Outcome, Run, TaskState
from orch.runner import process_started_at, run_task
from test_runner import (  # noqa: F401 - fixtures used by name
    TASK,
    add_task,
    conn,
    events_of,
    home,
    isolated_git,
    make_config,
    make_env,
    make_target,
    paths,
    real_ps,
    repo,
    run_of,
    task_of,
    worktree,
    write,
)

FAST = {"settle_sec": 0.3, "grace_sec": 1.0, "poll_sec": 0.05}


def kill(
    conn: sqlite3.Connection, paths: TargetPaths, home: Path, **options: object
) -> KillReport:
    values: dict[str, object] = {**FAST, "environ": {"HOME": str(home)}}
    values.update(options)
    return kill_task(conn, TASK, paths=paths, **values)


def make_running(
    conn: sqlite3.Connection,
    *,
    pid: int | None = None,
    pid_started_at: str | None = None,
) -> int:
    started = datetime.now(UTC).isoformat()
    with store.transaction(conn):
        store.transition(
            conn, TASK, TaskState.RESERVED, TaskState.RUNNING, actor="orch"
        )
        run_id = store.insert_run(
            conn,
            Run(
                task_id=TASK,
                attempt=1,
                lane=Lane.B,
                model="m",
                started_at=started,
            ),
        )
    if pid is not None:
        store.set_run_process(
            conn, run_id, pid=pid, pid_started_at=pid_started_at or ""
        )
    return run_id


def start_sleeper() -> subprocess.Popen[bytes]:
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "print('ready', flush=True)\nimport time\ntime.sleep(3600)",
        ],
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline() == b"ready\n"
    return process


def unchanged(conn: sqlite3.Connection, state: TaskState) -> None:
    assert task_of(conn).state is state


# Refusals change nothing


def test_a_missing_task_is_refused(
    conn: sqlite3.Connection, paths: TargetPaths, home: Path
) -> None:
    with pytest.raises(LifecycleError):
        kill(conn, paths, home)


def test_a_task_that_is_not_running_is_refused(
    conn: sqlite3.Connection, paths: TargetPaths, home: Path, worktree: Path
) -> None:
    with pytest.raises(LifecycleError):
        kill(conn, paths, home)
    unchanged(conn, TaskState.RESERVED)


def test_a_running_task_without_a_run_is_refused(
    conn: sqlite3.Connection, paths: TargetPaths, home: Path, worktree: Path
) -> None:
    store.transition(conn, TASK, TaskState.RESERVED, TaskState.RUNNING, actor="orch")
    with pytest.raises(LifecycleError):
        kill(conn, paths, home)
    unchanged(conn, TaskState.RUNNING)


def test_a_finished_run_is_refused(
    conn: sqlite3.Connection, paths: TargetPaths, home: Path, worktree: Path
) -> None:
    run_id = make_running(conn)
    store.finish_run(conn, run_id, ended_at="t", exit_code=0, outcome=Outcome.DONE)
    with pytest.raises(LifecycleError):
        kill(conn, paths, home)
    assert store.get_run(conn, run_id).kill_requested_at is None
    unchanged(conn, TaskState.RUNNING)


# The runner is gone: kill records the abort itself


def test_kill_records_the_abort_when_no_runner_finishes(
    conn: sqlite3.Connection, paths: TargetPaths, home: Path, worktree: Path
) -> None:
    real_ps()
    process = start_sleeper()
    run_id = make_running(
        conn, pid=process.pid, pid_started_at=process_started_at(process.pid)
    )
    write(paths.logs / f"{TASK}.1.log")
    write(paths.logs / f"{TASK}.2.log")
    write(worktree / ".orch-result.json", '{"partial": true}')
    try:
        report = kill(conn, paths, home)
    finally:
        returncode = process.wait(timeout=10)
    assert returncode < 0
    assert report == KillReport(
        task_id=TASK,
        state=TaskState.JUDGING,
        outcome=Outcome.FAILED,
        failure_reason=FailureReason.ABORTED,
        finished_by="kill",
        signalled=True,
    )
    run = store.get_run(conn, run_id)
    assert run.kill_requested_at is not None
    assert (run.outcome, run.failure_reason, run.exit_code) == (
        Outcome.FAILED,
        FailureReason.ABORTED,
        None,
    )
    assert run.ended_at is not None
    saved = results.load_result(paths.results / f"{TASK}.json", TASK)
    assert (saved.status, saved.failure_reason) == (
        Outcome.FAILED,
        FailureReason.ABORTED,
    )
    assert saved.summary == runner.KILL_SUMMARY
    assert saved.log_path == f"~/.orch/sample/logs/{TASK}.2.log"
    assert saved.verification_command == "true"
    assert not (worktree / ".orch-result.json").exists()
    assert (paths.logs / f"{TASK}.2.rejected-result.json").read_text(
        encoding="utf-8"
    ) == '{"partial": true}'
    notes = conn.execute(
        "SELECT from_state, to_state, actor, note FROM events WHERE task_id = ?",
        (TASK,),
    ).fetchall()
    assert notes[-1] == ("running", "judging", "orch", "kill")
    assert task_of(conn).worktree == str(worktree)
    assert worktree.is_dir()


def test_kill_without_a_process_records_the_abort_unsignalled(
    conn: sqlite3.Connection, paths: TargetPaths, home: Path, worktree: Path
) -> None:
    make_running(conn)
    report = kill(conn, paths, home)
    assert (report.finished_by, report.signalled) == ("kill", False)
    assert report.failure_reason is FailureReason.ABORTED
    assert task_of(conn).state is TaskState.JUDGING
    saved = results.load_result(paths.results / f"{TASK}.json", TASK)
    assert saved.log_path == ""


def test_kill_without_a_log_names_the_first_rejected_file(
    conn: sqlite3.Connection, paths: TargetPaths, home: Path, worktree: Path
) -> None:
    make_running(conn)
    write(worktree / ".orch-result.json", "{}")
    kill(conn, paths, home)
    assert (paths.logs / f"{TASK}.1.rejected-result.json").exists()


def test_kill_leaves_a_reused_pid_alone(
    conn: sqlite3.Connection, paths: TargetPaths, home: Path, worktree: Path
) -> None:
    real_ps()
    process = start_sleeper()
    try:
        make_running(conn, pid=process.pid, pid_started_at="Thu Jan  1 00:00:00 1970")
        report = kill(conn, paths, home)
        assert report.signalled is False
        assert process.poll() is None
        assert report.finished_by == "kill"
    finally:
        process.kill()
        process.wait()


def test_kill_uses_the_given_liveness_check(
    conn: sqlite3.Connection,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[int] = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: sent.append(sig))
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append(sig))
    make_running(conn, pid=4242, pid_started_at="then")
    report = kill(conn, paths, home, alive=lambda pid, started: False)
    assert report.signalled is False
    assert sent == []


def test_a_runner_that_finishes_during_the_settle_wins(
    conn: sqlite3.Connection,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    tmp_path: Path,
) -> None:
    run_id = make_running(conn)

    def runner_finishes() -> None:
        time.sleep(0.2)
        own = store.connect(tmp_path / "state.db")
        try:
            with store.transaction(own):
                store.finish_run(
                    own,
                    run_id,
                    ended_at="by-runner",
                    exit_code=-15,
                    outcome=Outcome.FAILED,
                    failure_reason=FailureReason.ABORTED,
                )
                store.transition(
                    own, TASK, TaskState.RUNNING, TaskState.JUDGING, actor="orch"
                )
        finally:
            own.close()

    thread = threading.Thread(target=runner_finishes)
    thread.start()
    report = kill(conn, paths, home, settle_sec=10.0)
    thread.join()
    assert report.finished_by == "runner"
    assert store.get_run(conn, run_id).ended_at == "by-runner"
    assert not (paths.results / f"{TASK}.json").exists()
    assert events_of(conn).count(("running", "judging")) == 1


def test_kill_does_not_record_over_a_runner_that_finished_in_between(
    conn: sqlite3.Connection,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = make_running(conn)
    real_get_run = store.get_run
    calls = {"n": 0}

    def get_run_then_finish(connection: sqlite3.Connection, wanted: int) -> Run | None:
        calls["n"] += 1
        row = real_get_run(connection, wanted)
        if connection.in_transaction and row is not None and row.ended_at is None:
            connection.execute(
                "UPDATE runs SET ended_at = 'raced', outcome = 'FAILED', "
                "failure_reason = 'killed' WHERE run_id = ?",
                (wanted,),
            )
            connection.execute(
                "UPDATE tasks SET state = 'judging' WHERE id = ?", (TASK,)
            )
            return real_get_run(connection, wanted)
        return row

    monkeypatch.setattr(store, "get_run", get_run_then_finish)
    report = kill(conn, paths, home)
    assert report.finished_by == "runner"
    assert (report.outcome, report.failure_reason) == (
        Outcome.FAILED,
        FailureReason.KILLED,
    )
    assert real_get_run(conn, run_id).ended_at == "raced"
    assert not (paths.results / f"{TASK}.json").exists()


# A live runner records the abort


def test_kill_stops_a_live_run_and_the_runner_records_the_abort(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    tmp_path: Path,
) -> None:
    real_ps()
    outcome: dict[str, object] = {}

    def run_it() -> None:
        own = store.connect(tmp_path / "state.db")
        try:
            outcome["result"] = run_task(
                own,
                TASK,
                target=make_target(repo),
                config=make_config(),
                paths=paths,
                environ=make_env(home, "ignore_term"),
            )
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            outcome["error"] = exc
        finally:
            own.close()

    thread = threading.Thread(target=run_it, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while True:
        row = store.latest_run(conn, TASK)
        if row is not None and row.pid and row.pid_started_at:
            break
        assert time.monotonic() < deadline
        time.sleep(0.05)
    report = kill(conn, paths, home, settle_sec=20.0, grace_sec=0.5)
    thread.join(timeout=30)
    assert not thread.is_alive()
    assert "error" not in outcome, outcome.get("error")
    assert report == KillReport(
        task_id=TASK,
        state=TaskState.JUDGING,
        outcome=Outcome.FAILED,
        failure_reason=FailureReason.ABORTED,
        finished_by="runner",
        signalled=True,
    )
    result = outcome["result"]
    assert isinstance(result, results.TaskResult)
    assert result.summary == runner.KILL_SUMMARY
    assert run_of(conn).exit_code == -signal.SIGKILL
    assert events_of(conn).count(("running", "judging")) == 1


def test_kill_report_is_exported_from_lifecycle() -> None:
    assert lifecycle.KillReport is KillReport
