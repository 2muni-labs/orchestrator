import ast
import dataclasses
import inspect
import sys
from enum import StrEnum
from types import MappingProxyType

import pytest

from orch import models
from orch.models import (
    TERMINAL_STATES,
    TRANSITIONS,
    Effort,
    FailureReason,
    Finding,
    Gate,
    GateResult,
    Grade,
    Lane,
    Lock,
    Outcome,
    Review,
    Reviewer,
    Run,
    Severity,
    Task,
    TaskState,
    Verdict,
    can_transition,
)

# Technical design, chapter 3. Written out here so that the test pins the table.
EXPECTED_TRANSITIONS = {
    "prepared": {"reserved", "queued", "discarded"},
    "queued": {"reserved", "discarded"},
    "reserved": {"running", "queued", "discarded"},
    "running": {"judging", "orphaned"},
    "judging": {"gating", "reserved", "queued", "discarded", "blocked"},
    "gating": {"reviewing", "ready", "judging"},
    "reviewing": {"ready", "judging"},
    "ready": {"pr_open", "conflict", "judging"},
    "conflict": {"pr_open", "discarded", "judging"},
    "pr_open": {"conflict", "closed", "discarded"},
    "closed": set(),
    "blocked": {"reserved", "queued", "discarded"},
    "discarded": set(),
    "orphaned": {"reserved", "queued", "discarded"},
}


@pytest.mark.parametrize(
    ("enum_class", "values"),
    [
        (TaskState, set(EXPECTED_TRANSITIONS)),
        (Grade, {"L1", "L2", "L3", "L3R"}),
        (Lane, {"B", "C", "D"}),
        (Effort, {"low", "medium", "high", "xhigh"}),
        (Outcome, {"DONE", "PARTIAL", "FAILED", "BLOCKED", "BLOCKED_QUOTA"}),
        (
            FailureReason,
            {
                "report_missing",
                "timeout",
                "killed",
                "aborted",
                "spec_mismatch",
                "out_of_scope",
            },
        ),
        (Gate, {"G1", "G2", "G3", "G2_PRE_PR"}),
        (Severity, {"P0", "P1", "P2", "P3"}),
        (Verdict, {"PASS", "BLOCK"}),
        (Reviewer, {"claude-reviewer", "codex-review"}),
    ],
)
def test_enum_values(enum_class: type[StrEnum], values: set[str]) -> None:
    assert issubclass(enum_class, StrEnum)
    assert {member.value for member in enum_class} == values


def test_enum_member_names_are_upper_case_values() -> None:
    assert TaskState.PR_OPEN == "pr_open"
    assert Grade.L3R == "L3R"
    assert Effort.XHIGH == "xhigh"
    assert Outcome.BLOCKED_QUOTA == "BLOCKED_QUOTA"
    assert FailureReason.REPORT_MISSING == "report_missing"
    assert Gate.G2_PRE_PR == "G2_PRE_PR"
    assert Reviewer.CLAUDE_REVIEWER == "claude-reviewer"
    assert Reviewer.CODEX_REVIEW == "codex-review"


def test_transition_table_matches_the_design() -> None:
    actual = {
        state.value: {target.value for target in targets}
        for state, targets in TRANSITIONS.items()
    }
    assert actual == EXPECTED_TRANSITIONS


def test_transition_table_is_read_only_data() -> None:
    assert isinstance(TRANSITIONS, MappingProxyType)
    assert all(isinstance(targets, frozenset) for targets in TRANSITIONS.values())
    with pytest.raises(TypeError):
        TRANSITIONS[TaskState.CLOSED] = frozenset()  # type: ignore[index]


def test_terminal_states_have_no_exit() -> None:
    assert TERMINAL_STATES == frozenset({TaskState.CLOSED, TaskState.DISCARDED})
    assert TERMINAL_STATES == {
        state for state, targets in TRANSITIONS.items() if not targets
    }


@pytest.mark.parametrize("current", sorted(EXPECTED_TRANSITIONS))
@pytest.mark.parametrize("new", sorted(EXPECTED_TRANSITIONS))
def test_can_transition_follows_the_table(current: str, new: str) -> None:
    allowed = new in EXPECTED_TRANSITIONS[current]
    assert can_transition(TaskState(current), TaskState(new)) is allowed


def test_can_transition_reads_the_table(monkeypatch: pytest.MonkeyPatch) -> None:
    replacement = MappingProxyType({TaskState.CLOSED: frozenset({TaskState.PREPARED})})
    monkeypatch.setattr(models, "TRANSITIONS", replacement)
    assert can_transition(TaskState.CLOSED, TaskState.PREPARED) is True
    assert can_transition(TaskState.PREPARED, TaskState.QUEUED) is False


def field_names(cls: type) -> set[str]:
    return {field.name for field in dataclasses.fields(cls)}


def make_task(**overrides: object) -> Task:
    values: dict[str, object] = {
        "id": "feat-x",
        "target": "orchestrator",
        "title": "Feature X",
        "grade": Grade.L2,
        "lane": Lane.B,
        "model": "gpt-6.1-sol",
        "state": TaskState.PREPARED,
        "timeout_sec": 2700,
        "created_at": "2026-11-03T00:00:00+00:00",
        "updated_at": "2026-11-03T00:00:00+00:00",
    }
    return Task(**(values | overrides))  # type: ignore[arg-type]


def test_task_fields_match_the_tasks_table() -> None:
    assert field_names(Task) == {
        "id",
        "target",
        "title",
        "grade",
        "lane",
        "model",
        "effort",
        "state",
        "attempt",
        "timeout_sec",
        "base_sha",
        "test_sha",
        "depends_on",
        "worktree",
        "pr_url",
        "pushed_sha",
        "slot_expires_at",
        "created_at",
        "updated_at",
    }


def test_task_defaults() -> None:
    task = make_task()
    assert task.attempt == 0
    assert task.depends_on == ()
    for name in (
        "effort",
        "base_sha",
        "test_sha",
        "worktree",
        "pr_url",
        "pushed_sha",
        "slot_expires_at",
    ):
        assert getattr(task, name) is None


def test_task_requires_its_required_fields() -> None:
    with pytest.raises(TypeError):
        Task(id="feat-x")  # type: ignore[call-arg]


def test_run_fields_match_the_runs_table() -> None:
    assert field_names(Run) == {
        "run_id",
        "task_id",
        "attempt",
        "lane",
        "model",
        "effort",
        "pid",
        "pid_started_at",
        "started_at",
        "ended_at",
        "exit_code",
        "outcome",
        "failure_reason",
    }


def test_run_defaults() -> None:
    run = Run(task_id="feat-x", attempt=1, lane=Lane.B, model="gpt-6.1-sol")
    for name in field_names(Run) - {"task_id", "attempt", "lane", "model"}:
        assert getattr(run, name) is None


def test_review_and_finding() -> None:
    finding = Finding(
        severity=Severity.P1, path="src/orch/x.py", summary="경계 조건 누락"
    )
    review = Review(
        task_id="feat-x",
        reviewer=Reviewer.CLAUDE_REVIEWER,
        verdict=Verdict.BLOCK,
        findings=(finding,),
    )
    assert field_names(Finding) == {"severity", "path", "summary"}
    assert field_names(Review) == {"task_id", "reviewer", "verdict", "findings"}
    assert review.findings == (finding,)


def test_review_findings_default_to_empty() -> None:
    review = Review(
        task_id="feat-x", reviewer=Reviewer.CODEX_REVIEW, verdict=Verdict.PASS
    )
    assert review.findings == ()


@pytest.mark.parametrize("cls", [Task, Run, Finding, Review, Lock, GateResult])
def test_data_classes_are_frozen_keyword_only_with_slots(cls: type) -> None:
    assert dataclasses.is_dataclass(cls)
    assert cls.__dataclass_params__.frozen is True  # type: ignore[attr-defined]
    assert "__slots__" in vars(cls)
    assert all(field.kw_only for field in dataclasses.fields(cls))


def test_task_is_immutable() -> None:
    task = make_task()
    with pytest.raises(dataclasses.FrozenInstanceError):
        task.state = TaskState.QUEUED  # type: ignore[misc]


def test_module_imports_only_the_standard_library() -> None:
    tree = ast.parse(inspect.getsource(models))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module is not None
            imported.add(node.module.split(".")[0])
    assert imported <= sys.stdlib_module_names


def test_public_functions_have_type_hints() -> None:
    hints = inspect.signature(can_transition)
    assert hints.return_annotation is not inspect.Signature.empty
    assert all(
        p.annotation is not inspect.Parameter.empty for p in hints.parameters.values()
    )
