"""plans.py: per-group plan files from state.db and the task bodies' priority lines."""

import ast
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orch import config, plans, render, store, tracking
from orch.models import Grade, Lane, Task, TaskState
from orch.render import PlanEntry
from orch.tracking import Project

TARGET = "orchestrator"
OTHER = "alpha"
REPO = "2muni-labs/orchestrator"
ISSUE = f"https://github.com/{REPO}/issues/"
PULL = f"https://github.com/{REPO}/pull/"
G05_URL = "https://github.com/orgs/2muni-labs/projects/7"
G01_URL = "https://github.com/orgs/2muni-labs/projects/3"
NOW = datetime(2026, 11, 19, 3, 0, 0, tzinfo=UTC)


def record(
    owner: str, project: str = "G05", issue: int | None = None, **extra: str
) -> dict:
    value: dict[str, object] = {
        "title": f"[{owner}] record",
        "kind": "task" if owner == "task" else "backlog",
        "project_key": project,
        "completion_owner": owner,
    }
    if issue is not None:
        value["issue_url"] = ISSUE + str(issue)
    value.update(extra)
    return value


def tracking_data() -> dict:
    return {
        "schema_version": 1,
        "target": TARGET,
        "repository": REPO,
        "projects": {
            "G01": {"title": "orch 01", "url": G01_URL},
            "G05": {"title": "orch 05 — 파이프라인", "url": G05_URL},
            "G07": {
                "title": "orch 07",
                "url": "https://github.com/orgs/2muni-labs/projects/9",
            },
        },
        "records": {
            "m-render": record("task", issue=516),
            "m-empty": record("task", issue=520),
            "h-docs": record("harness", issue=509, branch="task/h-docs"),
            "B03": record("manual", project="G01", issue=300),
            "done-task": record("task", project="G07", issue=400),
        },
        "tasks": {
            "m-render": {"record_id": "m-render"},
            "m-render-2": {"record_id": "m-render"},
            "m-ghost": {"record_id": "m-render"},
            "m-done": {"record_id": "done-task"},
        },
        "aliases": {},
    }


def make_task(
    task_id: str,
    state: TaskState,
    updated: str,
    *,
    target: str = TARGET,
    depends_on: tuple[str, ...] = (),
) -> Task:
    return Task(
        id=task_id,
        target=target,
        title=task_id,
        grade=Grade.L2,
        lane=Lane.B,
        model="m",
        state=state,
        timeout_sec=60,
        created_at="2026-11-18T00:00:00+00:00",
        updated_at=updated,
        depends_on=depends_on,
    )


def import_data(conn: sqlite3.Connection, folder: Path, data: dict) -> None:
    path = folder / f"{data['target']}.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tracking.import_file(conn, path, target=data["target"], at="t0")


def spec(orch_home: Path, task_id: str, body: str, target: str = TARGET) -> None:
    path = config.target_paths(orch_home, target).tasks / f"{task_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'+++\nid = "{task_id}"\n+++\n\n{body}', encoding="utf-8")


@pytest.fixture
def orch_home(tmp_path: Path) -> Path:
    return tmp_path / "runtime"


@pytest.fixture
def conn(tmp_path: Path, orch_home: Path) -> sqlite3.Connection:
    connection = store.connect(config.state_db_path(orch_home))
    import_data(connection, tmp_path, tracking_data())
    return connection


def plan_dir(orch_home: Path, target: str = TARGET) -> Path:
    return config.target_paths(orch_home, target).plans


def read_plan(orch_home: Path, key: str) -> str:
    return (plan_dir(orch_home) / f"{key}.md").read_text(encoding="utf-8")


def add(conn: sqlite3.Connection, *tasks: Task) -> dict[str, Task]:
    for task in tasks:
        store.insert_task(conn, task)
    return {task.id: task for task in tasks}


def project(conn: sqlite3.Connection, key: str) -> Project:
    return next(p for p in tracking.list_projects(conn, TARGET) if p.key == key)


def test_no_tracking_for_the_target_writes_nothing(
    orch_home: Path, tmp_path: Path
) -> None:
    connection = store.connect(config.state_db_path(orch_home))
    add(connection, make_task("m-x", TaskState.QUEUED, "2026-11-18T01:00:00+00:00"))
    assert plans.refresh_plans(connection, orch_home, TARGET, NOW) == ((), ())
    assert not plan_dir(orch_home).exists()


def test_open_groups_get_a_file_from_render(
    conn: sqlite3.Connection, orch_home: Path
) -> None:
    tasks = add(
        conn,
        # The later update decides; created_at and id order do not.
        make_task("m-render-2", TaskState.DISCARDED, "2026-11-18T01:00:00+00:00"),
        make_task(
            "m-render",
            TaskState.QUEUED,
            "2026-11-18T02:00:00+00:00",
            depends_on=("m-dep",),
        ),
        make_task("m-dep", TaskState.CLOSED, "2026-11-18T00:30:00+00:00"),
        make_task("m-done", TaskState.CLOSED, "2026-11-18T03:00:00+00:00"),
        make_task(
            "m-else", TaskState.RUNNING, "2026-11-18T00:00:00+00:00", target=OTHER
        ),
    )
    spec(orch_home, "m-render", "# Planning\n\n- 작업 우선도: P1\n")
    spec(orch_home, "m-render-2", "- 작업 우선도: P0\n")

    written, removed = plans.refresh_plans(conn, orch_home, TARGET, NOW)

    assert (written, removed) == (("G01", "G05"), ())
    assert sorted(p.name for p in plan_dir(orch_home).iterdir()) == [
        "G01.md",
        "G05.md",
    ]
    records = {r.record_id: r for r in tracking.list_records(conn, TARGET)}
    own = {k: v for k, v in tasks.items() if v.target == TARGET}
    g05 = [
        PlanEntry(
            record=records["h-docs"], tasks=(), priority=None, issue_closed=False
        ),
        PlanEntry(
            record=records["m-empty"], tasks=(), priority=None, issue_closed=False
        ),
        PlanEntry(
            record=records["m-render"],
            tasks=(tasks["m-render-2"], tasks["m-render"]),
            priority="P1",
            issue_closed=False,
        ),
    ]
    expected = render.plan_text(project(conn, "G05"), g05, own, NOW)
    assert read_plan(orch_home, "G05") == expected
    assert "- m-render\n" in expected
    g01 = [
        PlanEntry(record=records["B03"], tasks=(), priority=None, issue_closed=False)
    ]
    assert read_plan(orch_home, "G01") == render.plan_text(
        project(conn, "G01"), g01, own, NOW
    )


def test_ties_on_the_update_time_go_by_task_id(
    conn: sqlite3.Connection, orch_home: Path
) -> None:
    same = "2026-11-18T01:00:00+00:00"
    add(
        conn,
        make_task("m-render-2", TaskState.QUEUED, same),
        make_task("m-render", TaskState.CLOSED, same),
    )
    spec(orch_home, "m-render-2", "- 작업 우선도: P3\n")
    spec(orch_home, "m-render", "- 작업 우선도: P0\n")
    plans.refresh_plans(conn, orch_home, TARGET, NOW)
    text = read_plan(orch_home, "G05")
    assert "| P3 | m-render |" in text
    assert "m-render closed L2/B, m-render-2 queued L2/B" in text


@pytest.mark.parametrize("case", ["missing", "not_utf8", "directory", "no_line"])
def test_an_unreadable_spec_gives_no_priority(
    conn: sqlite3.Connection, orch_home: Path, case: str
) -> None:
    add(
        conn,
        make_task("m-render", TaskState.QUEUED, "2026-11-18T01:00:00+00:00"),
        make_task("m-done", TaskState.CLOSED, "2026-11-18T03:00:00+00:00"),
    )
    path = config.target_paths(orch_home, TARGET).tasks / "m-render.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if case == "not_utf8":
        path.write_bytes(b"- \xff\xfe P1\n")
    elif case == "directory":
        path.mkdir()
    elif case == "no_line":
        path.write_text("# Planning\n\n- 작업 우선도: 높음\n", encoding="utf-8")
    assert plans.refresh_plans(conn, orch_home, TARGET, NOW)[0] == ("G01", "G05")
    assert "| - | m-render |" in read_plan(orch_home, "G05")


def test_a_closed_group_loses_its_file_and_others_stay(
    conn: sqlite3.Connection, orch_home: Path, tmp_path: Path
) -> None:
    folder = plan_dir(orch_home)
    folder.mkdir(parents=True)
    for name in ("G07.md", "notes.md", "G99.md", "G01.txt"):
        (folder / name).write_text("keep", encoding="utf-8")
    add(conn, make_task("m-done", TaskState.CLOSED, "2026-11-18T03:00:00+00:00"))

    assert plans.refresh_plans(conn, orch_home, TARGET, NOW) == (
        ("G01", "G05"),
        ("G07",),
    )
    assert sorted(p.name for p in folder.iterdir()) == [
        "G01.md",
        "G01.txt",
        "G05.md",
        "G99.md",
        "notes.md",
    ]
    assert (folder / "notes.md").read_text(encoding="utf-8") == "keep"


def test_a_missing_file_of_a_closed_group_is_not_reported(
    conn: sqlite3.Connection, orch_home: Path
) -> None:
    add(conn, make_task("m-done", TaskState.CLOSED, "2026-11-18T03:00:00+00:00"))
    assert plans.refresh_plans(conn, orch_home, TARGET, NOW)[1] == ()


def test_a_task_record_whose_tasks_are_not_in_the_db_stays_open(
    conn: sqlite3.Connection, orch_home: Path
) -> None:
    # done-task names m-done, which has no row: the record counts as having no task.
    assert plans.refresh_plans(conn, orch_home, TARGET, NOW) == (
        ("G01", "G05", "G07"),
        (),
    )
    assert "- 작업 미등록: done-task\n" in read_plan(orch_home, "G07")


def test_harness_and_manual_records_close_by_local_evidence(
    conn: sqlite3.Connection, orch_home: Path
) -> None:
    tracking.save_verified(
        conn,
        target=TARGET,
        record_id="h-docs",
        verified={"issue_state": "closed"},
        at="t1",
    )
    tracking.accept_record(conn, target=TARGET, record_id="B03", at="t1")
    add(
        conn,
        make_task("m-render", TaskState.CLOSED, "2026-11-18T01:00:00+00:00"),
        make_task("m-done", TaskState.CLOSED, "2026-11-18T03:00:00+00:00"),
    )
    (plan_dir(orch_home)).mkdir(parents=True)
    (plan_dir(orch_home) / "G01.md").write_text("old", encoding="utf-8")

    assert plans.refresh_plans(conn, orch_home, TARGET, NOW) == (
        ("G05",),
        ("G01",),
    )
    text = read_plan(orch_home, "G05")
    assert "| h-docs |" not in text and "| m-render |" not in text
    assert "- 작업 미등록: m-empty\n" in text


def test_an_open_harness_issue_keeps_the_record(
    conn: sqlite3.Connection, orch_home: Path
) -> None:
    tracking.save_verified(
        conn,
        target=TARGET,
        record_id="h-docs",
        verified={"issue_state": "open"},
        at="t1",
    )
    plans.refresh_plans(conn, orch_home, TARGET, NOW)
    assert "| - | h-docs |" in read_plan(orch_home, "G05")


def test_targets_are_kept_apart(
    conn: sqlite3.Connection, orch_home: Path, tmp_path: Path
) -> None:
    other = tracking_data()
    other.update(target=OTHER, repository="2muni-labs/alpha", tasks={}, aliases={})
    other["records"] = {"a-1": record("task", project="G01")}
    other["records"]["a-1"].pop("issue_url", None)
    other["projects"] = {"G01": {"title": "alpha 01", "url": G01_URL}}
    import_data(conn, tmp_path, other)
    add(conn, make_task("m-render", TaskState.QUEUED, "2026-11-18T01:00:00+00:00"))

    assert plans.refresh_plans(conn, orch_home, OTHER, NOW) == (("G01",), ())
    text = (plan_dir(orch_home, OTHER) / "G01.md").read_text(encoding="utf-8")
    assert "| a-1 |" in text and "m-render" not in text
    assert not plan_dir(orch_home).exists()


def test_refresh_changes_no_state(conn: sqlite3.Connection, orch_home: Path) -> None:
    add(conn, make_task("m-render", TaskState.QUEUED, "2026-11-18T01:00:00+00:00"))
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
    ]

    def dump() -> dict[str, list[tuple]]:
        return {t: conn.execute(f"SELECT * FROM {t}").fetchall() for t in tables}

    before = dump()
    plans.refresh_plans(conn, orch_home, TARGET, NOW)
    assert dump() == before


def test_the_file_is_written_through_render(
    conn: sqlite3.Connection, orch_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path] = []
    real = render.write_todo

    def spy(path: Path, text: str) -> None:
        calls.append(path)
        real(path, text)

    add(conn, make_task("m-done", TaskState.CLOSED, "2026-11-18T03:00:00+00:00"))
    monkeypatch.setattr(render, "write_todo", spy)
    plans.refresh_plans(conn, orch_home, TARGET, NOW)
    assert calls == [plan_dir(orch_home) / "G01.md", plan_dir(orch_home) / "G05.md"]


def test_plans_sends_nothing() -> None:
    tree = ast.parse(Path(plans.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    assert not names & {
        "subprocess",
        "socket",
        "urllib",
        "http",
        "orch.github",
        "orch.credentials",
        "orch.tracksync",
    }
