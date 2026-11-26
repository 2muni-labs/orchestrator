"""orch pr adopts an open pull request it created without recording it (m-pr-adopt)."""
# ruff: noqa: F811 - fixtures imported from test_prflow are used as parameters

import sqlite3
from pathlib import Path

import pytest

from orch.github import GitHubError
from orch.models import Grade, TaskState
from orch.store import connect
from test_prflow import (  # noqa: F401 - fixtures used by name
    BRANCH,
    REDACT,
    TOKEN,
    TRIAGE,
    URL,
    FakeGitHub,
    add_ready_task,
    conn,
    events_of,
    fix_commit,
    hub,
    implemented,
    isolated_git,
    move_main,
    open_pr,
    opened,
    remote,
    remote_tip,
    repo,
    return_to_ready,
    save_block,
    task_of,
    test_sha,
    worktree,
)

OTHER = "https://github.com/owner/sample/pull/9"


def test_open_looks_for_an_open_pull_request_before_creating(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    add_ready_task(conn, worktree, test_sha)
    report = open_pr(conn, repo, tmp_path)
    assert report.state is TaskState.PR_OPEN
    assert report.pr_url == URL
    assert report.adopted is False
    assert hub.order == ["find", "create"]
    assert hub.found == [
        {"github": "owner/sample", "head": BRANCH, "base": "main", "token": TOKEN}
    ]
    assert hub.edited == []


def test_open_adopts_the_open_pull_request_instead_of_creating(
    conn: sqlite3.Connection,
    repo: Path,
    remote: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    hub.open_url = OTHER
    add_ready_task(conn, worktree, test_sha)
    report = open_pr(conn, repo, tmp_path)
    assert report.state is TaskState.PR_OPEN
    assert report.pr_url == OTHER
    assert report.adopted is True
    assert hub.created == []
    assert hub.added == []
    assert hub.comments == []
    assert hub.order == ["find", "edit"]
    assert len(hub.edited) == 1
    edit = hub.edited[0]
    assert edit["url"] == OTHER
    assert edit["token"] == TOKEN
    assert edit["home"] == REDACT["home"]
    assert edit["orch_home"] == REDACT["orch_home"]
    body = str(edit["body"])
    assert "- `src/app.py`" in body
    assert "- G2_PRE_PR: passed" in body
    task = task_of(conn)
    assert task.state is TaskState.PR_OPEN
    assert task.pr_url == OTHER
    assert task.pushed_sha == implemented
    assert remote_tip(remote) == implemented
    assert events_of(conn) == [("ready", "pr_open")]


def test_the_adopted_body_is_the_body_a_create_would_get(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Same task and gates twice: once created, once adopted, in separate databases.
    add_ready_task(conn, worktree, test_sha)
    open_pr(conn, repo, tmp_path)
    created_body = hub.created[0]["body"]

    other_conn = connect(tmp_path / "other.db")
    add_ready_task(other_conn, worktree, test_sha)
    hub.open_url = OTHER
    open_pr(other_conn, repo, tmp_path)
    assert hub.edited[0]["body"] == created_body


def test_an_adopted_override_gets_the_label(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    add_ready_task(conn, worktree, test_sha, grade=Grade.L3)
    save_block(conn, tmp_path, TRIAGE)
    hub.open_url = OTHER
    report = open_pr(conn, repo, tmp_path)
    assert report.adopted is True
    assert report.user_check is True
    assert hub.created == []
    assert hub.order == ["find", "edit", "add_label"]
    assert hub.added == [{"url": OTHER, "name": "review-overridden", "token": TOKEN}]
    assert task_of(conn).pr_url == OTHER


def test_a_failed_find_keeps_the_push_and_a_rerun_adopts(
    conn: sqlite3.Connection,
    repo: Path,
    remote: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    add_ready_task(conn, worktree, test_sha)
    hub.find_error = GitHubError("pr list: HTTP 502")
    with pytest.raises(GitHubError):
        open_pr(conn, repo, tmp_path)
    task = task_of(conn)
    assert task.state is TaskState.READY
    assert task.pr_url is None
    assert task.pushed_sha == implemented
    assert remote_tip(remote) == implemented
    assert hub.created == []
    assert hub.edited == []

    hub.find_error = None
    hub.open_url = OTHER
    report = open_pr(conn, repo, tmp_path)
    assert report.state is TaskState.PR_OPEN
    assert report.adopted is True
    assert task_of(conn).pr_url == OTHER
    assert hub.created == []


def test_a_failed_edit_records_nothing_and_a_rerun_adopts(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    add_ready_task(conn, worktree, test_sha)
    hub.open_url = OTHER
    hub.edit_error = GitHubError("pr edit: HTTP 502")
    with pytest.raises(GitHubError):
        open_pr(conn, repo, tmp_path)
    task = task_of(conn)
    assert task.state is TaskState.READY
    assert task.pr_url is None
    assert task.pushed_sha == implemented
    assert hub.created == []

    hub.edit_error = None
    report = open_pr(conn, repo, tmp_path)
    assert report.adopted is True
    assert task_of(conn).pr_url == OTHER
    assert len(hub.edited) == 2


def test_a_failed_create_is_adopted_on_the_rerun(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    # The #617 case: create answered with an error, but GitHub made the PR.
    add_ready_task(conn, worktree, test_sha)
    hub.create_error = GitHubError("pr create: HTTP 502")
    with pytest.raises(GitHubError):
        open_pr(conn, repo, tmp_path)
    assert task_of(conn).pr_url is None
    assert len(hub.created) == 1

    hub.create_error = None
    hub.open_url = OTHER
    report = open_pr(conn, repo, tmp_path)
    assert report.adopted is True
    assert report.pr_url == OTHER
    assert len(hub.created) == 1
    assert task_of(conn).state is TaskState.PR_OPEN


def test_an_open_pull_request_is_not_looked_up_again(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    opened: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    hub.found.clear()
    hub.order.clear()
    fix_commit(worktree)
    report = open_pr(conn, repo, tmp_path, resolution="Addressed the review.")
    assert report.state is TaskState.PR_OPEN
    assert report.adopted is False
    assert hub.found == []
    assert hub.edited == []


def test_a_new_attempt_on_a_recorded_pull_request_is_not_looked_up(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    opened: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    return_to_ready(conn, repo, worktree, tmp_path)
    hub.found.clear()
    hub.created.clear()
    report = open_pr(conn, repo, tmp_path)
    assert report.state is TaskState.PR_OPEN
    assert report.adopted is False
    assert hub.found == []
    assert hub.created == []
    assert hub.edited == []


def test_a_rebase_conflict_looks_nothing_up(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    add_ready_task(conn, worktree, test_sha)
    move_main(repo, "src/app.py", "main\n")
    report = open_pr(conn, repo, tmp_path)
    assert report.state is TaskState.CONFLICT
    assert report.adopted is False
    assert hub.found == []
