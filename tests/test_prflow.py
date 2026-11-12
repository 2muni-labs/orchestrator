import ast
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from orch import gate, github, prflow, store
from orch.config import Target
from orch.gate import GateReport, GateStatus
from orch.github import GitHubError, PullRequestStatus
from orch.gitops import GitError, LeaseRejectedError, create_worktree, fetch
from orch.models import (
    Finding,
    Gate,
    GateResult,
    Grade,
    Lane,
    Review,
    Reviewer,
    Severity,
    Task,
    TaskState,
    Verdict,
)
from orch.prflow import (
    PullRequestFlowError,
    build_body,
    close_task,
    open_pull_request,
    update_pull_request,
    user_check,
)
from orch.results import write_review
from orch.store import (
    add_locks,
    connect,
    gate_results,
    get_task,
    insert_task,
    list_locks,
)
from orch.taskfile import TaskSpec

MAIN = "origin/main"
TASK = "feat-x"
BRANCH = "task/feat-x"
URL = "https://github.com/owner/sample/pull/7"
FAKE_HOME = Path("/", "Users", "tester")
REDACT = {"home": FAKE_HOME, "orch_home": FAKE_HOME / ".orch"}
# Assembled at run time so that no token of this shape is in the repository.
TOKEN = "gh" + "s_" + "t0ken" * 8
INTEGRATION_FAILED = "Integration check failed against the latest main. Do not merge."


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def write(path: Path, text: str = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture(autouse=True)
def isolated_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "gitconfig"
    config.write_text(
        "[user]\n\tname = t\n\temail = t@example.com\n[init]\n\tdefaultBranch = main\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    path = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True)
    return path


@pytest.fixture
def repo(tmp_path: Path, remote: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q")
    write(path / "src" / "app.py", "base\n")
    git(path, "add", ".")
    git(path, "commit", "-qm", "base")
    git(path, "remote", "add", "origin", str(remote))
    git(path, "push", "-q", "origin", "main")
    fetch(path)
    return path


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return connect(tmp_path / "state.db")


def commit_all(cwd: Path, message: str) -> str:
    git(cwd, "add", ".")
    git(cwd, "commit", "-qm", message)
    return git(cwd, "rev-parse", "HEAD")


@pytest.fixture
def worktree(repo: Path, tmp_path: Path) -> Path:
    path = tmp_path / "wt"
    create_worktree(repo, path, BRANCH, MAIN)
    return path


@pytest.fixture
def test_sha(worktree: Path) -> str:
    write(worktree / "tests" / "test_x.py")
    return commit_all(worktree, "tests")


@pytest.fixture
def implemented(worktree: Path, test_sha: str) -> str:
    write(worktree / "src" / "app.py", "task\n")
    return commit_all(worktree, "implementation")


def make_spec(**overrides: object) -> TaskSpec:
    values: dict[str, object] = {
        "id": TASK,
        "title": "Add the feature",
        "target": "sample",
        "grade": Grade.L2,
        "lane": Lane.B,
        "model": "m",
        "timeout_min": 45,
        "modify": ("src/**",),
        "read": ("src/**",),
        "verify": "true",
        "depends_on": (),
        "body": "",
    }
    values.update(overrides)
    return TaskSpec(**values)


def make_target(repo: Path, **overrides: object) -> Target:
    values: dict[str, object] = {
        "name": "sample",
        "path": repo,
        "github": "owner/sample",
        "default_branch": "main",
        "setup_command": "true",
        "test_command": "true",
        "lint_command": "true",
    }
    values.update(overrides)
    return Target(**values)


def add_ready_task(
    conn: sqlite3.Connection,
    worktree: Path,
    test_sha: str | None,
    *,
    state: TaskState = TaskState.READY,
    grade: Grade = Grade.L2,
    pushed_sha: str | None = None,
    pr_url: str | None = None,
    base_sha: str | None = None,
    gated_sha: str | None = None,
) -> None:
    insert_task(
        conn,
        Task(
            id=TASK,
            target="sample",
            title="Add feature X",
            grade=grade,
            lane=Lane.B,
            model="m",
            state=state,
            attempt=1,
            timeout_sec=60,
            base_sha=base_sha,
            test_sha=test_sha,
            worktree=str(worktree),
            pushed_sha=pushed_sha,
            pr_url=pr_url,
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        ),
    )
    if gated_sha is not None:
        store.update_task(conn, TASK, gated_sha=gated_sha)
    for name in (Gate.G1, Gate.G2, Gate.G3):
        store.record_gate_result(conn, TASK, 1, name, passed=True)


def task_of(conn: sqlite3.Connection) -> Task:
    task = get_task(conn, TASK)
    assert task is not None
    return task


def gates_of(conn: sqlite3.Connection) -> dict[Gate, GateResult]:
    return {result.gate: result for result in gate_results(conn, TASK, 1)}


def events_of(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT from_state, to_state FROM events WHERE task_id = ? ORDER BY id", (TASK,)
    )
    return list(rows)


def move_main(repo: Path, name: str = "src/other.py", text: str = "main\n") -> str:
    write(repo / name, text)
    sha = commit_all(repo, f"change {name}")
    git(repo, "push", "-q", "origin", "main")
    return sha


def remote_tip(remote: Path, branch: str = BRANCH) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
        cwd=remote,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def resolve_by_rebase(worktree: Path) -> None:
    git(worktree, "fetch", "-q", "origin")
    subprocess.run(
        ["git", "rebase", MAIN], cwd=worktree, capture_output=True, check=False
    )
    write(worktree / "src" / "app.py", "resolved\n")
    git(worktree, "add", "src/app.py")
    git(worktree, "-c", "core.editor=true", "rebase", "--continue")


class FakeGitHub:
    """Stands in for the github module's calls and records them."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.created: list[dict[str, object]] = []
        self.comments: list[dict[str, object]] = []
        self.status_calls: list[dict[str, object]] = []
        self.merged = True
        self.label_present = True
        self.label_calls: list[dict[str, object]] = []
        self.create_error: Exception | None = None
        self.comment_error: Exception | None = None
        self.added: list[dict[str, object]] = []
        self.add_error: Exception | None = None
        monkeypatch.setattr(github, "add_label", self.add_label)
        monkeypatch.setattr(github, "create_pull_request", self.create)
        monkeypatch.setattr(github, "comment_on_pull_request", self.comment)
        monkeypatch.setattr(github, "pull_request_status", self.status)
        monkeypatch.setattr(github, "label_exists", self.label_exists)

    def label_exists(self, **options: object) -> bool:
        self.label_calls.append(options)
        return self.label_present

    def add_label(self, **options: object) -> None:
        self.added.append(options)
        if self.add_error is not None:
            raise self.add_error

    def create(self, **options: object) -> str:
        self.created.append(options)
        if self.create_error is not None:
            raise self.create_error
        return URL

    def comment(self, **options: object) -> None:
        self.comments.append(options)
        if self.comment_error is not None:
            raise self.comment_error

    def status(self, **options: object) -> PullRequestStatus:
        self.status_calls.append(options)
        return PullRequestStatus(
            state="MERGED" if self.merged else "OPEN",
            merged=self.merged,
            merged_at="2026-01-02T00:00:00Z" if self.merged else None,
            mergeable=None,
        )


@pytest.fixture
def hub(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    return FakeGitHub(monkeypatch)


def open_pr(
    conn: sqlite3.Connection,
    repo: Path,
    tmp_path: Path,
    *,
    spec: TaskSpec | None = None,
    target: Target | None = None,
    resolution: str | None = None,
) -> prflow.PullRequestReport:
    return open_pull_request(
        conn,
        spec if spec is not None else make_spec(),
        target if target is not None else make_target(repo),
        token=TOKEN,
        log_path=tmp_path / "logs" / "feat-x.pr.log",
        review_path=tmp_path / "reviews" / "feat-x.json",
        triage_path=tmp_path / "reviews" / "feat-x.triage.md",
        resolution=resolution,
        **REDACT,
    )


def update_pr(
    conn: sqlite3.Connection,
    repo: Path,
    tmp_path: Path,
    target: Target | None = None,
) -> prflow.UpdateReport:
    return update_pull_request(
        conn,
        make_spec(),
        target if target is not None else make_target(repo),
        token=TOKEN,
        log_path=tmp_path / "logs" / "feat-x.update.log",
        **REDACT,
    )


def make_review(verdict: Verdict = Verdict.PASS) -> Review:
    findings = (
        Finding(severity=Severity.P2, path="src/app.py", summary="두 번째 지적"),
        Finding(severity=Severity.P3, path="src/app.py", summary="세 번째 지적"),
    )
    if verdict is Verdict.BLOCK:
        findings += (
            Finding(severity=Severity.P1, path="tests/test_x.py", summary="첫 지적"),
        )
    return Review(
        task_id=TASK,
        reviewer=Reviewer.CLAUDE_REVIEWER,
        verdict=verdict,
        findings=findings,
    )


# The pull request body


def body_task() -> Task:
    return Task(
        id=TASK,
        target="sample",
        title="Add feature X",
        grade=Grade.L2,
        lane=Lane.B,
        model="m",
        state=TaskState.READY,
        timeout_sec=60,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


def gate_result(name: Gate, passed: bool) -> GateResult:
    return GateResult(
        task_id=TASK,
        attempt=1,
        gate=name,
        passed=passed,
        detail=None,
        at="2026-01-01T00:00:00+00:00",
    )


def test_body_names_the_task_files_and_gates() -> None:
    body = build_body(
        body_task(),
        changed=["src/b.py", "src/a.py"],
        gates=[gate_result(Gate.G1, True), gate_result(Gate.G2_PRE_PR, False)],
        review=None,
    )
    assert "## Summary" in body
    assert TASK in body
    assert "Add feature X" in body
    assert "## Changed files" in body
    assert body.index("- `src/a.py`") < body.index("- `src/b.py`")
    assert "## Gates" in body
    assert "- G1: passed" in body
    assert "- G2_PRE_PR: failed" in body
    assert "## Review" in body
    assert "Not required" in body


def test_body_gives_the_review_verdict_and_counts_only() -> None:
    body = build_body(body_task(), changed=[], gates=[], review=make_review())
    assert "PASS" in body
    assert "P2: 1" in body
    assert "P3: 1" in body
    assert "P0" not in body
    assert "지적" not in body


# Opening a pull request


def test_open_pushes_and_creates_the_pull_request(
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
    report = open_pr(conn, repo, tmp_path)
    assert report.state is TaskState.PR_OPEN
    assert report.pr_url == URL
    assert report.conflicts == ()
    assert report.detail is None
    task = task_of(conn)
    assert task.state is TaskState.PR_OPEN
    assert task.pr_url == URL
    assert task.pushed_sha == implemented
    assert remote_tip(remote) == implemented
    assert events_of(conn) == [("ready", "pr_open")]
    assert len(hub.created) == 1
    call = hub.created[0]
    assert call["github"] == "owner/sample"
    assert call["head"] == BRANCH
    assert call["base"] == "main"
    assert call["title"] == "Add feature X"
    assert call["token"] == TOKEN
    assert call["home"] == REDACT["home"]
    assert call["orch_home"] == REDACT["orch_home"]
    assert "- `src/app.py`" in str(call["body"])
    assert "- `tests/test_x.py`" in str(call["body"])
    assert "- G2_PRE_PR: passed" in str(call["body"])


def test_open_records_the_pre_pr_gate(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    add_ready_task(conn, worktree, test_sha)
    open_pr(conn, repo, tmp_path)
    assert gates_of(conn)[Gate.G2_PRE_PR].passed is True
    assert gates_of(conn)[Gate.G2_PRE_PR].detail is None
    assert task_of(conn).gated_sha == implemented


def test_open_rebases_and_updates_the_test_commit(
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
    main_tip = move_main(repo)
    open_pr(conn, repo, tmp_path)
    head = git(worktree, "rev-parse", "HEAD")
    assert head != implemented
    assert git(worktree, "merge-base", MAIN, "HEAD") == main_tip
    task = task_of(conn)
    assert task.pushed_sha == head
    assert remote_tip(remote) == head
    assert task.base_sha == main_tip
    assert task.test_sha == git(worktree, "rev-parse", "HEAD~1")
    assert task.test_sha != test_sha
    assert git(worktree, "log", "-1", "--format=%s", task.test_sha) == "tests"


def test_a_task_without_tests_uses_the_new_base_as_its_test_commit(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    base = git(worktree, "rev-parse", "HEAD")
    write(worktree / "src" / "app.py", "task\n")
    commit_all(worktree, "implementation")
    add_ready_task(conn, worktree, base, base_sha=base)
    main_tip = move_main(repo)
    open_pr(conn, repo, tmp_path, spec=make_spec(tests="none", grade=Grade.L1))
    task = task_of(conn)
    assert task.test_sha == main_tip
    assert task.base_sha == main_tip


def test_open_tells_the_tests_about_tests_none(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    base = git(worktree, "rev-parse", "HEAD")
    write(worktree / "src" / "app.py", "task\n")
    commit_all(worktree, "implementation")
    add_ready_task(conn, worktree, base, base_sha=base)
    report = open_pr(
        conn,
        repo,
        tmp_path,
        spec=make_spec(tests="none", grade=Grade.L1),
        target=make_target(repo, test_command="exit 5"),
    )
    assert report.state is TaskState.PR_OPEN


@pytest.mark.parametrize(
    "state",
    [
        TaskState.JUDGING,
        TaskState.GATING,
        TaskState.REVIEWING,
        TaskState.PR_OPEN,
        TaskState.CLOSED,
    ],
)
def test_open_refuses_other_states(
    conn: sqlite3.Connection,
    repo: Path,
    remote: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
    state: TaskState,
) -> None:
    add_ready_task(conn, worktree, test_sha, state=state)
    with pytest.raises(PullRequestFlowError):
        open_pr(conn, repo, tmp_path)
    assert task_of(conn).state is state
    assert remote_tip(remote) is None
    assert hub.created == []


def test_open_refuses_a_missing_task(
    conn: sqlite3.Connection, repo: Path, tmp_path: Path, hub: FakeGitHub
) -> None:
    with pytest.raises(PullRequestFlowError):
        open_pr(conn, repo, tmp_path)


def test_open_refuses_a_task_without_a_test_commit(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    add_ready_task(conn, worktree, None)
    with pytest.raises(PullRequestFlowError):
        open_pr(conn, repo, tmp_path)
    assert task_of(conn).state is TaskState.READY


def test_a_rebase_conflict_moves_to_conflict_without_pushing(
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
    move_main(repo, "src/app.py")
    report = open_pr(conn, repo, tmp_path)
    assert report.state is TaskState.CONFLICT
    assert report.conflicts == ("src/app.py",)
    assert task_of(conn).state is TaskState.CONFLICT
    assert git(worktree, "rev-parse", "HEAD") == implemented
    assert git(worktree, "status", "--porcelain") == ""
    assert remote_tip(remote) is None
    assert hub.created == []
    assert task_of(conn).test_sha == test_sha


def test_an_unresolved_conflict_stays_in_conflict(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    add_ready_task(conn, worktree, test_sha, state=TaskState.CONFLICT)
    move_main(repo, "src/app.py")
    report = open_pr(conn, repo, tmp_path)
    assert report.state is TaskState.CONFLICT
    assert report.conflicts == ("src/app.py",)
    assert events_of(conn) == []


def test_a_resolved_conflict_before_a_pull_request_pushes_and_creates(
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
    move_main(repo, "src/app.py")
    open_pr(conn, repo, tmp_path)
    resolve_by_rebase(worktree)
    report = open_pr(conn, repo, tmp_path)
    assert report.state is TaskState.PR_OPEN
    assert len(hub.created) == 1
    assert hub.comments == []
    assert remote_tip(remote) == git(worktree, "rev-parse", "HEAD")
    assert events_of(conn) == [("ready", "conflict"), ("conflict", "pr_open")]


def test_a_failing_pre_pr_gate_returns_to_judging_without_pushing(
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
    report = open_pr(
        conn, repo, tmp_path, target=make_target(repo, test_command="exit 1")
    )
    assert report.state is TaskState.JUDGING
    assert report.pr_url is None
    assert report.detail is not None
    assert "exit 1" in report.detail
    assert task_of(conn).state is TaskState.JUDGING
    result = gates_of(conn)[Gate.G2_PRE_PR]
    assert result.passed is False
    assert result.detail == report.detail
    assert remote_tip(remote) is None
    assert hub.created == []


def test_a_failing_pre_pr_gate_after_a_conflict_returns_to_judging(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    add_ready_task(conn, worktree, test_sha, state=TaskState.CONFLICT)
    report = open_pr(
        conn, repo, tmp_path, target=make_target(repo, test_command="exit 1")
    )
    assert report.state is TaskState.JUDGING
    assert events_of(conn) == [("conflict", "judging")]


def test_a_commit_made_during_the_pre_pr_gate_is_not_pushed(
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
    sneak = (
        "echo x > outside.py; git add outside.py; "
        "git -c user.name=t -c user.email=t@example.com commit -qm sneak"
    )
    report = open_pr(conn, repo, tmp_path, target=make_target(repo, test_command=sneak))
    assert report.state is TaskState.JUDGING
    assert report.detail
    assert gates_of(conn)[Gate.G2_PRE_PR].passed is False
    assert task_of(conn).gated_sha is None
    assert remote_tip(remote) is None
    assert hub.created == []


def test_uncommitted_files_from_the_pre_pr_gate_are_not_pushed(
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
    report = open_pr(
        conn, repo, tmp_path, target=make_target(repo, test_command="echo x > stray.py")
    )
    assert report.state is TaskState.PR_OPEN
    tip = remote_tip(remote)
    assert tip == implemented
    assert "stray.py" not in git(remote, "ls-tree", "-r", "--name-only", tip)


def test_a_differing_remote_branch_stops_without_force(
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
    other = git(repo, "rev-parse", "HEAD")
    git(repo, "push", "-q", "origin", f"{other}:refs/heads/{BRANCH}")
    with pytest.raises(GitError):
        open_pr(conn, repo, tmp_path)
    assert remote_tip(remote) == other
    assert task_of(conn).state is TaskState.READY
    assert task_of(conn).pushed_sha is None
    assert hub.created == []


def test_a_dirty_worktree_stops_before_anything_changes(
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
    move_main(repo)
    write(worktree / "src" / "app.py", "dirty\n")
    with pytest.raises(GitError):
        open_pr(conn, repo, tmp_path)
    assert task_of(conn).state is TaskState.READY
    assert remote_tip(remote) is None


def test_a_failed_create_keeps_the_push_and_can_be_retried(
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
    hub.create_error = GitHubError("boom")
    with pytest.raises(GitHubError):
        open_pr(conn, repo, tmp_path)
    task = task_of(conn)
    assert task.state is TaskState.READY
    assert task.pushed_sha == implemented
    assert task.pr_url is None
    hub.create_error = None
    report = open_pr(conn, repo, tmp_path)
    assert report.state is TaskState.PR_OPEN
    assert len(hub.created) == 2
    assert remote_tip(remote) == implemented


def test_the_body_reports_a_saved_review(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    add_ready_task(conn, worktree, test_sha, grade=Grade.L3)
    write_review(tmp_path / "reviews" / "feat-x.json", make_review())
    open_pr(conn, repo, tmp_path)
    assert "PASS" in str(hub.created[0]["body"])


TRIAGE = """+++
follow_up = "feat-x-tests"

[[decisions]]
severity = "P1"
path = "tests/test_x.py"
verdict = "insufficient_tests"
decision = "The code is right; only a check is missing."
+++
"""


def save_block(conn: sqlite3.Connection, tmp_path: Path, triage: str | None) -> None:
    write_review(tmp_path / "reviews" / "feat-x.json", make_review(Verdict.BLOCK))
    if triage is not None:
        write(tmp_path / "reviews" / "feat-x.triage.md", triage)
    add_follow_up(conn)


def add_follow_up(conn: sqlite3.Connection) -> None:
    insert_task(
        conn,
        Task(
            id="feat-x-tests",
            target="sample",
            title="Add the missing check",
            grade=Grade.L2,
            lane=Lane.B,
            model="m",
            state=TaskState.PREPARED,
            timeout_sec=60,
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        ),
    )


def test_an_overridden_block_is_marked_at_the_top_and_labeled(
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
    report = open_pr(conn, repo, tmp_path)
    assert report.state is TaskState.PR_OPEN
    created = hub.created[0]
    body = str(created["body"])
    assert body.startswith("## Review BLOCK overridden\n")
    assert body.index("## Review BLOCK overridden") < body.index("## Summary")
    assert "Follow-up task: `feat-x-tests`" in body
    assert "(insufficient_tests): The code is right" in body
    assert "Verdict: BLOCK" in body
    assert list(created["labels"]) == ["review-overridden"]
    assert hub.added == []
    assert hub.label_calls == [
        {"github": "owner/sample", "name": "review-overridden", "token": TOKEN}
    ]


def assert_nothing_happened(
    conn: sqlite3.Connection, remote: Path, worktree: Path, tip: str, hub: FakeGitHub
) -> None:
    assert task_of(conn).state is TaskState.READY
    assert remote_tip(remote) is None
    assert hub.created == []
    assert git(worktree, "rev-parse", "HEAD") == tip


def test_a_missing_label_stops_before_rebase_and_push(
    conn: sqlite3.Connection,
    repo: Path,
    remote: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    move_main(repo)
    add_ready_task(conn, worktree, test_sha, grade=Grade.L3)
    save_block(conn, tmp_path, TRIAGE)
    hub.label_present = False
    with pytest.raises(PullRequestFlowError, match="review-overridden"):
        open_pr(conn, repo, tmp_path)
    assert_nothing_happened(conn, remote, worktree, implemented, hub)


def test_a_failed_label_check_stops_before_rebase_and_push(
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
    def failed(**options: object) -> bool:
        raise GitHubError("label list: failed")

    monkeypatch.setattr(github, "label_exists", failed)
    add_ready_task(conn, worktree, test_sha, grade=Grade.L3)
    save_block(conn, tmp_path, TRIAGE)
    with pytest.raises(GitHubError):
        open_pr(conn, repo, tmp_path)
    assert_nothing_happened(conn, remote, worktree, implemented, hub)


def test_a_block_without_a_triage_cannot_open_a_pull_request(
    conn: sqlite3.Connection,
    repo: Path,
    remote: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    add_ready_task(conn, worktree, test_sha, grade=Grade.L3)
    save_block(conn, tmp_path, None)
    with pytest.raises(PullRequestFlowError):
        open_pr(conn, repo, tmp_path)
    assert_nothing_happened(conn, remote, worktree, implemented, hub)


@pytest.mark.parametrize(
    "triage",
    [
        "not a triage\n",
        TRIAGE.replace("tests/test_x.py", "tests/other.py"),
        TRIAGE.replace("feat-x-tests", "feat-unregistered"),
    ],
)
def test_a_block_with_a_bad_triage_cannot_open_a_pull_request(
    conn: sqlite3.Connection,
    repo: Path,
    remote: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
    triage: str,
) -> None:
    add_ready_task(conn, worktree, test_sha, grade=Grade.L3)
    save_block(conn, tmp_path, triage)
    with pytest.raises(PullRequestFlowError):
        open_pr(conn, repo, tmp_path)
    assert_nothing_happened(conn, remote, worktree, implemented, hub)


def test_a_pass_needs_no_label_and_gets_none(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    add_ready_task(conn, worktree, test_sha, grade=Grade.L3)
    write_review(tmp_path / "reviews" / "feat-x.json", make_review())
    hub.label_present = False
    assert open_pr(conn, repo, tmp_path).state is TaskState.PR_OPEN
    assert hub.label_calls == []
    assert list(hub.created[0].get("labels", ())) == []
    assert str(hub.created[0]["body"]).startswith("## Summary")


def test_a_stale_triage_is_ignored_when_the_review_passes(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    add_ready_task(conn, worktree, test_sha, grade=Grade.L3)
    write_review(tmp_path / "reviews" / "feat-x.json", make_review())
    write(tmp_path / "reviews" / "feat-x.triage.md", TRIAGE)
    open_pr(conn, repo, tmp_path)
    assert "overridden" not in str(hub.created[0]["body"])


def test_reopening_after_a_conflict_needs_no_label_check(
    conn: sqlite3.Connection,
    repo: Path,
    remote: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    """The open pull request already has its label and its section."""
    git(worktree, "push", "-q", "-u", "origin", BRANCH)
    add_ready_task(
        conn,
        worktree,
        test_sha,
        state=TaskState.CONFLICT,
        grade=Grade.L3,
        pushed_sha=implemented,
        pr_url=URL,
    )
    save_block(conn, tmp_path, TRIAGE)
    hub.label_present = False
    assert open_pr(conn, repo, tmp_path).state is TaskState.PR_OPEN
    assert hub.created == []
    assert hub.label_calls == []
    assert hub.added == []
    assert "Updated after attempt" not in str(hub.comments[0]["body"])


def test_the_review_flag_requires_a_saved_review(
    conn: sqlite3.Connection,
    repo: Path,
    remote: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    """An L2 task with review = true must not reach a pull request unreviewed."""
    add_ready_task(conn, worktree, test_sha, grade=Grade.L2)
    with pytest.raises(PullRequestFlowError):
        open_pr(conn, repo, tmp_path, spec=make_spec(review=True))
    assert_nothing_happened(conn, remote, worktree, implemented, hub)


def test_an_l2_task_without_the_flag_needs_no_review(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    add_ready_task(conn, worktree, test_sha, grade=Grade.L2)
    assert open_pr(conn, repo, tmp_path).state is TaskState.PR_OPEN


@pytest.mark.parametrize("grade", [Grade.L3, Grade.L3R])
def test_a_review_grade_needs_a_saved_review(
    conn: sqlite3.Connection,
    repo: Path,
    remote: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
    grade: Grade,
) -> None:
    add_ready_task(conn, worktree, test_sha, grade=grade)
    with pytest.raises(PullRequestFlowError):
        open_pr(conn, repo, tmp_path)
    assert task_of(conn).state is TaskState.READY
    assert remote_tip(remote) is None


def test_an_invalid_review_stops_before_the_push(
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
    write(tmp_path / "reviews" / "feat-x.json", "{not json")
    with pytest.raises(PullRequestFlowError):
        open_pr(conn, repo, tmp_path)
    assert remote_tip(remote) is None
    assert task_of(conn).state is TaskState.READY


# The token


def test_the_token_stays_out_of_arguments_logs_and_reports(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "gh-record"
    script = bin_dir / "gh"
    script.write_text(
        f"""#!/bin/sh
printf '%s\\n' "$@" > '{record}.argv'
printf '%s' "$GH_TOKEN" > '{record}.token'
cat > /dev/null
echo {URL}
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    add_ready_task(conn, worktree, test_sha)
    report = open_pr(conn, repo, tmp_path, target=make_target(repo, test_command="env"))
    assert report.pr_url == URL
    assert Path(f"{record}.token").read_text(encoding="utf-8") == TOKEN
    assert TOKEN not in Path(f"{record}.argv").read_text(encoding="utf-8")
    log = (tmp_path / "logs" / "feat-x.pr.log").read_text(encoding="utf-8")
    assert TOKEN not in log
    assert TOKEN not in repr(report)


# Updating a branch after a conflict on an open pull request


@pytest.fixture
def opened(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> str:
    add_ready_task(conn, worktree, test_sha)
    open_pr(conn, repo, tmp_path)
    return implemented


def test_a_resolved_conflict_on_an_open_pull_request_updates_with_a_lease(
    conn: sqlite3.Connection,
    repo: Path,
    remote: Path,
    worktree: Path,
    opened: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    move_main(repo, "src/app.py")
    assert update_pr(conn, repo, tmp_path).state is TaskState.CONFLICT
    resolve_by_rebase(worktree)
    report = open_pr(conn, repo, tmp_path, resolution="Kept both changes.")
    head = git(worktree, "rev-parse", "HEAD")
    assert report.state is TaskState.PR_OPEN
    assert report.pr_url == URL
    assert remote_tip(remote) == head
    task = task_of(conn)
    assert task.pushed_sha == head
    assert task.pr_url == URL
    assert len(hub.created) == 1
    assert len(hub.comments) == 1
    comment = hub.comments[0]
    assert comment["url"] == URL
    assert comment["token"] == TOKEN
    assert "Kept both changes." in str(comment["body"])
    assert events_of(conn)[-2:] == [("pr_open", "conflict"), ("conflict", "pr_open")]


def test_the_update_comment_has_a_default_text(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    opened: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    move_main(repo, "src/app.py")
    update_pr(conn, repo, tmp_path)
    resolve_by_rebase(worktree)
    open_pr(conn, repo, tmp_path)
    assert str(hub.comments[0]["body"]).strip()


def test_a_moved_remote_branch_rejects_the_lease(
    conn: sqlite3.Connection,
    repo: Path,
    remote: Path,
    worktree: Path,
    opened: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    move_main(repo, "src/app.py")
    update_pr(conn, repo, tmp_path)
    resolve_by_rebase(worktree)
    other = git(repo, "rev-parse", "HEAD")
    git(repo, "push", "-qf", "origin", f"{other}:refs/heads/{BRANCH}")
    with pytest.raises(LeaseRejectedError):
        open_pr(conn, repo, tmp_path)
    assert remote_tip(remote) == other
    task = task_of(conn)
    assert task.state is TaskState.CONFLICT
    assert task.pushed_sha == opened
    assert hub.comments == []


def test_a_failed_comment_keeps_the_conflict_state_and_can_be_retried(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    opened: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    move_main(repo, "src/app.py")
    update_pr(conn, repo, tmp_path)
    resolve_by_rebase(worktree)
    hub.comment_error = GitHubError("boom")
    with pytest.raises(GitHubError):
        open_pr(conn, repo, tmp_path)
    head = git(worktree, "rev-parse", "HEAD")
    assert task_of(conn).state is TaskState.CONFLICT
    assert task_of(conn).pushed_sha == head
    hub.comment_error = None
    assert open_pr(conn, repo, tmp_path).state is TaskState.PR_OPEN
    assert task_of(conn).pushed_sha == head


# A pushed branch that returns through ready


def return_to_ready(
    conn: sqlite3.Connection, repo: Path, worktree: Path, tmp_path: Path
) -> None:
    """Take an open pull request through a failed pre-PR gate and back to ready."""
    move_main(repo, "src/app.py")
    assert update_pr(conn, repo, tmp_path).state is TaskState.CONFLICT
    resolve_by_rebase(worktree)
    failing = make_target(repo, test_command="exit 1")
    assert open_pr(conn, repo, tmp_path, target=failing).state is TaskState.JUDGING
    store.transition(conn, TASK, TaskState.JUDGING, TaskState.GATING, actor="orch")
    store.transition(conn, TASK, TaskState.GATING, TaskState.READY, actor="orch")


def test_a_pushed_branch_back_in_ready_updates_with_a_lease(
    conn: sqlite3.Connection,
    repo: Path,
    remote: Path,
    worktree: Path,
    opened: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    return_to_ready(conn, repo, worktree, tmp_path)
    assert task_of(conn).pushed_sha == opened
    report = open_pr(conn, repo, tmp_path)
    head = git(worktree, "rev-parse", "HEAD")
    assert head != opened
    assert report.state is TaskState.PR_OPEN
    assert report.pr_url == URL
    assert remote_tip(remote) == head
    assert task_of(conn).pushed_sha == head
    assert len(hub.created) == 1
    assert len(hub.comments) == 1
    assert events_of(conn)[-1] == ("ready", "pr_open")


def test_a_moved_remote_branch_rejects_the_lease_from_ready(
    conn: sqlite3.Connection,
    repo: Path,
    remote: Path,
    worktree: Path,
    opened: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    return_to_ready(conn, repo, worktree, tmp_path)
    other = git(repo, "rev-parse", "HEAD")
    git(repo, "push", "-qf", "origin", f"{other}:refs/heads/{BRANCH}")
    with pytest.raises(LeaseRejectedError):
        open_pr(conn, repo, tmp_path)
    assert remote_tip(remote) == other
    task = task_of(conn)
    assert task.state is TaskState.READY
    assert task.pushed_sha == opened
    assert hub.comments == []


def test_a_failed_create_can_be_retried_after_main_moves(
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
    hub.create_error = GitHubError("boom")
    with pytest.raises(GitHubError):
        open_pr(conn, repo, tmp_path)
    hub.create_error = None
    move_main(repo)
    report = open_pr(conn, repo, tmp_path)
    head = git(worktree, "rev-parse", "HEAD")
    assert head != implemented
    assert report.state is TaskState.PR_OPEN
    assert remote_tip(remote) == head
    assert task_of(conn).pushed_sha == head
    assert len(hub.created) == 2
    assert hub.comments == []


# Skipping the pre-PR gate on a commit that already passed G2


def test_the_pre_pr_gate_is_skipped_on_the_gated_commit(
    conn: sqlite3.Connection,
    repo: Path,
    remote: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    add_ready_task(conn, worktree, test_sha, gated_sha=implemented)
    report = open_pr(
        conn, repo, tmp_path, target=make_target(repo, test_command="exit 1")
    )
    assert report.state is TaskState.PR_OPEN
    assert report.detail is None
    result = gates_of(conn)[Gate.G2_PRE_PR]
    assert result.passed is True
    assert result.detail == "skipped"
    task = task_of(conn)
    assert task.gated_sha == implemented
    assert task.pushed_sha == implemented
    assert remote_tip(remote) == implemented
    assert len(hub.created) == 1
    assert "- G2_PRE_PR: passed" in str(hub.created[0]["body"])


def test_a_skipped_pre_pr_gate_runs_no_test_command(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_ready_task(conn, worktree, test_sha, gated_sha=implemented)
    calls: list[object] = []

    def run_tests(*args: object, **kwargs: object) -> str | None:
        calls.append(args)
        return "should not run"

    monkeypatch.setattr(gate, "run_tests", run_tests)
    report = open_pr(conn, repo, tmp_path)
    assert report.state is TaskState.PR_OPEN
    assert calls == []


def test_the_pre_pr_gate_runs_when_main_moved_since_the_gate(
    conn: sqlite3.Connection,
    repo: Path,
    remote: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    add_ready_task(conn, worktree, test_sha, gated_sha=implemented)
    move_main(repo)
    report = open_pr(
        conn, repo, tmp_path, target=make_target(repo, test_command="exit 1")
    )
    assert report.state is TaskState.JUDGING
    assert gates_of(conn)[Gate.G2_PRE_PR].passed is False
    assert task_of(conn).gated_sha == implemented
    assert remote_tip(remote) is None
    assert hub.created == []


def test_a_passing_pre_pr_gate_after_main_moved_records_the_new_head(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    add_ready_task(conn, worktree, test_sha, gated_sha=implemented)
    move_main(repo)
    report = open_pr(conn, repo, tmp_path)
    head = git(worktree, "rev-parse", "HEAD")
    assert head != implemented
    assert report.state is TaskState.PR_OPEN
    assert gates_of(conn)[Gate.G2_PRE_PR].detail is None
    assert task_of(conn).gated_sha == head


def test_the_pre_pr_gate_runs_when_the_head_differs_from_the_gated_commit(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    add_ready_task(
        conn, worktree, test_sha, state=TaskState.CONFLICT, gated_sha=test_sha
    )
    report = open_pr(
        conn, repo, tmp_path, target=make_target(repo, test_command="exit 1")
    )
    assert report.state is TaskState.JUDGING
    assert events_of(conn) == [("conflict", "judging")]
    assert task_of(conn).gated_sha == test_sha


def test_a_failing_pre_pr_gate_leaves_the_gated_commit_alone(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    add_ready_task(conn, worktree, test_sha, gated_sha="f" * 40)
    report = open_pr(
        conn, repo, tmp_path, target=make_target(repo, test_command="exit 1")
    )
    assert report.state is TaskState.JUDGING
    assert task_of(conn).gated_sha == "f" * 40


def test_rerunning_after_a_failed_create_skips_the_pre_pr_gate(
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
    hub.create_error = GitHubError("boom")
    with pytest.raises(GitHubError):
        open_pr(conn, repo, tmp_path)
    assert task_of(conn).gated_sha == implemented
    hub.create_error = None
    report = open_pr(
        conn, repo, tmp_path, target=make_target(repo, test_command="exit 1")
    )
    assert report.state is TaskState.PR_OPEN
    assert gates_of(conn)[Gate.G2_PRE_PR].detail == "skipped"
    assert remote_tip(remote) == implemented
    assert len(hub.created) == 2


# orch update


def test_update_without_a_conflict_runs_the_gates_and_pushes_nothing(
    conn: sqlite3.Connection,
    repo: Path,
    remote: Path,
    worktree: Path,
    opened: str,
    tmp_path: Path,
) -> None:
    move_main(repo)
    report = update_pr(conn, repo, tmp_path)
    assert report.state is TaskState.PR_OPEN
    assert report.conflicts == ()
    assert report.gates is not None
    assert report.gates.passed is True
    assert git(worktree, "rev-parse", "HEAD") == opened
    assert remote_tip(remote) == opened
    assert task_of(conn).state is TaskState.PR_OPEN
    assert task_of(conn).pushed_sha == opened


def test_update_ignores_files_from_the_default_branch_in_the_scope_check(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    opened: str,
    tmp_path: Path,
) -> None:
    move_main(repo, "outside/main_only.py")
    report = update_pr(conn, repo, tmp_path)
    assert report.gates is not None
    assert report.gates.gates["G1"] is GateStatus.PASSED


def test_update_still_passes_scope_after_open_rebased_the_task(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    add_ready_task(conn, worktree, test_sha)
    move_main(repo, "outside/first.py")
    open_pr(conn, repo, tmp_path)
    move_main(repo, "outside/second.py")
    report = update_pr(conn, repo, tmp_path)
    assert report.gates is not None
    assert report.gates.passed is True


def test_a_failing_update_gate_reports_and_discards_the_merge(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    opened: str,
    tmp_path: Path,
) -> None:
    move_main(repo)
    report = update_pr(
        conn, repo, tmp_path, target=make_target(repo, test_command="exit 1")
    )
    assert report.state is TaskState.PR_OPEN
    assert report.gates is not None
    assert report.gates.passed is False
    assert report.gates.failed_gate == "G2"
    assert git(worktree, "rev-parse", "HEAD") == opened
    assert task_of(conn).state is TaskState.PR_OPEN


def test_an_error_during_update_still_discards_the_merge(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    opened: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **options: object) -> GateReport:
        raise RuntimeError("boom")

    monkeypatch.setattr(gate, "run_gates", fail)
    move_main(repo)
    with pytest.raises(RuntimeError):
        update_pr(conn, repo, tmp_path)
    assert git(worktree, "rev-parse", "HEAD") == opened
    assert task_of(conn).state is TaskState.PR_OPEN


def test_update_removes_files_left_by_the_gates(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    opened: str,
    tmp_path: Path,
) -> None:
    move_main(repo)
    update_pr(
        conn, repo, tmp_path, target=make_target(repo, test_command="echo x > src/y.py")
    )
    assert git(worktree, "status", "--porcelain") == ""


def test_an_update_conflict_moves_to_conflict_and_leaves_the_worktree_clean(
    conn: sqlite3.Connection,
    repo: Path,
    remote: Path,
    worktree: Path,
    opened: str,
    tmp_path: Path,
) -> None:
    move_main(repo, "src/app.py")
    report = update_pr(conn, repo, tmp_path)
    assert report.state is TaskState.CONFLICT
    assert report.conflicts == ("src/app.py",)
    assert report.gates is None
    assert task_of(conn).state is TaskState.CONFLICT
    assert git(worktree, "rev-parse", "HEAD") == opened
    assert git(worktree, "status", "--porcelain") == ""
    assert remote_tip(remote) == opened


def test_update_refuses_a_task_that_is_not_open(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    tmp_path: Path,
) -> None:
    add_ready_task(conn, worktree, test_sha)
    with pytest.raises(PullRequestFlowError):
        update_pr(conn, repo, tmp_path)
    assert task_of(conn).state is TaskState.READY


def test_update_refuses_a_task_with_no_pushed_commit(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    tmp_path: Path,
) -> None:
    add_ready_task(conn, worktree, test_sha, state=TaskState.PR_OPEN)
    with pytest.raises(PullRequestFlowError):
        update_pr(conn, repo, tmp_path)


# The comment after a failed integration check


@pytest.mark.parametrize(
    ("overrides", "failed_gate"),
    [
        ({"test_command": "echo x > outside.py"}, "G1"),
        ({"test_command": "exit 1"}, "G2"),
        ({"lint_command": "exit 1"}, "G3"),
    ],
)
def test_a_failing_update_gate_comments_on_the_pull_request(
    conn: sqlite3.Connection,
    repo: Path,
    remote: Path,
    worktree: Path,
    opened: str,
    tmp_path: Path,
    hub: FakeGitHub,
    overrides: dict[str, str],
    failed_gate: str,
) -> None:
    move_main(repo)
    report = update_pr(conn, repo, tmp_path, target=make_target(repo, **overrides))
    assert report.gates is not None
    assert report.gates.failed_gate == failed_gate
    assert hub.comments == [
        {"url": URL, "body": INTEGRATION_FAILED, "token": TOKEN, **REDACT}
    ]
    assert report.state is TaskState.PR_OPEN
    assert task_of(conn).state is TaskState.PR_OPEN
    assert task_of(conn).pushed_sha == opened
    assert git(worktree, "rev-parse", "HEAD") == opened
    assert remote_tip(remote) == opened


def test_a_passing_update_gate_leaves_no_comment(
    conn: sqlite3.Connection,
    repo: Path,
    opened: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    move_main(repo)
    report = update_pr(conn, repo, tmp_path)
    assert report.gates is not None
    assert report.gates.passed is True
    assert hub.comments == []


def test_an_update_conflict_leaves_no_integration_comment(
    conn: sqlite3.Connection,
    repo: Path,
    opened: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    move_main(repo, "src/app.py")
    assert update_pr(conn, repo, tmp_path).state is TaskState.CONFLICT
    assert hub.comments == []


def test_an_error_during_update_leaves_no_comment(
    conn: sqlite3.Connection,
    repo: Path,
    opened: str,
    tmp_path: Path,
    hub: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **options: object) -> GateReport:
        raise RuntimeError("boom")

    monkeypatch.setattr(gate, "run_gates", fail)
    move_main(repo)
    with pytest.raises(RuntimeError):
        update_pr(conn, repo, tmp_path)
    assert hub.comments == []


def test_a_failed_integration_comment_raises_and_still_discards_the_merge(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    opened: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    move_main(repo)
    hub.comment_error = GitHubError("boom")
    with pytest.raises(GitHubError):
        update_pr(conn, repo, tmp_path, target=make_target(repo, test_command="exit 1"))
    assert git(worktree, "rev-parse", "HEAD") == opened
    assert task_of(conn).state is TaskState.PR_OPEN


def test_update_refuses_a_task_with_no_pull_request_url(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    add_ready_task(
        conn, worktree, test_sha, state=TaskState.PR_OPEN, pushed_sha=implemented
    )
    move_main(repo)
    with pytest.raises(PullRequestFlowError):
        update_pr(conn, repo, tmp_path, target=make_target(repo, test_command="exit 1"))
    assert hub.comments == []
    assert git(worktree, "rev-parse", "HEAD") == implemented


# orch close


def close(conn: sqlite3.Connection, repo: Path) -> None:
    close_task(conn, TASK, make_target(repo), token=TOKEN)


def test_close_cleans_up_a_merged_task(
    conn: sqlite3.Connection,
    repo: Path,
    remote: Path,
    worktree: Path,
    opened: str,
    hub: FakeGitHub,
) -> None:
    add_locks(conn, "sample", TASK, ("src/**",))
    close(conn, repo)
    assert task_of(conn).state is TaskState.CLOSED
    assert not worktree.exists()
    assert BRANCH not in git(repo, "branch", "--list", BRANCH)
    assert list_locks(conn, "sample") == ()
    assert remote_tip(remote) == opened
    assert hub.status_calls == [{"url": URL, "token": TOKEN}]


def to_conflict(conn: sqlite3.Connection) -> None:
    store.transition(conn, TASK, TaskState.PR_OPEN, TaskState.CONFLICT, actor="orch")


def test_close_cleans_up_a_merged_task_in_conflict(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    opened: str,
    hub: FakeGitHub,
) -> None:
    to_conflict(conn)
    close(conn, repo)
    assert task_of(conn).state is TaskState.CLOSED
    assert not worktree.exists()
    assert BRANCH not in git(repo, "branch", "--list", BRANCH)
    last = conn.execute(
        "SELECT from_state, to_state FROM events WHERE task_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (TASK,),
    ).fetchone()
    assert last == ("conflict", "closed")
    assert hub.status_calls == [{"url": URL, "token": TOKEN}]


def test_close_refuses_an_unmerged_pull_request_in_conflict(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    opened: str,
    hub: FakeGitHub,
) -> None:
    to_conflict(conn)
    hub.merged = False
    with pytest.raises(PullRequestFlowError):
        close(conn, repo)
    assert task_of(conn).state is TaskState.CONFLICT
    assert worktree.exists()


def test_close_refuses_a_conflict_without_a_pull_request(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    hub: FakeGitHub,
) -> None:
    add_ready_task(conn, worktree, test_sha, state=TaskState.CONFLICT)
    with pytest.raises(PullRequestFlowError):
        close(conn, repo)
    assert task_of(conn).state is TaskState.CONFLICT
    assert hub.status_calls == []


def test_close_refuses_an_unmerged_pull_request(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    opened: str,
    hub: FakeGitHub,
) -> None:
    hub.merged = False
    with pytest.raises(PullRequestFlowError):
        close(conn, repo)
    assert task_of(conn).state is TaskState.PR_OPEN
    assert worktree.exists()
    assert BRANCH in git(repo, "branch", "--list", BRANCH)


def test_close_refuses_other_states(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    hub: FakeGitHub,
) -> None:
    add_ready_task(conn, worktree, test_sha)
    with pytest.raises(PullRequestFlowError):
        close(conn, repo)
    assert hub.status_calls == []


def test_close_refuses_a_missing_task(
    conn: sqlite3.Connection, repo: Path, hub: FakeGitHub
) -> None:
    with pytest.raises(PullRequestFlowError):
        close(conn, repo)


def test_close_refuses_a_task_without_a_pull_request(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    hub: FakeGitHub,
) -> None:
    add_ready_task(conn, worktree, test_sha, state=TaskState.PR_OPEN)
    with pytest.raises(PullRequestFlowError):
        close(conn, repo)
    assert hub.status_calls == []


def test_close_finishes_after_a_partial_cleanup(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    opened: str,
    hub: FakeGitHub,
) -> None:
    git(repo, "worktree", "remove", "--force", str(worktree))
    close(conn, repo)
    assert task_of(conn).state is TaskState.CLOSED
    assert BRANCH not in git(repo, "branch", "--list", BRANCH)


def test_close_finishes_when_the_branch_is_already_gone(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    opened: str,
    hub: FakeGitHub,
) -> None:
    git(repo, "worktree", "remove", "--force", str(worktree))
    git(repo, "branch", "-qD", BRANCH)
    close(conn, repo)
    assert task_of(conn).state is TaskState.CLOSED


def test_close_removes_a_worktree_with_stray_files(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    opened: str,
    hub: FakeGitHub,
) -> None:
    write(worktree / "stray.py")
    close(conn, repo)
    assert not worktree.exists()


def test_close_leaves_another_task_alone(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    opened: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    other_tree = tmp_path / "other-wt"
    create_worktree(repo, other_tree, "task/other", MAIN)
    insert_task(
        conn,
        Task(
            id="other",
            target="sample",
            title="Other",
            grade=Grade.L2,
            lane=Lane.B,
            model="m",
            state=TaskState.PR_OPEN,
            attempt=1,
            timeout_sec=60,
            test_sha=opened,
            worktree=str(other_tree),
            pushed_sha=opened,
            pr_url="https://github.com/owner/sample/pull/8",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        ),
    )
    add_locks(conn, "sample", TASK, ("src/**",))
    add_locks(conn, "sample", "other", ("docs/**",))
    close(conn, repo)
    other = get_task(conn, "other")
    assert other is not None
    assert other.state is TaskState.PR_OPEN
    assert other_tree.exists()
    assert git(repo, "branch", "--list", "task/other").strip()
    assert [(lock.task_id, lock.path_glob) for lock in list_locks(conn, "sample")] == [
        ("other", "docs/**")
    ]
    assert task_of(conn).state is TaskState.CLOSED


# The token in exception messages


def failing_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Put a gh on PATH that fails and writes the token and arguments to stderr."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "gh"
    script.write_text(
        """#!/bin/sh
cat > /dev/null
echo "denied $GH_TOKEN $*" >&2
exit 1
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def assert_no_token(error: BaseException) -> None:
    seen: BaseException | None = error
    while seen is not None:
        assert TOKEN not in str(seen)
        assert TOKEN not in repr(seen)
        seen = seen.__cause__ or seen.__context__


def test_the_token_stays_out_of_a_failed_create(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failing_gh(tmp_path, monkeypatch)
    add_ready_task(conn, worktree, test_sha)
    with pytest.raises(GitHubError) as caught:
        open_pr(conn, repo, tmp_path)
    assert_no_token(caught.value)


def test_the_token_stays_out_of_a_failed_conflict_comment(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failing_gh(tmp_path, monkeypatch)
    add_ready_task(conn, worktree, test_sha, state=TaskState.CONFLICT, pr_url=URL)
    with pytest.raises(GitHubError) as caught:
        open_pr(conn, repo, tmp_path)
    assert_no_token(caught.value)


def test_the_token_stays_out_of_a_failed_integration_comment(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failing_gh(tmp_path, monkeypatch)
    add_ready_task(
        conn,
        worktree,
        test_sha,
        state=TaskState.PR_OPEN,
        pushed_sha=implemented,
        pr_url=URL,
    )
    move_main(repo)
    with pytest.raises(GitHubError) as caught:
        update_pr(conn, repo, tmp_path, target=make_target(repo, test_command="exit 1"))
    assert_no_token(caught.value)


def test_the_token_stays_out_of_a_failed_status_check(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failing_gh(tmp_path, monkeypatch)
    add_ready_task(
        conn,
        worktree,
        test_sha,
        state=TaskState.PR_OPEN,
        pushed_sha=implemented,
        pr_url=URL,
    )
    with pytest.raises(GitHubError) as caught:
        close(conn, repo)
    assert_no_token(caught.value)
    assert task_of(conn).state is TaskState.PR_OPEN


# Conventions


def test_module_uses_only_the_standard_library_and_orch() -> None:
    tree = ast.parse(Path(prflow.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module is not None
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Constant) and node.value in ("git", "gh"):
            pytest.fail("prflow.py must not name the git or gh command")
    assert imported - {"orch"} <= sys.stdlib_module_names


# user check


@pytest.mark.parametrize(
    ("changed", "safety", "overridden", "expected"),
    [
        ((), (), False, False),
        (("src/app.py",), (), False, False),
        (("src/app.py",), (), True, True),
        ((), ("CLAUDE.md",), True, True),
        (("CLAUDE.md",), ("CLAUDE.md",), False, True),
        (("docs/CLAUDE.md",), ("CLAUDE.md",), False, False),
        (("src/orch/notify.py",), ("src/orch/notify.py",), False, True),
        (("src/orch/notify.pyc",), ("src/orch/notify.py",), False, False),
        (("scripts/gate.sh",), ("scripts/**",), False, True),
        (("scripts/a/b/c.sh",), ("scripts/**",), False, True),
        (("scripts",), ("scripts/**",), False, True),
        (("scriptsx/a.sh",), ("scripts/**",), False, False),
        ((".claude/agents/r.md",), (".claude/agents/",), False, True),
        ((".claude/settings.json",), (".claude/agents/",), False, False),
        (("src/app.py", "scripts/run.sh"), ("CLAUDE.md", "scripts/**"), False, True),
    ],
)
def test_user_check(
    changed: tuple[str, ...],
    safety: tuple[str, ...],
    overridden: bool,
    expected: bool,
) -> None:
    assert user_check(changed, safety, overridden=overridden) is expected


def test_user_check_accepts_any_iterable() -> None:
    assert user_check(iter(["CLAUDE.md"]), iter(["CLAUDE.md"]), overridden=False)


@pytest.mark.parametrize(
    ("safety", "expected"),
    [
        ((), False),
        (("src/app.py",), True),
        (("tests/**",), True),
        (("src/other.py",), False),
        (("scripts/**", "docs/"), False),
    ],
)
def test_open_reports_user_check_from_the_task_changes(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
    safety: tuple[str, ...],
    expected: bool,
) -> None:
    add_ready_task(conn, worktree, test_sha)
    report = open_pr(
        conn, repo, tmp_path, target=make_target(repo, safety_paths=safety)
    )
    assert report.state is TaskState.PR_OPEN
    assert report.user_check is expected


def test_changes_on_main_do_not_count_for_user_check(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    add_ready_task(conn, worktree, test_sha)
    move_main(repo, "scripts/gate.sh")
    report = open_pr(
        conn, repo, tmp_path, target=make_target(repo, safety_paths=("scripts/**",))
    )
    assert report.state is TaskState.PR_OPEN
    assert report.user_check is False


def test_an_overridden_block_is_a_user_check_without_safety_paths(
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
    report = open_pr(conn, repo, tmp_path)
    assert report.state is TaskState.PR_OPEN
    assert report.user_check is True


def test_a_passing_review_is_not_a_user_check_by_itself(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    add_ready_task(conn, worktree, test_sha, grade=Grade.L3)
    write_review(tmp_path / "reviews" / "feat-x.json", make_review())
    report = open_pr(conn, repo, tmp_path)
    assert report.state is TaskState.PR_OPEN
    assert report.user_check is False


def test_an_update_of_an_open_pull_request_reports_user_check(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    opened: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    move_main(repo, "src/app.py")
    assert update_pr(conn, repo, tmp_path).state is TaskState.CONFLICT
    resolve_by_rebase(worktree)
    report = open_pr(
        conn, repo, tmp_path, target=make_target(repo, safety_paths=("src/app.py",))
    )
    assert report.state is TaskState.PR_OPEN
    assert len(hub.comments) == 1
    assert report.user_check is True


def test_a_conflict_or_failed_gate_is_never_a_user_check(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    add_ready_task(conn, worktree, test_sha)
    failing = make_target(repo, test_command="exit 1", safety_paths=("src/**",))
    judged = open_pr(conn, repo, tmp_path, target=failing)
    assert judged.state is TaskState.JUDGING
    assert judged.user_check is False
    store.transition(conn, TASK, TaskState.JUDGING, TaskState.GATING, actor="test")
    store.transition(conn, TASK, TaskState.GATING, TaskState.READY, actor="test")
    move_main(repo, "src/app.py")
    conflicted = open_pr(
        conn, repo, tmp_path, target=make_target(repo, safety_paths=("src/**",))
    )
    assert conflicted.state is TaskState.CONFLICT
    assert conflicted.user_check is False


# Returning the worktree to its commit after the pre-PR gate


def porcelain(worktree: Path) -> str:
    return git(worktree, "status", "--porcelain")


LITTER = "echo x > stray.py; echo changed > src/app.py"


def test_a_failing_pre_pr_gate_leaves_a_clean_worktree(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    add_ready_task(conn, worktree, test_sha)
    target = make_target(repo, test_command=f"{LITTER}; exit 1")
    assert open_pr(conn, repo, tmp_path, target=target).state is TaskState.JUDGING
    assert git(worktree, "rev-parse", "HEAD") == implemented
    assert porcelain(worktree) == ""
    assert not (worktree / "stray.py").exists()
    assert (worktree / "src" / "app.py").read_text(encoding="utf-8") == "task\n"


def test_a_passing_pre_pr_gate_leaves_a_clean_worktree(
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
    target = make_target(repo, test_command=LITTER)
    assert open_pr(conn, repo, tmp_path, target=target).state is TaskState.PR_OPEN
    assert remote_tip(remote) == implemented
    assert git(worktree, "rev-parse", "HEAD") == implemented
    assert porcelain(worktree) == ""


def test_a_commit_made_during_the_pre_pr_gate_is_discarded(
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
    sneak = (
        "echo x > outside.py; git add outside.py; "
        "git -c user.name=t -c user.email=t@example.com commit -qm sneak"
    )
    report = open_pr(conn, repo, tmp_path, target=make_target(repo, test_command=sneak))
    assert report.state is TaskState.JUDGING
    assert report.detail == "G2 실행 중 커밋이 생겼습니다"
    assert git(worktree, "rev-parse", "HEAD") == implemented
    assert not (worktree / "outside.py").exists()
    assert porcelain(worktree) == ""
    assert remote_tip(remote) is None


def test_a_skipped_pre_pr_gate_does_not_reset(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resets: list[tuple[object, ...]] = []
    original = prflow.gitops.reset_to

    def spy(*args: object) -> None:
        resets.append(args)
        original(*args)

    monkeypatch.setattr(prflow.gitops, "reset_to", spy)
    add_ready_task(conn, worktree, test_sha, gated_sha=implemented)
    assert open_pr(conn, repo, tmp_path).state is TaskState.PR_OPEN
    assert gates_of(conn)[Gate.G2_PRE_PR].detail == "skipped"
    assert resets == []


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("exit 1", ["record", "reset", "transition"]),
        ("true", ["record", "reset", "push", "transition"]),
    ],
)
def test_the_pre_pr_gate_is_recorded_then_reset_then_acted_on(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    expected: list[str],
) -> None:
    add_ready_task(conn, worktree, test_sha)
    order: list[str] = []

    def wrap(module: object, name: str, label: str, *, only_pre_pr: bool = False):
        original = getattr(module, name)

        def spy(*args: object, **kwargs: object) -> object:
            if not only_pre_pr or Gate.G2_PRE_PR in args:
                order.append(label)
            return original(*args, **kwargs)

        monkeypatch.setattr(module, name, spy)

    wrap(store, "record_gate_result", "record", only_pre_pr=True)
    wrap(prflow.gitops, "reset_to", "reset")
    wrap(prflow.gitops, "push_task_branch", "push")
    wrap(store, "transition", "transition")
    open_pr(conn, repo, tmp_path, target=make_target(repo, test_command=command))
    assert order == expected


def test_a_failed_reset_after_a_failing_gate_keeps_the_state_and_can_be_rerun(
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
    add_ready_task(conn, worktree, test_sha)
    original = prflow.gitops.reset_to
    broken = {"on": True}

    def reset(*args: object) -> None:
        if broken["on"]:
            raise GitError("reset failed")
        original(*args)

    monkeypatch.setattr(prflow.gitops, "reset_to", reset)
    failing = make_target(repo, test_command="exit 1")
    with pytest.raises(GitError, match="reset failed"):
        open_pr(conn, repo, tmp_path, target=failing)
    assert task_of(conn).state is TaskState.READY
    assert events_of(conn) == []
    assert gates_of(conn)[Gate.G2_PRE_PR].passed is False
    assert remote_tip(remote) is None
    assert hub.created == []
    broken["on"] = False
    report = open_pr(conn, repo, tmp_path, target=failing)
    assert report.state is TaskState.JUDGING
    assert events_of(conn) == [("ready", "judging")]


def test_a_failed_reset_after_a_passing_gate_keeps_the_state_and_can_be_rerun(
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
    add_ready_task(conn, worktree, test_sha)
    original = prflow.gitops.reset_to
    broken = {"on": True}

    def reset(*args: object) -> None:
        if broken["on"]:
            raise GitError("reset failed")
        original(*args)

    monkeypatch.setattr(prflow.gitops, "reset_to", reset)
    with pytest.raises(GitError, match="reset failed"):
        open_pr(conn, repo, tmp_path)
    task = task_of(conn)
    assert task.state is TaskState.READY
    assert task.gated_sha == implemented
    assert task.pushed_sha is None
    assert gates_of(conn)[Gate.G2_PRE_PR].passed is True
    assert remote_tip(remote) is None
    assert hub.created == []
    broken["on"] = False
    report = open_pr(conn, repo, tmp_path)
    assert report.state is TaskState.PR_OPEN
    assert gates_of(conn)[Gate.G2_PRE_PR].detail == "skipped"
    assert remote_tip(remote) == implemented


def test_a_failing_pre_pr_gate_after_main_moved_keeps_the_rebased_commits(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    add_ready_task(conn, worktree, test_sha, base_sha=git(repo, "rev-parse", "HEAD"))
    new_main = move_main(repo)
    failing = make_target(repo, test_command="exit 1")
    assert open_pr(conn, repo, tmp_path, target=failing).state is TaskState.JUDGING
    task = task_of(conn)
    assert task.base_sha == new_main
    commits = git(worktree, "rev-list", "--reverse", f"{new_main}..HEAD").split()
    assert len(commits) == 2
    assert task.test_sha == commits[0]
    assert task.test_sha != test_sha
    assert git(worktree, "rev-parse", "HEAD") == commits[1]
    assert porcelain(worktree) == ""


# A new attempt on an open pull request


@pytest.fixture
def reopened(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    opened: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> str:
    """An open pull request back in ready with a new, unpushed commit."""
    return_to_ready(conn, repo, worktree, tmp_path)
    head = git(worktree, "rev-parse", "HEAD")
    assert head != opened
    assert task_of(conn).pushed_sha == opened
    hub.comments.clear()
    hub.label_calls.clear()
    return head


def test_a_new_override_on_an_open_pull_request_adds_the_label(
    conn: sqlite3.Connection,
    repo: Path,
    remote: Path,
    reopened: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    save_block(conn, tmp_path, TRIAGE)
    report = open_pr(conn, repo, tmp_path)
    assert report.state is TaskState.PR_OPEN
    assert report.pr_url == URL
    assert report.user_check is True
    assert remote_tip(remote) == reopened
    assert len(hub.created) == 1
    assert hub.label_calls == [
        {"github": "owner/sample", "name": "review-overridden", "token": TOKEN}
    ]
    assert hub.added == [{"url": URL, "name": "review-overridden", "token": TOKEN}]
    assert len(hub.comments) == 1
    body = str(hub.comments[0]["body"])
    assert body.startswith("Updated after attempt 1.\n\n")
    assert "## Review BLOCK overridden" in body
    assert "Follow-up task: `feat-x-tests`" in body
    assert "Verdict: BLOCK" in body
    assert "## Gates" in body
    assert "G2_PRE_PR: passed" in body
    assert body.rstrip().endswith(f"Commit: `{reopened}`")
    assert events_of(conn)[-1] == ("ready", "pr_open")


def test_a_missing_label_stops_a_new_override_before_rebase_and_push(
    conn: sqlite3.Connection,
    repo: Path,
    remote: Path,
    worktree: Path,
    opened: str,
    reopened: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    save_block(conn, tmp_path, TRIAGE)
    move_main(repo)
    hub.label_present = False
    with pytest.raises(PullRequestFlowError, match="review-overridden"):
        open_pr(conn, repo, tmp_path)
    task = task_of(conn)
    assert task.state is TaskState.READY
    assert task.pushed_sha == opened
    assert remote_tip(remote) == opened
    assert git(worktree, "rev-parse", "HEAD") == reopened
    assert hub.added == []
    assert hub.comments == []


@pytest.mark.parametrize(
    "triage", [None, "not a triage\n", TRIAGE.replace("feat-x-tests", "feat-none")]
)
def test_a_new_block_without_a_good_triage_is_refused_on_an_open_pull_request(
    conn: sqlite3.Connection,
    repo: Path,
    remote: Path,
    worktree: Path,
    opened: str,
    reopened: str,
    tmp_path: Path,
    hub: FakeGitHub,
    triage: str | None,
) -> None:
    save_block(conn, tmp_path, triage)
    with pytest.raises(PullRequestFlowError):
        open_pr(conn, repo, tmp_path)
    assert task_of(conn).state is TaskState.READY
    assert remote_tip(remote) == opened
    assert git(worktree, "rev-parse", "HEAD") == reopened
    assert hub.added == []
    assert hub.comments == []


def test_a_failed_label_add_keeps_the_push_and_a_rerun_finishes(
    conn: sqlite3.Connection,
    repo: Path,
    remote: Path,
    reopened: str,
    tmp_path: Path,
    hub: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save_block(conn, tmp_path, TRIAGE)
    hub.add_error = GitHubError("pr edit: failed")
    with pytest.raises(GitHubError):
        open_pr(conn, repo, tmp_path)
    task = task_of(conn)
    assert task.state is TaskState.READY
    assert task.pushed_sha == reopened
    assert remote_tip(remote) == reopened
    assert len(hub.added) == 1
    assert hub.comments == []

    hub.add_error = None
    runs: list[object] = []
    leases: list[tuple[object, ...]] = []
    original_update = prflow.gitops.update_task_branch

    def run_tests(*args: object, **kwargs: object) -> str | None:
        runs.append(args)
        return None

    def update(*args: object, **kwargs: object) -> str:
        leases.append(args)
        return original_update(*args, **kwargs)

    monkeypatch.setattr(prflow.gate, "run_tests", run_tests)
    monkeypatch.setattr(prflow.gitops, "update_task_branch", update)
    report = open_pr(conn, repo, tmp_path)
    assert report.state is TaskState.PR_OPEN
    assert runs == []
    assert gates_of(conn)[Gate.G2_PRE_PR].detail == "skipped"
    assert [args[2] for args in leases] == [reopened]
    assert remote_tip(remote) == reopened
    assert task_of(conn).pushed_sha == reopened
    assert len(hub.added) == 2
    assert hub.added[1] == {"url": URL, "name": "review-overridden", "token": TOKEN}
    assert len(hub.comments) == 1
    assert "## Review BLOCK overridden" in str(hub.comments[0]["body"])
    assert len(hub.created) == 1


def test_a_passing_new_attempt_keeps_the_label_and_names_the_attempt(
    conn: sqlite3.Connection,
    repo: Path,
    reopened: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    store.update_task(conn, TASK, attempt=2)
    write_review(tmp_path / "reviews" / "feat-x.json", make_review())
    hub.label_present = False
    assert open_pr(conn, repo, tmp_path).state is TaskState.PR_OPEN
    assert hub.label_calls == []
    assert hub.added == []
    body = str(hub.comments[0]["body"])
    assert body.startswith("Updated after attempt 2.\n\n")
    assert "overridden" not in body
    assert "Verdict: PASS" in body


def test_github_has_no_way_to_remove_a_label() -> None:
    assert not [name for name in dir(github) if "remove" in name.lower()]


def test_a_resolution_replaces_the_first_line_of_a_new_attempt_comment(
    conn: sqlite3.Connection,
    repo: Path,
    reopened: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    open_pr(conn, repo, tmp_path, resolution="Retried after a flaky test.")
    body = str(hub.comments[0]["body"])
    assert body.startswith("Retried after a flaky test.\n\n## Summary")
    assert "Updated after attempt" not in body
