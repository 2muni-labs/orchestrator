"""runner.append_note keeps the proxy denial note within the result limit."""

import dataclasses

import pytest

from orch import results
from orch.models import FailureReason, Outcome
from orch.results import TaskResult
from orch.runner import append_note

LIMIT = results.SUMMARY_LIMIT
NOTE = "프록시가 연결 1건을 거부했습니다: ~/.orch/x/logs/t.1.proxy.log"


def make(summary: str = "요약", unresolved: tuple[str, ...] = ()) -> TaskResult:
    return TaskResult(
        task_id="t",
        status=Outcome.DONE,
        failure_reason=None,
        summary=summary,
        changed_paths=("src/a.py",),
        out_of_scope_changes=("x/b.py",),
        verification_command="fake verify",
        verification_passed=True,
        unresolved=unresolved,
        log_path="~/.orch/x/logs/t.1.log",
    )


def total(result: TaskResult) -> int:
    return len(result.summary) + sum(map(len, result.unresolved))


def roundtrip(result: TaskResult) -> TaskResult:
    """The saved form must load again through the real parser."""
    return results.parse_result(dict(results.result_to_json(result)), "t")


def test_a_note_that_fits_is_only_appended() -> None:
    before = make(unresolved=("하나", "둘"))
    after = append_note(before, NOTE)
    assert after == dataclasses.replace(before, unresolved=("하나", "둘", NOTE))
    assert roundtrip(after) == after


def test_a_long_summary_is_cut_first_when_unresolved_is_empty() -> None:
    after = append_note(make(summary="가" * LIMIT), NOTE)
    assert after.unresolved == (NOTE,)
    assert after.summary == "가" * (LIMIT - len(NOTE))
    assert total(after) == LIMIT
    roundtrip(after)


def test_long_unresolved_items_are_cut_and_the_summary_emptied() -> None:
    before = make(summary="요약", unresolved=("나" * 690,))
    after = append_note(before, NOTE)
    assert after.unresolved == ("나" * (LIMIT - len(NOTE)), NOTE)
    assert after.summary == ""
    assert total(after) == LIMIT
    roundtrip(after)


def test_items_past_the_budget_are_dropped_in_order() -> None:
    first = "가" * (LIMIT - len(NOTE) - 10)
    before = make(summary="요약", unresolved=(first, "나" * 30, "다" * 30))
    after = append_note(before, NOTE)
    assert after.unresolved == (first, "나" * 10, NOTE)
    assert after.summary == ""
    roundtrip(after)


def test_an_empty_item_is_kept_while_budget_remains() -> None:
    before = make(summary="요약", unresolved=("", "나" * 690))
    after = append_note(before, NOTE)
    assert after.unresolved == ("", "나" * (LIMIT - len(NOTE)), NOTE)
    assert after.summary == ""


def test_an_empty_item_after_the_budget_is_dropped() -> None:
    before = make(summary="", unresolved=("나" * LIMIT, ""))
    after = append_note(before, NOTE)
    assert after.unresolved == ("나" * (LIMIT - len(NOTE)), NOTE)


def test_a_note_longer_than_the_limit_is_cut_and_stands_alone() -> None:
    note = "메" * (LIMIT + 50)
    after = append_note(make(summary="요약", unresolved=("하나",)), note)
    assert after.unresolved == ("메" * LIMIT,)
    assert after.summary == ""
    roundtrip(after)


def test_the_other_fields_and_the_input_are_unchanged() -> None:
    before = dataclasses.replace(
        make(summary="가" * 400, unresolved=("나" * 400,)),
        status=Outcome.FAILED,
        failure_reason=FailureReason.TIMEOUT,
        verification_passed=False,
    )
    after = append_note(before, NOTE)
    for field in dataclasses.fields(TaskResult):
        if field.name not in ("summary", "unresolved"):
            assert getattr(after, field.name) == getattr(before, field.name)
    assert before.unresolved == ("나" * 400,)
    assert before.summary == "가" * 400
    assert total(after) == LIMIT


@pytest.mark.parametrize("size", [0, 1, 299, 650, 699, 700])
def test_the_total_never_exceeds_the_limit(size: int) -> None:
    for summary_size in (0, LIMIT - size):
        before = make(summary="가" * summary_size, unresolved=("나" * size,))
        after = append_note(before, NOTE)
        assert total(after) <= LIMIT
        assert after.unresolved[-1] == NOTE
        roundtrip(after)
