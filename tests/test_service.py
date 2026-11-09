"""launchctl exit codes in these tests come from a measurement on macOS 15.8.1."""

import plistlib
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from orch import service
from orch.service import (
    LABELS,
    RestoreReport,
    ServiceReport,
    agent_path,
    agent_plist,
    install,
    restore,
    run_command,
    uninstall,
)
from orch.terminal import SessionProbe

UID = 501
ORCH = Path("/home/u/.local/bin/orch")
TMUX = Path("/opt/local/bin/tmux")
HUB = Path("/home/u/hub")
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


class FakeRun:
    """Record argv. codes maps (step, label) or step to an exit code or exception."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.codes: dict[object, int | Exception] = {}
        self.plist_at_bootstrap: dict[str, bytes] = {}

    @staticmethod
    def step(argv: list[str]) -> tuple[str, str]:
        if argv[0] == service.PLUTIL:
            return "lint", Path(argv[-1]).name.removesuffix(".plist")
        if argv[0] == service.LAUNCHCTL and argv[1] == "bootout":
            return "bootout", argv[2].rsplit("/", 1)[1]
        if argv[0] == service.LAUNCHCTL and argv[1] == "bootstrap":
            return "bootstrap", Path(argv[3]).name.removesuffix(".plist")
        return argv[1], argv[2] if len(argv) > 2 else ""

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        self.calls.append(argv)
        step, label = self.step(argv)
        if step == "bootstrap":
            self.plist_at_bootstrap[label] = Path(argv[3]).read_bytes()
        code = self.codes.get((step, label), self.codes.get(step, 0))
        if isinstance(code, Exception):
            raise code
        return subprocess.CompletedProcess(argv, code, "", "private stderr")

    def steps(self) -> list[tuple[str, str]]:
        return [self.step(argv) for argv in self.calls]


@pytest.fixture
def fake() -> FakeRun:
    return FakeRun()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


@pytest.fixture
def orch_home(home: Path) -> Path:
    return home / ".orch"


def agents(home: Path) -> Path:
    return home / "Library" / "LaunchAgents"


def run_install(
    fake: FakeRun, home: Path, orch_home: Path, **kwargs: object
) -> ServiceReport:
    return install(
        orch=ORCH,
        tmux=TMUX,
        hub=HUB,
        home=home,
        orch_home=orch_home,
        uid=UID,
        run=fake,
        **kwargs,  # type: ignore[arg-type]
    )


# agent_path


def test_the_path_holds_orch_tmux_and_the_system_directories() -> None:
    assert (
        agent_path(orch=ORCH, tmux=TMUX)
        == f"/home/u/.local/bin:/opt/local/bin:{SYSTEM_PATH}"
    )


def test_the_path_keeps_the_first_of_a_repeated_directory() -> None:
    same = Path("/usr/bin/tool")
    assert agent_path(orch=same, tmux=same) == SYSTEM_PATH


def test_the_path_does_not_resolve_a_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real" / "orch"
    real.parent.mkdir()
    real.write_text("", encoding="utf-8")
    links = tmp_path / "links"
    links.mkdir()
    (links / "orch").symlink_to(real)
    assert agent_path(orch=links / "orch", tmux=TMUX).startswith(f"{links}:")


# agent_plist


def plist(label: str, **kwargs: object) -> dict[str, object]:
    return agent_plist(
        label,
        orch=ORCH,
        tmux=TMUX,
        hub=HUB,
        orch_home=Path("/home/u/.orch"),
        **kwargs,  # type: ignore[arg-type]
    )


def test_the_labels_match_the_design() -> None:
    assert LABELS == ("orch.session", "orch.doctor", "orch.gc")


@pytest.mark.parametrize(
    ("label", "arguments"),
    [
        ("orch.session", ["service", "restore"]),
        ("orch.doctor", ["doctor"]),
        ("orch.gc", ["gc"]),
    ],
)
def test_each_agent_runs_the_absolute_orch(label: str, arguments: list[str]) -> None:
    value = plist(label)
    assert value["Label"] == label
    assert value["ProgramArguments"] == [str(ORCH), *arguments]


def test_each_agent_logs_to_its_own_file() -> None:
    for label in LABELS:
        value = plist(label)
        log = f"/home/u/.orch/service/{label}.log"
        assert value["StandardOutPath"] == log
        assert value["StandardErrorPath"] == log


def test_the_environment_holds_only_the_hub_and_path() -> None:
    for label in LABELS:
        assert plist(label)["EnvironmentVariables"] == {
            "ORCH_HUB": str(HUB),
            "PATH": agent_path(orch=ORCH, tmux=TMUX),
        }


def test_a_custom_orch_home_is_passed_on() -> None:
    environment = plist("orch.gc", custom_orch_home=True)["EnvironmentVariables"]
    assert environment == {
        "ORCH_HUB": str(HUB),
        "ORCH_HOME": "/home/u/.orch",
        "PATH": agent_path(orch=ORCH, tmux=TMUX),
    }


def schedule_keys(value: dict[str, object]) -> dict[str, object]:
    common = {
        "Label",
        "ProgramArguments",
        "EnvironmentVariables",
        "StandardOutPath",
        "StandardErrorPath",
    }
    return {key: item for key, item in value.items() if key not in common}


def test_the_session_agent_runs_at_load_and_leaves_its_processes() -> None:
    assert schedule_keys(plist("orch.session")) == {
        "RunAtLoad": True,
        "AbandonProcessGroup": True,
    }


def test_the_doctor_agent_runs_every_ten_minutes_and_not_at_load() -> None:
    assert schedule_keys(plist("orch.doctor")) == {"StartInterval": 600}


def test_the_gc_agent_runs_once_a_day_and_not_at_load() -> None:
    assert schedule_keys(plist("orch.gc")) == {
        "StartCalendarInterval": {"Hour": 4, "Minute": 0}
    }


def test_an_unknown_label_is_refused() -> None:
    with pytest.raises(ValueError):
        plist("orch.other")


# install


def test_install_loads_every_agent_in_order(
    fake: FakeRun, home: Path, orch_home: Path
) -> None:
    report = run_install(fake, home, orch_home)
    assert report == ServiceReport(ok=True, done=LABELS)
    assert fake.steps() == [
        (step, label) for label in LABELS for step in ("bootout", "lint", "bootstrap")
    ]


def test_install_uses_the_measured_commands(
    fake: FakeRun, home: Path, orch_home: Path
) -> None:
    run_install(fake, home, orch_home)
    path = agents(home) / "orch.session.plist"
    assert fake.calls[:3] == [
        ["/bin/launchctl", "bootout", f"gui/{UID}/orch.session"],
        ["/usr/bin/plutil", "-lint", str(path)],
        ["/bin/launchctl", "bootstrap", f"gui/{UID}", str(path)],
    ]


def test_install_never_uses_kickstart_or_asuser(
    fake: FakeRun, home: Path, orch_home: Path
) -> None:
    run_install(fake, home, orch_home)
    flat = {word for argv in fake.calls for word in argv}
    assert "kickstart" not in flat
    assert "asuser" not in flat


def test_install_creates_the_missing_directories(
    fake: FakeRun, home: Path, orch_home: Path
) -> None:
    assert not home.exists()
    run_install(fake, home, orch_home)
    assert agents(home).is_dir()
    assert (orch_home / "service").is_dir()


def test_install_writes_the_plist_before_bootstrap(
    fake: FakeRun, home: Path, orch_home: Path
) -> None:
    run_install(fake, home, orch_home)
    for label in LABELS:
        expected = agent_plist(
            label, orch=ORCH, tmux=TMUX, hub=HUB, orch_home=orch_home
        )
        assert plistlib.loads(fake.plist_at_bootstrap[label]) == expected
        assert (
            plistlib.loads((agents(home) / f"{label}.plist").read_bytes()) == expected
        )


def test_install_leaves_no_temporary_file(
    fake: FakeRun, home: Path, orch_home: Path
) -> None:
    run_install(fake, home, orch_home)
    assert sorted(path.name for path in agents(home).iterdir()) == sorted(
        f"{label}.plist" for label in LABELS
    )


def test_install_passes_a_custom_orch_home(
    fake: FakeRun, home: Path, orch_home: Path
) -> None:
    run_install(fake, home, orch_home, custom_orch_home=True)
    value = plistlib.loads((agents(home) / "orch.gc.plist").read_bytes())
    assert value["EnvironmentVariables"]["ORCH_HOME"] == str(orch_home)


def test_a_label_that_is_not_loaded_is_not_a_failure(
    fake: FakeRun, home: Path, orch_home: Path
) -> None:
    fake.codes["bootout"] = 3  # measured: No such process
    assert run_install(fake, home, orch_home).ok is True


def test_installing_again_unloads_first_and_replaces_the_plist(
    fake: FakeRun, home: Path, orch_home: Path
) -> None:
    run_install(fake, home, orch_home)
    fake.calls.clear()
    assert run_install(fake, home, orch_home).ok is True
    assert fake.steps()[0] == ("bootout", "orch.session")


def test_an_unexpected_bootout_failure_stops_before_writing(
    fake: FakeRun, home: Path, orch_home: Path
) -> None:
    fake.codes[("bootout", "orch.doctor")] = 5
    report = run_install(fake, home, orch_home)
    assert report == ServiceReport(
        ok=False, done=("orch.session",), failed=("orch.doctor bootout exit=5",)
    )
    assert not (agents(home) / "orch.doctor.plist").exists()
    assert fake.steps()[-1] == ("bootout", "orch.doctor")


def test_a_failed_lint_removes_the_plist_and_skips_bootstrap(
    fake: FakeRun, home: Path, orch_home: Path
) -> None:
    fake.codes[("lint", "orch.session")] = 1
    report = run_install(fake, home, orch_home)
    assert report == ServiceReport(ok=False, failed=("orch.session lint exit=1",))
    assert not (agents(home) / "orch.session.plist").exists()
    assert ("bootstrap", "orch.session") not in fake.steps()


def test_a_failed_bootstrap_stops_the_install(
    fake: FakeRun, home: Path, orch_home: Path
) -> None:
    fake.codes[("bootstrap", "orch.doctor")] = 5
    report = run_install(fake, home, orch_home)
    assert report == ServiceReport(
        ok=False, done=("orch.session",), failed=("orch.doctor bootstrap exit=5",)
    )
    assert ("bootout", "orch.gc") not in fake.steps()


@pytest.mark.parametrize(
    ("step", "error", "name"),
    [
        ("bootout", OSError("private"), "OSError"),
        ("lint", subprocess.TimeoutExpired("x", 30), "TimeoutExpired"),
        ("bootstrap", OSError("private"), "OSError"),
    ],
)
def test_a_command_that_cannot_run_is_reported_by_its_exception_name(
    fake: FakeRun,
    home: Path,
    orch_home: Path,
    step: str,
    error: Exception,
    name: str,
) -> None:
    fake.codes[step] = error
    report = run_install(fake, home, orch_home)
    assert report == ServiceReport(ok=False, failed=(f"orch.session {step} {name}",))


def test_the_report_holds_no_command_output(
    fake: FakeRun, home: Path, orch_home: Path
) -> None:
    fake.codes["bootstrap"] = 5
    assert "private" not in str(run_install(fake, home, orch_home))


# uninstall


def test_uninstall_unloads_and_removes_every_agent(
    fake: FakeRun, home: Path, orch_home: Path
) -> None:
    run_install(fake, home, orch_home)
    fake.calls.clear()
    assert uninstall(home=home, uid=UID, run=fake) == ServiceReport(
        ok=True, done=LABELS
    )
    assert fake.calls == [
        ["/bin/launchctl", "bootout", f"gui/{UID}/{label}"] for label in LABELS
    ]
    assert list(agents(home).iterdir()) == []


def test_uninstall_of_nothing_is_fine(fake: FakeRun, home: Path) -> None:
    fake.codes["bootout"] = 3  # measured: not loaded
    report = uninstall(home=home, uid=UID, run=fake)
    assert report == ServiceReport(ok=True, done=LABELS)


def test_uninstall_keeps_a_plist_it_could_not_unload_and_continues(
    fake: FakeRun, home: Path, orch_home: Path
) -> None:
    run_install(fake, home, orch_home)
    fake.codes[("bootout", "orch.session")] = 5
    report = uninstall(home=home, uid=UID, run=fake)
    assert report == ServiceReport(
        ok=False,
        done=("orch.doctor", "orch.gc"),
        failed=("orch.session bootout exit=5",),
    )
    assert (agents(home) / "orch.session.plist").exists()
    assert not (agents(home) / "orch.gc.plist").exists()


def test_uninstall_leaves_other_files(
    fake: FakeRun, home: Path, orch_home: Path
) -> None:
    run_install(fake, home, orch_home)
    other = agents(home) / "com.example.other.plist"
    other.write_text("x", encoding="utf-8")
    log = orch_home / "service" / "orch.gc.log"
    log.write_text("x", encoding="utf-8")
    uninstall(home=home, uid=UID, run=fake)
    assert other.exists()
    assert log.exists()


def test_uninstall_reports_a_command_that_cannot_run(fake: FakeRun, home: Path) -> None:
    fake.codes[("bootout", "orch.gc")] = OSError("private")
    report = uninstall(home=home, uid=UID, run=fake)
    assert report.failed == ("orch.gc bootout OSError",)
    assert report.ok is False


# restore


class FakeTerminal:
    def __init__(self, probes: dict[str, SessionProbe | Exception]) -> None:
        self.probes = probes
        self.seen: list[tuple[str, Path]] = []

    def __call__(self, session: str, record: Path) -> FakeTerminal:
        self.seen.append((session, record))
        self.current = session
        return self

    def probe_session(self) -> SessionProbe:
        value = self.probes[self.current]
        if isinstance(value, Exception):
            raise value
        return value


def record_for(orch_home: Path, *names: str) -> None:
    for name in names:
        path = orch_home / name / "terminal.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")


def run_restore(
    fake: FakeRun,
    orch_home: Path,
    probes: dict[str, SessionProbe | Exception],
    targets: Sequence[str],
) -> tuple[RestoreReport, FakeTerminal]:
    terminal = FakeTerminal(probes)
    report = restore(
        targets,
        orch=ORCH,
        orch_home=orch_home,
        run=fake,
        terminal=terminal,  # type: ignore[arg-type]
    )
    return report, terminal


def test_restore_opens_only_a_target_whose_session_is_absent(
    fake: FakeRun, orch_home: Path
) -> None:
    record_for(orch_home, "one")
    report, terminal = run_restore(
        fake, orch_home, {"orch-one": SessionProbe.ABSENT}, ["one"]
    )
    assert report == RestoreReport(opened=("one",))
    assert fake.calls == [[str(ORCH), "open", "one", "--no-attach"]]
    assert terminal.seen == [("orch-one", orch_home / "one" / "terminal.json")]


def test_restore_never_opens_when_the_session_exists(
    fake: FakeRun, orch_home: Path
) -> None:
    record_for(orch_home, "one")
    report, _ = run_restore(
        fake, orch_home, {"orch-one": SessionProbe.PRESENT}, ["one"]
    )
    assert report == RestoreReport(already_open=("one",))
    assert fake.calls == []


def test_restore_never_opens_when_tmux_cannot_be_asked(
    fake: FakeRun, orch_home: Path
) -> None:
    """A failed tmux run must not look like a missing session (measured: exit 127)."""
    record_for(orch_home, "one")
    report, _ = run_restore(
        fake, orch_home, {"orch-one": SessionProbe.UNAVAILABLE}, ["one"]
    )
    assert report == RestoreReport(failed=("one unavailable",))
    assert fake.calls == []


def test_restore_skips_a_target_without_a_record(
    fake: FakeRun, orch_home: Path
) -> None:
    report, terminal = run_restore(fake, orch_home, {}, ["one"])
    assert report == RestoreReport()
    assert fake.calls == []
    assert terminal.seen == []


def test_restore_handles_targets_in_name_order_and_continues_after_a_failure(
    fake: FakeRun, orch_home: Path
) -> None:
    record_for(orch_home, "a", "b", "c", "d")
    fake.codes[("open", "a")] = 70
    fake.codes[("open", "b")] = OSError("private")
    report, _ = run_restore(
        fake,
        orch_home,
        {
            "orch-a": SessionProbe.ABSENT,
            "orch-b": SessionProbe.ABSENT,
            "orch-c": SessionProbe.PRESENT,
            "orch-d": SessionProbe.ABSENT,
        },
        ["d", "c", "b", "a"],
    )
    assert report == RestoreReport(
        opened=("d",),
        already_open=("c",),
        failed=("a open exit=70", "b open OSError"),
    )


# run_command


def test_run_command_uses_an_argument_list_and_a_time_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_run(argv: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen["argv"] = argv
        seen.update(kwargs)
        return subprocess.CompletedProcess(["x"], 0, "", "")

    monkeypatch.setattr(service.subprocess, "run", fake_run)
    run_command(("/bin/launchctl", "print"))
    assert seen["argv"] == ["/bin/launchctl", "print"]
    assert seen.get("shell", False) is False
    assert seen["capture_output"] is True
    assert seen["text"] is True
    assert seen["check"] is False
    assert seen["timeout"] == 30
