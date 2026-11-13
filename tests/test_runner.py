import ast
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from orch import gate, lanes, results, runner, store
from orch.config import Config, Target, TargetPaths, target_paths
from orch.gate import ScopeViolations
from orch.gitops import GitError, create_worktree, fetch
from orch.models import (
    Effort,
    FailureReason,
    Grade,
    Lane,
    Outcome,
    Run,
    Task,
    TaskState,
)
from orch.results import load_result
from orch.runner import (
    RunError,
    descendants,
    log_label,
    next_log_path,
    process_started_at,
    run_task,
    stop_process,
)
from orch.store import StateMismatchError, connect, get_task, insert_task, latest_run

HUB = Path(__file__).resolve().parents[1]
MAIN = "origin/main"
TASK = "feat-x"
CHANGED = "src/fake_change.py"
OUTSIDE = "outside/fake_change.py"
TEST_FILE = "tests/test_fake_change.py"
SPEC = """+++
id = "{task_id}"
title = "Add the feature"
target = "sample"
grade = "L2"
lane = "{lane}"
{model}timeout_min = 1
modify = ["src/**"]
read = ["src/**"]
verify = "true"
depends_on = []
+++

Body of the task.
"""


MODEL_LINES = {
    "B": 'model = "gpt-6.1-sol"\neffort = "medium"\n',
    "C": 'model = "claude-sonnet-5-5"\n',
    "D": 'model = "gpt-6.1-sol"\neffort = "medium"\n',
}


def make_spec_text(task_id: str, lane: str) -> str:
    return SPEC.format(task_id=task_id, lane=lane, model=MODEL_LINES[lane])


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
def repo(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q")
    write(path / "src" / "app.py")
    git(path, "add", ".")
    git(path, "commit", "-qm", "base")
    git(path, "remote", "add", "origin", str(remote))
    git(path, "push", "-q", "origin", "main")
    fetch(path)
    return path


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def paths(home: Path) -> TargetPaths:
    return target_paths(home / ".orch", "sample")


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return connect(tmp_path / "state.db")


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


def make_config(**overrides: object) -> Config:
    values: dict[str, object] = {
        "usage_limit_patterns": {
            "codex": ("usage limit",),
            "claude": ("usage limit",),
        },
    }
    values.update(overrides)
    return Config(**values)


def make_env(home: Path, scenario: str, **extra: str) -> dict[str, str]:
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "CODEX_HOME": str(home / "codex"),
        "ORCH_HUB": str(HUB),
        "ORCH_FAKE_AGENTS": "1",
        "ORCH_FAKE_PYTHON": sys.executable,
        "ORCH_FAKE_SCENARIO": scenario,
        **extra,
    }


def add_task(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    worktree_root: Path,
    *,
    task_id: str = TASK,
    lane: Lane = Lane.B,
    state: TaskState = TaskState.RESERVED,
    attempt: int = 0,
    timeout_sec: int = 60,
) -> Path:
    worktree = worktree_root / task_id
    create_worktree(repo, worktree, f"task/{task_id}", MAIN)
    write(worktree / "tests" / "test_x.py")
    git(worktree, "add", ".")
    git(worktree, "commit", "-qm", "tests")
    test_sha = git(worktree, "rev-parse", "HEAD")
    insert_task(
        conn,
        Task(
            id=task_id,
            target="sample",
            title="Add feature X",
            grade=Grade.L2,
            lane=lane,
            model="m",
            effort=Effort.MEDIUM,
            state=state,
            attempt=attempt,
            timeout_sec=timeout_sec,
            test_sha=test_sha,
            worktree=str(worktree),
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        ),
    )
    write(
        paths.tasks / f"{task_id}.md",
        make_spec_text(task_id, lane.value),
    )
    return worktree


@pytest.fixture
def worktree(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths, tmp_path: Path
) -> Path:
    return add_task(conn, repo, paths, tmp_path / "worktrees")


def run(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    scenario: str,
    *,
    task_id: str = TASK,
    target: Target | None = None,
    config: Config | None = None,
    **extra: str,
) -> results.TaskResult:
    return run_task(
        conn,
        task_id,
        target=target if target is not None else make_target(repo),
        config=config if config is not None else make_config(),
        paths=paths,
        environ=make_env(home, scenario, **extra),
    )


def task_of(conn: sqlite3.Connection, task_id: str = TASK) -> Task:
    task = get_task(conn, task_id)
    assert task is not None
    return task


def run_of(conn: sqlite3.Connection, task_id: str = TASK) -> Run:
    row = latest_run(conn, task_id)
    assert row is not None
    return row


def events_of(conn: sqlite3.Connection, task_id: str = TASK) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT from_state, to_state FROM events WHERE task_id = ? ORDER BY id",
        (task_id,),
    )
    return list(rows)


def wait_until_dead(pid: int, limit: float = 8.0) -> bool:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def real_ps() -> None:
    """Skip inside the Codex sandbox, where ps cannot start. G2 runs these outside."""
    try:
        subprocess.run(["ps", "-o", "pid=", "-p", "1"], capture_output=True)
    except PermissionError:
        pytest.skip("ps is not allowed here")


# Checks before anything starts


def test_a_missing_task_is_refused(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths, home: Path
) -> None:
    with pytest.raises(RunError):
        run(conn, repo, paths, home, "success", task_id="nope")


@pytest.mark.parametrize(
    "state",
    [TaskState.PREPARED, TaskState.QUEUED, TaskState.RUNNING, TaskState.JUDGING],
)
def test_only_a_reserved_task_runs(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    tmp_path: Path,
    state: TaskState,
) -> None:
    add_task(conn, repo, paths, tmp_path / "worktrees", state=state)
    with pytest.raises(RunError):
        run(conn, repo, paths, home, "success")
    assert task_of(conn).state is state
    assert latest_run(conn, TASK) is None


@pytest.mark.parametrize("field", ["worktree", "test_sha"])
def test_a_task_needs_its_worktree_and_test_commit(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    field: str,
) -> None:
    store.update_task(conn, TASK, **{field: None})
    with pytest.raises(RunError):
        run(conn, repo, paths, home, "success")
    assert task_of(conn).state is TaskState.RESERVED


def test_a_removed_worktree_is_refused(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths, home: Path, worktree: Path
) -> None:
    store.update_task(conn, TASK, worktree=str(worktree / "gone"))
    with pytest.raises(RunError):
        run(conn, repo, paths, home, "success")
    assert task_of(conn).state is TaskState.RESERVED


@pytest.mark.parametrize(
    "text",
    [
        None,
        "no front matter\n",
        make_spec_text("other", "B"),
        make_spec_text(TASK, "D"),
        make_spec_text(TASK, "B").replace('effort = "medium"\n', ""),
    ],
    ids=["missing", "invalid", "other_id", "unsupported_lane", "no_effort"],
)
def test_a_spec_that_cannot_start_leaves_the_task_reserved(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    text: str | None,
) -> None:
    spec = paths.tasks / f"{TASK}.md"
    if text is None:
        spec.unlink()
    else:
        spec.write_text(text, encoding="utf-8")
    with pytest.raises(RunError):
        run(conn, repo, paths, home, "success")
    assert task_of(conn).state is TaskState.RESERVED
    assert latest_run(conn, TASK) is None


# A normal run


def test_success_records_the_run_and_moves_to_judging(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths, home: Path, worktree: Path
) -> None:
    result = run(conn, repo, paths, home, "success")
    assert result.status is Outcome.DONE
    assert result.failure_reason is None
    assert task_of(conn).state is TaskState.JUDGING
    assert events_of(conn) == [("reserved", "running"), ("running", "judging")]
    row = run_of(conn)
    assert row.attempt == 1
    assert row.lane is Lane.B
    assert row.model == "gpt-6.1-sol"
    assert row.effort is Effort.MEDIUM
    assert row.pid is not None and row.pid > 0
    assert row.started_at is not None
    assert row.ended_at is not None
    assert row.exit_code == 0
    assert row.outcome is Outcome.DONE
    assert row.failure_reason is None


def test_the_report_moves_to_the_results_directory(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths, home: Path, worktree: Path
) -> None:
    result = run(conn, repo, paths, home, "success")
    assert not (worktree / ".orch-result.json").exists()
    assert load_result(paths.results / f"{TASK}.json", TASK) == result


def test_a_leftover_report_does_not_count_as_this_run(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths, home: Path, worktree: Path
) -> None:
    write(worktree / ".orch-result.json", "{}\n")
    result = run(conn, repo, paths, home, "immediate_exit")
    assert result.status is Outcome.FAILED
    assert result.failure_reason is FailureReason.REPORT_MISSING
    assert not (paths.logs / f"{TASK}.1.rejected-result.json").exists()


def test_first_attempt_becomes_one_and_a_retry_keeps_its_number(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    tmp_path: Path,
) -> None:
    add_task(conn, repo, paths, tmp_path / "worktrees", attempt=2)
    run(conn, repo, paths, home, "success")
    assert task_of(conn).attempt == 2
    assert run_of(conn).attempt == 2


def test_first_attempt_is_stored_on_the_task(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths, home: Path, worktree: Path
) -> None:
    run(conn, repo, paths, home, "success")
    assert task_of(conn).attempt == 1


def test_lane_c_runs_through_the_same_flow(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    tmp_path: Path,
) -> None:
    add_task(conn, repo, paths, tmp_path / "worktrees", lane=Lane.C)
    result = run(conn, repo, paths, home, "success")
    assert result.status is Outcome.DONE
    assert run_of(conn).lane is Lane.C


def test_the_agent_sees_only_the_allowed_environment(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    tmp_path: Path,
) -> None:
    dump = tmp_path / "env.json"
    run(
        conn,
        repo,
        paths,
        home,
        "env_dump",
        ORCH_FAKE_ENV_FILE=str(dump),
        GH_TOKEN="secret",
        ORCH_SECRET="secret",
    )
    names = json.loads(dump.read_text(encoding="utf-8"))
    assert "GH_TOKEN" not in names
    assert "ORCH_SECRET" not in names
    assert "PATH" in names


def test_the_output_goes_to_the_log(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths, home: Path, worktree: Path
) -> None:
    run(conn, repo, paths, home, "report_with_limit_message")
    text = (paths.logs / f"{TASK}.1.log").read_text(encoding="utf-8")
    assert "You hit your usage limit" in text


def test_no_transaction_is_open_while_the_agent_runs(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[bool] = []
    real = subprocess.Popen

    def spy(args: object, *rest: object, **options: object) -> object:
        if isinstance(args, list | tuple) and str(args[0]).endswith("codex"):
            seen.append(conn.in_transaction)
        return real(args, *rest, **options)

    monkeypatch.setattr(subprocess, "Popen", spy)
    run(conn, repo, paths, home, "success")
    assert seen == [False]


# Classification: the first matching rule wins


@pytest.mark.parametrize(
    ("scenario", "outcome", "reason", "exit_code"),
    [
        ("success", Outcome.DONE, None, 0),
        ("partial", Outcome.PARTIAL, None, 0),
        ("blocked", Outcome.BLOCKED, FailureReason.SPEC_MISMATCH, 0),
        ("report_nonzero", Outcome.DONE, None, 3),
        ("report_with_limit_message", Outcome.DONE, None, 0),
        ("partial_out_of_scope", Outcome.PARTIAL, None, 0),
        ("usage_limit", Outcome.BLOCKED_QUOTA, None, 1),
        ("report_missing", Outcome.FAILED, FailureReason.REPORT_MISSING, 0),
        ("immediate_exit", Outcome.FAILED, FailureReason.REPORT_MISSING, 1),
        ("malformed_report", Outcome.FAILED, FailureReason.REPORT_MISSING, 0),
        ("out_of_scope", Outcome.FAILED, FailureReason.OUT_OF_SCOPE, 0),
        ("test_file_change", Outcome.FAILED, FailureReason.OUT_OF_SCOPE, 0),
        ("killed", Outcome.FAILED, FailureReason.KILLED, -signal.SIGKILL),
        ("report_then_killed", Outcome.FAILED, FailureReason.KILLED, -signal.SIGKILL),
    ],
)
def test_classification(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    scenario: str,
    outcome: Outcome,
    reason: FailureReason | None,
    exit_code: int,
) -> None:
    result = run(conn, repo, paths, home, scenario)
    assert result.status is outcome
    assert result.failure_reason is reason
    assert load_result(paths.results / f"{TASK}.json", TASK) == result
    assert task_of(conn).state is TaskState.JUDGING
    row = run_of(conn)
    assert row.outcome is outcome
    assert row.failure_reason is reason
    assert row.exit_code == exit_code
    assert not (worktree / ".orch-result.json").exists()


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [("out_of_scope", [OUTSIDE]), ("test_file_change", [TEST_FILE])],
)
def test_out_of_scope_lists_the_paths_found(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    scenario: str,
    expected: list[str],
) -> None:
    result = run(conn, repo, paths, home, scenario)
    assert list(result.out_of_scope_changes) == expected


def test_a_shared_path_counts_as_out_of_scope(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths, home: Path, worktree: Path
) -> None:
    target = make_target(repo, shared_paths=(CHANGED,))
    result = run(conn, repo, paths, home, "success", target=target)
    assert result.failure_reason is FailureReason.OUT_OF_SCOPE
    assert list(result.out_of_scope_changes) == [CHANGED]


def test_an_out_of_scope_result_keeps_the_agents_summary(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths, home: Path, worktree: Path
) -> None:
    result = run(conn, repo, paths, home, "out_of_scope")
    assert result.summary == "가짜 에이전트의 결과입니다."


def test_the_scope_check_uses_the_recorded_test_commit(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def spy(path: Path, **options: object) -> ScopeViolations:
        calls.append({"path": path, **options})
        return ScopeViolations()

    monkeypatch.setattr(gate, "check_scope", spy)
    target = make_target(repo, shared_paths=("pyproject.toml",))
    run(conn, repo, paths, home, "success", target=target)
    assert calls == [
        {
            "path": worktree,
            "since": task_of(conn).test_sha,
            "main_ref": MAIN,
            "modify": ("src/**",),
            "shared_paths": ("pyproject.toml",),
        }
    ]


def test_a_rejected_report_is_kept_in_the_logs(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths, home: Path, worktree: Path
) -> None:
    run(conn, repo, paths, home, "malformed_report")
    rejected = paths.logs / f"{TASK}.1.rejected-result.json"
    assert rejected.read_text(encoding="utf-8") == "{not json"


def test_a_report_from_a_killed_run_is_kept_but_not_used(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths, home: Path, worktree: Path
) -> None:
    run(conn, repo, paths, home, "report_then_killed")
    assert (paths.logs / f"{TASK}.1.rejected-result.json").exists()


def test_a_substitute_report_names_the_log(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths, home: Path, worktree: Path
) -> None:
    result = run(conn, repo, paths, home, "immediate_exit")
    assert result.log_path == f"~/.orch/sample/logs/{TASK}.1.log"
    assert result.verification_command == "true"
    assert result.verification_passed is False


def test_a_limit_message_from_an_earlier_attempt_does_not_count(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths, home: Path, worktree: Path
) -> None:
    old = "You hit your usage limit. Try again later.\n"
    write(paths.logs / f"{TASK}.1.log", old)
    result = run(conn, repo, paths, home, "immediate_exit")
    assert result.failure_reason is FailureReason.REPORT_MISSING
    assert (paths.logs / f"{TASK}.1.log").read_text(encoding="utf-8") == old
    assert old not in (paths.logs / f"{TASK}.2.log").read_text(encoding="utf-8")


def test_a_failing_classification_does_not_strand_the_task(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **options: object) -> ScopeViolations:
        raise GitError("boom")

    monkeypatch.setattr(gate, "check_scope", fail)
    result = run(conn, repo, paths, home, "success")
    assert result.status is Outcome.FAILED
    assert result.failure_reason is FailureReason.REPORT_MISSING
    assert task_of(conn).state is TaskState.JUDGING
    assert run_of(conn).outcome is Outcome.FAILED


def test_an_agent_that_cannot_start_does_not_strand_the_task(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths, home: Path, worktree: Path
) -> None:
    environ = make_env(home, "success")
    del environ["ORCH_FAKE_AGENTS"]
    result = run_task(
        conn,
        TASK,
        target=make_target(repo),
        config=make_config(codex_path=home / "missing"),
        paths=paths,
        environ=environ,
    )
    assert result.status is Outcome.FAILED
    assert result.failure_reason is FailureReason.REPORT_MISSING
    assert task_of(conn).state is TaskState.JUDGING
    row = run_of(conn)
    assert row.outcome is Outcome.FAILED
    assert row.exit_code is None
    assert row.ended_at is not None


def test_run_and_state_change_together(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = store.transition

    def fail_at_the_end(
        connection: sqlite3.Connection,
        task_id: str,
        expected: TaskState,
        new: TaskState,
        **options: object,
    ) -> None:
        if new is TaskState.JUDGING:
            raise StateMismatchError("moved by recovery")
        real(connection, task_id, expected, new, **options)

    monkeypatch.setattr(store, "transition", fail_at_the_end)
    with pytest.raises(StateMismatchError):
        run(conn, repo, paths, home, "success")
    row = run_of(conn)
    assert row.ended_at is None
    assert row.outcome is None
    assert task_of(conn).state is TaskState.RUNNING


# Numbered logs


def test_next_log_path_starts_at_one_without_a_directory(tmp_path: Path) -> None:
    logs = tmp_path / "missing"
    assert next_log_path(logs, TASK) == logs / f"{TASK}.1.log"
    assert not logs.exists()


def test_next_log_path_follows_the_highest_number_of_this_task(tmp_path: Path) -> None:
    for name in (
        f"{TASK}.2.log",
        f"{TASK}.10.log",
        f"{TASK}-other.99.log",
        f"{TASK}.abc.log",
        f"{TASK}.log",
        f"{TASK}.prev.log",
        f"{TASK}.gate.log",
        f"{TASK}.3.log.bak",
    ):
        write(tmp_path / name)
    assert next_log_path(tmp_path, TASK) == tmp_path / f"{TASK}.11.log"
    assert not (tmp_path / f"{TASK}.11.log").exists()


def test_next_log_path_treats_the_task_id_literally(tmp_path: Path) -> None:
    write(tmp_path / "featxy.5.log")
    assert next_log_path(tmp_path, "feat.y") == tmp_path / "feat.y.1.log"


def test_each_run_writes_its_own_log(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths, home: Path, worktree: Path
) -> None:
    run(conn, repo, paths, home, "usage_limit")
    first = (paths.logs / f"{TASK}.1.log").read_text(encoding="utf-8")
    store.transition(conn, TASK, TaskState.JUDGING, TaskState.RESERVED, actor="test")
    result = run(conn, repo, paths, home, "immediate_exit")
    assert result.status is Outcome.FAILED
    assert result.failure_reason is FailureReason.REPORT_MISSING
    assert result.log_path == f"~/.orch/sample/logs/{TASK}.2.log"
    assert (paths.logs / f"{TASK}.1.log").read_text(encoding="utf-8") == first
    assert (paths.logs / f"{TASK}.2.log").exists()
    assert not (paths.logs / f"{TASK}.prev.log").exists()
    assert not (paths.logs / f"{TASK}.log").exists()


def test_an_old_unnumbered_log_stays_and_does_not_count(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths, home: Path, worktree: Path
) -> None:
    old = "You hit your usage limit. Try again later.\n"
    write(paths.logs / f"{TASK}.log", old)
    result = run(conn, repo, paths, home, "immediate_exit")
    assert result.failure_reason is FailureReason.REPORT_MISSING
    assert (paths.logs / f"{TASK}.log").read_text(encoding="utf-8") == old
    assert (paths.logs / f"{TASK}.1.log").exists()


def test_the_agent_is_told_this_runs_log(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    labels: list[str] = []
    real = lanes.build_command

    def spy(**options: object) -> object:
        labels.append(str(options["log_label"]))
        return real(**options)

    monkeypatch.setattr(lanes, "build_command", spy)
    write(paths.logs / f"{TASK}.4.log")
    run(conn, repo, paths, home, "success")
    assert labels == [f"~/.orch/sample/logs/{TASK}.5.log"]


def test_a_logs_path_that_cannot_be_read_leaves_the_task_reserved(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths, home: Path, worktree: Path
) -> None:
    write(paths.logs, "not a directory\n")
    with pytest.raises(RunError):
        run(conn, repo, paths, home, "success")
    assert task_of(conn).state is TaskState.RESERVED
    assert latest_run(conn, TASK) is None


# The result file


@pytest.mark.parametrize("scenario", ["success", "immediate_exit"])
def test_a_new_result_replaces_an_earlier_one(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    scenario: str,
) -> None:
    write(paths.results / f"{TASK}.json", "old result\n")
    result = run(conn, repo, paths, home, scenario)
    assert load_result(paths.results / f"{TASK}.json", TASK) == result


@pytest.mark.parametrize("scenario", ["success", "immediate_exit"])
def test_a_failed_result_write_does_not_strand_the_task(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    def fail(*args: object) -> None:
        raise OSError("disk full")

    write(paths.results / f"{TASK}.json", "old result\n")
    monkeypatch.setattr(results, "write_result", fail)
    result = run(conn, repo, paths, home, scenario)
    assert result.status is Outcome.FAILED
    assert result.failure_reason is FailureReason.REPORT_MISSING
    assert result.verification_passed is False
    assert task_of(conn).state is TaskState.JUDGING
    row = run_of(conn)
    assert row.outcome is Outcome.FAILED
    assert row.failure_reason is FailureReason.REPORT_MISSING
    assert row.ended_at is not None
    assert not (paths.results / f"{TASK}.json").exists()


def test_a_results_path_that_cannot_be_written_does_not_strand_the_task(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths, home: Path, worktree: Path
) -> None:
    write(paths.results, "not a directory\n")
    result = run(conn, repo, paths, home, "success")
    assert result.status is Outcome.FAILED
    assert result.failure_reason is FailureReason.REPORT_MISSING
    assert task_of(conn).state is TaskState.JUDGING
    assert run_of(conn).outcome is Outcome.FAILED


# Interrupts and failures around the process start


class ExitFails:
    """A context manager for a real file whose exit raises after closing it."""

    def __init__(self, stream: object, error: BaseException) -> None:
        self.stream = stream
        self.error = error

    def __enter__(self) -> object:
        return self.stream

    def __exit__(self, *details: object) -> None:
        self.stream.close()
        raise self.error


def fail_after_start(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> list[int]:
    """Let the agent start, then raise while the start block closes its files."""
    pids: list[int] = []
    real_file = tempfile.TemporaryFile
    real_popen = subprocess.Popen

    def popen(*args: object, **options: object) -> subprocess.Popen[bytes]:
        process = real_popen(*args, **options)
        pids.append(process.pid)
        return process

    monkeypatch.setattr(
        tempfile,
        "TemporaryFile",
        lambda **options: ExitFails(real_file(**options), error),
    )
    monkeypatch.setattr(subprocess, "Popen", popen)
    return pids


def test_an_interrupt_before_the_process_starts_records_an_abort(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **options: object) -> subprocess.Popen[bytes]:
        raise KeyboardInterrupt

    monkeypatch.setattr(subprocess, "Popen", fail)
    with pytest.raises(KeyboardInterrupt):
        run(conn, repo, paths, home, "success")
    assert task_of(conn).state is TaskState.JUDGING
    row = run_of(conn)
    assert row.outcome is Outcome.FAILED
    assert row.failure_reason is FailureReason.ABORTED
    assert row.pid is None
    assert row.exit_code is None
    result = load_result(paths.results / f"{TASK}.json", TASK)
    assert result.failure_reason is FailureReason.ABORTED


def test_any_error_starting_the_process_does_not_strand_the_task(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **options: object) -> subprocess.Popen[bytes]:
        raise ValueError("embedded null byte")

    monkeypatch.setattr(subprocess, "Popen", fail)
    result = run(conn, repo, paths, home, "success")
    assert result.status is Outcome.FAILED
    assert result.failure_reason is FailureReason.REPORT_MISSING
    assert task_of(conn).state is TaskState.JUDGING


def test_an_interrupt_right_after_the_start_stops_the_agent(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "TERM_GRACE_SEC", 2)
    pids = fail_after_start(monkeypatch, KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        run(conn, repo, paths, home, "timeout")
    assert pids
    assert wait_until_dead(pids[0])
    assert task_of(conn).state is TaskState.JUDGING
    assert run_of(conn).failure_reason is FailureReason.ABORTED


def test_an_error_right_after_the_start_stops_the_agent(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "TERM_GRACE_SEC", 2)
    pids = fail_after_start(monkeypatch, OSError("close failed"))
    result = run(conn, repo, paths, home, "timeout")
    assert result.status is Outcome.FAILED
    assert result.failure_reason is FailureReason.REPORT_MISSING
    assert pids
    assert wait_until_dead(pids[0])
    assert task_of(conn).state is TaskState.JUDGING


def test_an_interrupt_while_recording_the_process_stops_the_agent(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "TERM_GRACE_SEC", 2)
    pids: list[int] = []

    def fail(*args: object, **options: object) -> None:
        pids.append(int(options["pid"]))
        raise KeyboardInterrupt

    monkeypatch.setattr(store, "set_run_process", fail)
    with pytest.raises(KeyboardInterrupt):
        run(conn, repo, paths, home, "timeout")
    assert wait_until_dead(pids[0])
    assert task_of(conn).state is TaskState.JUDGING
    assert run_of(conn).failure_reason is FailureReason.ABORTED


def test_an_interrupt_during_classification_records_an_abort(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object) -> results.TaskResult:
        raise KeyboardInterrupt

    monkeypatch.setattr(results, "collect_result", fail)
    with pytest.raises(KeyboardInterrupt):
        run(conn, repo, paths, home, "success")
    assert task_of(conn).state is TaskState.JUDGING
    assert run_of(conn).failure_reason is FailureReason.ABORTED


# An error while recording an abort


def test_an_error_recording_an_abort_does_not_hide_the_interrupt(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupt(*args: object) -> results.TaskResult:
        raise KeyboardInterrupt

    def fail(*args: object, **options: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(results, "collect_result", interrupt)
    monkeypatch.setattr(store, "finish_run", fail)
    with pytest.raises(KeyboardInterrupt) as caught:
        run(conn, repo, paths, home, "success")
    assert "disk I/O error" in "\n".join(caught.value.__notes__)
    assert task_of(conn).state is TaskState.RUNNING


@pytest.mark.parametrize("helper", ["stop_process", "_reject_report"])
def test_a_failing_cleanup_step_still_records_the_abort(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
    helper: str,
) -> None:
    monkeypatch.setattr(runner, "TERM_GRACE_SEC", 2)
    real = getattr(runner, helper)
    pids: list[int] = []

    def interrupt(*args: object, **options: object) -> None:
        pids.append(int(options["pid"]))
        raise KeyboardInterrupt

    def fail(*args: object) -> None:
        real(*args)
        raise OSError("cleanup failed")

    monkeypatch.setattr(store, "set_run_process", interrupt)
    monkeypatch.setattr(runner, helper, fail)
    with pytest.raises(KeyboardInterrupt) as caught:
        run(conn, repo, paths, home, "timeout")
    assert wait_until_dead(pids[0])
    assert "cleanup failed" in "\n".join(caught.value.__notes__)
    assert task_of(conn).state is TaskState.JUDGING
    assert run_of(conn).failure_reason is FailureReason.ABORTED


# Time limit and stopping


def test_a_limit_message_does_not_save_a_run_past_its_limit(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    tmp_path: Path,
) -> None:
    add_task(conn, repo, paths, tmp_path / "worktrees", timeout_sec=1)
    result = run(conn, repo, paths, home, "limit_then_hang")
    assert result.status is Outcome.FAILED
    assert result.failure_reason is FailureReason.TIMEOUT
    text = (paths.logs / f"{TASK}.1.log").read_text(encoding="utf-8")
    assert "usage limit" in text


def test_a_run_past_its_limit_is_stopped(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    tmp_path: Path,
) -> None:
    add_task(conn, repo, paths, tmp_path / "worktrees", timeout_sec=1)
    started = time.monotonic()
    result = run(conn, repo, paths, home, "timeout")
    assert time.monotonic() - started < 30
    assert result.status is Outcome.FAILED
    assert result.failure_reason is FailureReason.TIMEOUT
    assert task_of(conn).state is TaskState.JUDGING
    row = run_of(conn)
    assert row.exit_code is not None and row.exit_code < 0


def test_a_report_does_not_save_a_run_past_its_limit(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    tmp_path: Path,
) -> None:
    worktree = add_task(conn, repo, paths, tmp_path / "worktrees", timeout_sec=1)
    result = run(conn, repo, paths, home, "report_then_hang")
    assert result.failure_reason is FailureReason.TIMEOUT
    assert not (worktree / ".orch-result.json").exists()
    assert (paths.logs / f"{TASK}.1.rejected-result.json").exists()


def test_an_agent_ignoring_sigterm_gets_sigkill(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "TERM_GRACE_SEC", 1)
    add_task(conn, repo, paths, tmp_path / "worktrees", timeout_sec=1)
    started = time.monotonic()
    result = run(conn, repo, paths, home, "ignore_term", ORCH_FAKE_SLEEP="60")
    assert time.monotonic() - started < 30
    assert result.failure_reason is FailureReason.TIMEOUT
    assert run_of(conn).exit_code == -signal.SIGKILL


def test_stop_process_ends_a_process_that_exits_on_sigterm(tmp_path: Path) -> None:
    process = subprocess.Popen(["sleep", "60"], start_new_session=True)
    stop_process(process)
    assert process.returncode == -signal.SIGTERM


def test_stop_process_accepts_a_process_already_gone(tmp_path: Path) -> None:
    process = subprocess.Popen(["true"], start_new_session=True)
    process.wait()
    stop_process(process)
    assert process.returncode == 0


def test_children_left_after_a_normal_exit_are_stopped(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "pid"
    result = run(
        conn, repo, paths, home, "leftover_child", ORCH_FAKE_PID_FILE=str(pid_file)
    )
    assert result.status is Outcome.DONE
    assert wait_until_dead(int(pid_file.read_text(encoding="utf-8")))


def test_an_interrupt_stops_the_agent_and_records_an_abort(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "TERM_GRACE_SEC", 2)

    def interrupt(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGALRM, interrupt)
    signal.setitimer(signal.ITIMER_REAL, 1.0)
    try:
        with pytest.raises(KeyboardInterrupt):
            run(conn, repo, paths, home, "timeout")
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    assert task_of(conn).state is TaskState.JUDGING
    row = run_of(conn)
    assert row.outcome is Outcome.FAILED
    assert row.failure_reason is FailureReason.ABORTED
    assert row.pid is not None
    assert wait_until_dead(row.pid)
    result = load_result(paths.results / f"{TASK}.json", TASK)
    assert result.failure_reason is FailureReason.ABORTED


# Process details, which need a real ps


def test_process_started_at_reads_ps() -> None:
    real_ps()
    assert process_started_at(os.getpid()) != ""


def test_process_started_at_is_stable_and_empty_for_a_dead_process() -> None:
    real_ps()
    assert process_started_at(os.getpid()) == process_started_at(os.getpid())
    process = subprocess.Popen(["true"])
    process.wait()
    assert process_started_at(process.pid) == ""


def test_descendants_finds_a_child_in_another_session() -> None:
    real_ps()
    script = (
        "import subprocess,time;"
        "subprocess.Popen(['sleep','60'],start_new_session=True);"
        "time.sleep(60)"
    )
    parent = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
    try:
        deadline = time.monotonic() + 8
        found: set[int] = set()
        while not found and time.monotonic() < deadline:
            found = descendants(parent.pid)
            time.sleep(0.05)
        assert len(found) == 1
    finally:
        for pid in found:
            os.kill(pid, signal.SIGKILL)
        parent.kill()
        parent.wait()


def test_the_process_start_time_is_recorded(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths, home: Path, worktree: Path
) -> None:
    real_ps()
    run(conn, repo, paths, home, "success")
    assert run_of(conn).pid_started_at


def test_a_timeout_stops_a_tool_command_in_its_own_session(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_ps()
    monkeypatch.setattr(runner, "TERM_GRACE_SEC", 2)
    add_task(conn, repo, paths, tmp_path / "worktrees", timeout_sec=2)
    pid_file = tmp_path / "pid"
    result = run(
        conn, repo, paths, home, "detached_child", ORCH_FAKE_PID_FILE=str(pid_file)
    )
    assert result.failure_reason is FailureReason.TIMEOUT
    assert wait_until_dead(int(pid_file.read_text(encoding="utf-8")))


def test_ps_that_cannot_start_gives_empty_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def denied(*args: object, **options: object) -> object:
        raise PermissionError("denied")

    monkeypatch.setattr(subprocess, "run", denied)
    assert process_started_at(os.getpid()) == ""
    assert descendants(os.getpid()) == set()


def test_ps_failure_does_not_stop_a_timeout(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "descendants", lambda root: set())
    add_task(conn, repo, paths, tmp_path / "worktrees", timeout_sec=1)
    result = run(conn, repo, paths, home, "timeout")
    assert result.failure_reason is FailureReason.TIMEOUT


# Log label


def test_log_label_uses_a_tilde_under_the_home() -> None:
    home = Path("/", "somewhere", "me")
    label = log_label(home / ".orch" / "x.log", {"HOME": str(home)})
    assert label == "~/.orch/x.log"


@pytest.mark.parametrize("environ", [{}, {"HOME": "/other"}])
def test_log_label_keeps_a_path_outside_the_home(environ: dict[str, str]) -> None:
    path = Path("/", "somewhere", "x.log")
    assert log_label(path, environ) == str(path)


# Several runs at once


def test_runs_ending_together_keep_the_state_consistent(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    tmp_path: Path,
) -> None:
    ids = [f"feat-{number}" for number in range(4)]
    for task_id in ids:
        add_task(conn, repo, paths, tmp_path / "worktrees", task_id=task_id)
    failures: list[BaseException] = []
    barrier = threading.Barrier(len(ids))

    def work(task_id: str) -> None:
        connection = connect(tmp_path / "state.db")
        try:
            barrier.wait(timeout=30)
            run(connection, repo, paths, home, "success", task_id=task_id)
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=work, args=(task_id,)) for task_id in ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert failures == []
    assert store.list_tasks(conn, states=[TaskState.RUNNING]) == ()
    for task_id in ids:
        assert task_of(conn, task_id).state is TaskState.JUDGING
        assert run_of(conn, task_id).outcome is Outcome.DONE
        assert (
            load_result(paths.results / f"{task_id}.json", task_id).task_id == task_id
        )


# Conventions


def test_module_uses_only_the_standard_library_and_orch() -> None:
    tree = ast.parse(Path(runner.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module is not None
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Constant) and node.value == "git":
            pytest.fail("runner.py must not name the git command")
    assert imported - {"orch"} <= sys.stdlib_module_names


# on_started


def run_with_callback(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    callback: object,
) -> results.TaskResult:
    return run_task(
        conn,
        TASK,
        target=make_target(repo),
        config=make_config(),
        paths=paths,
        environ=make_env(home, "success"),
        on_started=callback,
    )


def test_on_started_runs_once_after_running_is_committed_and_before_the_agent(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
) -> None:
    seen: list[tuple[TaskState, bool, bool]] = []

    def callback() -> None:
        seen.append(
            (
                task_of(conn).state,
                conn.in_transaction,
                (worktree / "src" / "fake_change.py").exists(),
            )
        )

    result = run_with_callback(conn, repo, paths, home, callback)
    assert result.status == "DONE"
    assert seen == [(TaskState.RUNNING, False, False)]
    assert task_of(conn).state is TaskState.JUDGING


def test_a_failing_on_started_changes_nothing(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
) -> None:
    def callback() -> None:
        raise OSError("disk full")

    result = run_with_callback(conn, repo, paths, home, callback)
    assert result.status == "DONE"
    assert result.failure_reason is None
    assert task_of(conn).state is TaskState.JUDGING
    assert run_of(conn).outcome == "DONE"


def test_on_started_is_optional(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
) -> None:
    assert run(conn, repo, paths, home, "success").status == "DONE"


# Rejected reports follow the run's log number (m-kill-core)


def test_rejected_path_uses_the_log_number(tmp_path: Path) -> None:
    assert runner.rejected_path(tmp_path / f"{TASK}.3.log") == (
        tmp_path / f"{TASK}.3.rejected-result.json"
    )
    assert runner.rejected_path(tmp_path / "a.b.12.log") == (
        tmp_path / "a.b.12.rejected-result.json"
    )


def test_latest_log_path_is_the_highest_number_of_this_task(tmp_path: Path) -> None:
    assert runner.latest_log_path(tmp_path / "missing", TASK) is None
    for name in (f"{TASK}.1.log", f"{TASK}.10.log", f"{TASK}.2.log", "other.11.log"):
        write(tmp_path / name)
    write(tmp_path / f"{TASK}.log")
    assert runner.latest_log_path(tmp_path, TASK) == tmp_path / f"{TASK}.10.log"
    assert runner.latest_log_path(tmp_path, "nobody") is None


def test_a_second_run_keeps_the_first_rejected_report(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths, home: Path, worktree: Path
) -> None:
    run(conn, repo, paths, home, "malformed_report")
    conn.execute("UPDATE tasks SET state = 'reserved' WHERE id = ?", (TASK,))
    run(conn, repo, paths, home, "report_then_killed")
    assert (paths.logs / f"{TASK}.1.rejected-result.json").read_text(
        encoding="utf-8"
    ) == "{not json"
    assert (paths.logs / f"{TASK}.2.rejected-result.json").exists()
    assert not (paths.logs / f"{TASK}.rejected-result.json").exists()


# Stopping a recorded process (m-kill-core)


def start_sleeper(*, ignore_term: bool = False) -> subprocess.Popen[bytes]:
    code = "import signal, time\n"
    if ignore_term:
        code += "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    code += "print('ready', flush=True)\ntime.sleep(3600)\n"
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline() == b"ready\n"
    return process


def test_process_alive_needs_the_recorded_start_time() -> None:
    real_ps()
    process = start_sleeper()
    try:
        started = process_started_at(process.pid)
        assert started
        assert runner.process_alive(process.pid, started) is True
        assert runner.process_alive(process.pid, "") is False
        assert runner.process_alive(process.pid, "Thu Jan  1 00:00:00 1970") is False
    finally:
        process.kill()
        process.wait()


def test_stop_pid_sends_nothing_to_a_process_that_is_not_the_recorded_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[str, int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: sent.append(("pg", pid, sig)))
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append(("p", pid, sig)))
    assert runner.stop_pid(4242, "x", alive=lambda pid, started: False) is False
    assert sent == []


def test_stop_pid_terminates_the_group() -> None:
    real_ps()
    process = start_sleeper()
    started = process_started_at(process.pid)
    assert runner.stop_pid(process.pid, started, grace_sec=5, poll_sec=0.05) is True
    assert process.wait(timeout=5) == -signal.SIGTERM


def test_stop_pid_kills_a_process_that_ignores_term() -> None:
    real_ps()
    process = start_sleeper(ignore_term=True)
    started = process_started_at(process.pid)
    begun = time.monotonic()
    assert runner.stop_pid(process.pid, started, grace_sec=0.5, poll_sec=0.05) is True
    assert process.wait(timeout=5) == -signal.SIGKILL
    assert time.monotonic() - begun < 5


# The runner honours an abort request (m-kill-core)


def start_run(
    tmp_path: Path, repo: Path, paths: TargetPaths, home: Path, scenario: str
) -> tuple[threading.Thread, dict[str, object]]:
    outcome: dict[str, object] = {}

    def target() -> None:
        own = connect(tmp_path / "state.db")
        try:
            outcome["result"] = run_task(
                own,
                TASK,
                target=make_target(repo),
                config=make_config(),
                paths=paths,
                environ=make_env(home, scenario),
            )
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            outcome["error"] = exc
        finally:
            own.close()

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, outcome


def wait_for_pid(conn: sqlite3.Connection, limit: float = 20.0) -> Run:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        row = latest_run(conn, TASK)
        if row is not None and row.pid:
            return row
        time.sleep(0.05)
    raise AssertionError("the run never recorded its process")


def finish(thread: threading.Thread, outcome: dict[str, object]) -> results.TaskResult:
    thread.join(timeout=60)
    assert not thread.is_alive()
    assert "error" not in outcome, outcome.get("error")
    result = outcome["result"]
    assert isinstance(result, results.TaskResult)
    return result


def test_an_abort_request_wins_over_a_signal_and_a_report(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    tmp_path: Path,
) -> None:
    thread, outcome = start_run(tmp_path, repo, paths, home, "report_then_hang")
    row = wait_for_pid(conn)
    deadline = time.monotonic() + 10
    while not (worktree / ".orch-result.json").exists():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert row.run_id is not None
    assert store.request_kill(conn, row.run_id, at="2026-11-07T00:00:00+00:00")
    os.killpg(row.pid, signal.SIGTERM)
    result = finish(thread, outcome)
    assert (result.status, result.failure_reason) == (
        Outcome.FAILED,
        FailureReason.ABORTED,
    )
    assert result.summary == runner.KILL_SUMMARY
    assert (paths.logs / f"{TASK}.1.rejected-result.json").exists()
    saved = load_result(paths.results / f"{TASK}.json", TASK)
    assert saved.failure_reason is FailureReason.ABORTED
    assert run_of(conn).failure_reason is FailureReason.ABORTED
    assert task_of(conn).state is TaskState.JUDGING


def test_an_abort_request_wins_over_the_time_limit(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    tmp_path: Path,
) -> None:
    add_task(conn, repo, paths, tmp_path / "worktrees", timeout_sec=2)
    thread, outcome = start_run(tmp_path, repo, paths, home, "timeout")
    row = wait_for_pid(conn)
    assert row.run_id is not None
    assert store.request_kill(conn, row.run_id, at="2026-11-07T00:00:00+00:00")
    result = finish(thread, outcome)
    assert result.failure_reason is FailureReason.ABORTED
    assert run_of(conn).failure_reason is FailureReason.ABORTED


def end_run_as_kill_would(conn: sqlite3.Connection, run_id: int) -> None:
    with store.transaction(conn):
        store.finish_run(
            conn,
            run_id,
            ended_at="2026-11-07T00:00:00+00:00",
            exit_code=None,
            outcome=Outcome.FAILED,
            failure_reason=FailureReason.ABORTED,
        )
        store.transition(
            conn, TASK, TaskState.RUNNING, TaskState.JUDGING, actor="orch", note="kill"
        )


def test_the_runner_keeps_a_run_that_kill_already_recorded(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    tmp_path: Path,
) -> None:
    thread, outcome = start_run(tmp_path, repo, paths, home, "timeout")
    row = wait_for_pid(conn)
    assert row.run_id is not None
    recorded = results.substitute_result(
        task_id=TASK,
        status=Outcome.FAILED,
        failure_reason=FailureReason.ABORTED,
        summary="kill이 기록한 결과",
        verification_command="true",
        log_path="~/x.log",
    )
    destination = paths.results / f"{TASK}.json"
    results.write_result(destination, recorded)
    before = destination.read_bytes()
    end_run_as_kill_would(conn, row.run_id)
    os.killpg(row.pid, signal.SIGKILL)
    result = finish(thread, outcome)
    assert result.summary == "kill이 기록한 결과"
    assert destination.read_bytes() == before
    assert events_of(conn).count(("running", "judging")) == 1
    assert run_of(conn).ended_at == "2026-11-07T00:00:00+00:00"
    assert task_of(conn).state is TaskState.JUDGING


def test_the_runner_writes_nothing_when_kill_recorded_without_a_file(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    tmp_path: Path,
) -> None:
    thread, outcome = start_run(tmp_path, repo, paths, home, "timeout")
    row = wait_for_pid(conn)
    assert row.run_id is not None
    end_run_as_kill_would(conn, row.run_id)
    os.killpg(row.pid, signal.SIGKILL)
    result = finish(thread, outcome)
    assert (result.status, result.failure_reason) == (
        Outcome.FAILED,
        FailureReason.ABORTED,
    )
    assert result.summary == runner.KILL_SUMMARY
    assert not (paths.results / f"{TASK}.json").exists()
    assert events_of(conn).count(("running", "judging")) == 1


# Hub sub-agent rules (M16)


@pytest.mark.parametrize("hub", ["unset", "relative", "empty", "rules-only"])
def test_a_run_without_the_hub_files_does_not_start(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    tmp_path: Path,
    hub: str,
) -> None:
    environ = make_env(home, "success")
    del environ["ORCH_FAKE_AGENTS"]
    if hub == "unset":
        del environ["ORCH_HUB"]
    elif hub == "relative":
        environ["ORCH_HUB"] = "hub"
    else:
        root = tmp_path / "bare-hub"
        root.mkdir()
        if hub == "rules-only":
            (root / "config").mkdir()
            (root / "config" / "subagent.md").write_text("rules\n", encoding="utf-8")
        environ["ORCH_HUB"] = str(root)
    with pytest.raises(RunError):
        run_task(
            conn,
            TASK,
            target=make_target(repo),
            config=make_config(),
            paths=paths,
            environ=environ,
        )
    assert task_of(conn).state is TaskState.RESERVED
    assert latest_run(conn, TASK) is None
    assert not list(paths.logs.glob(f"{TASK}.*.log")) if paths.logs.exists() else True


def test_a_fake_run_sends_the_hub_rules_to_the_agent(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
) -> None:
    captured: dict[str, str] = {}
    real_popen = runner.subprocess.Popen

    def spy(*args: object, **kwargs: object) -> object:
        stdin = kwargs.get("stdin")
        if hasattr(stdin, "read"):
            data = stdin.read()  # type: ignore[union-attr]
            stdin.seek(0)  # type: ignore[union-attr]
            captured["prompt"] = data.decode("utf-8")
        return real_popen(*args, **kwargs)  # type: ignore[arg-type]

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(runner.subprocess, "Popen", spy)
        run(conn, repo, paths, home, "success")
    rules = (HUB / "config" / "subagent.md").read_text(encoding="utf-8")
    schema = (HUB / "schemas" / "result.json").read_text(encoding="utf-8")
    assert captured["prompt"].endswith(rules + "\nResult schema:\n" + schema)
