import ast
import json
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from orch import prflow, render
from orch.models import (
    Finding,
    Gate,
    GateResult,
    Grade,
    Lane,
    Review,
    Reviewer,
    Severity,
    Task,
    TaskState,
    Verdict,
)
from orch.render import (
    build_body,
    elapsed_sec,
    format_elapsed,
    purpose_line,
    review_packet,
    status_lines,
    status_row,
    todo_text,
    write_todo,
)
from orch.scheduler import BlockedChain

NOW = datetime(2026, 11, 5, 12, 0, 0, tzinfo=UTC)
SHA = "318f3dd" + "0" * 33


def ago(seconds: int) -> str:
    return (NOW - timedelta(seconds=seconds)).isoformat()


def make_task(**overrides: object) -> Task:
    values: dict[str, object] = {
        "id": "feat-x",
        "target": "orchestrator",
        "title": "Add x",
        "grade": Grade.L2,
        "lane": Lane.B,
        "model": "m",
        "state": TaskState.RUNNING,
        "timeout_sec": 60,
        "created_at": ago(300),
        "updated_at": ago(60),
        "attempt": 1,
    }
    values.update(overrides)
    return Task(**values)


# Elapsed time


def test_a_live_task_counts_up_to_now() -> None:
    assert elapsed_sec(make_task(), NOW) == 300


@pytest.mark.parametrize("state", [TaskState.CLOSED, TaskState.DISCARDED])
def test_a_finished_task_stops_at_its_last_update(state: TaskState) -> None:
    assert elapsed_sec(make_task(state=state), NOW) == 240


def test_elapsed_rounds_down_and_never_goes_negative() -> None:
    created = (NOW - timedelta(seconds=1, milliseconds=900)).isoformat()
    assert elapsed_sec(make_task(created_at=created), NOW) == 1
    future = (NOW + timedelta(seconds=30)).isoformat()
    assert elapsed_sec(make_task(created_at=future), NOW) == 0


def test_elapsed_compares_across_time_zones() -> None:
    created = "2026-11-05T20:55:00+09:00"
    assert elapsed_sec(make_task(created_at=created), NOW) == 300


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0, "0s"),
        (59, "59s"),
        (60, "1m 00s"),
        (305, "5m 05s"),
        (3599, "59m 59s"),
        (3600, "1h 00m"),
        (3720, "1h 02m"),
        (90000, "25h 00m"),
    ],
)
def test_format_elapsed(seconds: int, text: str) -> None:
    assert format_elapsed(seconds) == text


# Status rows


def test_status_row_has_the_fields_in_order_and_is_json() -> None:
    row = status_row(make_task(pushed_sha=SHA, pr_url="https://x/pull/1"), NOW)
    assert list(row) == [
        "id",
        "target",
        "title",
        "state",
        "grade",
        "lane",
        "attempt",
        "pr_url",
        "elapsed_sec",
        "head_sha",
    ]
    assert row["elapsed_sec"] == 300
    assert row["head_sha"] == SHA
    decoded = json.loads(json.dumps(row))
    assert decoded["state"] == "running"
    assert decoded["grade"] == "L2"


def test_status_row_has_no_head_before_a_push() -> None:
    assert status_row(make_task(), NOW)["head_sha"] is None


def test_status_lines_show_tasks_then_chains() -> None:
    tasks = [
        make_task(id="a"),
        make_task(id="b", state=TaskState.PR_OPEN, pushed_sha=SHA),
    ]
    chains = [BlockedChain(task_id="c", state=TaskState.QUEUED, causes=("a", "b"))]
    assert status_lines(tasks, chains, NOW) == [
        "a running 5m 00s -",
        "b pr_open 5m 00s 318f3dd",
        "c queued: a, b",
    ]


def test_status_lines_are_empty_without_tasks() -> None:
    assert status_lines([], [], NOW) == []


# TODO.md contents

HEADER = (
    "# TODO\n"
    "\n"
    "자동 생성 파일입니다. 직접 수정하지 마세요. 상태의 원본은 `state.db`입니다.\n"
    "\n"
    "생성 시각: 2026-11-05T12:00:00+00:00\n"
    "\n"
    "| id | title | state | grade | lane | attempt | elapsed | head | PR |\n"
    "| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
)


def test_todo_text_has_the_exact_format() -> None:
    tasks = [
        make_task(id="a"),
        make_task(
            id="b",
            title="Fix | y",
            state=TaskState.CLOSED,
            grade=Grade.L3,
            pushed_sha=SHA,
            pr_url="https://github.com/o/r/pull/1",
            created_at=ago(3780),
        ),
    ]
    chains = [BlockedChain(task_id="c", state=TaskState.QUEUED, causes=("a", "b"))]
    assert todo_text(tasks, chains, NOW) == HEADER + (
        "| a | Add x | running | L2 | B | 1 | 5m 00s | - | - |\n"
        "| b | Fix \\| y | closed | L3 | B | 1 | 1h 02m | 318f3dd"
        " | https://github.com/o/r/pull/1 |\n"
        "\n"
        "## Blocked chains\n"
        "\n"
        "- c queued: a, b\n"
    )


def test_todo_text_ends_at_the_table_without_chains() -> None:
    assert todo_text([], [], NOW) == HEADER


def test_todo_text_is_the_same_for_the_same_input() -> None:
    tasks = [make_task()]
    assert todo_text(tasks, [], NOW) == todo_text(tasks, [], NOW)


def test_todo_text_leaves_out_paths() -> None:
    text = todo_text([make_task(worktree="/somewhere/worktrees/feat-x")], [], NOW)
    assert "/somewhere" not in text


# Writing TODO.md


def test_write_creates_parents_and_leaves_only_the_file(tmp_path: Path) -> None:
    path = tmp_path / "new" / "TODO.md"
    write_todo(path, "내용\n")
    assert path.read_bytes() == "내용\n".encode()
    assert [entry.name for entry in path.parent.iterdir()] == ["TODO.md"]


def test_the_target_keeps_its_old_contents_until_the_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "TODO.md"
    path.write_text("old", encoding="utf-8")
    real_replace = os.replace
    seen: dict[str, object] = {}

    def replace(source: str | Path, destination: str | Path) -> None:
        seen["old"] = path.read_text(encoding="utf-8")
        seen["new"] = Path(source).read_text(encoding="utf-8")
        seen["directory"] = Path(source).parent
        seen["destination"] = Path(destination)
        real_replace(source, destination)

    monkeypatch.setattr(render.os, "replace", replace)
    write_todo(path, "new")
    assert seen == {
        "old": "old",
        "new": "new",
        "directory": tmp_path,
        "destination": path,
    }
    assert path.read_text(encoding="utf-8") == "new"


def test_a_failed_replace_keeps_the_file_and_removes_the_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "TODO.md"
    path.write_text("old", encoding="utf-8")

    def replace(source: str | Path, destination: str | Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(render.os, "replace", replace)
    with pytest.raises(OSError, match="disk full"):
        write_todo(path, "new")
    assert path.read_text(encoding="utf-8") == "old"
    assert [entry.name for entry in tmp_path.iterdir()] == ["TODO.md"]


def test_a_failed_write_keeps_the_file_and_removes_the_temporary(
    tmp_path: Path,
) -> None:
    path = tmp_path / "TODO.md"
    path.write_text("old", encoding="utf-8")
    with pytest.raises(UnicodeError):
        write_todo(path, "lone surrogate \ud800")
    assert path.read_text(encoding="utf-8") == "old"
    assert [entry.name for entry in tmp_path.iterdir()] == ["TODO.md"]


def test_readers_never_see_a_half_written_file(tmp_path: Path) -> None:
    path = tmp_path / "TODO.md"
    # Large enough that a write in place would be seen partway through.
    contents = [letter * 2_000_000 for letter in "abcd"]
    write_todo(path, contents[0])
    stop = threading.Event()
    errors: list[BaseException] = []
    reads: list[int] = []

    def writer(text: str) -> None:
        try:
            for _ in range(10):
                write_todo(path, text)
        except BaseException as exc:
            errors.append(exc)

    def reader() -> None:
        try:
            while not stop.is_set():
                assert path.read_text(encoding="utf-8") in contents
                reads.append(1)
        except BaseException as exc:
            errors.append(exc)

    readers = [threading.Thread(target=reader) for _ in range(2)]
    writers = [threading.Thread(target=writer, args=(text,)) for text in contents]
    for thread in readers + writers:
        thread.start()
    for thread in writers:
        thread.join()
    stop.set()
    for thread in readers:
        thread.join()
    assert errors == []
    assert reads
    assert path.read_text(encoding="utf-8") in contents
    assert [entry.name for entry in tmp_path.iterdir()] == ["TODO.md"]


# The pull request body


def test_prflow_uses_the_body_from_render() -> None:
    assert prflow.build_body is render.build_body


def test_body_keeps_its_contents_after_the_move() -> None:
    gate = GateResult(
        task_id="feat-x", attempt=1, gate=Gate.G1, passed=True, detail=None, at=ago(0)
    )
    assert build_body(make_task(), changed=["b", "a"], gates=[gate], review=None) == (
        "## Summary\n\n"
        "Task `feat-x`: Add x\n\n"
        "## Changed files\n\n"
        "- `a`\n"
        "- `b`\n\n"
        "## Gates\n\n"
        "- G1: passed\n\n"
        "## Review\n\n"
        "Not required\n"
    )


# The review packet


def spec_body(purpose: str) -> str:
    return f"# Purpose\n\n{purpose}\n\n# Confirmed facts\n\n- A fact. Another. More.\n"


def gate_row(name: Gate, passed: bool) -> GateResult:
    return GateResult(
        task_id="feat-x", attempt=1, gate=name, passed=passed, detail=None, at=ago(0)
    )


def make_review(verdict: Verdict, *severities: Severity) -> Review:
    return Review(
        task_id="feat-x",
        reviewer=Reviewer.CLAUDE_REVIEWER,
        verdict=verdict,
        findings=tuple(
            Finding(severity=severity, path="src/app.py", summary="지적")
            for severity in severities
        ),
    )


def packet(**overrides: object) -> str:
    values: dict[str, object] = {
        "body": spec_body("Add x. It is small."),
        "modify": ("src/orch/render.py", "src/orch/prflow.py"),
        "gates": [gate_row(Gate.G1, True), gate_row(Gate.G2, True)],
        "review": None,
    }
    values.update(overrides)
    return review_packet(make_task(), **values)


def packet_line(text: str, key: str) -> str:
    lines = [line for line in text.split("\n") if line.startswith(f"- {key}: ")]
    assert len(lines) == 1
    return lines[0].removeprefix(f"- {key}: ")


def test_purpose_keeps_the_first_two_sentences() -> None:
    assert purpose_line(spec_body("First one. Second one! Third one.")) == (
        "First one. Second one!"
    )
    assert purpose_line(spec_body("Is it done? Yes. No.")) == "Is it done? Yes."
    assert purpose_line(spec_body("첫 문장이다。 둘째 문장이다. 셋째.")) == (
        "첫 문장이다。 둘째 문장이다."
    )


def test_purpose_keeps_a_shorter_text_whole() -> None:
    assert purpose_line(spec_body("Only one sentence.")) == "Only one sentence."
    assert purpose_line(spec_body("No final mark")) == "No final mark"
    assert purpose_line(spec_body("One. Two without a mark")) == (
        "One. Two without a mark"
    )


def test_purpose_is_one_line() -> None:
    body = spec_body("First   line\ncontinues\there.\n\nSecond\nsentence. Third.")
    assert purpose_line(body) == "First line continues here. Second sentence."


def test_a_dot_inside_a_word_does_not_end_a_sentence() -> None:
    body = spec_body("`github.py` reads v1.2 labels. It removes them. Third.")
    assert purpose_line(body) == "`github.py` reads v1.2 labels. It removes them."


def test_purpose_drops_comments() -> None:
    body = spec_body("<!-- One or two sentences. -->\nAdd x. <!--\nnote.\n--> Done.")
    assert purpose_line(body) == "Add x. Done."


def test_purpose_stops_at_the_next_section() -> None:
    assert purpose_line("# Purpose\nAdd x.\n# Confirmed facts\nA fact.\n") == "Add x."
    assert purpose_line("# Other\n\nNo.\n\n# Purpose\n\nAdd x.") == "Add x."
    body = "# Purpose\n\nAdd x.\n## Detail\nMore.\n\n# Interface\n\nNo. No.\n"
    assert purpose_line(body) == "Add x. ## Detail More."


@pytest.mark.parametrize(
    "body",
    [
        "",
        "# Confirmed facts\n\nA fact.\n",
        "# Purpose\n\n# Confirmed facts\n\nA fact.\n",
        "# Purpose\n\n<!-- One or two sentences. -->\n\n# Confirmed facts\n\nA.\n",
        "## Purpose\n\nA subsection is not the section.\n",
        "Text that mentions # Purpose in a line.\n",
    ],
)
def test_purpose_is_none_without_a_purpose_text(body: str) -> None:
    assert purpose_line(body) == "(none)"


def test_a_long_purpose_is_cut_to_300_characters() -> None:
    exact = "a" * 299 + "."
    assert purpose_line(spec_body(exact)) == exact
    long = "b" * 301
    cut = purpose_line(spec_body(long))
    assert cut == "b" * 297 + "..."
    assert len(cut) == 300
    two = "c" * 200 + ". " + "d" * 200 + ". Third."
    assert purpose_line(spec_body(two)) == ("c" * 200 + ". " + "d" * 95 + "...")


def test_packet_has_the_fixed_format() -> None:
    gates = [gate_row(Gate.G1, True), gate_row(Gate.G2, True), gate_row(Gate.G3, True)]
    assert packet(gates=gates) == (
        "## Review packet\n\n"
        "- task_id: `feat-x`\n"
        "- grade: L2\n"
        "- lane: B\n"
        "- purpose: Add x. It is small.\n"
        "- modify: `src/orch/render.py`, `src/orch/prflow.py`\n"
        "- gates: G1 passed, G2 passed, G3 passed\n"
        "- review: not required"
    )


def test_packet_names_the_grade_lane_and_task() -> None:
    task = make_task(id="m-other", grade=Grade.L3R, lane=Lane.D)
    text = review_packet(task, body="", modify=["a.py"], gates=[], review=None)
    assert packet_line(text, "task_id") == "`m-other`"
    assert packet_line(text, "grade") == "L3R"
    assert packet_line(text, "lane") == "D"
    assert packet_line(text, "purpose") == "(none)"
    assert packet_line(text, "modify") == "`a.py`"


def test_packet_keeps_the_order_of_modify() -> None:
    text = packet(modify=iter(["src/b.py", "src/a.py", "docs/**"]))
    assert packet_line(text, "modify") == "`src/b.py`, `src/a.py`, `docs/**`"


def test_packet_reports_a_gate_that_did_not_run() -> None:
    assert (
        packet_line(packet(gates=[]), "gates") == "G1 not run, G2 not run, G3 not run"
    )
    text = packet(gates=iter([gate_row(Gate.G2, False)]))
    assert packet_line(text, "gates") == "G1 not run, G2 failed, G3 not run"


def test_packet_uses_the_last_result_of_each_gate() -> None:
    gates = [
        gate_row(Gate.G1, False),
        gate_row(Gate.G3, True),
        gate_row(Gate.G1, True),
        gate_row(Gate.G3, False),
    ]
    assert packet_line(packet(gates=gates), "gates") == (
        "G1 passed, G2 not run, G3 failed"
    )


@pytest.mark.parametrize(
    ("gates", "expected"),
    [
        ([(Gate.G2, True), (Gate.G2_PRE_PR, False)], "failed"),
        ([(Gate.G2, False), (Gate.G2_PRE_PR, True)], "passed"),
        ([(Gate.G2_PRE_PR, True), (Gate.G2, False)], "failed"),
        ([(Gate.G2_PRE_PR, True)], "passed"),
        ([(Gate.G2_PRE_PR, False), (Gate.G2_PRE_PR, True)], "passed"),
    ],
)
def test_packet_joins_the_pre_pr_run_into_g2(
    gates: list[tuple[Gate, bool]], expected: str
) -> None:
    rows = [gate_row(name, passed) for name, passed in gates]
    text = packet(gates=rows)
    assert packet_line(text, "gates") == f"G1 not run, G2 {expected}, G3 not run"
    assert "G2_PRE_PR" not in text


@pytest.mark.parametrize(
    ("review", "expected"),
    [
        (None, "not required"),
        (make_review(Verdict.PASS), "PASS"),
        (make_review(Verdict.PASS, Severity.P3, Severity.P2), "PASS, P2: 1, P3: 1"),
        (make_review(Verdict.BLOCK, Severity.P1, Severity.P1), "BLOCK, P1: 2"),
        (
            make_review(Verdict.BLOCK, Severity.P2, Severity.P0, Severity.P2),
            "BLOCK, P0: 1, P2: 2",
        ),
    ],
)
def test_packet_gives_the_verdict_and_counts(
    review: Review | None, expected: str
) -> None:
    text = packet(review=review)
    assert packet_line(text, "review") == expected
    assert "지적" not in text


def test_body_starts_with_the_packet_when_given_one() -> None:
    gate = gate_row(Gate.G1, True)
    plain = build_body(make_task(), changed=["a"], gates=[gate], review=None)
    with_packet = build_body(
        make_task(), changed=["a"], gates=[gate], review=None, packet="## Review packet"
    )
    assert with_packet == "## Review packet\n\n" + plain
    without = build_body(
        make_task(), changed=["a"], gates=[gate], review=None, packet=None
    )
    assert without == plain


def test_render_sends_nothing_and_imports_only_what_it_may() -> None:
    tree = ast.parse(Path(render.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    assert {name for name in names if name.startswith("orch")} <= {
        "orch.models",
        "orch.review",
        "orch.scheduler",
    }
    assert not names & {"subprocess", "socket", "urllib", "http"}
