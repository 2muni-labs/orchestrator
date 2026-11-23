"""G2_PRE_PR and orch update run the selected test groups (m-p5-test-select-gate)."""
# ruff: noqa: F811 - fixtures imported from test_prflow are used as parameters

import sqlite3
from pathlib import Path

import pytest

from orch import store
from orch.config import SelectConfig, SelectGroup, Target
from orch.models import Gate, Grade, TaskState
from orch.results import write_review
from test_prflow import (  # noqa: F401 - fixtures used by name
    TASK,
    FakeGitHub,
    add_ready_task,
    conn,
    git,
    hub,
    implemented,
    isolated_git,
    make_review,
    make_spec,
    make_target,
    move_main,
    open_pr,
    opened,
    remote,
    reopened,
    repo,
    task_of,
    test_sha,
    update_pr,
    worktree,
    write,
)


def app_target(repo: Path, order: Path | None = None, **overrides: object) -> Target:
    """A target with group app (src/app.py and tests/**) and group other."""
    sink = f" >> {order}" if order is not None else ""
    select = SelectConfig(
        groups=(
            SelectGroup(
                name="app",
                sources=("src/app.py",),
                tests=("tests/**",),
                command=f"echo app{sink}",
            ),
            SelectGroup(
                name="other",
                sources=("src/other.py",),
                tests=("other/**",),
                command=f"echo other{sink}",
            ),
        ),
    )
    values: dict[str, object] = {
        "test_command": f"echo full{sink}",
        "test_select": select,
    }
    values.update(overrides)
    return make_target(repo, **values)


def suite(conn: sqlite3.Connection, gate: Gate) -> store.SuiteRecord | None:
    return store.suite_run(conn, TASK, 1, gate)


def body_of(hub: FakeGitHub) -> str:
    return str(hub.created[-1]["body"])


SELECTED = "## Tests\n\n- G2_PRE_PR: selected run (app)\n  - app: path:src/app.py"


def test_open_runs_only_the_selected_group_and_records_it(
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
    spec = make_spec(verify=f"echo verify >> {order}")
    report = open_pr(conn, repo, tmp_path, spec=spec, target=app_target(repo, order))
    assert report.state is TaskState.PR_OPEN
    assert order.read_text().split() == ["app", "verify"]
    record = suite(conn, Gate.G2_PRE_PR)
    assert record is not None
    assert (record.full, record.groups, record.reasons, record.failed_tests) == (
        False,
        ("app",),
        ("app: path:src/app.py",),
        (),
    )
    body = body_of(hub)
    assert body.rstrip().endswith(SELECTED)
    assert "\n\n" + SELECTED in body
    assert "## Gates" in body.split("## Tests")[0]


def test_open_selects_after_the_rebase(
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
    move_main(repo, "src/other.py")
    open_pr(conn, repo, tmp_path, target=app_target(repo, order))
    assert order.read_text().split() == ["app"]
    record = suite(conn, Gate.G2_PRE_PR)
    assert record is not None and record.groups == ("app",)


def test_open_runs_everything_for_a_higher_grade(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    order = tmp_path / "order.txt"
    add_ready_task(conn, worktree, test_sha, grade=Grade.L3)
    reviews = tmp_path / "reviews"
    reviews.mkdir()
    write_review(reviews / "feat-x.json", make_review())
    spec = make_spec(grade=Grade.L3)
    target = app_target(repo, order)
    open_pr(conn, repo, tmp_path, spec=spec, target=target)
    assert order.read_text().split() == ["full"]
    record = suite(conn, Gate.G2_PRE_PR)
    assert record is not None
    assert (record.full, record.reasons) == (True, ("grade:L3",))
    assert (
        body_of(hub)
        .rstrip()
        .endswith("## Tests\n\n- G2_PRE_PR: full run\n  - grade:L3")
    )


def test_open_without_groups_shows_a_full_run(
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
    assert (
        body_of(hub)
        .rstrip()
        .endswith("## Tests\n\n- G2_PRE_PR: full run\n  - no_test_groups")
    )


def test_a_failing_pre_pr_gate_records_only_this_runs_failed_tests(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    add_ready_task(conn, worktree, test_sha)
    log = tmp_path / "logs" / "feat-x.pr.log"
    write(log, "FAILED tests/old.py::test_before - earlier run\n")
    select = SelectConfig(
        groups=(
            SelectGroup(
                name="app",
                sources=("src/app.py",),
                tests=("tests/**",),
                command="echo 'FAILED tests/test_x.py::test_now - boom'; exit 1",
            ),
        )
    )
    target = make_target(repo, test_select=select)
    report = open_pr(conn, repo, tmp_path, target=target)
    assert report.state is TaskState.JUDGING
    record = suite(conn, Gate.G2_PRE_PR)
    assert record is not None
    assert record.failed_tests == ("tests/test_x.py::test_now",)
    assert hub.created == []


def test_a_skipped_pre_pr_gate_records_nothing_and_shows_the_g2_selection(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    add_ready_task(conn, worktree, test_sha, gated_sha=implemented)
    store.record_suite_run(
        conn,
        TASK,
        1,
        Gate.G2,
        full=False,
        groups=("app",),
        reasons=("app: import:src/a.py:tests/test_x.py",),
        failed_tests=(),
    )
    order = tmp_path / "order.txt"
    report = open_pr(conn, repo, tmp_path, target=app_target(repo, order))
    assert report.state is TaskState.PR_OPEN
    assert not order.exists()
    assert suite(conn, Gate.G2_PRE_PR) is None
    expected = (
        "## Tests\n\n- G2: selected run (app)\n  - app: import:src/a.py:tests/test_x.py"
    )
    assert body_of(hub).rstrip().endswith(expected)


def test_a_skipped_pre_pr_gate_without_a_g2_selection_shows_no_tests(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    test_sha: str,
    implemented: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    add_ready_task(conn, worktree, test_sha, gated_sha=implemented)
    open_pr(conn, repo, tmp_path, target=app_target(repo))
    assert "## Tests" not in body_of(hub)


def test_a_new_attempt_comment_keeps_the_commit_line_last(
    conn: sqlite3.Connection,
    repo: Path,
    reopened: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    open_pr(conn, repo, tmp_path, target=app_target(repo))
    body = str(hub.comments[-1]["body"])
    assert body.rstrip().endswith(f"Commit: `{reopened}`")
    tests = body.index("## Tests\n\n- G2_PRE_PR: selected run (app)")
    assert body.index("## Gates") < tests < body.index("Commit: `")


def test_update_runs_only_the_selected_group_and_records_nothing(
    conn: sqlite3.Connection,
    repo: Path,
    worktree: Path,
    opened: str,
    tmp_path: Path,
    hub: FakeGitHub,
) -> None:
    order = tmp_path / "order.txt"
    move_main(repo, "src/other.py")
    before = conn.execute("SELECT * FROM suite_runs ORDER BY 1, 2, 3").fetchall()
    report = update_pr(conn, repo, tmp_path, target=app_target(repo, order))
    assert report.state is TaskState.PR_OPEN
    assert report.gates is not None and report.gates.passed is True
    assert order.read_text().split() == ["app"]
    assert report.gates.suite is not None
    assert report.gates.suite.groups == ("app",)
    after = conn.execute("SELECT * FROM suite_runs ORDER BY 1, 2, 3").fetchall()
    assert after == before


@pytest.mark.parametrize("safety", [("src/app.py",), ("tests/**",)])
def test_update_runs_everything_for_a_safety_change(
    conn: sqlite3.Connection,
    repo: Path,
    opened: str,
    tmp_path: Path,
    hub: FakeGitHub,
    safety: tuple[str, ...],
) -> None:
    order = tmp_path / "order.txt"
    move_main(repo, "src/other.py")
    target = app_target(repo, order, safety_paths=safety)
    report = update_pr(conn, repo, tmp_path, target=target)
    assert report.gates is not None and report.gates.passed is True
    assert order.read_text().split() == ["full"]
    assert task_of(conn).state is TaskState.PR_OPEN
