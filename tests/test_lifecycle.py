import sqlite3
import subprocess
from pathlib import Path

import pytest

from orch import gate, github, gitops, results, sandbox, scheduler, store
from orch.config import Limits, Target, TargetPaths, ensure_target
from orch.github import PullRequestStatus
from orch.gitops import create_worktree, fetch
from orch.lifecycle import (
    LifecycleError,
    discard_task,
    prepare_task,
    resume_task,
    retry_task,
    settle_run,
)
from orch.models import (
    Effort,
    FailureReason,
    Finding,
    Gate,
    Grade,
    Lane,
    Outcome,
    Review,
    Reviewer,
    Run,
    Severity,
    Task,
    TaskState,
    Verdict,
)
from orch.scheduler import Decision
from orch.store import add_locks, connect, get_task, insert_task, list_locks
from orch.taskfile import parse_task

TASK = "feat-x"
BRANCH = "task/feat-x"
MAIN = "origin/main"
TARGET = "sample"
URL = "https://github.com/owner/sample/pull/7"
LIMITS = Limits(total=3, lane_b=2)
TTL = 10
TOKEN = "gh" + "s_" + "t0ken" * 8
SECTION = "# Previous failure (attempt 1)"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def write(path: Path, text: str = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def commit_all(cwd: Path, message: str) -> str:
    git(cwd, "add", ".")
    git(cwd, "commit", "-qm", message)
    return git(cwd, "rev-parse", "HEAD")


def spec_text(
    task_id: str = TASK,
    *,
    target: str = TARGET,
    depends_on: tuple[str, ...] = (),
    modify: str = "src/**",
) -> str:
    dependencies = ", ".join(f'"{item}"' for item in depends_on)
    return (
        "+++\n"
        f'id = "{task_id}"\n'
        'title = "Add feature X"\n'
        f'target = "{target}"\n'
        'grade = "L2"\n'
        'lane = "B"\n'
        'model = "gpt-6.1-sol"\n'
        'effort = "medium"\n'
        "timeout_min = 45\n"
        f'modify = ["{modify}"]\n'
        'read = ["src/**"]\n'
        'verify = "true"\n'
        f"depends_on = [{dependencies}]\n"
        "+++\n\n# Purpose\n\n본문\n"
    )


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


@pytest.fixture
def paths(tmp_path: Path) -> TargetPaths:
    return ensure_target(tmp_path / "runtime", TARGET, "owner/sample")


def make_target(repo: Path, **overrides: object) -> Target:
    values: dict[str, object] = {
        "name": TARGET,
        "path": repo,
        "github": "owner/sample",
        "default_branch": "main",
        "setup_command": "true",
        "test_command": "true",
        "lint_command": "true",
    }
    values.update(overrides)
    return Target(**values)


def add_task(
    conn: sqlite3.Connection,
    task_id: str = TASK,
    state: TaskState = TaskState.JUDGING,
    *,
    attempt: int = 1,
    target: str = TARGET,
    worktree: Path | None = None,
    test_sha: str | None = None,
    depends_on: tuple[str, ...] = (),
    pr_url: str | None = None,
) -> None:
    insert_task(
        conn,
        Task(
            id=task_id,
            target=target,
            title="Add feature X",
            grade=Grade.L2,
            lane=Lane.B,
            model="gpt-6.1-sol",
            effort=Effort.MEDIUM,
            state=state,
            attempt=attempt,
            timeout_sec=60,
            test_sha=test_sha,
            worktree=str(worktree) if worktree is not None else None,
            depends_on=depends_on,
            pr_url=pr_url,
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        ),
    )


def add_run(
    conn: sqlite3.Connection,
    outcome: Outcome,
    reason: FailureReason | None = None,
    *,
    task_id: str = TASK,
    attempt: int = 1,
) -> None:
    run_id = store.insert_run(
        conn,
        Run(
            task_id=task_id,
            attempt=attempt,
            lane=Lane.B,
            model="gpt-6.1-sol",
            started_at="2026-01-01T00:00:00+00:00",
        ),
    )
    store.finish_run(
        conn,
        run_id,
        ended_at="2026-01-01T00:01:00+00:00",
        exit_code=0,
        outcome=outcome,
        failure_reason=reason,
    )


def write_result(
    paths: TargetPaths,
    status: Outcome,
    *,
    reason: FailureReason | None = None,
    summary: str = "실패 요약 문장",
    unresolved: tuple[str, ...] = (),
) -> None:
    path = paths.results / f"{TASK}.json"
    results.write_result(
        path,
        results.parse_result(
            {
                "task_id": TASK,
                "status": status.value,
                "failure_reason": reason.value if reason is not None else None,
                "summary": summary,
                "changed_paths": [],
                "out_of_scope_changes": [],
                "verification": {"command": "true", "passed": False},
                "unresolved": list(unresolved),
                "log_path": "logs/feat-x.1.log",
            },
            TASK,
        ),
    )


def block_review() -> Review:
    return Review(
        task_id=TASK,
        reviewer=Reviewer.CLAUDE_REVIEWER,
        verdict=Verdict.BLOCK,
        findings=(
            Finding(severity=Severity.P0, path="src/app.py", summary="치명적 지적"),
            Finding(severity=Severity.P1, path="src/b.py", summary="경계 지적"),
            Finding(severity=Severity.P2, path="src/c.py", summary="사소한 지적"),
        ),
    )


def pass_review() -> Review:
    return Review(
        task_id=TASK,
        reviewer=Reviewer.CLAUDE_REVIEWER,
        verdict=Verdict.PASS,
        findings=(),
    )


def state_of(conn: sqlite3.Connection, task_id: str = TASK) -> TaskState:
    task = get_task(conn, task_id)
    assert task is not None
    return task.state


def task_of(conn: sqlite3.Connection, task_id: str = TASK) -> Task:
    task = get_task(conn, task_id)
    assert task is not None
    return task


def branch_exists(repo: Path, branch: str = BRANCH) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repo,
        capture_output=True,
    )
    return result.returncode == 0


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
def spec_file(paths: TargetPaths) -> Path:
    path = paths.tasks / f"{TASK}.md"
    write(path, spec_text())
    return path


# prepare


@pytest.fixture
def draft(tmp_path: Path) -> Path:
    path = tmp_path / "draft.md"
    write(path, spec_text())
    return path


def test_prepare_creates_the_worktree_and_registers_the_task(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths, draft: Path
) -> None:
    task = prepare_task(conn, draft, make_target(repo), paths)
    worktree = paths.worktrees / TASK
    assert worktree.is_dir()
    assert git(worktree, "rev-parse", "--abbrev-ref", "HEAD") == BRANCH
    assert task == task_of(conn)
    assert task.state is TaskState.PREPARED
    assert task.title == "Add feature X"
    assert task.target == TARGET
    assert task.grade is Grade.L2
    assert task.lane is Lane.B
    assert task.model == "gpt-6.1-sol"
    assert task.effort is Effort.MEDIUM
    assert task.attempt == 0
    assert task.timeout_sec == 45 * 60
    assert task.worktree == str(worktree)
    assert task.test_sha is None


def test_prepare_records_the_fetched_main_as_base(
    conn: sqlite3.Connection, repo: Path, remote: Path, paths: TargetPaths, draft: Path
) -> None:
    other = remote.parent / "other"
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
    write(other / "src" / "moved.py")
    moved = commit_all(other, "moved")
    git(other, "push", "-q", "origin", "main")
    task = prepare_task(conn, draft, make_target(repo), paths)
    assert task.base_sha == moved
    assert git(paths.worktrees / TASK, "rev-parse", "HEAD") == moved


def test_prepare_saves_the_spec_in_the_tasks_directory(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths, draft: Path
) -> None:
    prepare_task(conn, draft, make_target(repo), paths)
    saved = paths.tasks / f"{TASK}.md"
    assert saved.read_text(encoding="utf-8") == spec_text()


def test_prepare_accepts_a_spec_already_in_the_tasks_directory(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths, spec_file: Path
) -> None:
    prepare_task(conn, spec_file, make_target(repo), paths)
    assert spec_file.read_text(encoding="utf-8") == spec_text()
    assert state_of(conn) is TaskState.PREPARED


def test_prepare_runs_the_setup_command_in_the_worktree(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths, draft: Path
) -> None:
    target = make_target(repo, setup_command="touch .setup-ran")
    prepare_task(conn, draft, target, paths)
    assert (paths.worktrees / TASK / ".setup-ran").exists()
    assert not (repo / ".setup-ran").exists()


def test_prepare_runs_setup_outside_the_sandbox_with_the_default_limit(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    draft: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setup needs the network (uv sync) and is no gate: no sandbox, no [gate]."""
    original = gate.run_command
    seen: list[dict[str, object]] = []

    def spy(*args: object, **kwargs: object) -> int | None:
        seen.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(gate, "run_command", spy)
    prepare_task(conn, draft, make_target(repo), paths)
    assert len(seen) == 1
    assert seen[0].get("sandbox") is False
    assert seen[0].get("timeout_sec") is None


def test_prepare_does_not_need_sandbox_exec(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    draft: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox, "SANDBOX_EXEC", str(tmp_path / "missing" / "sx"))
    target = make_target(repo, setup_command="touch .setup-ran")
    prepare_task(conn, draft, target, paths)
    assert (paths.worktrees / TASK / ".setup-ran").exists()
    assert state_of(conn) is TaskState.PREPARED


def test_prepare_refuses_a_dependency_that_is_not_registered(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths, tmp_path: Path
) -> None:
    """A typo in depends_on must not leave a task waiting forever."""
    draft = tmp_path / "typo.md"
    write(draft, spec_text(depends_on=("feat-typo",)))
    with pytest.raises(LifecycleError):
        prepare_task(conn, draft, make_target(repo), paths)
    assert get_task(conn, TASK) is None
    assert not (paths.worktrees / TASK).exists()
    assert not branch_exists(repo)
    assert not (paths.tasks / f"{TASK}.md").exists()


def test_prepare_names_every_missing_dependency(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths, tmp_path: Path
) -> None:
    add_task(conn, "known", TaskState.PREPARED)
    draft = tmp_path / "typo.md"
    write(draft, spec_text(depends_on=("known", "miss-one", "miss-two")))
    with pytest.raises(LifecycleError, match="miss-one") as caught:
        prepare_task(conn, draft, make_target(repo), paths)
    assert "miss-two" in str(caught.value)
    assert "known" not in str(caught.value)


def test_prepare_refuses_a_dependency_on_another_target(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths, tmp_path: Path
) -> None:
    add_task(conn, "elsewhere", TaskState.PREPARED, target="other")
    draft = tmp_path / "other.md"
    write(draft, spec_text(depends_on=("elsewhere",)))
    with pytest.raises(LifecycleError):
        prepare_task(conn, draft, make_target(repo), paths)
    assert get_task(conn, TASK) is None


@pytest.mark.parametrize(
    "state", [TaskState.PREPARED, TaskState.CLOSED, TaskState.DISCARDED]
)
def test_prepare_accepts_a_registered_dependency_in_any_state(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    tmp_path: Path,
    state: TaskState,
) -> None:
    """A discarded dependency is registered: dispatch sends the task to blocked."""
    add_task(conn, "known", state)
    draft = tmp_path / "ok.md"
    write(draft, spec_text(depends_on=("known",)))
    task = prepare_task(conn, draft, make_target(repo), paths)
    assert task.depends_on == ("known",)


def test_prepare_refuses_a_spec_for_another_target(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths, tmp_path: Path
) -> None:
    draft = tmp_path / "other.md"
    write(draft, spec_text(target="other"))
    with pytest.raises(LifecycleError):
        prepare_task(conn, draft, make_target(repo), paths)
    assert not branch_exists(repo)


@pytest.mark.parametrize("modify", ["tests/**", "uv.lock"])
def test_prepare_refuses_protected_paths_in_modify(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    tmp_path: Path,
    modify: str,
) -> None:
    draft = tmp_path / "protected.md"
    write(draft, spec_text(modify=modify))
    target = make_target(repo, shared_paths=("uv.lock",))
    with pytest.raises(LifecycleError):
        prepare_task(conn, draft, target, paths)
    assert get_task(conn, TASK) is None
    assert not branch_exists(repo)


def test_prepare_refuses_an_invalid_or_missing_spec(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths, tmp_path: Path
) -> None:
    bad = tmp_path / "bad.md"
    write(bad, "not a spec\n")
    with pytest.raises(LifecycleError):
        prepare_task(conn, bad, make_target(repo), paths)
    with pytest.raises(LifecycleError):
        prepare_task(conn, tmp_path / "missing.md", make_target(repo), paths)


def test_prepare_refuses_an_id_already_registered(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths, draft: Path
) -> None:
    add_task(conn, TASK, TaskState.DISCARDED)
    with pytest.raises(LifecycleError):
        prepare_task(conn, draft, make_target(repo), paths)
    assert state_of(conn) is TaskState.DISCARDED
    assert not branch_exists(repo)


def test_prepare_keeps_a_branch_it_did_not_create(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths, draft: Path
) -> None:
    git(repo, "branch", BRANCH)
    tip = git(repo, "rev-parse", BRANCH)
    with pytest.raises(LifecycleError):
        prepare_task(conn, draft, make_target(repo), paths)
    assert git(repo, "rev-parse", BRANCH) == tip
    assert get_task(conn, TASK) is None


def assert_prepare_left_nothing(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths
) -> None:
    assert get_task(conn, TASK) is None
    assert not (paths.worktrees / TASK).exists()
    assert not branch_exists(repo)
    assert not (paths.tasks / f"{TASK}.md").exists()


def test_prepare_undoes_everything_when_setup_fails(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths, draft: Path
) -> None:
    target = make_target(repo, setup_command="exit 3")
    with pytest.raises(LifecycleError):
        prepare_task(conn, draft, target, paths)
    assert_prepare_left_nothing(conn, repo, paths)


def test_prepare_undoes_everything_when_setup_times_out(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    draft: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gate, "run_command", lambda *args, **kwargs: None)
    with pytest.raises(LifecycleError):
        prepare_task(conn, draft, make_target(repo), paths)
    assert_prepare_left_nothing(conn, repo, paths)


def test_prepare_undoes_everything_on_an_interrupt(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    draft: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupted(*args: object, **kwargs: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(gate, "run_command", interrupted)
    with pytest.raises(KeyboardInterrupt):
        prepare_task(conn, draft, make_target(repo), paths)
    assert_prepare_left_nothing(conn, repo, paths)


def test_prepare_undoes_everything_when_registration_fails(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    draft: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(store, "insert_task", failed)
    with pytest.raises(sqlite3.OperationalError):
        prepare_task(conn, draft, make_target(repo), paths)
    assert_prepare_left_nothing(conn, repo, paths)


def test_prepare_keeps_an_existing_spec_when_setup_fails(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths, spec_file: Path
) -> None:
    target = make_target(repo, setup_command="exit 3")
    with pytest.raises(LifecycleError):
        prepare_task(conn, spec_file, target, paths)
    assert spec_file.read_text(encoding="utf-8") == spec_text()
    assert not (paths.worktrees / TASK).exists()


def test_prepare_refuses_a_draft_when_the_tasks_directory_has_that_spec(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    draft: Path,
    spec_file: Path,
) -> None:
    spec_file.write_text("older spec\n", encoding="utf-8")
    with pytest.raises(LifecycleError):
        prepare_task(conn, draft, make_target(repo), paths)
    assert spec_file.read_text(encoding="utf-8") == "older spec\n"
    assert get_task(conn, TASK) is None
    assert not (paths.worktrees / TASK).exists()
    assert not branch_exists(repo)


def test_prepare_does_not_lose_an_existing_spec_when_setup_would_fail(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    draft: Path,
    spec_file: Path,
) -> None:
    spec_file.write_text("older spec\n", encoding="utf-8")
    target = make_target(repo, setup_command="exit 3")
    with pytest.raises(LifecycleError):
        prepare_task(conn, draft, target, paths)
    assert spec_file.read_text(encoding="utf-8") == "older spec\n"


def test_prepare_refuses_a_dangling_link_at_the_spec_path(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    draft: Path,
    tmp_path: Path,
) -> None:
    saved = paths.tasks / f"{TASK}.md"
    missing = tmp_path / "missing.md"
    saved.symlink_to(missing)
    with pytest.raises(LifecycleError):
        prepare_task(conn, draft, make_target(repo), paths)
    assert saved.is_symlink()
    assert not missing.exists()
    assert get_task(conn, TASK) is None


def test_prepare_keeps_a_spec_that_appears_during_the_call(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    draft: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The file was not this call's, so it is neither overwritten nor cleaned up."""
    saved = paths.tasks / f"{TASK}.md"
    create = gitops.create_worktree

    def create_then_appear(*args: object) -> None:
        create(*args)
        saved.write_text("another writer\n", encoding="utf-8")

    monkeypatch.setattr(gitops, "create_worktree", create_then_appear)
    with pytest.raises(LifecycleError):
        prepare_task(conn, draft, make_target(repo), paths)
    assert saved.read_text(encoding="utf-8") == "another writer\n"
    assert get_task(conn, TASK) is None
    assert not (paths.worktrees / TASK).exists()
    assert not branch_exists(repo)


def test_prepare_can_run_again_after_cleaning_up_its_own_spec(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths, draft: Path
) -> None:
    with pytest.raises(LifecycleError):
        prepare_task(conn, draft, make_target(repo, setup_command="exit 3"), paths)
    task = prepare_task(conn, draft, make_target(repo), paths)
    assert task.state is TaskState.PREPARED
    assert (paths.tasks / f"{TASK}.md").read_text(encoding="utf-8") == spec_text()


def test_prepare_cleanup_continues_after_an_interrupt_and_raises_it(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    draft: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remove = gitops.remove_worktree

    def remove_then_interrupt(*args: object, **kwargs: object) -> None:
        remove(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(gitops, "remove_worktree", remove_then_interrupt)
    target = make_target(repo, setup_command="exit 3")
    with pytest.raises(KeyboardInterrupt):
        prepare_task(conn, draft, target, paths)
    assert_prepare_left_nothing(conn, repo, paths)


def test_prepare_cleanup_failure_does_not_hide_the_original_error(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    draft: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed(*args: object, **kwargs: object) -> None:
        raise gitops.GitError("cannot remove")

    monkeypatch.setattr(gitops, "remove_worktree", failed)
    target = make_target(repo, setup_command="exit 3")
    with pytest.raises(LifecycleError, match="Setup failed"):
        prepare_task(conn, draft, target, paths)
    assert not (paths.tasks / f"{TASK}.md").exists()


# settle_run


@pytest.mark.parametrize("outcome", [Outcome.BLOCKED, Outcome.BLOCKED_QUOTA])
def test_settle_moves_a_blocked_run_to_blocked(
    conn: sqlite3.Connection, outcome: Outcome
) -> None:
    add_task(conn)
    add_run(conn, outcome)
    assert settle_run(conn, TASK) is TaskState.BLOCKED
    assert state_of(conn) is TaskState.BLOCKED


@pytest.mark.parametrize("outcome", [Outcome.DONE, Outcome.PARTIAL, Outcome.FAILED])
def test_settle_leaves_other_outcomes_judging(
    conn: sqlite3.Connection, outcome: Outcome
) -> None:
    add_task(conn)
    add_run(conn, outcome)
    assert settle_run(conn, TASK) is TaskState.JUDGING
    assert state_of(conn) is TaskState.JUDGING


def test_settle_leaves_a_task_in_another_state_alone(conn: sqlite3.Connection) -> None:
    add_task(conn, state=TaskState.READY)
    add_run(conn, Outcome.BLOCKED)
    assert settle_run(conn, TASK) is TaskState.READY


def test_settle_keeps_locks(conn: sqlite3.Connection) -> None:
    add_task(conn)
    add_locks(conn, TARGET, TASK, ("src/**",))
    add_run(conn, Outcome.BLOCKED_QUOTA)
    settle_run(conn, TASK)
    assert [lock.task_id for lock in list_locks(conn, TARGET)] == [TASK]


def test_settle_needs_a_task(conn: sqlite3.Connection) -> None:
    with pytest.raises(LifecycleError):
        settle_run(conn, "missing")


# retry


def retry(
    conn: sqlite3.Connection, paths: TargetPaths, limits: Limits = LIMITS
) -> Decision:
    return retry_task(conn, TASK, paths=paths, limits=limits, ttl_min=TTL)


@pytest.fixture
def failed_task(
    conn: sqlite3.Connection,
    paths: TargetPaths,
    worktree: Path,
    test_sha: str,
    spec_file: Path,
) -> None:
    add_task(conn, worktree=worktree, test_sha=test_sha)
    add_run(conn, Outcome.FAILED, FailureReason.REPORT_MISSING)
    write_result(paths, Outcome.FAILED, reason=FailureReason.REPORT_MISSING)


@pytest.mark.usefixtures("failed_task")
def test_retry_reserves_the_second_attempt(
    conn: sqlite3.Connection, paths: TargetPaths
) -> None:
    decision = retry(conn, paths)
    assert decision == Decision(state=TaskState.RESERVED)
    task = task_of(conn)
    assert task.state is TaskState.RESERVED
    assert task.attempt == 2
    assert task.slot_expires_at is not None


@pytest.mark.usefixtures("failed_task")
def test_retry_returns_the_worktree_to_the_test_commit(
    conn: sqlite3.Connection, paths: TargetPaths, worktree: Path, test_sha: str
) -> None:
    write(worktree / "src" / "app.py", "changed\n")
    write(worktree / "src" / "new.py")
    write(worktree / ".orch-result.json", "{}")
    retry(conn, paths)
    assert git(worktree, "rev-parse", "HEAD") == test_sha
    assert (worktree / "src" / "app.py").read_text(encoding="utf-8") == "base\n"
    assert not (worktree / "src" / "new.py").exists()
    assert not (worktree / ".orch-result.json").exists()


@pytest.mark.usefixtures("failed_task")
def test_retry_drops_a_commit_after_the_test_commit(
    conn: sqlite3.Connection, paths: TargetPaths, worktree: Path, test_sha: str
) -> None:
    write(worktree / "src" / "app.py", "committed\n")
    commit_all(worktree, "implementation")
    retry(conn, paths)
    assert git(worktree, "rev-parse", "HEAD") == test_sha


@pytest.mark.usefixtures("failed_task")
def test_retry_appends_the_failure_to_the_spec(
    conn: sqlite3.Connection, paths: TargetPaths, spec_file: Path
) -> None:
    retry(conn, paths)
    text = spec_file.read_text(encoding="utf-8")
    assert text.startswith(spec_text())
    assert SECTION in text
    assert "FAILED" in text
    assert "report_missing" in text
    assert "실패 요약 문장" in text
    assert parse_task(text).modify == parse_task(spec_text()).modify


def test_retry_after_partial_includes_unresolved(
    conn: sqlite3.Connection,
    paths: TargetPaths,
    worktree: Path,
    test_sha: str,
    spec_file: Path,
) -> None:
    add_task(conn, worktree=worktree, test_sha=test_sha)
    add_run(conn, Outcome.PARTIAL)
    write_result(paths, Outcome.PARTIAL, unresolved=("남은 항목 하나",))
    assert retry(conn, paths).state is TaskState.RESERVED
    assert "남은 항목 하나" in spec_file.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", [Gate.G1, Gate.G2, Gate.G3, Gate.G2_PRE_PR])
def test_retry_after_a_failed_gate(
    conn: sqlite3.Connection,
    paths: TargetPaths,
    worktree: Path,
    test_sha: str,
    spec_file: Path,
    name: Gate,
) -> None:
    add_task(conn, worktree=worktree, test_sha=test_sha)
    add_run(conn, Outcome.DONE)
    store.record_gate_result(conn, TASK, 1, name, passed=False, detail="게이트 상세")
    assert retry(conn, paths).state is TaskState.RESERVED
    text = spec_file.read_text(encoding="utf-8")
    assert name.value in text
    assert "게이트 상세" in text


def test_retry_after_a_block_review_lists_only_p0_and_p1(
    conn: sqlite3.Connection,
    paths: TargetPaths,
    worktree: Path,
    test_sha: str,
    spec_file: Path,
) -> None:
    add_task(conn, worktree=worktree, test_sha=test_sha)
    add_run(conn, Outcome.DONE)
    results.write_review(paths.review_path(TASK), block_review())
    assert retry(conn, paths).state is TaskState.RESERVED
    text = spec_file.read_text(encoding="utf-8")
    assert "치명적 지적" in text
    assert "경계 지적" in text
    assert "src/app.py" in text
    assert "사소한 지적" not in text


def test_retry_archives_the_review_and_triage(
    conn: sqlite3.Connection,
    paths: TargetPaths,
    worktree: Path,
    test_sha: str,
    spec_file: Path,
) -> None:
    """The second attempt must not reuse the first attempt's verdict."""
    add_task(conn, worktree=worktree, test_sha=test_sha)
    add_run(conn, Outcome.DONE)
    results.write_review(paths.review_path(TASK), block_review())
    write(paths.triage_path(TASK), "triage\n")
    retry(conn, paths)
    assert not paths.review_path(TASK).exists()
    assert not paths.triage_path(TASK).exists()
    assert results.load_review(paths.review_path(TASK, 1), TASK) == block_review()
    assert paths.triage_path(TASK, 1).read_text(encoding="utf-8") == "triage\n"


def test_retry_after_a_failed_pre_pr_gate_archives_a_passing_review(
    conn: sqlite3.Connection,
    paths: TargetPaths,
    worktree: Path,
    test_sha: str,
    spec_file: Path,
) -> None:
    add_task(conn, worktree=worktree, test_sha=test_sha)
    add_run(conn, Outcome.DONE)
    store.record_gate_result(conn, TASK, 1, Gate.G2_PRE_PR, passed=False, detail="d")
    results.write_review(paths.review_path(TASK), pass_review())
    retry(conn, paths)
    assert not paths.review_path(TASK).exists()
    assert paths.review_path(TASK, 1).exists()


def test_retry_refuses_a_done_task_without_a_failure(
    conn: sqlite3.Connection,
    paths: TargetPaths,
    worktree: Path,
    test_sha: str,
    spec_file: Path,
) -> None:
    add_task(conn, worktree=worktree, test_sha=test_sha)
    add_run(conn, Outcome.DONE)
    store.record_gate_result(conn, TASK, 1, Gate.G1, passed=True)
    results.write_review(paths.review_path(TASK), pass_review())
    write(worktree / "src" / "app.py", "kept\n")
    with pytest.raises(LifecycleError):
        retry(conn, paths)
    task = task_of(conn)
    assert task.state is TaskState.JUDGING
    assert task.attempt == 1
    assert (worktree / "src" / "app.py").read_text(encoding="utf-8") == "kept\n"
    assert spec_file.read_text(encoding="utf-8") == spec_text()
    assert paths.review_path(TASK).exists()


def test_retry_ignores_a_gate_failure_of_another_attempt(
    conn: sqlite3.Connection,
    paths: TargetPaths,
    worktree: Path,
    test_sha: str,
    spec_file: Path,
) -> None:
    add_task(conn, worktree=worktree, test_sha=test_sha)
    add_run(conn, Outcome.DONE)
    store.record_gate_result(conn, TASK, 2, Gate.G2, passed=False, detail="d")
    with pytest.raises(LifecycleError):
        retry(conn, paths)


@pytest.mark.parametrize("attempt", [0, 2])
def test_retry_is_allowed_only_on_the_first_attempt(
    conn: sqlite3.Connection,
    paths: TargetPaths,
    worktree: Path,
    test_sha: str,
    spec_file: Path,
    attempt: int,
) -> None:
    add_task(conn, worktree=worktree, test_sha=test_sha, attempt=attempt)
    add_run(conn, Outcome.FAILED, FailureReason.REPORT_MISSING, attempt=attempt)
    with pytest.raises(LifecycleError):
        retry(conn, paths)
    assert task_of(conn).attempt == attempt
    assert state_of(conn) is TaskState.JUDGING


@pytest.mark.parametrize(
    "state", [TaskState.RUNNING, TaskState.READY, TaskState.BLOCKED, TaskState.PR_OPEN]
)
def test_retry_needs_a_judging_task(
    conn: sqlite3.Connection,
    paths: TargetPaths,
    worktree: Path,
    test_sha: str,
    spec_file: Path,
    state: TaskState,
) -> None:
    add_task(conn, state=state, worktree=worktree, test_sha=test_sha)
    add_run(conn, Outcome.FAILED, FailureReason.REPORT_MISSING)
    with pytest.raises(LifecycleError):
        retry(conn, paths)
    assert state_of(conn) is state


@pytest.mark.parametrize("outcome", [Outcome.BLOCKED, Outcome.BLOCKED_QUOTA])
def test_retry_refuses_a_blocked_run(
    conn: sqlite3.Connection,
    paths: TargetPaths,
    worktree: Path,
    test_sha: str,
    spec_file: Path,
    outcome: Outcome,
) -> None:
    """A blocked run is settled to blocked and goes through resume."""
    add_task(conn, worktree=worktree, test_sha=test_sha)
    add_run(conn, outcome)
    with pytest.raises(LifecycleError):
        retry(conn, paths)
    assert task_of(conn).attempt == 1


def test_retry_needs_a_task_a_run_a_worktree_and_a_test_commit(
    conn: sqlite3.Connection, paths: TargetPaths, worktree: Path, test_sha: str
) -> None:
    with pytest.raises(LifecycleError):
        retry(conn, paths)
    add_task(conn, worktree=worktree, test_sha=test_sha)
    with pytest.raises(LifecycleError):
        retry(conn, paths)
    add_task(conn, "no-tree", test_sha=test_sha)
    add_run(conn, Outcome.FAILED, task_id="no-tree")
    with pytest.raises(LifecycleError):
        retry_task(conn, "no-tree", paths=paths, limits=LIMITS, ttl_min=TTL)


@pytest.mark.usefixtures("failed_task")
def test_retry_queues_without_a_slot_and_keeps_its_locks(
    conn: sqlite3.Connection, paths: TargetPaths
) -> None:
    add_locks(conn, TARGET, TASK, ("src/**",))
    add_task(conn, "busy", TaskState.RUNNING)
    decision = retry(conn, paths, Limits(total=1, lane_b=1))
    assert decision.state is TaskState.QUEUED
    task = task_of(conn)
    assert task.state is TaskState.QUEUED
    assert task.attempt == 2
    assert [lock.task_id for lock in list_locks(conn, TARGET)] == [TASK]


@pytest.mark.usefixtures("failed_task")
def test_retry_works_without_a_readable_result(
    conn: sqlite3.Connection, paths: TargetPaths, spec_file: Path
) -> None:
    (paths.results / f"{TASK}.json").unlink()
    assert retry(conn, paths).state is TaskState.RESERVED
    assert SECTION in spec_file.read_text(encoding="utf-8")


def test_retry_can_run_again_after_the_reservation_fails(
    conn: sqlite3.Connection,
    paths: TargetPaths,
    worktree: Path,
    test_sha: str,
    spec_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The review is already archived then, and the spec must not repeat itself."""
    add_task(conn, worktree=worktree, test_sha=test_sha)
    add_run(conn, Outcome.DONE)
    results.write_review(paths.review_path(TASK), block_review())

    def failed(*args: object, **kwargs: object) -> Decision:
        raise sqlite3.OperationalError("database is locked")

    with monkeypatch.context() as patch:
        patch.setattr(scheduler, "try_reserve", failed)
        with pytest.raises(sqlite3.OperationalError):
            retry(conn, paths)
    task = task_of(conn)
    assert task.state is TaskState.JUDGING
    assert task.attempt == 1

    assert retry(conn, paths).state is TaskState.RESERVED
    text = spec_file.read_text(encoding="utf-8")
    assert text.count(SECTION) == 1
    assert "치명적 지적" in text


def test_retry_refuses_a_last_run_from_another_attempt(
    conn: sqlite3.Connection,
    paths: TargetPaths,
    worktree: Path,
    test_sha: str,
    spec_file: Path,
) -> None:
    add_task(conn, worktree=worktree, test_sha=test_sha, attempt=1)
    add_run(conn, Outcome.FAILED, FailureReason.REPORT_MISSING, attempt=2)
    write(worktree / "src" / "dirty.py")
    with pytest.raises(LifecycleError):
        retry(conn, paths)
    task = task_of(conn)
    assert task.state is TaskState.JUDGING
    assert task.attempt == 1
    assert (worktree / "src" / "dirty.py").exists()
    assert SECTION not in spec_file.read_text(encoding="utf-8")


# resume


def resume(
    conn: sqlite3.Connection,
    paths: TargetPaths,
    *,
    keep: bool = False,
    limits: Limits = LIMITS,
) -> Decision:
    return resume_task(conn, TASK, paths=paths, limits=limits, ttl_min=TTL, keep=keep)


STOPPED = [TaskState.BLOCKED, TaskState.ORPHANED]


@pytest.mark.parametrize("state", STOPPED)
def test_resume_resets_by_default(
    conn: sqlite3.Connection,
    paths: TargetPaths,
    worktree: Path,
    test_sha: str,
    spec_file: Path,
    state: TaskState,
) -> None:
    add_task(conn, state=state, worktree=worktree, test_sha=test_sha)
    write(worktree / "src" / "app.py", "partial\n")
    write(worktree / "src" / "new.py")
    write(worktree / ".orch-result.json", "{}")
    assert resume(conn, paths) == Decision(state=TaskState.RESERVED)
    assert git(worktree, "rev-parse", "HEAD") == test_sha
    assert (worktree / "src" / "app.py").read_text(encoding="utf-8") == "base\n"
    assert not (worktree / "src" / "new.py").exists()
    assert not (worktree / ".orch-result.json").exists()


@pytest.mark.parametrize("state", STOPPED)
def test_resume_with_keep_leaves_the_worktree_alone(
    conn: sqlite3.Connection,
    paths: TargetPaths,
    worktree: Path,
    test_sha: str,
    spec_file: Path,
    state: TaskState,
) -> None:
    add_task(conn, state=state, worktree=worktree, test_sha=test_sha)
    write(worktree / "src" / "app.py", "partial\n")
    assert resume(conn, paths, keep=True).state is TaskState.RESERVED
    assert (worktree / "src" / "app.py").read_text(encoding="utf-8") == "partial\n"


def test_keep_must_be_named(
    conn: sqlite3.Connection, paths: TargetPaths, spec_file: Path
) -> None:
    with pytest.raises(TypeError):
        resume_task(conn, TASK, True, paths=paths, limits=LIMITS, ttl_min=TTL)  # type: ignore[misc]


@pytest.mark.parametrize("keep", [False, True])
@pytest.mark.parametrize("attempt", [1, 2])
def test_resume_does_not_use_an_attempt(
    conn: sqlite3.Connection,
    paths: TargetPaths,
    worktree: Path,
    test_sha: str,
    spec_file: Path,
    keep: bool,
    attempt: int,
) -> None:
    add_task(
        conn,
        state=TaskState.BLOCKED,
        worktree=worktree,
        test_sha=test_sha,
        attempt=attempt,
    )
    resume(conn, paths, keep=keep)
    assert task_of(conn).attempt == attempt


def test_resume_adds_nothing_to_the_spec(
    conn: sqlite3.Connection,
    paths: TargetPaths,
    worktree: Path,
    test_sha: str,
    spec_file: Path,
) -> None:
    add_task(conn, state=TaskState.BLOCKED, worktree=worktree, test_sha=test_sha)
    resume(conn, paths)
    assert spec_file.read_text(encoding="utf-8") == spec_text()


def test_resume_queues_without_a_slot(
    conn: sqlite3.Connection,
    paths: TargetPaths,
    worktree: Path,
    test_sha: str,
    spec_file: Path,
) -> None:
    add_task(conn, state=TaskState.BLOCKED, worktree=worktree, test_sha=test_sha)
    add_locks(conn, TARGET, TASK, ("src/**",))
    add_task(conn, "busy", TaskState.RUNNING)
    decision = resume(conn, paths, limits=Limits(total=1, lane_b=1))
    assert decision.state is TaskState.QUEUED
    assert [lock.task_id for lock in list_locks(conn, TARGET)] == [TASK]


def test_resume_with_a_discarded_dependency_blocks_again_and_resets_nothing(
    conn: sqlite3.Connection,
    paths: TargetPaths,
    worktree: Path,
    test_sha: str,
    spec_file: Path,
) -> None:
    """Work is not lost to a reset that could not lead to a run."""
    add_task(conn, "dep", TaskState.DISCARDED)
    add_task(
        conn,
        state=TaskState.BLOCKED,
        worktree=worktree,
        test_sha=test_sha,
        depends_on=("dep",),
    )
    write(worktree / "src" / "app.py", "partial\n")
    decision = resume(conn, paths)
    assert decision == Decision(state=TaskState.BLOCKED, waiting_for=("dep:dep",))
    assert state_of(conn) is TaskState.BLOCKED
    assert (worktree / "src" / "app.py").read_text(encoding="utf-8") == "partial\n"


@pytest.mark.parametrize(
    "state",
    [TaskState.PREPARED, TaskState.QUEUED, TaskState.JUDGING, TaskState.PR_OPEN],
)
def test_resume_needs_a_stopped_task(
    conn: sqlite3.Connection,
    paths: TargetPaths,
    worktree: Path,
    test_sha: str,
    spec_file: Path,
    state: TaskState,
) -> None:
    add_task(conn, state=state, worktree=worktree, test_sha=test_sha)
    write(worktree / "src" / "app.py", "kept\n")
    with pytest.raises(LifecycleError):
        resume(conn, paths)
    assert state_of(conn) is state
    assert (worktree / "src" / "app.py").read_text(encoding="utf-8") == "kept\n"


@pytest.mark.parametrize("keep", [False, True])
def test_resume_can_run_again_after_the_reservation_fails(
    conn: sqlite3.Connection,
    paths: TargetPaths,
    worktree: Path,
    test_sha: str,
    spec_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    keep: bool,
) -> None:
    add_task(
        conn, state=TaskState.BLOCKED, worktree=worktree, test_sha=test_sha, attempt=1
    )
    before = spec_file.read_text(encoding="utf-8")

    def failed(*args: object, **kwargs: object) -> Decision:
        raise sqlite3.OperationalError("database is locked")

    with monkeypatch.context() as patch:
        patch.setattr(scheduler, "try_reserve", failed)
        with pytest.raises(sqlite3.OperationalError):
            resume(conn, paths, keep=keep)
    task = task_of(conn)
    assert task.state is TaskState.BLOCKED
    assert task.attempt == 1
    assert list_locks(conn, TARGET) == ()
    assert spec_file.read_text(encoding="utf-8") == before

    assert resume(conn, paths, keep=keep).state is TaskState.RESERVED
    assert task_of(conn).attempt == 1


def test_resume_needs_a_task(conn: sqlite3.Connection, paths: TargetPaths) -> None:
    with pytest.raises(LifecycleError):
        resume(conn, paths)


@pytest.mark.parametrize("keep", [False, True])
def test_resume_needs_a_worktree_and_a_test_commit(
    conn: sqlite3.Connection, paths: TargetPaths, spec_file: Path, keep: bool
) -> None:
    add_task(conn, state=TaskState.BLOCKED)
    with pytest.raises(LifecycleError):
        resume(conn, paths, keep=keep)
    assert state_of(conn) is TaskState.BLOCKED


# discard


class FakeStatus:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.state = "CLOSED"
        self.tokens: list[str] = []
        monkeypatch.setattr(github, "pull_request_status", self.status)

    def status(self, *, url: str, token: str) -> PullRequestStatus:
        self.tokens.append(token)
        return PullRequestStatus(
            state=self.state,
            merged=self.state == "MERGED",
            merged_at="2026-01-02T00:00:00Z" if self.state == "MERGED" else None,
        )


@pytest.fixture
def status(monkeypatch: pytest.MonkeyPatch) -> FakeStatus:
    return FakeStatus(monkeypatch)


@pytest.mark.parametrize(
    "state",
    [
        TaskState.PREPARED,
        TaskState.QUEUED,
        TaskState.RESERVED,
        TaskState.JUDGING,
        TaskState.CONFLICT,
        TaskState.BLOCKED,
        TaskState.ORPHANED,
    ],
)
def test_discard_removes_the_worktree_branch_and_locks(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    state: TaskState,
    status: FakeStatus,
) -> None:
    add_task(conn, state=state, worktree=worktree)
    add_locks(conn, TARGET, TASK, ("src/**",))
    write(worktree / "src" / "dirty.py")
    discard_task(conn, TASK, make_target(repo))
    assert state_of(conn) is TaskState.DISCARDED
    assert not worktree.exists()
    assert not branch_exists(repo)
    assert list_locks(conn, TARGET) == ()
    assert status.tokens == []


@pytest.mark.parametrize(
    "state",
    [
        TaskState.RUNNING,
        TaskState.GATING,
        TaskState.REVIEWING,
        TaskState.READY,
        TaskState.CLOSED,
        TaskState.DISCARDED,
    ],
)
def test_discard_refuses_a_state_the_table_does_not_allow(
    conn: sqlite3.Connection, repo: Path, worktree: Path, state: TaskState
) -> None:
    add_task(conn, state=state, worktree=worktree)
    add_locks(conn, TARGET, TASK, ("src/**",))
    with pytest.raises(LifecycleError):
        discard_task(conn, TASK, make_target(repo))
    assert state_of(conn) is state
    assert worktree.exists()
    assert branch_exists(repo)


def test_discard_leaves_the_remote_branch(
    conn: sqlite3.Connection, repo: Path, remote: Path, worktree: Path
) -> None:
    git(worktree, "push", "-q", "origin", BRANCH)
    tip = git(remote, "rev-parse", f"refs/heads/{BRANCH}")
    add_task(conn, state=TaskState.JUDGING, worktree=worktree)
    discard_task(conn, TASK, make_target(repo))
    assert git(remote, "rev-parse", f"refs/heads/{BRANCH}") == tip


def test_discard_tolerates_a_missing_worktree_and_branch(
    conn: sqlite3.Connection, repo: Path, tmp_path: Path
) -> None:
    add_task(conn, state=TaskState.BLOCKED, worktree=tmp_path / "gone")
    discard_task(conn, TASK, make_target(repo))
    assert state_of(conn) is TaskState.DISCARDED


def test_discard_can_run_again_after_the_branch_delete_fails(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_task(conn, state=TaskState.BLOCKED, worktree=worktree)
    add_locks(conn, TARGET, TASK, ("src/**",))

    def failed(*args: object) -> None:
        raise gitops.GitError("cannot delete")

    with monkeypatch.context() as patch:
        patch.setattr(gitops, "delete_branch", failed)
        with pytest.raises(LifecycleError):
            discard_task(conn, TASK, make_target(repo))
    assert state_of(conn) is TaskState.BLOCKED
    assert not worktree.exists()
    assert branch_exists(repo)
    assert len(list_locks(conn, TARGET)) == 1

    discard_task(conn, TASK, make_target(repo))
    assert state_of(conn) is TaskState.DISCARDED
    assert not branch_exists(repo)
    assert list_locks(conn, TARGET) == ()


def test_discard_without_a_recorded_worktree(
    conn: sqlite3.Connection, repo: Path
) -> None:
    add_task(conn, state=TaskState.PREPARED)
    discard_task(conn, TASK, make_target(repo))
    assert state_of(conn) is TaskState.DISCARDED


def test_discard_needs_a_task(conn: sqlite3.Connection, repo: Path) -> None:
    with pytest.raises(LifecycleError):
        discard_task(conn, "missing", make_target(repo))


@pytest.mark.parametrize("state", [TaskState.PR_OPEN, TaskState.CONFLICT])
def test_discard_with_a_closed_pull_request(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    status: FakeStatus,
    state: TaskState,
) -> None:
    add_task(conn, state=state, worktree=worktree, pr_url=URL)
    discard_task(conn, TASK, make_target(repo), token=TOKEN)
    assert status.tokens == [TOKEN]
    assert state_of(conn) is TaskState.DISCARDED
    assert not worktree.exists()


@pytest.mark.parametrize("answer", ["OPEN", "MERGED"])
@pytest.mark.parametrize("state", [TaskState.PR_OPEN, TaskState.CONFLICT])
def test_discard_refuses_an_open_or_merged_pull_request(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    status: FakeStatus,
    answer: str,
    state: TaskState,
) -> None:
    status.state = answer
    add_task(conn, state=state, worktree=worktree, pr_url=URL)
    with pytest.raises(LifecycleError):
        discard_task(conn, TASK, make_target(repo), token=TOKEN)
    assert state_of(conn) is state
    assert worktree.exists()
    assert branch_exists(repo)


def test_discard_with_a_pull_request_needs_a_token(
    conn: sqlite3.Connection, repo: Path, worktree: Path, status: FakeStatus
) -> None:
    add_task(conn, state=TaskState.PR_OPEN, worktree=worktree, pr_url=URL)
    with pytest.raises(LifecycleError):
        discard_task(conn, TASK, make_target(repo))
    assert status.tokens == []
    assert state_of(conn) is TaskState.PR_OPEN


def test_discard_reports_a_failed_status_check_and_changes_nothing(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed(*, url: str, token: str) -> PullRequestStatus:
        raise github.GitHubError("pr view: failed")

    monkeypatch.setattr(github, "pull_request_status", failed)
    add_task(conn, state=TaskState.PR_OPEN, worktree=worktree, pr_url=URL)
    with pytest.raises(LifecycleError):
        discard_task(conn, TASK, make_target(repo), token=TOKEN)
    assert state_of(conn) is TaskState.PR_OPEN
    assert worktree.exists()


def test_a_discarded_task_blocks_its_dependent_at_next(
    conn: sqlite3.Connection, repo: Path, worktree: Path
) -> None:
    add_task(conn, state=TaskState.JUDGING, worktree=worktree)
    add_task(conn, "later", TaskState.QUEUED, depends_on=(TASK,))
    discard_task(conn, TASK, make_target(repo))
    report = scheduler.next_reservations(
        conn, modify_of=lambda task: ("src/**",), limits=LIMITS, ttl_min=TTL
    )
    assert report.newly_blocked == ("later",)
    assert state_of(conn, "later") is TaskState.BLOCKED
