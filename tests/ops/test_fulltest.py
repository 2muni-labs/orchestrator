"""orch fulltest: the daily full test run on the default branch tip."""

import fcntl
import io
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from orch import config, fulltest, gate, gitops, notify, sandbox, store
from orch.cli import main
from orch.config import Target, TargetPaths, target_paths
from orch.fulltest import FulltestOutcome, FulltestReport, run_fulltest
from orch.models import Grade, Lane, Task, TaskState
from transitions import check_call

NAME = "sample"
NOW = datetime(2026, 11, 23, 3, 0, 5, tzinfo=UTC)
STAMP = "20261123T030005Z"
LATER = NOW + timedelta(seconds=125)
TIMEOUT = 1800
SETUP = "setup-cmd --frozen"
TEST = "test-cmd -q"


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


@pytest.fixture(autouse=True)
def isolated_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(
        "[user]\n\tname = t\n\temail = t@example.com\n[init]\n\tdefaultBranch = main\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    path = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True)
    return path


@pytest.fixture
def repo(tmp_path: Path, remote: Path) -> Path:
    path = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "base.txt").write_text("x\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-qm", "base")
    git(path, "remote", "add", "origin", str(remote))
    git(path, "push", "-q", "origin", "main")
    git(path, "fetch", "-q", "origin")
    return path


def advance_remote(tmp_path: Path, remote: Path) -> str:
    """Push a new commit to the remote from another clone and return its id."""
    clone = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True)
    (clone / "new.txt").write_text("y\n", encoding="utf-8")
    git(clone, "add", ".")
    git(clone, "commit", "-qm", "new")
    git(clone, "push", "-q", "origin", "main")
    return git(clone, "rev-parse", "HEAD")


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = store.connect(tmp_path / "state.db")
    yield connection
    connection.close()


@pytest.fixture
def paths(tmp_path: Path) -> TargetPaths:
    return target_paths(tmp_path / "home" / ".orch", NAME)


def make_target(repo: Path, name: str = NAME) -> Target:
    return Target(
        name=name,
        path=repo,
        github="o/r",
        default_branch="main",
        setup_command=SETUP,
        test_command=TEST,
        lint_command="true",
    )


def add_task(
    conn: sqlite3.Connection, task_id: str, state: TaskState, target: str = "other"
) -> None:
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
            updated_at="2026-01-01T00:00:00+00:00",
        ),
    )


class Clock:
    """Return NOW on the first call and LATER on every call after it."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        return NOW if self.calls == 1 else LATER


Result = int | None | BaseException


class Boom(Exception):
    """An exception fulltest does not expect; not a RuntimeError."""


class FakeCommands:
    """Stand in for gate.run_command and record what each call saw."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.results: dict[str, Result] = {SETUP: 0, TEST: 0}
        self.output: dict[str, str] = {}
        self.calls: list[dict[str, object]] = []
        monkeypatch.setattr(gate, "run_command", self)

    def __call__(
        self,
        command: str,
        worktree: Path,
        log_path: Path,
        *,
        timeout_sec: int | None = None,
        sandbox: bool = True,
    ) -> int | None:
        head = git(worktree, "rev-parse", "HEAD")
        branch = git(worktree, "rev-parse", "--abbrev-ref", "HEAD")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"$ {command}\n{self.output.get(command, '')}")
        self.calls.append(
            {
                "command": command,
                "worktree": worktree,
                "log_path": log_path,
                "timeout_sec": timeout_sec,
                "sandbox": sandbox,
                "head": head,
                "branch": branch,
            }
        )
        result = self.results[command]
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.fixture
def commands(monkeypatch: pytest.MonkeyPatch) -> FakeCommands:
    return FakeCommands(monkeypatch)


def run(conn: sqlite3.Connection, repo: Path, paths: TargetPaths) -> FulltestReport:
    return run_fulltest(
        conn, make_target(repo), paths, timeout_sec=TIMEOUT, now=Clock()
    )


def worktree_of(paths: TargetPaths) -> Path:
    return paths.root / "fulltest" / f"wt-{STAMP}"


def log_of(paths: TargetPaths) -> Path:
    return paths.logs / f"fulltest.{STAMP}.log"


def last(paths: TargetPaths) -> dict[str, object]:
    return json.loads((paths.root / "fulltest" / "last.json").read_text("utf-8"))


def assert_cleaned(repo: Path, paths: TargetPaths, branches: str) -> None:
    assert not worktree_of(paths).exists()
    assert str(worktree_of(paths)) not in git(repo, "worktree", "list")
    assert git(repo, "branch", "--list") == branches
    leftovers = sorted(p.name for p in (paths.root / "fulltest").iterdir())
    assert set(leftovers) <= {"lock", "last.json"}


# A full run


def test_a_passing_run_tests_the_fetched_tip_and_records_it(
    conn: sqlite3.Connection,
    repo: Path,
    remote: Path,
    paths: TargetPaths,
    tmp_path: Path,
    commands: FakeCommands,
) -> None:
    tip = advance_remote(tmp_path, remote)
    assert git(repo, "rev-parse", "origin/main") != tip
    branches = git(repo, "branch", "--list")

    report = run(conn, repo, paths)

    assert report == FulltestReport(
        target=NAME,
        outcome=FulltestOutcome.PASSED,
        commit=tip,
        exit_code=0,
        log_path=str(log_of(paths)),
        started_at=NOW.isoformat(),
        duration_sec=125,
        detail=None,
    )
    assert [(c["command"], c["sandbox"], c["timeout_sec"]) for c in commands.calls] == [
        (SETUP, False, TIMEOUT),
        (TEST, True, TIMEOUT),
    ]
    for call in commands.calls:
        assert call["worktree"] == worktree_of(paths)
        assert call["log_path"] == log_of(paths)
        assert call["head"] == tip
        assert call["branch"] == "HEAD"
    assert log_of(paths).read_text("utf-8") == f"$ {SETUP}\n$ {TEST}\n"
    assert_cleaned(repo, paths, branches)
    assert last(paths) == json.loads(json.dumps(asdict(report)))
    assert last(paths)["outcome"] == "passed"


def test_the_report_values_match_the_design() -> None:
    assert [outcome.value for outcome in FulltestOutcome] == [
        "passed",
        "failed",
        "timeout",
        "setup_failed",
        "sandbox_failed",
        "busy",
        "error",
    ]


@pytest.mark.parametrize(
    ("result", "outcome", "exit_code"),
    [
        (1, FulltestOutcome.FAILED, 1),
        (5, FulltestOutcome.FAILED, 5),
        (None, FulltestOutcome.TIMEOUT, None),
        (sandbox.SandboxError("no start"), FulltestOutcome.SANDBOX_FAILED, None),
    ],
)
def test_a_test_command_that_does_not_pass(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    commands: FakeCommands,
    result: Result,
    outcome: FulltestOutcome,
    exit_code: int | None,
) -> None:
    branches = git(repo, "branch", "--list")
    commands.results[TEST] = result

    report = run(conn, repo, paths)

    assert report.outcome == outcome
    assert report.exit_code == exit_code
    assert report.commit == git(repo, "rev-parse", "origin/main")
    assert report.log_path == str(log_of(paths))
    assert [c["command"] for c in commands.calls] == [SETUP, TEST]
    assert_cleaned(repo, paths, branches)
    assert last(paths)["outcome"] == outcome.value
    assert last(paths)["exit_code"] == exit_code


@pytest.mark.parametrize("result", [1, 127, None])
def test_a_failed_setup_skips_the_tests(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    commands: FakeCommands,
    result: Result,
) -> None:
    branches = git(repo, "branch", "--list")
    commands.results[SETUP] = result

    report = run(conn, repo, paths)

    assert report.outcome == FulltestOutcome.SETUP_FAILED
    assert report.exit_code is None
    assert [c["command"] for c in commands.calls] == [SETUP]
    assert_cleaned(repo, paths, branches)
    assert last(paths)["outcome"] == "setup_failed"


def test_a_skipped_test_does_not_matter(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    commands: FakeCommands,
) -> None:
    commands.output[TEST] = "SKIPPED [1] tests/x.py:1: reason\n1 passed, 1 skipped\n"
    assert run(conn, repo, paths).outcome == FulltestOutcome.PASSED


# Errors and cleanup


def test_a_fetch_failure_is_an_error_without_a_worktree(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    commands: FakeCommands,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing(repo: Path, remote: str = "origin") -> None:
        raise gitops.GitError("fetch failed at /secret/path")

    monkeypatch.setattr(gitops, "fetch", failing)
    report = run(conn, repo, paths)
    assert report.outcome == FulltestOutcome.ERROR
    assert report.detail == "GitError"
    assert commands.calls == []
    assert not worktree_of(paths).exists()
    assert last(paths)["outcome"] == "error"
    assert last(paths)["detail"] == "GitError"


def test_a_missing_default_branch_is_an_error(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    commands: FakeCommands,
) -> None:
    target = Target(**{**asdict(make_target(repo)), "default_branch": "absent"})
    report = run_fulltest(conn, target, paths, timeout_sec=TIMEOUT, now=Clock())
    assert report.outcome == FulltestOutcome.ERROR
    assert report.detail == "GitError"
    assert commands.calls == []


def test_an_os_error_is_an_error_and_still_cleans_up(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    commands: FakeCommands,
) -> None:
    branches = git(repo, "branch", "--list")
    commands.results[TEST] = PermissionError("denied")
    report = run(conn, repo, paths)
    assert report.outcome == FulltestOutcome.ERROR
    assert report.detail == "PermissionError"
    assert_cleaned(repo, paths, branches)


def test_a_cleanup_failure_does_not_change_the_outcome(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    commands: FakeCommands,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing(repo: Path, worktree: Path, *, force: bool = False) -> None:
        raise gitops.GitError("remove failed")

    monkeypatch.setattr(gitops, "remove_worktree", failing)
    report = run(conn, repo, paths)
    assert report.outcome == FulltestOutcome.PASSED
    assert not worktree_of(paths).exists()
    assert last(paths)["outcome"] == "passed"


def test_an_unexpected_exception_still_cleans_up_and_releases_the_lock(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    commands: FakeCommands,
) -> None:
    branches = git(repo, "branch", "--list")
    commands.results[TEST] = Boom("boom")
    with pytest.raises(Boom):
        run(conn, repo, paths)
    assert_cleaned(repo, paths, branches)
    commands.results[TEST] = 0
    assert run(conn, repo, paths).outcome == FulltestOutcome.PASSED


# Busy


def hold_lock(paths: TargetPaths) -> int:
    lock = paths.root / "fulltest" / "lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_a_held_lock_is_busy_and_does_nothing(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    commands: FakeCommands,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched: list[Path] = []
    monkeypatch.setattr(
        gitops, "fetch", lambda repo, remote="origin": fetched.append(repo)
    )
    fd = hold_lock(paths)
    try:
        report = run(conn, repo, paths)
    finally:
        os.close(fd)
    assert report.outcome == FulltestOutcome.BUSY
    assert report.detail == "locked"
    assert report.target == NAME
    assert commands.calls == [] and fetched == []
    assert not (paths.root / "fulltest" / "last.json").exists()


def test_a_gating_task_of_any_target_is_busy(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    commands: FakeCommands,
) -> None:
    add_task(conn, "m-other", TaskState.GATING, target="other")
    previous = paths.root / "fulltest" / "last.json"
    previous.parent.mkdir(parents=True, exist_ok=True)
    previous.write_text('{"outcome": "passed"}', encoding="utf-8")

    report = run(conn, repo, paths)

    assert report.outcome == FulltestOutcome.BUSY
    assert report.detail == "gating"
    assert commands.calls == []
    assert previous.read_text("utf-8") == '{"outcome": "passed"}'
    assert not worktree_of(paths).exists()


@pytest.mark.parametrize(
    "state", [TaskState.RUNNING, TaskState.REVIEWING, TaskState.PR_OPEN]
)
def test_tasks_in_other_states_do_not_block(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    commands: FakeCommands,
    state: TaskState,
) -> None:
    add_task(conn, "m-other", state, target=NAME)
    assert run(conn, repo, paths).outcome == FulltestOutcome.PASSED


def test_the_lock_is_released_after_a_run(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    commands: FakeCommands,
) -> None:
    assert run(conn, repo, paths).outcome == FulltestOutcome.PASSED
    os.close(hold_lock(paths))
    add_task(conn, "m-gate", TaskState.GATING)
    assert run(conn, repo, paths).outcome == FulltestOutcome.BUSY
    os.close(hold_lock(paths))


def test_the_default_clock_is_utc(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    commands: FakeCommands,
) -> None:
    report = run_fulltest(conn, make_target(repo), paths, timeout_sec=TIMEOUT)
    started = datetime.fromisoformat(report.started_at)
    assert started.utcoffset() == timedelta(0)
    assert report.outcome == FulltestOutcome.PASSED


# Command line


ALPHA = "alpha"
BETA = "beta"
USAGE = 64

Call = Callable[..., tuple[int, dict[str, object]]]


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    hub = tmp_path / "hub"
    lines = []
    for name in (BETA, ALPHA):
        (home / name).mkdir(exist_ok=True)
        lines += [
            f"[targets.{name}]",
            f'path = "{home / name}"',
            f'github = "owner/{name}"',
            'default_branch = "main"',
            'setup_command = "true"',
            'test_command = "true"',
            'lint_command = "true"',
            "",
        ]
    write(hub / "targets.toml", "\n".join(lines))
    assert sorted(config.load_targets(hub / "targets.toml")) == [ALPHA, BETA]
    orch_home = home / "runtime"
    write(config.config_path(orch_home), "[gate]\ntimeout_min = 7\n")
    assert config.load_config(orch_home).gate_timeout_min == 7
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ORCH_HOME", str(orch_home))
    monkeypatch.setenv("ORCH_HUB", str(hub))
    for name in ("ORCH_TARGET", "GH_TOKEN", "GITHUB_TOKEN", "ORCH_FAKE_AGENTS"):
        monkeypatch.delenv(name, raising=False)
    return orch_home


@pytest.fixture
def orch(
    env: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> Call:
    def call(*args: str) -> tuple[int, dict[str, object]]:
        check_call(config.state_db_path(env), args)
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        code = main(list(args))
        captured = capsys.readouterr()
        sys.stderr.write(captured.err)
        out = captured.out
        assert out.endswith("\n") and out.count("\n") == 1, out
        return code, json.loads(out)

    return call


def report_for(
    target: str, outcome: FulltestOutcome, **fields: object
) -> FulltestReport:
    values: dict[str, object] = {
        "target": target,
        "outcome": outcome,
        "started_at": NOW.isoformat(),
    }
    values.update(fields)
    return FulltestReport(**values)  # type: ignore[arg-type]


class FakeRun:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[dict[str, object]] = []
        self.outcomes: dict[str, FulltestOutcome] = {}
        self.errors: dict[str, Exception] = {}
        monkeypatch.setattr(fulltest, "run_fulltest", self)

    def __call__(
        self,
        conn: sqlite3.Connection,
        target: Target,
        paths: TargetPaths,
        **kwargs: object,
    ) -> FulltestReport:
        self.calls.append({"conn": conn, "target": target, "paths": paths, **kwargs})
        if target.name in self.errors:
            raise self.errors[target.name]
        outcome = self.outcomes.get(target.name, FulltestOutcome.PASSED)
        return report_for(target.name, outcome)


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch) -> FakeRun:
    return FakeRun(monkeypatch)


class FakeSend:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.error: Exception | None = None
        monkeypatch.setattr(notify, "send", self)

    def __call__(self, event: str, **kwargs: object) -> notify.Outcome:
        self.calls.append((event, kwargs))
        if self.error is not None:
            raise self.error
        return notify.Outcome.SENT


@pytest.fixture
def fake_send(monkeypatch: pytest.MonkeyPatch) -> FakeSend:
    return FakeSend(monkeypatch)


def plain(value: FulltestReport) -> dict[str, object]:
    return json.loads(json.dumps(asdict(value)))


def test_cli_runs_one_target_from_orch_target(
    orch: Call,
    env: Path,
    fake_run: FakeRun,
    fake_send: FakeSend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ORCH_TARGET", ALPHA)
    code, data = orch("fulltest")
    assert (code, data) == (
        0,
        {"ok": True, "results": [plain(report_for(ALPHA, FulltestOutcome.PASSED))]},
    )
    [call] = fake_run.calls
    target = call["target"]
    assert isinstance(target, Target) and target.name == ALPHA
    assert call["paths"] == target_paths(env, ALPHA)
    assert call["timeout_sec"] == 7 * 60
    assert isinstance(call["conn"], sqlite3.Connection)
    assert fake_send.calls == []


def test_cli_runs_one_target_from_the_option(
    orch: Call, fake_run: FakeRun, fake_send: FakeSend
) -> None:
    code, _ = orch("fulltest", "--target", BETA)
    assert code == 0
    assert [c["target"].name for c in fake_run.calls] == [BETA]  # type: ignore[attr-defined]


def test_cli_all_runs_every_target_by_name(
    orch: Call, fake_run: FakeRun, fake_send: FakeSend
) -> None:
    fake_run.outcomes[ALPHA] = FulltestOutcome.BUSY
    code, data = orch("fulltest", "--all")
    assert code == 0
    assert data["ok"] is True
    assert [c["target"].name for c in fake_run.calls] == [ALPHA, BETA]  # type: ignore[attr-defined]
    assert [r["target"] for r in data["results"]] == [ALPHA, BETA]  # type: ignore[index]
    assert [r["outcome"] for r in data["results"]] == ["busy", "passed"]  # type: ignore[index]
    assert len({id(c["conn"]) for c in fake_run.calls}) == 1
    assert fake_send.calls == []


@pytest.mark.parametrize(
    "outcome",
    [
        FulltestOutcome.FAILED,
        FulltestOutcome.TIMEOUT,
        FulltestOutcome.SETUP_FAILED,
        FulltestOutcome.SANDBOX_FAILED,
        FulltestOutcome.ERROR,
    ],
)
def test_cli_notifies_and_exits_20_on_a_result_that_did_not_pass(
    orch: Call,
    env: Path,
    fake_run: FakeRun,
    fake_send: FakeSend,
    outcome: FulltestOutcome,
) -> None:
    fake_run.outcomes[BETA] = outcome
    code, data = orch("fulltest", "--all")
    assert code == 20
    assert data == {
        "ok": False,
        "error": "fulltest_failed",
        "message": "기본 브랜치 전체 테스트가 통과하지 못한 대상이 있습니다.",
        "results": [
            plain(report_for(ALPHA, FulltestOutcome.PASSED)),
            plain(report_for(BETA, outcome)),
        ],
    }
    [(event, kwargs)] = fake_send.calls
    assert event == "fulltest_failed"
    assert kwargs["target"] == BETA
    assert kwargs["orch_home"] == env
    assert kwargs.get("task_id", "") == ""


def test_cli_a_failed_notification_changes_nothing(
    orch: Call, fake_run: FakeRun, fake_send: FakeSend
) -> None:
    fake_run.outcomes[ALPHA] = FulltestOutcome.FAILED
    fake_send.error = RuntimeError("network")
    code, data = orch("fulltest", "--all")
    assert code == 20
    assert data["error"] == "fulltest_failed"
    assert [r["outcome"] for r in data["results"]] == ["failed", "passed"]  # type: ignore[index]
    assert len(fake_run.calls) == 2


def test_cli_all_isolates_an_unexpected_error_per_target(
    orch: Call, env: Path, fake_run: FakeRun, fake_send: FakeSend
) -> None:
    fake_run.errors[ALPHA] = sqlite3.OperationalError("private detail")
    code, data = orch("fulltest", "--all")
    assert code == 20
    assert data == {
        "ok": False,
        "error": "fulltest_failed",
        "message": "기본 브랜치 전체 테스트가 통과하지 못한 대상이 있습니다.",
        "results": [
            {"target": ALPHA, "outcome": "error", "detail": "OperationalError"},
            plain(report_for(BETA, FulltestOutcome.PASSED)),
        ],
    }
    assert [c["target"].name for c in fake_run.calls] == [ALPHA, BETA]  # type: ignore[attr-defined]
    [(event, kwargs)] = fake_send.calls
    assert event == "fulltest_failed"
    assert kwargs["target"] == ALPHA
    assert kwargs["orch_home"] == env
    assert "private" not in json.dumps(data)


def test_cli_all_notifies_every_target_that_raised(
    orch: Call, fake_run: FakeRun, fake_send: FakeSend
) -> None:
    fake_run.errors[ALPHA] = ValueError("x")
    fake_run.errors[BETA] = RuntimeError("y")
    code, data = orch("fulltest", "--all")
    assert code == 20
    assert data["results"] == [  # type: ignore[comparison-overlap]
        {"target": ALPHA, "outcome": "error", "detail": "ValueError"},
        {"target": BETA, "outcome": "error", "detail": "RuntimeError"},
    ]
    assert [kwargs["target"] for _, kwargs in fake_send.calls] == [ALPHA, BETA]


def test_cli_one_target_still_raises_an_unexpected_error(
    orch: Call, fake_run: FakeRun, fake_send: FakeSend
) -> None:
    fake_run.errors[BETA] = RuntimeError("private detail")
    code, data = orch("fulltest", "--target", BETA)
    assert code == 70
    assert data["ok"] is False and data["error"] == "internal"
    assert fake_send.calls == []


@pytest.mark.parametrize(
    ("args", "environ"),
    [
        (("fulltest",), {}),
        (("fulltest", "--all", "--target", ALPHA), {}),
        (("fulltest", "--all"), {"ORCH_TARGET": ALPHA}),
        (("fulltest", "--bogus"), {}),
    ],
)
def test_cli_usage_errors(
    orch: Call,
    fake_run: FakeRun,
    fake_send: FakeSend,
    monkeypatch: pytest.MonkeyPatch,
    args: tuple[str, ...],
    environ: dict[str, str],
) -> None:
    for key, value in environ.items():
        monkeypatch.setenv(key, value)
    code, data = orch(*args)
    assert code == USAGE
    assert data["ok"] is False and data["error"] == "usage"
    assert fake_run.calls == [] and fake_send.calls == []


def test_cli_an_unknown_target_is_refused(
    orch: Call, fake_run: FakeRun, fake_send: FakeSend
) -> None:
    code, data = orch("fulltest", "--target", "gamma")
    assert code == 64
    assert data["ok"] is False
    assert fake_run.calls == []
