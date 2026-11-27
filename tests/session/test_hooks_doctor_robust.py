"""The session start summary survives any doctor_checks content.

Task m-summary-doctor-robust.

Invariant: no row in doctor_checks, and no exception from reading it, makes
hooks.summary raise. Only the doctor line changes.
"""

# ruff: noqa: F811 - fixtures imported from test_hooks are used as parameters

import json
import sqlite3
from pathlib import Path

import pytest

from orch import hooks, render, store
from orch.hooks import handle_session_start, summary
from orch.models import TaskState
from test_hooks import (  # noqa: F401 - fixtures used by name
    NOW,
    add_task,
    conn,
    dead,
    environ,
    no_network,
    orch_home,
)

HEAD = "orch 복구 요약 (대상 orchestrator)"
UNREADABLE = "doctor 상태를 읽지 못함"

# (level, at) pairs that a manual edit can store; each breaks the old summary.
# An integer or real at becomes text under the column's TEXT affinity and is
# already ignored as unreadable, so only a BLOB reaches fromisoformat.
BAD_ROWS = [
    pytest.param("bogus", NOW.isoformat(), ValueError, id="unknown-level"),
    pytest.param("ok", NOW.isoformat().encode(), TypeError, id="blob-at"),
    pytest.param("", NOW.isoformat(), ValueError, id="empty-level"),
]


def insert_row(conn: sqlite3.Connection, level: object, at: object) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO doctor_checks VALUES ('', 'bad', ?, NULL, ?)",
        (level, at),
    )
    conn.commit()


def test_the_unreadable_line_is_a_constant() -> None:
    assert hooks.DOCTOR_UNREADABLE_LINE == UNREADABLE


@pytest.mark.parametrize(("level", "at", "error"), BAD_ROWS)
def test_a_bad_row_keeps_the_tasks_and_reports_it(
    conn: sqlite3.Connection, level: object, at: object, error: type[Exception]
) -> None:
    add_task(conn, "j", TaskState.JUDGING)
    insert_row(conn, level, at)
    seen: list[Exception] = []
    text = summary(
        conn, target="orchestrator", orphaned=[], now=NOW, on_error=seen.append
    )
    assert text.splitlines() == [HEAD, "- j: judging", UNREADABLE]
    assert len(seen) == 1
    assert isinstance(seen[0], error)


@pytest.mark.parametrize(("level", "at", "error"), BAD_ROWS)
def test_a_bad_row_alone_still_gives_a_summary(
    conn: sqlite3.Connection, level: object, at: object, error: type[Exception]
) -> None:
    insert_row(conn, level, at)
    text = summary(conn, target="orchestrator", orphaned=[], now=NOW)
    assert text.splitlines() == [HEAD, UNREADABLE]


def test_the_unreadable_line_comes_after_the_orphan_line(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "a", TaskState.ORPHANED)
    insert_row(conn, "bogus", NOW.isoformat())
    text = summary(conn, target="orchestrator", orphaned=["a"], now=NOW)
    assert text.splitlines() == [
        HEAD,
        "- a: orphaned",
        "이번에 orphaned로 바꾼 작업: a",
        UNREADABLE,
    ]


@pytest.mark.parametrize(
    ("module", "name", "error"),
    [
        (store, "list_doctor_checks", sqlite3.DatabaseError("disk image malformed")),
        (render, "doctor_state", RuntimeError("state")),
        (render, "doctor_stale_line", KeyError("stale")),
    ],
)
def test_any_exception_in_the_doctor_line_is_contained(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    module: object,
    name: str,
    error: Exception,
) -> None:
    def broken(*args: object, **kwargs: object) -> object:
        raise error

    monkeypatch.setattr(module, name, broken)
    add_task(conn, "j", TaskState.JUDGING)
    seen: list[Exception] = []
    text = summary(
        conn, target="orchestrator", orphaned=[], now=NOW, on_error=seen.append
    )
    assert text.splitlines() == [HEAD, "- j: judging", UNREADABLE]
    assert seen == [error]


def test_a_failing_error_callback_is_ignored(conn: sqlite3.Connection) -> None:
    add_task(conn, "j", TaskState.JUDGING)
    insert_row(conn, "bogus", NOW.isoformat())

    def refuse(exc: Exception) -> None:
        raise RuntimeError("callback failed")

    text = summary(conn, target="orchestrator", orphaned=[], now=NOW, on_error=refuse)
    assert text.splitlines() == [HEAD, "- j: judging", UNREADABLE]


def test_readable_rows_do_not_call_the_error_callback(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "j", TaskState.JUDGING)
    seen: list[Exception] = []
    text = summary(
        conn, target="orchestrator", orphaned=[], now=NOW, on_error=seen.append
    )
    assert text.splitlines() == [HEAD, "- j: judging"]
    assert seen == []


def test_session_start_keeps_the_summary_and_logs_the_doctor_error(
    conn: sqlite3.Connection, environ: dict[str, str], orch_home: Path
) -> None:
    add_task(conn, "j", TaskState.JUDGING)
    insert_row(conn, "ok", NOW.isoformat().encode())
    text = handle_session_start("{}", environ, now=NOW, alive=dead)
    assert json.loads(text)["hookSpecificOutput"]["additionalContext"] == "\n".join(
        [HEAD, "- j: judging", UNREADABLE]
    )
    assert (orch_home / "hooks.log").read_text() == (
        f"{NOW.isoformat()} session-start doctor TypeError\n"
    )


def test_session_start_reports_an_unknown_level_alone(
    conn: sqlite3.Connection, environ: dict[str, str], orch_home: Path
) -> None:
    insert_row(conn, "bogus", NOW.isoformat())
    text = handle_session_start("{}", environ, now=NOW, alive=dead)
    assert json.loads(text)["hookSpecificOutput"]["additionalContext"] == "\n".join(
        [HEAD, UNREADABLE]
    )
    assert (orch_home / "hooks.log").read_text() == (
        f"{NOW.isoformat()} session-start doctor ValueError\n"
    )
