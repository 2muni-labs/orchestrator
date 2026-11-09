import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from orch.config import Target
from orch.launcher import build_plan
from orch.terminal import (
    MainPaneStatus,
    PaneRecord,
    SessionProbe,
    TerminalError,
    TmuxTerminal,
    load_record,
    record_path,
    save_record,
)

SESSION = "orch-orchestrator"


class FakeTmux:
    """Record tmux argv and answer new panes with increasing %<n> ids."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.next_id = 1
        self.fail: dict[str, tuple[int, str]] = {}
        self.stdout: dict[str, str] = {}

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        self.calls.append(argv)
        command = argv[1]
        if command in self.fail:
            code, err = self.fail[command]
            return subprocess.CompletedProcess(argv, code, "", err)
        if command in self.stdout:
            return subprocess.CompletedProcess(argv, 0, self.stdout[command], "")
        if "-P" in argv:
            pane = f"%{self.next_id}"
            self.next_id += 1
            return subprocess.CompletedProcess(argv, 0, pane + "\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture
def fake() -> FakeTmux:
    return FakeTmux()


@pytest.fixture
def record(tmp_path: Path) -> Path:
    return tmp_path / "orchestrator" / "terminal.json"


def opened(fake: FakeTmux, record: Path) -> TmuxTerminal:
    terminal = TmuxTerminal(SESSION, record, runner=fake, tmux="/opt/tmux")
    terminal.open_session(cwd=Path("/repo"), env={}, command=["claude"])
    fake.calls.clear()
    return terminal


# record file


def test_record_path_is_terminal_json_in_the_target_root(tmp_path: Path) -> None:
    assert record_path(tmp_path / "t") == tmp_path / "t" / "terminal.json"


def test_load_record_returns_none_when_missing(record: Path) -> None:
    assert load_record(record) is None


def test_save_and_load_round_trip(record: Path) -> None:
    value = PaneRecord(session=SESSION, main_pane="%0", created=("%3", "%7"))
    save_record(record, value)
    assert load_record(record) == value
    assert json.loads(record.read_text()) == {
        "session": SESSION,
        "main_pane": "%0",
        "created": ["%3", "%7"],
    }


def test_save_leaves_no_temporary_file(record: Path) -> None:
    save_record(record, PaneRecord(session=SESSION, main_pane="%0"))
    assert sorted(
        p.name for p in record.parent.iterdir() if p.name != "terminal.json.lock"
    ) == ["terminal.json"]


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        '{"session": "s", "created": []}',
        '{"session": "s", "main_pane": "%0"}',
        '{"session": 1, "main_pane": "%0", "created": []}',
        '{"session": "s", "main_pane": "0", "created": []}',
        '{"session": "s", "main_pane": "%0", "created": ["@1"]}',
        '{"session": "s", "main_pane": "%0", "created": "%1"}',
    ],
)
def test_load_record_rejects_a_malformed_file(record: Path, text: str) -> None:
    record.parent.mkdir(parents=True)
    record.write_text(text)
    with pytest.raises(TerminalError):
        load_record(record)


# session


def test_has_session_uses_an_exact_target(fake: FakeTmux, record: Path) -> None:
    terminal = TmuxTerminal(SESSION, record, runner=fake, tmux="/opt/tmux")
    assert terminal.has_session() is True
    assert fake.calls == [["/opt/tmux", "has-session", "-t", f"={SESSION}"]]


def test_has_session_is_false_on_a_nonzero_exit(fake: FakeTmux, record: Path) -> None:
    fake.fail["has-session"] = (1, "can't find session")
    terminal = TmuxTerminal(SESSION, record, runner=fake)
    assert terminal.has_session() is False


def test_open_session_records_the_main_pane(fake: FakeTmux, record: Path) -> None:
    terminal = TmuxTerminal(SESSION, record, runner=fake, tmux="/opt/tmux")
    pane = terminal.open_session(
        cwd=Path("/repo"),
        env={"ORCH_TARGET": "orchestrator", "ORCH_HUB": "/hub"},
        command=["/bin/claude", "--model", "m"],
    )
    assert pane == "%1"
    assert fake.calls == [
        [
            "/opt/tmux",
            "new-session",
            "-d",
            "-s",
            SESSION,
            "-c",
            "/repo",
            "-e",
            "ORCH_TARGET=orchestrator",
            "-e",
            "ORCH_HUB=/hub",
            "-P",
            "-F",
            "#{pane_id}",
            "/bin/claude",
            "--model",
            "m",
        ]
    ]
    assert load_record(record) == PaneRecord(session=SESSION, main_pane="%1")


def test_open_session_replaces_an_old_record(fake: FakeTmux, record: Path) -> None:
    save_record(record, PaneRecord(session=SESSION, main_pane="%9", created=("%10",)))
    TmuxTerminal(SESSION, record, runner=fake).open_session(
        cwd=Path("/repo"), env={}, command=["claude"]
    )
    assert load_record(record) == PaneRecord(session=SESSION, main_pane="%1")


def test_open_session_refuses_an_empty_command(fake: FakeTmux, record: Path) -> None:
    with pytest.raises(TerminalError):
        TmuxTerminal(SESSION, record, runner=fake).open_session(
            cwd=Path("/repo"), env={}, command=[]
        )
    assert fake.calls == []


def test_open_session_failure_raises_with_stderr(fake: FakeTmux, record: Path) -> None:
    fake.fail["new-session"] = (1, "duplicate session: orch-orchestrator")
    with pytest.raises(TerminalError, match="duplicate session"):
        TmuxTerminal(SESSION, record, runner=fake).open_session(
            cwd=Path("/repo"), env={}, command=["claude"]
        )
    assert load_record(record) is None


@pytest.mark.parametrize("output", ["", "oops\n", "%1\n%2\n", "1\n"])
def test_a_bad_pane_id_output_raises(fake: FakeTmux, record: Path, output: str) -> None:
    fake.stdout["new-session"] = output
    with pytest.raises(TerminalError):
        TmuxTerminal(SESSION, record, runner=fake).open_session(
            cwd=Path("/repo"), env={}, command=["claude"]
        )


def test_attach_outside_tmux(fake: FakeTmux, record: Path) -> None:
    terminal = TmuxTerminal(SESSION, record, runner=fake, tmux="/opt/tmux")
    assert terminal.attach_argv({}) == [
        "/opt/tmux",
        "attach-session",
        "-t",
        f"={SESSION}",
    ]
    assert fake.calls == []


def test_attach_inside_tmux_switches_client(fake: FakeTmux, record: Path) -> None:
    terminal = TmuxTerminal(SESSION, record, runner=fake, tmux="/opt/tmux")
    assert terminal.attach_argv({"TMUX": "/tmp/s,1,0"}) == [
        "/opt/tmux",
        "switch-client",
        "-t",
        f"={SESSION}",
    ]
    assert fake.calls == []


# split and tabs


def test_split_defaults_to_the_main_pane_horizontally(
    fake: FakeTmux, record: Path
) -> None:
    terminal = opened(fake, record)
    pane = terminal.terminal_split(["orch", "watch"])
    assert pane == "%2"
    assert fake.calls == [
        [
            "/opt/tmux",
            "split-window",
            "-d",
            "-t",
            "%1",
            "-h",
            "-P",
            "-F",
            "#{pane_id}",
            "orch",
            "watch",
        ]
    ]
    assert load_record(record) == PaneRecord(
        session=SESSION, main_pane="%1", created=("%2",)
    )


def test_split_a_created_pane_vertically_with_a_size(
    fake: FakeTmux, record: Path
) -> None:
    terminal = opened(fake, record)
    first = terminal.terminal_split(["orch", "watch"])
    fake.calls.clear()
    second = terminal.terminal_split(
        ["tail", "-F", "x.log"], target=first, vertical=True, percent=40
    )
    assert fake.calls == [
        [
            "/opt/tmux",
            "split-window",
            "-d",
            "-t",
            first,
            "-v",
            "-l",
            "40%",
            "-P",
            "-F",
            "#{pane_id}",
            "tail",
            "-F",
            "x.log",
        ]
    ]
    record_value = load_record(record)
    assert record_value is not None
    assert record_value.created == (first, second)


def test_split_refuses_a_pane_orch_does_not_own(fake: FakeTmux, record: Path) -> None:
    terminal = opened(fake, record)
    with pytest.raises(TerminalError):
        terminal.terminal_split(["x"], target="%99")
    assert fake.calls == []


def test_open_tab_records_its_pane(fake: FakeTmux, record: Path) -> None:
    terminal = opened(fake, record)
    pane = terminal.terminal_open_tab(["codex"], name="d-lane")
    assert fake.calls == [
        [
            "/opt/tmux",
            "new-window",
            "-d",
            "-t",
            f"={SESSION}:",
            "-n",
            "d-lane",
            "-P",
            "-F",
            "#{pane_id}",
            "codex",
        ]
    ]
    record_value = load_record(record)
    assert record_value is not None
    assert pane in record_value.created


# send and close: only panes orch created, never the main pane


def test_send_types_literal_text_then_enter(fake: FakeTmux, record: Path) -> None:
    terminal = opened(fake, record)
    pane = terminal.terminal_open_tab(["sh"], name="d")
    fake.calls.clear()
    terminal.terminal_send(pane, "echo 'a; b'")
    assert fake.calls == [
        ["/opt/tmux", "send-keys", "-t", pane, "-l", "echo 'a; b'"],
        ["/opt/tmux", "send-keys", "-t", pane, "Enter"],
    ]


def test_send_never_reaches_the_main_pane(fake: FakeTmux, record: Path) -> None:
    terminal = opened(fake, record)
    with pytest.raises(TerminalError):
        terminal.terminal_send("%1", "rm -rf x")
    assert fake.calls == []


def test_send_refuses_the_main_pane_even_if_listed_as_created(
    fake: FakeTmux, record: Path
) -> None:
    terminal = opened(fake, record)
    save_record(record, PaneRecord(session=SESSION, main_pane="%1", created=("%1",)))
    with pytest.raises(TerminalError):
        terminal.terminal_send("%1", "x")
    with pytest.raises(TerminalError):
        terminal.close_pane("%1")
    assert fake.calls == []


def test_send_refuses_an_unrecorded_pane(fake: FakeTmux, record: Path) -> None:
    terminal = opened(fake, record)
    with pytest.raises(TerminalError):
        terminal.terminal_send("%42", "x")
    assert fake.calls == []


def test_send_refuses_without_a_record(fake: FakeTmux, record: Path) -> None:
    terminal = TmuxTerminal(SESSION, record, runner=fake)
    with pytest.raises(TerminalError):
        terminal.terminal_send("%2", "x")
    with pytest.raises(TerminalError):
        terminal.terminal_split(["x"])
    with pytest.raises(TerminalError):
        terminal.terminal_open_tab(["x"], name="n")
    with pytest.raises(TerminalError):
        terminal.close_pane("%2")
    assert fake.calls == []


def test_a_record_for_another_session_is_not_used(fake: FakeTmux, record: Path) -> None:
    save_record(
        record, PaneRecord(session="orch-other", main_pane="%1", created=("%2",))
    )
    terminal = TmuxTerminal(SESSION, record, runner=fake)
    with pytest.raises(TerminalError):
        terminal.terminal_send("%2", "x")
    with pytest.raises(TerminalError):
        terminal.close_pane("%2")
    assert fake.calls == []


def test_send_sees_panes_another_process_recorded(fake: FakeTmux, record: Path) -> None:
    first = opened(fake, record)
    second = TmuxTerminal(SESSION, record, runner=fake, tmux="/opt/tmux")
    pane = second.terminal_open_tab(["sh"], name="d")
    fake.calls.clear()
    first.terminal_send(pane, "x")
    assert len(fake.calls) == 2


def test_close_kills_and_forgets_the_pane(fake: FakeTmux, record: Path) -> None:
    terminal = opened(fake, record)
    pane = terminal.terminal_split(["orch", "watch"])
    fake.calls.clear()
    terminal.close_pane(pane)
    assert fake.calls == [["/opt/tmux", "kill-pane", "-t", pane]]
    assert load_record(record) == PaneRecord(session=SESSION, main_pane="%1")
    with pytest.raises(TerminalError):
        terminal.terminal_send(pane, "x")


def test_a_failed_send_raises(fake: FakeTmux, record: Path) -> None:
    terminal = opened(fake, record)
    pane = terminal.terminal_open_tab(["sh"], name="d")
    fake.fail["send-keys"] = (1, "can't find pane")
    with pytest.raises(TerminalError, match="can't find pane"):
        terminal.terminal_send(pane, "x")


def test_live_panes_lists_the_session(fake: FakeTmux, record: Path) -> None:
    terminal = TmuxTerminal(SESSION, record, runner=fake, tmux="/opt/tmux")
    fake.stdout["list-panes"] = "%1\n%4\n"
    assert terminal.live_panes() == frozenset({"%1", "%4"})
    assert fake.calls == [
        ["/opt/tmux", "list-panes", "-s", "-t", f"={SESSION}", "-F", "#{pane_id}"]
    ]


def test_run_tmux_passes_an_argument_list_without_a_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orch.terminal import tmux as tmux_module

    seen: dict[str, object] = {}

    def fake_run(argv: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen["argv"] = argv
        seen.update(kwargs)
        return subprocess.CompletedProcess(["x"], 0, "", "")

    monkeypatch.setattr(tmux_module.subprocess, "run", fake_run)
    tmux_module.run_tmux(("tmux", "has-session", "-t", "=s"))
    assert seen["argv"] == ["tmux", "has-session", "-t", "=s"]
    assert seen.get("shell", False) is False
    assert seen["capture_output"] is True
    assert seen["text"] is True
    assert seen["check"] is False
    assert seen["timeout"] == 10


# main_pane_status. Output formats measured on tmux 3.8 and Claude Code 2.1.319.

CLAUDE = "/abs/bin/claude"


def panes_output(*lines: str) -> str:
    return "".join(line + "\n" for line in lines)


def test_a_live_main_pane_running_claude_is_alive(fake: FakeTmux, record: Path) -> None:
    terminal = opened(fake, record)
    fake.stdout["list-panes"] = panes_output(
        f"%1|0|{CLAUDE} --model claude-opus-5-5 --settings /abs/settings.json",
        "%2|0|orch watch --panels",
    )
    assert terminal.main_pane_status(CLAUDE) is MainPaneStatus.ALIVE


def test_main_pane_status_asks_for_the_measured_fields(
    fake: FakeTmux, record: Path
) -> None:
    terminal = opened(fake, record)
    fake.stdout["list-panes"] = panes_output(f"%1|0|{CLAUDE}")
    terminal.main_pane_status(CLAUDE)
    assert fake.calls == [
        ["/opt/tmux", "has-session", "-t", f"={SESSION}"],
        [
            "/opt/tmux",
            "list-panes",
            "-s",
            "-t",
            f"={SESSION}",
            "-F",
            "#{pane_id}|#{pane_dead}|#{pane_start_command}",
        ],
    ]


def test_main_pane_status_accepts_a_path_object(fake: FakeTmux, record: Path) -> None:
    terminal = opened(fake, record)
    fake.stdout["list-panes"] = panes_output(f"%1|0|{CLAUDE} --model x")
    assert terminal.main_pane_status(Path(CLAUDE)) is MainPaneStatus.ALIVE


def test_a_quoted_claude_path_with_a_space_is_alive(
    fake: FakeTmux, record: Path
) -> None:
    terminal = opened(fake, record)
    fake.stdout["list-panes"] = panes_output('%1|0|"/abs/my dir/claude" --model x')
    assert terminal.main_pane_status("/abs/my dir/claude") is MainPaneStatus.ALIVE


def test_a_start_command_containing_the_separator_is_kept_whole(
    fake: FakeTmux, record: Path
) -> None:
    terminal = opened(fake, record)
    fake.stdout["list-panes"] = panes_output(f'%1|0|{CLAUDE} --x "a|b"')
    assert terminal.main_pane_status(CLAUDE) is MainPaneStatus.ALIVE


def test_a_missing_record_is_reported(fake: FakeTmux, record: Path) -> None:
    terminal = TmuxTerminal(SESSION, record, runner=fake, tmux="/opt/tmux")
    assert terminal.main_pane_status(CLAUDE) is MainPaneStatus.NO_RECORD
    assert fake.calls == []


def test_a_broken_record_is_reported_without_raising(
    fake: FakeTmux, record: Path
) -> None:
    terminal = opened(fake, record)
    record.write_text("not json", encoding="utf-8")
    assert terminal.main_pane_status(CLAUDE) is MainPaneStatus.NO_RECORD


def test_a_record_of_another_session_is_reported(fake: FakeTmux, record: Path) -> None:
    save_record(record, PaneRecord("orch-other", "%1"))
    terminal = TmuxTerminal(SESSION, record, runner=fake, tmux="/opt/tmux")
    assert terminal.main_pane_status(CLAUDE) is MainPaneStatus.NO_RECORD


def test_a_missing_session_is_reported(fake: FakeTmux, record: Path) -> None:
    terminal = opened(fake, record)
    fake.fail["has-session"] = (1, "can't find session")
    assert terminal.main_pane_status(CLAUDE) is MainPaneStatus.NO_SESSION


def test_a_failed_pane_listing_is_unavailable(fake: FakeTmux, record: Path) -> None:
    terminal = opened(fake, record)
    fake.fail["list-panes"] = (1, "no server running")
    assert terminal.main_pane_status(CLAUDE) is MainPaneStatus.UNAVAILABLE


@pytest.mark.parametrize("error", [OSError("gone"), subprocess.TimeoutExpired("t", 1)])
def test_a_raising_pane_listing_is_unavailable(
    fake: FakeTmux, record: Path, error: Exception
) -> None:
    terminal = opened(fake, record)

    def runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if argv[1] == "list-panes":
            raise error
        return fake(argv)

    terminal.runner = runner
    assert terminal.main_pane_status(CLAUDE) is MainPaneStatus.UNAVAILABLE


def test_a_main_pane_that_is_gone_is_reported(fake: FakeTmux, record: Path) -> None:
    terminal = opened(fake, record)
    fake.stdout["list-panes"] = panes_output("%2|0|orch watch --panels")
    assert terminal.main_pane_status(CLAUDE) is MainPaneStatus.PANE_GONE


def test_a_pane_id_prefix_is_not_the_main_pane(fake: FakeTmux, record: Path) -> None:
    terminal = opened(fake, record)
    fake.stdout["list-panes"] = panes_output(f"%10|0|{CLAUDE}")
    assert terminal.main_pane_status(CLAUDE) is MainPaneStatus.PANE_GONE


def test_a_dead_main_pane_is_reported(fake: FakeTmux, record: Path) -> None:
    terminal = opened(fake, record)
    fake.stdout["list-panes"] = panes_output(f"%1|1|{CLAUDE} --model x")
    assert terminal.main_pane_status(CLAUDE) is MainPaneStatus.PANE_DEAD


@pytest.mark.parametrize(
    "start",
    [
        "",  # measured: a pane started as a plain shell
        "   ",  # shlex.split returns an empty list
        '/bin/sh -c "sleep 60" "a b"',  # measured
        f"{CLAUDE}-old --model x",
        f"/other{CLAUDE}",
        f"/bin/sh {CLAUDE}",
        '"/abs/bin/claude',  # unbalanced quote: shlex raises ValueError
    ],
)
def test_a_main_pane_running_something_else_is_reported(
    fake: FakeTmux, record: Path, start: str
) -> None:
    terminal = opened(fake, record)
    fake.stdout["list-panes"] = panes_output(f"%1|0|{start}")
    assert terminal.main_pane_status(CLAUDE) is MainPaneStatus.NOT_CLAUDE


def test_a_main_pane_line_without_fields_is_not_claude(
    fake: FakeTmux, record: Path
) -> None:
    terminal = opened(fake, record)
    fake.stdout["list-panes"] = panes_output("%1|0")
    assert terminal.main_pane_status(CLAUDE) is MainPaneStatus.NOT_CLAUDE


def test_a_malformed_other_line_is_ignored(fake: FakeTmux, record: Path) -> None:
    terminal = opened(fake, record)
    fake.stdout["list-panes"] = panes_output("garbage", f"%1|0|{CLAUDE}")
    assert terminal.main_pane_status(CLAUDE) is MainPaneStatus.ALIVE


def test_main_pane_status_does_not_change_the_record(
    fake: FakeTmux, record: Path
) -> None:
    terminal = opened(fake, record)
    fake.stdout["list-panes"] = panes_output(f"%1|0|{CLAUDE}")
    before = record.read_bytes()
    terminal.main_pane_status(CLAUDE)
    assert record.read_bytes() == before


def launched_argv(tmp_path: Path, claude: Path) -> tuple[str, ...]:
    hub = tmp_path / "hub"
    (hub / "config").mkdir(parents=True)
    (hub / "config" / "settings.json").write_text("{}", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    return build_plan(
        target=Target(
            name="orchestrator",
            path=repo,
            github="o/r",
            default_branch="main",
            setup_command="true",
            test_command="true",
            lint_command="true",
        ),
        hub=hub,
        orch_home=tmp_path / "orch",
        default_orch_home=tmp_path / "orch",
        claude=claude,
        claude_version="2.1.319",
        dirty=False,
    ).argv


def tmux_start_command(argv: Sequence[str]) -> str:
    """Format argv as measured: tmux double-quotes an argument with a space."""
    return " ".join(f'"{word}"' if " " in word else word for word in argv)


@pytest.mark.parametrize("directory", ["bin", "my dir"])
def test_the_path_orch_open_launches_with_is_alive(
    fake: FakeTmux, record: Path, tmp_path: Path, directory: str
) -> None:
    claude = tmp_path / directory / "claude"
    argv = launched_argv(tmp_path, claude)
    terminal = opened(fake, record)
    fake.stdout["list-panes"] = panes_output(f"%1|0|{tmux_start_command(argv)}")
    assert argv[0] == str(claude)
    assert terminal.main_pane_status(claude) is MainPaneStatus.ALIVE


def test_a_symlinked_claude_path_is_compared_without_resolving_it(
    fake: FakeTmux, record: Path, tmp_path: Path
) -> None:
    """Measured: the configured claude path is a symlink and tmux shows the link."""
    real = tmp_path / "versions" / "2.1.319"
    real.parent.mkdir()
    real.write_text("", encoding="utf-8")
    link = tmp_path / "claude"
    link.symlink_to(real)
    terminal = opened(fake, record)
    fake.stdout["list-panes"] = panes_output(f"%1|0|{link} --model x")
    assert terminal.main_pane_status(link) is MainPaneStatus.ALIVE
    fake.stdout["list-panes"] = panes_output(f"%1|0|{real} --model x")
    assert terminal.main_pane_status(link) is MainPaneStatus.NOT_CLAUDE


# probe_session. has-session exit codes measured on tmux 3.8.


def test_probe_finds_a_present_session(fake: FakeTmux, record: Path) -> None:
    terminal = TmuxTerminal(SESSION, record, runner=fake, tmux="/opt/tmux")
    assert terminal.probe_session() is SessionProbe.PRESENT
    assert fake.calls == [["/opt/tmux", "has-session", "-t", f"={SESSION}"]]


def test_probe_reads_exit_one_as_absent(fake: FakeTmux, record: Path) -> None:
    terminal = TmuxTerminal(SESSION, record, runner=fake, tmux="/opt/tmux")
    fake.fail["has-session"] = (1, "no server running")
    assert terminal.probe_session() is SessionProbe.ABSENT


@pytest.mark.parametrize("code", [2, 127])
def test_probe_reads_another_exit_code_as_unavailable(
    fake: FakeTmux, record: Path, code: int
) -> None:
    terminal = TmuxTerminal(SESSION, record, runner=fake, tmux="/opt/tmux")
    fake.fail["has-session"] = (code, "failure")
    assert terminal.probe_session() is SessionProbe.UNAVAILABLE


@pytest.mark.parametrize(
    "error", [FileNotFoundError("tmux"), subprocess.TimeoutExpired("t", 1)]
)
def test_probe_reads_a_failed_run_as_unavailable(
    record: Path, error: Exception
) -> None:
    def runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        raise error

    terminal = TmuxTerminal(SESSION, record, runner=runner)
    assert terminal.probe_session() is SessionProbe.UNAVAILABLE


def test_has_session_still_reads_a_failed_run_as_false(record: Path) -> None:
    def runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("tmux")

    assert TmuxTerminal(SESSION, record, runner=runner).has_session() is False


def test_a_main_pane_check_without_tmux_is_unavailable_not_no_session(
    fake: FakeTmux, record: Path
) -> None:
    """Under launchd tmux is not on PATH: a live session must not look absent."""
    terminal = opened(fake, record)

    def runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("tmux")

    terminal.runner = runner
    assert terminal.main_pane_status(CLAUDE) is MainPaneStatus.UNAVAILABLE


def test_a_main_pane_check_with_exit_127_is_unavailable(
    fake: FakeTmux, record: Path
) -> None:
    terminal = opened(fake, record)
    fake.fail["has-session"] = (127, "not found")
    assert terminal.main_pane_status(CLAUDE) is MainPaneStatus.UNAVAILABLE
