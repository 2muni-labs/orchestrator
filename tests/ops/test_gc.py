import os
import sqlite3
import subprocess
from collections.abc import Iterator
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from orch import gc, gitops, store
from orch.config import Target, target_paths
from orch.gc import DEFAULT_RETENTION_DAYS, GcReport, collect
from orch.models import Grade, Lane, Task, TaskState

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
OLD = NOW - timedelta(days=31)
FRESH = NOW - timedelta(days=1)
NAME = "sample"


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


@pytest.fixture
def orch_home(tmp_path: Path) -> Path:
    return tmp_path / "home" / ".orch"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = store.connect(tmp_path / "state.db")
    yield connection
    connection.close()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "gitconfig"
    config.write_text(
        "[user]\n\tname = t\n\temail = t@example.com\n[init]\n\tdefaultBranch = main\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    path = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "base.txt").write_text("x\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-qm", "base")
    return path


def make_target(repo: Path, name: str = NAME) -> Target:
    return Target(
        name=name,
        path=repo,
        github="o/r",
        default_branch="main",
        setup_command="true",
        test_command="true",
        lint_command="true",
    )


def add_task(
    conn: sqlite3.Connection,
    task_id: str,
    state: TaskState = TaskState.CLOSED,
    *,
    updated: datetime | str = OLD,
    target: str = NAME,
) -> None:
    at = updated if isinstance(updated, str) else updated.isoformat()
    store.insert_task(
        conn,
        Task(
            id=task_id,
            target=target,
            title="T",
            grade=Grade.L2,
            lane=Lane.B,
            model="m",
            state=state,
            timeout_sec=60,
            created_at="2026-01-01T00:00:00+00:00",
            updated_at=at,
        ),
    )


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    return path


def task_files(orch_home: Path, task_id: str, target: str = NAME) -> list[Path]:
    """One file of every name the design removes for a task."""
    paths = target_paths(orch_home, target)
    return [
        paths.logs / f"{task_id}.1.log",
        paths.logs / f"{task_id}.12.log",
        paths.logs / f"{task_id}.1.rejected-result.json",
        paths.logs / f"{task_id}.gate.log",
        paths.logs / f"{task_id}.setup.log",
        paths.logs / f"{task_id}.pr.log",
        paths.logs / f"{task_id}.update.log",
        paths.logs / "review" / f"{task_id}.1.log",
        paths.results / f"{task_id}.json",
        paths.reviews / f"{task_id}.json",
        paths.reviews / f"{task_id}.diff",
        paths.reviews / f"{task_id}.triage.md",
        paths.reviews / "attempts" / f"{task_id}.1.json",
        paths.reviews / "attempts" / f"{task_id}.1.triage.md",
    ]


def relative(orch_home: Path, paths: list[Path]) -> tuple[str, ...]:
    return tuple(sorted(str(path.relative_to(orch_home)) for path in paths))


@pytest.fixture
def no_git(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace worktree git calls, for tests about files only."""
    calls: list[str] = []
    monkeypatch.setattr(gitops, "prune_worktrees", lambda repo: calls.append("prune"))

    def remove(repo: Path, worktree: Path, *, force: bool = False) -> None:
        calls.append(f"remove {worktree.name} force={force}")

    monkeypatch.setattr(gitops, "remove_worktree", remove)
    return calls


def run(
    conn: sqlite3.Connection,
    orch_home: Path,
    repo: Path = Path("/repo"),
    **kwargs: object,
) -> GcReport:
    return collect(
        conn,
        targets={NAME: make_target(repo)},
        orch_home=orch_home,
        now=NOW,
        **kwargs,  # type: ignore[arg-type]
    )


# Files


def test_the_default_retention_is_thirty_days() -> None:
    assert DEFAULT_RETENTION_DAYS == 30


@pytest.mark.parametrize("state", [TaskState.CLOSED, TaskState.DISCARDED])
def test_every_named_file_of_an_expired_task_is_removed(
    conn: sqlite3.Connection, orch_home: Path, no_git: list[str], state: TaskState
) -> None:
    add_task(conn, "a", state)
    files = [touch(path) for path in task_files(orch_home, "a")]
    report = run(conn, orch_home)
    assert report.removed_files == relative(orch_home, files)
    assert not any(path.exists() for path in files)
    assert report.errors == ()
    assert report.dry_run is False


def test_a_fresh_task_keeps_its_files(
    conn: sqlite3.Connection, orch_home: Path, no_git: list[str]
) -> None:
    add_task(conn, "a", updated=FRESH)
    files = [touch(path) for path in task_files(orch_home, "a")]
    assert run(conn, orch_home).removed_files == ()
    assert all(path.exists() for path in files)


def test_a_task_at_exactly_the_retention_is_removed(
    conn: sqlite3.Connection, orch_home: Path, no_git: list[str]
) -> None:
    add_task(conn, "a", updated=NOW - timedelta(days=30))
    log = touch(target_paths(orch_home, NAME).logs / "a.1.log")
    run(conn, orch_home)
    assert not log.exists()


def test_a_task_just_inside_the_retention_is_kept(
    conn: sqlite3.Connection, orch_home: Path, no_git: list[str]
) -> None:
    add_task(conn, "a", updated=NOW - timedelta(days=30) + timedelta(seconds=1))
    log = touch(target_paths(orch_home, NAME).logs / "a.1.log")
    run(conn, orch_home)
    assert log.exists()


def test_the_retention_can_be_changed(
    conn: sqlite3.Connection, orch_home: Path, no_git: list[str]
) -> None:
    add_task(conn, "a", updated=FRESH)
    log = touch(target_paths(orch_home, NAME).logs / "a.1.log")
    run(conn, orch_home, retention_days=1)
    assert not log.exists()


@pytest.mark.parametrize(
    "state",
    [
        state
        for state in TaskState
        if state not in (TaskState.CLOSED, TaskState.DISCARDED)
    ],
)
def test_an_unfinished_task_keeps_its_files_however_old(
    conn: sqlite3.Connection, orch_home: Path, no_git: list[str], state: TaskState
) -> None:
    add_task(conn, "a", state)
    files = [touch(path) for path in task_files(orch_home, "a")]
    assert run(conn, orch_home).removed_files == ()
    assert all(path.exists() for path in files)


@pytest.mark.parametrize(
    "name",
    [
        "a.log",
        "a.1.1.log",
        "a.x.log",
        "a.1.log.bak",
        "a-2.1.log",
        "ab.1.log",
        "xa.1.log",
        "a.gate.1.log",
        "a..1.log",
        "a.1.rejected-result.json.tmp",
        "a.json",
        "a.rejected-result.json",
        "subagents.log",
        "agents-summary.log",
    ],
)
def test_a_log_name_outside_the_rules_is_kept(
    conn: sqlite3.Connection, orch_home: Path, no_git: list[str], name: str
) -> None:
    add_task(conn, "a")
    path = touch(target_paths(orch_home, NAME).logs / name)
    assert run(conn, orch_home).removed_files == ()
    assert path.exists()


def test_a_dot_in_an_id_is_not_a_wildcard(
    conn: sqlite3.Connection, orch_home: Path, no_git: list[str]
) -> None:
    add_task(conn, "a.b")
    paths = target_paths(orch_home, NAME)
    other = touch(paths.logs / "axb.1.log")
    other_result = touch(paths.results / "axb.json")
    own = touch(paths.logs / "a.b.1.log")
    run(conn, orch_home)
    assert other.exists()
    assert other_result.exists()
    assert not own.exists()


def test_a_task_does_not_remove_the_files_of_a_longer_id(
    conn: sqlite3.Connection, orch_home: Path, no_git: list[str]
) -> None:
    add_task(conn, "a")
    add_task(conn, "a.1", TaskState.RUNNING)
    files = [touch(path) for path in task_files(orch_home, "a.1")]
    run(conn, orch_home)
    assert all(path.exists() for path in files)


def test_other_files_are_never_touched(
    conn: sqlite3.Connection, orch_home: Path, no_git: list[str]
) -> None:
    add_task(conn, "a")
    paths = target_paths(orch_home, NAME)
    kept = [
        touch(paths.tasks / "a.md"),
        touch(paths.logs / "subagents.log"),
        touch(orch_home / "hooks.log"),
        touch(orch_home / "notify.json"),
    ]
    run(conn, orch_home)
    assert all(path.exists() for path in kept)
    assert store.get_task(conn, "a") is not None


def test_a_directory_with_a_matching_name_is_left(
    conn: sqlite3.Connection, orch_home: Path, no_git: list[str]
) -> None:
    add_task(conn, "a")
    directory = target_paths(orch_home, NAME).logs / "a.1.log"
    directory.mkdir(parents=True)
    report = run(conn, orch_home)
    assert report.removed_files == ()
    assert report.errors == ()
    assert directory.is_dir()


def test_missing_runtime_directories_are_not_an_error(
    conn: sqlite3.Connection, orch_home: Path, no_git: list[str]
) -> None:
    add_task(conn, "a")
    assert run(conn, orch_home) == GcReport(dry_run=False)


@pytest.mark.parametrize(
    ("updated", "error"),
    [("yesterday", "ValueError"), ("2026-01-01T00:00:00", "TypeError")],
)
def test_an_unreadable_time_keeps_the_files_and_continues(
    conn: sqlite3.Connection,
    orch_home: Path,
    no_git: list[str],
    updated: str,
    error: str,
) -> None:
    add_task(conn, "a", updated=updated)
    add_task(conn, "b")
    paths = target_paths(orch_home, NAME)
    kept = touch(paths.logs / "a.1.log")
    gone = touch(paths.logs / "b.1.log")
    report = run(conn, orch_home)
    assert report.errors == (f"{NAME}/a {error}",)
    assert kept.exists()
    assert not gone.exists()


def test_a_failed_file_removal_is_reported_and_the_rest_continue(
    conn: sqlite3.Connection,
    orch_home: Path,
    no_git: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_task(conn, "a")
    paths = target_paths(orch_home, NAME)
    stuck = touch(paths.logs / "a.1.log")
    gone = touch(paths.results / "a.json")
    real = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self.name == stuck.name:
            raise PermissionError("private detail")
        real(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    real_remove = os.remove

    def remove(path: object, *args: object, **kwargs: object) -> None:
        if Path(str(path)).name == stuck.name:
            raise PermissionError("private detail")
        real_remove(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "remove", remove)
    monkeypatch.setattr(os, "unlink", remove)
    report = run(conn, orch_home)
    assert report.errors == (f"{NAME}/logs/a.1.log PermissionError",)
    assert report.removed_files == (f"{NAME}/results/a.json",)
    assert stuck.exists()
    assert not gone.exists()


def test_tasks_of_an_unlisted_target_are_left(
    conn: sqlite3.Connection, orch_home: Path, no_git: list[str]
) -> None:
    add_task(conn, "a", target="other")
    log = touch(target_paths(orch_home, "other").logs / "a.1.log")
    assert run(conn, orch_home).removed_files == ()
    assert log.exists()


def test_each_target_removes_only_its_own_tasks_files(
    conn: sqlite3.Connection, orch_home: Path, no_git: list[str]
) -> None:
    add_task(conn, "a", target="one")
    add_task(conn, "b", TaskState.RUNNING, target="two")
    one = touch(target_paths(orch_home, "one").logs / "a.1.log")
    # The same file name under a target whose task "a" does not exist.
    two = touch(target_paths(orch_home, "two").logs / "a.1.log")
    report = collect(
        conn,
        targets={
            "two": make_target(Path("/two"), "two"),
            "one": make_target(Path("/one"), "one"),
        },
        orch_home=orch_home,
        now=NOW,
    )
    assert report.removed_files == ("one/logs/a.1.log",)
    assert not one.exists()
    assert two.exists()


def test_the_report_holds_no_absolute_path(
    conn: sqlite3.Connection, orch_home: Path, repo: Path
) -> None:
    add_task(conn, "a")
    add_task(conn, "b")
    touch(target_paths(orch_home, NAME).logs / "a.1.log")
    paths = target_paths(orch_home, NAME)
    git(repo, "worktree", "add", "-q", "-b", "task/b", str(paths.worktrees / "b"))
    (paths.worktrees / "stray").mkdir()
    report = run(conn, orch_home, repo)
    text = str(asdict(report))
    assert (
        report.removed_files and report.removed_worktrees and report.unknown_worktrees
    )
    assert str(orch_home) not in text
    assert str(repo) not in text


# Worktrees


def worktree(repo: Path, orch_home: Path, task_id: str) -> Path:
    path = target_paths(orch_home, NAME).worktrees / task_id
    git(repo, "worktree", "add", "-q", "-b", f"task/{task_id}", str(path))
    return path


@pytest.mark.parametrize("state", [TaskState.CLOSED, TaskState.DISCARDED])
def test_a_finished_tasks_worktree_is_removed_regardless_of_age(
    conn: sqlite3.Connection, orch_home: Path, repo: Path, state: TaskState
) -> None:
    add_task(conn, "a", state, updated=FRESH)
    path = worktree(repo, orch_home, "a")
    report = run(conn, orch_home, repo)
    assert report.removed_worktrees == (f"{NAME}/a",)
    assert not path.exists()


def test_a_worktree_with_changes_is_skipped_and_reported(
    conn: sqlite3.Connection, orch_home: Path, repo: Path
) -> None:
    add_task(conn, "a")
    add_task(conn, "b")
    dirty = worktree(repo, orch_home, "a")
    clean = worktree(repo, orch_home, "b")
    (dirty / "base.txt").write_text("changed\n", encoding="utf-8")
    report = run(conn, orch_home, repo)
    assert report.skipped_worktrees == (f"{NAME}/a",)
    assert report.removed_worktrees == (f"{NAME}/b",)
    assert report.errors == ()
    assert (dirty / "base.txt").read_text(encoding="utf-8") == "changed\n"
    assert not clean.exists()


def test_removal_never_uses_force(
    conn: sqlite3.Connection, orch_home: Path, no_git: list[str]
) -> None:
    add_task(conn, "a")
    (target_paths(orch_home, NAME).worktrees / "a").mkdir(parents=True)
    run(conn, orch_home)
    assert no_git == ["prune", "remove a force=False"]


def test_an_unfinished_tasks_worktree_is_left_and_not_reported(
    conn: sqlite3.Connection, orch_home: Path, repo: Path
) -> None:
    add_task(conn, "a", TaskState.RUNNING)
    path = worktree(repo, orch_home, "a")
    report = run(conn, orch_home, repo)
    assert path.exists()
    assert report == GcReport(dry_run=False)


def test_an_unknown_directory_is_reported_and_kept(
    conn: sqlite3.Connection, orch_home: Path, repo: Path
) -> None:
    path = worktree(repo, orch_home, "h-harness")
    report = run(conn, orch_home, repo)
    assert report.unknown_worktrees == (f"{NAME}/h-harness",)
    assert path.exists()


def test_another_targets_task_id_counts_as_unknown(
    conn: sqlite3.Connection, orch_home: Path, repo: Path
) -> None:
    add_task(conn, "a", target="other")
    path = worktree(repo, orch_home, "a")
    report = run(conn, orch_home, repo)
    assert report.unknown_worktrees == (f"{NAME}/a",)
    assert report.removed_worktrees == ()
    assert path.exists()


def test_a_plain_file_in_worktrees_is_ignored(
    conn: sqlite3.Connection, orch_home: Path, no_git: list[str]
) -> None:
    touch(target_paths(orch_home, NAME).worktrees / ".DS_Store")
    assert run(conn, orch_home) == GcReport(dry_run=False)


def test_a_stale_worktree_record_is_pruned(
    conn: sqlite3.Connection, orch_home: Path, repo: Path
) -> None:
    add_task(conn, "a", TaskState.RUNNING)
    path = worktree(repo, orch_home, "a")
    subprocess.run(["rm", "-rf", str(path)], check=True)
    assert str(path) in git(repo, "worktree", "list")
    run(conn, orch_home, repo)
    assert str(path) not in git(repo, "worktree", "list")


def test_a_failed_prune_skips_worktrees_but_not_files(
    conn: sqlite3.Connection,
    orch_home: Path,
    no_git: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def prune(repo: Path) -> None:
        raise gitops.GitError("private detail")

    monkeypatch.setattr(gitops, "prune_worktrees", prune)
    add_task(conn, "a")
    paths = target_paths(orch_home, NAME)
    log = touch(paths.logs / "a.1.log")
    (paths.worktrees / "a").mkdir(parents=True)
    report = run(conn, orch_home)
    assert report.errors == (f"{NAME} GitError",)
    assert report.removed_files == (f"{NAME}/logs/a.1.log",)
    assert report.removed_worktrees == ()
    assert no_git == []
    assert not log.exists()


def test_one_targets_failure_does_not_stop_the_next(
    conn: sqlite3.Connection,
    orch_home: Path,
    no_git: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def prune(repo: Path) -> None:
        if repo == Path("/one"):
            raise gitops.GitError("no repository")

    monkeypatch.setattr(gitops, "prune_worktrees", prune)
    add_task(conn, "a", target="two")
    (target_paths(orch_home, "two").worktrees / "a").mkdir(parents=True)
    report = collect(
        conn,
        targets={
            "one": make_target(Path("/one"), "one"),
            "two": make_target(Path("/two"), "two"),
        },
        orch_home=orch_home,
        now=NOW,
    )
    assert report.errors == ("one GitError",)
    assert report.removed_worktrees == ("two/a",)


def test_removing_a_worktree_keeps_its_branch(
    conn: sqlite3.Connection, orch_home: Path, repo: Path
) -> None:
    add_task(conn, "a")
    path = worktree(repo, orch_home, "a")
    report = run(conn, orch_home, repo)
    assert report.removed_worktrees == (f"{NAME}/a",)
    assert not path.exists()
    assert git(repo, "branch", "--list", "task/a").endswith("task/a")


def test_a_worktree_with_an_untracked_file_is_skipped(
    conn: sqlite3.Connection, orch_home: Path, repo: Path
) -> None:
    add_task(conn, "a")
    path = worktree(repo, orch_home, "a")
    (path / "new.txt").write_text("x", encoding="utf-8")
    report = run(conn, orch_home, repo)
    assert report.skipped_worktrees == (f"{NAME}/a",)
    assert (path / "new.txt").exists()


@pytest.mark.parametrize(
    "state",
    [
        state
        for state in TaskState
        if state not in (TaskState.CLOSED, TaskState.DISCARDED)
    ],
)
def test_no_unfinished_state_loses_its_worktree(
    conn: sqlite3.Connection, orch_home: Path, no_git: list[str], state: TaskState
) -> None:
    add_task(conn, "a", state)
    path = target_paths(orch_home, NAME).worktrees / "a"
    path.mkdir(parents=True)
    report = run(conn, orch_home)
    assert no_git == ["prune"]
    assert report == GcReport(dry_run=False)
    assert path.is_dir()


def test_a_symlink_in_worktrees_is_ignored(
    conn: sqlite3.Connection, orch_home: Path, no_git: list[str], tmp_path: Path
) -> None:
    add_task(conn, "a")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    worktrees = target_paths(orch_home, NAME).worktrees
    worktrees.mkdir(parents=True)
    (worktrees / "a").symlink_to(elsewhere)
    (worktrees / "stray").symlink_to(elsewhere)
    assert run(conn, orch_home) == GcReport(dry_run=False)
    assert no_git == ["prune"]
    assert elsewhere.is_dir()


# Which tasks and names: decided from the database, matched exactly


def test_expiring_one_task_leaves_an_id_that_extends_its_name(
    conn: sqlite3.Connection, orch_home: Path, no_git: list[str]
) -> None:
    add_task(conn, "m15-gc")
    add_task(conn, "m15-gc-2", updated=FRESH)
    removed = [touch(path) for path in task_files(orch_home, "m15-gc")]
    kept = [touch(path) for path in task_files(orch_home, "m15-gc-2")]
    report = run(conn, orch_home)
    assert report.removed_files == relative(orch_home, removed)
    assert all(path.exists() for path in kept)
    assert not any(path.exists() for path in removed)


def test_expiring_the_longer_id_leaves_the_shorter_one(
    conn: sqlite3.Connection, orch_home: Path, no_git: list[str]
) -> None:
    add_task(conn, "m15-gc", updated=FRESH)
    add_task(conn, "m15-gc-2")
    kept = [touch(path) for path in task_files(orch_home, "m15-gc")]
    removed = [touch(path) for path in task_files(orch_home, "m15-gc-2")]
    assert run(conn, orch_home).removed_files == relative(orch_home, removed)
    assert all(path.exists() for path in kept)


def test_files_of_an_id_not_in_the_database_are_kept(
    conn: sqlite3.Connection, orch_home: Path, no_git: list[str]
) -> None:
    files = [touch(path) for path in task_files(orch_home, "ghost")]
    assert run(conn, orch_home).removed_files == ()
    assert all(path.exists() for path in files)


@pytest.mark.parametrize(
    "relative_path",
    ["logs/a.1.log", "logs/review/a.1.log", "results/a.json", "reviews/a.diff"],
)
def test_a_symlink_with_a_matching_name_and_its_target_are_kept(
    conn: sqlite3.Connection,
    orch_home: Path,
    no_git: list[str],
    tmp_path: Path,
    relative_path: str,
) -> None:
    add_task(conn, "a")
    outside = touch(tmp_path / "outside.txt")
    link = target_paths(orch_home, NAME).root / relative_path
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)
    report = run(conn, orch_home)
    assert report.removed_files == ()
    assert report.errors == ()
    assert link.is_symlink()
    assert outside.exists()


def test_a_broken_symlink_with_a_matching_name_is_kept(
    conn: sqlite3.Connection, orch_home: Path, no_git: list[str], tmp_path: Path
) -> None:
    add_task(conn, "a")
    link = target_paths(orch_home, NAME).logs / "a.1.log"
    link.parent.mkdir(parents=True)
    link.symlink_to(tmp_path / "missing")
    assert run(conn, orch_home) == GcReport(dry_run=False)
    assert link.is_symlink()


@pytest.mark.parametrize(
    "directory", ["logs", "logs/review", "results", "reviews", "reviews/attempts"]
)
def test_a_symlinked_directory_is_not_entered(
    conn: sqlite3.Connection,
    orch_home: Path,
    no_git: list[str],
    tmp_path: Path,
    directory: str,
) -> None:
    add_task(conn, "a")
    outside = tmp_path / "outside"
    outside.mkdir()
    names = ("a.1.log", "a.json", "a.diff", "a.1.json")
    files = [touch(outside / name) for name in names]
    link = target_paths(orch_home, NAME).root / directory
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)
    assert run(conn, orch_home).removed_files == ()
    assert all(path.exists() for path in files)


def test_a_dry_run_lists_no_symlink(
    conn: sqlite3.Connection, orch_home: Path, no_git: list[str], tmp_path: Path
) -> None:
    add_task(conn, "a")
    outside = touch(tmp_path / "outside.txt")
    link = target_paths(orch_home, NAME).logs / "a.1.log"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)
    assert run(conn, orch_home, dry_run=True).removed_files == ()


# Dry run


def test_a_dry_run_changes_nothing_and_lists_the_same_items(
    conn: sqlite3.Connection, orch_home: Path, repo: Path
) -> None:
    add_task(conn, "a")
    add_task(conn, "b")
    files = [touch(path) for path in task_files(orch_home, "a")]
    path = worktree(repo, orch_home, "b")
    stale = worktree(repo, orch_home, "stray")
    subprocess.run(["rm", "-rf", str(stale)], check=True)
    report = run(conn, orch_home, repo, dry_run=True)
    assert report.dry_run is True
    assert report.removed_files == relative(orch_home, files)
    assert report.removed_worktrees == (f"{NAME}/b",)
    assert all(item.exists() for item in files)
    assert path.exists()
    assert str(stale) in git(repo, "worktree", "list")


def test_a_dry_run_calls_no_git_command_that_changes_anything(
    conn: sqlite3.Connection, orch_home: Path, no_git: list[str]
) -> None:
    add_task(conn, "a")
    (target_paths(orch_home, NAME).worktrees / "a").mkdir(parents=True)
    report = run(conn, orch_home, dry_run=True)
    assert no_git == []
    assert report.removed_worktrees == (f"{NAME}/a",)
    assert report.skipped_worktrees == ()


def test_a_dry_run_still_reports_unknown_directories(
    conn: sqlite3.Connection, orch_home: Path, no_git: list[str]
) -> None:
    (target_paths(orch_home, NAME).worktrees / "stray").mkdir(parents=True)
    report = run(conn, orch_home, dry_run=True)
    assert report.unknown_worktrees == (f"{NAME}/stray",)


def test_the_module_does_not_shadow_the_standard_gc() -> None:
    assert gc.__name__ == "orch.gc"
