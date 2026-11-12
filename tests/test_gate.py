import ast
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from orch import gate, gitops, store
from orch.config import Target
from orch.gate import (
    GateError,
    GateReport,
    GateStatus,
    ScopeViolations,
    check_scope,
    gate_task,
    glob_matches,
    review_required,
    run_command,
    run_gates,
    run_tests,
    scope_violations,
)
from orch.gitops import GitError, create_worktree, fetch, temporary_merge
from orch.models import Gate, Grade, Lane, Outcome, Run, Task, TaskState
from orch.store import (
    connect,
    finish_run,
    gate_results,
    get_task,
    insert_run,
    insert_task,
    latest_run,
    update_task,
)
from orch.taskfile import TaskSpec

MAIN = "origin/main"
BRANCH = "task/feat-x"
FAKE_HOME = Path("/", "Users", "tester")
REDACT = {"home": FAKE_HOME, "orch_home": FAKE_HOME / ".orch"}


# Matching paths


@pytest.mark.parametrize(
    ("path", "glob"),
    [
        ("src/a.py", "src/a.py"),
        ("src/a.py", "src/*.py"),
        ("src/a.py", "src/?.py"),
        ("src/a/b/c.py", "src/**"),
        ("src/a/b/c.py", "src/**/c.py"),
        ("src/c.py", "src/**/c.py"),
        ("c.py", "**/c.py"),
        ("a/b", "**"),
        ("a.b+c(d).py", "a.b+c(d).py"),
    ],
)
def test_glob_matches(path: str, glob: str) -> None:
    assert glob_matches(path, glob) is True


@pytest.mark.parametrize(
    ("path", "glob"),
    [
        ("src/a/b.py", "src/*.py"),
        ("src/ab.py", "src/?.py"),
        ("src/a.py", "src/a"),
        ("xsrc/a.py", "src/**"),
        ("src/a.py.bak", "src/a.py"),
        ("axb", "a.b"),
        ("src", "src/**"),
        ("src/a.py\n", "src/a.py"),
    ],
)
def test_glob_does_not_match(path: str, glob: str) -> None:
    assert glob_matches(path, glob) is False


# Scope comparison


def violations(changed: list[str], **overrides: object) -> ScopeViolations:
    values: dict[str, object] = {"modify": ["src/**"], "shared_paths": []}
    values.update(overrides)
    return scope_violations(changed, **values)


def test_paths_in_scope_have_no_violations() -> None:
    result = violations(["src/a.py", "src/b/c.py"])
    assert result == ScopeViolations()
    assert not result


def test_out_of_scope_paths_are_sorted() -> None:
    result = violations(["z.py", "a.py", "src/a.py"])
    assert result.out_of_scope == ("a.py", "z.py")
    assert result


def test_test_files_are_reported_even_inside_modify() -> None:
    result = violations(["tests/test_a.py"], modify=["**"])
    assert result.test_file_changed == ("tests/test_a.py",)
    assert result.out_of_scope == ()


@pytest.mark.parametrize(
    ("shared", "path"),
    [
        ("uv.lock", "uv.lock"),
        ("migrations/", "migrations/001.sql"),
        ("migrations/**", "migrations/a/001.sql"),
        ("migrations", "migrations/001.sql"),
        (".github/**", ".github/workflows/ci.yml"),
    ],
)
def test_shared_paths_are_reported_even_inside_modify(shared: str, path: str) -> None:
    result = violations([path], modify=["**"], shared_paths=[shared])
    assert result.shared_file_changed == (path,)


def test_a_shared_directory_does_not_cover_similar_names() -> None:
    result = violations(
        ["migrationsx/a.sql"], modify=["**"], shared_paths=["migrations/"]
    )
    assert result == ScopeViolations()


def test_the_result_file_does_not_count() -> None:
    assert violations([".orch-result.json"]) == ScopeViolations()


def test_one_path_can_break_several_rules() -> None:
    result = violations(["tests/a.py"], shared_paths=["tests/**"])
    assert result.test_file_changed == ("tests/a.py",)
    assert result.shared_file_changed == ("tests/a.py",)
    assert result.out_of_scope == ("tests/a.py",)


# Fixtures with real git


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def write(path: Path, text: str = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def add_commit(cwd: Path, name: str, text: str = "x\n") -> str:
    write(cwd / name, text)
    git(cwd, "add", "--", name)
    git(cwd, "commit", "-qm", f"change {name}")
    return git(cwd, "rev-parse", "HEAD")


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
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    write(path / ".gitignore", ".orch-result.json\n")
    add_commit(path, "base.txt")
    git(path, "add", ".gitignore")
    git(path, "commit", "-qm", "ignore")
    git(path, "remote", "add", "origin", str(remote))
    git(path, "push", "-q", "origin", "main")
    git(path, "fetch", "-q", "origin")
    return path


@pytest.fixture
def worktree(repo: Path, tmp_path: Path) -> Path:
    path = tmp_path / "wt"
    create_worktree(repo, path, BRANCH, MAIN)
    return path


@pytest.fixture
def test_sha(worktree: Path) -> str:
    return add_commit(worktree, "tests/test_x.py")


def make_spec(**overrides: object) -> TaskSpec:
    values: dict[str, object] = {
        "id": "feat-x",
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


def make_target(**overrides: object) -> Target:
    values: dict[str, object] = {
        "name": "sample",
        "path": Path("/", "repo"),
        "github": "owner/sample",
        "default_branch": "main",
        "setup_command": "true",
        "test_command": "true",
        "lint_command": "true",
    }
    values.update(overrides)
    return Target(**values)


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "logs" / "feat-x.gate.log"


# Checking scope on a worktree


def scope(worktree: Path, since: str, **overrides: object) -> ScopeViolations:
    values: dict[str, object] = {"modify": ["src/**"], "shared_paths": ["uv.lock"]}
    values.update(overrides)
    return check_scope(worktree, since=since, main_ref=MAIN, **values)


def test_check_scope_sees_new_uncommitted_and_committed_changes(
    worktree: Path, test_sha: str
) -> None:
    add_commit(worktree, "committed.py")
    write(worktree / "base.txt", "edited\n")
    write(worktree / "untracked.py")
    write(worktree / "src" / "ok.py")
    assert scope(worktree, test_sha).out_of_scope == (
        "base.txt",
        "committed.py",
        "untracked.py",
    )


def test_check_scope_catches_test_and_shared_files(
    worktree: Path, test_sha: str
) -> None:
    write(worktree / "tests" / "test_x.py", "edited\n")
    write(worktree / "uv.lock")
    result = scope(worktree, test_sha)
    assert result.test_file_changed == ("tests/test_x.py",)
    assert result.shared_file_changed == ("uv.lock",)


def test_check_scope_ignores_the_result_file_and_the_test_commit(
    worktree: Path, test_sha: str
) -> None:
    write(worktree / ".orch-result.json", "{}\n")
    assert scope(worktree, test_sha) == ScopeViolations()


def test_check_scope_ignores_files_from_a_temporary_merge(
    worktree: Path, test_sha: str, repo: Path
) -> None:
    add_commit(worktree, "src/ok.py")
    add_commit(repo, "main_only.py")
    git(repo, "push", "-q", "origin", "main")
    fetch(worktree)
    assert temporary_merge(worktree, MAIN).ok
    assert scope(worktree, test_sha) == ScopeViolations()


# Running commands


def test_run_command_returns_the_exit_code_and_logs_output(
    tmp_path: Path, log_path: Path
) -> None:
    code = run_command("echo out; echo err >&2; exit 3", tmp_path, log_path)
    assert code == 3
    text = log_path.read_text(encoding="utf-8")
    assert "echo out; echo err >&2; exit 3" in text
    assert "out\n" in text
    assert "err\n" in text


def test_run_command_appends_to_the_log(tmp_path: Path, log_path: Path) -> None:
    run_command("echo first", tmp_path, log_path)
    run_command("echo second", tmp_path, log_path)
    text = log_path.read_text(encoding="utf-8")
    assert "first" in text
    assert "second" in text


def test_run_command_runs_in_the_worktree(tmp_path: Path, log_path: Path) -> None:
    run_command("pwd > here.txt", tmp_path, log_path)
    assert (tmp_path / "here.txt").read_text().strip() == str(tmp_path.resolve())


def test_run_command_has_no_input(tmp_path: Path, log_path: Path) -> None:
    assert run_command("cat", tmp_path, log_path) == 0


def test_run_command_times_out_and_stops_the_whole_group(
    tmp_path: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gate, "COMMAND_TIMEOUT_SEC", 1)
    pid_file = tmp_path / "child.pid"
    started = time.monotonic()
    code = run_command(f"sleep 60 & echo $! > {pid_file}; wait", tmp_path, log_path)
    assert code is None
    assert time.monotonic() - started < 15
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(pid, signal.SIGKILL)
        pytest.fail("A child of the command survived the timeout")


def test_commands_do_not_see_tokens_or_the_gate_s_virtualenv(
    tmp_path: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gates run a sub-agent's code outside the sandbox."""
    for name in (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "VIRTUAL_ENV",
        "SOME_SECRET",
    ):
        monkeypatch.setenv(name, "must-not-leak")
    monkeypatch.setenv("HOME", str(tmp_path))
    code = run_command("env > env.txt", tmp_path, log_path)
    assert code == 0
    env = (tmp_path / "env.txt").read_text(encoding="utf-8")
    assert "must-not-leak" not in env
    assert f"HOME={tmp_path}" in env
    assert "PATH=" in env


# G2


def test_run_tests_runs_the_test_command_then_verify(
    tmp_path: Path, log_path: Path
) -> None:
    spec = make_spec(verify="echo verify >> order.txt")
    target = make_target(test_command="echo test >> order.txt")
    assert run_tests(spec, target, tmp_path, log_path) is None
    assert (tmp_path / "order.txt").read_text().split() == ["test", "verify"]


def test_run_tests_stops_at_a_failing_test_command(
    tmp_path: Path, log_path: Path
) -> None:
    spec = make_spec(verify="touch verify_ran")
    target = make_target(test_command="exit 1")
    failure = run_tests(spec, target, tmp_path, log_path)
    assert failure is not None
    assert "exit 1" in failure
    assert "1" in failure
    assert not (tmp_path / "verify_ran").exists()


def test_run_tests_reports_a_failing_verify(tmp_path: Path, log_path: Path) -> None:
    failure = run_tests(make_spec(verify="exit 2"), make_target(), tmp_path, log_path)
    assert failure is not None
    assert "exit 2" in failure


def test_run_tests_reports_a_timeout(
    tmp_path: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gate, "COMMAND_TIMEOUT_SEC", 1)
    target = make_target(test_command="sleep 30")
    failure = run_tests(make_spec(), target, tmp_path, log_path)
    assert failure is not None
    assert "시간 초과" in failure


def test_collecting_no_tests_passes_only_for_tests_none(
    tmp_path: Path, log_path: Path
) -> None:
    target = make_target(test_command="exit 5")
    assert run_tests(make_spec(), target, tmp_path, log_path) is not None
    allowed = make_spec(grade=Grade.L1, tests="none")
    assert run_tests(allowed, target, tmp_path, log_path) is None


def test_verify_exit_five_never_passes(tmp_path: Path, log_path: Path) -> None:
    spec = make_spec(grade=Grade.L1, tests="none", verify="exit 5")
    assert run_tests(spec, make_target(), tmp_path, log_path) is not None


def test_failure_summary_is_limited(tmp_path: Path, log_path: Path) -> None:
    long_command = "exit 1 # " + "x" * 2000
    failure = run_tests(
        make_spec(), make_target(test_command=long_command), tmp_path, log_path
    )
    assert failure is not None
    assert len(failure) <= gate.DETAIL_LIMIT


# All gates


def gates_on(
    worktree: Path, since: str, log_path: Path, spec: TaskSpec, target: Target
) -> GateReport:
    return run_gates(
        spec, target, worktree, since=since, main_ref=MAIN, log_path=log_path
    )


def test_all_gates_pass(worktree: Path, test_sha: str, log_path: Path) -> None:
    write(worktree / "src" / "ok.py")
    report = gates_on(worktree, test_sha, log_path, make_spec(), make_target())
    assert report == GateReport(
        passed=True,
        gates={
            "G1": GateStatus.PASSED,
            "G2": GateStatus.PASSED,
            "G3": GateStatus.PASSED,
        },
        log_path=str(log_path),
    )


def test_g1_failure_stops_before_any_command_runs(
    worktree: Path, test_sha: str, log_path: Path
) -> None:
    write(worktree / "outside.py")
    target = make_target(test_command="touch src_ran")
    report = gates_on(worktree, test_sha, log_path, make_spec(), target)
    assert report.passed is False
    assert report.failed_gate == "G1"
    assert report.gates == {
        "G1": GateStatus.FAILED,
        "G2": GateStatus.NOT_RUN,
        "G3": GateStatus.NOT_RUN,
    }
    assert report.detail is not None
    assert "outside.py" in report.detail
    assert "out_of_scope" in report.detail
    assert not (worktree / "src_ran").exists()


def test_g2_failure_stops_before_g3(
    worktree: Path, test_sha: str, log_path: Path
) -> None:
    target = make_target(test_command="exit 1", lint_command="touch lint_ran")
    report = gates_on(worktree, test_sha, log_path, make_spec(), target)
    assert report.failed_gate == "G2"
    assert report.gates == {
        "G1": GateStatus.PASSED,
        "G2": GateStatus.FAILED,
        "G3": GateStatus.NOT_RUN,
    }
    assert not (worktree / "lint_ran").exists()


def test_g3_failure_is_reported(worktree: Path, test_sha: str, log_path: Path) -> None:
    target = make_target(lint_command="exit 1")
    report = gates_on(worktree, test_sha, log_path, make_spec(), target)
    assert report.failed_gate == "G3"
    assert report.gates["G3"] is GateStatus.FAILED
    assert report.log_path == str(log_path)


@pytest.mark.parametrize("lint", ["", "   "])
def test_empty_lint_command_skips_g3(
    worktree: Path, test_sha: str, log_path: Path, lint: str
) -> None:
    target = make_target(lint_command=lint)
    report = gates_on(worktree, test_sha, log_path, make_spec(), target)
    assert report.passed is True
    assert report.gates["G3"] is GateStatus.SKIPPED


def test_a_new_run_starts_a_new_log(
    worktree: Path, test_sha: str, log_path: Path
) -> None:
    log_path.parent.mkdir(parents=True)
    log_path.write_text("old run\n", encoding="utf-8")
    gates_on(worktree, test_sha, log_path, make_spec(), make_target())
    assert "old run" not in log_path.read_text(encoding="utf-8")


def test_long_detail_is_limited(worktree: Path, test_sha: str, log_path: Path) -> None:
    for index in range(100):
        write(worktree / f"outside_{index:03}_{'x' * 40}.py")
    report = gates_on(worktree, test_sha, log_path, make_spec(), make_target())
    assert report.detail is not None
    assert len(report.detail) <= gate.DETAIL_LIMIT


# Which grades get reviewed


@pytest.mark.parametrize(
    ("grade", "expected"),
    [(Grade.L1, False), (Grade.L2, False), (Grade.L3, True), (Grade.L3R, True)],
)
def test_review_required(grade: Grade, expected: bool) -> None:
    assert review_required(grade) is expected


@pytest.mark.parametrize(
    ("grade", "flag", "expected"),
    [
        (Grade.L1, False, False),
        (Grade.L2, False, False),
        (Grade.L1, True, True),
        (Grade.L2, True, True),
        (Grade.L3, False, True),
        (Grade.L3R, False, True),
        (Grade.L3, True, True),
        (Grade.L3R, True, True),
    ],
)
def test_the_review_flag_can_only_add_a_review(
    grade: Grade, flag: bool, expected: bool
) -> None:
    assert review_required(grade, flag=flag) is expected


def test_the_review_flag_is_keyword_only() -> None:
    with pytest.raises(TypeError):
        review_required(Grade.L2, True)  # type: ignore[misc]


# The orch gate flow


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return connect(tmp_path / "state.db")


def add_judged_task(
    conn: sqlite3.Connection,
    worktree: Path | None,
    test_sha: str | None,
    *,
    grade: Grade = Grade.L2,
    state: TaskState = TaskState.JUDGING,
    outcome: Outcome | None = Outcome.DONE,
    attempt: int = 1,
) -> None:
    insert_task(
        conn,
        Task(
            id="feat-x",
            target="sample",
            title="Add feature X",
            grade=grade,
            lane=Lane.B,
            model="m",
            state=state,
            attempt=attempt,
            timeout_sec=60,
            test_sha=test_sha,
            worktree=str(worktree) if worktree is not None else None,
            created_at="2026-11-04T00:00:00+00:00",
            updated_at="2026-11-04T00:00:00+00:00",
        ),
    )
    if outcome is not None:
        run_id = insert_run(
            conn, Run(task_id="feat-x", attempt=attempt, lane=Lane.B, model="m")
        )
        finish_run(conn, run_id, ended_at="t", exit_code=0, outcome=outcome)


def run_flow(
    conn: sqlite3.Connection,
    log_path: Path,
    target: Target | None = None,
    spec: TaskSpec | None = None,
) -> GateReport:
    return gate_task(
        conn,
        spec if spec is not None else make_spec(),
        target if target is not None else make_target(),
        main_ref=MAIN,
        log_path=log_path,
        **REDACT,
    )


def state_of(conn: sqlite3.Connection) -> TaskState:
    task = get_task(conn, "feat-x")
    assert task is not None
    return task.state


def recorded(conn: sqlite3.Connection, attempt: int = 1) -> dict[Gate, bool]:
    return {r.gate: r.passed for r in gate_results(conn, "feat-x", attempt)}


def states_passed(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT from_state, to_state FROM events WHERE task_id = 'feat-x' ORDER BY id"
    )
    return list(rows)


def test_passing_l2_task_is_committed_and_ready(
    conn: sqlite3.Connection, worktree: Path, test_sha: str, log_path: Path
) -> None:
    add_judged_task(conn, worktree, test_sha)
    write(worktree / "src" / "new.py")
    write(worktree / ".orch-result.json", "{}\n")
    report = run_flow(conn, log_path)
    assert report.passed is True
    assert state_of(conn) is TaskState.READY
    assert states_passed(conn) == [("judging", "gating"), ("gating", "ready")]
    assert recorded(conn) == {Gate.G1: True, Gate.G2: True, Gate.G3: True}
    assert git(worktree, "status", "--porcelain") == ""
    assert git(worktree, "log", "-1", "--format=%s") == "Add feature X"
    assert git(worktree, "show", "--name-only", "--format=", "HEAD") == "src/new.py"
    assert git(worktree, "rev-parse", "HEAD~1") == test_sha


@pytest.mark.parametrize("grade", [Grade.L3, Grade.L3R])
def test_passing_higher_grade_goes_to_review(
    conn: sqlite3.Connection,
    worktree: Path,
    test_sha: str,
    log_path: Path,
    grade: Grade,
) -> None:
    add_judged_task(conn, worktree, test_sha, grade=grade)
    write(worktree / "src" / "new.py")
    assert run_flow(conn, log_path).passed is True
    assert state_of(conn) is TaskState.REVIEWING


def test_commit_includes_edits_and_deletions(
    conn: sqlite3.Connection, worktree: Path, log_path: Path
) -> None:
    add_commit(worktree, "src/edit.py")
    add_commit(worktree, "src/gone.py")
    test_sha = add_commit(worktree, "tests/test_x.py")
    add_judged_task(conn, worktree, test_sha)
    write(worktree / "src" / "edit.py", "edited\n")
    (worktree / "src" / "gone.py").unlink()
    assert run_flow(conn, log_path).passed is True
    names = git(worktree, "show", "--name-only", "--format=", "HEAD").split()
    assert names == ["src/edit.py", "src/gone.py"]


def test_passing_task_with_no_changes_makes_no_commit(
    conn: sqlite3.Connection, worktree: Path, test_sha: str, log_path: Path
) -> None:
    add_judged_task(conn, worktree, test_sha)
    assert run_flow(conn, log_path).passed is True
    assert git(worktree, "rev-parse", "HEAD") == test_sha
    assert state_of(conn) is TaskState.READY


def gated_of(conn: sqlite3.Connection) -> str | None:
    task = get_task(conn, "feat-x")
    assert task is not None
    return task.gated_sha


def test_a_passing_gate_records_the_committed_head_as_gated(
    conn: sqlite3.Connection, worktree: Path, test_sha: str, log_path: Path
) -> None:
    add_judged_task(conn, worktree, test_sha)
    write(worktree / "src" / "new.py")
    assert run_flow(conn, log_path).passed is True
    head = git(worktree, "rev-parse", "HEAD")
    assert head != test_sha
    assert gated_of(conn) == head


@pytest.mark.parametrize("grade", [Grade.L3, Grade.L3R])
def test_a_passing_gate_before_review_records_the_gated_head(
    conn: sqlite3.Connection,
    worktree: Path,
    test_sha: str,
    log_path: Path,
    grade: Grade,
) -> None:
    add_judged_task(conn, worktree, test_sha, grade=grade)
    write(worktree / "src" / "new.py")
    assert run_flow(conn, log_path).passed is True
    assert state_of(conn) is TaskState.REVIEWING
    assert gated_of(conn) == git(worktree, "rev-parse", "HEAD")


def test_a_passing_gate_without_changes_records_the_test_commit_as_gated(
    conn: sqlite3.Connection, worktree: Path, test_sha: str, log_path: Path
) -> None:
    add_judged_task(conn, worktree, test_sha)
    assert run_flow(conn, log_path).passed is True
    assert gated_of(conn) == test_sha


def test_a_failing_gate_leaves_the_gated_commit_alone(
    conn: sqlite3.Connection, worktree: Path, test_sha: str, log_path: Path
) -> None:
    add_judged_task(conn, worktree, test_sha)
    update_task(conn, "feat-x", gated_sha="f" * 40)
    write(worktree / "src" / "new.py")
    report = run_flow(conn, log_path, make_target(test_command="exit 1"))
    assert report.passed is False
    assert gated_of(conn) == "f" * 40


def test_failing_task_returns_to_judging_without_a_commit(
    conn: sqlite3.Connection, worktree: Path, test_sha: str, log_path: Path
) -> None:
    add_judged_task(conn, worktree, test_sha)
    write(worktree / "src" / "new.py")
    report = run_flow(conn, log_path, make_target(test_command="exit 1"))
    assert report.passed is False
    assert report.failed_gate == "G2"
    assert state_of(conn) is TaskState.JUDGING
    assert states_passed(conn) == [("judging", "gating"), ("gating", "judging")]
    assert recorded(conn) == {Gate.G1: True, Gate.G2: False}
    assert git(worktree, "rev-parse", "HEAD") == test_sha
    assert (worktree / "src" / "new.py").exists()


def test_failure_detail_is_recorded(
    conn: sqlite3.Connection, worktree: Path, test_sha: str, log_path: Path
) -> None:
    add_judged_task(conn, worktree, test_sha)
    write(worktree / "outside.py")
    report = run_flow(conn, log_path)
    assert report.failed_gate == "G1"
    result = gate_results(conn, "feat-x", 1)[0]
    assert result.detail == report.detail


def test_results_are_recorded_for_the_current_attempt(
    conn: sqlite3.Connection, worktree: Path, test_sha: str, log_path: Path
) -> None:
    add_judged_task(conn, worktree, test_sha, attempt=2)
    run_flow(conn, log_path)
    assert recorded(conn, 2) == {Gate.G1: True, Gate.G2: True, Gate.G3: True}
    assert recorded(conn, 1) == {}


def test_a_skipped_g3_is_recorded_as_passed_with_a_note(
    conn: sqlite3.Connection, worktree: Path, test_sha: str, log_path: Path
) -> None:
    add_judged_task(conn, worktree, test_sha)
    run_flow(conn, log_path, make_target(lint_command=""))
    result = {r.gate: r for r in gate_results(conn, "feat-x", 1)}[Gate.G3]
    assert result.passed is True
    assert result.detail == "skipped"


@pytest.mark.parametrize(
    "state", [TaskState.PREPARED, TaskState.RUNNING, TaskState.READY]
)
def test_only_a_judged_task_can_be_gated(
    conn: sqlite3.Connection,
    worktree: Path,
    test_sha: str,
    log_path: Path,
    state: TaskState,
) -> None:
    add_judged_task(conn, worktree, test_sha, state=state)
    with pytest.raises(GateError):
        run_flow(conn, log_path)
    assert state_of(conn) is state
    assert recorded(conn) == {}


@pytest.mark.parametrize(
    "outcome", [Outcome.FAILED, Outcome.PARTIAL, Outcome.BLOCKED, None]
)
def test_only_a_done_run_can_be_gated(
    conn: sqlite3.Connection,
    worktree: Path,
    test_sha: str,
    log_path: Path,
    outcome: Outcome | None,
) -> None:
    add_judged_task(conn, worktree, test_sha, outcome=outcome)
    with pytest.raises(GateError):
        run_flow(conn, log_path)
    assert state_of(conn) is TaskState.JUDGING


def test_task_without_a_worktree_or_test_commit_is_refused(
    conn: sqlite3.Connection, worktree: Path, test_sha: str, log_path: Path
) -> None:
    add_judged_task(conn, None, test_sha)
    with pytest.raises(GateError):
        run_flow(conn, log_path)
    assert state_of(conn) is TaskState.JUDGING
    conn.execute("UPDATE tasks SET worktree = ?, test_sha = NULL", (str(worktree),))
    with pytest.raises(GateError):
        run_flow(conn, log_path)
    assert state_of(conn) is TaskState.JUDGING


def test_missing_task_is_refused(conn: sqlite3.Connection, log_path: Path) -> None:
    with pytest.raises(GateError):
        run_flow(conn, log_path)


def test_a_commit_failure_returns_to_judging_and_raises(
    conn: sqlite3.Connection,
    worktree: Path,
    test_sha: str,
    log_path: Path,
    repo: Path,
) -> None:
    add_judged_task(conn, worktree, test_sha)
    write(worktree / "src" / "new.py")
    hook = repo / ".git" / "hooks" / "pre-commit"
    write(hook, "#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    with pytest.raises(GitError):
        run_flow(conn, log_path)
    assert state_of(conn) is TaskState.JUDGING


def test_a_git_failure_during_the_gates_returns_to_judging_and_raises(
    conn: sqlite3.Connection, worktree: Path, log_path: Path
) -> None:
    add_judged_task(conn, worktree, "0" * 40)
    with pytest.raises(GitError):
        run_flow(conn, log_path)
    assert state_of(conn) is TaskState.JUDGING


def test_no_database_transaction_stays_open_while_commands_run(
    conn: sqlite3.Connection,
    worktree: Path,
    test_sha: str,
    log_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_judged_task(conn, worktree, test_sha)
    seen: list[bool] = []
    real = gate.run_command

    def spy(*args: object, **kwargs: object) -> int | None:
        seen.append(conn.in_transaction)
        return real(*args, **kwargs)

    monkeypatch.setattr(gate, "run_command", spy)
    run_flow(conn, log_path)
    assert seen
    assert not any(seen)
    assert latest_run(conn, "feat-x") is not None


# The spec's review flag


@pytest.mark.parametrize("grade", [Grade.L1, Grade.L2])
def test_the_spec_flag_sends_a_lower_grade_to_review(
    conn: sqlite3.Connection,
    worktree: Path,
    test_sha: str,
    log_path: Path,
    grade: Grade,
) -> None:
    add_judged_task(conn, worktree, test_sha, grade=grade)
    assert run_flow(conn, log_path, spec=make_spec(review=True)).passed is True
    assert state_of(conn) is TaskState.REVIEWING


@pytest.mark.parametrize("grade", [Grade.L3, Grade.L3R])
def test_the_spec_flag_cannot_turn_off_a_required_review(
    conn: sqlite3.Connection,
    worktree: Path,
    test_sha: str,
    log_path: Path,
    grade: Grade,
) -> None:
    add_judged_task(conn, worktree, test_sha, grade=grade)
    assert run_flow(conn, log_path, spec=make_spec(review=False)).passed is True
    assert state_of(conn) is TaskState.REVIEWING


# Interrupts and failed recovery


@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit])
def test_an_interrupt_during_the_gates_returns_to_judging_and_propagates(
    conn: sqlite3.Connection,
    worktree: Path,
    test_sha: str,
    log_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: type[BaseException],
) -> None:
    def fail(*args: object, **options: object) -> GateReport:
        raise error

    add_judged_task(conn, worktree, test_sha)
    monkeypatch.setattr(gate, "run_gates", fail)
    with pytest.raises(error):
        run_flow(conn, log_path)
    assert state_of(conn) is TaskState.JUDGING
    assert states_passed(conn)[-1] == ("gating", "judging")


def test_an_interrupt_during_the_commit_returns_to_judging_and_propagates(
    conn: sqlite3.Connection,
    worktree: Path,
    test_sha: str,
    log_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **options: object) -> str:
        raise KeyboardInterrupt

    add_judged_task(conn, worktree, test_sha)
    write(worktree / "src" / "new.py")
    monkeypatch.setattr(gitops, "commit", fail)
    with pytest.raises(KeyboardInterrupt):
        run_flow(conn, log_path)
    assert state_of(conn) is TaskState.JUDGING


@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt])
def test_a_failing_recovery_does_not_hide_the_original_exception(
    conn: sqlite3.Connection,
    worktree: Path,
    test_sha: str,
    log_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: type[BaseException],
) -> None:
    real = store.transition

    def fail(*args: object, **options: object) -> GateReport:
        raise error("original")

    def transition(
        conn: sqlite3.Connection,
        task_id: str,
        from_state: TaskState,
        to_state: TaskState,
        **options: object,
    ) -> None:
        if from_state is TaskState.GATING and to_state is TaskState.JUDGING:
            raise sqlite3.OperationalError("disk I/O error")
        real(conn, task_id, from_state, to_state, **options)

    add_judged_task(conn, worktree, test_sha)
    monkeypatch.setattr(gate, "run_gates", fail)
    monkeypatch.setattr(store, "transition", transition)
    with pytest.raises(error) as caught:
        run_flow(conn, log_path)
    assert "original" in str(caught.value)
    assert "disk I/O error" in "\n".join(caught.value.__notes__)
    assert state_of(conn) is TaskState.GATING


def test_an_exception_from_run_command_returns_to_judging_and_raises(
    conn: sqlite3.Connection,
    worktree: Path,
    test_sha: str,
    log_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **kwargs: object) -> int | None:
        raise OSError("cannot start")

    add_judged_task(conn, worktree, test_sha)
    monkeypatch.setattr(gate, "run_command", fail)
    with pytest.raises(OSError, match="cannot start"):
        run_flow(conn, log_path)
    assert state_of(conn) is TaskState.JUDGING
    assert recorded(conn) == {}


def test_an_interrupt_in_run_command_stops_the_group(
    tmp_path: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "late.txt"
    real_wait = subprocess.Popen.wait

    def interrupted(self: subprocess.Popen[bytes], timeout: float | None = None) -> int:
        if timeout is not None:
            raise KeyboardInterrupt
        return real_wait(self)

    monkeypatch.setattr(subprocess.Popen, "wait", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run_command(f"sleep 1; echo x > '{marker}'", tmp_path, log_path)
    time.sleep(1.5)
    assert not marker.exists()


# Rerunning the gates in the same attempt


def test_a_rerun_clears_results_of_gates_that_did_not_run(
    conn: sqlite3.Connection, worktree: Path, test_sha: str, log_path: Path
) -> None:
    add_judged_task(conn, worktree, test_sha)
    run_flow(conn, log_path, make_target(test_command="exit 1"))
    assert recorded(conn) == {Gate.G1: True, Gate.G2: False}
    write(worktree / "outside.py")
    run_flow(conn, log_path)
    assert recorded(conn) == {Gate.G1: False}


def test_a_rerun_keeps_the_pre_pr_gate_and_other_attempts(
    conn: sqlite3.Connection, worktree: Path, test_sha: str, log_path: Path
) -> None:
    add_judged_task(conn, worktree, test_sha, attempt=2)
    store.record_gate_result(conn, "feat-x", 1, Gate.G3, passed=True)
    store.record_gate_result(conn, "feat-x", 2, Gate.G2_PRE_PR, passed=False)
    store.record_gate_result(conn, "feat-x", 2, Gate.G3, passed=True)
    run_flow(conn, log_path, make_target(test_command="exit 1"))
    assert recorded(conn, 2) == {Gate.G2_PRE_PR: False, Gate.G1: True, Gate.G2: False}
    assert recorded(conn, 1) == {Gate.G3: True}


def test_an_exception_during_a_rerun_leaves_no_old_results(
    conn: sqlite3.Connection,
    worktree: Path,
    test_sha: str,
    log_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **options: object) -> GateReport:
        raise RuntimeError("boom")

    add_judged_task(conn, worktree, test_sha)
    run_flow(conn, log_path, make_target(test_command="exit 1"))
    monkeypatch.setattr(gate, "run_gates", fail)
    with pytest.raises(RuntimeError):
        run_flow(conn, log_path)
    assert recorded(conn) == {}
    assert state_of(conn) is TaskState.JUDGING


# Changes made while the gates run


COMMIT_OUTSIDE = (
    "echo x > outside.py; git add outside.py; "
    "git -c user.name=t -c user.email=t@example.com commit -qm sneak"
)


@pytest.mark.parametrize("stage", ["test_command", "verify", "lint_command"])
@pytest.mark.parametrize(
    ("command", "path"),
    [
        ("echo x > outside.py", "outside.py"),
        ("echo x > tests/evil.py", "tests/evil.py"),
        ("echo x >> tests/test_x.py", "tests/test_x.py"),
        ("echo x > shared.toml", "shared.toml"),
        (COMMIT_OUTSIDE, "outside.py"),
    ],
)
def test_scope_escape_during_a_gate_fails_g1_without_a_commit(
    conn: sqlite3.Connection,
    worktree: Path,
    test_sha: str,
    log_path: Path,
    stage: str,
    command: str,
    path: str,
) -> None:
    add_judged_task(conn, worktree, test_sha)
    write(worktree / "src" / "new.py")
    target_values: dict[str, object] = {"shared_paths": ("shared.toml",)}
    spec_values: dict[str, object] = {}
    (spec_values if stage == "verify" else target_values)[stage] = command
    report = gate_task(
        conn,
        make_spec(**spec_values),
        make_target(**target_values),
        main_ref=MAIN,
        log_path=log_path,
        **REDACT,
    )
    assert report.passed is False
    assert report.failed_gate == "G1"
    assert report.gates == {
        "G1": GateStatus.FAILED,
        "G2": GateStatus.PASSED,
        "G3": GateStatus.PASSED,
    }
    assert report.detail is not None
    assert path in report.detail
    assert state_of(conn) is TaskState.JUDGING
    assert recorded(conn) == {Gate.G1: False, Gate.G2: True, Gate.G3: True}
    assert "Add feature X" not in git(worktree, "log", "--format=%s")


def test_in_scope_file_made_during_a_gate_is_committed(
    conn: sqlite3.Connection, worktree: Path, test_sha: str, log_path: Path
) -> None:
    add_judged_task(conn, worktree, test_sha)
    write(worktree / "src" / "new.py")
    report = run_flow(
        conn,
        log_path,
        make_target(
            test_command="echo x > src/generated.py; echo x > .orch-result.json"
        ),
    )
    assert report.passed is True
    assert state_of(conn) is TaskState.READY
    committed = git(worktree, "show", "--name-only", "--format=", "HEAD").split()
    assert committed == ["src/generated.py", "src/new.py"]


def test_run_gates_reports_a_scope_escape(
    worktree: Path, test_sha: str, log_path: Path
) -> None:
    report = run_gates(
        make_spec(),
        make_target(test_command="echo x > outside.py"),
        worktree,
        since=test_sha,
        main_ref=MAIN,
        log_path=log_path,
    )
    assert report.passed is False
    assert report.failed_gate == "G1"
    assert report.gates["G1"] is GateStatus.FAILED
    assert report.gates["G2"] is GateStatus.PASSED


def test_the_scope_is_checked_again_when_g3_is_skipped(
    worktree: Path, test_sha: str, log_path: Path
) -> None:
    report = run_gates(
        make_spec(),
        make_target(test_command="echo x > outside.py", lint_command=""),
        worktree,
        since=test_sha,
        main_ref=MAIN,
        log_path=log_path,
    )
    assert report.passed is False
    assert report.failed_gate == "G1"
    assert report.gates["G3"] is GateStatus.SKIPPED


def test_a_failing_gate_keeps_its_own_failure_over_a_scope_escape(
    worktree: Path, test_sha: str, log_path: Path
) -> None:
    report = run_gates(
        make_spec(),
        make_target(test_command="echo x > outside.py; exit 1"),
        worktree,
        since=test_sha,
        main_ref=MAIN,
        log_path=log_path,
    )
    assert report.failed_gate == "G2"
    assert report.gates == {
        "G1": GateStatus.PASSED,
        "G2": GateStatus.FAILED,
        "G3": GateStatus.NOT_RUN,
    }


@pytest.mark.parametrize("code", [0, 3])
def test_run_command_stops_children_left_after_exit(
    tmp_path: Path, log_path: Path, code: int
) -> None:
    marker = tmp_path / "late.txt"
    command = f"(sleep 1; echo x > '{marker}') & exit {code}"
    assert run_command(command, tmp_path, log_path) == code
    time.sleep(1.5)
    assert not marker.exists()


# Conventions


def test_module_runs_git_only_through_gitops() -> None:
    tree = ast.parse(Path(gate.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module is not None
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Constant) and node.value == "git":
            pytest.fail("gate.py must not name the git command")
    assert imported - {"orch"} <= sys.stdlib_module_names


# The configured time limit


def test_an_explicit_limit_overrides_the_default(
    tmp_path: Path, log_path: Path
) -> None:
    started = time.monotonic()
    assert run_command("sleep 30", tmp_path, log_path, timeout_sec=1) is None
    assert time.monotonic() - started < 15


def test_an_explicit_limit_can_be_longer_than_the_default(
    tmp_path: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gate, "COMMAND_TIMEOUT_SEC", 1)
    assert run_command("sleep 2", tmp_path, log_path, timeout_sec=30) == 0


def test_no_explicit_limit_reads_the_default_at_call_time(
    tmp_path: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gate, "COMMAND_TIMEOUT_SEC", 1)
    assert run_command("sleep 30", tmp_path, log_path, timeout_sec=None) is None


def record_limits(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, object]]:
    calls: list[tuple[str, object]] = []
    original = gate.run_command

    def spy(
        command: str, worktree: Path, log_path: Path, **kwargs: object
    ) -> int | None:
        calls.append((command, kwargs.get("timeout_sec")))
        return original(command, worktree, log_path, **kwargs)

    monkeypatch.setattr(gate, "run_command", spy)
    return calls


def test_run_tests_passes_the_limit_to_both_commands(
    tmp_path: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = record_limits(monkeypatch)
    spec = make_spec(verify="echo verify")
    target = make_target(test_command="echo tests")
    assert run_tests(spec, target, tmp_path, log_path, timeout_sec=77) is None
    assert calls == [("echo tests", 77), ("echo verify", 77)]


def test_run_tests_reports_a_timeout_from_the_explicit_limit(
    tmp_path: Path, log_path: Path
) -> None:
    target = make_target(test_command="sleep 30")
    failure = run_tests(make_spec(), target, tmp_path, log_path, timeout_sec=1)
    assert failure is not None
    assert "시간 초과" in failure


def test_run_gates_passes_the_limit_to_g2_and_g3(
    worktree: Path, test_sha: str, log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = record_limits(monkeypatch)
    spec = make_spec(verify="echo verify")
    target = make_target(test_command="echo tests", lint_command="echo lint")
    report = run_gates(
        spec,
        target,
        worktree,
        since=test_sha,
        main_ref=MAIN,
        log_path=log_path,
        timeout_sec=88,
    )
    assert report.passed
    assert calls == [("echo tests", 88), ("echo verify", 88), ("echo lint", 88)]


def test_run_gates_times_out_g3_on_the_explicit_limit(
    worktree: Path, test_sha: str, log_path: Path
) -> None:
    target = make_target(lint_command="sleep 30")
    report = run_gates(
        make_spec(),
        target,
        worktree,
        since=test_sha,
        main_ref=MAIN,
        log_path=log_path,
        timeout_sec=1,
    )
    assert report.failed_gate == "G3"
    assert report.detail is not None
    assert "시간 초과" in report.detail


def test_gate_task_passes_the_limit_to_run_gates(
    conn: sqlite3.Connection,
    worktree: Path,
    test_sha: str,
    log_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_judged_task(conn, worktree, test_sha)
    calls = record_limits(monkeypatch)
    report = gate_task(
        conn,
        make_spec(),
        make_target(),
        main_ref=MAIN,
        log_path=log_path,
        timeout_sec=99,
        **REDACT,
    )
    assert report.passed
    assert [limit for _, limit in calls] == [99, 99, 99]


def test_gate_task_without_a_limit_uses_the_default(
    conn: sqlite3.Connection,
    worktree: Path,
    test_sha: str,
    log_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_judged_task(conn, worktree, test_sha)
    monkeypatch.setattr(gate, "COMMAND_TIMEOUT_SEC", 1)
    report = run_flow(conn, log_path, target=make_target(test_command="sleep 30"))
    assert report.failed_gate == "G2"
    assert report.detail is not None
    assert "시간 초과" in report.detail
