"""A LaneError during lane B preparation keeps its cause (m-runner-lane-cause)."""

# ruff: noqa: F811 - fixtures imported from test_runner are used as parameters

import sqlite3
from pathlib import Path
from typing import NoReturn

import pytest

from orch import lanes, runner
from orch.config import TargetPaths
from orch.models import FailureReason, Outcome, TaskState
from orch.results import load_result
from test_runner import (  # noqa: F401 - fixtures used by name
    CHANGED,
    PROXY_START_SUMMARY,
    TASK,
    conn,
    home,
    isolated_git,
    paths,
    repo,
    run,
    run_of,
    task_of,
    worktree,
)

CODEX_HOME_SUMMARY = (
    "Codex 실행 명령을 만들지 못했습니다: CODEX_HOME must name HOME/.codex"
)


def raise_from_build(error: Exception) -> object:
    def build_command(**kwargs: object) -> NoReturn:
        raise error

    return build_command


def test_the_constants_name_the_failure() -> None:
    assert runner.LANE_ERROR_SUMMARY == "Codex 실행 명령을 만들지 못했습니다"
    assert runner.LANE_ERROR_DETAIL_LIMIT == 200


def test_a_codex_home_mismatch_reports_the_lane_error(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "env.json"
    result = run(
        conn,
        repo,
        paths,
        home,
        "env_values",
        ORCH_FAKE_ENV_FILE=str(marker),
        CODEX_HOME=str(tmp_path / "elsewhere" / ".codex"),
    )
    assert result.status is Outcome.FAILED
    assert result.failure_reason is FailureReason.ABORTED
    assert result.summary == CODEX_HOME_SUMMARY
    assert not marker.exists()
    assert not (worktree / CHANGED).exists()
    assert task_of(conn).state is TaskState.JUDGING
    assert run_of(conn).outcome is Outcome.FAILED
    saved = load_result(paths.results / f"{TASK}.json", TASK)
    assert saved.summary == CODEX_HOME_SUMMARY
    assert saved.failure_reason is FailureReason.ABORTED


def test_a_long_lane_error_message_is_cut(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        lanes, "build_command", raise_from_build(lanes.LaneError("가" * 500))
    )
    result = run(conn, repo, paths, home, "success")
    assert result.failure_reason is FailureReason.ABORTED
    assert result.summary == "Codex 실행 명령을 만들지 못했습니다: " + "가" * 200
    assert not (worktree / CHANGED).exists()
    saved = load_result(paths.results / f"{TASK}.json", TASK)
    assert saved.summary == result.summary


def test_another_preparation_error_keeps_the_proxy_summary(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        lanes, "build_command", raise_from_build(RuntimeError("not a lane error"))
    )
    result = run(conn, repo, paths, home, "success")
    assert result.status is Outcome.FAILED
    assert result.failure_reason is FailureReason.ABORTED
    assert result.summary == PROXY_START_SUMMARY
    assert not (worktree / CHANGED).exists()
    assert task_of(conn).state is TaskState.JUDGING
