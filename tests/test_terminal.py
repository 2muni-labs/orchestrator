import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from orch.terminal import (
    PaneRecord,
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
