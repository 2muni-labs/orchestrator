import ast
import io
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest

from orch import watch as watch_module
from orch.watch import CLEAR_SCREEN, watch


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
