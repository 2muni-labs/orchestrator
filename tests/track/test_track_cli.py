"""orch track: sync, import, bind, accept and status on the command line."""

import io
import json
import os
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import pytest

from orch import config, credentials, notify, plans, store, tracking, tracksync
from orch.cli import main
from orch.models import Grade, Lane, Task, TaskState
from orch.tracksync import Change, SyncReport, Wanted
from transitions import check_call

ALPHA = "alpha"
BETA = "beta"
REPO = "Owner/Alpha"
TOKEN = "gh" + "s_" + "t0ken" * 8
TOPIC = "topic-" + "q" * 20
USAGE = 64
REFUSED = 64

Call = Callable[..., tuple[int, dict[str, object]]]


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def targets_toml(home: Path) -> str:
    lines = []
    for name, github in ((BETA, "owner/beta"), (ALPHA, REPO)):
        (home / name).mkdir(exist_ok=True)
        lines += [
            f"[targets.{name}]",
            f'path = "{home / name}"',
            f'github = "{github}"',
            'default_branch = "main"',
            'setup_command = "true"',
            'test_command = "true"',
            'lint_command = "true"',
            "",
        ]
    return "\n".join(lines)


@pytest.fixture(autouse=True)
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    hub = tmp_path / "hub"
    write(hub / "targets.toml", targets_toml(home))
    assert sorted(config.load_targets(hub / "targets.toml")) == [ALPHA, BETA]
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ORCH_HOME", str(home / "runtime"))
    monkeypatch.setenv("ORCH_HUB", str(hub))
    for name in ("ORCH_TARGET", "GH_TOKEN", "GITHUB_TOKEN", "ORCH_FAKE_AGENTS"):
        monkeypatch.delenv(name, raising=False)
    return home / "runtime"


@pytest.fixture
def orch(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> Call:
    def call(*args: str) -> tuple[int, dict[str, object]]:
        check_call(config.state_db_path(Path(os.environ["ORCH_HOME"])), args)
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        code = main(list(args))
        captured = capsys.readouterr()
        sys.stderr.write(captured.err)
        out = captured.out
        assert out.endswith("\n") and out.count("\n") == 1, out
        return code, json.loads(out)

    return call


def db(orch_home: Path) -> sqlite3.Connection:
    return store.connect(config.state_db_path(orch_home))


def tracking_text(repository: str = REPO) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "target": ALPHA,
            "repository": repository,
            "projects": {
                "G01": {
                    "title": "one",
                    "url": "https://github.com/orgs/owner/projects/1",
                }
            },
            "records": {
                "task-a": {
                    "title": "작업 A",
                    "kind": "task",
                    "issue_url": f"https://github.com/{repository}/issues/1",
                    "project_key": "G01",
                    "completion_owner": "task",
                },
                "meas-b": {
                    "title": "측정 B",
                    "kind": "measurement",
                    "issue_url": f"https://github.com/{repository}/issues/2",
                    "project_key": "G01",
                    "completion_owner": "manual",
                },
                "agg-c": {
                    "title": "묶음 C",
                    "kind": "backlog",
                    "project_key": "G01",
                    "completion_owner": "aggregate",
                },
            },
            "tasks": {"m-a": {"record_id": "task-a"}},
            "aliases": {"B-7": "meas-b"},
            "sync": {"task-a": {}},
        }
    )


def tracking_file(orch_home: Path, repository: str = REPO) -> Path:
    path = config.target_paths(orch_home, ALPHA).root / tracksync.FILE_NAME
    write(path, tracking_text(repository))
    tracking.parse_file(tracking_text(repository), target=ALPHA)
    return path


def imported(orch_home: Path) -> None:
    path = tracking_file(orch_home)
    with db(orch_home) as conn:
        tracking.import_file(conn, path, target=ALPHA, at="2026-11-18T00:00:00+00:00")


def report(
    target: str = ALPHA, outcome: str = "synced", **fields: object
) -> SyncReport:
    values: dict[str, object] = {
        "target": target,
        "outcome": outcome,
        "imported": False,
        "checked": 0,
        "skipped": 0,
        "unchanged": 0,
        "written": (),
        "failed": (),
        "changes": (),
        "notified": False,
    }
    values.update(fields)
    return SyncReport(**values)


class FakeSync:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[dict[str, object]] = []
        self.reports: dict[str, SyncReport | Exception] = {}
        self.action: Callable[[dict[str, object]], None] | None = None
        monkeypatch.setattr(tracksync, "sync", self)

    def __call__(self, **kwargs: object) -> SyncReport:
        self.calls.append(kwargs)
        if self.action is not None:
            self.action(kwargs)
        result = self.reports.get(str(kwargs["target"]), report(str(kwargs["target"])))
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def fake_sync(monkeypatch: pytest.MonkeyPatch) -> FakeSync:
    return FakeSync(monkeypatch)


def plain(value: SyncReport) -> dict[str, object]:
    return json.loads(json.dumps(asdict(value)))


# Choosing the target


def test_sync_hands_over_the_target_and_its_repository(
    orch: Call, fake_sync: FakeSync, env: Path
) -> None:
    code, data = orch("track", "sync", "--target", ALPHA)
    assert (code, data) == (0, {"ok": True, "reports": [plain(report())]})
    [call] = fake_sync.calls
    assert call["target"] == ALPHA
    assert call["github_repo"] == REPO
    assert call["orch_home"] == env
    assert call["home"] == env.parent
    assert call["dry_run"] is False
    assert isinstance(call["conn"], sqlite3.Connection)
    now = call["now"]
    assert isinstance(now, datetime) and now.utcoffset() is not None
    assert "api" not in call and "sleep" not in call


def test_sync_uses_orch_target(
    orch: Call, fake_sync: FakeSync, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ORCH_TARGET", BETA)
    assert orch("track", "sync", "--dry-run")[0] == 0
    [call] = fake_sync.calls
    assert (call["target"], call["github_repo"], call["dry_run"]) == (
        BETA,
        "owner/beta",
        True,
    )


def test_sync_all_runs_every_target_by_name(orch: Call, fake_sync: FakeSync) -> None:
    code, data = orch("track", "sync", "--all")
    assert code == 0
    assert [c["target"] for c in fake_sync.calls] == [ALPHA, BETA]
    assert [r["target"] for r in data["reports"]] == [ALPHA, BETA]  # type: ignore[index]
    assert len({id(c["conn"]) for c in fake_sync.calls}) == 1


def test_sync_all_goes_on_after_a_target_fails(orch: Call, fake_sync: FakeSync) -> None:
    fake_sync.reports[ALPHA] = RuntimeError("boom")
    code, data = orch("track", "sync", "--all")
    assert code == 20
    assert data["ok"] is False and data["error"] == "track_failed"
    assert data["message"] == "GitHub 작업 기록 동기화에 실패한 기록이 있습니다."
    assert data["reports"] == [
        {"target": ALPHA, "outcome": "error"},
        plain(report(BETA)),
    ]


@pytest.mark.parametrize(
    ("args", "environ"),
    [
        (("track", "sync"), {}),
        (("track", "status"), {}),
        (("track", "import"), {}),
        (("track", "accept", "meas-b"), {}),
        (("track", "bind", "m-x", "task-a"), {}),
        (("track", "sync", "--all", "--target", ALPHA), {}),
        (("track", "sync", "--all"), {"ORCH_TARGET": ALPHA}),
        (("track", "sync", "--target", ALPHA), {"ORCH_HUB": ""}),
        (("track",), {}),
        (("track", "sync", "--bogus"), {}),
    ],
)
def test_usage_errors(
    orch: Call,
    fake_sync: FakeSync,
    monkeypatch: pytest.MonkeyPatch,
    env: Path,
    args: tuple[str, ...],
    environ: dict[str, str],
) -> None:
    for name, value in environ.items():
        if value:
            monkeypatch.setenv(name, value)
        else:
            monkeypatch.delenv(name)
    code, data = orch(*args)
    assert (code, data["ok"], data["error"]) == (USAGE, False, "usage")
    assert fake_sync.calls == []
    assert not config.state_db_path(env).exists()


def test_another_target_than_the_session_is_refused(
    orch: Call, fake_sync: FakeSync, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ORCH_TARGET", ALPHA)
    code, data = orch("track", "sync", "--target", BETA)
    assert (code, data["error"]) == (REFUSED, "refused")
    assert fake_sync.calls == []


def test_an_unregistered_target_is_refused(orch: Call, fake_sync: FakeSync) -> None:
    for args in (
        ("track", "sync", "--target", "gamma"),
        ("track", "status", "--target", "gamma"),
    ):
        code, data = orch(*args)
        assert (code, data["error"]) == (REFUSED, "refused")
    assert fake_sync.calls == []


# sync: output and exit codes


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (report(outcome="locked"), 0),
        (report(outcome="not_configured"), 0),
        (report(outcome="synced", written=("task-a",)), 0),
        (report(outcome="dry_run"), 0),
        (report(outcome="failed", failed=(("*", "auth_failed"),)), 20),
        (report(outcome="failed", failed=(("task-a", "api_error"),)), 20),
        (report(outcome="dry_run", failed=(("task-a", "not_found"),)), 20),
    ],
)
def test_sync_exit_code_follows_the_report(
    orch: Call, fake_sync: FakeSync, value: SyncReport, code: int
) -> None:
    fake_sync.reports[ALPHA] = value
    got, data = orch("track", "sync", "--target", ALPHA)
    assert got == code
    assert data["reports"] == [plain(value)]
    assert data["ok"] is (code == 0)


def test_sync_reports_dry_run_changes(orch: Call, fake_sync: FakeSync) -> None:
    wanted = Wanted(
        issue_state="closed",
        reason="completed",
        status="Done",
        pr_url=None,
        task_id="m-a",
    )
    fake_sync.reports[ALPHA] = report(
        outcome="dry_run",
        changes=(
            Change(record_id="task-a", wanted=wanted, actions=("close", "set_status")),
        ),
    )
    code, data = orch("track", "sync", "--target", ALPHA, "--dry-run")
    assert code == 0
    assert data["reports"][0]["changes"] == [  # type: ignore[index]
        {
            "record_id": "task-a",
            "wanted": {
                "issue_state": "closed",
                "reason": "completed",
                "status": "Done",
                "pr_url": None,
                "task_id": "m-a",
            },
            "actions": ["close", "set_status"],
        }
    ]


def test_sync_token_comes_from_the_app_and_never_shows(
    orch: Call, fake_sync: FakeSync, monkeypatch: pytest.MonkeyPatch, env: Path
) -> None:
    issued: list[Path] = []

    def issue(orch_home: Path, secrets: list[str]) -> str:
        issued.append(orch_home)
        secrets.append(TOKEN)
        return TOKEN

    monkeypatch.setattr(credentials, "issue_token", issue)

    def use_token(call: dict[str, object]) -> None:
        token = call["token_provider"]()  # type: ignore[operator]
        assert token == TOKEN
        raise RuntimeError(f"leaked {token}")

    fake_sync.action = use_token
    code, data = orch("track", "sync", "--target", ALPHA)
    assert code == 70
    assert issued == [env]
    assert TOKEN not in json.dumps(data)


def test_sync_failure_notification(
    orch: Call, fake_sync: FakeSync, monkeypatch: pytest.MonkeyPatch, env: Path
) -> None:
    write(config.config_path(env), f'[notify]\nmethod = "ntfy"\ntopic = "{TOPIC}"\n')
    config.config_path(env).chmod(0o600)
    sent: list[tuple[str, dict[str, object]]] = []

    def send(event: str, **kwargs: object) -> notify.Outcome:
        sent.append((event, kwargs))
        return notify.Outcome.SENT

    monkeypatch.setattr(notify, "send", send)

    def notify_and_leak(call: dict[str, object]) -> None:
        call["notify_failure"]()  # type: ignore[operator]
        if call["target"] == BETA:
            raise RuntimeError(f"leaked {TOPIC}")

    fake_sync.action = notify_and_leak
    code, data = orch("track", "sync", "--all")
    assert code == 20
    assert [(event, kwargs["target"]) for event, kwargs in sent] == [
        ("doctor_track_failed", ALPHA),
        ("doctor_track_failed", BETA),
    ]
    assert sent[0][1]["orch_home"] == env
    assert TOPIC not in json.dumps(data)


def test_sync_runs_the_real_engine_without_a_tracking_file(
    orch: Call, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_token(orch_home: Path, secrets: list[str]) -> str:
        raise AssertionError("no token is needed")

    monkeypatch.setattr(credentials, "issue_token", no_token)
    for args in (
        ("track", "sync", "--target", ALPHA),
        ("track", "sync", "--all", "--dry-run"),
    ):
        code, data = orch(*args)
        assert code == 0, data
        assert {r["outcome"] for r in data["reports"]} <= {"not_configured", "dry_run"}  # type: ignore[union-attr]


# import


def test_import_reads_the_runtime_file(orch: Call, env: Path) -> None:
    path = tracking_file(env)
    before = path.read_bytes()
    code, data = orch("track", "import", "--target", ALPHA)
    assert code == 0, data
    assert data == {
        "ok": True,
        "target": ALPHA,
        "records_added": ["agg-c", "meas-b", "task-a"],
        "records_updated": [],
        "records_unchanged": 0,
        "projects": 1,
        "tasks": 1,
        "aliases": 1,
        "dropped_keys": ["sync"],
    }
    with db(env) as conn:
        assert tracking.repository(conn, ALPHA) == REPO
        assert tracking.record_for_task(conn, ALPHA, "m-a").record_id == "task-a"
    assert path.read_bytes() == before
    code, data = orch("track", "import", "--target", ALPHA)
    assert (code, data["records_added"], data["records_unchanged"]) == (0, [], 3)


def test_import_compares_the_repository_without_case(orch: Call, env: Path) -> None:
    tracking_file(env, repository="owner/alpha")
    assert orch("track", "import", "--target", ALPHA)[0] == 0


@pytest.mark.parametrize("case", ["missing", "other_repository", "malformed"])
def test_import_refusals_change_nothing(orch: Call, env: Path, case: str) -> None:
    if case == "other_repository":
        tracking_file(env, repository="owner/other")
    elif case == "malformed":
        write(config.target_paths(env, ALPHA).root / tracksync.FILE_NAME, "{")
    code, data = orch("track", "import", "--target", ALPHA)
    assert (code, data["error"]) == (REFUSED, "refused")
    with db(env) as conn:
        assert tracking.repository(conn, ALPHA) is None
        assert tracking.list_records(conn, ALPHA) == ()


# bind and accept


def test_bind_a_task_to_a_record(orch: Call, env: Path) -> None:
    imported(env)
    first = orch("track", "bind", "m-new", "agg-c", "--target", ALPHA)
    again = orch("track", "bind", "m-new", "agg-c", "--target", ALPHA)
    by_alias = orch("track", "bind", "m-other", "B-7", "--target", ALPHA)
    expected = {"ok": True, "target": ALPHA, "task_id": "m-new", "record_id": "agg-c"}
    assert first == (0, {**expected, "changed": True})
    assert again == (0, {**expected, "changed": False})
    assert by_alias == (
        0,
        {**expected, "task_id": "m-other", "record_id": "meas-b", "changed": True},
    )
    with db(env) as conn:
        assert tracking.task_ids(conn, ALPHA, "agg-c") == ("m-new",)


@pytest.mark.parametrize(
    ("task_id", "record"), [("m-new", "nope"), ("-bad id", "agg-c")]
)
def test_bind_refusals(orch: Call, env: Path, task_id: str, record: str) -> None:
    imported(env)
    code, data = orch("track", "bind", "--target", ALPHA, "--", task_id, record)
    assert (code, data["error"]) == (REFUSED, "refused")
    with db(env) as conn:
        assert tracking.task_ids(conn, ALPHA, "agg-c") == ()


def test_accept_a_manual_record(
    orch: Call, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    imported(env)
    monkeypatch.setenv("ORCH_TARGET", ALPHA)
    code, first = orch("track", "accept", "B-7")
    assert code == 0
    accepted_at = first["accepted_at"]
    assert isinstance(accepted_at, str)
    assert datetime.fromisoformat(accepted_at).utcoffset() is not None
    assert first == {
        "ok": True,
        "target": ALPHA,
        "record_id": "meas-b",
        "accepted_at": accepted_at,
        "changed": True,
    }
    assert orch("track", "accept", "meas-b") == (0, {**first, "changed": False})
    assert orch("track", "accept", "agg-c")[0] == 0


@pytest.mark.parametrize("record", ["task-a", "nope"])
def test_accept_refusals(orch: Call, env: Path, record: str) -> None:
    imported(env)
    code, data = orch("track", "accept", record, "--target", ALPHA)
    assert (code, data["error"]) == (REFUSED, "refused")
    with db(env) as conn:
        assert all(r.accepted_at is None for r in tracking.list_records(conn, ALPHA))


# status


def test_status_before_an_import(orch: Call, env: Path) -> None:
    assert orch("track", "status", "--target", ALPHA) == (
        0,
        {
            "ok": True,
            "target": ALPHA,
            "repository": None,
            "records": 0,
            "by_owner": {},
            "accepted": 0,
            "failing": [],
            "run": None,
        },
    )


def test_status_summarises_records_failures_and_the_last_run(
    orch: Call, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_token(orch_home: Path, secrets: list[str]) -> str:
        raise AssertionError("status reads only")

    monkeypatch.setattr(credentials, "issue_token", no_token)
    imported(env)
    at = "2026-11-18T01:00:00+00:00"
    with db(env) as conn:
        tracking.accept_record(conn, target=ALPHA, record_id="meas-b", at=at)
        for _ in range(2):
            tracking.record_failure(
                conn, target=ALPHA, record_id="task-a", error="api_error", at=at
            )
        tracking.record_run(conn, target=ALPHA, at=at, error=None)
        tracking.record_run(conn, target=ALPHA, at=at, error="records_failed")
    code, data = orch("track", "status", "--target", ALPHA)
    assert code == 0
    assert data == {
        "ok": True,
        "target": ALPHA,
        "repository": REPO,
        "records": 3,
        "by_owner": {"aggregate": 1, "manual": 1, "task": 1},
        "accepted": 1,
        "failing": [{"record_id": "task-a", "failures": 2, "last_error": "api_error"}],
        "run": {
            "target": ALPHA,
            "last_at": at,
            "last_success_at": at,
            "consecutive_failures": 1,
            "last_error": "records_failed",
        },
    }


# Plan files after import, bind and accept, and with TODO.md


def plan_file(orch_home: Path, target: str = ALPHA, key: str = "G01") -> Path:
    return config.target_paths(orch_home, target).plans / f"{key}.md"


def add_task(orch_home: Path, task_id: str, state: TaskState) -> None:
    with db(orch_home) as conn:
        store.insert_task(
            conn,
            Task(
                id=task_id,
                target=ALPHA,
                title=task_id,
                grade=Grade.L2,
                lane=Lane.B,
                model="m",
                state=state,
                timeout_sec=60,
                created_at="2026-11-18T00:00:00+00:00",
                updated_at="2026-11-18T00:00:00+00:00",
            ),
        )


def import_beta(orch_home: Path) -> None:
    data = json.loads(tracking_text("owner/beta"))
    data.update(target=BETA, tasks={}, aliases={})
    path = orch_home / "beta.json"
    write(path, json.dumps(data, ensure_ascii=False))
    with db(orch_home) as conn:
        tracking.import_file(conn, path, target=BETA, at="2026-11-18T00:00:00+00:00")


class PlanSpy:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[str] = []
        self.fail: dict[str, Exception] = {}
        self.real = plans.refresh_plans
        monkeypatch.setattr(plans, "refresh_plans", self)

    def __call__(
        self, conn: sqlite3.Connection, orch_home: Path, target: str, now: datetime
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        assert now.utcoffset() is not None
        self.calls.append(target)
        if target in self.fail:
            raise self.fail[target]
        return self.real(conn, orch_home, target, now)


@pytest.fixture
def plan_spy(monkeypatch: pytest.MonkeyPatch) -> PlanSpy:
    return PlanSpy(monkeypatch)


def test_import_writes_the_plan_files(orch: Call, env: Path, plan_spy: PlanSpy) -> None:
    tracking_file(env)
    assert orch("track", "import", "--target", ALPHA)[0] == 0
    assert plan_spy.calls == [ALPHA]
    text = plan_file(env).read_text(encoding="utf-8")
    assert text.startswith("# 계획: G01\n")
    assert "- 작업 미등록: task-a\n" in text


def test_bind_rewrites_the_plan_file(orch: Call, env: Path, plan_spy: PlanSpy) -> None:
    imported(env)
    add_task(env, "m-new", TaskState.QUEUED)
    assert not plan_file(env).exists()
    assert orch("track", "bind", "m-new", "agg-c", "--target", ALPHA)[0] == 0
    assert plan_spy.calls == [ALPHA]
    text = plan_file(env).read_text(encoding="utf-8")
    assert "| m-new queued L2/B |" in text
    assert "- m-new\n" in text


def test_accept_removes_the_plan_of_a_finished_group(
    orch: Call, env: Path, plan_spy: PlanSpy
) -> None:
    imported(env)
    add_task(env, "m-a", TaskState.CLOSED)
    write(plan_file(env), "old")
    assert orch("track", "accept", "meas-b", "--target", ALPHA)[0] == 0
    text = plan_file(env).read_text(encoding="utf-8")
    assert "| agg-c |" in text and "| meas-b |" not in text
    assert orch("track", "accept", "agg-c", "--target", ALPHA)[0] == 0
    assert not plan_file(env).exists()
    assert plan_spy.calls == [ALPHA, ALPHA]


@pytest.mark.parametrize(
    "args",
    [
        ("track", "import"),
        ("track", "bind", "m-x", "task-a"),
        ("track", "accept", "meas-b"),
    ],
)
def test_a_failed_plan_refresh_only_warns(
    orch: Call,
    env: Path,
    plan_spy: PlanSpy,
    capsys: pytest.CaptureFixture[str],
    args: tuple[str, ...],
) -> None:
    if args[1] == "import":
        tracking_file(env)
    else:
        imported(env)
    plan_spy.fail[ALPHA] = OSError("disk\nfull")
    code, data = orch(*args, "--target", ALPHA)
    assert (code, data["ok"], data["target"]) == (0, True, ALPHA)
    err = capsys.readouterr().err
    assert "계획 파일 갱신 실패: disk full\n" in err
    assert not plan_file(env).exists()
    with db(env) as conn:
        assert tracking.repository(conn, ALPHA) == REPO


def test_refusals_and_sync_leave_the_plans_alone(
    orch: Call, env: Path, plan_spy: PlanSpy, fake_sync: FakeSync
) -> None:
    imported(env)
    assert orch("track", "accept", "nope", "--target", ALPHA)[0] == REFUSED
    assert orch("track", "sync", "--target", ALPHA)[0] == 0
    assert orch("track", "status", "--target", ALPHA)[0] == 0
    assert plan_spy.calls == []
    assert not plan_file(env).exists()


def test_todo_refresh_writes_every_tracked_target(
    orch: Call, env: Path, plan_spy: PlanSpy
) -> None:
    imported(env)
    import_beta(env)
    add_task(env, "m-a", TaskState.PR_OPEN)
    code, data = orch("next")
    assert code == 0, data
    assert plan_spy.calls == [ALPHA, BETA]
    assert "| m-a pr_open L2/B |" in plan_file(env).read_text(encoding="utf-8")
    assert "- 작업 미등록: task-a\n" in plan_file(env, BETA).read_text(encoding="utf-8")
    assert config.target_paths(env, ALPHA).todo.is_file()


def test_one_failed_plan_target_does_not_stop_the_others(
    orch: Call,
    env: Path,
    plan_spy: PlanSpy,
    capsys: pytest.CaptureFixture[str],
) -> None:
    imported(env)
    import_beta(env)
    add_task(env, "m-a", TaskState.PREPARED)
    plan_spy.fail[ALPHA] = OSError("disk full")
    assert orch("next")[0] == 0
    assert plan_spy.calls == [ALPHA, BETA]
    assert plan_file(env, BETA).is_file()
    assert not plan_file(env).exists()
    assert config.target_paths(env, ALPHA).todo.is_file()
    assert "계획 파일 갱신 실패: disk full" in capsys.readouterr().err
