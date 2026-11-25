import io
import json
import os
import sqlite3
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from orch import hooks, store
from orch.config import state_db_path
from orch.hooks import (
    handle_notification,
    handle_session_start,
    handle_subagent_stop,
    process_alive,
    recover,
    summary,
)
from orch.models import CheckLevel, Grade, Lane, Run, Task, TaskState

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
TOPIC = "abcdefghijklmnopqrstuvwxyz012345"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network used")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)


@pytest.fixture
def orch_home(tmp_path: Path) -> Path:
    return tmp_path / "home" / ".orch"


@pytest.fixture
def environ(tmp_path: Path, orch_home: Path) -> dict[str, str]:
    return {
        "HOME": str(tmp_path / "home"),
        "ORCH_HOME": str(orch_home),
        "ORCH_TARGET": "orchestrator",
    }


@pytest.fixture
def conn(orch_home: Path) -> Iterator[sqlite3.Connection]:
    connection = store.connect(state_db_path(orch_home))
    # A doctor run at NOW keeps the summaries free of the stale doctor line.
    record_doctor_run(connection, NOW)
    yield connection
    connection.close()


def record_doctor_run(
    conn: sqlite3.Connection, at: datetime, *, target: str = ""
) -> None:
    store.record_doctor_check(
        conn,
        target=target,
        item="disk",
        level=CheckLevel.OK,
        detail=None,
        at=at.isoformat(),
    )


def add_task(
    conn: sqlite3.Connection,
    task_id: str,
    state: TaskState,
    *,
    target: str = "orchestrator",
    pr_url: str | None = None,
) -> None:
    store.insert_task(
        conn,
        Task(
            id=task_id,
            target=target,
            title="T",
            grade=Grade.L2,
            lane=Lane.B,
            model="gpt-6.1-sol",
            state=state,
            timeout_sec=2700,
            created_at=f"2026-11-06T00:00:{len(task_id):02d}+00:00",
            updated_at="2026-11-06T00:00:00+00:00",
        ),
    )
    if pr_url:
        store.update_task(conn, task_id, pr_url=pr_url)


def add_run(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    pid: int | None = 4242,
    pid_started_at: str = "Fri Nov  6 11:00:00 2026",
    started_at: datetime = NOW - timedelta(hours=1),
) -> None:
    run_id = store.insert_run(
        conn,
        Run(
            task_id=task_id,
            attempt=1,
            lane=Lane.B,
            model="gpt-6.1-sol",
            started_at=started_at.isoformat(),
        ),
    )
    if pid is not None:
        store.set_run_process(conn, run_id, pid=pid, pid_started_at=pid_started_at)


def state(conn: sqlite3.Connection, task_id: str) -> TaskState:
    task = store.get_task(conn, task_id)
    assert task is not None
    return task.state


def dead(pid: int, started_at: str) -> bool:
    return False


def live(pid: int, started_at: str) -> bool:
    return True


# process_alive


def test_process_alive_compares_the_start_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hooks.runner, "process_started_at", lambda pid: "T1")
    assert process_alive(1, "T1") is True
    assert process_alive(1, "T2") is False


def test_process_alive_is_false_for_an_empty_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hooks.runner, "process_started_at", lambda pid: "")
    assert process_alive(1, "") is False


# recover


def test_recover_orphans_a_dead_run(conn: sqlite3.Connection) -> None:
    add_task(conn, "a", TaskState.RUNNING)
    add_run(conn, "a")
    seen: list[tuple[int, str]] = []

    def alive(pid: int, started_at: str) -> bool:
        seen.append((pid, started_at))
        return False

    assert recover(conn, now=NOW, alive=alive) == ("a",)
    assert seen == [(4242, "Fri Nov  6 11:00:00 2026")]
    assert state(conn, "a") is TaskState.ORPHANED
    event = conn.execute(
        "SELECT from_state, to_state, actor, note, at FROM events WHERE task_id = 'a'"
        " ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    assert event == ("running", "orphaned", "orch", "session-start", NOW.isoformat())


def test_recover_keeps_a_live_run(conn: sqlite3.Connection) -> None:
    add_task(conn, "a", TaskState.RUNNING)
    add_run(conn, "a")
    assert recover(conn, now=NOW, alive=live) == ()
    assert state(conn, "a") is TaskState.RUNNING


def test_recover_covers_every_target(conn: sqlite3.Connection) -> None:
    add_task(conn, "a", TaskState.RUNNING, target="other")
    add_run(conn, "a")
    assert recover(conn, now=NOW, alive=dead) == ("a",)


def test_recover_orphans_a_running_task_without_a_run(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "a", TaskState.RUNNING)
    assert recover(conn, now=NOW, alive=live) == ("a",)


def test_recover_skips_a_run_that_just_started(conn: sqlite3.Connection) -> None:
    add_task(conn, "a", TaskState.RUNNING)
    add_run(conn, "a", pid=None, started_at=NOW - timedelta(seconds=30))
    assert recover(conn, now=NOW, alive=dead) == ()
    assert state(conn, "a") is TaskState.RUNNING


def test_recover_orphans_an_old_run_without_a_pid(conn: sqlite3.Connection) -> None:
    add_task(conn, "a", TaskState.RUNNING)
    add_run(conn, "a", pid=None, started_at=NOW - timedelta(seconds=90))
    assert recover(conn, now=NOW, alive=live) == ("a",)


def test_recover_orphans_a_run_without_a_start_time(conn: sqlite3.Connection) -> None:
    add_task(conn, "a", TaskState.RUNNING)
    add_run(conn, "a", pid_started_at="")
    assert recover(conn, now=NOW, alive=live) == ("a",)


def test_recover_skips_a_task_that_moved_meanwhile(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "a", TaskState.RUNNING)
    add_task(conn, "bb", TaskState.RUNNING)
    add_run(conn, "a")
    add_run(conn, "bb")

    def finished_meanwhile(pid: int, started_at: str) -> bool:
        if store.get_task(conn, "a").state is TaskState.RUNNING:  # type: ignore[union-attr]
            store.transition(
                conn, "a", TaskState.RUNNING, TaskState.JUDGING, actor="orch"
            )
        return False

    assert recover(conn, now=NOW, alive=finished_meanwhile) == ("bb",)
    assert state(conn, "a") is TaskState.JUDGING


def test_recover_leaves_other_states(conn: sqlite3.Connection) -> None:
    for task_id, task_state in [
        ("j", TaskState.JUDGING),
        ("bl", TaskState.BLOCKED),
        ("pro", TaskState.PR_OPEN),
    ]:
        add_task(conn, task_id, task_state)
    assert recover(conn, now=NOW, alive=dead) == ()


def test_recover_returns_expired_reservations(conn: sqlite3.Connection) -> None:
    add_task(conn, "a", TaskState.RESERVED)
    store.update_task(
        conn, "a", slot_expires_at=(NOW - timedelta(minutes=1)).isoformat()
    )
    add_task(conn, "bb", TaskState.RESERVED)
    store.update_task(
        conn, "bb", slot_expires_at=(NOW + timedelta(minutes=5)).isoformat()
    )
    recover(conn, now=NOW, alive=live)
    assert state(conn, "a") is TaskState.QUEUED
    assert state(conn, "bb") is TaskState.RESERVED


# recover: boundaries and per-task isolation


def recorder() -> tuple[list[tuple[str, Exception]], hooks.RecoverErrorHandler]:
    seen: list[tuple[str, Exception]] = []

    def on_error(key: str, exc: Exception) -> None:
        seen.append((key, exc))

    return seen, on_error


def test_recover_skips_a_run_at_exactly_sixty_seconds(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "a", TaskState.RUNNING)
    add_run(conn, "a", pid=None, started_at=NOW - timedelta(seconds=60))
    assert recover(conn, now=NOW, alive=dead) == ()


def test_recover_skips_a_run_started_this_instant(conn: sqlite3.Connection) -> None:
    add_task(conn, "a", TaskState.RUNNING)
    add_run(conn, "a", pid=None, started_at=NOW)
    assert recover(conn, now=NOW, alive=dead) == ()


def test_recover_orphans_a_run_without_a_pid_started_in_the_future(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "a", TaskState.RUNNING)
    add_run(conn, "a", pid=None, started_at=NOW + timedelta(seconds=1))
    assert recover(conn, now=NOW, alive=dead) == ("a",)


def test_recover_isolates_a_start_time_without_a_time_zone(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "a", TaskState.RUNNING)
    add_run(conn, "a", pid=None, started_at=NOW.replace(tzinfo=None))
    add_task(conn, "bb", TaskState.RUNNING)
    add_run(conn, "bb")
    seen, on_error = recorder()
    assert recover(conn, now=NOW, alive=dead, on_error=on_error) == ("bb",)
    assert state(conn, "a") is TaskState.RUNNING
    assert state(conn, "bb") is TaskState.ORPHANED
    assert [(key, type(exc)) for key, exc in seen] == [("a", TypeError)]


def test_recover_isolates_a_malformed_start_time(conn: sqlite3.Connection) -> None:
    add_task(conn, "a", TaskState.RUNNING)
    add_run(conn, "a", pid=None)
    conn.execute("UPDATE runs SET started_at = 'yesterday' WHERE task_id = 'a'")
    add_task(conn, "bb", TaskState.RUNNING)
    add_run(conn, "bb")
    seen, on_error = recorder()
    assert recover(conn, now=NOW, alive=dead, on_error=on_error) == ("bb",)
    assert [(key, type(exc)) for key, exc in seen] == [("a", ValueError)]


def test_recover_isolates_a_failing_liveness_check(conn: sqlite3.Connection) -> None:
    add_task(conn, "a", TaskState.RUNNING)
    add_run(conn, "a", pid=1)
    add_task(conn, "bb", TaskState.RUNNING)
    add_run(conn, "bb", pid=2)

    def alive(pid: int, started_at: str) -> bool:
        if pid == 1:
            raise OSError("ps failed")
        return False

    seen, on_error = recorder()
    assert recover(conn, now=NOW, alive=alive, on_error=on_error) == ("bb",)
    assert [(key, type(exc)) for key, exc in seen] == [("a", OSError)]


def test_recover_isolates_a_failing_transition(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_task(conn, "a", TaskState.RUNNING)
    add_run(conn, "a")
    add_task(conn, "bb", TaskState.RUNNING)
    add_run(conn, "bb")
    real = store.transition

    def transition(
        conn: sqlite3.Connection, task_id: str, *args: object, **kwargs: object
    ) -> None:
        if task_id == "a":
            raise sqlite3.OperationalError("database is locked")
        real(conn, task_id, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(hooks.store, "transition", transition)
    seen, on_error = recorder()
    assert recover(conn, now=NOW, alive=dead, on_error=on_error) == ("bb",)
    assert [(key, type(exc)) for key, exc in seen] == [("a", sqlite3.OperationalError)]
    assert state(conn, "a") is TaskState.RUNNING


def test_recover_is_isolated_without_an_error_handler(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "a", TaskState.RUNNING)
    add_run(conn, "a", pid=None, started_at=NOW.replace(tzinfo=None))
    add_task(conn, "bb", TaskState.RUNNING)
    add_run(conn, "bb")
    assert recover(conn, now=NOW, alive=dead) == ("bb",)


def test_recover_does_not_report_a_task_that_moved_meanwhile(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "a", TaskState.RUNNING)
    add_run(conn, "a")

    def finished_meanwhile(pid: int, started_at: str) -> bool:
        store.transition(conn, "a", TaskState.RUNNING, TaskState.JUDGING, actor="orch")
        return False

    seen, on_error = recorder()
    assert recover(conn, now=NOW, alive=finished_meanwhile, on_error=on_error) == ()
    assert seen == []


def test_recover_does_not_swallow_an_interrupt(conn: sqlite3.Connection) -> None:
    add_task(conn, "a", TaskState.RUNNING)
    add_run(conn, "a")

    def interrupted(pid: int, started_at: str) -> bool:
        raise KeyboardInterrupt

    seen, on_error = recorder()
    with pytest.raises(KeyboardInterrupt):
        recover(conn, now=NOW, alive=interrupted, on_error=on_error)
    assert seen == []


def test_recover_keeps_orphans_when_reservation_expiry_fails(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "a", TaskState.RUNNING)
    add_run(conn, "a")
    add_task(conn, "expired", TaskState.RESERVED)
    store.update_task(
        conn, "expired", slot_expires_at=(NOW - timedelta(minutes=1)).isoformat()
    )
    add_task(conn, "no-zone-task", TaskState.RESERVED)
    store.update_task(
        conn,
        "no-zone-task",
        slot_expires_at=NOW.replace(tzinfo=None).isoformat(),
    )
    seen, on_error = recorder()
    assert recover(conn, now=NOW, alive=dead, on_error=on_error) == ("a",)
    assert [(key, type(exc)) for key, exc in seen] == [("reservations", TypeError)]
    assert not conn.in_transaction
    # expire_reservations rolls back as a whole; the next healthy pass handles it.
    assert state(conn, "expired") is TaskState.RESERVED
    assert state(conn, "a") is TaskState.ORPHANED


# summary


def test_summary_lists_states_the_main_must_act_on(conn: sqlite3.Connection) -> None:
    add_task(conn, "j", TaskState.JUDGING)
    add_task(conn, "bl", TaskState.BLOCKED)
    add_task(conn, "orp", TaskState.ORPHANED)
    add_task(conn, "conf", TaskState.CONFLICT, pr_url="https://github.com/o/r/pull/1")
    add_task(conn, "propen", TaskState.PR_OPEN, pr_url="https://github.com/o/r/pull/2")
    add_task(conn, "run", TaskState.RUNNING)
    add_task(conn, "done1", TaskState.CLOSED)
    add_task(conn, "other", TaskState.JUDGING, target="other")
    text = summary(conn, target="orchestrator", orphaned=["orp"], now=NOW)
    assert text.splitlines() == [
        "orch 복구 요약 (대상 orchestrator)",
        "- j: judging",
        "- bl: blocked",
        "- orp: orphaned",
        "- conf: conflict https://github.com/o/r/pull/1",
        "- propen: pr_open https://github.com/o/r/pull/2",
        "이번에 orphaned로 바꾼 작업: orp",
    ]


def test_summary_is_empty_when_nothing_needs_action(conn: sqlite3.Connection) -> None:
    add_task(conn, "run", TaskState.RUNNING)
    add_task(conn, "other", TaskState.JUDGING, target="other")
    assert summary(conn, target="orchestrator", orphaned=[], now=NOW) == ""


def test_summary_lists_only_this_targets_new_orphans(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "zz", TaskState.ORPHANED)
    add_task(conn, "x", TaskState.ORPHANED, target="other")
    add_task(conn, "a", TaskState.ORPHANED)
    text = summary(
        conn, target="orchestrator", orphaned=["zz", "x", "gone", "a"], now=NOW
    )
    assert text.splitlines() == [
        "orch 복구 요약 (대상 orchestrator)",
        "- a: orphaned",
        "- zz: orphaned",
        "이번에 orphaned로 바꾼 작업: zz, a",
    ]


def test_summary_is_empty_when_only_another_target_was_orphaned(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "x", TaskState.ORPHANED, target="other")
    assert summary(conn, target="orchestrator", orphaned=["x"], now=NOW) == ""


def test_summary_drops_the_line_when_no_new_orphan_is_this_targets(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "j", TaskState.JUDGING)
    add_task(conn, "x", TaskState.ORPHANED, target="other")
    text = summary(conn, target="orchestrator", orphaned=["x"], now=NOW)
    assert text.splitlines() == ["orch 복구 요약 (대상 orchestrator)", "- j: judging"]


def test_summary_reports_a_doctor_that_never_ran(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM doctor_checks")
    conn.commit()
    assert summary(conn, target="orchestrator", orphaned=[], now=NOW).splitlines() == [
        "orch 복구 요약 (대상 orchestrator)",
        "doctor 미실행 (마지막 실행: 없음)",
    ]


def test_summary_ends_with_a_stale_doctor_line(conn: sqlite3.Connection) -> None:
    add_task(conn, "j", TaskState.JUDGING)
    later = NOW + timedelta(minutes=30)
    text = summary(conn, target="orchestrator", orphaned=[], now=later)
    assert text.splitlines() == [
        "orch 복구 요약 (대상 orchestrator)",
        "- j: judging",
        f"doctor 미실행 (마지막 실행: {NOW.isoformat()})",
    ]


def test_summary_counts_a_doctor_run_of_any_target(conn: sqlite3.Connection) -> None:
    record_doctor_run(conn, NOW + timedelta(minutes=20), target="other")
    later = NOW + timedelta(minutes=40)
    assert summary(conn, target="orchestrator", orphaned=[], now=later) == ""


# handle_session_start


def test_session_start_outputs_additional_context(
    conn: sqlite3.Connection, environ: dict[str, str]
) -> None:
    add_task(conn, "a", TaskState.RUNNING)
    add_run(conn, "a")
    text = handle_session_start("{}", environ, now=NOW, alive=dead)
    value = json.loads(text)
    inner = value["hookSpecificOutput"]
    assert inner["hookEventName"] == "SessionStart"
    assert inner["additionalContext"] == summary(
        conn, target="orchestrator", orphaned=["a"], now=NOW
    )
    assert "\n" not in text.strip()


def test_session_start_recovers_another_target_without_reporting_it(
    conn: sqlite3.Connection, environ: dict[str, str]
) -> None:
    add_task(conn, "x", TaskState.RUNNING, target="other")
    add_run(conn, "x")
    assert handle_session_start("{}", environ, now=NOW, alive=dead) == ""
    assert state(conn, "x") is TaskState.ORPHANED


def test_session_start_reports_only_this_targets_orphans(
    conn: sqlite3.Connection, environ: dict[str, str]
) -> None:
    add_task(conn, "a", TaskState.RUNNING)
    add_run(conn, "a", pid=1)
    add_task(conn, "x", TaskState.RUNNING, target="other")
    add_run(conn, "x", pid=2)
    text = handle_session_start("{}", environ, now=NOW, alive=dead)
    assert json.loads(text)["hookSpecificOutput"]["additionalContext"] == "\n".join(
        [
            "orch 복구 요약 (대상 orchestrator)",
            "- a: orphaned",
            "이번에 orphaned로 바꾼 작업: a",
        ]
    )
    assert state(conn, "x") is TaskState.ORPHANED


def test_session_start_without_a_target_recovers_silently(
    conn: sqlite3.Connection, environ: dict[str, str]
) -> None:
    add_task(conn, "a", TaskState.RUNNING)
    add_run(conn, "a")
    del environ["ORCH_TARGET"]
    assert handle_session_start("{}", environ, now=NOW, alive=dead) == ""
    assert state(conn, "a") is TaskState.ORPHANED


def test_session_start_with_nothing_to_report(
    conn: sqlite3.Connection, environ: dict[str, str]
) -> None:
    assert handle_session_start("{}", environ, now=NOW, alive=dead) == ""


def test_session_start_does_not_create_a_database(
    orch_home: Path, environ: dict[str, str]
) -> None:
    assert handle_session_start("{}", environ, now=NOW, alive=dead) == ""
    assert not orch_home.exists()


def test_session_start_fails_open_and_logs(
    orch_home: Path, environ: dict[str, str]
) -> None:
    orch_home.mkdir(parents=True)
    state_db_path(orch_home).write_text("not a database")
    assert handle_session_start("{}", environ, now=NOW, alive=dead) == ""
    line = (orch_home / "hooks.log").read_text()
    assert line.startswith(f"{NOW.isoformat()} session-start ")
    assert line.endswith("\n")
    assert len(line.splitlines()) == 1
    assert "not a database" not in line


def test_session_start_logs_each_recovery_failure_and_still_summarises(
    conn: sqlite3.Connection, orch_home: Path, environ: dict[str, str]
) -> None:
    add_task(conn, "a", TaskState.RUNNING)
    add_run(conn, "a", pid=1)
    add_task(conn, "bb", TaskState.RUNNING)
    add_run(conn, "bb", pid=2)

    def alive(pid: int, started_at: str) -> bool:
        if pid == 1:
            raise RuntimeError("private detail")
        return False

    text = handle_session_start("{}", environ, now=NOW, alive=alive)
    assert json.loads(text)["hookSpecificOutput"]["additionalContext"] == summary(
        conn, target="orchestrator", orphaned=["bb"], now=NOW
    )
    assert (orch_home / "hooks.log").read_text() == (
        f"{NOW.isoformat()} session-start a RuntimeError\n"
    )


def test_session_start_logs_a_reservation_expiry_failure(
    conn: sqlite3.Connection, orch_home: Path, environ: dict[str, str]
) -> None:
    add_task(conn, "a", TaskState.RUNNING)
    add_run(conn, "a")
    add_task(conn, "no-zone-task", TaskState.RESERVED)
    store.update_task(
        conn,
        "no-zone-task",
        slot_expires_at=NOW.replace(tzinfo=None).isoformat(),
    )
    text = handle_session_start("{}", environ, now=NOW, alive=dead)
    assert json.loads(text)["hookSpecificOutput"]["additionalContext"] == summary(
        conn, target="orchestrator", orphaned=["a"], now=NOW
    )
    assert (orch_home / "hooks.log").read_text() == (
        f"{NOW.isoformat()} session-start reservations TypeError\n"
    )


# handle_subagent_stop


def subagents_log(orch_home: Path) -> Path:
    return orch_home / "orchestrator" / "logs" / "subagents.log"


def test_subagent_stop_appends_a_line(orch_home: Path, environ: dict[str, str]) -> None:
    payload = {"agent_type": "orch-reviewer", "last_assistant_message": "PASS\nok"}
    assert handle_subagent_stop(json.dumps(payload), environ, now=NOW) == ""
    handle_subagent_stop(json.dumps(payload), environ, now=NOW)
    assert subagents_log(orch_home).read_text() == (
        f"{NOW.isoformat()} orch-reviewer PASS ok\n" * 2
    )


def test_subagent_stop_truncates_and_cleans(
    orch_home: Path, environ: dict[str, str]
) -> None:
    message = "\x1b[31m\t" + "x" * 600
    payload = {"agent_type": "bad type!", "last_assistant_message": message}
    handle_subagent_stop(json.dumps(payload), environ, now=NOW)
    line = subagents_log(orch_home).read_text()
    expected = (" [31m " + "x" * 600)[:500]
    assert line == f"{NOW.isoformat()} unknown {expected}\n"


def test_subagent_stop_tolerates_missing_fields(
    orch_home: Path, environ: dict[str, str]
) -> None:
    handle_subagent_stop(json.dumps({"last_assistant_message": 5}), environ, now=NOW)
    assert subagents_log(orch_home).read_text() == f"{NOW.isoformat()} unknown \n"


def test_subagent_stop_without_a_target_does_nothing(
    orch_home: Path, environ: dict[str, str]
) -> None:
    environ["ORCH_TARGET"] = ""
    handle_subagent_stop('{"agent_type": "x"}', environ, now=NOW)
    assert not orch_home.exists()


def test_subagent_stop_fails_open_on_bad_json(
    orch_home: Path, environ: dict[str, str]
) -> None:
    assert handle_subagent_stop("not json", environ, now=NOW) == ""
    assert (
        (orch_home / "hooks.log")
        .read_text()
        .startswith(f"{NOW.isoformat()} subagent-stop ")
    )


# handle_notification


class Transport:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, bytes, float]] = []
        self.error = error

    def __call__(self, url: str, body: bytes, timeout: float) -> None:
        self.calls.append((url, body, timeout))
        if self.error:
            raise self.error


def write_config(orch_home: Path, text: str, mode: int = 0o600) -> None:
    orch_home.mkdir(parents=True, exist_ok=True)
    path = orch_home / "config"
    path.write_text(text)
    path.chmod(mode)


NTFY = f'[notify]\nmethod = "ntfy"\ntopic = "{TOPIC}"\n'


def permission(message: str = "Claude needs your permission to use Bash") -> str:
    return json.dumps({"notification_type": "permission_prompt", "message": message})


def test_permission_prompt_sends_input_wait(
    orch_home: Path, environ: dict[str, str]
) -> None:
    write_config(orch_home, NTFY)
    transport = Transport()
    assert (
        handle_notification(
            permission("run rm -rf secret-path"), environ, now=NOW, transport=transport
        )
        == ""
    )
    assert len(transport.calls) == 1
    url, body, _ = transport.calls[0]
    assert url.endswith(TOPIC)
    text = body.decode()
    assert "input_wait" in text
    assert "orchestrator" in text
    assert "main" in text
    assert "rm -rf" not in text
    assert "secret-path" not in text


def test_a_repeat_is_suppressed(orch_home: Path, environ: dict[str, str]) -> None:
    write_config(orch_home, NTFY)
    transport = Transport()
    handle_notification(permission(), environ, now=NOW, transport=transport)
    handle_notification(
        permission(), environ, now=NOW + timedelta(minutes=5), transport=transport
    )
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"notification_type": "idle_prompt"},
        {"notification_type": "auth_success"},
        {"notification_type": "elicitation_dialog"},
        {"message": "Claude needs your permission"},
    ],
)
def test_other_notifications_are_ignored(
    orch_home: Path, environ: dict[str, str], payload: dict[str, str]
) -> None:
    write_config(orch_home, NTFY)
    transport = Transport()
    handle_notification(json.dumps(payload), environ, now=NOW, transport=transport)
    assert transport.calls == []
    assert not (orch_home / "hooks.log").exists()


def test_ignored_notifications_do_not_read_the_config(
    orch_home: Path, environ: dict[str, str]
) -> None:
    write_config(orch_home, "not = [valid")
    transport = Transport()
    handle_notification(
        '{"notification_type": "idle_prompt"}', environ, now=NOW, transport=transport
    )
    assert not (orch_home / "hooks.log").exists()


def test_no_target_sends_nothing(orch_home: Path, environ: dict[str, str]) -> None:
    write_config(orch_home, NTFY)
    del environ["ORCH_TARGET"]
    transport = Transport()
    handle_notification(permission(), environ, now=NOW, transport=transport)
    assert transport.calls == []


def test_disabled_notifications_send_nothing(
    orch_home: Path, environ: dict[str, str]
) -> None:
    write_config(orch_home, '[notify]\nmethod = "none"\n')
    transport = Transport()
    handle_notification(permission(), environ, now=NOW, transport=transport)
    assert transport.calls == []
    assert not (orch_home / "hooks.log").exists()


def test_a_failed_send_is_logged(orch_home: Path, environ: dict[str, str]) -> None:
    write_config(orch_home, NTFY)
    transport = Transport(OSError("down"))
    assert (
        handle_notification(permission(), environ, now=NOW, transport=transport) == ""
    )
    assert (orch_home / "hooks.log").read_text() == (
        f"{NOW.isoformat()} notification failed\n"
    )


def test_open_permissions_are_logged(orch_home: Path, environ: dict[str, str]) -> None:
    write_config(orch_home, NTFY, mode=0o644)
    transport = Transport()
    handle_notification(permission(), environ, now=NOW, transport=transport)
    assert transport.calls == []
    assert (orch_home / "hooks.log").read_text() == (
        f"{NOW.isoformat()} notification refused_permissions\n"
    )


def test_a_broken_config_fails_open(orch_home: Path, environ: dict[str, str]) -> None:
    write_config(orch_home, "not = [valid")
    transport = Transport()
    assert (
        handle_notification(permission(), environ, now=NOW, transport=transport) == ""
    )
    assert transport.calls == []
    log = (orch_home / "hooks.log").read_text()
    assert log.startswith(f"{NOW.isoformat()} notification ")
    assert TOPIC not in log


def test_bad_json_fails_open(orch_home: Path, environ: dict[str, str]) -> None:
    assert handle_notification("[", environ, now=NOW, transport=Transport()) == ""
    assert (
        (orch_home / "hooks.log")
        .read_text()
        .startswith(f"{NOW.isoformat()} notification ")
    )


# Input wait records (m-p5-input-wait, technical design 4.13, ADR 0030)

SESSION = "0b6f6a1e-5c3a-4b8e-9c55-1d2f3e4a5b6c"


def hook_input(session_id: object = SESSION, **extra: object) -> str:
    return json.dumps({"session_id": session_id, **extra})


def wait_rows(conn: sqlite3.Connection) -> list[tuple[object, ...]]:
    return conn.execute(
        """SELECT kind, target, session_id, task_id, from_state, to_state, actor,
        note, at FROM events ORDER BY id"""
    ).fetchall()


def marker(orch_home: Path, session_id: str = SESSION) -> Path:
    return orch_home / "orchestrator" / "waits" / session_id


def start_row(note: str, at: datetime = NOW) -> tuple[object, ...]:
    return (
        "wait_start",
        "orchestrator",
        SESSION,
        None,
        None,
        None,
        "hook",
        note,
        at.isoformat(),
    )


def end_row(note: str, at: datetime) -> tuple[object, ...]:
    return (
        "wait_end",
        "orchestrator",
        SESSION,
        None,
        None,
        None,
        "hook",
        note,
        at.isoformat(),
    )


def test_wait_constants_follow_the_design() -> None:
    assert hooks.SESSION_ID_PATTERN == r"[A-Za-z0-9_-]{1,128}"
    assert hooks.WAIT_MARKER_MAX_AGE == timedelta(days=7)
    assert dict(hooks.END_HOOKS) == {
        "post-tool-use": "post_tool_use",
        "post-tool-use-failure": "post_tool_use_failure",
        "user-prompt-submit": "user_prompt_submit",
        "stop": "stop",
        "session-end": "session_end",
    }


def test_waits_dir_is_under_the_target_runtime(orch_home: Path) -> None:
    assert hooks.waits_dir(orch_home, "orchestrator") == (
        orch_home / "orchestrator" / "waits"
    )


def test_a_permission_request_starts_a_wait(
    orch_home: Path, environ: dict[str, str], conn: sqlite3.Connection
) -> None:
    output = hooks.handle_permission_request(hook_input(), environ, now=NOW)
    assert output == ""
    assert wait_rows(conn) == [start_row("permission_request")]
    assert marker(orch_home).is_file()
    assert not (orch_home / "hooks.log").exists()


def test_an_end_hook_closes_the_open_wait(
    orch_home: Path, environ: dict[str, str], conn: sqlite3.Connection
) -> None:
    later = NOW + timedelta(seconds=42)
    hooks.handle_permission_request(hook_input(), environ, now=NOW)
    output = hooks.handle_wait_end(
        hook_input(), environ, hook="post-tool-use", now=later
    )
    assert output == ""
    assert wait_rows(conn) == [
        start_row("permission_request"),
        end_row("post_tool_use", later),
    ]
    assert not marker(orch_home).exists()


@pytest.mark.parametrize("hook", list(hooks.END_HOOKS))
def test_every_end_hook_records_its_own_note(
    environ: dict[str, str], conn: sqlite3.Connection, hook: str
) -> None:
    later = NOW + timedelta(seconds=1)
    hooks.handle_permission_request(hook_input(), environ, now=NOW)
    hooks.handle_wait_end(hook_input(), environ, hook=hook, now=later)
    assert wait_rows(conn)[-1] == end_row(hooks.END_HOOKS[hook], later)


def test_an_end_without_an_open_wait_records_nothing(
    environ: dict[str, str], conn: sqlite3.Connection
) -> None:
    hooks.handle_wait_end(hook_input(), environ, hook="stop", now=NOW)
    assert wait_rows(conn) == []


def test_an_end_without_an_open_wait_does_not_open_the_database(
    orch_home: Path, environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    state_db_path(orch_home).parent.mkdir(parents=True, exist_ok=True)
    state_db_path(orch_home).touch()

    def refuse(path: Path) -> sqlite3.Connection:
        raise AssertionError("state.db opened")

    monkeypatch.setattr(store, "connect", refuse)
    hooks.handle_wait_end(hook_input(), environ, hook="post-tool-use", now=NOW)
    assert not (orch_home / "hooks.log").exists()


def test_a_second_start_in_the_same_wait_records_nothing(
    environ: dict[str, str], conn: sqlite3.Connection
) -> None:
    hooks.handle_permission_request(hook_input(), environ, now=NOW)
    hooks.handle_notification(
        json.dumps({"session_id": SESSION, "notification_type": "idle_prompt"}),
        environ,
        now=NOW + timedelta(seconds=6),
        transport=Transport(),
    )
    assert wait_rows(conn) == [start_row("permission_request")]


def test_two_ends_record_one_end(
    environ: dict[str, str], conn: sqlite3.Connection
) -> None:
    later = NOW + timedelta(seconds=3)
    hooks.handle_permission_request(hook_input(), environ, now=NOW)
    hooks.handle_wait_end(hook_input(), environ, hook="post-tool-use", now=later)
    hooks.handle_wait_end(hook_input(), environ, hook="stop", now=later)
    assert wait_rows(conn) == [
        start_row("permission_request"),
        end_row("post_tool_use", later),
    ]


def test_a_new_wait_after_an_end_starts_again(
    environ: dict[str, str], conn: sqlite3.Connection
) -> None:
    t1, t2 = NOW + timedelta(seconds=1), NOW + timedelta(seconds=2)
    hooks.handle_permission_request(hook_input(), environ, now=NOW)
    hooks.handle_wait_end(hook_input(), environ, hook="post-tool-use", now=t1)
    hooks.handle_permission_request(hook_input(), environ, now=t2)
    assert [row[0] for row in wait_rows(conn)] == [
        "wait_start",
        "wait_end",
        "wait_start",
    ]


def test_sessions_have_separate_waits(
    orch_home: Path, environ: dict[str, str], conn: sqlite3.Connection
) -> None:
    hooks.handle_permission_request(hook_input(), environ, now=NOW)
    hooks.handle_permission_request(hook_input("other-session"), environ, now=NOW)
    hooks.handle_wait_end(hook_input("other-session"), environ, hook="stop", now=NOW)
    assert [(row[0], row[2]) for row in wait_rows(conn)] == [
        ("wait_start", SESSION),
        ("wait_start", "other-session"),
        ("wait_end", "other-session"),
    ]
    assert marker(orch_home).is_file()


@pytest.mark.parametrize(
    "session_id",
    [None, "", 7, "has space", "../escape", "a/b", "x" * 129, "ünicode"],
)
def test_a_bad_session_id_records_nothing(
    orch_home: Path,
    environ: dict[str, str],
    conn: sqlite3.Connection,
    session_id: object,
) -> None:
    hooks.handle_permission_request(hook_input(session_id), environ, now=NOW)
    hooks.handle_wait_end(hook_input(session_id), environ, hook="stop", now=NOW)
    assert wait_rows(conn) == []
    assert not (orch_home / "orchestrator" / "waits").exists() or not any(
        (orch_home / "orchestrator" / "waits").iterdir()
    )
    assert not (orch_home / "hooks.log").exists()


def test_the_longest_session_id_is_accepted(
    environ: dict[str, str], conn: sqlite3.Connection
) -> None:
    long_id = "a" * 128
    hooks.handle_permission_request(hook_input(long_id), environ, now=NOW)
    assert [row[2] for row in wait_rows(conn)] == [long_id]


def test_no_target_records_nothing(
    orch_home: Path, environ: dict[str, str], conn: sqlite3.Connection
) -> None:
    del environ["ORCH_TARGET"]
    hooks.handle_permission_request(hook_input(), environ, now=NOW)
    assert wait_rows(conn) == []
    assert not (orch_home / "hooks.log").exists()


def test_without_a_database_nothing_is_created(
    orch_home: Path, environ: dict[str, str]
) -> None:
    hooks.handle_permission_request(hook_input(), environ, now=NOW)
    assert not state_db_path(orch_home).exists()
    assert not marker(orch_home).exists()
    assert not (orch_home / "hooks.log").exists()


def test_a_failed_start_write_removes_the_marker_and_logs(
    orch_home: Path,
    environ: dict[str, str],
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*args: object, **kwargs: object) -> int:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "record_wait", broken)
    assert hooks.handle_permission_request(hook_input(), environ, now=NOW) == ""
    assert not marker(orch_home).exists()
    assert (orch_home / "hooks.log").read_text() == (
        f"{NOW.isoformat()} permission-request OperationalError\n"
    )


def test_a_failed_end_write_is_logged_under_the_hook_name(
    orch_home: Path,
    environ: dict[str, str],
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hooks.handle_permission_request(hook_input(), environ, now=NOW)

    def broken(*args: object, **kwargs: object) -> int:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "record_wait", broken)
    assert (
        hooks.handle_wait_end(hook_input(), environ, hook="user-prompt-submit", now=NOW)
        == ""
    )
    assert (orch_home / "hooks.log").read_text() == (
        f"{NOW.isoformat()} user-prompt-submit OperationalError\n"
    )


@pytest.mark.parametrize("text", ["[", "[]", "null"])
def test_bad_wait_hook_input_fails_open_and_logs(
    orch_home: Path, environ: dict[str, str], text: str
) -> None:
    assert hooks.handle_permission_request(text, environ, now=NOW) == ""
    assert hooks.handle_wait_end(text, environ, hook="stop", now=NOW) == ""
    lines = (orch_home / "hooks.log").read_text().splitlines()
    assert [line.split()[1] for line in lines] == ["permission-request", "stop"]


def test_start_and_end_wait_raise_for_their_callers(
    environ: dict[str, str], conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> int:
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(store, "record_wait", broken)
    payload = {"session_id": SESSION}
    with pytest.raises(sqlite3.OperationalError):
        hooks.start_wait(environ, payload, note="permission_request", now=NOW)
    monkeypatch.undo()
    hooks.start_wait(environ, payload, note="permission_request", now=NOW)
    monkeypatch.setattr(store, "record_wait", broken)
    with pytest.raises(sqlite3.OperationalError):
        hooks.end_wait(environ, payload, note="stop", now=NOW)


# Notification: permission_prompt and idle_prompt start a wait


def notification(kind: str, session_id: str = SESSION) -> str:
    return json.dumps(
        {"session_id": session_id, "notification_type": kind, "message": "m"}
    )


def test_a_permission_prompt_sends_ntfy_and_starts_a_wait(
    orch_home: Path, environ: dict[str, str], conn: sqlite3.Connection
) -> None:
    write_config(orch_home, NTFY)
    transport = Transport()
    hooks.handle_notification(
        notification("permission_prompt"), environ, now=NOW, transport=transport
    )
    assert len(transport.calls) == 1
    assert wait_rows(conn) == [start_row("permission_prompt")]


def test_an_idle_prompt_starts_a_wait_without_ntfy_or_config(
    orch_home: Path, environ: dict[str, str], conn: sqlite3.Connection
) -> None:
    write_config(orch_home, "not = [valid")
    transport = Transport()
    hooks.handle_notification(
        notification("idle_prompt"), environ, now=NOW, transport=transport
    )
    assert transport.calls == []
    assert wait_rows(conn) == [start_row("idle_prompt")]
    assert not (orch_home / "hooks.log").exists()


@pytest.mark.parametrize("kind", ["auth_success", "elicitation_dialog"])
def test_other_notifications_record_no_wait(
    environ: dict[str, str], conn: sqlite3.Connection, kind: str
) -> None:
    hooks.handle_notification(
        notification(kind), environ, now=NOW, transport=Transport()
    )
    assert wait_rows(conn) == []


def test_a_failed_send_still_starts_the_wait(
    orch_home: Path, environ: dict[str, str], conn: sqlite3.Connection
) -> None:
    write_config(orch_home, NTFY)
    hooks.handle_notification(
        notification("permission_prompt"),
        environ,
        now=NOW,
        transport=Transport(OSError("down")),
    )
    assert wait_rows(conn) == [start_row("permission_prompt")]
    assert (orch_home / "hooks.log").read_text() == (
        f"{NOW.isoformat()} notification failed\n"
    )


def test_a_broken_config_still_starts_the_wait(
    orch_home: Path, environ: dict[str, str], conn: sqlite3.Connection
) -> None:
    write_config(orch_home, "not = [valid")
    hooks.handle_notification(
        notification("permission_prompt"), environ, now=NOW, transport=Transport()
    )
    assert wait_rows(conn) == [start_row("permission_prompt")]


def test_a_failed_wait_still_sends_ntfy_and_logs(
    orch_home: Path,
    environ: dict[str, str],
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(orch_home, NTFY)

    def broken(*args: object, **kwargs: object) -> int:
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(store, "record_wait", broken)
    transport = Transport()
    hooks.handle_notification(
        notification("permission_prompt"), environ, now=NOW, transport=transport
    )
    assert len(transport.calls) == 1
    assert (orch_home / "hooks.log").read_text() == (
        f"{NOW.isoformat()} notification wait OperationalError\n"
    )


# SessionStart removes stale wait markers


def age(path: Path, days: float) -> None:
    stamp = (NOW - timedelta(days=days)).timestamp()
    os.utime(path, (stamp, stamp))


def test_clean_wait_markers_removes_only_old_markers(orch_home: Path) -> None:
    waits = hooks.waits_dir(orch_home, "orchestrator")
    waits.mkdir(parents=True)
    old, fresh, edge = waits / "old", waits / "fresh", waits / "edge"
    for path in (old, fresh, edge):
        path.touch()
    age(old, 8)
    age(fresh, 1)
    age(edge, 6.9)
    assert hooks.clean_wait_markers(orch_home, "orchestrator", now=NOW) == 1
    assert sorted(p.name for p in waits.iterdir()) == ["edge", "fresh"]


def test_clean_wait_markers_without_a_directory_is_zero(orch_home: Path) -> None:
    assert hooks.clean_wait_markers(orch_home, "orchestrator", now=NOW) == 0


def test_clean_wait_markers_leaves_directories(orch_home: Path) -> None:
    waits = hooks.waits_dir(orch_home, "orchestrator")
    (waits / "nested").mkdir(parents=True)
    age(waits / "nested", 30)
    assert hooks.clean_wait_markers(orch_home, "orchestrator", now=NOW) == 0
    assert (waits / "nested").is_dir()


def test_clean_wait_markers_records_no_wait_end(
    orch_home: Path, environ: dict[str, str], conn: sqlite3.Connection
) -> None:
    hooks.handle_permission_request(hook_input(), environ, now=NOW - timedelta(days=9))
    age(marker(orch_home), 9)
    hooks.clean_wait_markers(orch_home, "orchestrator", now=NOW)
    assert [row[0] for row in wait_rows(conn)] == ["wait_start"]
    assert not marker(orch_home).exists()


def test_session_start_cleans_markers_even_without_a_database(
    orch_home: Path, environ: dict[str, str]
) -> None:
    waits = hooks.waits_dir(orch_home, "orchestrator")
    waits.mkdir(parents=True)
    (waits / "old").touch()
    age(waits / "old", 8)
    assert handle_session_start("{}", environ, now=NOW) == ""
    assert not (waits / "old").exists()
    assert not state_db_path(orch_home).exists()


def test_session_start_logs_a_cleanup_failure_and_continues(
    orch_home: Path,
    environ: dict[str, str],
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_task(conn, "feat-a", TaskState.JUDGING)

    def broken(*args: object, **kwargs: object) -> int:
        raise PermissionError("denied")

    monkeypatch.setattr(hooks, "clean_wait_markers", broken)
    output = handle_session_start("{}", environ, now=NOW)
    assert "feat-a" in output
    assert (orch_home / "hooks.log").read_text() == (
        f"{NOW.isoformat()} session-start waits PermissionError\n"
    )


def test_session_start_without_a_target_cleans_nothing(
    orch_home: Path, environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    del environ["ORCH_TARGET"]
    calls: list[object] = []
    monkeypatch.setattr(hooks, "clean_wait_markers", lambda *a, **k: calls.append(a))
    handle_session_start("{}", environ, now=NOW)
    assert calls == []


# run_hook dispatch


@pytest.mark.parametrize(
    ("name", "note"),
    [("permission-request", None), *[(n, v) for n, v in hooks.END_HOOKS.items()]],
)
def test_run_hook_routes_the_wait_hooks(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    note: str | None,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def start(stdin_text: str, environ: object, **kwargs: object) -> str:
        calls.append(("start", kwargs))
        return ""

    def end(stdin_text: str, environ: object, **kwargs: object) -> str:
        calls.append(("end", kwargs))
        return ""

    monkeypatch.setattr(hooks, "handle_permission_request", start)
    monkeypatch.setattr(hooks, "handle_wait_end", end)
    monkeypatch.setattr("sys.stdin", io.StringIO(hook_input()))
    assert hooks.run_hook(name) == 0
    if note is None:
        assert calls == [("start", {})]
    else:
        assert calls == [("end", {"hook": name})]
    assert capsys.readouterr().out == ""


def test_session_start_reports_a_stale_doctor(
    conn: sqlite3.Connection, environ: dict[str, str]
) -> None:
    later = NOW + timedelta(hours=1)
    text = handle_session_start("{}", environ, now=later, alive=dead)
    assert json.loads(text)["hookSpecificOutput"]["additionalContext"] == "\n".join(
        [
            "orch 복구 요약 (대상 orchestrator)",
            f"doctor 미실행 (마지막 실행: {NOW.isoformat()})",
        ]
    )


def record_unreadable_doctor_runs(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM doctor_checks")
    conn.commit()
    for item, at in (("disk", "garbage"), ("network", NOW.replace(tzinfo=None))):
        store.record_doctor_check(
            conn,
            target="",
            item=item,
            level=CheckLevel.OK,
            detail=None,
            at=at if isinstance(at, str) else at.isoformat(),
        )


def test_summary_keeps_the_tasks_when_no_doctor_run_is_readable(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "j", TaskState.JUDGING)
    record_unreadable_doctor_runs(conn)
    assert summary(conn, target="orchestrator", orphaned=[], now=NOW).splitlines() == [
        "orch 복구 요약 (대상 orchestrator)",
        "- j: judging",
        "doctor 미실행 (마지막 실행: 없음)",
    ]


def test_session_start_keeps_the_summary_with_an_unreadable_doctor_run(
    conn: sqlite3.Connection, environ: dict[str, str]
) -> None:
    add_task(conn, "j", TaskState.JUDGING)
    record_unreadable_doctor_runs(conn)
    text = handle_session_start("{}", environ, now=NOW, alive=dead)
    assert json.loads(text)["hookSpecificOutput"]["additionalContext"] == "\n".join(
        [
            "orch 복구 요약 (대상 orchestrator)",
            "- j: judging",
            "doctor 미실행 (마지막 실행: 없음)",
        ]
    )
