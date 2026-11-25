"""docs/roadmap.toml validation and per-milestone progress (4.13, D4)."""

import sqlite3
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from orch import roadmap, store
from orch.models import Effort, Grade, Lane, Task, TaskState
from orch.roadmap import Milestone, Phase, Problem, Roadmap, RoadmapError

GOOD = """
schema_version = 1

[[phase]]
id = "P4"
title = "실환경"
status = "done"
exit = ["재부팅 뒤 복원"]

[[phase]]
id = "P5a"
title = "파이프라인 확장"
status = "current"
exit = ["1주 무인 운용", "장애 기록"]

  [[phase.milestone]]
  id = "M18"
  title = "테스트 묶음"

  [[phase.milestone]]
  id = "M19"
  title = "파이프라인 확장"

[[phase]]
id = "P5b"
title = "작업공간"
status = "planned"
exit = ["작업공간 v1"]
"""

PARSED = Roadmap(
    (
        Phase("P4", "실환경", "done", ("재부팅 뒤 복원",), ()),
        Phase(
            "P5a",
            "파이프라인 확장",
            "current",
            ("1주 무인 운용", "장애 기록"),
            (Milestone("M18", "테스트 묶음"), Milestone("M19", "파이프라인 확장")),
        ),
        Phase("P5b", "작업공간", "planned", ("작업공간 v1",), ()),
    )
)


def problems_of(text: str) -> list[tuple[str, str]]:
    with pytest.raises(RoadmapError) as caught:
        roadmap.parse_roadmap(text)
    assert caught.value.error == "invalid_roadmap"
    return [(p.where, p.reason) for p in caught.value.problems]


# Constants


def test_constants_follow_the_design() -> None:
    assert roadmap.ROADMAP_PATH == "docs/roadmap.toml"
    assert roadmap.ROADMAP_FORMAT == 1
    assert roadmap.ID_PATTERN == r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}"
    assert roadmap.STATUSES == ("done", "current", "planned")


def test_the_error_carries_its_code_message_and_problems() -> None:
    error = RoadmapError("no_ref", "msg")
    assert (error.error, error.message, error.problems) == ("no_ref", "msg", ())


# parse_roadmap


def test_a_good_roadmap_parses_in_file_order() -> None:
    assert roadmap.parse_roadmap(GOOD) == PARSED


def test_a_phase_without_milestones_is_fine() -> None:
    text = (
        'schema_version = 1\n[[phase]]\nid = "P0"\ntitle = "t"\n'
        'status = "done"\nexit = ["e"]\n'
    )
    assert roadmap.parse_roadmap(text) == Roadmap(
        (Phase("P0", "t", "done", ("e",), ()),)
    )


def test_a_toml_syntax_error_is_one_problem() -> None:
    assert problems_of("schema_version = 1\n[[phase]\n") == [("", "syntax")]


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        ("", [("schema_version", "schema_version")]),
        ("schema_version = 2\n", [("schema_version", "schema_version")]),
        ("schema_version = true\n", [("schema_version", "schema_version")]),
        ('schema_version = "1"\n', [("schema_version", "schema_version")]),
    ],
    ids=["missing", "two", "bool", "string"],
)
def test_schema_version_must_be_the_integer_one(
    head: str, expected: list[tuple[str, str]]
) -> None:
    body = '[[phase]]\nid = "P0"\ntitle = "t"\nstatus = "done"\nexit = ["e"]\n'
    assert problems_of(head + body) == expected


def test_a_missing_or_empty_phase_list() -> None:
    assert problems_of("schema_version = 1\n") == [("phase", "missing")]
    assert problems_of("schema_version = 1\nphase = []\n") == [("phase", "empty")]
    assert problems_of('schema_version = 1\nphase = "x"\n') == [("phase", "type")]


def test_unknown_keys_are_reported_at_every_level() -> None:
    text = GOOD.replace("schema_version = 1\n", "schema_version = 1\nowner = 'me'\n")
    text = text.replace('status = "done"\n', 'status = "done"\nnotes = "x"\n')
    text = text.replace(
        'title = "테스트 묶음"\n', 'title = "테스트 묶음"\n  due = "2026-12"\n'
    )
    assert problems_of(text) == [
        ("owner", "unknown_key"),
        ("phase[0].notes", "unknown_key"),
        ("phase[1].milestone[0].due", "unknown_key"),
    ]


def test_missing_keys_are_reported() -> None:
    text = 'schema_version = 1\n[[phase]]\n  [[phase.milestone]]\n  title = "t"\n'
    assert problems_of(text) == [
        ("phase[0].id", "missing"),
        ("phase[0].title", "missing"),
        ("phase[0].status", "missing"),
        ("phase[0].exit", "missing"),
        ("phase[0].milestone[0].id", "missing"),
    ]


def test_wrong_types_are_reported() -> None:
    text = (
        "schema_version = 1\n[[phase]]\nid = 5\ntitle = []\nstatus = 1\n"
        'exit = ["ok", 2]\nmilestone = "M1"\n'
    )
    assert problems_of(text) == [
        ("phase[0].id", "type"),
        ("phase[0].title", "type"),
        ("phase[0].status", "type"),
        ("phase[0].exit", "type"),
        ("phase[0].milestone", "type"),
    ]


def test_empty_titles_and_exit_conditions_are_reported() -> None:
    text = (
        'schema_version = 1\n[[phase]]\nid = "P0"\ntitle = "  "\nstatus = "done"\n'
        'exit = []\n[[phase]]\nid = "P1"\ntitle = "t"\nstatus = "planned"\n'
        'exit = ["a", ""]\n  [[phase.milestone]]\n  id = "M1"\n  title = ""\n'
    )
    assert problems_of(text) == [
        ("phase[0].title", "empty"),
        ("phase[0].exit", "empty"),
        ("phase[1].exit", "empty"),
        ("phase[1].milestone[0].title", "empty"),
    ]


@pytest.mark.parametrize("bad", ["-M1", "M 1", "M/1", "", "x" * 33, "ü"])
def test_ids_must_match_the_milestone_format(bad: str) -> None:
    text = GOOD.replace('id = "M19"', f'id = "{bad}"')
    assert problems_of(text) == [("phase[1].milestone[1].id", "id_format")]


def test_the_longest_id_is_accepted() -> None:
    longest = "M" + "x" * 31
    text = GOOD.replace('id = "M19"', f'id = "{longest}"')
    parsed = roadmap.parse_roadmap(text)
    assert parsed.phases[1].milestones[1].id == longest


def test_duplicate_ids_across_phases_and_milestones() -> None:
    text = GOOD.replace('id = "M19"', 'id = "P4"').replace('id = "P5b"', 'id = "M18"')
    assert problems_of(text) == [
        ("phase[1].milestone[1].id", "duplicate_id"),
        ("phase[2].id", "duplicate_id"),
    ]


def test_an_unknown_status_is_reported() -> None:
    text = GOOD.replace('status = "planned"', 'status = "later"')
    assert problems_of(text) == [("phase[2].status", "status")]


def test_more_than_one_current_phase_is_reported_from_the_second() -> None:
    text = GOOD.replace('status = "done"', 'status = "current"').replace(
        'status = "planned"', 'status = "current"'
    )
    assert problems_of(text) == [
        ("phase[1].status", "multiple_current"),
        ("phase[2].status", "multiple_current"),
    ]


def test_every_problem_is_reported_at_once() -> None:
    text = (
        "schema_version = 3\nextra = 1\n[[phase]]\n"
        'id = "bad id"\ntitle = "t"\nstatus = "soon"\nexit = ["e"]\n'
    )
    assert problems_of(text) == [
        ("schema_version", "schema_version"),
        ("extra", "unknown_key"),
        ("phase[0].id", "id_format"),
        ("phase[0].status", "status"),
    ]


def test_the_hub_example_in_the_design_parses() -> None:
    text = (
        "schema_version = 1\n\n[[phase]]\n"
        'id = "P5a"\ntitle = "파이프라인 확장과 무인 운용"\nstatus = "current"\n'
        'exit = ["M19 설치 뒤 1주 무인 운용, 처리하지 못한 장애 없음"]\n\n'
        '  [[phase.milestone]]\n  id = "M19"\n  title = "파이프라인 확장"\n'
    )
    parsed = roadmap.parse_roadmap(text)
    assert [m.id for m in parsed.phases[0].milestones] == ["M19"]


# read_roadmap (a real git repository; no fetch)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "t")
    (path / "docs").mkdir()
    (path / "docs" / "roadmap.toml").write_text(GOOD, encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-qm", "roadmap")
    git(path, "remote", "add", "origin", str(remote))
    git(path, "push", "-q", "origin", "main")
    return path


def test_the_roadmap_is_read_from_the_remote_tracking_ref(repo: Path) -> None:
    pushed = git(repo, "rev-parse", "origin/main")
    (repo / "docs" / "roadmap.toml").write_text("local edit\n", encoding="utf-8")
    git(repo, "commit", "-qam", "local only")
    ref, commit, text = roadmap.read_roadmap(repo, "main")
    assert (ref, commit, text) == ("origin/main", pushed, GOOD)


def test_a_missing_remote_ref_is_no_ref(repo: Path) -> None:
    with pytest.raises(RoadmapError) as caught:
        roadmap.read_roadmap(repo, "trunk")
    assert caught.value.error == "no_ref"


def test_a_missing_file_is_no_roadmap(repo: Path) -> None:
    git(repo, "rm", "-q", "docs/roadmap.toml")
    git(repo, "commit", "-qm", "drop")
    git(repo, "push", "-q", "origin", "main")
    with pytest.raises(RoadmapError) as caught:
        roadmap.read_roadmap(repo, "main")
    assert caught.value.error == "no_roadmap"


def test_bytes_that_are_not_utf8_are_an_invalid_roadmap(repo: Path) -> None:
    (repo / "docs" / "roadmap.toml").write_bytes(b"\xff\xfe")
    git(repo, "commit", "-qam", "binary")
    git(repo, "push", "-q", "origin", "main")
    with pytest.raises(RoadmapError) as caught:
        roadmap.read_roadmap(repo, "main")
    assert caught.value.error == "invalid_roadmap"
    assert caught.value.problems == (Problem("", "syntax"),)


def test_read_roadmap_runs_git_by_absolute_path_without_fetch(repo: Path) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        calls.append(argv)
        return subprocess.run(argv, **kwargs)  # type: ignore[call-overload]

    roadmap.read_roadmap(repo, "main", run=run)
    assert [argv[0] for argv in calls] == [roadmap.GIT, roadmap.GIT]
    assert [argv[3] for argv in calls] == ["rev-parse", "show"]
    assert all("fetch" not in argv for argv in calls)


# chains


def task(
    task_id: str,
    state: TaskState = TaskState.CLOSED,
    *,
    milestone: str | None = "M19",
    replaces: str | None = None,
    created: int = 0,
    target: str = "sample",
) -> Task:
    return Task(
        id=task_id,
        target=target,
        title="T",
        grade=Grade.L2,
        lane=Lane.B,
        model="gpt-6.1-sol",
        effort=Effort.MEDIUM,
        state=state,
        timeout_sec=300,
        milestone=milestone,
        replaces=replaces,
        created_at=f"2026-11-{created + 1:02d}T00:00:00+00:00",
        updated_at=f"2026-11-{created + 1:02d}T00:00:00+00:00",
    )


def ids(found: Sequence[tuple[Task, ...]]) -> list[list[str]]:
    return [[t.id for t in chain] for chain in found]


def test_single_tasks_are_chains_of_one_in_creation_order() -> None:
    tasks = [task("b", created=2), task("a", created=1), task("c", created=1)]
    assert ids(roadmap.chains(tasks)) == [["a"], ["c"], ["b"]]


def test_replacements_join_one_chain_oldest_first() -> None:
    tasks = [
        task("old", TaskState.DISCARDED, created=1),
        task("old-2", TaskState.DISCARDED, replaces="old", created=2),
        task("other", created=3),
        task("old-3", replaces="old-2", created=4),
    ]
    assert ids(roadmap.chains(tasks)) == [["old", "old-2", "old-3"], ["other"]]


def test_a_replaced_task_outside_the_list_starts_the_chain() -> None:
    tasks = [task("new", replaces="gone", created=1)]
    assert ids(roadmap.chains(tasks)) == [["new"]]


def test_a_cycle_does_not_loop() -> None:
    tasks = [
        task("x", replaces="y", created=1),
        task("y", replaces="x", created=2),
        task("z", created=3),
    ]
    assert ids(roadmap.chains(tasks)) == [["x"], ["y"], ["z"]]


# progress_report


def test_progress_counts_chains_by_their_last_task() -> None:
    tasks = [
        task("done-1", created=1),
        task("open-1", TaskState.PR_OPEN, created=2),
        task("old", TaskState.DISCARDED, created=3),
        task("old-2", replaces="old", created=4),
        task("dropped", TaskState.DISCARDED, created=5),
        task("m18", milestone="M18", created=6),
    ]
    report = roadmap.progress_report(PARSED, tasks)
    p5a = report["phases"][1]  # type: ignore[index]
    m18, m19 = p5a["milestones"]
    assert m18 == {
        "id": "M18",
        "title": "테스트 묶음",
        "total": 1,
        "done": 1,
        "progress": 1.0,
        "chains": [{"tasks": ["m18"], "state": "closed"}],
    }
    assert m19 == {
        "id": "M19",
        "title": "파이프라인 확장",
        "total": 3,
        "done": 2,
        "progress": 0.6667,
        "chains": [
            {"tasks": ["done-1"], "state": "closed"},
            {"tasks": ["open-1"], "state": "pr_open"},
            {"tasks": ["old", "old-2"], "state": "closed"},
            {"tasks": ["dropped"], "state": "discarded"},
        ],
    }
    assert report["unknown_milestones"] == []
    assert report["no_milestone"] == 0


def test_phases_keep_their_fields_and_order() -> None:
    report = roadmap.progress_report(PARSED, [])
    assert [list(p) for p in report["phases"]] == [  # type: ignore[union-attr]
        ["id", "title", "status", "exit", "milestones"]
    ] * 3
    assert report["phases"][1]["exit"] == ["1주 무인 운용", "장애 기록"]  # type: ignore[index]
    assert list(report) == ["phases", "unknown_milestones", "no_milestone"]


def test_a_milestone_without_counted_chains_has_null_progress() -> None:
    tasks = [task("dropped", TaskState.DISCARDED)]
    report = roadmap.progress_report(PARSED, tasks)
    m19 = report["phases"][1]["milestones"][1]  # type: ignore[index]
    assert (m19["total"], m19["done"], m19["progress"]) == (0, 0, None)
    assert m19["chains"] == [{"tasks": ["dropped"], "state": "discarded"}]
    m18 = report["phases"][1]["milestones"][0]  # type: ignore[index]
    assert (m18["total"], m18["progress"], m18["chains"]) == (0, None, [])


def test_the_chain_milestone_is_the_last_tasks() -> None:
    tasks = [
        task("old", TaskState.DISCARDED, milestone="M18", created=1),
        task("old-2", TaskState.RUNNING, replaces="old", milestone="M19", created=2),
    ]
    report = roadmap.progress_report(PARSED, tasks)
    m18, m19 = report["phases"][1]["milestones"]  # type: ignore[index]
    assert m18["chains"] == []
    assert m19["chains"] == [{"tasks": ["old", "old-2"], "state": "running"}]


def test_unknown_and_missing_milestones_are_reported_apart() -> None:
    tasks = [
        task("z1", milestone="M99", created=1),
        task("a1", milestone="M20", created=2),
        task("z2", TaskState.QUEUED, milestone="M99", created=3),
        task("none-1", milestone=None, created=4),
        task("none-2", TaskState.DISCARDED, milestone=None, created=5),
    ]
    report = roadmap.progress_report(PARSED, tasks)
    assert report["unknown_milestones"] == [
        {"milestone": "M20", "chains": [{"tasks": ["a1"], "state": "closed"}]},
        {
            "milestone": "M99",
            "chains": [
                {"tasks": ["z1"], "state": "closed"},
                {"tasks": ["z2"], "state": "queued"},
            ],
        },
    ]
    assert report["no_milestone"] == 2


# build_report


def insert(conn: sqlite3.Connection, value: Task) -> None:
    store.insert_task(conn, value)


def test_build_report_reads_git_and_only_the_targets_tasks(
    repo: Path, tmp_path: Path
) -> None:
    conn = store.connect(tmp_path / "state.db")
    try:
        insert(conn, task("mine", created=1))
        insert(conn, task("theirs", created=2, target="other"))
        before = conn.total_changes
        report = roadmap.build_report(
            conn, target="sample", repo=repo, default_branch="main"
        )
        assert conn.total_changes == before
    finally:
        conn.close()
    assert list(report)[:3] == ["target", "ref", "commit"]
    assert report["target"] == "sample"
    assert report["ref"] == "origin/main"
    assert report["commit"] == git(repo, "rev-parse", "origin/main")
    m19 = report["phases"][1]["milestones"][1]  # type: ignore[index]
    assert m19["chains"] == [{"tasks": ["mine"], "state": "closed"}]


def test_build_report_raises_for_an_invalid_roadmap(repo: Path, tmp_path: Path) -> None:
    (repo / "docs" / "roadmap.toml").write_text(
        "schema_version = 2\n", encoding="utf-8"
    )
    git(repo, "commit", "-qam", "bad")
    git(repo, "push", "-q", "origin", "main")
    conn = store.connect(tmp_path / "state.db")
    try:
        with pytest.raises(RoadmapError) as caught:
            roadmap.build_report(
                conn, target="sample", repo=repo, default_branch="main"
            )
    finally:
        conn.close()
    assert caught.value.error == "invalid_roadmap"
    assert Problem("schema_version", "schema_version") in caught.value.problems
