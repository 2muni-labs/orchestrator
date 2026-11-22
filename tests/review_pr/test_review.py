import json
import sqlite3
from pathlib import Path

import pytest

from orch import results
from orch.config import TargetPaths, ensure_target
from orch.models import Grade, Lane, Severity, Task, TaskState, Verdict
from orch.review import (
    OVERRIDE_HEADING,
    OVERRIDE_LABEL,
    ReviewFlowError,
    Triage,
    TriageDecision,
    TriageVerdict,
    has_blocking_review,
    load_triage,
    override_section,
    parse_triage,
    save_review,
)
from orch.store import connect, get_task, insert_task

TASK = "feat-x"
TARGET = "sample"
FAKE_HOME = Path("/", "Users", "tester")
REDACT = {"home": FAKE_HOME, "orch_home": FAKE_HOME / ".orch"}
TOKEN = "gh" + "p_" + "t0ken" * 8

WRONG = """
[[decisions]]
severity = "P0"
path = "src/app.py"
verdict = "wrong_finding"
decision = "The caller validates this value."
"""

INSUFFICIENT = """
[[decisions]]
severity = "P1"
path = "tests/test_x.py"
verdict = "insufficient_tests"
decision = "The code is right; only a check is missing."
"""


def triage_text(*tables: str, follow_up: str | None = None, body: str = "") -> str:
    head = f'follow_up = "{follow_up}"\n' if follow_up is not None else ""
    return "+++\n" + head + "".join(tables) + "+++\n" + body


def finding(severity: str, path: str) -> dict[str, str]:
    return {"severity": severity, "path": path, "summary": "지적 요약"}


def review_text(*findings: dict[str, str], task_id: str = TASK) -> str:
    blocking = any(item["severity"] in ("P0", "P1") for item in findings)
    return json.dumps(
        {
            "task_id": task_id,
            "reviewer": "claude-reviewer",
            "verdict": "BLOCK" if blocking else "PASS",
            "findings": list(findings),
        }
    )


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return connect(tmp_path / "state.db")


@pytest.fixture
def paths(tmp_path: Path) -> TargetPaths:
    return ensure_target(tmp_path / "runtime", TARGET, "owner/sample")


def add_task(
    conn: sqlite3.Connection,
    task_id: str = TASK,
    state: TaskState = TaskState.REVIEWING,
    *,
    target: str = TARGET,
) -> None:
    insert_task(
        conn,
        Task(
            id=task_id,
            target=target,
            title="Add feature X",
            grade=Grade.L3,
            lane=Lane.B,
            model="m",
            state=state,
            attempt=1,
            timeout_sec=60,
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        ),
    )


def state_of(conn: sqlite3.Connection, task_id: str = TASK) -> TaskState:
    task = get_task(conn, task_id)
    assert task is not None
    return task.state


def notes_of(conn: sqlite3.Connection) -> list[str | None]:
    rows = conn.execute(
        "SELECT note FROM events WHERE task_id = ? ORDER BY id", (TASK,)
    )
    return [row[0] for row in rows]


def save(
    conn: sqlite3.Connection,
    paths: TargetPaths,
    text: str,
    triage_file: Path | None = None,
) -> TaskState:
    return save_review(conn, TASK, text, paths=paths, triage_file=triage_file, **REDACT)


def write_triage(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "draft.triage.md"
    path.write_text(text, encoding="utf-8")
    return path


# parse_triage


def test_parse_reads_each_decision() -> None:
    triage = parse_triage(
        triage_text(WRONG, INSUFFICIENT, follow_up="feat-x-tests", body="\nNotes.\n")
    )
    assert triage == Triage(
        follow_up="feat-x-tests",
        decisions=(
            TriageDecision(
                severity=Severity.P0,
                path="src/app.py",
                verdict=TriageVerdict.WRONG_FINDING,
                decision="The caller validates this value.",
            ),
            TriageDecision(
                severity=Severity.P1,
                path="tests/test_x.py",
                verdict=TriageVerdict.INSUFFICIENT_TESTS,
                decision="The code is right; only a check is missing.",
            ),
        ),
        body="\nNotes.\n",
    )


def test_parse_accepts_wrong_findings_without_a_follow_up() -> None:
    triage = parse_triage(triage_text(WRONG))
    assert triage.follow_up is None
    assert triage.body == ""


@pytest.mark.parametrize(
    "text",
    [
        "",
        "no front matter\n",
        "+++\nnot toml [\n+++\n",
        "+++\n" + WRONG,
        "+++\n+++\n",
        '+++\nkind = "wrong_finding"\n' + WRONG + "+++\n",
        "+++\nextra = 1\n" + WRONG + "+++\n",
        "+++\ndecisions = []\n+++\n",
        '+++\ndecisions = "x"\n+++\n',
    ],
)
def test_parse_refuses_a_malformed_file(text: str) -> None:
    with pytest.raises(ReviewFlowError):
        parse_triage(text)


@pytest.mark.parametrize(
    "old, new",
    [
        ('severity = "P0"\n', ""),
        ('path = "src/app.py"\n', ""),
        ('verdict = "wrong_finding"\n', ""),
        ('decision = "The caller validates this value."\n', ""),
        ('severity = "P0"', 'severity = "P2"'),
        ('severity = "P0"', 'severity = "P3"'),
        ('severity = "P0"', 'severity = "P9"'),
        ('severity = "P0"', "severity = 0"),
        ('path = "src/app.py"', 'path = ""'),
        ('path = "src/app.py"', "path = 1"),
        ('verdict = "wrong_finding"', 'verdict = "implementation_defect"'),
        ('verdict = "wrong_finding"', "verdict = 1"),
        ('decision = "The caller validates this value."', 'decision = ""'),
        ('decision = "The caller validates this value."', 'decision = "   "'),
        ('decision = "The caller validates this value."', "decision = 1"),
        ('decision = "The caller validates this value."', 'decision = "두 단어"'),
        ('decision = "The caller validates this value."', 'decision = "a\\nb"'),
        ('verdict = "wrong_finding"', 'verdict = "wrong_finding"\nextra = 1'),
    ],
)
def test_parse_refuses_a_bad_decision(old: str, new: str) -> None:
    assert old in WRONG
    with pytest.raises(ReviewFlowError):
        parse_triage(triage_text(WRONG.replace(old, new)))


def test_parse_refuses_a_body_that_is_not_english() -> None:
    with pytest.raises(ReviewFlowError):
        parse_triage(triage_text(WRONG, body="\n한국어 본문\n"))


@pytest.mark.parametrize("path", ["src/test_x.py", "test/x.py", "tests", "testsuite/a"])
def test_insufficient_tests_applies_only_to_findings_under_tests(path: str) -> None:
    table = INSUFFICIENT.replace("tests/test_x.py", path)
    with pytest.raises(ReviewFlowError):
        parse_triage(triage_text(table, follow_up="feat-x-tests"))


def test_insufficient_tests_needs_a_follow_up() -> None:
    with pytest.raises(ReviewFlowError):
        parse_triage(triage_text(INSUFFICIENT))
    with pytest.raises(ReviewFlowError):
        parse_triage(triage_text(WRONG, INSUFFICIENT))


def test_a_follow_up_needs_an_insufficient_tests_decision() -> None:
    with pytest.raises(ReviewFlowError):
        parse_triage(triage_text(WRONG, follow_up="feat-x-tests"))


@pytest.mark.parametrize("follow_up", ["", "../x", "a b"])
def test_parse_refuses_an_invalid_follow_up_id(follow_up: str) -> None:
    with pytest.raises(ReviewFlowError):
        parse_triage(triage_text(INSUFFICIENT, follow_up=follow_up))


def test_wrong_finding_may_name_a_tests_path() -> None:
    table = WRONG.replace("src/app.py", "tests/test_x.py")
    assert parse_triage(triage_text(table)).follow_up is None


def test_load_triage_reads_a_file(tmp_path: Path) -> None:
    path = write_triage(tmp_path, triage_text(WRONG))
    assert load_triage(path) == parse_triage(triage_text(WRONG))


def test_load_triage_reports_an_unreadable_file(tmp_path: Path) -> None:
    with pytest.raises(ReviewFlowError):
        load_triage(tmp_path / "missing.md")
    bad = tmp_path / "bad.md"
    bad.write_bytes(b"\xff\xfe")
    with pytest.raises(ReviewFlowError):
        load_triage(bad)


# override_section


def test_section_starts_with_the_heading_and_lists_each_decision() -> None:
    section = override_section(
        parse_triage(
            triage_text(
                WRONG, INSUFFICIENT, follow_up="feat-x-tests", body="\nNotes.\n"
            )
        )
    )
    assert section.startswith(OVERRIDE_HEADING + "\n")
    assert "Follow-up task: `feat-x-tests`" in section
    assert (
        "- P0 `src/app.py` (wrong_finding): The caller validates this value." in section
    )
    assert (
        "- P1 `tests/test_x.py` (insufficient_tests): "
        "The code is right; only a check is missing." in section
    )
    assert "Notes." in section


def test_section_without_a_follow_up_omits_the_line() -> None:
    section = override_section(parse_triage(triage_text(WRONG)))
    assert "Follow-up task" not in section


def test_the_label_and_heading_are_the_agreed_names() -> None:
    assert OVERRIDE_LABEL == "review-overridden"
    assert OVERRIDE_HEADING == "## Review BLOCK overridden"


# has_blocking_review


def test_no_saved_review_is_not_blocking(paths: TargetPaths) -> None:
    assert has_blocking_review(paths, TASK) is False


def test_a_saved_block_is_blocking(paths: TargetPaths) -> None:
    review = results.parse_review(json.loads(review_text(finding("P1", "a.py"))), TASK)
    results.write_review(paths.review_path(TASK), review)
    assert has_blocking_review(paths, TASK) is True


def test_a_saved_pass_is_not_blocking(paths: TargetPaths) -> None:
    review = results.parse_review(json.loads(review_text(finding("P2", "a.py"))), TASK)
    results.write_review(paths.review_path(TASK), review)
    assert has_blocking_review(paths, TASK) is False


def test_an_unreadable_review_raises_rather_than_passing(paths: TargetPaths) -> None:
    paths.review_path(TASK).write_text("not json", encoding="utf-8")
    with pytest.raises(ReviewFlowError):
        has_blocking_review(paths, TASK)


# save_review without a triage


def test_pass_moves_to_ready_and_stores_the_review(
    conn: sqlite3.Connection, paths: TargetPaths
) -> None:
    add_task(conn)
    assert save(conn, paths, review_text()) is TaskState.READY
    assert state_of(conn) is TaskState.READY
    saved = results.load_review(paths.review_path(TASK), TASK)
    assert saved.verdict is Verdict.PASS


def test_block_moves_to_judging_and_stores_the_review(
    conn: sqlite3.Connection, paths: TargetPaths
) -> None:
    """A confirmed defect takes this route: no triage, then retry or discard."""
    add_task(conn)
    text = review_text(finding("P0", "src/app.py"))
    assert save(conn, paths, text) is TaskState.JUDGING
    assert state_of(conn) is TaskState.JUDGING
    assert results.load_review(paths.review_path(TASK), TASK).verdict is Verdict.BLOCK


def test_p2_and_p3_are_recorded_in_the_event(
    conn: sqlite3.Connection, paths: TargetPaths
) -> None:
    add_task(conn)
    save(conn, paths, review_text(finding("P2", "a.py"), finding("P3", "b.py")))
    note = notes_of(conn)[-1]
    assert note is not None
    assert "P2: 1" in note
    assert "P3: 1" in note


def test_no_minor_findings_leave_no_note(
    conn: sqlite3.Connection, paths: TargetPaths
) -> None:
    add_task(conn)
    save(conn, paths, review_text())
    assert notes_of(conn)[-1] is None


@pytest.mark.parametrize(
    "state",
    [
        TaskState.JUDGING,
        TaskState.GATING,
        TaskState.READY,
        TaskState.PR_OPEN,
        TaskState.DISCARDED,
    ],
)
def test_save_needs_a_reviewing_task(
    conn: sqlite3.Connection, paths: TargetPaths, state: TaskState
) -> None:
    add_task(conn, state=state)
    with pytest.raises(ReviewFlowError):
        save(conn, paths, review_text())
    assert state_of(conn) is state
    assert not paths.review_path(TASK).exists()


def test_save_needs_a_task(conn: sqlite3.Connection, paths: TargetPaths) -> None:
    with pytest.raises(ReviewFlowError):
        save(conn, paths, review_text())


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        "[]",
        review_text(task_id="other-task"),
        json.dumps(
            {
                "task_id": TASK,
                "reviewer": "claude-reviewer",
                "verdict": "PASS",
                "findings": [finding("P0", "a.py")],
            }
        ),
        json.dumps(
            {
                "task_id": TASK,
                "reviewer": "claude-reviewer",
                "verdict": "BLOCK",
                "findings": [],
            }
        ),
    ],
)
def test_save_refuses_an_invalid_review(
    conn: sqlite3.Connection, paths: TargetPaths, text: str
) -> None:
    add_task(conn)
    with pytest.raises(ReviewFlowError):
        save(conn, paths, text)
    assert state_of(conn) is TaskState.REVIEWING
    assert not paths.review_path(TASK).exists()


def test_save_removes_a_stale_triage(
    conn: sqlite3.Connection, paths: TargetPaths
) -> None:
    """Without this, a later pull request could present an old override."""
    add_task(conn)
    paths.triage_path(TASK).write_text("old", encoding="utf-8")
    save(conn, paths, review_text())
    assert not paths.triage_path(TASK).exists()


# save_review with a triage


def test_triage_moves_a_block_to_ready_and_stores_both(
    conn: sqlite3.Connection, paths: TargetPaths, tmp_path: Path
) -> None:
    add_task(conn)
    text = triage_text(WRONG)
    file = write_triage(tmp_path, text)
    state = save(conn, paths, review_text(finding("P0", "src/app.py")), file)
    assert state is TaskState.READY
    assert state_of(conn) is TaskState.READY
    assert results.load_review(paths.review_path(TASK), TASK).verdict is Verdict.BLOCK
    assert paths.triage_path(TASK).read_text(encoding="utf-8") == text


def test_override_is_recorded_in_the_event(
    conn: sqlite3.Connection, paths: TargetPaths, tmp_path: Path
) -> None:
    add_task(conn)
    file = write_triage(tmp_path, triage_text(WRONG))
    save(conn, paths, review_text(finding("P0", "src/app.py")), file)
    note = notes_of(conn)[-1]
    assert note is not None
    assert "BLOCK overridden" in note


def test_triage_already_in_its_place_is_accepted(
    conn: sqlite3.Connection, paths: TargetPaths
) -> None:
    add_task(conn)
    text = triage_text(WRONG)
    file = paths.triage_path(TASK)
    file.write_text(text, encoding="utf-8")
    save(conn, paths, review_text(finding("P0", "src/app.py")), file)
    assert file.read_text(encoding="utf-8") == text


def test_each_finding_gets_its_own_verdict(
    conn: sqlite3.Connection, paths: TargetPaths, tmp_path: Path
) -> None:
    add_task(conn)
    add_task(conn, "feat-x-tests", TaskState.PREPARED)
    file = write_triage(
        tmp_path, triage_text(WRONG, INSUFFICIENT, follow_up="feat-x-tests")
    )
    text = review_text(
        finding("P0", "src/app.py"),
        finding("P1", "tests/test_x.py"),
        finding("P2", "src/other.py"),
    )
    assert save(conn, paths, text, file) is TaskState.READY


def assert_refused(
    conn: sqlite3.Connection, paths: TargetPaths, text: str, file: Path
) -> None:
    with pytest.raises(ReviewFlowError):
        save(conn, paths, text, file)
    assert state_of(conn) is TaskState.REVIEWING
    assert not paths.review_path(TASK).exists()
    assert not paths.triage_path(TASK).exists()


def test_triage_refuses_a_pass(
    conn: sqlite3.Connection, paths: TargetPaths, tmp_path: Path
) -> None:
    add_task(conn)
    file = write_triage(tmp_path, triage_text(WRONG))
    assert_refused(conn, paths, review_text(finding("P2", "a.py")), file)


def test_triage_refuses_an_unhandled_finding(
    conn: sqlite3.Connection, paths: TargetPaths, tmp_path: Path
) -> None:
    add_task(conn)
    file = write_triage(tmp_path, triage_text(WRONG))
    text = review_text(finding("P0", "src/app.py"), finding("P1", "src/app.py"))
    assert_refused(conn, paths, text, file)


def test_triage_refuses_a_decision_with_no_finding(
    conn: sqlite3.Connection, paths: TargetPaths, tmp_path: Path
) -> None:
    add_task(conn)
    second = WRONG.replace("src/app.py", "src/other.py")
    file = write_triage(tmp_path, triage_text(WRONG, second))
    assert_refused(conn, paths, review_text(finding("P0", "src/app.py")), file)


def test_triage_matches_the_severity_as_well_as_the_path(
    conn: sqlite3.Connection, paths: TargetPaths, tmp_path: Path
) -> None:
    add_task(conn)
    file = write_triage(tmp_path, triage_text(WRONG))
    assert_refused(conn, paths, review_text(finding("P1", "src/app.py")), file)


def test_triage_counts_repeated_findings(
    conn: sqlite3.Connection, paths: TargetPaths, tmp_path: Path
) -> None:
    add_task(conn)
    one = write_triage(tmp_path, triage_text(WRONG))
    text = review_text(finding("P0", "src/app.py"), finding("P0", "src/app.py"))
    assert_refused(conn, paths, text, one)
    two = write_triage(tmp_path, triage_text(WRONG, WRONG))
    assert save(conn, paths, text, two) is TaskState.READY


def test_triage_refuses_a_decision_for_a_minor_finding(
    conn: sqlite3.Connection, paths: TargetPaths, tmp_path: Path
) -> None:
    add_task(conn)
    file = write_triage(tmp_path, triage_text(WRONG))
    text = review_text(finding("P1", "src/b.py"), finding("P2", "src/app.py"))
    assert_refused(conn, paths, text, file)


def test_triage_needs_a_registered_follow_up(
    conn: sqlite3.Connection, paths: TargetPaths, tmp_path: Path
) -> None:
    add_task(conn)
    file = write_triage(tmp_path, triage_text(INSUFFICIENT, follow_up="feat-x-tests"))
    assert_refused(conn, paths, review_text(finding("P1", "tests/test_x.py")), file)


def test_triage_refuses_a_discarded_follow_up(
    conn: sqlite3.Connection, paths: TargetPaths, tmp_path: Path
) -> None:
    add_task(conn)
    add_task(conn, "feat-x-tests", TaskState.DISCARDED)
    file = write_triage(tmp_path, triage_text(INSUFFICIENT, follow_up="feat-x-tests"))
    assert_refused(conn, paths, review_text(finding("P1", "tests/test_x.py")), file)


def test_triage_refuses_a_follow_up_on_another_target(
    conn: sqlite3.Connection, paths: TargetPaths, tmp_path: Path
) -> None:
    add_task(conn)
    add_task(conn, "feat-x-tests", TaskState.PREPARED, target="other")
    file = write_triage(tmp_path, triage_text(INSUFFICIENT, follow_up="feat-x-tests"))
    assert_refused(conn, paths, review_text(finding("P1", "tests/test_x.py")), file)


def test_triage_refuses_the_task_as_its_own_follow_up(
    conn: sqlite3.Connection, paths: TargetPaths, tmp_path: Path
) -> None:
    add_task(conn)
    file = write_triage(tmp_path, triage_text(INSUFFICIENT, follow_up=TASK))
    assert_refused(conn, paths, review_text(finding("P1", "tests/test_x.py")), file)


@pytest.mark.parametrize(
    "state", [TaskState.PREPARED, TaskState.QUEUED, TaskState.RUNNING, TaskState.CLOSED]
)
def test_any_other_follow_up_state_counts_as_registered(
    conn: sqlite3.Connection, paths: TargetPaths, tmp_path: Path, state: TaskState
) -> None:
    add_task(conn)
    add_task(conn, "feat-x-tests", state)
    file = write_triage(tmp_path, triage_text(INSUFFICIENT, follow_up="feat-x-tests"))
    text = review_text(finding("P1", "tests/test_x.py"))
    assert save(conn, paths, text, file) is TaskState.READY


def test_triage_refuses_an_unreadable_file(
    conn: sqlite3.Connection, paths: TargetPaths, tmp_path: Path
) -> None:
    add_task(conn)
    assert_refused(
        conn, paths, review_text(finding("P0", "src/app.py")), tmp_path / "missing.md"
    )


@pytest.mark.parametrize(
    "decision",
    [
        "See /Users/someone/notes for the reason.",
        "See /private/tmp/notes for the reason.",
        "Leaked " + TOKEN,
    ],
)
def test_triage_refuses_text_that_could_not_leave_this_machine(
    conn: sqlite3.Connection, paths: TargetPaths, tmp_path: Path, decision: str
) -> None:
    """The refusal comes here, not after the branch was pushed."""
    add_task(conn)
    table = WRONG.replace("The caller validates this value.", decision)
    file = write_triage(tmp_path, triage_text(table))
    assert_refused(conn, paths, review_text(finding("P0", "src/app.py")), file)


def test_triage_refuses_a_known_path_redact_would_replace(
    conn: sqlite3.Connection, paths: TargetPaths, tmp_path: Path
) -> None:
    add_task(conn)
    table = WRONG.replace(
        "The caller validates this value.", f"See {REDACT['home']}/notes."
    )
    file = write_triage(tmp_path, triage_text(table))
    assert_refused(conn, paths, review_text(finding("P0", "src/app.py")), file)


def test_triage_refuses_a_personal_path_in_the_body(
    conn: sqlite3.Connection, paths: TargetPaths, tmp_path: Path
) -> None:
    add_task(conn)
    file = write_triage(
        tmp_path, triage_text(WRONG, body="\nSee /Users/someone/notes.\n")
    )
    assert_refused(conn, paths, review_text(finding("P0", "src/app.py")), file)


# Triage paths are checked before the pull request (m12-review-core, backlog P2)


@pytest.mark.parametrize(
    "path",
    [
        "/abs/app.py",
        "~/app.py",
        "src\\\\app.py",
        "src/../app.py",
        "..",
        "src/a`b.py",
        "src/앱.py",
        "src/a\\tb.py",
    ],
)
def test_parse_refuses_a_triage_path_that_is_not_plain_and_relative(path: str) -> None:
    table = WRONG.replace('path = "src/app.py"', f'path = "{path}"')
    assert table != WRONG
    with pytest.raises(ReviewFlowError):
        parse_triage(triage_text(table))


@pytest.mark.parametrize("path", ["src/app.py", "a/b-c_d.e/f.py", "tests/x.py"])
def test_parse_keeps_a_plain_relative_triage_path(path: str) -> None:
    table = WRONG.replace('path = "src/app.py"', f'path = "{path}"')
    assert parse_triage(triage_text(table)).decisions[0].path == path


@pytest.mark.parametrize(
    "path", ["src/Users/someone/app.py", "src/" + TOKEN + ".py", "a/private/tmp/b.py"]
)
def test_triage_refuses_a_path_that_could_not_leave_this_machine(
    conn: sqlite3.Connection, paths: TargetPaths, tmp_path: Path, path: str
) -> None:
    add_task(conn)
    table = WRONG.replace('path = "src/app.py"', f'path = "{path}"')
    file = write_triage(tmp_path, triage_text(table))
    assert_refused(conn, paths, review_text(finding("P0", path)), file)
