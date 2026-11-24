"""review.run_codex_review: a read-only Codex review of a Claude implementation."""
# ruff: noqa: F811 - fixtures imported from test_runner are used as parameters

import dataclasses
import json
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from orch import results
from orch.config import TargetPaths
from orch.models import (
    Finding,
    Grade,
    Lane,
    Review,
    Reviewer,
    Severity,
    Task,
    TaskState,
    Verdict,
)
from orch.redact import redact
from orch.render import build_body
from orch.review import (
    ReviewFlowError,
    ReviewOutcome,
    ReviewRun,
    next_review_log,
    review_prompt,
    run_codex_review,
)
from sandbox_env import FAKE_SANDBOX_EXEC
from test_runner import (  # noqa: F401 - fixtures used by name
    HUB,
    MAIN,
    TASK,
    add_task,
    conn,
    git,
    home,
    isolated_git,
    make_config,
    make_env,
    make_target,
    paths,
    repo,
    task_of,
    write,
)

MODEL = "gpt-6.1-sol"


def reviewing_task(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    tmp_path: Path,
    *,
    lane: Lane = Lane.C,
    state: TaskState = TaskState.REVIEWING,
    timeout_sec: int = 60,
) -> Path:
    worktree = add_task(
        conn,
        repo,
        paths,
        tmp_path / "worktrees",
        lane=lane,
        state=state,
        timeout_sec=timeout_sec,
    )
    write(worktree / "src" / "feature.py", "VALUE = 'implemented'\n")
    git(worktree, "add", ".")
    git(worktree, "commit", "-qm", "implement")
    return worktree


@pytest.fixture
def worktree(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths, tmp_path: Path
) -> Path:
    return reviewing_task(conn, repo, paths, tmp_path)


def review(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    scenario: str,
    *,
    timeout_sec: int | None = None,
    **extra: str,
) -> ReviewRun:
    return run_codex_review(
        conn,
        TASK,
        target=make_target(repo),
        config=make_config(),
        paths=paths,
        environ=make_env(home, scenario, **extra),
        model=MODEL,
        effort="high",
        home=home,
        orch_home=home / ".orch",
        timeout_sec=timeout_sec,
    )


# Refusals write nothing and change nothing


def assert_untouched(paths: TargetPaths, conn: sqlite3.Connection) -> None:
    assert not (paths.reviews / f"{TASK}.diff").exists()
    assert not (paths.logs / "review").exists()
    assert not paths.review_path(TASK).exists()


def test_a_missing_task_is_refused(
    conn: sqlite3.Connection, repo: Path, paths: TargetPaths, home: Path
) -> None:
    with pytest.raises(ReviewFlowError):
        review(conn, repo, paths, home, "review_pass")
    assert_untouched(paths, conn)


def test_a_task_that_is_not_reviewing_is_refused(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    tmp_path: Path,
) -> None:
    reviewing_task(conn, repo, paths, tmp_path, state=TaskState.JUDGING)
    with pytest.raises(ReviewFlowError):
        review(conn, repo, paths, home, "review_pass")
    assert task_of(conn).state is TaskState.JUDGING
    assert_untouched(paths, conn)


def test_a_codex_implementation_is_not_reviewed_by_codex(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    tmp_path: Path,
) -> None:
    reviewing_task(conn, repo, paths, tmp_path, lane=Lane.B)
    with pytest.raises(ReviewFlowError):
        review(conn, repo, paths, home, "review_pass")
    assert task_of(conn).state is TaskState.REVIEWING
    assert_untouched(paths, conn)


def test_an_interactive_claude_implementation_is_reviewed(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    tmp_path: Path,
) -> None:
    reviewing_task(conn, repo, paths, tmp_path, lane=Lane.D)
    result = review(conn, repo, paths, home, "review_pass")
    assert result.outcome is ReviewOutcome.SAVED


def test_a_missing_worktree_is_refused(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
) -> None:
    git(repo, "worktree", "remove", "--force", str(worktree))
    with pytest.raises(ReviewFlowError):
        review(conn, repo, paths, home, "review_pass")
    assert task_of(conn).state is TaskState.REVIEWING
    assert_untouched(paths, conn)


def test_a_missing_spec_is_refused(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
) -> None:
    (paths.tasks / f"{TASK}.md").unlink()
    with pytest.raises(ReviewFlowError):
        review(conn, repo, paths, home, "review_pass")
    assert_untouched(paths, conn)


# Saved reviews


def test_a_pass_is_saved_and_moves_to_ready(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
) -> None:
    result = review(conn, repo, paths, home, "review_pass")
    log = paths.logs / "review" / f"{TASK}.1.log"
    assert result == ReviewRun(
        task_id=TASK,
        outcome=ReviewOutcome.SAVED,
        state=TaskState.READY,
        exit_code=0,
        log_path=log,
        output_path=paths.logs / "review" / f"{TASK}.1.out",
        diff_path=paths.reviews / f"{TASK}.diff",
        proxy_denials=0,
    )
    assert task_of(conn).state is TaskState.READY
    saved = results.load_review(paths.review_path(TASK), TASK)
    assert (saved.reviewer, saved.verdict) == (Reviewer.CODEX_REVIEW, Verdict.PASS)
    assert log.is_file()


def test_a_block_is_saved_and_moves_to_judging(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
) -> None:
    result = review(conn, repo, paths, home, "review_block")
    assert (result.outcome, result.state) == (ReviewOutcome.SAVED, TaskState.JUDGING)
    saved = results.load_review(paths.review_path(TASK), TASK)
    assert saved.verdict is Verdict.BLOCK
    assert saved.findings[0].severity is Severity.P1


def test_the_diff_holds_the_tests_and_the_implementation(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
) -> None:
    review(conn, repo, paths, home, "review_pass")
    base = git(worktree, "merge-base", MAIN, "HEAD")
    expected = git(worktree, "diff", "--no-color", "--no-ext-diff", f"{base}..HEAD")
    diff = (paths.reviews / f"{TASK}.diff").read_text(encoding="utf-8")
    assert diff.strip() == expected.strip()
    assert "tests/test_x.py" in diff
    assert "+VALUE = 'implemented'" in diff


def test_the_agent_gets_the_sandboxed_command_and_the_prompt(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    tmp_path: Path,
) -> None:
    args_file = tmp_path / "args.json"
    environ_extra = {"ORCH_FAKE_ARGS_FILE": str(args_file), "GH_TOKEN": "secret"}
    result = review(conn, repo, paths, home, "review_args", **environ_extra)
    seen = json.loads(args_file.read_text(encoding="utf-8"))
    argv = seen["argv"]
    assert argv[argv.index("--sandbox") + 1] == "danger-full-access"
    assert argv[argv.index("-o") + 1] == str(result.output_path)
    assert argv[argv.index("-C") + 1] == str(worktree)
    assert argv[argv.index("-m") + 1] == MODEL
    assert "model_reasoning_effort=high" in argv
    assert argv[-1] == "-"
    assert "--json" in argv
    assert Path(seen["cwd"]).resolve() == worktree.resolve()
    assert "GH_TOKEN" not in seen["env"]
    assert {"TMPDIR", "HTTPS_PROXY", "ALL_PROXY", "HTTP_PROXY"} <= set(seen["env"])
    assert seen["stdin"] == review_prompt(
        TASK,
        spec_path=paths.tasks / f"{TASK}.md",
        diff_path=paths.reviews / f"{TASK}.diff",
    )


# Failures leave the task in reviewing


@pytest.mark.parametrize("scenario", ["review_invalid", "review_wrong_task"])
def test_an_invalid_final_message_is_not_saved(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    scenario: str,
) -> None:
    result = review(conn, repo, paths, home, scenario)
    assert (result.outcome, result.state, result.exit_code) == (
        ReviewOutcome.INVALID,
        TaskState.REVIEWING,
        0,
    )
    assert task_of(conn).state is TaskState.REVIEWING
    assert not paths.review_path(TASK).exists()


def test_an_undecodable_final_message_is_invalid(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
) -> None:
    result = review(conn, repo, paths, home, "review_bad_bytes")
    assert (result.outcome, result.state) == (
        ReviewOutcome.INVALID,
        TaskState.REVIEWING,
    )
    assert not paths.review_path(TASK).exists()


def test_exit_zero_without_a_final_message_is_a_failure(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
) -> None:
    result = review(conn, repo, paths, home, "review_no_output")
    assert (result.outcome, result.state, result.exit_code) == (
        ReviewOutcome.FAILED,
        TaskState.REVIEWING,
        0,
    )


def test_a_nonzero_exit_is_a_failure(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
) -> None:
    result = review(conn, repo, paths, home, "review_fail")
    assert (result.outcome, result.state, result.exit_code) == (
        ReviewOutcome.FAILED,
        TaskState.REVIEWING,
        1,
    )
    assert "boom" in result.log_path.read_text(encoding="utf-8")


def test_a_usage_limit_is_reported_apart_from_a_failure(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
) -> None:
    result = review(conn, repo, paths, home, "review_usage_limit")
    assert (result.outcome, result.state) == (ReviewOutcome.QUOTA, TaskState.REVIEWING)


def test_the_time_limit_stops_the_review(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
) -> None:
    started = time.monotonic()
    result = review(conn, repo, paths, home, "timeout", timeout_sec=1)
    assert time.monotonic() - started < 20
    assert (result.outcome, result.state) == (
        ReviewOutcome.TIMEOUT,
        TaskState.REVIEWING,
    )


def test_the_task_time_limit_applies_without_an_override(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    tmp_path: Path,
) -> None:
    reviewing_task(conn, repo, paths, tmp_path, timeout_sec=1)
    started = time.monotonic()
    result = review(conn, repo, paths, home, "timeout")
    assert time.monotonic() - started < 20
    assert result.outcome is ReviewOutcome.TIMEOUT


def test_an_agent_that_cannot_start_is_a_failure(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
) -> None:
    result = run_codex_review(
        conn,
        TASK,
        target=make_target(repo),
        config=make_config(codex_path=home / "missing" / "codex"),
        paths=paths,
        environ={"PATH": "/usr/bin", "HOME": str(home)},
        model=MODEL,
        effort="high",
        home=home,
        orch_home=home / ".orch",
    )
    assert (result.outcome, result.state, result.exit_code) == (
        ReviewOutcome.FAILED,
        TaskState.REVIEWING,
        None,
    )


# Logs and numbering


def test_each_review_gets_its_own_log_and_ignores_a_stale_output(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
) -> None:
    review_dir = paths.logs / "review"
    write(review_dir / f"{TASK}.1.log")
    write(review_dir / f"{TASK}.2.out", "hello")
    result = review(conn, repo, paths, home, "review_no_output")
    assert result.log_path == review_dir / f"{TASK}.2.log"
    assert result.output_path == review_dir / f"{TASK}.2.out"
    assert result.outcome is ReviewOutcome.FAILED
    assert not (paths.logs / f"{TASK}.1.log").exists()


def test_next_review_log_numbers_per_task(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    assert next_review_log(logs, TASK) == logs / "review" / f"{TASK}.1.log"
    assert not (logs / "review").exists()
    for name in (f"{TASK}.1.log", f"{TASK}.3.log", "other.9.log", f"{TASK}.7.out"):
        write(logs / "review" / name)
    assert next_review_log(logs, TASK) == logs / "review" / f"{TASK}.4.log"
    assert next_review_log(logs, "a.b") == logs / "review" / "a.b.1.log"


def test_the_log_keeps_what_the_agent_printed_and_the_report_does_not(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
) -> None:
    result = review(conn, repo, paths, home, "review_noisy")
    assert result.outcome is ReviewOutcome.SAVED
    assert result.log_path.read_text(encoding="utf-8").count("SECRET-LOG-MARKER") == 2
    assert result.log_path.is_relative_to(paths.logs)
    assert "SECRET-LOG-MARKER" not in repr(result)
    assert all(
        "SECRET-LOG-MARKER" not in str(value)
        for value in dataclasses.asdict(result).values()
    )


# The prompt


def test_the_prompt_names_the_task_files_and_the_format() -> None:
    spec = Path("/", "o", "tasks", f"{TASK}.md")
    diff = Path("/", "o", "reviews", f"{TASK}.diff")
    prompt = review_prompt(TASK, spec_path=spec, diff_path=diff)
    assert f'Task id: "{TASK}"' in prompt.splitlines()
    assert str(spec) in prompt
    assert str(diff) in prompt
    for word in ("codex-review", "PASS", "BLOCK", "P0", "P3", "findings", "Korean"):
        assert word in prompt
    assert prompt.isascii()


# What reaches the pull request


def test_the_pull_request_body_carries_counts_not_review_text() -> None:
    home = Path("/", "Users", "tester")
    review_report = Review(
        task_id=TASK,
        reviewer=Reviewer.CODEX_REVIEW,
        verdict=Verdict.BLOCK,
        findings=(
            Finding(
                severity=Severity.P1,
                path="src/app.py",
                summary="SECRET-LOG-MARKER /Users/tester/notes 에 남은 내용",
            ),
            Finding(severity=Severity.P3, path="src/b.py", summary="사소한 지적"),
        ),
    )
    task = Task(
        id=TASK,
        target="sample",
        title="Add feature X",
        grade=Grade.L3,
        lane=Lane.C,
        model="claude-sonnet-5-5",
        state=TaskState.READY,
        timeout_sec=60,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    body = build_body(task, changed=["src/app.py"], gates=[], review=review_report)
    assert "Verdict: BLOCK, P1: 1, P3: 1" in body
    assert "SECRET-LOG-MARKER" not in body
    assert "사소한" not in body
    assert redact(body, home=home, orch_home=home / ".orch") == body


# Under the orch profile behind the allowlist proxy (m-codex-review-sandbox)


def proxy_log(paths: TargetPaths, n: int = 1) -> Path:
    return paths.logs / "review" / f"{TASK}.{n}.proxy.log"


def assert_failed_without_review(result: ReviewRun, conn: sqlite3.Connection) -> None:
    assert result.outcome is ReviewOutcome.FAILED
    assert result.state is TaskState.REVIEWING
    assert task_of(conn).state is TaskState.REVIEWING


@pytest.fixture
def run_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Where mkdtemp puts the review's run directory, to see that it is removed."""
    root = tmp_path / "run-tmp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


@pytest.mark.parametrize("mode", ["fail", "unknown-mode"])
def test_a_proxy_that_cannot_start_fails_without_starting_codex(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    tmp_path: Path,
    run_tmp: Path,
    mode: str,
) -> None:
    args_file = tmp_path / "args.json"
    result = review(
        conn,
        repo,
        paths,
        home,
        "review_args",
        ORCH_FAKE_PROXY=mode,
        ORCH_FAKE_ARGS_FILE=str(args_file),
    )
    assert_failed_without_review(result, conn)
    assert (result.exit_code, result.proxy_denials) == (None, 0)
    assert not args_file.exists()
    assert not paths.review_path(TASK).exists()
    assert list(run_tmp.iterdir()) == []


@pytest.mark.parametrize("hub", ["unset", "relative", "no-hosts"])
def test_a_review_without_valid_proxy_hosts_fails_without_starting_codex(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    tmp_path: Path,
    hub: str,
) -> None:
    args_file = tmp_path / "args.json"
    environ = make_env(home, "review_args", ORCH_FAKE_ARGS_FILE=str(args_file))
    if hub == "unset":
        del environ["ORCH_HUB"]
    elif hub == "relative":
        environ["ORCH_HUB"] = "hub"
    else:
        root = tmp_path / "hub-no-hosts"
        (root / "config").mkdir(parents=True)
        (root / "tests").mkdir()
        (root / "tests" / "fake_agents").symlink_to(HUB / "tests" / "fake_agents")
        (root / "config" / "pins.toml").write_text(
            "[codex]\nproxy_hosts = []\n", encoding="utf-8"
        )
        environ["ORCH_HUB"] = str(root)
    result = run_codex_review(
        conn,
        TASK,
        target=make_target(repo),
        config=make_config(),
        paths=paths,
        environ=environ,
        model=MODEL,
        effort="high",
        home=home,
        orch_home=home / ".orch",
    )
    assert_failed_without_review(result, conn)
    assert result.exit_code is None
    assert not args_file.exists()


def test_a_proxy_that_stops_during_the_review_stops_codex(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    tmp_path: Path,
    run_tmp: Path,
) -> None:
    env_file = tmp_path / "env.json"
    started = time.monotonic()
    result = review(
        conn,
        repo,
        paths,
        home,
        "review_env_then_hang",
        ORCH_FAKE_PROXY="die",
        ORCH_FAKE_ENV_FILE=str(env_file),
    )
    assert time.monotonic() - started < 30
    assert_failed_without_review(result, conn)
    assert result.exit_code is not None and result.exit_code < 0
    values = json.loads(env_file.read_text(encoding="utf-8"))
    assert Path(values["TMPDIR"]).parent == run_tmp.resolve()
    assert list(run_tmp.iterdir()) == []


def test_a_review_written_before_the_proxy_stops_is_not_saved(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
) -> None:
    result = review(
        conn, repo, paths, home, "review_pass_then_hang", ORCH_FAKE_PROXY="die"
    )
    assert_failed_without_review(result, conn)
    assert result.output_path.exists()
    assert not paths.review_path(TASK).exists()


def test_a_review_without_command_events_is_invalid(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
) -> None:
    result = review(conn, repo, paths, home, "review_pass", ORCH_FAKE_NO_COMMANDS="1")
    assert (result.outcome, result.state, result.exit_code) == (
        ReviewOutcome.INVALID,
        TaskState.REVIEWING,
        0,
    )
    assert task_of(conn).state is TaskState.REVIEWING
    assert not paths.review_path(TASK).exists()


@pytest.mark.parametrize(
    ("scenario", "outcome"),
    [
        ("review_no_output", ReviewOutcome.FAILED),
        ("review_fail", ReviewOutcome.FAILED),
        ("review_usage_limit", ReviewOutcome.QUOTA),
    ],
)
def test_without_a_final_message_the_command_check_changes_nothing(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    scenario: str,
    outcome: ReviewOutcome,
) -> None:
    result = review(conn, repo, paths, home, scenario, ORCH_FAKE_NO_COMMANDS="1")
    assert result.outcome is outcome


def test_codex_gets_the_proxy_variables_and_a_run_tmpdir_that_is_removed(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    tmp_path: Path,
    run_tmp: Path,
) -> None:
    env_file = tmp_path / "env.json"
    result = review(
        conn, repo, paths, home, "review_env_values", ORCH_FAKE_ENV_FILE=str(env_file)
    )
    assert result.outcome is ReviewOutcome.SAVED
    values = json.loads(env_file.read_text(encoding="utf-8"))
    for name in (
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
        "HTTP_PROXY",
        "http_proxy",
    ):
        assert values[name] == "http://127.0.0.1:9"
    assert values["tmpdir_is_dir"] is True
    tmpdir = Path(values["TMPDIR"])
    assert tmpdir.name.startswith("orch-codex-")
    assert tmpdir == tmpdir.resolve()
    assert not tmpdir.exists()
    assert list(run_tmp.iterdir()) == []


def test_codex_starts_through_the_sandbox_exec_with_the_review_profile(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []
    real = subprocess.Popen

    def spy(args: object, *rest: object, **options: object) -> object:
        if isinstance(args, list | tuple):
            seen.append([str(arg) for arg in args])
        return real(args, *rest, **options)

    monkeypatch.setattr(subprocess, "Popen", spy)
    result = review(conn, repo, paths, home, "review_pass")
    agents = [argv for argv in seen if any(a.endswith("/codex") for a in argv)]
    assert len(agents) == 1
    argv = agents[0]
    assert argv[0] == str(FAKE_SANDBOX_EXEC)
    assert argv[1] == "-p"
    assert '(remote ip "localhost:9")' in argv[2]
    assert f'(literal "{result.output_path.resolve()}")' in argv[2]
    assert argv[argv.index("--sandbox") + 1] == "danger-full-access"


def test_a_denied_connection_is_counted_and_logged(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
) -> None:
    result = review(conn, repo, paths, home, "review_slow_pass", ORCH_FAKE_PROXY="deny")
    assert result.outcome is ReviewOutcome.SAVED
    assert result.proxy_denials == 1
    lines = [
        json.loads(line)
        for line in proxy_log(paths).read_text(encoding="utf-8").splitlines()
    ]
    assert [(x["proxy"], x["host"], x["reason"]) for x in lines] == [
        ("deny", "denied.invalid", "host")
    ]
    assert dataclasses.asdict(result)["proxy_denials"] == 1


def test_no_denial_leaves_an_empty_proxy_log(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
) -> None:
    result = review(conn, repo, paths, home, "review_pass")
    assert result.proxy_denials == 0
    assert proxy_log(paths).read_text(encoding="utf-8") == ""


def test_the_proxy_log_does_not_change_the_review_numbering(
    conn: sqlite3.Connection,
    repo: Path,
    paths: TargetPaths,
    home: Path,
    worktree: Path,
) -> None:
    first = review(conn, repo, paths, home, "review_no_output")
    second = review(conn, repo, paths, home, "review_no_output")
    assert first.log_path.name == f"{TASK}.1.log"
    assert second.log_path.name == f"{TASK}.2.log"
    assert proxy_log(paths, 1).exists()
    assert proxy_log(paths, 2).exists()
