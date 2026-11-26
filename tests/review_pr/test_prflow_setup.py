"""orch pr and orch update rerun the target setup (m-pr-setup-after-rebase)."""
# ruff: noqa: F811 - fixtures imported from test_prflow are used as parameters

import sqlite3
from pathlib import Path

import pytest

from orch import gate, store
from orch.config import Target
from orch.models import Gate, TaskState
from orch.prflow import PullRequestFlowError
from test_prflow import (  # noqa: F401 - fixtures used by name
    TASK,
    FakeGitHub,
    add_ready_task,
    conn,
    events_of,
    fix_commit,
    gates_of,
    git,
    hub,
    implemented,
    isolated_git,
    make_target,
    move_main,
    open_pr,
    opened,
    remote,
    remote_tip,
    repo,
    resolve_by_rebase,
    task_of,
    test_sha,
    update_pr,
    worktree,
)


class SetupSpy:
    """Records gate.run_command calls for the setup command and can replace them."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, command: str) -> None:
        self.command = command
        self.calls: list[dict[str, object]] = []
        self.result: int | None | Exception = 0
        self.replace = False
        self._real = gate.run_command
        monkeypatch.setattr(gate, "run_command", self)

    def __call__(
        self, command: str, worktree: Path, log_path: Path, **options: object
    ) -> int | None:
        if command == self.command:
            self.calls.append(
                {
                    "worktree": Path(worktree),
                    "log_path": Path(log_path),
                    "head": git(Path(worktree), "rev-parse", "HEAD"),
                    **options,
                }
            )
            if self.replace:
                if isinstance(self.result, Exception):
                    raise self.result
                return self.result
        return self._real(command, worktree, log_path, **options)


def setup_target(repo: Path, order: Path, *, setup_exit: int = 0) -> Target:
    """A target whose setup and full test command write to an order file."""
    setup = f"echo setup >> {order}"
    if setup_exit:
        setup += f"; exit {setup_exit}"
    return make_target(repo, setup_command=setup, test_command=f"echo test >> {order}")


def order_of(order: Path) -> list[str]:
    if not order.exists():
        return []
    return order.read_text(encoding="utf-8").split()


# orch pr


def test_open_runs_the_setup_on_the_rebased_head_before_the_tests(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = tmp_path / "order.txt"
    target = setup_target(repo, order)
    spy = SetupSpy(monkeypatch, target.setup_command)
    add_ready_task(conn, worktree, test_sha, gated_sha=implemented)
    move_main(repo)
    report = open_pr(conn, repo, tmp_path, target=target)
    head = git(worktree, "rev-parse", "HEAD")
    assert head != implemented
    assert report.state is TaskState.PR_OPEN
    assert order_of(order) == ["setup", "test"]
    assert len(spy.calls) == 1
    call = spy.calls[0]
    assert call["worktree"] == worktree
    assert call["head"] == head
    assert call["log_path"] == tmp_path / "logs" / "feat-x.pr.log"
    assert call["sandbox"] is False
    assert call.get("timeout_sec") is None


def test_open_runs_the_setup_without_a_gated_commit(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    order = tmp_path / "order.txt"
    add_ready_task(conn, worktree, test_sha)
    report = open_pr(conn, repo, tmp_path, target=setup_target(repo, order))
    assert report.state is TaskState.PR_OPEN
    assert order_of(order) == ["setup", "test"]


def test_a_skipped_pre_pr_gate_runs_no_setup(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    order = tmp_path / "order.txt"
    add_ready_task(conn, worktree, test_sha, gated_sha=implemented)
    report = open_pr(conn, repo, tmp_path, target=setup_target(repo, order))
    assert report.state is TaskState.PR_OPEN
    assert gates_of(conn)[Gate.G2_PRE_PR].detail == "skipped"
    assert order_of(order) == []


def test_a_rebase_conflict_runs_no_setup(
    conn: sqlite3.Connection,
    repo: Path,
    remote: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    order = tmp_path / "order.txt"
    add_ready_task(conn, worktree, test_sha)
    move_main(repo, "src/app.py", "main\n")
    report = open_pr(conn, repo, tmp_path, target=setup_target(repo, order))
    assert report.state is TaskState.CONFLICT
    assert order_of(order) == []
    assert remote_tip(remote) is None


def test_a_failing_setup_returns_to_judging_without_tests_or_push(
    conn: sqlite3.Connection,
    repo: Path,
    remote: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    order = tmp_path / "order.txt"
    add_ready_task(conn, worktree, test_sha, gated_sha=implemented)
    move_main(repo)
    report = open_pr(
        conn, repo, tmp_path, target=setup_target(repo, order, setup_exit=3)
    )
    assert order_of(order) == ["setup"]
    assert report.state is TaskState.JUDGING
    assert report.detail == "setup 실패: 종료 코드 3"
    result = gates_of(conn)[Gate.G2_PRE_PR]
    assert result.passed is False
    assert result.detail == "setup 실패: 종료 코드 3"
    assert store.suite_run(conn, TASK, 1, Gate.G2_PRE_PR) is None
    task = task_of(conn)
    assert task.state is TaskState.JUDGING
    assert task.gated_sha == implemented
    assert task.pushed_sha is None
    assert events_of(conn) == [("ready", "judging")]
    assert remote_tip(remote) is None
    assert hub.created == []
    assert git(worktree, "status", "--porcelain") == ""


def test_a_setup_timeout_returns_to_judging(
    conn: sqlite3.Connection,
    repo: Path,
    remote: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = tmp_path / "order.txt"
    target = setup_target(repo, order)
    spy = SetupSpy(monkeypatch, target.setup_command)
    spy.replace = True
    spy.result = None
    add_ready_task(conn, worktree, test_sha)
    report = open_pr(conn, repo, tmp_path, target=target)
    assert len(spy.calls) == 1
    assert order_of(order) == []
    assert report.state is TaskState.JUDGING
    assert report.detail == "setup 실패: 시간 초과"
    assert gates_of(conn)[Gate.G2_PRE_PR].detail == "setup 실패: 시간 초과"
    assert remote_tip(remote) is None
    assert hub.created == []


def test_a_failing_setup_after_a_conflict_returns_to_judging(
    conn: sqlite3.Connection,
    repo: Path,
    remote: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    order = tmp_path / "order.txt"
    add_ready_task(conn, worktree, test_sha, state=TaskState.CONFLICT)
    move_main(repo, "src/app.py", "main\n")
    resolve_by_rebase(worktree)
    report = open_pr(
        conn, repo, tmp_path, target=setup_target(repo, order, setup_exit=1)
    )
    assert report.state is TaskState.JUDGING
    assert report.detail == "setup 실패: 종료 코드 1"
    assert order_of(order) == ["setup"]
    assert events_of(conn) == [("conflict", "judging")]
    assert remote_tip(remote) is None


def test_a_fix_whose_setup_fails_is_refused_and_stays_open(
    conn: sqlite3.Connection,
    repo: Path,
    remote: Path,
    worktree: Path,
    opened: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    order = tmp_path / "order.txt"
    fixed = fix_commit(worktree)
    with pytest.raises(PullRequestFlowError, match="setup 실패: 종료 코드 2"):
        open_pr(
            conn,
            repo,
            tmp_path,
            target=setup_target(repo, order, setup_exit=2),
            resolution="Addressed the review.",
        )
    assert order_of(order) == ["setup"]
    task = task_of(conn)
    assert task.state is TaskState.PR_OPEN
    assert task.pushed_sha == opened
    assert task.gated_sha == opened
    assert gates_of(conn)[Gate.G2_PRE_PR].passed is False
    assert git(worktree, "rev-parse", "HEAD") == fixed
    assert remote_tip(remote) == opened
    assert hub.removed == []
    assert hub.comments == []


# orch update


def test_update_runs_the_setup_on_the_merge_before_the_gates(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    opened: str,
    tmp_path: Path,
    hub: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = tmp_path / "order.txt"
    target = setup_target(repo, order)
    spy = SetupSpy(monkeypatch, target.setup_command)
    move_main(repo)
    report = update_pr(conn, repo, tmp_path, target=target)
    assert report.state is TaskState.PR_OPEN
    assert report.pushed_sha is not None
    assert order_of(order)[:2] == ["setup", "test"]
    assert order_of(order).count("setup") == 1
    assert len(spy.calls) == 1
    call = spy.calls[0]
    assert call["worktree"] == worktree
    assert call["head"] == report.pushed_sha
    assert call["log_path"] == tmp_path / "logs" / "feat-x.update.log"
    assert call["sandbox"] is False
    assert call.get("timeout_sec") is None


def test_update_runs_the_setup_on_an_up_to_date_branch(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    opened: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    order = tmp_path / "order.txt"
    report = update_pr(conn, repo, tmp_path, target=setup_target(repo, order))
    assert report.state is TaskState.PR_OPEN
    assert report.pushed_sha is None
    assert order_of(order)[:2] == ["setup", "test"]
    assert order_of(order).count("setup") == 1


def test_an_update_conflict_runs_no_setup(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    opened: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    order = tmp_path / "order.txt"
    move_main(repo, "src/app.py", "main\n")
    report = update_pr(conn, repo, tmp_path, target=setup_target(repo, order))
    assert report.state is TaskState.CONFLICT
    assert order_of(order) == []


def test_a_failing_update_setup_is_refused_and_discards_the_merge(
    conn: sqlite3.Connection,
    repo: Path,
    remote: Path,
    worktree: Path,
    opened: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    order = tmp_path / "order.txt"
    move_main(repo)
    with pytest.raises(
        PullRequestFlowError, match=r"Setup failed: feat-x \(setup 실패: 종료 코드 4\)"
    ):
        update_pr(conn, repo, tmp_path, target=setup_target(repo, order, setup_exit=4))
    assert order_of(order) == ["setup"]
    assert git(worktree, "rev-parse", "HEAD") == opened
    assert git(worktree, "status", "--porcelain") == ""
    assert remote_tip(remote) == opened
    task = task_of(conn)
    assert task.state is TaskState.PR_OPEN
    assert task.pushed_sha == opened
    assert hub.removed == []
    assert hub.comments == []


def test_an_update_setup_timeout_is_refused(
    conn: sqlite3.Connection,
    repo: Path,
    remote: Path,
    worktree: Path,
    opened: str,
    tmp_path: Path,
    hub: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = tmp_path / "order.txt"
    target = setup_target(repo, order)
    spy = SetupSpy(monkeypatch, target.setup_command)
    spy.replace = True
    spy.result = None
    move_main(repo)
    with pytest.raises(PullRequestFlowError, match="setup 실패: 시간 초과"):
        update_pr(conn, repo, tmp_path, target=target)
    assert len(spy.calls) == 1
    assert order_of(order) == []
    assert git(worktree, "rev-parse", "HEAD") == opened
    assert remote_tip(remote) == opened
    assert hub.comments == []


def test_an_error_in_the_update_setup_still_discards_the_merge(
    conn: sqlite3.Connection,
    repo: Path,
    remote: Path,
    worktree: Path,
    opened: str,
    tmp_path: Path,
    hub: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = tmp_path / "order.txt"
    target = setup_target(repo, order)
    spy = SetupSpy(monkeypatch, target.setup_command)
    spy.replace = True
    spy.result = RuntimeError("boom")
    move_main(repo)
    with pytest.raises(RuntimeError, match="boom"):
        update_pr(conn, repo, tmp_path, target=target)
    assert len(spy.calls) == 1
    assert order_of(order) == []
    assert git(worktree, "rev-parse", "HEAD") == opened
    assert remote_tip(remote) == opened
    assert task_of(conn).state is TaskState.PR_OPEN
    assert hub.comments == []
