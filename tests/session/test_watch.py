import ast
import io
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

import pytest

from orch import watch as watch_module
from orch.watch import (
    CLEAR_SCREEN,
    current_log,
    load_panel_map,
    save_panel_map,
    sync_panels,
    watch,
)


class Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def frames(items: Iterable[str | BaseException]) -> Callable[[], str]:
    iterator = iter(items)

    def frame() -> str:
        item = next(iterator)
        if isinstance(item, BaseException):
            raise item
        return item

    return frame


class Sleeps:
    """Record sleeps, and interrupt the loop after a number of them."""

    def __init__(self, limit: int) -> None:
        self.calls: list[float] = []
        self.limit = limit

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if len(self.calls) >= self.limit:
            raise KeyboardInterrupt


def test_once_draws_one_frame_without_sleeping() -> None:
    out = io.StringIO()
    sleeps = Sleeps(1)
    assert watch(frames(["a\n"]), once=True, out=out, sleep=sleeps) == 0
    assert out.getvalue() == "a\n"
    assert sleeps.calls == []


def test_once_returns_70_when_the_frame_fails() -> None:
    out = io.StringIO()
    code = watch(frames([ValueError("db locked")]), once=True, out=out)
    assert code == 70
    assert out.getvalue() == "조회 실패: db locked\n"


def test_the_first_frame_comes_before_any_sleep() -> None:
    events: list[str] = []

    def frame() -> str:
        events.append("frame")
        return "a\n"

    def sleep(seconds: float) -> None:
        events.append("sleep")
        raise KeyboardInterrupt

    assert watch(frame, out=io.StringIO(), sleep=sleep) == 0
    assert events == ["frame", "sleep"]


def test_the_loop_redraws_on_the_interval_until_interrupted() -> None:
    out = io.StringIO()
    sleeps = Sleeps(3)
    code = watch(frames(["a\n", "b\n", "c\n"]), interval_sec=7, out=out, sleep=sleeps)
    assert code == 0
    assert sleeps.calls == [7, 7, 7]
    assert out.getvalue() == "a\nb\nc\n"


def test_the_default_interval_is_two_seconds() -> None:
    sleeps = Sleeps(1)
    watch(frames(["a\n"]), out=io.StringIO(), sleep=sleeps)
    assert sleeps.calls == [2]


def test_a_failed_frame_is_shown_and_the_loop_goes_on() -> None:
    out = io.StringIO()
    sleeps = Sleeps(2)
    code = watch(frames([OSError("gone"), "b\n"]), out=out, sleep=sleeps)
    assert code == 0
    assert out.getvalue() == "조회 실패: gone\nb\n"


def test_an_earlier_failure_does_not_affect_the_exit_code_when_interrupted() -> None:
    sleeps = Sleeps(1)
    assert watch(frames([OSError("x")]), out=io.StringIO(), sleep=sleeps) == 0


def test_an_interrupt_during_the_frame_ends_cleanly() -> None:
    out = io.StringIO()
    assert watch(frames([KeyboardInterrupt()]), out=out, sleep=Sleeps(1)) == 0
    assert out.getvalue() == ""


def test_other_base_exceptions_are_not_caught() -> None:
    with pytest.raises(SystemExit):
        watch(frames([SystemExit(3)]), out=io.StringIO(), sleep=Sleeps(1))


def test_a_missing_final_newline_is_added_once() -> None:
    out = io.StringIO()
    watch(frames(["a"]), once=True, out=out)
    assert out.getvalue() == "a\n"


def test_a_terminal_is_cleared_before_each_frame() -> None:
    out = Tty()
    watch(frames(["a\n", "b\n"]), out=out, sleep=Sleeps(2))
    assert out.getvalue() == f"{CLEAR_SCREEN}a\n{CLEAR_SCREEN}b\n"


def test_a_pipe_is_not_cleared() -> None:
    out = io.StringIO()
    watch(frames(["a\n"]), once=True, out=out)
    assert CLEAR_SCREEN not in out.getvalue()


def test_each_frame_is_flushed() -> None:
    class Counting(io.StringIO):
        flushes = 0

        def flush(self) -> None:
            self.flushes += 1
            super().flush()

    out = Counting()
    watch(frames(["a\n"]), once=True, out=out)
    assert out.flushes == 1


def test_the_default_output_is_read_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    watch(frames(["a\n"]), once=True)
    assert out.getvalue() == "a\n"


def test_the_module_imports_only_the_standard_library() -> None:
    tree = ast.parse(Path(watch_module.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    assert not {name for name in names if name.startswith("orch")}
    assert not names & {"subprocess", "socket", "sqlite3"}


# log panels (M13)


class SplitError(Exception):
    pass


class FakePanels:
    def __init__(self, live: set[str] | None = None) -> None:
        self.live = set() if live is None else set(live)
        self.calls: list[tuple[object, ...]] = []
        self.next_id = 10
        self.fail_split: set[str] = set()
        self.fail_close: set[str] = set()

    def terminal_split(
        self,
        command: Sequence[str],
        *,
        target: str | None = None,
        vertical: bool = False,
        percent: int | None = None,
    ) -> str:
        self.calls.append(("split", list(command), target, vertical, percent))
        if command[-1] in self.fail_split:
            raise SplitError(f"split failed for {command[-1]}")
        pane = f"%{self.next_id}"
        self.next_id += 1
        self.live.add(pane)
        return pane

    def close_pane(self, pane: str) -> None:
        self.calls.append(("close", pane))
        if pane in self.fail_close:
            raise RuntimeError("close failed")
        self.live.discard(pane)

    def live_panes(self) -> frozenset[str]:
        self.calls.append(("live",))
        return frozenset(self.live)


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    return path


def test_current_log_picks_the_highest_run(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    touch(logs / "a.1.log")
    touch(logs / "a.10.log")
    touch(logs / "a.2.log")
    touch(logs / "a.gate.log")
    touch(logs / "ab.99.log")
    touch(logs / "a.3.log.bak")
    assert current_log(logs, "a") == logs / "a.10.log"


def test_current_log_without_logs(tmp_path: Path) -> None:
    assert current_log(tmp_path / "missing", "a") is None
    touch(tmp_path / "logs" / "b.1.log")
    assert current_log(tmp_path / "logs", "a") is None


def test_panel_map_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "t" / "panels.json"
    save_panel_map(path, {"a": "%1", "b": "%2"})
    assert load_panel_map(path) == {"a": "%1", "b": "%2"}
    assert sorted(p.name for p in path.parent.iterdir()) == ["panels.json"]


@pytest.mark.parametrize(
    "text", ["", "not json", "[]", '"x"', '{"a": 1, "b": "%2", "3": null}']
)
def test_load_panel_map_tolerates_bad_files(tmp_path: Path, text: str) -> None:
    path = tmp_path / "panels.json"
    path.write_text(text)
    expected = {"b": "%2"} if text.startswith("{") else {}
    assert load_panel_map(path) == expected


def test_load_panel_map_missing_file(tmp_path: Path) -> None:
    assert load_panel_map(tmp_path / "panels.json") == {}


def test_sync_opens_a_panel_per_running_task(tmp_path: Path) -> None:
    state = tmp_path / "panels.json"
    terminal = FakePanels()
    running = {"b": tmp_path / "b.1.log", "a": tmp_path / "a.2.log"}
    result = sync_panels(terminal, running=running, state=state, anchor="%3")
    assert result == {"a": "%10", "b": "%11"}
    assert terminal.calls == [
        ("live",),
        ("split", ["tail", "-F", str(tmp_path / "a.2.log")], "%3", True, None),
        ("split", ["tail", "-F", str(tmp_path / "b.1.log")], "%3", True, None),
    ]
    assert load_panel_map(state) == result


def test_sync_keeps_an_open_panel(tmp_path: Path) -> None:
    state = tmp_path / "panels.json"
    save_panel_map(state, {"a": "%5"})
    terminal = FakePanels(live={"%5"})
    result = sync_panels(
        terminal, running={"a": tmp_path / "a.3.log"}, state=state, anchor=None
    )
    assert result == {"a": "%5"}
    assert terminal.calls == [("live",)]


def test_sync_closes_panels_of_finished_tasks(tmp_path: Path) -> None:
    state = tmp_path / "panels.json"
    save_panel_map(state, {"a": "%5", "b": "%6"})
    terminal = FakePanels(live={"%5", "%6"})
    result = sync_panels(terminal, running={"b": tmp_path / "b.1.log"}, state=state)
    assert result == {"b": "%6"}
    assert ("close", "%5") in terminal.calls
    assert ("close", "%6") not in terminal.calls
    assert load_panel_map(state) == {"b": "%6"}


def test_sync_forgets_panels_that_disappeared(tmp_path: Path) -> None:
    state = tmp_path / "panels.json"
    save_panel_map(state, {"a": "%5", "b": "%6"})
    terminal = FakePanels(live={"%6"})
    result = sync_panels(
        terminal,
        running={"a": tmp_path / "a.1.log", "b": tmp_path / "b.1.log"},
        state=state,
    )
    assert result == {"a": "%10", "b": "%6"}
    assert ("close", "%5") not in terminal.calls


def test_sync_drops_a_panel_that_fails_to_close(tmp_path: Path) -> None:
    state = tmp_path / "panels.json"
    save_panel_map(state, {"a": "%5", "c": "%7"})
    terminal = FakePanels(live={"%5", "%7"})
    terminal.fail_close.add("%5")
    result = sync_panels(terminal, running={}, state=state)
    assert result == {}
    assert ("close", "%5") in terminal.calls
    assert ("close", "%7") in terminal.calls
    assert load_panel_map(state) == {}


def test_sync_continues_after_a_failed_split_and_reraises(tmp_path: Path) -> None:
    state = tmp_path / "panels.json"
    terminal = FakePanels()
    bad = tmp_path / "a.1.log"
    terminal.fail_split.add(str(bad))
    with pytest.raises(SplitError, match="split failed"):
        sync_panels(
            terminal,
            running={"a": bad, "b": tmp_path / "b.1.log"},
            state=state,
        )
    assert load_panel_map(state) == {"b": "%10"}


def test_watch_calls_tick_before_each_frame() -> None:
    order: list[str] = []

    def frame() -> str:
        order.append("frame")
        return "board"

    out = io.StringIO()
    code = watch(frame, once=True, out=out, tick=lambda: order.append("tick"))
    assert code == 0
    assert order == ["tick", "frame"]
    assert out.getvalue() == "board\n"


def test_watch_reports_a_tick_failure_and_keeps_going() -> None:
    calls = {"n": 0}

    def tick() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("tmux gone")

    out = io.StringIO()
    sleeps = Sleeps(limit=2)
    code = watch(frames(["one", "two"]), out=out, sleep=sleeps, tick=tick)
    assert code == 0
    assert out.getvalue() == "one\n패널 갱신 실패: tmux gone\ntwo\n"
    assert calls["n"] == 2


def test_watch_once_tick_failure_keeps_exit_code() -> None:
    def tick() -> None:
        raise RuntimeError("x")

    out = io.StringIO()
    assert watch(frames(["board"]), once=True, out=out, tick=tick) == 0
    assert out.getvalue() == "board\n패널 갱신 실패: x\n"
