"""tmux backend. Every call goes through an injected runner, never a shell."""

import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path

from orch.terminal.base import (
    PaneRecord,
    TerminalError,
    _record_lock,
    _valid_pane,
    load_record,
    save_record,
)

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def run_tmux(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv), capture_output=True, text=True, check=False, timeout=10
    )


class TmuxTerminal:
    def __init__(
        self,
        session: str,
        record: Path,
        *,
        runner: Runner = run_tmux,
        tmux: str = "tmux",
    ) -> None:
        self.session = session
        self.record = record
        self.runner = runner
        self.tmux = tmux

    def _run(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        try:
            result = self.runner([self.tmux, *args])
        except (OSError, subprocess.SubprocessError) as exc:
            raise TerminalError(f"tmux call failed: {exc}") from exc
        if result.returncode != 0:
            raise TerminalError(f"tmux {args[0]} failed: {result.stderr}")
        return result

    def _new_pane(self, args: Sequence[str]) -> str:
        result = self._run(args)
        lines = result.stdout.splitlines()
        if len(lines) != 1 or not _valid_pane(lines[0]):
            raise TerminalError(f"tmux returned an invalid pane id: {result.stderr}")
        return lines[0]

    def _load(self) -> PaneRecord:
        record = load_record(self.record)
        if record is None or record.session != self.session:
            raise TerminalError("No terminal record for this session")
        return record

    def _require_created(self, record: PaneRecord, pane: str) -> None:
        if pane == record.main_pane or pane not in record.created:
            raise TerminalError("Pane is not an owned non-main pane")

    def has_session(self) -> bool:
        try:
            return (
                self.runner(
                    [self.tmux, "has-session", "-t", f"={self.session}"]
                ).returncode
                == 0
            )
        except OSError, subprocess.SubprocessError:
            return False

    def open_session(
        self, *, cwd: Path, env: Mapping[str, str], command: Sequence[str]
    ) -> str:
        if not command:
            raise TerminalError("Session command must not be empty")
        args = ["new-session", "-d", "-s", self.session, "-c", str(cwd)]
        for key, value in env.items():
            args.extend(["-e", f"{key}={value}"])
        args.extend(["-P", "-F", "#{pane_id}", *command])
        with _record_lock(self.record):
            pane = self._new_pane(args)
            save_record(self.record, PaneRecord(self.session, pane))
        return pane

    def attach_argv(self, env: Mapping[str, str]) -> list[str]:
        action = "switch-client" if "TMUX" in env else "attach-session"
        return [self.tmux, action, "-t", f"={self.session}"]

    def terminal_split(
        self,
        command: Sequence[str],
        *,
        target: str | None = None,
        vertical: bool = False,
        percent: int | None = None,
    ) -> str:
        with _record_lock(self.record):
            record = self._load()
            destination = record.main_pane if target is None else target
            if destination != record.main_pane and destination not in record.created:
                raise TerminalError("Split target is not a recorded pane")
            args = ["split-window", "-d", "-t", destination, "-v" if vertical else "-h"]
            if percent is not None:
                args.extend(["-l", f"{percent}%"])
            args.extend(["-P", "-F", "#{pane_id}", *command])
            pane = self._new_pane(args)
            save_record(self.record, replace(record, created=(*record.created, pane)))
        return pane

    def terminal_open_tab(self, command: Sequence[str], *, name: str) -> str:
        with _record_lock(self.record):
            record = self._load()
            pane = self._new_pane(
                [
                    "new-window",
                    "-d",
                    "-t",
                    f"={self.session}:",
                    "-n",
                    name,
                    "-P",
                    "-F",
                    "#{pane_id}",
                    *command,
                ]
            )
            save_record(self.record, replace(record, created=(*record.created, pane)))
        return pane

    def terminal_send(self, pane: str, text: str) -> None:
        with _record_lock(self.record):
            record = self._load()
            self._require_created(record, pane)
            self._run(["send-keys", "-t", pane, "-l", text])
            self._run(["send-keys", "-t", pane, "Enter"])

    def close_pane(self, pane: str) -> None:
        with _record_lock(self.record):
            record = self._load()
            self._require_created(record, pane)
            self._run(["kill-pane", "-t", pane])
            save_record(
                self.record,
                replace(record, created=tuple(p for p in record.created if p != pane)),
            )

    def live_panes(self) -> frozenset[str]:
        result = self._run(
            ["list-panes", "-s", "-t", f"={self.session}", "-F", "#{pane_id}"]
        )
        return frozenset(result.stdout.splitlines())
