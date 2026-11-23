"""G2 runs the selected test groups and records them (m-p5-test-select-gate)."""
# ruff: noqa: F811 - fixtures imported from test_gate are used as parameters

import sqlite3
from pathlib import Path

import pytest

from orch import store, testselect
from orch.config import SelectConfig, SelectGroup, Target
from orch.gate import (
    GateReport,
    RunPlan,
    SuiteRun,
    failed_tests,
    plan_tests,
    run_gates,
    run_tests,
)
from orch.models import Gate, Grade, TaskState
from orch.testselect import Selection
from test_gate import (  # noqa: F401 - fixtures used by name
    MAIN,
    add_judged_task,
    conn,
    git,
    isolated_git,
    log_path,
    make_spec,
    make_target,
    repo,
    run_flow,
    state_of,
    test_sha,
    worktree,
    write,
)

# Planning


def groups_target(order: Path | None = None, **overrides: object) -> Target:
    """A target with groups a (src/a.py) and b (src/b.py) that log their runs."""
    sink = f" >> {order}" if order is not None else ""
    select = SelectConfig(
        groups=(
            SelectGroup(
                name="a",
                sources=("src/a.py",),
                tests=("tests/a/**",),
                command=f"echo a{sink}",
            ),
            SelectGroup(
                name="b",
                sources=("src/b.py",),
                tests=("tests/b/**",),
                command=f"echo b{sink}",
            ),
        ),
        ignore_paths=("docs/**",),
    )
    values: dict[str, object] = {
        "test_command": f"echo full{sink}",
        "test_select": select,
    }
    values.update(overrides)
    return make_target(**values)


def test_a_target_without_groups_runs_everything(tmp_path: Path) -> None:
    target = make_target(test_command="pytest -q")
    assert plan_tests(target, Grade.L2, tmp_path, ["src/a.py"]) == RunPlan(
        full=True, reasons=("no_test_groups",), commands=("pytest -q",)
    )


@pytest.mark.parametrize("grade", [Grade.L3, Grade.L3R])
def test_a_higher_grade_runs_everything(tmp_path: Path, grade: Grade) -> None:
    target = groups_target(safety_paths=("src/a.py",))
    assert plan_tests(target, grade, tmp_path, ["src/a.py"]) == RunPlan(
        full=True, reasons=(f"grade:{grade}",), commands=("echo full",)
    )


def test_no_groups_wins_over_the_grade(tmp_path: Path) -> None:
    target = make_target(test_command="t")
    assert plan_tests(target, Grade.L3, tmp_path, ["src/a.py"]).reasons == (
        "no_test_groups",
    )


def test_a_safety_change_runs_everything(tmp_path: Path) -> None:
    target = groups_target(safety_paths=("src/b.py", "src/a.py"))
    plan = plan_tests(target, Grade.L2, tmp_path, ["src/b.py", "src/a.py", "x.md"])
    assert plan == RunPlan(
        full=True,
        reasons=("safety_path:src/a.py", "safety_path:src/b.py"),
        commands=("echo full",),
    )


def test_a_selection_error_runs_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(*args: object, **kwargs: object) -> Selection:
        raise RecursionError("deep")

    monkeypatch.setattr(testselect, "select", failing)
    assert plan_tests(groups_target(), Grade.L2, tmp_path, ["src/a.py"]) == RunPlan(
        full=True, reasons=("select_error:RecursionError",), commands=("echo full",)
    )


def test_a_full_selection_runs_everything_with_its_reasons(tmp_path: Path) -> None:
    plan = plan_tests(groups_target(), Grade.L2, tmp_path, ["Makefile", "src/a.py"])
    assert plan == RunPlan(
        full=True, reasons=("unmatched:Makefile",), commands=("echo full",)
    )


@pytest.mark.parametrize(
    "changed",
    [["docs/a.md"], [], [".orch-result.json"], [".orch-result.json", "docs/b"]],
)
def test_no_selected_group_runs_everything(tmp_path: Path, changed: list[str]) -> None:
    assert plan_tests(groups_target(), Grade.L2, tmp_path, changed) == RunPlan(
        full=True, reasons=("no_groups",), commands=("echo full",)
    )


def test_selected_groups_run_their_commands_in_name_order(tmp_path: Path) -> None:
    plan = plan_tests(
        groups_target(), Grade.L2, tmp_path, ["src/b.py", "docs/a.md", "src/a.py"]
    )
    assert plan == RunPlan(
        full=False,
        groups=("a", "b"),
        reasons=("a: path:src/a.py", "b: path:src/b.py"),
        commands=("echo a", "echo b"),
    )


def test_the_selection_gets_the_worktree_and_the_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[object, ...]] = []

    def fake(worktree: Path, changed: object, config: SelectConfig) -> Selection:
        calls.append((worktree, sorted(changed), config))  # type: ignore[arg-type]
        return Selection(full=False, groups=("b",), reasons=("b: also:a",))

    monkeypatch.setattr(testselect, "select", fake)
    target = groups_target()
    plan = plan_tests(target, Grade.L1, tmp_path, iter(["src/a.py"]))
    assert calls == [(tmp_path, ["src/a.py"], target.test_select)]
    assert plan == RunPlan(
        full=False, groups=("b",), reasons=("b: also:a",), commands=("echo b",)
    )


# Failed test names


def test_failed_tests_reads_the_pytest_summary_lines() -> None:
    text = (
        "$ pytest\n"
        "FAILED tests/x.py::test_a - AssertionError: boom\n"
        "ERROR tests/y.py::test_b\n"
        "  FAILED tests/indented.py::test_c\n"
        "PASSED tests/z.py::test_d\n"
        "FAILED tests/x.py::test_a - again\n"
        "FAILED\n"
        "xFAILED tests/w.py::test_e\n"
    )
    assert failed_tests(text) == ("tests/x.py::test_a", "tests/y.py::test_b")


def test_failed_tests_keeps_at_most_fifty_short_ids() -> None:
    text = "".join(f"FAILED t.py::test_{n}\n" for n in range(60))
    found = failed_tests(text + "FAILED " + "x" * 400 + "\n")
    assert len(found) == 50
    assert found[0] == "t.py::test_0" and found[-1] == "t.py::test_49"
    assert failed_tests("FAILED " + "x" * 400) == ("x" * 300,)
    assert failed_tests("") == ()


# Running a plan


def test_run_tests_runs_the_plan_commands_then_verify(
    tmp_path: Path, worktree: Path, log_path: Path
) -> None:
    order = tmp_path / "order.txt"
    plan = RunPlan(
        full=False,
        groups=("a", "b"),
        commands=(f"echo a >> {order}", f"echo b >> {order}"),
    )
    spec = make_spec(verify=f"echo verify >> {order}")
    target = make_target(test_command=f"echo full >> {order}")
    assert run_tests(spec, target, worktree, log_path, plan=plan) is None
    assert order.read_text().split() == ["a", "b", "verify"]


def test_run_tests_stops_at_the_first_failing_plan_command(
    tmp_path: Path, worktree: Path, log_path: Path
) -> None:
    order = tmp_path / "order.txt"
    plan = RunPlan(full=False, commands=("exit 3", f"echo b >> {order}"))
    spec = make_spec(verify=f"echo verify >> {order}")
    failure = run_tests(spec, make_target(), worktree, log_path, plan=plan)
    assert failure is not None and "exit 3" in failure
    assert not order.exists()


def test_tests_none_allows_an_empty_group(worktree: Path, log_path: Path) -> None:
    plan = RunPlan(full=False, commands=("exit 5", "exit 5"))
    assert run_tests(make_spec(), make_target(), worktree, log_path, plan=plan)
    allowed = make_spec(grade=Grade.L1, tests="none")
    assert run_tests(allowed, make_target(), worktree, log_path, plan=plan) is None


def test_run_gates_without_a_plan_reports_a_full_run(
    worktree: Path, test_sha: str, log_path: Path
) -> None:
    report = run_gates(
        make_spec(),
        make_target(),
        worktree,
        since=test_sha,
        main_ref=MAIN,
        log_path=log_path,
    )
    assert report.passed is True
    assert report.suite == SuiteRun(full=True, reasons=("no_plan",))


def test_run_gates_passes_the_plan_and_reports_the_failed_tests(
    worktree: Path, test_sha: str, log_path: Path
) -> None:
    plan = RunPlan(
        full=False,
        groups=("a",),
        reasons=("a: path:src/a.py",),
        commands=("echo 'FAILED tests/a/test_x.py::test_y - boom'; exit 1",),
    )
    report = run_gates(
        make_spec(),
        make_target(test_command="exit 9"),
        worktree,
        since=test_sha,
        main_ref=MAIN,
        log_path=log_path,
        plan=plan,
    )
    assert report.failed_gate == "G2"
    assert report.suite == SuiteRun(
        full=False,
        groups=("a",),
        reasons=("a: path:src/a.py",),
        failed_tests=("tests/a/test_x.py::test_y",),
    )


def test_a_gate_report_has_no_suite_by_default() -> None:
    assert GateReport(passed=True).suite is None


# The orch gate flow


def suite(conn: sqlite3.Connection, name: Gate = Gate.G2) -> store.SuiteRecord | None:
    return store.suite_run(conn, "feat-x", 1, name)


def test_the_gate_runs_only_the_selected_group_and_records_it(
    conn: sqlite3.Connection,
    worktree: Path,
    test_sha: str,
    log_path: Path,
    tmp_path: Path,
) -> None:
    order = tmp_path / "order.txt"
    add_judged_task(conn, worktree, test_sha)
    write(worktree / "src" / "a.py")
    spec = make_spec(verify=f"echo verify >> {order}")
    report = run_flow(conn, log_path, groups_target(order), spec)
    assert report.passed is True
    assert order.read_text().split() == ["a", "verify"]
    assert report.suite == SuiteRun(
        full=False, groups=("a",), reasons=("a: path:src/a.py",)
    )
    record = suite(conn)
    assert record is not None
    assert (record.full, record.groups, record.reasons, record.failed_tests) == (
        False,
        ("a",),
        ("a: path:src/a.py",),
        (),
    )
    assert suite(conn, Gate.G2_PRE_PR) is None


@pytest.mark.parametrize(
    ("grade", "safety", "reasons"),
    [
        (Grade.L3, (), ("grade:L3",)),
        (Grade.L2, ("src/a.py",), ("safety_path:src/a.py",)),
    ],
)
def test_the_gate_runs_everything_for_l3_and_safety_changes(
    conn: sqlite3.Connection,
    worktree: Path,
    test_sha: str,
    log_path: Path,
    tmp_path: Path,
    grade: Grade,
    safety: tuple[str, ...],
    reasons: tuple[str, ...],
) -> None:
    order = tmp_path / "order.txt"
    add_judged_task(conn, worktree, test_sha, grade=grade)
    write(worktree / "src" / "a.py")
    report = run_flow(conn, log_path, groups_target(order, safety_paths=safety))
    assert report.passed is True
    assert order.read_text().split() == ["full"]
    record = suite(conn)
    assert record is not None
    assert (record.full, record.groups, record.reasons) == (True, (), reasons)


def test_the_gate_without_groups_records_a_full_run(
    conn: sqlite3.Connection, worktree: Path, test_sha: str, log_path: Path
) -> None:
    add_judged_task(conn, worktree, test_sha)
    write(worktree / "src" / "a.py")
    assert run_flow(conn, log_path).passed is True
    record = suite(conn)
    assert record is not None
    assert (record.full, record.reasons) == (True, ("no_test_groups",))


def test_the_changes_count_from_the_base_when_there_is_one(
    conn: sqlite3.Connection, worktree: Path, log_path: Path, tmp_path: Path
) -> None:
    base = git(worktree, "rev-parse", "HEAD")
    write(worktree / "src" / "b.py")
    git(worktree, "add", ".")
    git(worktree, "commit", "-qm", "stub")
    write(worktree / "tests" / "a" / "test_a.py")
    git(worktree, "add", ".")
    git(worktree, "commit", "-qm", "tests")
    test_sha = git(worktree, "rev-parse", "HEAD")
    add_judged_task(conn, worktree, test_sha)
    store.update_task(conn, "feat-x", base_sha=base)
    write(worktree / "src" / "a.py")
    order = tmp_path / "order.txt"
    assert run_flow(conn, log_path, groups_target(order)).passed is True
    assert order.read_text().split() == ["a", "b"]


def test_a_failing_group_records_the_failed_tests(
    conn: sqlite3.Connection, worktree: Path, test_sha: str, log_path: Path
) -> None:
    add_judged_task(conn, worktree, test_sha)
    write(worktree / "src" / "a.py")
    target = groups_target()
    failing = SelectGroup(
        name="a",
        sources=("src/a.py",),
        tests=("tests/a/**",),
        command="echo 'FAILED tests/a/test_a.py::test_one - x'; exit 1",
    )
    assert target.test_select is not None
    select = SelectConfig(groups=(failing, target.test_select.groups[1]))
    target = make_target(test_command="true", test_select=select)
    report = run_flow(conn, log_path, target)
    assert report.failed_gate == "G2"
    assert state_of(conn) is TaskState.JUDGING
    record = suite(conn)
    assert record is not None
    assert record.failed_tests == ("tests/a/test_a.py::test_one",)
    assert report.suite is not None
    assert report.suite.failed_tests == ("tests/a/test_a.py::test_one",)


def test_a_gate_that_stops_before_g2_records_no_selection_and_clears_the_old(
    conn: sqlite3.Connection, worktree: Path, test_sha: str, log_path: Path
) -> None:
    add_judged_task(conn, worktree, test_sha)
    store.record_suite_run(
        conn,
        "feat-x",
        1,
        Gate.G2,
        full=True,
        groups=(),
        reasons=("old",),
        failed_tests=(),
    )
    write(worktree / "outside.txt")
    report = run_flow(conn, log_path, groups_target())
    assert report.failed_gate == "G1"
    assert report.suite is None
    assert suite(conn) is None


def test_the_selection_goes_through_testselect(
    conn: sqlite3.Connection,
    worktree: Path,
    test_sha: str,
    log_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []

    def fake(worktree: Path, changed: object, config: SelectConfig) -> Selection:
        seen.append(sorted(changed))  # type: ignore[arg-type]
        return Selection(full=False, groups=("b",), reasons=("b: path:x",))

    monkeypatch.setattr(testselect, "select", fake)
    add_judged_task(conn, worktree, test_sha)
    write(worktree / "src" / "a.py")
    report = run_flow(conn, log_path, groups_target())
    assert seen == [["src/a.py"]]
    assert report.suite == SuiteRun(full=False, groups=("b",), reasons=("b: path:x",))
