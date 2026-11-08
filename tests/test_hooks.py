import json
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
from orch.models import Grade, Lane, Run, Task, TaskState

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
    yield connection
    connection.close()


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
    text = summary(conn, target="orchestrator", orphaned=["orp"])
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
    assert summary(conn, target="orchestrator", orphaned=[]) == ""


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
        conn, target="orchestrator", orphaned=["a"]
    )
    assert "\n" not in text.strip()


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
        conn, target="orchestrator", orphaned=["bb"]
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
        conn, target="orchestrator", orphaned=["a"]
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
