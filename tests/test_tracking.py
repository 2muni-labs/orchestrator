"""Tests for tracking.py: the tracking file, its import and the tracking tables."""

import copy
import json
import sqlite3
from pathlib import Path

import pytest

from orch import store, tracking
from orch.tracking import (
    ImportReport,
    Project,
    Record,
    SyncRun,
    SyncState,
    TrackingConflictError,
    TrackingError,
    TrackingFormatError,
)

TARGET = "orchestrator"
REPO = "2muni-labs/orchestrator"
ISSUE = f"https://github.com/{REPO}/issues/"
PULL = f"https://github.com/{REPO}/pull/"
PROJECT_URL = "https://github.com/orgs/2muni-labs/projects/7"


def sample() -> dict:
    """A valid tracking file shaped like the runtime file."""
    return {
        "schema_version": 1,
        "target": TARGET,
        "repository": REPO,
        "seeded_at": "2026-11-17",
        "projects": {
            "G01": {
                "title": "orch 01",
                "url": "https://github.com/orgs/2muni-labs/projects/3",
            },
            "G05": {"title": "orch 05 — 파이프라인", "url": PROJECT_URL},
        },
        "records": {
            "m-track-store": {
                "title": "[track] state.db 추적 테이블",
                "kind": "task",
                "issue_url": ISSUE + "492",
                "project_key": "G05",
                "completion_owner": "task",
            },
            "h-track-switch": {
                "title": "[하네스] 전환",
                "kind": "harness",
                "issue_url": ISSUE + "498",
                "project_key": "G05",
                "completion_owner": "harness",
                "branch": "task/h-track-switch",
            },
            "B03": {
                "title": "[Backlog B03] item",
                "kind": "backlog",
                "issue_url": ISSUE + "300",
                "project_key": "G01",
                "completion_owner": "manual",
                "pr_url": PULL + "41",
            },
            "pending": {
                "title": "Not yet an issue",
                "kind": "measurement",
                "project_key": "G05",
                "completion_owner": "aggregate",
            },
        },
        "tasks": {
            "m-track-store": {"record_id": "m-track-store"},
            "m-old": {"record_id": "B03"},
        },
        "aliases": {"B03": "B03", "pr-41": "B03"},
    }


def text(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False)


def write(path: Path, data: dict) -> Path:
    path.write_text(text(data), encoding="utf-8")
    return path


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return store.connect(tmp_path / "state.db")


@pytest.fixture
def imported(conn: sqlite3.Connection, tmp_path: Path) -> sqlite3.Connection:
    tracking.import_file(
        conn, write(tmp_path / "t.json", sample()), target=TARGET, at="t0"
    )
    return conn


def rows(conn: sqlite3.Connection, table: str) -> list[tuple]:
    return conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()


ALL_TABLES = (
    "tracking_targets",
    "tracking_projects",
    "tracking_records",
    "tracking_tasks",
    "tracking_aliases",
    "tracking_state",
    "tracking_runs",
)


def snapshot(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    return {table: rows(conn, table) for table in ALL_TABLES}


# parse_file: a valid file


def test_parse_returns_the_contents_in_key_order() -> None:
    parsed = tracking.parse_file(text(sample()), target=TARGET)
    assert parsed.target == TARGET
    assert parsed.repository == REPO
    assert parsed.projects == (
        Project(
            key="G01",
            title="orch 01",
            url="https://github.com/orgs/2muni-labs/projects/3",
        ),
        Project(key="G05", title="orch 05 — 파이프라인", url=PROJECT_URL),
    )
    assert [r.record_id for r in parsed.records] == [
        "B03",
        "h-track-switch",
        "m-track-store",
        "pending",
    ]
    assert parsed.records[1] == Record(
        record_id="h-track-switch",
        title="[하네스] 전환",
        kind="harness",
        completion_owner="harness",
        project_key="G05",
        issue_url=ISSUE + "498",
        branch="task/h-track-switch",
    )
    assert parsed.records[0].pr_url == PULL + "41"
    assert parsed.records[3].issue_url is None
    assert all(r.accepted_at is None for r in parsed.records)
    assert dict(parsed.tasks) == {"m-track-store": "m-track-store", "m-old": "B03"}
    assert dict(parsed.aliases) == {"B03": "B03", "pr-41": "B03"}
    assert parsed.dropped_keys == ()


def test_parse_drops_the_legacy_sync_key() -> None:
    data = sample()
    data["sync"] = {"m-track-store": {"revision": 3, "pending": False}}
    assert tracking.parse_file(text(data), target=TARGET).dropped_keys == ("sync",)


def test_parse_accepts_a_project_owner_in_another_case() -> None:
    data = sample()
    data["projects"]["G05"]["url"] = "https://github.com/orgs/2Muni-Labs/projects/7"
    tracking.parse_file(text(data), target=TARGET)


def test_parse_accepts_a_file_without_seeded_at() -> None:
    data = sample()
    del data["seeded_at"]
    tracking.parse_file(text(data), target=TARGET)


def test_load_file_reads_utf8(tmp_path: Path) -> None:
    parsed = tracking.load_file(write(tmp_path / "t.json", sample()), target=TARGET)
    assert parsed.projects[1].title == "orch 05 — 파이프라인"


def test_load_file_passes_a_read_error_through(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        tracking.load_file(tmp_path / "missing.json", target=TARGET)


# parse_file: refusals


@pytest.mark.parametrize("version", [2, 0, "1", 1.0, True, None])
def test_parse_refuses_an_unknown_schema_version(version: object) -> None:
    data = sample()
    data["schema_version"] = version
    with pytest.raises(TrackingFormatError, match="schema_version"):
        tracking.parse_file(text(data), target=TARGET)


def test_parse_refuses_a_missing_schema_version() -> None:
    data = sample()
    del data["schema_version"]
    with pytest.raises(TrackingFormatError):
        tracking.parse_file(text(data), target=TARGET)


def test_parse_reports_the_version_before_other_errors() -> None:
    data = {"schema_version": 2, "records": "not an object"}
    with pytest.raises(TrackingFormatError, match="2"):
        tracking.parse_file(text(data), target=TARGET)


def test_parse_refuses_text_that_is_not_json() -> None:
    with pytest.raises(TrackingFormatError):
        tracking.parse_file("{not json", target=TARGET)


def test_parse_refuses_a_top_level_list() -> None:
    with pytest.raises(TrackingFormatError):
        tracking.parse_file("[]", target=TARGET)


def mutated(change) -> str:
    data = copy.deepcopy(sample())
    change(data)
    return text(data)


def set_key(path: tuple, value: object):
    def change(data: dict) -> None:
        node = data
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value

    return change


def del_key(path: tuple):
    def change(data: dict) -> None:
        node = data
        for key in path[:-1]:
            node = node[key]
        del node[path[-1]]

    return change


REFUSED = {
    "unknown top-level key": set_key(("extra",), 1),
    "other target": set_key(("target",), "orch-sample"),
    "bad repository": set_key(("repository",), "no-slash"),
    "missing records": del_key(("records",)),
    "missing aliases": del_key(("aliases",)),
    "projects not an object": set_key(("projects",), []),
    "bad project key": set_key(("projects", "-G"), {"title": "x", "url": PROJECT_URL}),
    "project extra key": set_key(("projects", "G05", "id"), "PVT"),
    "project missing url": del_key(("projects", "G05", "url")),
    "project empty title": set_key(("projects", "G05", "title"), ""),
    "project title with newline": set_key(("projects", "G05", "title"), "a\nb"),
    "project of another owner": set_key(
        ("projects", "G05", "url"), "https://github.com/orgs/someone/projects/7"
    ),
    "user project url": set_key(
        ("projects", "G05", "url"), "https://github.com/users/2muni-labs/projects/7"
    ),
    "project url with suffix": set_key(
        ("projects", "G05", "url"), PROJECT_URL + "/views/1"
    ),
    "bad record id": set_key(
        ("records", "a b"),
        {
            "title": "t",
            "kind": "task",
            "project_key": "G05",
            "completion_owner": "task",
        },
    ),
    "record id too long": set_key(
        ("records", "a" * 101),
        {
            "title": "t",
            "kind": "task",
            "project_key": "G05",
            "completion_owner": "task",
        },
    ),
    "record extra key": set_key(("records", "B03", "state"), "open"),
    "record missing title": del_key(("records", "B03", "title")),
    "record missing kind": del_key(("records", "B03", "kind")),
    "record missing owner": del_key(("records", "B03", "completion_owner")),
    "record missing project": del_key(("records", "B03", "project_key")),
    "record empty title": set_key(("records", "B03", "title"), ""),
    "record title with newline": set_key(("records", "B03", "title"), "a\nb"),
    "record title with return": set_key(("records", "B03", "title"), "a\rb"),
    "record title not a string": set_key(("records", "B03", "title"), 5),
    "bad kind": set_key(("records", "B03", "kind"), "Backlog"),
    "unknown owner": set_key(("records", "B03", "completion_owner"), "user"),
    "unknown project key": set_key(("records", "B03", "project_key"), "G99"),
    "issue in another repository": set_key(
        ("records", "B03", "issue_url"),
        "https://github.com/2muni-labs/orch-sample/issues/1",
    ),
    "issue url is a pull": set_key(("records", "B03", "issue_url"), PULL + "300"),
    "issue number zero": set_key(("records", "B03", "issue_url"), ISSUE + "0"),
    "issue url not a string": set_key(("records", "B03", "issue_url"), 300),
    "issue url null": set_key(("records", "B03", "issue_url"), None),
    "pr url is an issue": set_key(("records", "B03", "pr_url"), ISSUE + "41"),
    "pr in another repository": set_key(
        ("records", "B03", "pr_url"),
        "https://github.com/2muni-labs/orchXorchestrator/pull/1",
    ),
    "branch without task prefix": set_key(
        ("records", "h-track-switch", "branch"), "main"
    ),
    "branch with a bad id": set_key(
        ("records", "h-track-switch", "branch"), "task/a b"
    ),
    "duplicate issue url": set_key(
        ("records", "h-track-switch", "issue_url"), ISSUE + "300"
    ),
    "task value not an object": set_key(("tasks", "m-old"), "B03"),
    "task extra key": set_key(("tasks", "m-old", "note"), "x"),
    "task of an unknown record": set_key(("tasks", "m-old"), {"record_id": "nope"}),
    "bad task id": set_key(("tasks", "m old"), {"record_id": "B03"}),
    "alias of an unknown record": set_key(("aliases", "B99"), "nope"),
    "alias value not a string": set_key(("aliases", "B99"), {"record_id": "B03"}),
    "bad alias": set_key(("aliases", "B 99"), "B03"),
}


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_parse_refuses_invalid_values(case: str) -> None:
    with pytest.raises(TrackingFormatError):
        tracking.parse_file(mutated(REFUSED[case]), target=TARGET)


def test_the_repository_in_urls_is_matched_literally() -> None:
    data = sample()
    data["repository"] = "2muni-labs/orch.sample"
    data["records"] = {
        "r": {
            "title": "t",
            "kind": "task",
            "project_key": "G05",
            "completion_owner": "task",
            "issue_url": "https://github.com/2muni-labs/orchXsample/issues/1",
        }
    }
    data["tasks"] = {}
    data["aliases"] = {}
    with pytest.raises(TrackingFormatError):
        tracking.parse_file(text(data), target=TARGET)


# import_file


def test_import_writes_every_table(conn: sqlite3.Connection, tmp_path: Path) -> None:
    report = tracking.import_file(
        conn, write(tmp_path / "t.json", sample()), target=TARGET, at="t0"
    )
    assert report == ImportReport(
        records_added=("B03", "h-track-switch", "m-track-store", "pending"),
        records_updated=(),
        records_unchanged=0,
        projects=2,
        tasks=2,
        aliases=2,
        dropped_keys=(),
    )
    assert tracking.repository(conn, TARGET) == REPO
    assert rows(conn, "tracking_targets") == [(TARGET, REPO, "t0")]
    assert (
        tracking.list_projects(conn, TARGET)
        == tracking.parse_file(text(sample()), target=TARGET).projects
    )
    assert (
        tracking.list_records(conn, TARGET)
        == tracking.parse_file(text(sample()), target=TARGET).records
    )
    assert tracking.record_for_task(conn, TARGET, "m-old").record_id == "B03"
    assert rows(conn, "tracking_state") == []
    assert rows(conn, "tracking_runs") == []


def test_import_twice_changes_nothing_but_the_import_time(
    imported: sqlite3.Connection, tmp_path: Path
) -> None:
    before = snapshot(imported)
    report = tracking.import_file(
        imported, write(tmp_path / "t.json", sample()), target=TARGET, at="t1"
    )
    assert report.records_added == ()
    assert report.records_updated == ()
    assert report.records_unchanged == 4
    after = snapshot(imported)
    assert after["tracking_targets"] == [(TARGET, REPO, "t1")]
    del before["tracking_targets"], after["tracking_targets"]
    assert after == before


def test_import_reports_the_dropped_sync_key_and_leaves_the_file(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    data = sample()
    data["sync"] = {"x": {}}
    path = write(tmp_path / "t.json", data)
    original = path.read_bytes()
    report = tracking.import_file(conn, path, target=TARGET, at="t0")
    assert report.dropped_keys == ("sync",)
    assert path.read_bytes() == original


def test_import_updates_changed_records_and_keeps_acceptance(
    imported: sqlite3.Connection, tmp_path: Path
) -> None:
    assert tracking.accept_record(imported, target=TARGET, record_id="B03", at="ta")
    data = sample()
    data["records"]["B03"]["title"] = "[Backlog B03] renamed"
    data["records"]["h-track-switch"]["pr_url"] = PULL + "600"
    data["records"]["new"] = {
        "title": "n",
        "kind": "task",
        "project_key": "G01",
        "completion_owner": "task",
    }
    report = tracking.import_file(
        imported, write(tmp_path / "t.json", data), target=TARGET, at="t1"
    )
    assert report.records_added == ("new",)
    assert report.records_updated == ("B03", "h-track-switch")
    assert report.records_unchanged == 2
    b03 = tracking.get_record(imported, TARGET, "B03")
    assert b03.title == "[Backlog B03] renamed"
    assert b03.accepted_at == "ta"
    assert (
        tracking.get_record(imported, TARGET, "h-track-switch").pr_url == PULL + "600"
    )


def test_import_clears_an_optional_field_the_file_dropped(
    imported: sqlite3.Connection, tmp_path: Path
) -> None:
    data = sample()
    del data["records"]["B03"]["pr_url"]
    report = tracking.import_file(
        imported, write(tmp_path / "t.json", data), target=TARGET, at="t1"
    )
    assert report.records_updated == ("B03",)
    assert tracking.get_record(imported, TARGET, "B03").pr_url is None


def test_import_keeps_rows_the_file_does_not_name(
    imported: sqlite3.Connection, tmp_path: Path
) -> None:
    tracking.bind_task(imported, target=TARGET, task_id="m-extra", record_id="pending")
    tracking.save_verified(
        imported, target=TARGET, record_id="B03", verified={"issue": "OPEN"}, at="tv"
    )
    data = sample()
    del data["tasks"]["m-old"]
    del data["aliases"]["pr-41"]
    tracking.import_file(
        imported, write(tmp_path / "t.json", data), target=TARGET, at="t1"
    )
    assert tracking.record_for_task(imported, TARGET, "m-extra").record_id == "pending"
    assert tracking.record_for_task(imported, TARGET, "m-old").record_id == "B03"
    assert tracking.resolve(imported, TARGET, "pr-41").record_id == "B03"
    assert tracking.get_state(imported, TARGET, "B03").verified == {"issue": "OPEN"}


def test_import_moves_a_task_binding_to_the_file_value(
    imported: sqlite3.Connection, tmp_path: Path
) -> None:
    tracking.bind_task(imported, target=TARGET, task_id="m-old", record_id="pending")
    tracking.import_file(
        imported, write(tmp_path / "t.json", sample()), target=TARGET, at="t1"
    )
    assert tracking.record_for_task(imported, TARGET, "m-old").record_id == "B03"


def test_import_refuses_another_repository_for_the_target(
    imported: sqlite3.Connection, tmp_path: Path
) -> None:
    data = sample()
    data["repository"] = "2muni-labs/other"
    for record in data["records"].values():
        for key in ("issue_url", "pr_url"):
            if key in record:
                record[key] = record[key].replace(REPO, "2muni-labs/other")
    before = snapshot(imported)
    with pytest.raises(TrackingConflictError):
        tracking.import_file(
            imported, write(tmp_path / "t.json", data), target=TARGET, at="t1"
        )
    assert snapshot(imported) == before


def test_an_invalid_file_writes_nothing(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    with pytest.raises(TrackingFormatError):
        tracking.import_file(
            conn,
            write(tmp_path / "t.json", {**sample(), "schema_version": 2}),
            target=TARGET,
            at="t0",
        )
    assert all(not table for table in snapshot(conn).values())


def test_a_failure_while_writing_rolls_back_the_import(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    conn.execute(
        """CREATE TRIGGER fail_alias BEFORE INSERT ON tracking_aliases
        BEGIN SELECT RAISE(ABORT, 'boom'); END"""
    )
    with pytest.raises(sqlite3.DatabaseError):
        tracking.import_file(
            conn, write(tmp_path / "t.json", sample()), target=TARGET, at="t0"
        )
    assert all(not table for table in snapshot(conn).values())
    assert conn.in_transaction is False


def test_targets_are_kept_apart(imported: sqlite3.Connection, tmp_path: Path) -> None:
    data = sample()
    data["target"] = "orch-sample"
    data["repository"] = "2muni-labs/orch-sample"
    data["records"] = {
        "B03": {
            "title": "sample record",
            "kind": "task",
            "project_key": "G01",
            "completion_owner": "task",
            "issue_url": "https://github.com/2muni-labs/orch-sample/issues/1",
        }
    }
    data["tasks"] = {}
    data["aliases"] = {}
    tracking.import_file(
        imported, write(tmp_path / "s.json", data), target="orch-sample", at="t1"
    )
    assert tracking.get_record(imported, TARGET, "B03").title == "[Backlog B03] item"
    assert tracking.get_record(imported, "orch-sample", "B03").title == "sample record"
    assert [r.record_id for r in tracking.list_records(imported, "orch-sample")] == [
        "B03"
    ]
    assert tracking.repository(imported, "orch-sample") == "2muni-labs/orch-sample"


def test_the_real_runtime_shape_imports(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A file of the real size: 349 records, alias collisions and the sync key."""
    data = sample()
    for n in range(1, 346):
        data["records"][f"pr-{n}"] = {
            "title": f"[History PR #{n}] item",
            "kind": "history",
            "issue_url": ISSUE + str(1000 + n),
            "project_key": "G01",
            "completion_owner": "manual",
            "pr_url": PULL + str(n),
        }
        data["aliases"][f"B{n:02d}"] = f"pr-{n}"
    data["sync"] = {}
    report = tracking.import_file(
        conn, write(tmp_path / "t.json", data), target=TARGET, at="t0"
    )
    assert len(report.records_added) == 349
    assert report.dropped_keys == ("sync",)
    assert tracking.resolve(conn, TARGET, "B03").record_id == "B03"
    assert tracking.resolve(conn, TARGET, "B04").record_id == "pr-4"


# reads


def test_reads_of_unknown_rows_return_none(imported: sqlite3.Connection) -> None:
    assert tracking.get_record(imported, TARGET, "nope") is None
    assert tracking.resolve(imported, TARGET, "nope") is None
    assert tracking.record_for_task(imported, TARGET, "nope") is None
    assert tracking.task_ids(imported, TARGET, "h-track-switch") == ()
    assert tracking.repository(imported, "other") is None
    assert tracking.list_records(imported, "other") == ()
    assert tracking.list_projects(imported, "other") == ()


def test_resolve_prefers_a_record_id_over_an_alias(
    imported: sqlite3.Connection, tmp_path: Path
) -> None:
    data = sample()
    data["aliases"]["pending"] = "B03"
    tracking.import_file(
        imported, write(tmp_path / "t.json", data), target=TARGET, at="t1"
    )
    assert tracking.resolve(imported, TARGET, "pending").record_id == "pending"
    assert tracking.resolve(imported, TARGET, "pr-41").record_id == "B03"


def test_task_ids_are_sorted(imported: sqlite3.Connection) -> None:
    tracking.bind_task(imported, target=TARGET, task_id="m-a", record_id="B03")
    assert tracking.task_ids(imported, TARGET, "B03") == ("m-a", "m-old")


def test_list_targets_is_empty_before_an_import(conn: sqlite3.Connection) -> None:
    assert tracking.list_targets(conn) == ()


def test_list_targets_returns_imported_targets_sorted(
    imported: sqlite3.Connection, tmp_path: Path
) -> None:
    other = sample()
    other["target"] = "alpha"
    other["repository"] = "owner/alpha"
    other["records"] = {}
    other["tasks"] = {}
    other["aliases"] = {}
    other["projects"] = {
        "G01": {"title": "a", "url": "https://github.com/orgs/owner/projects/1"}
    }
    tracking.import_file(
        imported, write(tmp_path / "a.json", other), target="alpha", at="t1"
    )
    assert tracking.list_targets(imported) == ("alpha", TARGET)


# bind_task and accept_record


def test_bind_task_reports_whether_it_changed(imported: sqlite3.Connection) -> None:
    assert tracking.bind_task(
        imported, target=TARGET, task_id="m-new", record_id="pending"
    )
    assert not tracking.bind_task(
        imported, target=TARGET, task_id="m-new", record_id="pending"
    )
    assert tracking.bind_task(imported, target=TARGET, task_id="m-new", record_id="B03")
    assert tracking.record_for_task(imported, TARGET, "m-new").record_id == "B03"


def test_bind_task_needs_no_orch_task(imported: sqlite3.Connection) -> None:
    assert store.get_task(imported, "m-planned") is None
    tracking.bind_task(
        imported, target=TARGET, task_id="m-planned", record_id="pending"
    )


def test_bind_task_refuses_an_unknown_record_or_bad_id(
    imported: sqlite3.Connection,
) -> None:
    with pytest.raises(TrackingError):
        tracking.bind_task(imported, target=TARGET, task_id="m-x", record_id="nope")
    with pytest.raises(TrackingError):
        tracking.bind_task(imported, target=TARGET, task_id="m x", record_id="B03")
    assert tracking.record_for_task(imported, TARGET, "m-x") is None


def test_accept_record_once(imported: sqlite3.Connection) -> None:
    assert tracking.accept_record(imported, target=TARGET, record_id="pending", at="t1")
    assert not tracking.accept_record(
        imported, target=TARGET, record_id="pending", at="t2"
    )
    assert tracking.get_record(imported, TARGET, "pending").accepted_at == "t1"


@pytest.mark.parametrize("record_id", ["m-track-store", "h-track-switch", "nope"])
def test_accept_record_refuses_task_harness_and_unknown(
    imported: sqlite3.Connection, record_id: str
) -> None:
    with pytest.raises(TrackingError):
        tracking.accept_record(imported, target=TARGET, record_id=record_id, at="t1")


# sync state


def test_save_verified_round_trips_and_clears_failures(
    imported: sqlite3.Connection,
) -> None:
    assert tracking.get_state(imported, TARGET, "B03") is None
    assert (
        tracking.record_failure(
            imported, target=TARGET, record_id="B03", error="api_error", at="t1"
        )
        == 1
    )
    verified = {"issue_state": "CLOSED", "status": "Done", "n": 3, "note": "한국어"}
    tracking.save_verified(
        imported, target=TARGET, record_id="B03", verified=verified, at="t2"
    )
    assert tracking.get_state(imported, TARGET, "B03") == SyncState(
        record_id="B03",
        verified=verified,
        verified_at="t2",
        failures=0,
        last_error=None,
    )
    stored = imported.execute(
        "SELECT verified FROM tracking_state WHERE record_id = 'B03'"
    ).fetchone()[0]
    assert stored == json.dumps(verified, sort_keys=True, ensure_ascii=False)


def test_record_failure_counts_and_keeps_the_verified_values(
    imported: sqlite3.Connection,
) -> None:
    tracking.save_verified(
        imported, target=TARGET, record_id="B03", verified={"a": 1}, at="t1"
    )
    assert (
        tracking.record_failure(
            imported,
            target=TARGET,
            record_id="B03",
            error="read_back_mismatch",
            at="t2",
        )
        == 1
    )
    assert (
        tracking.record_failure(
            imported, target=TARGET, record_id="B03", error="api_error", at="t3"
        )
        == 2
    )
    assert tracking.get_state(imported, TARGET, "B03") == SyncState(
        record_id="B03",
        verified={"a": 1},
        verified_at="t1",
        failures=2,
        last_error="api_error",
    )


@pytest.mark.parametrize(
    "error", ["", "Api", "has space", "token ghs_abc", "a" * 65, "x\ny", "1abc"]
)
def test_errors_must_be_codes(imported: sqlite3.Connection, error: str) -> None:
    with pytest.raises(ValueError):
        tracking.record_failure(
            imported, target=TARGET, record_id="B03", error=error, at="t"
        )
    with pytest.raises(ValueError):
        tracking.record_run(imported, target=TARGET, at="t", error=error)
    assert tracking.get_state(imported, TARGET, "B03") is None
    assert tracking.get_run(imported, TARGET) is None


def test_state_writes_refuse_an_unknown_record(imported: sqlite3.Connection) -> None:
    with pytest.raises(TrackingError):
        tracking.save_verified(
            imported, target=TARGET, record_id="nope", verified={}, at="t"
        )
    with pytest.raises(TrackingError):
        tracking.record_failure(
            imported, target=TARGET, record_id="nope", error="e", at="t"
        )


def test_record_run_tracks_consecutive_failures(conn: sqlite3.Connection) -> None:
    assert tracking.get_run(conn, TARGET) is None
    assert tracking.record_run(
        conn, target=TARGET, at="t1", error="auth_failed"
    ) == SyncRun(
        target=TARGET,
        last_at="t1",
        last_success_at=None,
        consecutive_failures=1,
        last_error="auth_failed",
    )
    tracking.record_run(conn, target=TARGET, at="t2", error="api_error")
    assert tracking.get_run(conn, TARGET).consecutive_failures == 2
    assert tracking.record_run(conn, target=TARGET, at="t3", error=None) == SyncRun(
        target=TARGET,
        last_at="t3",
        last_success_at="t3",
        consecutive_failures=0,
        last_error=None,
    )
    assert tracking.record_run(
        conn, target=TARGET, at="t4", error="api_error"
    ) == SyncRun(
        target=TARGET,
        last_at="t4",
        last_success_at="t3",
        consecutive_failures=1,
        last_error="api_error",
    )
    tracking.record_run(conn, target="other", at="t5", error=None)
    assert tracking.get_run(conn, TARGET) == SyncRun(
        target=TARGET,
        last_at="t4",
        last_success_at="t3",
        consecutive_failures=1,
        last_error="api_error",
    )


def test_writes_leave_no_transaction_open(imported: sqlite3.Connection) -> None:
    tracking.bind_task(imported, target=TARGET, task_id="m-z", record_id="B03")
    tracking.accept_record(imported, target=TARGET, record_id="B03", at="t")
    tracking.save_verified(
        imported, target=TARGET, record_id="B03", verified={}, at="t"
    )
    tracking.record_failure(imported, target=TARGET, record_id="B03", error="e", at="t")
    tracking.record_run(imported, target=TARGET, at="t", error=None)
    assert imported.in_transaction is False


def test_writes_join_an_open_transaction(imported: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError):
        with store.transaction(imported):
            tracking.bind_task(imported, target=TARGET, task_id="m-z", record_id="B03")
            tracking.record_run(imported, target=TARGET, at="t", error=None)
            raise RuntimeError
    assert tracking.record_for_task(imported, TARGET, "m-z") is None
    assert tracking.get_run(imported, TARGET) is None


def test_tracking_uses_only_the_standard_library() -> None:
    import ast
    import sys

    source = Path(tracking.__file__).read_text()
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    assert names <= set(sys.stdlib_module_names) | {"orch"}
