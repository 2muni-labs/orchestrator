import ast
import copy
import json
import os
import threading
from pathlib import Path

import pytest

from orch import results
from orch.models import FailureReason, Finding, Outcome, Review, Reviewer, Severity
from orch.models import Verdict as ReviewVerdict
from orch.results import (
    MAX_REPORT_BYTES,
    SUMMARY_LIMIT,
    ReportError,
    TaskResult,
    collect_result,
    load_result,
    load_review,
    parse_result,
    parse_review,
    result_to_json,
    review_from_text,
    substitute_result,
    write_result,
    write_review,
)

TASK = "m4-results"
SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"
THREADS = 8

VALID_RESULT = {
    "task_id": TASK,
    "status": "DONE",
    "failure_reason": None,
    "summary": "구현을 마쳤습니다.",
    "changed_paths": ["src/orch/results.py"],
    "out_of_scope_changes": [],
    "verification": {"command": ".venv/bin/python -m pytest", "passed": True},
    "unresolved": [],
    "log_path": "~/.orch/hub/logs/m4-results.log",
}

VALID_REVIEW = {
    "task_id": TASK,
    "reviewer": "claude-reviewer",
    "verdict": "PASS",
    "findings": [
        {"severity": "P2", "path": "src/orch/a.py", "summary": "구조 개선."},
        {"severity": "P3", "path": "tests/", "summary": "취향."},
    ],
}


def changed(base: dict, **changes: object) -> dict:
    data = copy.deepcopy(base)
    data.update(changes)
    return data


def without(base: dict, key: str) -> dict:
    data = copy.deepcopy(base)
    del data[key]
    return data


def write_json(path: Path, data: object) -> Path:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


# Schema agreement


def test_result_enums_match_the_schema() -> None:
    schema = json.loads((SCHEMAS / "result.json").read_text(encoding="utf-8"))
    properties = schema["properties"]
    assert set(properties["status"]["enum"]) == {o.value for o in Outcome}
    assert set(properties["failure_reason"]["enum"]) == {
        r.value for r in FailureReason
    } | {None}


def test_valid_sample_has_exactly_the_schema_keys() -> None:
    schema = json.loads((SCHEMAS / "result.json").read_text(encoding="utf-8"))
    assert set(VALID_RESULT) == set(schema["required"]) == set(schema["properties"])


def test_summary_limit_matches_the_requirement() -> None:
    assert SUMMARY_LIMIT == 700


# parse_result


def test_parse_result_returns_every_field() -> None:
    assert parse_result(VALID_RESULT, TASK) == TaskResult(
        task_id=TASK,
        status=Outcome.DONE,
        failure_reason=None,
        summary="구현을 마쳤습니다.",
        changed_paths=("src/orch/results.py",),
        out_of_scope_changes=(),
        verification_command=".venv/bin/python -m pytest",
        verification_passed=True,
        unresolved=(),
        log_path="~/.orch/hub/logs/m4-results.log",
    )


@pytest.mark.parametrize("key", sorted(VALID_RESULT))
def test_every_key_is_required(key: str) -> None:
    with pytest.raises(ReportError):
        parse_result(without(VALID_RESULT, key), TASK)


def test_unknown_key_raises() -> None:
    with pytest.raises(ReportError):
        parse_result(changed(VALID_RESULT, extra=1), TASK)


@pytest.mark.parametrize("data", [None, [], "text", 1, True])
def test_non_objects_raise(data: object) -> None:
    with pytest.raises(ReportError):
        parse_result(data, TASK)


def test_other_task_id_raises() -> None:
    with pytest.raises(ReportError):
        parse_result(changed(VALID_RESULT, task_id="other"), TASK)


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "NOPE"},
        {"status": 1},
        {"status": None},
        {"status": ["DONE"]},
        {"status": {"a": 1}},
        {"status": "FAILED", "failure_reason": "nope"},
        {"status": "FAILED", "failure_reason": ["timeout"]},
        {"status": "FAILED", "failure_reason": {"a": 1}},
        {"status": "FAILED", "failure_reason": 1},
        {"summary": 1},
        {"summary": None},
        {"changed_paths": "a"},
        {"changed_paths": [1]},
        {"out_of_scope_changes": None},
        {"out_of_scope_changes": [None]},
        {"unresolved": "a"},
        {"unresolved": [1]},
        {"log_path": None},
        {"log_path": 1},
        {"verification": None},
        {"verification": []},
        {"verification": {"command": "x"}},
        {"verification": {"passed": True}},
        {"verification": {"command": "x", "passed": True, "extra": 1}},
        {"verification": {"command": 1, "passed": True}},
        {"verification": {"command": "x", "passed": 1}},
    ],
)
def test_wrong_types_raise_report_error(changes: dict) -> None:
    with pytest.raises(ReportError):
        parse_result(changed(VALID_RESULT, **changes), TASK)


@pytest.mark.parametrize("status", ["FAILED", "BLOCKED"])
def test_failed_and_blocked_require_a_reason(status: str) -> None:
    with pytest.raises(ReportError):
        parse_result(changed(VALID_RESULT, status=status), TASK)
    result = parse_result(
        changed(VALID_RESULT, status=status, failure_reason="out_of_scope"), TASK
    )
    assert result.failure_reason is FailureReason.OUT_OF_SCOPE


@pytest.mark.parametrize("status", ["DONE", "PARTIAL", "BLOCKED_QUOTA"])
def test_other_statuses_forbid_a_reason(status: str) -> None:
    assert parse_result(changed(VALID_RESULT, status=status), TASK).status == status
    with pytest.raises(ReportError):
        parse_result(
            changed(VALID_RESULT, status=status, failure_reason="timeout"), TASK
        )


def test_summary_and_unresolved_share_the_limit() -> None:
    fits = changed(VALID_RESULT, summary="가" * 690, unresolved=["나" * 5, "다" * 5])
    assert parse_result(fits, TASK).summary == "가" * 690
    with pytest.raises(ReportError):
        parse_result(changed(fits, summary="가" * 691), TASK)
    with pytest.raises(ReportError):
        parse_result(changed(VALID_RESULT, summary="가" * 701), TASK)


# load_result


def test_load_result_reads_a_file(tmp_path: Path) -> None:
    path = write_json(tmp_path / "r.json", VALID_RESULT)
    assert load_result(path, TASK).task_id == TASK


def test_load_result_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ReportError):
        load_result(tmp_path / "missing.json", TASK)


@pytest.mark.parametrize("content", [b"", b"{not json", b"\xff\xfe", b"[]"])
def test_load_result_malformed_file_raises(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "r.json"
    path.write_bytes(content)
    with pytest.raises(ReportError):
        load_result(path, TASK)


def test_load_result_refuses_a_directory(tmp_path: Path) -> None:
    with pytest.raises(ReportError):
        load_result(tmp_path, TASK)


def test_load_result_refuses_a_symlink(tmp_path: Path) -> None:
    target = write_json(tmp_path / "real.json", VALID_RESULT)
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(ReportError):
        load_result(link, TASK)


def test_load_result_refuses_an_oversized_file(tmp_path: Path) -> None:
    path = tmp_path / "r.json"
    path.write_bytes(b" " * MAX_REPORT_BYTES + json.dumps(VALID_RESULT).encode())
    with pytest.raises(ReportError):
        load_result(path, TASK)


# write_result


def test_write_and_load_round_trip_keeps_korean(tmp_path: Path) -> None:
    path = tmp_path / "new" / "dir" / "r.json"
    result = parse_result(VALID_RESULT, TASK)
    write_result(path, result)
    assert load_result(path, TASK) == result
    text = path.read_text(encoding="utf-8")
    assert "구현을 마쳤습니다." in text
    assert text.endswith("\n")
    assert json.loads(text) == VALID_RESULT


def test_result_to_json_matches_the_schema_shape() -> None:
    result = parse_result(
        changed(VALID_RESULT, status="FAILED", failure_reason="timeout"), TASK
    )
    data = dict(result_to_json(result))
    assert data["status"] == "FAILED"
    assert data["failure_reason"] == "timeout"
    assert parse_result(json.loads(json.dumps(data)), TASK) == result


def test_failed_write_keeps_the_old_file_and_leaves_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "r.json"
    write_result(path, parse_result(VALID_RESULT, TASK))
    before = path.read_bytes()

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("no space")

    monkeypatch.setattr(os, "replace", fail)
    other = changed(VALID_RESULT, summary="다른 내용")
    with pytest.raises(OSError):
        write_result(path, parse_result(other, TASK))
    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["r.json"]


def test_concurrent_writes_leave_one_complete_file(tmp_path: Path) -> None:
    path = tmp_path / "r.json"
    barrier = threading.Barrier(THREADS)
    errors: list[BaseException] = []

    def attempt(number: int) -> None:
        result = parse_result(changed(VALID_RESULT, summary=f"작성자 {number}"), TASK)
        barrier.wait()
        try:
            for _ in range(20):
                write_result(path, result)
                load_result(path, TASK)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=attempt, args=(n,)) for n in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert [p.name for p in tmp_path.iterdir()] == ["r.json"]


# substitute_result


def test_substitute_result_fills_the_empty_fields() -> None:
    result = substitute_result(
        task_id=TASK,
        status=Outcome.FAILED,
        failure_reason=FailureReason.REPORT_MISSING,
        summary="결과 보고가 없습니다.",
        verification_command="x",
        log_path="~/l.log",
    )
    assert result == TaskResult(
        task_id=TASK,
        status=Outcome.FAILED,
        failure_reason=FailureReason.REPORT_MISSING,
        summary="결과 보고가 없습니다.",
        changed_paths=(),
        out_of_scope_changes=(),
        verification_command="x",
        verification_passed=False,
        unresolved=(),
        log_path="~/l.log",
    )


def test_substitute_result_cuts_a_long_summary_to_the_limit() -> None:
    result = substitute_result(
        task_id=TASK,
        status=Outcome.BLOCKED_QUOTA,
        failure_reason=None,
        summary="가" * 900,
        verification_command="x",
        log_path="~/l.log",
    )
    assert len(result.summary) == SUMMARY_LIMIT
    assert parse_result(dict(result_to_json(result)), TASK) == result


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (Outcome.FAILED, None),
        (Outcome.BLOCKED, None),
        (Outcome.DONE, FailureReason.TIMEOUT),
        (Outcome.BLOCKED_QUOTA, FailureReason.TIMEOUT),
    ],
)
def test_substitute_result_refuses_inconsistent_status_and_reason(
    status: Outcome, reason: FailureReason | None
) -> None:
    with pytest.raises(ReportError):
        substitute_result(
            task_id=TASK,
            status=status,
            failure_reason=reason,
            summary="s",
            verification_command="x",
            log_path="~/l.log",
        )


# collect_result


@pytest.fixture
def places(tmp_path: Path) -> tuple[Path, Path, Path]:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    return (
        worktree / ".orch-result.json",
        tmp_path / "results" / f"{TASK}.json",
        tmp_path / "logs" / f"{TASK}.rejected-result.json",
    )


def test_collect_moves_a_valid_report(places: tuple[Path, Path, Path]) -> None:
    source, destination, rejected = places
    write_json(source, VALID_RESULT)
    result = collect_result(source, destination, rejected, TASK)
    assert result.task_id == TASK
    assert not source.exists()
    assert load_result(destination, TASK) == result
    assert not rejected.exists()


def test_collect_replaces_an_earlier_result(places: tuple[Path, Path, Path]) -> None:
    source, destination, rejected = places
    destination.parent.mkdir(parents=True)
    write_json(destination, changed(VALID_RESULT, summary="이전"))
    write_json(source, VALID_RESULT)
    collect_result(source, destination, rejected, TASK)
    assert load_result(destination, TASK).summary == VALID_RESULT["summary"]


def test_collect_missing_report_raises_and_creates_nothing(
    places: tuple[Path, Path, Path],
) -> None:
    source, destination, rejected = places
    with pytest.raises(ReportError):
        collect_result(source, destination, rejected, TASK)
    assert not destination.exists()
    assert not rejected.exists()


@pytest.mark.parametrize(
    "content",
    [b"{not json", json.dumps(changed(VALID_RESULT, task_id="other")).encode()],
    ids=["not_json", "other_task"],
)
def test_collect_moves_an_invalid_report_aside(
    places: tuple[Path, Path, Path], content: bytes
) -> None:
    source, destination, rejected = places
    source.write_bytes(content)
    with pytest.raises(ReportError):
        collect_result(source, destination, rejected, TASK)
    assert not source.exists()
    assert not destination.exists()
    assert rejected.read_bytes() == content


def test_collect_moves_a_symlink_aside_without_following_it(
    places: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    source, destination, rejected = places
    target = write_json(tmp_path / "secret.json", VALID_RESULT)
    source.symlink_to(target)
    with pytest.raises(ReportError):
        collect_result(source, destination, rejected, TASK)
    assert not destination.exists()
    assert target.is_file()
    assert rejected.is_symlink()


# parse_review


def test_parse_review_returns_the_model() -> None:
    assert parse_review(VALID_REVIEW, TASK) == Review(
        task_id=TASK,
        reviewer=Reviewer.CLAUDE_REVIEWER,
        verdict=ReviewVerdict.PASS,
        findings=(
            Finding(severity=Severity.P2, path="src/orch/a.py", summary="구조 개선."),
            Finding(severity=Severity.P3, path="tests/", summary="취향."),
        ),
    )


def test_review_without_findings_passes() -> None:
    assert parse_review(changed(VALID_REVIEW, findings=[]), TASK).findings == ()


@pytest.mark.parametrize("key", sorted(VALID_REVIEW))
def test_every_review_key_is_required(key: str) -> None:
    with pytest.raises(ReportError):
        parse_review(without(VALID_REVIEW, key), TASK)


@pytest.mark.parametrize("severity", ["P0", "P1"])
def test_blocking_finding_requires_block(severity: str) -> None:
    finding = {"severity": severity, "path": "src/a.py", "summary": "s"}
    with pytest.raises(ReportError):
        parse_review(changed(VALID_REVIEW, findings=[finding]), TASK)
    review = parse_review(
        changed(VALID_REVIEW, verdict="BLOCK", findings=[finding]), TASK
    )
    assert review.verdict is ReviewVerdict.BLOCK


def test_block_without_a_blocking_finding_raises() -> None:
    with pytest.raises(ReportError):
        parse_review(changed(VALID_REVIEW, verdict="BLOCK"), TASK)


@pytest.mark.parametrize(
    "changes",
    [
        {"task_id": "other"},
        {"reviewer": "someone"},
        {"reviewer": ["claude-reviewer"]},
        {"verdict": "MAYBE"},
        {"verdict": {"a": 1}},
        {"findings": None},
        {"findings": "x"},
        {"findings": [1]},
        {"findings": [{"severity": "P2", "path": "a"}]},
        {"findings": [{"severity": "P9", "path": "a", "summary": "s"}]},
        {"findings": [{"severity": ["P2"], "path": "a", "summary": "s"}]},
        {"findings": [{"severity": "P2", "path": 1, "summary": "s"}]},
        {"findings": [{"severity": "P2", "path": "a", "summary": 1}]},
        {"findings": [{"severity": "P2", "path": "a", "summary": ""}]},
        {"findings": [{"severity": "P2", "path": "a", "summary": "s", "x": 1}]},
        {"extra": 1},
    ],
)
def test_invalid_reviews_raise_report_error(changes: dict) -> None:
    with pytest.raises(ReportError):
        parse_review(changed(VALID_REVIEW, **changes), TASK)


@pytest.mark.parametrize(
    "path",
    ["", "/abs/a.py", "~/a.py", "../a.py", "src/../../a.py", "C:\\x\\a.py", "a\\b.py"],
)
def test_finding_path_must_be_repository_relative(path: str) -> None:
    finding = {"severity": "P2", "path": path, "summary": "s"}
    with pytest.raises(ReportError):
        parse_review(changed(VALID_REVIEW, findings=[finding]), TASK)


@pytest.mark.parametrize("path", ["tests/", "m4-results.diff", "src/orch/기술 설계.md"])
def test_finding_paths_that_are_allowed(path: str) -> None:
    finding = {"severity": "P2", "path": path, "summary": "s"}
    review = parse_review(changed(VALID_REVIEW, findings=[finding]), TASK)
    assert review.findings[0].path == path


def test_review_from_text() -> None:
    text = json.dumps(VALID_REVIEW, ensure_ascii=False)
    assert review_from_text(text, TASK) == parse_review(VALID_REVIEW, TASK)


@pytest.mark.parametrize("text", ["", "{not json", "Here is the review: {}", "[]"])
def test_review_from_malformed_text_raises(text: str) -> None:
    with pytest.raises(ReportError):
        review_from_text(text, TASK)


def test_review_file_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "new" / "r.json"
    review = parse_review(VALID_REVIEW, TASK)
    write_review(path, review)
    assert load_review(path, TASK) == review
    assert json.loads(path.read_text(encoding="utf-8")) == VALID_REVIEW


def test_load_review_missing_or_malformed_raises(tmp_path: Path) -> None:
    with pytest.raises(ReportError):
        load_review(tmp_path / "missing.json", TASK)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ReportError):
        load_review(bad, TASK)


# Module rules


def test_module_imports_only_the_standard_library_and_models() -> None:
    tree = ast.parse(Path(results.__file__).read_text(encoding="utf-8"))
    orch_imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.split(".")[0] == "orch"
    }
    assert orch_imports == {"orch.models"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] != "orch"
