import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from orch import github, store, waiter
from orch.github import GitHubError, PullRequestStatus
from orch.models import Effort, Grade, Lane, Task, TaskState
from orch.waiter import MergeCheckError, MergeWaitError, wait_for_merge

TASK = "feat-x"
URL = "https://github.com/owner/sample/pull/7"
MERGED_AT = "2026-01-01T00:00:00Z"
OPEN = PullRequestStatus(state="OPEN", merged=False, mergeable="MERGEABLE")
CLOSED = PullRequestStatus(state="CLOSED", merged=False, mergeable="UNKNOWN")
MERGED = PullRequestStatus(
    state="MERGED", merged=True, merged_at=MERGED_AT, mergeable="UNKNOWN"
)
WAITABLE = {TaskState.PR_OPEN, TaskState.CONFLICT}


def failure(text: str = "gh failed") -> GitHubError:
    return GitHubError(text)


class Time:
    """A clock that only sleeping moves. on_sleep runs during each sleep."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []
        self.on_sleep: Callable[[], None] = lambda: None

    def clock(self) -> float:
        return self.now

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


class Answers:
    """Stands in for github.pull_request_status with a scripted list of answers."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.script: list[PullRequestStatus | GitHubError] = []
        self.repeat: PullRequestStatus | None = None
        self.calls: list[tuple[str, str]] = []
        monkeypatch.setattr(github, "pull_request_status", self)

    def __call__(self, *, url: str, token: str) -> PullRequestStatus:
        self.calls.append((url, token))
        if not self.script:
            if self.repeat is None:
                pytest.fail("More checks than the test expected")
            return self.repeat
        answer = self.script.pop(0)
        if isinstance(answer, GitHubError):
            raise answer
        return answer


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = store.connect(tmp_path / "state.db")
    yield connection
    connection.close()


@pytest.fixture
def db_path(tmp_path: Path, conn: sqlite3.Connection) -> Path:
    return tmp_path / "state.db"


@pytest.fixture
def answers(monkeypatch: pytest.MonkeyPatch) -> Answers:
    return Answers(monkeypatch)


@pytest.fixture
def tokens() -> Tokens:
    return Tokens()


@pytest.fixture
def time_(conn: sqlite3.Connection) -> Time:
    return Time()


def insert(
    conn: sqlite3.Connection,
    state: TaskState = TaskState.PR_OPEN,
    *,
    pr_url: str | None = URL,
) -> None:
    store.insert_task(
        conn,
        Task(
            id=TASK,
            target="sample",
            title="Add feature X",
            grade=Grade.L3,
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


@pytest.fixture
def wait(
    conn: sqlite3.Connection, tokens: Tokens, time_: Time
) -> Callable[..., waiter.MergeWaitReport]:
    def call(**options: object) -> waiter.MergeWaitReport:
        return wait_for_merge(
            conn, TASK, token=tokens, sleep=time_.sleep, clock=time_.clock, **options
        )

    return call


Wait = Callable[..., waiter.MergeWaitReport]


# How a wait ends


def test_a_merged_pull_request_ends_at_the_first_check_without_sleeping(
    conn: sqlite3.Connection, answers: Answers, tokens: Tokens, time_: Time, wait: Wait
) -> None:
    insert(conn)
    answers.script = [MERGED]
    report = wait()
    assert report == waiter.MergeWaitReport(
        task_id=TASK,
        state=TaskState.PR_OPEN,
        pr_url=URL,
        pr_state="MERGED",
        merged=True,
        merged_at=MERGED_AT,
        checks=1,
    )
    assert time_.sleeps == []
    assert answers.calls == [(URL, "token-1")]


def test_a_pull_request_closed_without_a_merge_ends_the_wait(
    conn: sqlite3.Connection, answers: Answers, time_: Time, wait: Wait
) -> None:
    insert(conn)
    answers.script = [CLOSED]
    report = wait()
    assert (report.pr_state, report.merged, report.merged_at) == ("CLOSED", False, None)
    assert report.checks == 1
    assert time_.sleeps == []


def test_a_wait_sleeps_the_default_interval_until_the_merge(
    conn: sqlite3.Connection, answers: Answers, time_: Time, wait: Wait
) -> None:
    insert(conn)
    answers.script = [OPEN, OPEN, MERGED]
    report = wait()
    assert report.merged is True
    assert report.checks == 3
    assert time_.sleeps == [60, 60]


def test_the_interval_can_be_set(
    conn: sqlite3.Connection, answers: Answers, time_: Time, wait: Wait
) -> None:
    insert(conn)
    answers.script = [OPEN, MERGED]
    wait(interval_sec=5)
    assert time_.sleeps == [5]


def test_real_sleep_and_clock_are_the_defaults(
    conn: sqlite3.Connection, answers: Answers, tokens: Tokens
) -> None:
    insert(conn)
    answers.script = [MERGED]
    assert wait_for_merge(conn, TASK, token=tokens).merged is True


def test_each_check_gets_a_new_token(
    conn: sqlite3.Connection, answers: Answers, tokens: Tokens, wait: Wait
) -> None:
    insert(conn)
    answers.script = [OPEN, OPEN, MERGED]
    wait()
    assert tokens.issued == ["token-1", "token-2", "token-3"]
    assert answers.calls == [
        (URL, "token-1"),
        (URL, "token-2"),
        (URL, "token-3"),
    ]


# Which tasks can be waited on


def test_a_task_in_conflict_can_be_waited_on(
    conn: sqlite3.Connection, answers: Answers, wait: Wait
) -> None:
    insert(conn, TaskState.CONFLICT)
    answers.script = [MERGED]
    report = wait()
    assert report.merged is True
    assert report.state is TaskState.CONFLICT


@pytest.mark.parametrize("state", sorted(set(TaskState) - WAITABLE))
def test_other_states_are_refused_before_any_token(
    conn: sqlite3.Connection,
    answers: Answers,
    tokens: Tokens,
    wait: Wait,
    state: TaskState,
) -> None:
    insert(conn, state)
    with pytest.raises(MergeWaitError):
        wait()
    assert tokens.issued == []
    assert answers.calls == []


def test_a_task_without_a_pull_request_is_refused(
    conn: sqlite3.Connection, answers: Answers, tokens: Tokens, wait: Wait
) -> None:
    insert(conn, pr_url=None)
    with pytest.raises(MergeWaitError):
        wait()
    assert tokens.issued == []


def test_an_unknown_task_is_refused(
    conn: sqlite3.Connection, tokens: Tokens, wait: Wait
) -> None:
    with pytest.raises(MergeWaitError):
        wait()
    assert tokens.issued == []


def test_a_state_change_during_the_wait_is_refused_at_the_next_check(
    conn: sqlite3.Connection,
    answers: Answers,
    tokens: Tokens,
    time_: Time,
    wait: Wait,
) -> None:
    insert(conn)
    answers.repeat = OPEN
    time_.on_sleep = lambda: store.transition(
        conn, TASK, TaskState.PR_OPEN, TaskState.CLOSED, actor="test"
    )
    with pytest.raises(MergeWaitError):
        wait()
    assert len(answers.calls) == 1
    assert len(tokens.issued) == 1


def test_a_move_between_waitable_states_keeps_waiting_and_is_reported(
    conn: sqlite3.Connection, answers: Answers, time_: Time, wait: Wait
) -> None:
    insert(conn)
    answers.script = [OPEN, MERGED]
    time_.on_sleep = lambda: store.transition(
        conn, TASK, TaskState.PR_OPEN, TaskState.CONFLICT, actor="test"
    )
    report = wait()
    assert report.merged is True
    assert report.state is TaskState.CONFLICT


# It changes nothing and holds nothing


def test_a_wait_does_not_change_the_task(
    conn: sqlite3.Connection, answers: Answers, wait: Wait
) -> None:
    insert(conn)
    before = store.get_task(conn, TASK)
    events = conn.execute("SELECT count(*) FROM events").fetchone()
    answers.script = [OPEN, MERGED]
    wait()
    assert store.get_task(conn, TASK) == before
    assert conn.execute("SELECT count(*) FROM events").fetchone() == events


def test_no_transaction_is_open_while_sleeping(
    conn: sqlite3.Connection, db_path: Path, answers: Answers, time_: Time, wait: Wait
) -> None:
    insert(conn)
    answers.script = [OPEN, MERGED]
    seen: list[bool] = []

    def during_sleep() -> None:
        seen.append(conn.in_transaction)
        # Another process must be able to write during the whole wait.
        other = sqlite3.connect(db_path, timeout=0)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute("COMMIT")
        finally:
            other.close()

    time_.on_sleep = during_sleep
    wait()
    assert seen == [False]


# Failed checks


def test_a_failed_check_is_tried_again_at_the_next_interval(
    conn: sqlite3.Connection, answers: Answers, time_: Time, wait: Wait
) -> None:
    insert(conn)
    answers.script = [failure(), MERGED]
    report = wait()
    assert report.merged is True
    assert report.checks == 2
    assert time_.sleeps == [60]


def test_a_failed_token_is_a_failed_check(
    conn: sqlite3.Connection, answers: Answers, tokens: Tokens, wait: Wait
) -> None:
    insert(conn)
    tokens.fail_on = {1}
    answers.script = [MERGED]
    report = wait()
    assert report.merged is True
    assert report.checks == 2
    assert answers.calls == [(URL, "token-2")]


def test_ten_failures_in_a_row_stop_the_wait(
    conn: sqlite3.Connection, answers: Answers, time_: Time, wait: Wait
) -> None:
    insert(conn)
    answers.script = [failure() for _ in range(9)] + [failure("the last failure")]
    with pytest.raises(MergeCheckError, match="the last failure"):
        wait()
    assert len(answers.calls) == 10
    assert len(time_.sleeps) == 9


def test_ten_token_failures_in_a_row_stop_the_wait(
    conn: sqlite3.Connection, answers: Answers, tokens: Tokens, wait: Wait
) -> None:
    insert(conn)
    tokens.fail_on = set(range(1, 11))
    with pytest.raises(MergeCheckError):
        wait()
    assert len(tokens.issued) == 10
    assert answers.calls == []


def test_a_successful_check_resets_the_failure_count(
    conn: sqlite3.Connection, answers: Answers, wait: Wait
) -> None:
    insert(conn)
    nine = [failure() for _ in range(9)]
    answers.script = [*nine, OPEN, *nine, MERGED]
    report = wait()
    assert report.merged is True
    assert report.checks == 20


def test_the_state_check_comes_before_the_failure_count(
    conn: sqlite3.Connection, answers: Answers, tokens: Tokens, time_: Time, wait: Wait
) -> None:
    insert(conn)
    answers.script = [failure()]
    time_.on_sleep = lambda: store.transition(
        conn, TASK, TaskState.PR_OPEN, TaskState.CLOSED, actor="test"
    )
    with pytest.raises(MergeWaitError):
        wait()
    assert len(tokens.issued) == 1


# The wait limit


def test_the_limit_ends_the_wait_with_an_open_report(
    conn: sqlite3.Connection, answers: Answers, time_: Time, wait: Wait
) -> None:
    insert(conn)
    answers.repeat = OPEN
    report = wait(max_wait_min=2)
    assert report == waiter.MergeWaitReport(
        task_id=TASK,
        state=TaskState.PR_OPEN,
        pr_url=URL,
        pr_state="OPEN",
        merged=False,
        merged_at=None,
        checks=3,
    )
    assert time_.sleeps == [60, 60]


def test_the_default_limit_is_ninety_minutes(
    conn: sqlite3.Connection, answers: Answers, time_: Time, wait: Wait
) -> None:
    insert(conn)
    answers.repeat = OPEN
    report = wait()
    assert report.checks == 91
    assert sum(time_.sleeps) == 90 * 60


def test_the_last_sleep_is_cut_to_the_time_left(
    conn: sqlite3.Connection, answers: Answers, time_: Time, wait: Wait
) -> None:
    insert(conn)
    answers.repeat = OPEN
    report = wait(interval_sec=40, max_wait_min=1)
    assert time_.sleeps == [40, 20]
    assert report.checks == 3


def test_an_interval_over_the_limit_still_checks_at_the_limit(
    conn: sqlite3.Connection, answers: Answers, time_: Time, wait: Wait
) -> None:
    insert(conn)
    answers.repeat = OPEN
    report = wait(interval_sec=600, max_wait_min=1)
    assert time_.sleeps == [60]
    assert report.checks == 2


def test_a_merge_seen_at_the_limit_is_a_merge(
    conn: sqlite3.Connection, answers: Answers, wait: Wait
) -> None:
    insert(conn)
    answers.script = [OPEN, MERGED]
    assert wait(max_wait_min=1).merged is True


def test_the_limit_with_no_successful_check_reports_no_pull_request_state(
    conn: sqlite3.Connection, answers: Answers, wait: Wait
) -> None:
    insert(conn)
    answers.script = [failure(), failure()]
    report = wait(max_wait_min=1)
    assert (report.pr_state, report.merged, report.checks) == (None, False, 2)


def test_the_limit_after_a_failed_check_reports_the_last_known_state(
    conn: sqlite3.Connection, answers: Answers, wait: Wait
) -> None:
    insert(conn)
    answers.script = [OPEN, failure()]
    report = wait(max_wait_min=1)
    assert (report.pr_state, report.merged, report.checks) == ("OPEN", False, 2)


def test_the_failure_limit_wins_over_the_wait_limit(
    conn: sqlite3.Connection, answers: Answers, wait: Wait
) -> None:
    # The tenth check falls exactly on the wait limit.
    insert(conn)
    answers.script = [failure() for _ in range(10)]
    with pytest.raises(MergeCheckError):
        wait(max_wait_min=9)
