import inspect
import sqlite3
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from orch import github, store, waiter
from orch.config import Target
from orch.github import (
    CheckResult,
    Comparison,
    GitHubError,
    LabelEvent,
    RequiredChecks,
    ReviewState,
)
from orch.models import Effort, Grade, Lane, Task, TaskState
from orch.waiter import MergeCheckError, MergeWaitError, Outcome, wait_for_merge

TASK = "feat-x"
URL = "https://github.com/owner/sample/pull/7"
SHA = "d0f05422f4b72ff9df4d76762e42a42060a501f4"
MERGED_AT = "2026-01-01T00:00:00Z"
T0 = datetime(2026, 11, 17, 3, 0, 0, tzinfo=UTC)
PASSED = {"ci": CheckResult.PASSED}
TARGET = Target(
    name="sample",
    path=Path("/", "nowhere"),
    github="owner/sample",
    default_branch="main",
    setup_command="true",
    test_command="true",
    lint_command="true",
    safety_paths=("config/**", "src/orch/github.py"),
)
WAITABLE = {TaskState.PR_OPEN, TaskState.CONFLICT}


def event(name: str, *, added: bool = True, minutes: float = 0) -> LabelEvent:
    return LabelEvent(name=name, added=added, at=T0 + timedelta(minutes=minutes))


def pr(
    state: str = "OPEN",
    *,
    labels: tuple[str, ...] = (),
    events: tuple[LabelEvent, ...] = (),
    checks: dict[str, CheckResult] | None = None,
    head: str = SHA,
) -> ReviewState:
    return ReviewState(
        state=state,
        merged=state == "MERGED",
        merged_at=MERGED_AT if state == "MERGED" else None,
        head_sha=head,
        labels=labels,
        events=events,
        checks=PASSED if checks is None else checks,
    )


OPEN_PENDING = pr(checks={})
MERGED = pr("MERGED")
CLOSED = pr("CLOSED")


class Time:
    """A clock that only sleeping moves. The wall clock starts at T0."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []
        self.on_sleep: Callable[[], None] = lambda: None

    def clock(self) -> float:
        return self.now

    def wall(self) -> datetime:
        return T0 + timedelta(seconds=self.now - 1000.0)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        self.on_sleep()


class Tokens:
    """Hands out a different token each call, or fails on the listed call numbers."""

    def __init__(self) -> None:
        self.issued: list[str] = []
        self.fail_on: set[int] = set()

    def __call__(self) -> str:
        number = len(self.issued) + 1
        self.issued.append(f"token-{number}")
        if number in self.fail_on:
            raise RuntimeError("Token script failed or returned an empty token")
        return self.issued[-1]


class Hub:
    """Stands in for the github calls of orch wait and records them in order."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.states: list[ReviewState | GitHubError] = []
        self.repeat: ReviewState | None = None
        self.rules = RequiredChecks(names=("ci",), strict=True)
        self.comparison = Comparison(
            behind_by=0, files=("src/app.py",), truncated=False
        )
        self.compare_error: GitHubError | None = None
        self.add_error: GitHubError | None = None
        self.remove_error: GitHubError | None = None
        self.calls: list[tuple[object, ...]] = []
        monkeypatch.setattr(github, "review_state", self.review_state)
        monkeypatch.setattr(github, "check_rules", self.check_rules)
        monkeypatch.setattr(github, "compare", self.compare)
        monkeypatch.setattr(github, "add_label", self.add_label)
        monkeypatch.setattr(github, "remove_label", self.remove_label)

        def unexpected(**options: object) -> None:
            pytest.fail("orch wait must not call pull_request_status")

        monkeypatch.setattr(github, "pull_request_status", unexpected)

    def review_state(self, *, url: str, token: str) -> ReviewState:
        self.calls.append(("review", url, token))
        if not self.states:
            if self.repeat is None:
                pytest.fail("More checks than the test expected")
            return self.repeat
        answer = self.states.pop(0)
        if isinstance(answer, GitHubError):
            raise answer
        return answer

    def check_rules(self, *, github: str, branch: str, token: str) -> RequiredChecks:
        self.calls.append(("rules", github, branch, token))
        return self.rules

    def compare(self, *, github: str, base: str, head: str, token: str) -> Comparison:
        self.calls.append(("compare", github, base, head, token))
        if self.compare_error is not None:
            raise self.compare_error
        return self.comparison

    def add_label(self, *, url: str, name: str, token: str) -> None:
        self.calls.append(("add", url, name, token))
        if self.add_error is not None:
            raise self.add_error

    def remove_label(self, *, url: str, name: str, token: str) -> None:
        self.calls.append(("remove", url, name, token))
        if self.remove_error is not None:
            raise self.remove_error

    def kinds(self) -> list[object]:
        return [call[0] for call in self.calls]

    def label_calls(self) -> list[tuple[object, ...]]:
        return [call[:3] for call in self.calls if call[0] in ("add", "remove")]


class Notes:
    """Records the events orch wait asks to notify."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.error: Exception | None = None

    def __call__(self, event: str) -> None:
        self.events.append(event)
        if self.error is not None:
            raise self.error


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = store.connect(tmp_path / "state.db")
    yield connection
    connection.close()


@pytest.fixture
def hub(monkeypatch: pytest.MonkeyPatch) -> Hub:
    return Hub(monkeypatch)


@pytest.fixture
def tokens() -> Tokens:
    return Tokens()


@pytest.fixture
def time_() -> Time:
    return Time()


@pytest.fixture
def notes() -> Notes:
    return Notes()


def insert(
    conn: sqlite3.Connection,
    state: TaskState = TaskState.PR_OPEN,
    *,
    pr_url: str | None = URL,
    grade: Grade = Grade.L2,
) -> None:
    store.insert_task(
        conn,
        Task(
            id=TASK,
            target="sample",
            title="Add feature X",
            grade=grade,
            lane=Lane.B,
            model="gpt-6.1-sol",
            effort=Effort.HIGH,
            state=state,
            timeout_sec=300,
            pr_url=pr_url,
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        ),
    )


Wait = Callable[..., waiter.MergeWaitReport]


@pytest.fixture
def wait(conn: sqlite3.Connection, tokens: Tokens, time_: Time, notes: Notes) -> Wait:
    def call(**options: object) -> waiter.MergeWaitReport:
        values: dict[str, object] = {
            "target": TARGET,
            "token": tokens,
            "notify": notes,
            "sleep": time_.sleep,
            "clock": time_.clock,
            "now": time_.wall,
        }
        values.update(options)
        return wait_for_merge(conn, TASK, **values)

    return call


# How a wait ends


def test_a_merged_pull_request_ends_at_the_first_check(
    conn: sqlite3.Connection, hub: Hub, time_: Time, wait: Wait
) -> None:
    insert(conn)
    hub.states = [pr("MERGED", labels=("verify-deep",))]
    report = wait()
    assert report == waiter.MergeWaitReport(
        task_id=TASK,
        state=TaskState.PR_OPEN,
        pr_url=URL,
        pr_state="MERGED",
        merged=True,
        merged_at=MERGED_AT,
        checks=1,
        outcome=Outcome.MERGED,
        review_label="verify-deep",
        failed_checks=(),
        notified=(),
    )
    assert hub.kinds() == ["review"]
    assert time_.sleeps == []


def test_a_pull_request_closed_without_a_merge_ends_the_wait(
    conn: sqlite3.Connection, hub: Hub, wait: Wait
) -> None:
    insert(conn)
    hub.states = [CLOSED]
    report = wait()
    assert report.outcome is Outcome.CLOSED
    assert (report.pr_state, report.merged, report.merged_at) == ("CLOSED", False, None)
    assert hub.kinds() == ["review"]


def test_a_wait_sleeps_the_default_interval_until_the_merge(
    conn: sqlite3.Connection, hub: Hub, time_: Time, wait: Wait
) -> None:
    insert(conn)
    hub.states = [OPEN_PENDING, OPEN_PENDING, MERGED]
    report = wait()
    assert report.outcome is Outcome.MERGED
    assert report.checks == 3
    assert time_.sleeps == [60, 60]


def test_the_interval_can_be_set(
    conn: sqlite3.Connection, hub: Hub, time_: Time, wait: Wait
) -> None:
    insert(conn)
    hub.states = [OPEN_PENDING, MERGED]
    wait(interval_sec=5)
    assert time_.sleeps == [5]


def test_the_default_limit_is_twelve_hours(
    conn: sqlite3.Connection, hub: Hub, time_: Time, wait: Wait
) -> None:
    insert(conn)
    hub.repeat = OPEN_PENDING
    report = wait(interval_sec=3600)
    assert report.outcome is Outcome.LIMIT
    assert report.merged is False
    assert report.pr_state == "OPEN"
    assert time_.sleeps == [3600] * 12
    assert report.checks == 13


def test_a_short_last_sleep_reaches_the_limit_exactly(
    conn: sqlite3.Connection, hub: Hub, time_: Time, wait: Wait
) -> None:
    insert(conn)
    hub.repeat = OPEN_PENDING
    report = wait(interval_sec=50, max_wait_min=2)
    assert time_.sleeps == [50, 50, 20]
    assert report.checks == 4
    assert report.outcome is Outcome.LIMIT


def test_real_time_is_the_default() -> None:
    parameters = inspect.signature(wait_for_merge).parameters
    assert parameters["sleep"].default is time.sleep
    assert parameters["clock"].default is time.monotonic
    assert parameters["interval_sec"].default == 60
    assert parameters["max_wait_min"].default == 720
    assert parameters["now"].default is not inspect.Parameter.empty
    assert parameters["now"].default().tzinfo is not None


def test_each_check_gets_one_new_token_for_all_its_calls(
    conn: sqlite3.Connection, hub: Hub, tokens: Tokens, wait: Wait
) -> None:
    insert(conn)
    hub.states = [OPEN_PENDING, MERGED]
    wait()
    assert tokens.issued == ["token-1", "token-2"]
    assert [call[-1] for call in hub.calls] == [
        "token-1",
        "token-1",
        "token-1",
        "token-2",
    ]


def test_the_target_names_the_repository_branch_and_head(
    conn: sqlite3.Connection, hub: Hub, wait: Wait
) -> None:
    insert(conn)
    hub.states = [pr(checks={}, head="a" * 40), MERGED]
    wait()
    assert hub.calls[:3] == [
        ("review", URL, "token-1"),
        ("rules", "owner/sample", "main", "token-1"),
        ("compare", "owner/sample", "main", "a" * 40, "token-1"),
    ]


# Refusals


def test_a_task_in_conflict_can_be_waited_on(
    conn: sqlite3.Connection, hub: Hub, wait: Wait
) -> None:
    insert(conn, TaskState.CONFLICT)
    hub.states = [MERGED]
    report = wait()
    assert report.state is TaskState.CONFLICT
    assert report.merged is True


@pytest.mark.parametrize("state", [s for s in TaskState if s not in WAITABLE])
def test_other_states_are_refused_before_any_token(
    conn: sqlite3.Connection, hub: Hub, tokens: Tokens, wait: Wait, state: TaskState
) -> None:
    insert(conn, state)
    with pytest.raises(MergeWaitError):
        wait()
    assert tokens.issued == []
    assert hub.calls == []


def test_a_task_without_a_pull_request_is_refused(
    conn: sqlite3.Connection, hub: Hub, tokens: Tokens, wait: Wait
) -> None:
    insert(conn, pr_url=None)
    with pytest.raises(MergeWaitError):
        wait()
    assert tokens.issued == []


def test_an_unknown_task_is_refused(hub: Hub, tokens: Tokens, wait: Wait) -> None:
    with pytest.raises(MergeWaitError):
        wait()
    assert tokens.issued == []


def test_a_state_change_during_the_wait_is_refused_at_the_next_check(
    conn: sqlite3.Connection, hub: Hub, time_: Time, wait: Wait
) -> None:
    insert(conn)
    hub.repeat = OPEN_PENDING

    def close_task() -> None:
        store.transition(conn, TASK, TaskState.PR_OPEN, TaskState.CLOSED, actor="t")

    time_.on_sleep = close_task
    with pytest.raises(MergeWaitError):
        wait()
    assert hub.kinds().count("review") == 1


def test_a_branch_without_required_checks_is_refused(
    conn: sqlite3.Connection, hub: Hub, wait: Wait
) -> None:
    insert(conn)
    hub.states = [pr()]
    hub.rules = RequiredChecks(names=(), strict=True)
    with pytest.raises(MergeWaitError):
        wait()
    assert hub.kinds() == ["review", "rules"]


def test_a_wait_does_not_change_the_task(
    conn: sqlite3.Connection, hub: Hub, wait: Wait
) -> None:
    insert(conn)
    before = store.get_task(conn, TASK)
    hub.states = [pr(), pr(labels=("verify-light",)), MERGED]
    wait()
    assert store.get_task(conn, TASK) == before


# Failed checks of the pull request


def test_failed_checks_are_retried_and_a_success_resets_the_count(
    conn: sqlite3.Connection, hub: Hub, wait: Wait
) -> None:
    insert(conn)
    hub.states = [GitHubError("boom")] * 9 + [OPEN_PENDING] + [GitHubError("boom")] * 9
    hub.states.append(MERGED)
    report = wait()
    assert report.outcome is Outcome.MERGED
    assert report.checks == 20


def test_ten_failed_checks_in_a_row_raise(
    conn: sqlite3.Connection, hub: Hub, tokens: Tokens, wait: Wait
) -> None:
    insert(conn)
    hub.states = [GitHubError("boom")] * 5
    tokens.fail_on = {6, 7, 8}
    hub.compare_error = GitHubError("boom")
    hub.repeat = pr()
    with pytest.raises(MergeCheckError):
        wait()
    assert len(tokens.issued) == 10


def test_a_failed_label_call_counts_as_a_failed_check_and_is_retried(
    conn: sqlite3.Connection, hub: Hub, time_: Time, wait: Wait
) -> None:
    insert(conn)
    hub.states = [pr(), pr(), MERGED]
    hub.add_error = GitHubError("boom")

    def recover() -> None:
        hub.add_error = None

    time_.on_sleep = recover
    report = wait()
    assert report.outcome is Outcome.MERGED
    assert [call for call in hub.calls if call[0] == "add"] == [
        ("add", URL, "verify-light", "token-1"),
        ("add", URL, "verify-light", "token-2"),
    ]


def test_the_last_known_state_is_reported_at_the_limit(
    conn: sqlite3.Connection, hub: Hub, wait: Wait
) -> None:
    insert(conn)
    hub.states = [pr(labels=("verify-deep",), checks={})]
    hub.repeat = None
    hub.states += [GitHubError("boom")] * 2
    report = wait(interval_sec=60, max_wait_min=2)
    assert report.outcome is Outcome.LIMIT
    assert report.pr_state == "OPEN"
    assert report.review_label == "verify-deep"


def test_no_known_state_when_every_check_failed(
    conn: sqlite3.Connection, hub: Hub, wait: Wait
) -> None:
    insert(conn)
    hub.states = [GitHubError("boom")] * 3
    report = wait(interval_sec=60, max_wait_min=2)
    assert report.outcome is Outcome.LIMIT
    assert report.pr_state is None
    assert report.review_label is None


# changes-requested


def test_the_first_changes_requested_ends_the_wait(
    conn: sqlite3.Connection, hub: Hub, wait: Wait
) -> None:
    insert(conn)
    hub.states = [
        pr(
            labels=("verify-deep", "changes-requested"),
            events=(event("verify-deep"), event("changes-requested", minutes=1)),
        )
    ]
    report = wait()
    assert report.outcome is Outcome.CHANGES_REQUESTED
    assert report.review_label == "verify-deep"
    assert hub.kinds() == ["review", "rules"]


def test_a_second_changes_requested_notifies_once_and_keeps_waiting(
    conn: sqlite3.Connection, hub: Hub, notes: Notes, wait: Wait
) -> None:
    insert(conn)
    again = pr(
        labels=("changes-requested",),
        events=(
            event("changes-requested"),
            event("changes-requested", added=False, minutes=1),
            event("changes-requested", minutes=2),
        ),
    )
    hub.states = [again, again, MERGED]
    report = wait()
    assert report.outcome is Outcome.MERGED
    assert notes.events == ["changes_requested_again"]
    assert report.notified == ("changes_requested_again",)
    assert hub.label_calls() == []


# A branch behind the default branch


def test_a_head_behind_a_strict_branch_ends_the_wait_before_any_label(
    conn: sqlite3.Connection, hub: Hub, wait: Wait
) -> None:
    insert(conn)
    hub.states = [pr()]
    hub.comparison = Comparison(behind_by=1, files=("src/app.py",), truncated=False)
    report = wait()
    assert report.outcome is Outcome.BEHIND
    assert hub.label_calls() == []
    assert hub.kinds() == ["review", "rules", "compare"]


def test_a_head_behind_a_branch_that_is_not_strict_keeps_going(
    conn: sqlite3.Connection, hub: Hub, wait: Wait
) -> None:
    insert(conn)
    hub.states = [pr(), MERGED]
    hub.rules = RequiredChecks(names=("ci",), strict=False)
    hub.comparison = Comparison(behind_by=3, files=("src/app.py",), truncated=False)
    report = wait()
    assert report.outcome is Outcome.MERGED
    assert hub.label_calls() == [("add", URL, "verify-light")]


def test_a_behind_head_waiting_for_the_user_is_still_behind(
    conn: sqlite3.Connection, hub: Hub, notes: Notes, wait: Wait
) -> None:
    insert(conn)
    hub.states = [pr(labels=("needs-user",))]
    hub.comparison = Comparison(behind_by=2, files=(), truncated=False)
    assert wait().outcome is Outcome.BEHIND
    assert notes.events == []


# Waiting for the user


def test_needs_user_notifies_once_and_keeps_waiting(
    conn: sqlite3.Connection, hub: Hub, notes: Notes, wait: Wait
) -> None:
    insert(conn)
    waiting = pr(labels=("needs-user",))
    hub.states = [waiting, waiting, waiting, MERGED]
    report = wait()
    assert report.outcome is Outcome.MERGED
    assert notes.events == ["user_wait"]
    assert report.notified == ("user_wait",)
    assert hub.label_calls() == []


def test_a_failed_notification_does_not_change_the_wait(
    conn: sqlite3.Connection, hub: Hub, notes: Notes, wait: Wait
) -> None:
    insert(conn)
    notes.error = RuntimeError("ntfy is down")
    waiting = pr(labels=("needs-user",))
    hub.states = [waiting, waiting, MERGED]
    report = wait()
    assert report.outcome is Outcome.MERGED
    assert notes.events == ["user_wait"]
    assert report.notified == ("user_wait",)


def test_an_overridden_pull_request_gets_no_review_label(
    conn: sqlite3.Connection, hub: Hub, notes: Notes, wait: Wait
) -> None:
    insert(conn)
    overridden = pr(labels=("review-overridden",))
    hub.states = [overridden, overridden, MERGED]
    report = wait()
    assert report.outcome is Outcome.MERGED
    assert hub.label_calls() == []
    assert notes.events == []


# Required checks


def test_a_failed_required_check_ends_the_wait_with_its_names(
    conn: sqlite3.Connection, hub: Hub, wait: Wait
) -> None:
    insert(conn)
    hub.rules = RequiredChecks(names=("ci", "lint", "docs"), strict=True)
    hub.states = [
        pr(
            checks={
                "lint": CheckResult.FAILED,
                "docs": CheckResult.PENDING,
                "ci": CheckResult.FAILED,
                "extra": CheckResult.FAILED,
            }
        )
    ]
    report = wait()
    assert report.outcome is Outcome.CHECKS_FAILED
    assert report.failed_checks == ("ci", "lint")
    assert hub.label_calls() == []


@pytest.mark.parametrize(
    "checks",
    [
        {},
        {"ci": CheckResult.PENDING},
        {"lint": CheckResult.PASSED},
    ],
)
def test_a_pending_or_missing_required_check_keeps_waiting(
    conn: sqlite3.Connection, hub: Hub, wait: Wait, checks: dict[str, CheckResult]
) -> None:
    insert(conn)
    hub.states = [pr(checks=checks), MERGED]
    report = wait()
    assert report.outcome is Outcome.MERGED
    assert hub.label_calls() == []


def test_a_check_that_is_not_required_is_ignored(
    conn: sqlite3.Connection, hub: Hub, wait: Wait
) -> None:
    insert(conn)
    hub.states = [
        pr(checks={"ci": CheckResult.PASSED, "extra": CheckResult.FAILED}),
        MERGED,
    ]
    assert wait().outcome is Outcome.MERGED
    assert hub.label_calls() == [("add", URL, "verify-light")]


# Choosing the review label


@pytest.mark.parametrize(
    ("grade", "files", "truncated", "label"),
    [
        (Grade.L1, ("src/app.py",), False, "verify-light"),
        (Grade.L2, ("src/app.py", "docs/a.md"), False, "verify-light"),
        (Grade.L2, (), False, "verify-light"),
        (Grade.L2, ("src/orch/github.py",), False, "verify-deep"),
        (Grade.L2, ("src/app.py", "config/main.md"), False, "verify-deep"),
        (Grade.L2, ("src/app.py",), True, "verify-deep"),
        (Grade.L3, ("src/app.py",), False, "verify-deep"),
        (Grade.L3R, ("src/app.py",), False, "verify-deep"),
    ],
)
def test_the_review_label_follows_the_grade_and_the_safety_paths(
    conn: sqlite3.Connection,
    hub: Hub,
    wait: Wait,
    grade: Grade,
    files: tuple[str, ...],
    truncated: bool,
    label: str,
) -> None:
    insert(conn, grade=grade)
    hub.comparison = Comparison(behind_by=0, files=files, truncated=truncated)
    hub.states = [pr(), MERGED]
    report = wait()
    assert hub.label_calls() == [("add", URL, label)]
    assert report.review_label is None


def test_the_review_label_is_added_once_and_reported(
    conn: sqlite3.Connection, hub: Hub, wait: Wait
) -> None:
    insert(conn, grade=Grade.L3)
    labeled = pr(labels=("verify-deep",), events=(event("verify-deep"),))
    hub.states = [pr(), labeled, labeled]
    report = wait(interval_sec=60, max_wait_min=2)
    assert report.outcome is Outcome.LIMIT
    assert report.review_label == "verify-deep"
    assert hub.label_calls() == [("add", URL, "verify-deep")]


# A review that does not answer


def stale(
    label: str = "verify-deep", *extra: LabelEvent, labels: tuple[str, ...] = ()
) -> ReviewState:
    return pr(
        labels=(label, *labels),
        events=(event(label, minutes=-30), *extra),
    )


def test_a_label_without_a_verdict_for_30_minutes_is_added_again_once(
    conn: sqlite3.Connection, hub: Hub, notes: Notes, wait: Wait
) -> None:
    insert(conn, grade=Grade.L3)
    hub.states = [stale(), MERGED]
    wait()
    assert hub.label_calls() == [
        ("remove", URL, "verify-deep"),
        ("add", URL, "verify-deep"),
    ]
    assert notes.events == []


def test_a_label_younger_than_30_minutes_is_left_alone(
    conn: sqlite3.Connection, hub: Hub, wait: Wait
) -> None:
    insert(conn)
    young = pr(labels=("verify-light",), events=(event("verify-light", minutes=-29),))
    hub.states = [young, MERGED]
    wait()
    assert hub.label_calls() == []


def test_the_age_counts_from_the_last_time_the_label_was_added(
    conn: sqlite3.Connection, hub: Hub, wait: Wait
) -> None:
    insert(conn)
    readded = pr(
        labels=("verify-light",),
        events=(
            event("verify-light", minutes=-90),
            event("verify-light", added=False, minutes=-60),
            event("verify-light", minutes=-10),
        ),
    )
    hub.states = [readded, MERGED]
    wait()
    assert hub.label_calls() == []


def test_a_label_added_twice_without_a_verdict_notifies_once(
    conn: sqlite3.Connection, hub: Hub, notes: Notes, wait: Wait
) -> None:
    insert(conn, grade=Grade.L3)
    twice = pr(
        labels=("verify-deep",),
        events=(
            event("verify-deep", minutes=-90),
            event("verify-deep", added=False, minutes=-60),
            event("verify-deep", minutes=-60),
        ),
    )
    hub.states = [twice, twice, MERGED]
    report = wait()
    assert hub.label_calls() == []
    assert notes.events == ["review_stalled"]
    assert report.notified == ("review_stalled",)


def test_labels_before_the_last_fix_are_not_counted(
    conn: sqlite3.Connection, hub: Hub, wait: Wait
) -> None:
    insert(conn, grade=Grade.L3)
    after_fix = pr(
        labels=("verify-deep",),
        events=(
            event("verify-deep", minutes=-120),
            event("changes-requested", minutes=-110),
            event("verify-deep", added=False, minutes=-100),
            event("changes-requested", added=False, minutes=-100),
            event("verify-deep", minutes=-40),
        ),
    )
    hub.states = [after_fix, MERGED]
    wait()
    assert hub.label_calls() == [
        ("remove", URL, "verify-deep"),
        ("add", URL, "verify-deep"),
    ]


def test_user_approved_stops_the_stall_check(
    conn: sqlite3.Connection, hub: Hub, notes: Notes, wait: Wait
) -> None:
    insert(conn, grade=Grade.L3)
    hub.states = [stale(labels=("user-approved",)), MERGED]
    wait()
    assert hub.label_calls() == []
    assert notes.events == []


def test_user_approved_is_never_touched(
    conn: sqlite3.Connection, hub: Hub, wait: Wait
) -> None:
    insert(conn)
    hub.states = [
        pr(labels=("user-approved",)),
        stale(labels=("user-approved",)),
        MERGED,
    ]
    wait()
    assert all(call[2] != "user-approved" for call in hub.label_calls())


def test_several_notifications_are_each_sent_once_in_order(
    conn: sqlite3.Connection, hub: Hub, notes: Notes, wait: Wait
) -> None:
    insert(conn)
    waiting = pr(labels=("needs-user",))
    again = pr(
        labels=("changes-requested",),
        events=(event("changes-requested"), event("changes-requested", minutes=1)),
    )
    hub.states = [waiting, again, waiting, again, MERGED]
    report = wait()
    assert notes.events == ["user_wait", "changes_requested_again"]
    assert report.notified == ("user_wait", "changes_requested_again")
