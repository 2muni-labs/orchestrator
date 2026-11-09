"""External command results here come from measurements recorded in the task spec."""

import ast
import functools
import inspect
import json
import sqlite3
import subprocess
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from orch import config, doctor, lifecycle, notify, service, store
from orch.config import Config, Target, target_paths
from orch.doctor import DoctorReport, run_doctor
from orch.gc import GcReport
from orch.lifecycle import KillReport, LifecycleError
from orch.models import (
    CheckLevel,
    DoctorCheck,
    FailureReason,
    Grade,
    Lane,
    Outcome,
    Pause,
    Run,
    Task,
    TaskState,
)
from orch.terminal import MainPaneStatus

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
CLAUDE = Path("/abs/bin/claude")
CODEX = Path("/abs/bin/codex")
TOKEN = "ghs_doctor_test_token_value"
GB = 10**9
SETTINGS = Config(claude_path=CLAUDE, codex_path=CODEX, retention_days=30)
CLAUDE_OK = json.dumps(
    {
        "loggedIn": True,
        "authMethod": "claude.ai",
        "email": "person@example.com",
        "loginExpiresAt": (NOW + timedelta(days=24)).isoformat(),
    }
)
TAILSCALE_OK = json.dumps({"BackendState": "Running", "Self": {"Online": True}})


class Harness:
    """Fakes for every external call, all healthy by default."""

    def __init__(self, conn: sqlite3.Connection, orch_home: Path) -> None:
        self.conn = conn
        self.orch_home = orch_home
        self.results: dict[str, tuple[int, str] | Exception] = {
            "claude": (0, CLAUDE_OK),
            "codex": (0, "Logged in using ChatGPT"),
            "token": (0, TOKEN + "\n"),
            "tailscale": (0, TAILSCALE_OK),
        }
        self.commands: list[list[str]] = []
        self.free: list[int] = [50 * GB]
        self.statuses: dict[str, MainPaneStatus | Exception] = {}
        self.terminals: list[tuple[str, Path]] = []
        self.sent: list[dict[str, object]] = []
        self.outcome = notify.Outcome.SENT
        self.collected: list[dict[str, object]] = []
        self.collect_error: Exception | None = None
        self.kills: list[tuple[str, dict[str, object]]] = []
        self.kill_results: dict[str, KillReport | Exception] = {}
        self.targets: dict[str, Target] = {}

    def run(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        self.commands.append(argv)
        name = {
            str(CLAUDE): "claude",
            str(CODEX): "codex",
            str(self.orch_home / "bin" / "ghapp-token.sh"): "token",
            "/usr/local/bin/tailscale": "tailscale",
        }[argv[0]]
        value = self.results[name]
        if isinstance(value, Exception):
            raise value
        return subprocess.CompletedProcess(argv, value[0], value[1], "private stderr")

    def free_bytes(self, path: Path) -> int:
        assert path == self.orch_home
        return self.free.pop(0) if len(self.free) > 1 else self.free[0]

    def terminal(self, session: str, record: Path) -> Harness._Terminal:
        self.terminals.append((session, record))
        return Harness._Terminal(self.statuses[session])

    class _Terminal:
        def __init__(self, status: MainPaneStatus | Exception) -> None:
            self.status = status

        def main_pane_status(self, claude: object) -> MainPaneStatus:
            assert claude == CLAUDE
            if isinstance(self.status, Exception):
                raise self.status
            return self.status

    def send(self, event: str, **kwargs: object) -> notify.Outcome:
        self.sent.append({"event": event, **kwargs})
        return self.outcome

    def collect(self, conn: sqlite3.Connection, **kwargs: object) -> GcReport:
        self.collected.append(kwargs)
        if self.collect_error is not None:
            raise self.collect_error
        return GcReport(dry_run=False, removed_files=("x/logs/a.1.log",))

    def kill(
        self, conn: sqlite3.Connection, task_id: str, **kwargs: object
    ) -> KillReport:
        self.kills.append((task_id, kwargs))
        value = self.kill_results.get(task_id, killed_report(task_id))
        if isinstance(value, Exception):
            raise value
        return value

    def go(self, **overrides: object) -> DoctorReport:
        values: dict[str, object] = {
            "settings": SETTINGS,
            "targets": self.targets,
            "orch_home": self.orch_home,
            "home": self.orch_home.parent,
            "now": NOW,
            "environ": {"HOME": str(self.orch_home.parent)},
            "run": self.run,
            "free_bytes": self.free_bytes,
            "terminal": self.terminal,
            "send": self.send,
            "collect": self.collect,
            "kill": self.kill,
        }
        values.update(overrides)
        return run_doctor(self.conn, **values)  # type: ignore[arg-type]

    def events(self) -> list[object]:
        return [call["event"] for call in self.sent]


def killed_report(task_id: str) -> KillReport:
    return KillReport(
        task_id=task_id,
        state=TaskState.JUDGING,
        outcome=Outcome.FAILED,
        failure_reason=FailureReason.ABORTED,
        finished_by="kill",
        signalled=True,
    )


@pytest.fixture
def orch_home(tmp_path: Path) -> Path:
    path = tmp_path / "home" / ".orch"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def conn(orch_home: Path) -> Iterator[sqlite3.Connection]:
    connection = store.connect(orch_home / "state.db")
    yield connection
    connection.close()


@pytest.fixture
def h(conn: sqlite3.Connection, orch_home: Path) -> Harness:
    return Harness(conn, orch_home)


def check(report: DoctorReport, item: str, target: str = "") -> DoctorCheck:
    found = [c for c in report.checks if c.item == item and c.target == target]
    assert len(found) == 1, (item, target, report.checks)
    return found[0]


def result_of(report: DoctorReport, item: str) -> tuple[CheckLevel, str | None]:
    found = check(report, item)
    return found.level, found.detail


def stored(conn: sqlite3.Connection, item: str, target: str = "") -> CheckLevel | None:
    for row in store.list_doctor_checks(conn):
        if row.item == item and row.target == target:
            return row.level
    return None


def pauses(conn: sqlite3.Connection) -> list[str]:
    return [pause.reason for pause in store.list_pauses(conn)]


# A healthy pass


def test_a_healthy_pass_reports_every_item_ok_and_sends_nothing(h: Harness) -> None:
    report = h.go()
    assert [(c.item, c.level) for c in report.checks] == [
        ("claude_auth", CheckLevel.OK),
        ("codex_auth", CheckLevel.OK),
        ("github_app", CheckLevel.OK),
        ("disk", CheckLevel.OK),
        ("network", CheckLevel.OK),
    ]
    assert all(c.at == NOW.isoformat() for c in report.checks)
    assert h.sent == []
    assert report.killed == ()
    assert report.paused == ()
    assert report.errors == ()
    assert report.gc is None


def test_the_commands_are_the_measured_ones(h: Harness, orch_home: Path) -> None:
    h.go()
    assert h.commands == [
        [str(CLAUDE), "auth", "status", "--json"],
        [str(CODEX), "login", "status"],
        [str(orch_home / "bin" / "ghapp-token.sh")],
        ["/usr/local/bin/tailscale", "status", "--json"],
    ]


def test_every_item_is_recorded(h: Harness, conn: sqlite3.Connection) -> None:
    h.go()
    assert {row.item for row in store.list_doctor_checks(conn)} == {
        "claude_auth",
        "codex_auth",
        "github_app",
        "disk",
        "network",
    }


# main_session


def record_for(orch_home: Path, *names: str) -> None:
    for name in names:
        path = orch_home / name / "terminal.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")


def add_target(h: Harness, *names: str) -> None:
    for name in names:
        h.targets[name] = Target(
            name=name,
            path=Path("/repo"),
            github="o/r",
            default_branch="main",
            setup_command="true",
            test_command="true",
            lint_command="true",
        )


def test_an_alive_main_pane_is_ok(h: Harness, orch_home: Path) -> None:
    add_target(h, "one")
    record_for(orch_home, "one")
    h.statuses["orch-one"] = MainPaneStatus.ALIVE
    report = h.go()
    found = check(report, "main_session", "one")
    assert (found.level, found.detail) == (CheckLevel.OK, None)
    assert h.terminals == [("orch-one", orch_home / "one" / "terminal.json")]


@pytest.mark.parametrize(
    "status",
    [status for status in MainPaneStatus if status is not MainPaneStatus.ALIVE],
)
def test_any_other_pane_status_is_a_warning(
    h: Harness, orch_home: Path, status: MainPaneStatus
) -> None:
    add_target(h, "one")
    record_for(orch_home, "one")
    h.statuses["orch-one"] = status
    found = check(h.go(), "main_session", "one")
    assert (found.level, found.detail) == (CheckLevel.WARN, status.value)


def test_a_target_without_a_record_is_not_checked(h: Harness) -> None:
    add_target(h, "one")
    report = h.go()
    assert h.terminals == []
    assert [c for c in report.checks if c.item == "main_session"] == []


def test_targets_are_checked_in_name_order(h: Harness, orch_home: Path) -> None:
    add_target(h, "two", "one")
    record_for(orch_home, "one", "two")
    h.statuses.update(
        {"orch-one": MainPaneStatus.ALIVE, "orch-two": MainPaneStatus.PANE_DEAD}
    )
    report = h.go()
    assert [(c.target, c.level) for c in report.checks[:2]] == [
        ("one", CheckLevel.OK),
        ("two", CheckLevel.WARN),
    ]
    assert h.events() == ["doctor_main_session"]
    assert h.sent[0]["target"] == "two"


def test_main_session_without_a_claude_path_is_a_warning(
    h: Harness, orch_home: Path
) -> None:
    add_target(h, "one")
    record_for(orch_home, "one")
    report = h.go(settings=Config(codex_path=CODEX))
    found = check(report, "main_session", "one")
    assert (found.level, found.detail) == (CheckLevel.WARN, "not_configured")
    assert h.terminals == []


def test_a_dead_main_session_never_pauses_or_restarts(
    h: Harness, conn: sqlite3.Connection, orch_home: Path
) -> None:
    add_target(h, "one")
    record_for(orch_home, "one")
    h.statuses["orch-one"] = MainPaneStatus.PANE_GONE
    h.go()
    assert pauses(conn) == []
    assert not any("open" in argv for argv in h.commands)


def test_the_module_does_not_reach_for_a_restart() -> None:
    tree = ast.parse(inspect.getsource(doctor))
    strings = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "open" not in strings
    assert "restore" not in strings
    assert "new-session" not in strings


# claude_auth


def claude_output(**fields: object) -> tuple[int, str]:
    return 0, json.dumps({"loggedIn": True, **fields})


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (claude_output(loginExpiresAt=None), (CheckLevel.OK, None)),
        (claude_output(), (CheckLevel.OK, None)),
        (
            claude_output(
                loginExpiresAt=(NOW + timedelta(days=7, seconds=1)).isoformat()
            ),
            (CheckLevel.OK, None),
        ),
        (
            claude_output(loginExpiresAt=(NOW + timedelta(days=7)).isoformat()),
            (CheckLevel.WARN, "expires_soon"),
        ),
        (
            claude_output(loginExpiresAt=(NOW - timedelta(days=1)).isoformat()),
            (CheckLevel.WARN, "expires_soon"),
        ),
        (
            claude_output(loginExpiresAt="2026-11-10T00:00:00Z"),
            (CheckLevel.WARN, "expires_soon"),
        ),
        (claude_output(loginExpiresAt="soon"), (CheckLevel.WARN, "expiry_unreadable")),
        (claude_output(loginExpiresAt=12), (CheckLevel.WARN, "expiry_unreadable")),
        (
            claude_output(loginExpiresAt="2026-11-10T00:00:00"),
            (CheckLevel.WARN, "expiry_unreadable"),
        ),
        ((1, json.dumps({"loggedIn": False})), (CheckLevel.FAIL, "exit=1")),
        ((0, "not json"), (CheckLevel.FAIL, "not_json")),
        ((0, "[1]"), (CheckLevel.FAIL, "not_json")),
        ((0, json.dumps({"loggedIn": False})), (CheckLevel.FAIL, "not_logged_in")),
        ((0, json.dumps({"loggedIn": "true"})), (CheckLevel.FAIL, "not_logged_in")),
        ((0, json.dumps({})), (CheckLevel.FAIL, "not_logged_in")),
        (OSError("private"), (CheckLevel.FAIL, "OSError")),
        (subprocess.TimeoutExpired("x", 30), (CheckLevel.FAIL, "TimeoutExpired")),
    ],
)
def test_claude_auth_levels(
    h: Harness,
    value: tuple[int, str] | Exception,
    expected: tuple[CheckLevel, str | None],
) -> None:
    h.results["claude"] = value
    assert result_of(h.go(), "claude_auth") == expected


def test_no_field_of_the_claude_output_reaches_the_report(h: Harness) -> None:
    h.results["claude"] = (
        0,
        json.dumps(
            {
                "loggedIn": True,
                "email": "person@example.com",
                "orgName": "private org",
                "loginExpiresAt": (NOW + timedelta(days=1)).isoformat(),
            }
        ),
    )
    text = str(h.go())
    assert "person@example.com" not in text
    assert "private org" not in text


def test_a_missing_claude_path_fails_without_a_command(h: Harness) -> None:
    report = h.go(settings=Config(codex_path=CODEX))
    assert result_of(report, "claude_auth") == (CheckLevel.FAIL, "not_configured")
    assert not any(argv[0] == str(CLAUDE) for argv in h.commands)


# codex_auth


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ((0, "Logged in"), (CheckLevel.OK, None)),
        ((1, "Not logged in"), (CheckLevel.FAIL, "exit=1")),
        (OSError("private"), (CheckLevel.FAIL, "OSError")),
    ],
)
def test_codex_auth_levels(
    h: Harness,
    value: tuple[int, str] | Exception,
    expected: tuple[CheckLevel, str | None],
) -> None:
    h.results["codex"] = value
    assert result_of(h.go(), "codex_auth") == expected


def test_a_missing_codex_path_fails_without_a_command(h: Harness) -> None:
    report = h.go(settings=Config(claude_path=CLAUDE))
    assert result_of(report, "codex_auth") == (CheckLevel.FAIL, "not_configured")
    assert not any(argv[0] == str(CODEX) for argv in h.commands)


# github_app


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ((0, "token\n"), (CheckLevel.OK, None)),
        ((0, "  \n"), (CheckLevel.FAIL, "empty")),
        ((1, ""), (CheckLevel.FAIL, "exit=1")),
        (OSError("private"), (CheckLevel.FAIL, "OSError")),
    ],
)
def test_github_app_levels(
    h: Harness,
    value: tuple[int, str] | Exception,
    expected: tuple[CheckLevel, str | None],
) -> None:
    h.results["token"] = value
    assert result_of(h.go(), "github_app") == expected


def test_the_token_stays_out_of_the_report_records_and_notifications(
    h: Harness, conn: sqlite3.Connection
) -> None:
    h.results["codex"] = (1, "")
    report = h.go()
    assert TOKEN not in str(report)
    assert TOKEN not in str(store.list_doctor_checks(conn))
    assert TOKEN not in str(store.list_pauses(conn))
    assert TOKEN not in str(h.sent)


PRIVATE = (
    TOKEN,
    "person@example.com",
    "private org",
    "private-host",
    "private stderr",
    "private message",
)
PRIVATE_CLAUDE = {"email": "person@example.com", "orgName": "private org"}
PRIVATE_SELF = {"DNSName": "private-host", "Online": True}
PRIVATE_ERROR = f"{TOKEN} person@example.com private message"


def ntfy_settings(orch_home: Path) -> Config:
    config_file = config.config_path(orch_home)
    config_file.write_text("", encoding="utf-8")
    config_file.chmod(0o600)
    return Config(
        claude_path=CLAUDE,
        codex_path=CODEX,
        retention_days=30,
        notify_method="ntfy",
        notify_topic="abcdefghijklmnopqrstuvwxyz012345",
    )


@pytest.mark.parametrize(
    ("results", "details"),
    [
        pytest.param(
            {
                "claude": (1, json.dumps({"loggedIn": False, **PRIVATE_CLAUDE})),
                "codex": (1, "Logged out person@example.com"),
                "token": (1, TOKEN),
                "tailscale": (1, json.dumps({"Self": PRIVATE_SELF})),
            },
            {
                "claude_auth": "exit=1",
                "codex_auth": "exit=1",
                "github_app": "exit=1",
                "network": "exit=1",
            },
            id="failing-commands-print-private-output",
        ),
        pytest.param(
            {
                "claude": (
                    0,
                    json.dumps(
                        {
                            "loggedIn": True,
                            **PRIVATE_CLAUDE,
                            "loginExpiresAt": (NOW + timedelta(days=1)).isoformat(),
                        }
                    ),
                ),
                "codex": (2, TOKEN),
                "token": (3, TOKEN),
                "tailscale": (
                    0,
                    json.dumps({"BackendState": "Stopped", "Self": PRIVATE_SELF}),
                ),
            },
            {
                "claude_auth": "expires_soon",
                "codex_auth": "exit=2",
                "github_app": "exit=3",
                "network": "not_running",
            },
            id="parsed-output-holds-private-fields",
        ),
        pytest.param(
            {
                "claude": (0, f"{TOKEN} person@example.com"),
                "codex": (1, ""),
                "token": (1, ""),
                "tailscale": (0, f"{TOKEN} private-host"),
            },
            {
                "claude_auth": "not_json",
                "codex_auth": "exit=1",
                "github_app": "exit=1",
                "network": "not_json",
            },
            id="unparsable-output",
        ),
        pytest.param(
            {
                "claude": OSError(PRIVATE_ERROR),
                "codex": subprocess.TimeoutExpired("x", 30, output=TOKEN),
                "token": OSError(PRIVATE_ERROR),
                "tailscale": OSError(PRIVATE_ERROR),
            },
            {
                "claude_auth": "OSError",
                "codex_auth": "TimeoutExpired",
                "github_app": "OSError",
                "network": "OSError",
            },
            id="exceptions-hold-private-messages",
        ),
    ],
)
def test_private_values_reach_no_report_record_pause_or_notification_body(
    h: Harness,
    conn: sqlite3.Connection,
    orch_home: Path,
    results: dict[str, tuple[int, str] | Exception],
    details: dict[str, str],
) -> None:
    bodies: list[bytes] = []

    def transport(url: str, body: bytes, timeout: float) -> None:
        bodies.append(body)

    h.results.update(results)
    report = h.go(
        settings=ntfy_settings(orch_home),
        send=functools.partial(notify.send, transport=transport),
    )

    stored_details = {
        row[0]: row[1] for row in conn.execute("SELECT item, detail FROM doctor_checks")
    }
    assert stored_details == {**details, "disk": "free_gb=50"}
    assert bodies == [
        b"orch doctor_claude_auth",
        b"orch doctor_codex_auth",
        b"orch doctor_github_app",
        b"orch doctor_network",
    ]

    tables = [
        tuple(row)
        for table in ("doctor_checks", "pauses")
        for row in conn.execute(f"SELECT * FROM {table}")
    ]
    for value in PRIVATE:
        assert value not in str(report)
        assert value not in str(tables)
        assert value.encode() not in b" ".join(bodies)


def test_a_github_failure_alerts_but_does_not_pause(
    h: Harness, conn: sqlite3.Connection
) -> None:
    h.results["token"] = (1, "")
    report = h.go()
    assert h.events() == ["doctor_github_app"]
    assert pauses(conn) == []
    assert report.paused == ()


# disk


@pytest.mark.parametrize(
    ("free", "expected"),
    [
        (10 * GB, (CheckLevel.OK, "free_gb=10")),
        (50 * GB + 5, (CheckLevel.OK, "free_gb=50")),
        (10 * GB - 1, (CheckLevel.WARN, "free_gb=9")),
        (2 * GB, (CheckLevel.WARN, "free_gb=2")),
        (2 * GB - 1, (CheckLevel.FAIL, "free_gb=1")),
        (0, (CheckLevel.FAIL, "free_gb=0")),
    ],
)
def test_disk_levels(
    h: Harness, free: int, expected: tuple[CheckLevel, str | None]
) -> None:
    h.free = [free]
    assert result_of(h.go(), "disk") == expected


def test_the_disk_thresholds_can_be_changed(h: Harness) -> None:
    h.free = [5 * GB]
    report = h.go(disk_warn_gb=4, disk_fail_gb=1)
    assert result_of(report, "disk") == (CheckLevel.OK, "free_gb=5")
    assert h.collected == []


def test_a_healthy_disk_does_not_collect(h: Harness) -> None:
    assert h.go().gc is None
    assert h.collected == []


def test_a_low_disk_collects_and_judges_the_new_value(h: Harness) -> None:
    h.free = [5 * GB, 20 * GB]
    report = h.go()
    assert result_of(report, "disk") == (CheckLevel.OK, "free_gb=20")
    assert report.gc == GcReport(dry_run=False, removed_files=("x/logs/a.1.log",))
    assert h.events() == []
    assert h.collected == [
        {
            "targets": h.targets,
            "orch_home": h.orch_home,
            "now": NOW,
            "retention_days": 30,
        }
    ]


def test_a_failing_disk_collects_too(h: Harness, conn: sqlite3.Connection) -> None:
    h.free = [GB, GB]
    report = h.go()
    assert len(h.collected) == 1
    assert result_of(report, "disk") == (CheckLevel.FAIL, "free_gb=1")
    assert pauses(conn) == ["disk"]


def test_a_failed_collect_is_reported_and_the_first_value_stands(h: Harness) -> None:
    h.free = [5 * GB, 99 * GB]
    h.collect_error = RuntimeError("private")
    report = h.go()
    assert result_of(report, "disk") == (CheckLevel.WARN, "free_gb=5")
    assert report.errors == ("disk gc RuntimeError",)
    assert report.gc is None


# network


@pytest.mark.parametrize(
    ("value", "detail"),
    [
        ((1, ""), "exit=1"),
        ((0, "not json"), "not_json"),
        (
            (0, json.dumps({"BackendState": "Stopped", "Self": {"Online": True}})),
            "not_running",
        ),
        (
            (0, json.dumps({"BackendState": "Running", "Self": {"Online": False}})),
            "not_running",
        ),
        ((0, json.dumps({"BackendState": "Running"})), "not_running"),
        ((0, json.dumps({"BackendState": "Running", "Self": None})), "not_running"),
        (OSError("private"), "OSError"),
    ],
)
def test_network_problems_are_warnings(
    h: Harness, value: tuple[int, str] | Exception, detail: str
) -> None:
    h.results["tailscale"] = value
    assert result_of(h.go(), "network") == (CheckLevel.WARN, detail)


def broken_program(kind: str, directory: Path) -> Path:
    """Return a path that the real runner fails to execute in the given way."""
    path = directory / "tailscale"
    if kind == "not_executable":
        path.write_text("x", encoding="utf-8")
        path.chmod(0o644)
    elif kind == "bad_format":
        path.write_bytes(bytes(range(1, 64)))
        path.chmod(0o755)
    return path


def assert_only_network_is_affected(
    h: Harness, conn: sqlite3.Connection, report: DoctorReport, detail: str
) -> None:
    assert result_of(report, "network") == (CheckLevel.WARN, detail)
    assert stored(conn, "network") is CheckLevel.WARN
    assert h.events() == ["doctor_network", "doctor_stuck_task"]
    assert report.errors == ()
    assert report.paused == ()
    assert pauses(conn) == []
    assert report.killed == ("a",)
    for item in ("claude_auth", "codex_auth", "github_app"):
        assert result_of(report, item) == (CheckLevel.OK, None)
    assert result_of(report, "disk") == (CheckLevel.OK, "free_gb=50")


@pytest.mark.parametrize(
    ("kind", "detail"),
    [
        ("missing", "FileNotFoundError"),
        ("not_executable", "PermissionError"),
        ("bad_format", "OSError"),
    ],
)
def test_a_tailscale_that_cannot_run_only_warns_on_network(
    h: Harness,
    conn: sqlite3.Connection,
    tmp_path: Path,
    kind: str,
    detail: str,
) -> None:
    """Measured with service.run_command on macOS: each raises this OSError subclass."""
    program = broken_program(kind, tmp_path)

    def run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if argv[0] == doctor.TAILSCALE:
            return service.run_command([str(program), *argv[1:]])
        return h.run(argv)

    add_running(conn, "a", started=NOW - LIMIT * 2)
    assert_only_network_is_affected(h, conn, h.go(run=run), detail)


@pytest.mark.parametrize(
    ("error", "detail"),
    [
        (subprocess.TimeoutExpired("x", 30), "TimeoutExpired"),
        (subprocess.CalledProcessError(1, "x"), "CalledProcessError"),
        (PermissionError("private"), "PermissionError"),
    ],
)
def test_a_tailscale_failure_of_any_measured_kind_only_warns_on_network(
    h: Harness, conn: sqlite3.Connection, error: Exception, detail: str
) -> None:
    h.results["tailscale"] = error
    add_running(conn, "a", started=NOW - LIMIT * 2)
    assert_only_network_is_affected(h, conn, h.go(), detail)


def test_an_unexpected_tailscale_error_still_lets_the_stuck_check_run(
    h: Harness, conn: sqlite3.Connection
) -> None:
    """Text mode decoding raises this from run_command; it is not a command failure."""
    h.results["tailscale"] = UnicodeDecodeError("utf-8", b"x", 0, 1, "private")
    add_running(conn, "a", started=NOW - LIMIT * 2)
    report = h.go()
    assert report.errors == ("network UnicodeDecodeError",)
    assert result_of(report, "claude_auth") == (CheckLevel.OK, None)
    assert report.killed == ("a",)


def test_tailscale_is_called_by_its_absolute_path() -> None:
    assert doctor.TAILSCALE == "/usr/local/bin/tailscale"


# Alerts: only when an item got worse


@pytest.mark.parametrize(
    ("first", "second", "alerted"),
    [
        ((0, "Logged in"), (1, ""), True),  # ok -> fail
        ((1, ""), (1, ""), False),  # fail -> fail
        ((1, ""), (0, "Logged in"), False),  # fail -> ok
        ((0, "Logged in"), (0, "Logged in"), False),  # ok -> ok
    ],
)
def test_codex_alerts_follow_the_level_change(
    h: Harness,
    first: tuple[int, str],
    second: tuple[int, str],
    alerted: bool,
) -> None:
    h.results["codex"] = first
    h.go()
    h.sent.clear()
    h.results["codex"] = second
    h.go()
    assert h.events() == (["doctor_codex_auth"] if alerted else [])


@pytest.mark.parametrize(
    ("first", "second", "alerted"),
    [
        (5 * GB, 1 * GB, True),  # warn -> fail
        (50 * GB, 5 * GB, True),  # ok -> warn
        (5 * GB, 5 * GB, False),  # warn -> warn
        (1 * GB, 5 * GB, False),  # fail -> warn
        (5 * GB, 50 * GB, False),  # warn -> ok
    ],
)
def test_disk_alerts_follow_the_level_change(
    h: Harness, first: int, second: int, alerted: bool
) -> None:
    h.free = [first]
    h.go()
    h.sent.clear()
    h.free = [second]
    h.go()
    assert h.events() == (["doctor_disk"] if alerted else [])


def test_a_first_failure_is_alerted_with_the_measured_arguments(
    h: Harness, orch_home: Path
) -> None:
    h.results["codex"] = (1, "")
    h.go()
    assert h.sent == [
        {
            "event": "doctor_codex_auth",
            "settings": SETTINGS,
            "orch_home": orch_home,
            "home": orch_home.parent,
            "now": NOW,
            "target": "",
        }
    ]


def test_a_first_ok_is_not_alerted(h: Harness) -> None:
    h.go()
    assert h.sent == []


@pytest.mark.parametrize("outcome", [notify.Outcome.FAILED, notify.Outcome.SUPPRESSED])
def test_an_alert_that_did_not_go_out_is_detected_again(
    h: Harness, conn: sqlite3.Connection, outcome: notify.Outcome
) -> None:
    h.outcome = outcome
    h.results["codex"] = (1, "")
    report = h.go()
    assert result_of(report, "codex_auth") == (CheckLevel.FAIL, "exit=1")
    assert stored(conn, "codex_auth") is CheckLevel.OK
    h.sent.clear()
    h.outcome = notify.Outcome.SENT
    h.go()
    assert h.events() == ["doctor_codex_auth"]
    assert stored(conn, "codex_auth") is CheckLevel.FAIL


def test_a_suppressed_escalation_keeps_the_previous_level(
    h: Harness, conn: sqlite3.Connection
) -> None:
    """Warn then fail within 30 minutes must still alert the failure later."""
    h.free = [5 * GB]
    h.go()
    assert stored(conn, "disk") is CheckLevel.WARN
    h.free = [GB]
    h.outcome = notify.Outcome.SUPPRESSED
    h.go()
    assert stored(conn, "disk") is CheckLevel.WARN
    h.sent.clear()
    h.outcome = notify.Outcome.SENT
    h.go()
    assert h.events() == ["doctor_disk"]


@pytest.mark.parametrize(
    "outcome",
    [
        notify.Outcome.SENT,
        notify.Outcome.DISABLED,
        notify.Outcome.REFUSED_PERMISSIONS,
    ],
)
def test_other_alert_outcomes_keep_the_new_level(
    h: Harness, conn: sqlite3.Connection, outcome: notify.Outcome
) -> None:
    h.outcome = outcome
    h.results["codex"] = (1, "")
    h.go()
    assert stored(conn, "codex_auth") is CheckLevel.FAIL
    h.sent.clear()
    h.go()
    assert h.sent == []


def test_a_pause_does_not_depend_on_the_alert(
    h: Harness, conn: sqlite3.Connection
) -> None:
    h.outcome = notify.Outcome.FAILED
    h.results["codex"] = (1, "")
    h.go()
    assert pauses(conn) == ["codex_auth"]


# Pauses


@pytest.mark.parametrize(
    ("result_key", "value", "reason"),
    [
        ("claude", (1, ""), "claude_auth"),
        ("codex", (1, ""), "codex_auth"),
    ],
)
def test_an_auth_failure_pauses_and_recovery_clears_it(
    h: Harness,
    conn: sqlite3.Connection,
    result_key: str,
    value: tuple[int, str],
    reason: str,
) -> None:
    healthy = h.results[result_key]
    h.results[result_key] = value
    report = h.go()
    assert report.paused == (reason,)
    assert store.list_pauses(conn) == (
        Pause(reason=reason, since=NOW.isoformat(), note="exit=1"),
    )
    h.results[result_key] = healthy
    assert h.go().paused == ()
    assert pauses(conn) == []


def test_a_disk_failure_pauses_and_a_warning_clears_it(
    h: Harness, conn: sqlite3.Connection
) -> None:
    h.free = [GB]
    assert h.go().paused == ("disk",)
    h.free = [5 * GB]
    assert h.go().paused == ()


def test_an_auth_warning_does_not_pause(h: Harness, conn: sqlite3.Connection) -> None:
    h.results["claude"] = claude_output(
        loginExpiresAt=(NOW + timedelta(days=1)).isoformat()
    )
    h.go()
    assert pauses(conn) == []


def test_a_network_failure_does_not_pause(h: Harness, conn: sqlite3.Connection) -> None:
    h.results["tailscale"] = (1, "")
    h.go()
    assert pauses(conn) == []


def test_one_recovery_leaves_another_pause(
    h: Harness, conn: sqlite3.Connection
) -> None:
    h.results["claude"] = (1, "")
    h.results["codex"] = (1, "")
    assert h.go().paused == ("claude_auth", "codex_auth")
    h.results["codex"] = (0, "ok")
    assert h.go().paused == ("claude_auth",)


def test_a_continuing_failure_keeps_the_original_pause_time(
    h: Harness, conn: sqlite3.Connection
) -> None:
    h.results["codex"] = (1, "")
    h.go()
    h.go(now=NOW + timedelta(minutes=10))
    assert store.list_pauses(conn)[0].since == NOW.isoformat()


# Isolation


def test_one_failing_item_does_not_stop_the_others_or_change_its_records(
    h: Harness,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h.results["codex"] = (1, "")
    h.go()
    real = store.record_doctor_check

    def record(conn: sqlite3.Connection, **kwargs: object) -> CheckLevel | None:
        if kwargs["item"] == "codex_auth":
            raise sqlite3.OperationalError("private")
        return real(conn, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "record_doctor_check", record)
    h.results["codex"] = (0, "ok")
    report = h.go()
    assert report.errors == ("codex_auth OperationalError",)
    assert result_of(report, "network") == (CheckLevel.OK, None)
    assert pauses(conn) == ["codex_auth"]
    assert not conn.in_transaction


def test_a_failing_pane_check_is_reported_by_target(
    h: Harness, orch_home: Path
) -> None:
    add_target(h, "one", "two")
    record_for(orch_home, "one", "two")
    h.statuses["orch-one"] = RuntimeError("private")
    h.statuses["orch-two"] = MainPaneStatus.ALIVE
    report = h.go()
    assert report.errors == ("one/main_session RuntimeError",)
    assert check(report, "main_session", "two").level is CheckLevel.OK


def test_a_failing_disk_measurement_is_isolated(h: Harness) -> None:
    def broken(path: Path) -> int:
        raise OSError("private")

    report = h.go(free_bytes=broken)
    assert report.errors == ("disk OSError",)
    assert result_of(report, "network") == (CheckLevel.OK, None)


def test_a_failing_send_is_isolated(h: Harness, conn: sqlite3.Connection) -> None:
    def broken(event: str, **kwargs: object) -> notify.Outcome:
        raise RuntimeError("private")

    h.results["codex"] = (1, "")
    report = h.go(send=broken)
    assert report.errors == ("codex_auth RuntimeError",)
    assert result_of(report, "network") == (CheckLevel.OK, None)


def test_no_error_holds_a_message(h: Harness) -> None:
    def broken(path: Path) -> int:
        raise OSError("private detail")

    assert "private" not in str(h.go(free_bytes=broken))


# Stuck tasks


def add_running(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    started: datetime | None,
    timeout_sec: int = 2700,
    target: str = "sample",
    worktree: str | None = None,
) -> None:
    store.insert_task(
        conn,
        Task(
            id=task_id,
            target=target,
            title="T",
            grade=Grade.L2,
            lane=Lane.B,
            model="m",
            state=TaskState.RUNNING,
            timeout_sec=timeout_sec,
            worktree=worktree,
            created_at=f"2026-11-09T00:00:{len(task_id):02d}+00:00",
            updated_at="2026-11-09T00:00:00+00:00",
        ),
    )
    store.insert_run(
        conn,
        Run(
            task_id=task_id,
            attempt=1,
            lane=Lane.B,
            model="m",
            started_at=started.isoformat() if started else None,
        ),
    )


LIMIT = timedelta(seconds=2700 + 600)


def test_a_task_past_its_limit_is_killed_and_alerted(
    h: Harness, conn: sqlite3.Connection, orch_home: Path
) -> None:
    add_running(conn, "a", started=NOW - LIMIT - timedelta(seconds=1))
    report = h.go()
    assert report.killed == ("a",)
    assert h.kills == [
        (
            "a",
            {
                "paths": target_paths(orch_home, "sample"),
                "environ": {"HOME": str(orch_home.parent)},
            },
        )
    ]
    assert h.sent == [
        {
            "event": "doctor_stuck_task",
            "settings": SETTINGS,
            "orch_home": orch_home,
            "home": orch_home.parent,
            "now": NOW,
            "target": "sample",
            "task_id": "a",
            "dedup": False,
        }
    ]


def test_a_task_at_exactly_its_limit_is_not_killed(
    h: Harness, conn: sqlite3.Connection
) -> None:
    add_running(conn, "a", started=NOW - LIMIT)
    assert h.go().killed == ()
    assert h.kills == []


def test_the_limit_uses_each_tasks_own_timeout(
    h: Harness, conn: sqlite3.Connection
) -> None:
    add_running(conn, "short", started=NOW - timedelta(seconds=701), timeout_sec=100)
    add_running(conn, "longer", started=NOW - timedelta(seconds=701), timeout_sec=3600)
    assert h.go().killed == ("short",)


def add_second_run(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    attempt: int,
    started: datetime | None,
) -> None:
    """End the first run and start another, as orch retry and orch resume do."""
    first = store.latest_run(conn, task_id)
    assert first is not None
    assert first.run_id is not None
    store.finish_run(
        conn,
        first.run_id,
        ended_at=(NOW - LIMIT).isoformat(),
        exit_code=1,
        outcome=Outcome.FAILED,
    )
    store.insert_run(
        conn,
        Run(
            task_id=task_id,
            attempt=attempt,
            lane=Lane.B,
            model="m",
            started_at=started.isoformat() if started else None,
        ),
    )


@pytest.mark.parametrize("attempt", [1, 2], ids=["resume", "retry"])
def test_a_second_run_is_timed_from_its_own_start(
    h: Harness, conn: sqlite3.Connection, attempt: int
) -> None:
    add_running(conn, "a", started=NOW - LIMIT * 3)
    add_second_run(conn, "a", attempt=attempt, started=NOW - timedelta(seconds=60))
    report = h.go()
    assert report.killed == ()
    assert h.kills == []
    assert h.sent == []
    assert report.errors == ()


def test_a_second_run_at_exactly_its_limit_is_not_killed(
    h: Harness, conn: sqlite3.Connection
) -> None:
    add_running(conn, "a", started=NOW - LIMIT * 3)
    add_second_run(conn, "a", attempt=2, started=NOW - LIMIT)
    assert h.go().killed == ()
    assert h.kills == []


def test_a_second_run_past_its_own_limit_is_killed(
    h: Harness, conn: sqlite3.Connection
) -> None:
    add_running(conn, "a", started=NOW - LIMIT * 3)
    add_second_run(conn, "a", attempt=2, started=NOW - LIMIT - timedelta(seconds=1))
    assert h.go().killed == ("a",)


def test_a_second_run_without_a_start_time_does_not_fall_back_to_the_first(
    h: Harness, conn: sqlite3.Connection
) -> None:
    add_running(conn, "a", started=NOW - LIMIT * 3)
    add_second_run(conn, "a", attempt=2, started=None)
    assert h.go().killed == ()
    assert h.kills == []


def test_a_run_without_a_start_time_is_left_alone(
    h: Harness, conn: sqlite3.Connection
) -> None:
    add_running(conn, "a", started=None)
    report = h.go()
    assert h.kills == []
    assert report.errors == ()


def test_every_target_is_checked_for_stuck_tasks(
    h: Harness, conn: sqlite3.Connection
) -> None:
    add_running(conn, "a", started=NOW - LIMIT * 2, target="elsewhere")
    assert h.go().killed == ("a",)
    assert h.sent[0]["target"] == "elsewhere"


def test_each_kill_gets_its_own_alert_within_one_pass(
    h: Harness, conn: sqlite3.Connection
) -> None:
    add_running(conn, "a", started=NOW - LIMIT * 2)
    add_running(conn, "bb", started=NOW - LIMIT * 2)
    assert h.go().killed == ("a", "bb")
    assert [call["task_id"] for call in h.sent] == ["a", "bb"]
    assert all(call["dedup"] is False for call in h.sent)


def test_a_real_send_delivers_every_kill_of_one_target(
    h: Harness, conn: sqlite3.Connection, orch_home: Path
) -> None:
    """The dedup key of doctor events holds no task id: two kills must both go out."""
    config_file = config.config_path(orch_home)
    config_file.write_text("", encoding="utf-8")
    config_file.chmod(0o600)
    settings = Config(
        claude_path=CLAUDE,
        codex_path=CODEX,
        notify_method="ntfy",
        notify_topic="abcdefghijklmnopqrstuvwxyz012345",
    )
    bodies: list[bytes] = []

    def transport(url: str, body: bytes, timeout: float) -> None:
        bodies.append(body)

    add_running(conn, "a", started=NOW - LIMIT * 2)
    add_running(conn, "bb", started=NOW - LIMIT * 2)
    h.go(
        settings=settings,
        send=functools.partial(notify.send, transport=transport),
    )
    assert bodies == [
        b"orch doctor_stuck_task target=sample task=a",
        b"orch doctor_stuck_task target=sample task=bb",
    ]


def test_a_runner_that_finished_first_is_not_counted_or_alerted(
    h: Harness, conn: sqlite3.Connection
) -> None:
    add_running(conn, "a", started=NOW - LIMIT * 2)
    h.kill_results["a"] = KillReport(
        task_id="a",
        state=TaskState.JUDGING,
        outcome=Outcome.DONE,
        failure_reason=None,
        finished_by="runner",
        signalled=False,
    )
    assert h.go().killed == ()
    assert h.sent == []


def test_a_failed_kill_is_reported_and_the_next_task_continues(
    h: Harness, conn: sqlite3.Connection
) -> None:
    add_running(conn, "a", started=NOW - LIMIT * 2)
    add_running(conn, "bb", started=NOW - LIMIT * 2)
    h.kill_results["a"] = LifecycleError("private")
    report = h.go()
    assert report.errors == ("stuck_task/a LifecycleError",)
    assert report.killed == ("bb",)
    assert [call["task_id"] for call in h.sent] == ["bb"]


def test_an_unreadable_start_time_is_isolated(
    h: Harness, conn: sqlite3.Connection
) -> None:
    add_running(conn, "a", started=NOW)
    conn.execute("UPDATE runs SET started_at = 'yesterday' WHERE task_id = 'a'")
    add_running(conn, "bb", started=NOW - LIMIT * 2)
    report = h.go()
    assert report.errors == ("stuck_task/a ValueError",)
    assert report.killed == ("bb",)


def test_stuck_tasks_are_not_recorded_as_a_check(
    h: Harness, conn: sqlite3.Connection
) -> None:
    add_running(conn, "a", started=NOW - LIMIT * 2)
    h.go()
    assert "stuck_task" not in {row.item for row in store.list_doctor_checks(conn)}


def test_a_real_kill_records_the_abort_and_keeps_the_worktree(
    h: Harness, conn: sqlite3.Connection, orch_home: Path, tmp_path: Path
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    add_running(conn, "a", started=NOW - LIMIT * 2, worktree=str(worktree))
    report = h.go(
        kill=functools.partial(
            lifecycle.kill_task, settle_sec=0, grace_sec=0, poll_sec=0
        )
    )
    assert report.killed == ("a",)
    task = store.get_task(conn, "a")
    assert task is not None
    assert task.state is TaskState.JUDGING
    run = store.latest_run(conn, "a")
    assert run is not None
    assert (run.outcome, run.failure_reason) == (
        Outcome.FAILED,
        FailureReason.ABORTED,
    )
    assert worktree.is_dir()


def test_the_real_runner_and_free_bytes_defaults_are_the_documented_ones() -> None:
    signature = inspect.signature(run_doctor)
    defaults: dict[str, Callable[..., object] | int] = {
        "run": doctor.service.run_command,
        "send": notify.send,
        "disk_warn_gb": 10,
        "disk_fail_gb": 2,
    }
    for name, expected in defaults.items():
        assert signature.parameters[name].default == expected


# Alerts and writes


def arrange_main_session(h: Harness, conn: sqlite3.Connection) -> None:
    add_target(h, "one")
    record_for(h.orch_home, "one")
    h.statuses["orch-one"] = MainPaneStatus.PANE_DEAD


def arrange_result(key: str) -> Callable[[Harness, sqlite3.Connection], None]:
    def arrange(h: Harness, conn: sqlite3.Connection) -> None:
        h.results[key] = (1, "")

    return arrange


def arrange_disk(h: Harness, conn: sqlite3.Connection) -> None:
    h.free = [GB]


def arrange_stuck(h: Harness, conn: sqlite3.Connection) -> None:
    add_running(conn, "a", started=NOW - LIMIT * 2)


@pytest.mark.parametrize(
    ("event", "arrange"),
    [
        ("doctor_main_session", arrange_main_session),
        ("doctor_claude_auth", arrange_result("claude")),
        ("doctor_codex_auth", arrange_result("codex")),
        ("doctor_github_app", arrange_result("token")),
        ("doctor_disk", arrange_disk),
        ("doctor_network", arrange_result("tailscale")),
        ("doctor_stuck_task", arrange_stuck),
    ],
)
def test_no_write_lock_is_held_while_an_alert_goes_out(
    h: Harness,
    conn: sqlite3.Connection,
    orch_home: Path,
    event: str,
    arrange: Callable[[Harness, sqlite3.Connection], None],
) -> None:
    """A slow ntfy must not make another orch process fail with database is locked."""
    arrange(h, conn)
    other = sqlite3.connect(orch_home / "state.db", timeout=0, isolation_level=None)
    seen: list[tuple[str, bool, str]] = []

    def probe(sent: str, **kwargs: object) -> notify.Outcome:
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute("ROLLBACK")
            lock = "free"
        except sqlite3.OperationalError:
            lock = "locked"
        seen.append((sent, conn.in_transaction, lock))
        return notify.Outcome.SENT

    report = h.go(send=probe)
    other.close()
    assert seen == [(event, False, "free")]
    assert report.errors == ()


LATER = NOW + timedelta(minutes=10)


def row_of(conn: sqlite3.Connection, item: str) -> DoctorCheck | None:
    for row in store.list_doctor_checks(conn):
        if row.item == item:
            return row
    return None


def fail_with(error: Exception) -> Callable[..., object]:
    def broken(*args: object, **kwargs: object) -> object:
        raise error

    return broken


def test_an_alert_that_raises_changes_no_record_or_pause(
    h: Harness, conn: sqlite3.Connection
) -> None:
    h.go()
    before = row_of(conn, "codex_auth")
    h.results["codex"] = (1, "")
    report = h.go(send=fail_with(RuntimeError("private")), now=LATER)
    assert report.errors == ("codex_auth RuntimeError",)
    assert row_of(conn, "codex_auth") == before
    assert pauses(conn) == []
    assert not conn.in_transaction


def test_an_escalation_alert_that_raises_keeps_the_warning(
    h: Harness, conn: sqlite3.Connection
) -> None:
    h.free = [5 * GB]
    h.go()
    before = row_of(conn, "disk")
    h.free = [GB]
    report = h.go(send=fail_with(RuntimeError("private")), now=LATER)
    assert report.errors == ("disk RuntimeError",)
    assert row_of(conn, "disk") == before
    assert pauses(conn) == []


def test_an_alert_that_raised_is_sent_on_the_next_pass(
    h: Harness, conn: sqlite3.Connection
) -> None:
    h.go()
    h.results["codex"] = (1, "")
    h.go(send=fail_with(RuntimeError("private")), now=LATER)
    h.go(now=LATER)
    assert h.events() == ["doctor_codex_auth"]
    assert stored(conn, "codex_auth") is CheckLevel.FAIL
    assert pauses(conn) == ["codex_auth"]


def test_a_failed_pause_write_leaves_the_record_as_it_was(
    h: Harness, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    h.go()
    before = row_of(conn, "codex_auth")
    h.results["codex"] = (1, "")
    with monkeypatch.context() as patch:
        patch.setattr(
            store, "add_pause", fail_with(sqlite3.OperationalError("private"))
        )
        report = h.go(now=LATER)
    assert report.errors == ("codex_auth OperationalError",)
    assert row_of(conn, "codex_auth") == before
    assert pauses(conn) == []
    assert not conn.in_transaction
    assert result_of(report, "network") == (CheckLevel.OK, None)


def test_a_failed_pause_removal_leaves_the_failure_recorded(
    h: Harness, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    h.results["codex"] = (1, "")
    h.go()
    before = row_of(conn, "codex_auth")
    h.results["codex"] = (0, "ok")
    real = store.remove_pause

    def remove(conn: sqlite3.Connection, reason: str) -> bool:
        if reason == "codex_auth":
            raise sqlite3.OperationalError("private")
        return real(conn, reason)

    with monkeypatch.context() as patch:
        patch.setattr(store, "remove_pause", remove)
        report = h.go(now=LATER)
    assert report.errors == ("codex_auth OperationalError",)
    assert row_of(conn, "codex_auth") == before
    assert pauses(conn) == ["codex_auth"]
    assert not conn.in_transaction


def test_a_failed_record_write_adds_no_pause(
    h: Harness, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    h.go()
    before = row_of(conn, "codex_auth")
    real = store.record_doctor_check

    def record(conn: sqlite3.Connection, **kwargs: object) -> CheckLevel | None:
        if kwargs["item"] == "codex_auth":
            raise sqlite3.OperationalError("private")
        return real(conn, **kwargs)  # type: ignore[arg-type]

    h.results["codex"] = (1, "")
    with monkeypatch.context() as patch:
        patch.setattr(store, "record_doctor_check", record)
        report = h.go(now=LATER)
    assert report.errors == ("codex_auth OperationalError",)
    assert row_of(conn, "codex_auth") == before
    assert pauses(conn) == []
    assert not conn.in_transaction


def test_a_failed_write_after_a_suppressed_alert_changes_nothing(
    h: Harness, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    h.go()
    before = row_of(conn, "codex_auth")
    h.results["codex"] = (1, "")
    h.outcome = notify.Outcome.SUPPRESSED
    with monkeypatch.context() as patch:
        patch.setattr(
            store, "add_pause", fail_with(sqlite3.OperationalError("private"))
        )
        h.go(now=LATER)
    assert row_of(conn, "codex_auth") == before
    assert pauses(conn) == []


def test_a_pass_after_a_failed_write_finishes_the_job(
    h: Harness, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    h.go()
    h.results["codex"] = (1, "")
    with monkeypatch.context() as patch:
        patch.setattr(
            store, "add_pause", fail_with(sqlite3.OperationalError("private"))
        )
        h.go(now=LATER)
    report = h.go(now=LATER)
    assert report.errors == ()
    assert stored(conn, "codex_auth") is CheckLevel.FAIL
    assert pauses(conn) == ["codex_auth"]


def test_the_configured_tailscale_path_replaces_the_constant(h: Harness) -> None:
    custom = "/opt/bin/tailscale"
    seen: list[list[str]] = []

    def run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if argv[0] == custom:
            seen.append(list(argv))
            return subprocess.CompletedProcess(list(argv), 0, TAILSCALE_OK, "")
        return h.run(argv)

    settings = Config(
        claude_path=CLAUDE,
        codex_path=CODEX,
        retention_days=30,
        tailscale_path=Path(custom),
    )
    report = h.go(settings=settings, run=run)
    assert seen == [[custom, "status", "--json"]]
    assert result_of(report, "network") == (CheckLevel.OK, None)
    assert not any(argv[0] == doctor.TAILSCALE for argv in h.commands)
