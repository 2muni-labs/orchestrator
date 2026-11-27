"""Tests for tracksync.py: the orch track sync engine.

FakeApi keeps issues and project items in memory and behaves as measured on
gh 2.102.0 on 2026-11-18: closing an issue turns its item's Status into Done,
reopening leaves the Status alone, and adding an item twice is harmless.
"""

import fcntl
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from orch import github, store, tracking, tracksync
from orch.github import (
    GitHubError,
    GitHubNotFoundError,
    Issue,
    Project,
    ProjectItem,
    PullRequestStatus,
)
from orch.models import Grade, Lane, Task, TaskState
from orch.redact import SecretFoundError
from orch.tracking import Record
from orch.tracksync import Change, Wanted

TARGET = "orchestrator"
REPO = "2muni-labs/orchestrator"
ISSUES = f"https://github.com/{REPO}/issues/"
PULLS = f"https://github.com/{REPO}/pull/"
PROJECT_URL = "https://github.com/orgs/2muni-labs/projects/7"
TOKEN = "ghs_" + "x" * 36
NOW = datetime(2026, 11, 18, 9, 30, 15, tzinfo=UTC)
MUTATING = {
    "create_issue",
    "update_issue_body",
    "set_issue_state",
    "add_project_item",
    "set_item_status",
}


class FakeApi:
    """In-memory issues and items of project 7, recording each call."""

    issue_number = staticmethod(github.issue_number)
    project_number = staticmethod(github.project_number)
    replace_managed_block = staticmethod(github.replace_managed_block)

    def __init__(self) -> None:
        self.issues: dict[int, Issue] = {}
        self.items: dict[int, list[str | None]] = {}
        self.prs: dict[str, str] = {}
        self.calls: list[str] = []
        self.touched: set[int] = set()
        self.tokens: set[str] = set()
        self.fail: dict[str, Exception] = {}
        self.fail_numbers: dict[int, Exception] = {}
        self.sticky_done = 0
        self.ignore_status = False
        self.next_number = 100

    def _call(self, name: str, token: str, number: int | None = None) -> None:
        self.calls.append(name)
        self.tokens.add(token)
        if number is not None:
            self.touched.add(number)
        if name in self.fail:
            raise self.fail[name]
        if number is not None and number in self.fail_numbers:
            raise self.fail_numbers[number]

    def add(
        self,
        number: int,
        *,
        state: str = "open",
        reason: str | None = None,
        body: str = "",
        status: str | None = "Todo",
    ) -> None:
        self.issues[number] = Issue(
            number=number,
            node_id=f"I_{number}",
            url=f"{ISSUES}{number}",
            title=f"issue {number}",
            state=state,
            state_reason=reason,
            body=body,
        )
        if status is not None:
            self.items[number] = [f"PVTI_{number}", status]

    def get_issue(self, *, github: str, number: int, token: str) -> Issue:
        assert github == REPO
        self._call("get_issue", token, number)
        if number not in self.issues:
            raise GitHubNotFoundError("api: gh: Not Found (HTTP 404)")
        return self.issues[number]

    def find_issues_by_marker(
        self, *, github: str, marker: str, token: str
    ) -> tuple[int, ...]:
        self._call("find_issues_by_marker", token)
        return tuple(
            sorted(
                n
                for n, issue in self.issues.items()
                if marker in [line.strip() for line in issue.body.splitlines()]
            )
        )

    def create_issue(
        self,
        *,
        github: str,
        title: str,
        body: str,
        token: str,
        home: Path,
        orch_home: Path,
    ) -> Issue:
        self._call("create_issue", token)
        assert body.splitlines()[0].startswith("<!-- orch-task:")
        number = self.next_number
        self.next_number += 1
        self.add(number, body=body, status=None)
        self.issues[number] = replace(self.issues[number], title=title)
        return self.issues[number]

    def update_issue_body(
        self,
        *,
        github: str,
        number: int,
        body: str,
        token: str,
        home: Path,
        orch_home: Path,
    ) -> Issue:
        self._call("update_issue_body", token, number)
        self.issues[number] = replace(self.issues[number], body=body)
        return self.issues[number]

    def set_issue_state(
        self, *, github: str, number: int, state: str, reason: str | None, token: str
    ) -> Issue:
        self._call("set_issue_state", token, number)
        new_reason = reason if state == "closed" else "reopened"
        self.issues[number] = replace(
            self.issues[number], state=state, state_reason=new_reason
        )
        if state == "closed" and number in self.items:
            self.items[number][1] = "Done"
        return self.issues[number]

    def get_project(self, *, owner: str, number: int, token: str) -> Project:
        self._call("get_project", token)
        assert (owner, number) == ("2muni-labs", 7)
        return PROJECT

    def add_project_item(self, *, project: Project, issue: Issue, token: str) -> str:
        self._call("add_project_item", token, issue.number)
        if issue.number not in self.items:
            self.items[issue.number] = [f"PVTI_{issue.number}", "Todo"]
        return self.items[issue.number][0]

    def issue_project_item(
        self, *, github: str, number: int, project: Project, token: str
    ) -> ProjectItem | None:
        self._call("issue_project_item", token, number)
        if number not in self.items:
            return None
        item_id, status = self.items[number]
        return ProjectItem(id=item_id, project_id=project.id, status=status)

    def set_item_status(
        self, *, project: Project, item_id: str, status: str, token: str
    ) -> None:
        self._call("set_item_status", token)
        number = int(item_id.removeprefix("PVTI_"))
        if self.ignore_status:
            return
        if status == "Cancelled" and self.sticky_done > 0:
            self.sticky_done -= 1
            self.items[number][1] = "Done"
            return
        self.items[number][1] = status

    def pull_request_status(self, *, url: str, token: str) -> PullRequestStatus:
        self._call("pull_request_status", token)
        state = self.prs[url]
        return PullRequestStatus(state=state, merged=state == "MERGED", merged_at=None)

    def status(self, number: int) -> str | None:
        return self.items[number][1] if number in self.items else None

    def mutations(self) -> list[str]:
        return [name for name in self.calls if name in MUTATING]


PROJECT = Project(
    id="PVT_7",
    owner="2muni-labs",
    number=7,
    status_field_id="PVTSSF_7",
    status_options={
        "Todo": "o1",
        "In Progress": "o2",
        "Done": "o3",
        "Blocked": "o4",
        "Cancelled": "o5",
    },
)


def make_task(
    task_id: str,
    state: TaskState,
    *,
    updated: str = "2026-11-18T00:00:00+00:00",
    pr_url: str | None = None,
) -> Task:
    return Task(
        id=task_id,
        target=TARGET,
        title=task_id,
        grade=Grade.L2,
        lane=Lane.B,
        model="m",
        state=state,
        timeout_sec=60,
        created_at="2026-11-18T00:00:00+00:00",
        updated_at=updated,
        pr_url=pr_url,
    )


def record(
    record_id: str, owner: str, *, issue: int | None = None, **extra: str
) -> dict:
    value = {
        "title": f"[track] {record_id}",
        "kind": "task" if owner == "task" else "backlog",
        "project_key": "G05",
        "completion_owner": owner,
        **extra,
    }
    if issue is not None:
        value["issue_url"] = f"{ISSUES}{issue}"
    return value


def tracking_file() -> dict:
    return {
        "schema_version": 1,
        "target": TARGET,
        "repository": REPO,
        "projects": {"G05": {"title": "orch 05", "url": PROJECT_URL}},
        "records": {
            "t-run": record("t-run", "task", issue=1),
            "t-done": record("t-done", "task", issue=2),
            "t-gone": record("t-gone", "task", issue=3),
            "h-merged": record(
                "h-merged",
                "harness",
                issue=4,
                branch="task/h-merged",
                pr_url=f"{PULLS}10",
            ),
            "old": record("old", "manual", issue=5),
            "planned": record("planned", "task", issue=6),
            "new": record("new", "task"),
        },
        "tasks": {
            "m-run": {"record_id": "t-run"},
            "m-done": {"record_id": "t-done"},
            "m-gone": {"record_id": "t-gone"},
            "m-planned": {"record_id": "planned"},
        },
        "aliases": {},
        "sync": {"t-run": {"revision": 1}},
    }


@pytest.fixture
def orch_home(tmp_path: Path) -> Path:
    home = tmp_path / "orch"
    (home / TARGET).mkdir(parents=True)
    (home / TARGET / "github-tracking.json").write_text(
        json.dumps(tracking_file()), encoding="utf-8"
    )
    return home


@pytest.fixture
def conn(orch_home: Path) -> sqlite3.Connection:
    conn = store.connect(orch_home / "state.db")
    store.insert_task(conn, make_task("m-run", TaskState.RUNNING))
    store.insert_task(conn, make_task("m-done", TaskState.CLOSED, pr_url=f"{PULLS}20"))
    store.insert_task(conn, make_task("m-gone", TaskState.DISCARDED))
    return conn


@pytest.fixture
def api() -> FakeApi:
    fake = FakeApi()
    fake.add(1, status="Todo")
    fake.add(2, status="In Progress")
    fake.add(3, status="Todo")
    fake.add(4, body="user text\n", status="In Progress")
    fake.add(5, state="closed", reason="completed", status="Done")
    fake.add(6, status="Todo")
    fake.prs[f"{PULLS}10"] = "MERGED"
    return fake


class Run:
    """Calls sync with the test's defaults and records sleeps and notifications."""

    def __init__(self, conn: sqlite3.Connection, orch_home: Path, api: FakeApi) -> None:
        self.conn = conn
        self.orch_home = orch_home
        self.api = api
        self.sleeps: list[float] = []
        self.notifications = 0
        self.token_calls = 0

    def token(self) -> str:
        self.token_calls += 1
        return TOKEN

    def notify(self) -> None:
        self.notifications += 1

    def __call__(self, **overrides: object) -> tracksync.SyncReport:
        values: dict[str, object] = {
            "conn": self.conn,
            "target": TARGET,
            "github_repo": REPO,
            "orch_home": self.orch_home,
            "home": self.orch_home.parent,
            "now": NOW,
            "token_provider": self.token,
            "notify_failure": self.notify,
            "api": self.api,
            "sleep": self.sleeps.append,
        }
        values.update(overrides)
        return tracksync.sync(**values)


@pytest.fixture
def run(conn: sqlite3.Connection, orch_home: Path, api: FakeApi) -> Run:
    return Run(conn, orch_home, api)


def snapshot(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    return {
        t: conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall() for t in tables
    }


# Pure functions


def test_marker() -> None:
    assert (
        tracksync.marker("orchestrator", "m-x") == "<!-- orch-task:orchestrator:m-x -->"
    )


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (TaskState.PREPARED, ("open", None, "Todo")),
        (TaskState.QUEUED, ("open", None, "In Progress")),
        (TaskState.RESERVED, ("open", None, "In Progress")),
        (TaskState.RUNNING, ("open", None, "In Progress")),
        (TaskState.JUDGING, ("open", None, "In Progress")),
        (TaskState.GATING, ("open", None, "In Progress")),
        (TaskState.REVIEWING, ("open", None, "In Progress")),
        (TaskState.READY, ("open", None, "In Progress")),
        (TaskState.PR_OPEN, ("open", None, "In Progress")),
        (TaskState.BLOCKED, ("open", None, "Blocked")),
        (TaskState.CONFLICT, ("open", None, "Blocked")),
        (TaskState.ORPHANED, ("open", None, "Blocked")),
        (TaskState.CLOSED, ("closed", "completed", "Done")),
        (TaskState.DISCARDED, ("closed", "not_planned", "Cancelled")),
    ],
)
def test_task_wanted(state: TaskState, expected: tuple) -> None:
    assert tracksync.task_wanted(state) == expected


def test_every_task_state_has_a_wanted_state() -> None:
    for state in TaskState:
        tracksync.task_wanted(state)


def rec(owner: str, **fields: object) -> Record:
    values = {
        "record_id": "r",
        "title": "t",
        "kind": "task",
        "completion_owner": owner,
        "project_key": "G05",
        "issue_url": f"{ISSUES}1",
    }
    values.update(fields)
    return Record(**values)


def test_task_record_uses_the_latest_task() -> None:
    tasks = [
        make_task("m-a", TaskState.DISCARDED, updated="2026-11-18T01:00:00+00:00"),
        make_task(
            "m-b",
            TaskState.PR_OPEN,
            updated="2026-11-18T02:00:00+00:00",
            pr_url=f"{PULLS}3",
        ),
    ]
    assert tracksync.compute_wanted(rec("task"), tasks=tasks, pr_state=None) == Wanted(
        issue_state="open",
        reason=None,
        status="In Progress",
        pr_url=f"{PULLS}3",
        task_id="m-b",
    )


def test_task_record_breaks_a_tie_by_id() -> None:
    tasks = [make_task("m-b", TaskState.CLOSED), make_task("m-a", TaskState.DISCARDED)]
    assert (
        tracksync.compute_wanted(rec("task"), tasks=tasks, pr_state=None).task_id
        == "m-b"
    )


def test_task_record_falls_back_to_the_record_pr_url() -> None:
    wanted = tracksync.compute_wanted(
        rec("task", pr_url=f"{PULLS}9"),
        tasks=[make_task("m-a", TaskState.CLOSED)],
        pr_state=None,
    )
    assert wanted == Wanted(
        issue_state="closed",
        reason="completed",
        status="Done",
        pr_url=f"{PULLS}9",
        task_id="m-a",
    )


def test_task_record_without_tasks() -> None:
    assert tracksync.compute_wanted(rec("task"), tasks=[], pr_state=None) is None
    assert tracksync.compute_wanted(
        rec("task", issue_url=None, pr_url=f"{PULLS}2"), tasks=[], pr_state=None
    ) == Wanted(
        issue_state="open", reason=None, status="Todo", pr_url=f"{PULLS}2", task_id=None
    )


@pytest.mark.parametrize(
    ("pr_state", "expected"),
    [
        ("OPEN", ("open", None, "In Progress")),
        ("MERGED", ("closed", "completed", "Done")),
        ("CLOSED", ("closed", "not_planned", "Cancelled")),
    ],
)
def test_harness_record_follows_its_pull_request(
    pr_state: str, expected: tuple
) -> None:
    wanted = tracksync.compute_wanted(
        rec("harness", pr_url=f"{PULLS}5"), tasks=[], pr_state=pr_state
    )
    assert (wanted.issue_state, wanted.reason, wanted.status) == expected
    assert (wanted.pr_url, wanted.task_id) == (f"{PULLS}5", None)


def test_harness_record_without_a_pull_request() -> None:
    assert tracksync.compute_wanted(rec("harness"), tasks=[], pr_state=None) is None
    assert (
        tracksync.compute_wanted(
            rec("harness", issue_url=None), tasks=[], pr_state=None
        ).status
        == "Todo"
    )


@pytest.mark.parametrize("owner", ["manual", "aggregate"])
def test_manual_and_aggregate_records_close_only_when_accepted(owner: str) -> None:
    assert tracksync.compute_wanted(rec(owner), tasks=[], pr_state=None) is None
    assert (
        tracksync.compute_wanted(rec(owner, issue_url=None), tasks=[], pr_state=None)
        is None
    )
    assert tracksync.compute_wanted(
        rec(owner, accepted_at="t", pr_url=f"{PULLS}4"), tasks=[], pr_state=None
    ) == Wanted(
        issue_state="closed",
        reason="completed",
        status="Done",
        pr_url=f"{PULLS}4",
        task_id=None,
    )


def test_as_dict_has_the_five_fields() -> None:
    wanted = Wanted(
        issue_state="open", reason=None, status="Todo", pr_url=None, task_id="m"
    )
    assert wanted.as_dict() == {
        "issue_state": "open",
        "reason": None,
        "status": "Todo",
        "pr_url": None,
        "task_id": "m",
    }


def test_progress_block() -> None:
    wanted = Wanted(
        issue_state="open",
        reason=None,
        status="In Progress",
        pr_url=f"{PULLS}3",
        task_id="m-b",
    )
    later = datetime(2026, 11, 18, 18, 30, 59, tzinfo=timezone(timedelta(hours=9)))
    assert tracksync.progress_block(wanted, now=later) == (
        "orch 자동 기록 (이 블록은 orch track sync가 고칩니다)\n"
        "상태: In Progress\n"
        "작업: m-b\n"
        f"PR: {PULLS}3\n"
        "갱신: 2026-11-18T09:30Z"
    )
    empty = replace(wanted, pr_url=None, task_id=None)
    assert "작업: -\nPR: -\n" in tracksync.progress_block(empty, now=NOW)


# A real run


def test_first_run_imports_and_brings_github_in_step(
    run: Run, api: FakeApi, conn: sqlite3.Connection
) -> None:
    report = run()
    assert report.outcome == "synced"
    assert report.imported is True
    assert tracking.repository(conn, TARGET) == REPO
    assert (
        report.skipped == 2
    )  # old (manual, not accepted) and planned (no task, has issue)
    assert report.checked == 5
    assert report.written == ("h-merged", "new", "t-done", "t-gone", "t-run")
    assert report.failed == ()
    assert report.unchanged == 0
    assert report.changes == ()
    assert report.notified is False
    assert run.token_calls == 1
    assert api.tokens == {TOKEN}

    assert (api.issues[1].state, api.status(1)) == ("open", "In Progress")
    assert (api.issues[2].state, api.issues[2].state_reason, api.status(2)) == (
        "closed",
        "completed",
        "Done",
    )
    assert (api.issues[3].state, api.issues[3].state_reason, api.status(3)) == (
        "closed",
        "not_planned",
        "Cancelled",
    )
    assert (api.issues[4].state, api.status(4)) == ("closed", "Done")
    assert api.issues[5].state == "closed" and api.status(5) == "Done"
    assert api.issues[6].state == "open" and api.status(6) == "Todo"


def test_skipped_records_get_no_calls(run: Run, api: FakeApi) -> None:
    run()
    assert api.touched.isdisjoint({5, 6})
    assert api.issues[5].body == "" and api.issues[6].body == ""


def test_verified_values_are_stored(run: Run, conn: sqlite3.Connection) -> None:
    run()
    state = tracking.get_state(conn, TARGET, "t-gone")
    assert state.verified == {
        "issue_state": "closed",
        "reason": "not_planned",
        "status": "Cancelled",
        "pr_url": None,
        "task_id": "m-gone",
    }
    assert state.failures == 0
    assert state.verified_at == NOW.isoformat()
    assert tracking.get_state(conn, TARGET, "t-done").verified["pr_url"] == f"{PULLS}20"
    sync_run = tracking.get_run(conn, TARGET)
    assert (sync_run.last_success_at, sync_run.consecutive_failures) == (
        NOW.isoformat(),
        0,
    )


def test_the_block_is_written_and_user_text_kept(run: Run, api: FakeApi) -> None:
    run()
    body = api.issues[4].body
    assert body.startswith("user text\n\n<!-- orch-progress:begin -->\n")
    assert "상태: Done\n" in body
    assert f"PR: {PULLS}10\n" in body
    assert body.endswith("<!-- orch-progress:end -->\n")


def test_a_new_record_gets_an_issue_with_marker_and_block(
    run: Run, api: FakeApi, conn: sqlite3.Connection
) -> None:
    run()
    created = api.issues[100]
    lines = created.body.splitlines()
    assert lines[0] == "<!-- orch-task:orchestrator:new -->"
    assert "<!-- orch-progress:begin -->" in lines and "상태: Todo" in lines
    assert created.title == "[track] new"
    assert api.status(100) == "Todo"
    assert tracking.get_record(conn, TARGET, "new").issue_url == f"{ISSUES}100"
    assert api.calls.count("create_issue") == 1
    assert api.calls.count("update_issue_body") == 4  # the four existing issues written


def test_closing_waits_before_setting_status(run: Run) -> None:
    run()
    assert (
        run.sleeps == [tracksync.SETTLE_SEC] * 3
    )  # t-done, t-gone, h-merged closed now


def test_cancelled_is_reapplied_once_after_an_overwrite(run: Run, api: FakeApi) -> None:
    api.sticky_done = 1
    report = run()
    assert "t-gone" in report.written
    assert api.status(3) == "Cancelled"


def test_cancelled_overwritten_twice_is_a_read_back_mismatch(
    run: Run, api: FakeApi, conn: sqlite3.Connection
) -> None:
    api.sticky_done = 2
    report = run()
    assert ("t-gone", "read_back_mismatch") in report.failed
    assert report.outcome == "failed"
    assert tracking.get_state(conn, TARGET, "t-gone").failures == 1
    assert tracking.get_state(conn, TARGET, "t-gone").verified is None


def test_second_run_changes_nothing_and_reads_no_issue(run: Run, api: FakeApi) -> None:
    run()
    api.calls.clear()
    report = run()
    assert report.outcome == "synced"
    assert report.imported is False
    # new now has its issue and no task, so it is left alone from here on
    assert (report.unchanged, report.skipped) == (4, 3)
    assert report.written == ()
    assert api.mutations() == []
    assert not {"get_issue", "issue_project_item", "find_issues_by_marker"} & set(
        api.calls
    )


def test_a_task_moving_on_is_written_again(
    run: Run, api: FakeApi, conn: sqlite3.Connection
) -> None:
    run()
    conn.execute("UPDATE tasks SET state = 'blocked' WHERE id = 'm-run'")
    report = run()
    assert report.written == ("t-run",)
    assert api.status(1) == "Blocked"


def test_drift_on_github_is_not_seen_until_the_wanted_state_changes(
    run: Run, api: FakeApi, conn: sqlite3.Connection
) -> None:
    run()
    api.items[1][1] = "Done"
    assert run().written == ()
    conn.execute("UPDATE tasks SET state = 'pr_open' WHERE id = 'm-run'")
    conn.execute("UPDATE tasks SET pr_url = ? WHERE id = 'm-run'", (f"{PULLS}30",))
    assert run().written == ("t-run",)
    assert api.status(1) == "In Progress"


def test_a_closed_issue_whose_task_is_running_is_reopened(
    run: Run, api: FakeApi
) -> None:
    api.add(1, state="closed", reason="completed", status="Done")
    run()
    assert (api.issues[1].state, api.issues[1].state_reason, api.status(1)) == (
        "open",
        "reopened",
        "In Progress",
    )


def test_an_issue_missing_from_the_project_is_added(run: Run, api: FakeApi) -> None:
    del api.items[1]
    run()
    assert api.status(1) == "In Progress"


def test_an_accepted_manual_record_is_closed(
    run: Run, api: FakeApi, conn: sqlite3.Connection
) -> None:
    run()
    api.add(5, status="In Progress")
    tracking.accept_record(conn, target=TARGET, record_id="old", at="t")
    report = run()
    assert report.written == ("old",)
    assert (api.issues[5].state, api.issues[5].state_reason, api.status(5)) == (
        "closed",
        "completed",
        "Done",
    )


def test_task_states_are_never_changed(run: Run, conn: sqlite3.Connection) -> None:
    before = conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
    run()
    assert conn.execute("SELECT * FROM tasks ORDER BY id").fetchall() == before


def test_the_tracking_file_is_not_changed(run: Run, orch_home: Path) -> None:
    path = orch_home / TARGET / "github-tracking.json"
    before = path.read_bytes()
    run()
    assert path.read_bytes() == before


# Finding an issue by its marker


def test_an_issue_with_the_marker_is_adopted_instead_of_created(
    run: Run, api: FakeApi, conn: sqlite3.Connection
) -> None:
    api.add(
        50, body="<!-- orch-task:orchestrator:new -->\nlost create answer", status=None
    )
    report = run()
    assert "new" in report.written
    assert "create_issue" not in api.calls
    assert tracking.get_record(conn, TARGET, "new").issue_url == f"{ISSUES}50"
    assert api.issues[50].body.startswith("<!-- orch-task:orchestrator:new -->\n")
    assert api.status(50) == "Todo"


def test_two_issues_with_the_marker_fail_the_record(
    run: Run, api: FakeApi, conn: sqlite3.Connection
) -> None:
    for number in (50, 51):
        api.add(number, body="<!-- orch-task:orchestrator:new -->", status=None)
    report = run()
    assert ("new", "duplicate_issues") in report.failed
    assert "create_issue" not in api.calls
    assert tracking.get_record(conn, TARGET, "new").issue_url is None
    assert "t-run" in report.written


# Failures


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (GitHubNotFoundError("api: gh: Not Found (HTTP 404)"), "not_found"),
        (GitHubError("api: gh: boom (HTTP 502)"), "api_error"),
    ],
)
def test_a_failing_issue_fails_only_its_record(
    run: Run, api: FakeApi, conn: sqlite3.Connection, error: Exception, code: str
) -> None:
    api.fail_numbers[1] = error
    report = run()
    assert report.failed == (("t-run", code),)
    assert report.outcome == "failed"
    assert set(report.written) == {"h-merged", "new", "t-done", "t-gone"}
    assert tracking.get_state(conn, TARGET, "t-run").failures == 1
    assert tracking.get_state(conn, TARGET, "t-run").last_error == code
    sync_run = tracking.get_run(conn, TARGET)
    assert (sync_run.consecutive_failures, sync_run.last_error) == (1, "records_failed")


def test_a_tracking_error_after_creating_fails_only_its_record(
    run: Run,
    api: FakeApi,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(conn: sqlite3.Connection, **kwargs: object) -> None:
        raise tracking.TrackingError("private detail")

    monkeypatch.setattr(tracking, "set_issue_url", refuse)
    report = run()
    assert report.failed == (("new", "tracking_error"),)
    assert report.outcome == "failed"
    assert set(report.written) == {"h-merged", "t-done", "t-gone", "t-run"}
    assert tracking.get_record(conn, TARGET, "new").issue_url is None
    state = tracking.get_state(conn, TARGET, "new")
    assert (state.failures, state.last_error) == (1, "tracking_error")
    sync_run = tracking.get_run(conn, TARGET)
    assert (sync_run.consecutive_failures, sync_run.last_error) == (1, "records_failed")
    assert "private" not in str(report)


def test_a_record_that_failed_after_creating_adopts_its_issue_next_run(
    run: Run,
    api: FakeApi,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(conn: sqlite3.Connection, **kwargs: object) -> None:
        raise tracking.TrackingError("x")

    with monkeypatch.context() as patch:
        patch.setattr(tracking, "set_issue_url", refuse)
        run()
    created = api.calls.count("create_issue")
    report = run()
    assert "new" in report.written
    assert api.calls.count("create_issue") == created
    assert tracking.get_record(conn, TARGET, "new").issue_url is not None


def test_error_code_of_a_tracking_error() -> None:
    assert tracksync._error_code(tracking.TrackingError("x")) == "tracking_error"


def test_a_failed_record_is_tried_again_next_run(
    run: Run, api: FakeApi, conn: sqlite3.Connection
) -> None:
    api.fail_numbers[1] = GitHubError("x")
    run()
    del api.fail_numbers[1]
    report = run()
    assert report.written == ("t-run",)
    assert tracking.get_state(conn, TARGET, "t-run").failures == 0


def test_a_redaction_failure_is_a_record_failure(run: Run, api: FakeApi) -> None:
    api.fail["update_issue_body"] = SecretFoundError("secret")
    report = run()
    assert ("t-run", "redaction_failed") in report.failed


def test_a_malformed_block_is_a_record_failure(run: Run, api: FakeApi) -> None:
    api.add(1, body="<!-- orch-progress:begin -->\nno end", status="Todo")
    report = run()
    assert ("t-run", "block_malformed") in report.failed
    assert api.issues[1].state == "open"


def test_a_status_that_does_not_stick_is_a_read_back_mismatch(
    run: Run, api: FakeApi
) -> None:
    api.ignore_status = True
    report = run()
    assert ("t-run", "read_back_mismatch") in report.failed
    assert ("t-gone", "read_back_mismatch") in report.failed


def test_a_missing_item_after_adding_is_item_missing(run: Run, api: FakeApi) -> None:
    api.issue_project_item = lambda **kwargs: None
    report = run()
    assert ("t-run", "item_missing") in report.failed


def test_a_pull_request_read_failure_fails_the_harness_record(
    run: Run, api: FakeApi
) -> None:
    api.fail["pull_request_status"] = GitHubError("x")
    report = run()
    assert ("h-merged", "api_error") in report.failed
    assert "t-run" in report.written


def test_a_project_failure_fails_its_records(run: Run, api: FakeApi) -> None:
    api.fail["get_project"] = GitHubError("x")
    report = run()
    assert report.written == ()
    assert {code for _, code in report.failed} == {"project_error"}
    assert {rid for rid, _ in report.failed} == {
        "h-merged",
        "new",
        "t-done",
        "t-gone",
        "t-run",
    }
    assert api.mutations() == []


def test_a_token_failure_fails_the_run(
    run: Run, api: FakeApi, conn: sqlite3.Connection
) -> None:
    def broken() -> str:
        raise RuntimeError(f"Token script failed {TOKEN}")

    report = run(token_provider=broken)
    assert report.outcome == "failed"
    assert report.failed == (("*", "auth_failed"),)
    assert api.calls == []
    assert TOKEN not in repr(report)
    assert tracking.get_run(conn, TARGET).last_error == "auth_failed"


def test_another_repository_fails_without_calling_github(
    run: Run, api: FakeApi, conn: sqlite3.Connection
) -> None:
    report = run(github_repo="2muni-labs/other")
    assert report.outcome == "failed"
    assert report.failed == (("*", "repository_mismatch"),)
    assert api.calls == []
    assert run.token_calls == 0
    assert tracking.get_run(conn, TARGET).last_error == "repository_mismatch"


def test_the_repository_compares_without_case(run: Run) -> None:
    assert run(github_repo="2Muni-Labs/Orchestrator").outcome == "synced"


def test_no_file_and_no_import_is_not_configured(
    run: Run, api: FakeApi, conn: sqlite3.Connection, orch_home: Path
) -> None:
    (orch_home / TARGET / "github-tracking.json").unlink()
    before = snapshot(conn)
    report = run()
    assert report.outcome == "not_configured"
    assert api.calls == [] and run.token_calls == 0
    assert snapshot(conn) == before


def test_a_held_lock_stops_the_run(
    run: Run, api: FakeApi, conn: sqlite3.Connection, orch_home: Path
) -> None:
    before = snapshot(conn)
    with open(orch_home / TARGET / "track.lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = run()
    assert report.outcome == "locked"
    assert api.calls == [] and run.token_calls == 0
    assert snapshot(conn) == before
    assert run().outcome == "synced"


def test_the_lock_is_released_after_an_exception(run: Run, api: FakeApi) -> None:
    api.fail["get_issue"] = KeyError("unexpected")
    with pytest.raises(KeyError):
        run()
    api.fail.clear()
    assert run().outcome == "synced"


def test_notification_only_when_failures_reach_three(run: Run, api: FakeApi) -> None:
    api.fail_numbers[1] = GitHubError("x")
    outcomes = [run().notified for _ in range(5)]
    assert outcomes == [False, False, True, False, False]
    assert run.notifications == 1


def test_a_success_resets_the_count(run: Run, api: FakeApi) -> None:
    api.fail_numbers[1] = GitHubError("x")
    run()
    run()
    del api.fail_numbers[1]
    assert run().outcome == "synced"
    api.fail["get_project"] = GitHubError("x")  # fails every record on every run
    assert [run().notified for _ in range(3)] == [False, False, True]


def test_a_failing_notifier_does_not_break_the_run(run: Run, api: FakeApi) -> None:
    def broken() -> None:
        raise RuntimeError("ntfy down")

    api.fail_numbers[1] = GitHubError("x")
    run()
    run()
    report = run(notify_failure=broken)
    assert report.notified is False
    assert report.outcome == "failed"


def test_auth_failures_also_notify_on_the_third(run: Run) -> None:
    def broken() -> str:
        raise RuntimeError("no token")

    assert [run(token_provider=broken).notified for _ in range(3)] == [
        False,
        False,
        True,
    ]


# Dry run


def test_dry_run_reads_only_and_writes_nothing(
    run: Run, api: FakeApi, conn: sqlite3.Connection, orch_home: Path
) -> None:
    before = snapshot(conn)
    report = run(dry_run=True)
    assert report.outcome == "dry_run"
    assert report.imported is False
    assert api.mutations() == []
    assert run.sleeps == []
    assert run.notifications == 0
    assert snapshot(conn) == before
    assert tracking.repository(conn, TARGET) is None
    assert report.written == ()
    assert report.checked == 5 and report.skipped == 2
    assert {c.record_id: c.actions for c in report.changes} == {
        "h-merged": ("close", "set_status"),
        "new": ("create_issue", "add_item", "set_status"),
        "t-done": ("close", "set_status"),
        "t-gone": ("close", "set_status"),
        "t-run": ("set_status",),
    }
    assert all(isinstance(c, Change) for c in report.changes)
    assert report.unchanged == 0


def test_dry_run_after_a_sync_reports_no_change(run: Run, api: FakeApi) -> None:
    run()
    api.calls.clear()
    report = run(dry_run=True)
    assert report.changes == ()
    assert (report.unchanged, report.skipped) == (4, 3)
    assert api.mutations() == []


def test_dry_run_compares_with_github_not_the_cache(run: Run, api: FakeApi) -> None:
    run()
    api.add(1, state="closed", reason="completed", status=None)
    del api.items[1]
    report = run(dry_run=True)
    assert {c.record_id: c.actions for c in report.changes} == {
        "t-run": ("reopen", "add_item", "set_status")
    }


def test_dry_run_takes_no_lock(run: Run, orch_home: Path) -> None:
    with open(orch_home / TARGET / "track.lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert run(dry_run=True).outcome == "dry_run"


def test_dry_run_reports_read_failures(
    run: Run, api: FakeApi, conn: sqlite3.Connection
) -> None:
    api.fail_numbers[1] = GitHubError("x")
    report = run(dry_run=True)
    assert ("t-run", "api_error") in report.failed
    assert tracking.get_run(conn, TARGET) is None


def test_dry_run_token_failure(run: Run, conn: sqlite3.Connection) -> None:
    def broken() -> str:
        raise RuntimeError("no token")

    report = run(dry_run=True, token_provider=broken)
    assert (report.outcome, report.failed) == ("failed", (("*", "auth_failed"),))
    assert tracking.get_run(conn, TARGET) is None
    assert run.notifications == 0


# tracking.set_issue_url


def test_set_issue_url(conn: sqlite3.Connection, orch_home: Path) -> None:
    tracking.import_file(
        conn, orch_home / TARGET / "github-tracking.json", target=TARGET, at="t"
    )
    tracking.set_issue_url(
        conn, target=TARGET, record_id="new", issue_url=f"{ISSUES}77"
    )
    assert tracking.get_record(conn, TARGET, "new").issue_url == f"{ISSUES}77"
    tracking.set_issue_url(
        conn, target=TARGET, record_id="new", issue_url=f"{ISSUES}77"
    )
    assert conn.in_transaction is False


@pytest.mark.parametrize(
    ("record_id", "url"),
    [
        ("nope", f"{ISSUES}77"),
        ("new", "https://github.com/2muni-labs/other/issues/77"),
        ("new", f"{PULLS}77"),
        ("new", f"{ISSUES}0"),
        ("new", f"{ISSUES}1"),  # already the issue of t-run
    ],
)
def test_set_issue_url_refuses(
    conn: sqlite3.Connection, orch_home: Path, record_id: str, url: str
) -> None:
    tracking.import_file(
        conn, orch_home / TARGET / "github-tracking.json", target=TARGET, at="t"
    )
    with pytest.raises(tracking.TrackingError):
        tracking.set_issue_url(conn, target=TARGET, record_id=record_id, issue_url=url)
    assert tracking.get_record(conn, TARGET, "new").issue_url is None


def test_set_issue_url_needs_an_imported_target(conn: sqlite3.Connection) -> None:
    with pytest.raises(tracking.TrackingError):
        tracking.set_issue_url(
            conn, target=TARGET, record_id="new", issue_url=f"{ISSUES}77"
        )


def test_tracksync_imports_only_the_standard_library_and_orch() -> None:
    import ast
    import sys

    tree = ast.parse(Path(tracksync.__file__).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    assert names <= set(sys.stdlib_module_names) | {"orch"}
    source = Path(tracksync.__file__).read_text(encoding="utf-8")
    assert "subprocess" not in source and '"gh"' not in source
