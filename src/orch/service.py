"""launchd agents: install, uninstall and restore of missing main sessions."""

import os
import plistlib
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from orch.terminal import SessionProbe, TmuxTerminal

LABELS: Final = ("orch.session", "orch.doctor", "orch.gc")
LAUNCHCTL: Final = "/bin/launchctl"
PLUTIL: Final = "/usr/bin/plutil"

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
TerminalFactory = Callable[[str, Path], TmuxTerminal]


@dataclass(frozen=True, slots=True, kw_only=True)
class ServiceReport:
    """Labels handled by install or uninstall. ok is true when nothing failed."""

    ok: bool
    done: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class RestoreReport:
    """What one restore pass did for each target that has a terminal record."""

    opened: tuple[str, ...] = ()
    already_open: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()


def run_command(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv), capture_output=True, text=True, check=False, timeout=30
    )


def agent_path(*, orch: Path, tmux: Path) -> str:
    directories = [
        str(orch.parent),
        str(tmux.parent),
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    return ":".join(dict.fromkeys(directories))


def agent_plist(
    label: str,
    *,
    orch: Path,
    tmux: Path,
    hub: Path,
    orch_home: Path,
    custom_orch_home: bool = False,
) -> dict[str, object]:
    match label:
        case "orch.session":
            arguments = [str(orch), "service", "restore"]
            schedule = {"RunAtLoad": True, "AbandonProcessGroup": True}
        case "orch.doctor":
            arguments = [str(orch), "doctor"]
            schedule = {"StartInterval": 600}
        case "orch.gc":
            arguments = [str(orch), "gc"]
            schedule = {"StartCalendarInterval": {"Hour": 4, "Minute": 0}}
        case _:
            raise ValueError(f"Unknown agent label: {label}")
    environment = {"ORCH_HUB": str(hub), "PATH": agent_path(orch=orch, tmux=tmux)}
    if custom_orch_home:
        environment["ORCH_HOME"] = str(orch_home)
    log = str(orch_home / "service" / f"{label}.log")
    return {
        "Label": label,
        "ProgramArguments": arguments,
        "EnvironmentVariables": environment,
        "StandardOutPath": log,
        "StandardErrorPath": log,
        **schedule,
    }


def _command_failure(
    argv: Sequence[str], *, run: Runner, accepted: tuple[int, ...] = (0,)
) -> str | None:
    """Describe a command failure without including its output or exception text."""
    try:
        result = run(argv)
    except (OSError, subprocess.SubprocessError) as exc:
        return type(exc).__name__
    if result.returncode not in accepted:
        return f"exit={result.returncode}"
    return None


def _write_plist(path: Path, value: dict[str, object]) -> None:
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = stream.name
            plistlib.dump(value, stream)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def install(
    *,
    orch: Path,
    tmux: Path,
    hub: Path,
    home: Path,
    orch_home: Path,
    uid: int,
    custom_orch_home: bool = False,
    run: Runner = run_command,
) -> ServiceReport:
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    (orch_home / "service").mkdir(parents=True, exist_ok=True)
    done: list[str] = []
    for label in LABELS:
        failure = _command_failure(
            [LAUNCHCTL, "bootout", f"gui/{uid}/{label}"], run=run, accepted=(0, 3)
        )
        if failure is not None:
            return ServiceReport(
                ok=False, done=tuple(done), failed=(f"{label} bootout {failure}",)
            )
        path = agents / f"{label}.plist"
        _write_plist(
            path,
            agent_plist(
                label,
                orch=orch,
                tmux=tmux,
                hub=hub,
                orch_home=orch_home,
                custom_orch_home=custom_orch_home,
            ),
        )
        failure = _command_failure([PLUTIL, "-lint", str(path)], run=run)
        if failure is not None:
            path.unlink(missing_ok=True)
            return ServiceReport(
                ok=False, done=tuple(done), failed=(f"{label} lint {failure}",)
            )
        failure = _command_failure(
            [LAUNCHCTL, "bootstrap", f"gui/{uid}", str(path)], run=run
        )
        if failure is not None:
            return ServiceReport(
                ok=False, done=tuple(done), failed=(f"{label} bootstrap {failure}",)
            )
        done.append(label)
    return ServiceReport(ok=True, done=tuple(done))


def uninstall(*, home: Path, uid: int, run: Runner = run_command) -> ServiceReport:
    agents = home / "Library" / "LaunchAgents"
    done: list[str] = []
    failed: list[str] = []
    for label in LABELS:
        failure = _command_failure(
            [LAUNCHCTL, "bootout", f"gui/{uid}/{label}"], run=run, accepted=(0, 3)
        )
        if failure is not None:
            failed.append(f"{label} bootout {failure}")
            continue
        (agents / f"{label}.plist").unlink(missing_ok=True)
        done.append(label)
    return ServiceReport(ok=not failed, done=tuple(done), failed=tuple(failed))


def restore(
    targets: Iterable[str],
    *,
    orch: Path,
    orch_home: Path,
    run: Runner = run_command,
    terminal: TerminalFactory = TmuxTerminal,
) -> RestoreReport:
    opened: list[str] = []
    already_open: list[str] = []
    failed: list[str] = []
    for target in sorted(targets):
        record = orch_home / target / "terminal.json"
        if not record.exists():
            continue
        probe = terminal(f"orch-{target}", record).probe_session()
        if probe is SessionProbe.PRESENT:
            already_open.append(target)
        elif probe is SessionProbe.UNAVAILABLE:
            failed.append(f"{target} unavailable")
        elif probe is SessionProbe.ABSENT:
            failure = _command_failure(
                [str(orch), "open", target, "--no-attach"], run=run
            )
            if failure is None:
                opened.append(target)
            else:
                failed.append(f"{target} open {failure}")
    return RestoreReport(
        opened=tuple(opened), already_open=tuple(already_open), failed=tuple(failed)
    )
