"""orch hook <name>: Claude Code hook output only, always exit code 0."""

import io
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from orch import config, guard, hooks, store
from orch.cli import main

HOOKS = {
    "session-start": "handle_session_start",
    "subagent-stop": "handle_subagent_stop",
    "notification": "handle_notification",
}

Run = Callable[..., tuple[int, str]]


@pytest.fixture(autouse=True)
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ORCH_HOME", str(home / "runtime"))
    for name in ("ORCH_HUB", "ORCH_TARGET", "ORCH_FAKE_AGENTS"):
        monkeypatch.delenv(name, raising=False)
    return home


@pytest.fixture
def run(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> Run:
    def call(name: str, stdin: object = "") -> tuple[int, str]:
        stream = io.StringIO(stdin) if isinstance(stdin, str) else stdin
        monkeypatch.setattr(sys, "stdin", stream)
        code = main(["hook", name])
        captured = capsys.readouterr()
        # Keep stderr for the test: reading the output must not discard it.
        sys.stderr.write(captured.err)
        return code, captured.out

    return call


def bash(command: str) -> str:
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})


def decision(out: str) -> str:
    assert out.endswith("\n") and out.count("\n") == 1, out
    data = json.loads(out)
    specific = data["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    assert specific["permissionDecisionReason"]
    return specific["permissionDecision"]


# pre-bash


def test_pre_bash_denies_with_one_json_line(run: Run) -> None:
    code, out = run("pre-bash", bash("codex exec x"))
    assert code == 0
    assert decision(out) == "deny"


def test_pre_bash_asks(run: Run) -> None:
    code, out = run("pre-bash", bash("orch resume m14-x"))
    assert code == 0
    assert decision(out) == "ask"


def test_pre_bash_prints_nothing_when_allowed(run: Run) -> None:
    assert run("pre-bash", bash("ls -la")) == (0, "")


def test_pre_bash_ignores_other_tools(run: Run) -> None:
    payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "x"}})
    assert run("pre-bash", payload) == (0, "")


@pytest.mark.parametrize("stdin", ["", "not json", "[]", '{"tool_name": "Bash"}'])
def test_pre_bash_fails_closed_on_bad_input(run: Run, stdin: str) -> None:
    code, out = run("pre-bash", stdin)
    assert code == 0
    assert decision(out) == "deny"


class BrokenStdin(io.StringIO):
    def read(self, size: int | None = -1) -> str:
        raise OSError("stdin is gone")


Failure = Callable[[], BaseException]

# Anything a handler or a stream can raise, including what Exception misses.
FAILURES: list[Failure] = [
    lambda: RuntimeError("bug"),
    lambda: KeyboardInterrupt(),
    lambda: SystemExit(1),
    lambda: SystemExit(0),
    lambda: BrokenPipeError(32, "Broken pipe"),
    lambda: MemoryError(),
]


def failure_id(error: Failure) -> str:
    value = error()
    return f"{type(value).__name__}{value.args[:1]}"


class Raising(io.StringIO):
    """A stream whose read, write or flush raises the given failure."""

    def __init__(
        self,
        *,
        read: Failure | None = None,
        write: Failure | None = None,
        flush: Failure | None = None,
    ) -> None:
        super().__init__()
        self.failures = {"read": read, "write": write, "flush": flush}

    def fail(self, method: str) -> None:
        failure = self.failures[method]
        if failure is not None:
            raise failure()

    def read(self, size: int | None = -1) -> str:
        self.fail("read")
        return ""

    def write(self, text: str) -> int:
        self.fail("write")
        return super().write(text)

    def flush(self) -> None:
        self.fail("flush")


RunTo = Callable[..., tuple[int, str]]


@pytest.fixture
def run_to(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> RunTo:
    """Run a hook with a given stdout stream; return the code and stderr."""

    def call(name: str, stdin: object, stdout: io.StringIO) -> tuple[int, str]:
        stream = io.StringIO(stdin) if isinstance(stdin, str) else stdin
        monkeypatch.setattr(sys, "stdin", stream)
        monkeypatch.setattr(sys, "stdout", stdout)
        code = main(["hook", name])
        err = capsys.readouterr().err
        # Keep stderr for the test: reading the output must not discard it.
        sys.stderr.write(err)
        return code, err

    return call


@pytest.mark.parametrize("error", FAILURES, ids=failure_id)
def test_pre_bash_denies_when_the_handler_raises_anything(
    run: Run, monkeypatch: pytest.MonkeyPatch, env: Path, error: Failure
) -> None:
    def broken(stdin_text: str, environ: Mapping[str, str]) -> str:
        raise error()

    monkeypatch.setattr(guard, "handle", broken)
    code, out = run("pre-bash", bash("ls"))
    assert code == 0
    assert decision(out) == "deny"
    assert not (env / "runtime" / "hooks.log").exists()


@pytest.mark.parametrize("error", FAILURES, ids=failure_id)
def test_pre_bash_denies_when_stdin_raises_anything(
    run: Run, env: Path, error: Failure
) -> None:
    code, out = run("pre-bash", Raising(read=error))
    assert code == 0
    assert decision(out) == "deny"
    assert not (env / "runtime" / "hooks.log").exists()


@pytest.mark.parametrize("method", ["write", "flush"])
@pytest.mark.parametrize("error", FAILURES, ids=failure_id)
def test_pre_bash_exits_2_when_stdout_fails(
    run_to: RunTo, env: Path, error: Failure, method: str
) -> None:
    code, err = run_to("pre-bash", bash("codex exec"), Raising(**{method: error}))
    assert code == 2
    assert re.search("[가-힣]", err), err
    assert not (env / "runtime" / "hooks.log").exists()


def test_pre_bash_exits_2_even_when_stderr_fails_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(bash("codex exec")))
    monkeypatch.setattr(sys, "stdout", Raising(write=OSError))
    monkeypatch.setattr(sys, "stderr", Raising(write=OSError, flush=OSError))
    assert main(["hook", "pre-bash"]) == 2


def test_pre_bash_does_not_write_an_allowed_command(run_to: RunTo) -> None:
    assert run_to("pre-bash", bash("ls"), Raising(write=OSError, flush=OSError)) == (
        0,
        "",
    )


def test_pre_bash_fails_closed_when_stdin_cannot_be_read(run: Run) -> None:
    code, out = run("pre-bash", BrokenStdin())
    assert code == 0
    assert decision(out) == "deny"


def test_pre_bash_fails_closed_when_the_handler_raises(
    run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(stdin_text: str, environ: Mapping[str, str]) -> str:
        raise RuntimeError("bug")

    monkeypatch.setattr(guard, "handle", broken)
    code, out = run("pre-bash", bash("ls"))
    assert code == 0
    assert decision(out) == "deny"


def test_pre_bash_passes_stdin_and_the_environment(
    run: Run, monkeypatch: pytest.MonkeyPatch, env: Path
) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def fake(stdin_text: str, environ: Mapping[str, str]) -> str:
        calls.append((stdin_text, dict(environ)))
        return '{"x": 1}'

    monkeypatch.setattr(guard, "handle", fake)
    assert run("pre-bash", "input text") == (0, '{"x": 1}\n')
    assert calls == [("input text", dict(os.environ))]
    assert calls[0][1]["HOME"] == str(env)


def test_pre_bash_reads_no_orch_settings_or_state(
    run: Run, monkeypatch: pytest.MonkeyPatch, env: Path, tmp_path: Path
) -> None:
    runtime = env / "runtime"
    runtime.mkdir()
    (runtime / "config").write_text("not = [valid", encoding="utf-8")
    monkeypatch.setenv("ORCH_HUB", str(tmp_path / "missing-hub"))

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("pre-bash must not read orch settings or state")

    for module, name in (
        (config, "orch_home"),
        (config, "load_config"),
        (config, "load_targets"),
        (config, "state_db_path"),
        (store, "connect"),
    ):
        monkeypatch.setattr(module, name, forbidden)
    assert run("pre-bash", bash("ls")) == (0, "")
    code, out = run("pre-bash", bash("codex exec"))
    assert code == 0
    assert decision(out) == "deny"
    assert not (runtime / "state.db").exists()


def test_pre_bash_works_with_a_relative_orch_home(
    run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ORCH_HOME", "relative")
    code, out = run("pre-bash", bash("codex exec"))
    assert code == 0
    assert decision(out) == "deny"


# the other hooks


@pytest.mark.parametrize("name", sorted(HOOKS))
def test_a_hook_calls_its_handler_with_stdin_and_the_environment(
    run: Run, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def fake(stdin_text: str, environ: Mapping[str, str]) -> str:
        calls.append((stdin_text, dict(environ)))
        return '{"hookSpecificOutput": {}}'

    monkeypatch.setattr(hooks, HOOKS[name], fake)
    assert run(name, '{"a": 1}') == (0, '{"hookSpecificOutput": {}}\n')
    assert calls == [('{"a": 1}', dict(os.environ))]


@pytest.mark.parametrize("name", sorted(HOOKS))
def test_a_hook_prints_nothing_for_empty_output(
    run: Run, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setattr(hooks, HOOKS[name], lambda stdin_text, environ: "")
    assert run(name, "{}") == (0, "")


def hook_log(env: Path) -> list[list[str]]:
    path = env / "runtime" / "hooks.log"
    if not path.exists():
        return []
    return [line.split(" ") for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("error", FAILURES, ids=failure_id)
@pytest.mark.parametrize("name", sorted(HOOKS))
def test_a_hook_logs_and_exits_0_when_its_handler_raises_anything(
    run: Run, monkeypatch: pytest.MonkeyPatch, env: Path, name: str, error: Failure
) -> None:
    def broken(stdin_text: str, environ: Mapping[str, str]) -> str:
        raise error()

    monkeypatch.setattr(hooks, HOOKS[name], broken)
    assert run(name, "{}") == (0, "")
    assert [line[1:] for line in hook_log(env)] == [[name, type(error()).__name__]]


@pytest.mark.parametrize("error", FAILURES, ids=failure_id)
@pytest.mark.parametrize("name", sorted(HOOKS))
def test_a_hook_logs_a_stdin_failure_and_reads_empty_input(
    run: Run, monkeypatch: pytest.MonkeyPatch, env: Path, name: str, error: Failure
) -> None:
    seen: list[str] = []

    def fake(stdin_text: str, environ: Mapping[str, str]) -> str:
        seen.append(stdin_text)
        return ""

    monkeypatch.setattr(hooks, HOOKS[name], fake)
    assert run(name, Raising(read=error)) == (0, "")
    assert seen == [""]
    assert [line[1:] for line in hook_log(env)] == [[name, type(error()).__name__]]


@pytest.mark.parametrize("method", ["write", "flush"])
@pytest.mark.parametrize("error", FAILURES, ids=failure_id)
@pytest.mark.parametrize("name", sorted(HOOKS))
def test_a_hook_logs_a_stdout_failure_and_exits_0(
    run_to: RunTo,
    monkeypatch: pytest.MonkeyPatch,
    env: Path,
    name: str,
    error: Failure,
    method: str,
) -> None:
    monkeypatch.setattr(hooks, HOOKS[name], lambda stdin_text, environ: '{"a": 1}')
    code, _ = run_to(name, "{}", Raising(**{method: error}))
    assert code == 0
    assert [line[1:] for line in hook_log(env)] == [[name, type(error()).__name__]]


@pytest.mark.parametrize("name", sorted(HOOKS))
def test_a_hook_exits_0_when_even_its_log_fails(
    run_to: RunTo, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv("ORCH_HOME", "relative")

    def broken(stdin_text: str, environ: Mapping[str, str]) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(hooks, HOOKS[name], broken)
    assert run_to(name, Raising(read=KeyboardInterrupt), Raising(write=OSError)) == (
        0,
        "",
    )


@pytest.mark.parametrize("name", sorted(HOOKS))
def test_a_hook_reads_empty_input_when_stdin_cannot_be_read(
    run: Run, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    seen: list[str] = []

    def fake(stdin_text: str, environ: Mapping[str, str]) -> str:
        seen.append(stdin_text)
        return ""

    monkeypatch.setattr(hooks, HOOKS[name], fake)
    assert run(name, BrokenStdin()) == (0, "")
    assert seen == [""]


def test_session_start_without_a_state_db_prints_nothing_and_creates_none(
    run: Run, env: Path
) -> None:
    assert run("session-start", "{}") == (0, "")
    assert not (env / "runtime" / "state.db").exists()


def test_subagent_stop_writes_the_target_log(
    run: Run, monkeypatch: pytest.MonkeyPatch, env: Path
) -> None:
    monkeypatch.setenv("ORCH_TARGET", "sample")
    payload = {"agent_type": "orch-reviewer", "last_assistant_message": "done"}
    assert run("subagent-stop", json.dumps(payload)) == (0, "")
    log = config.target_paths(env / "runtime", "sample").logs / "subagents.log"
    assert log.read_text(encoding="utf-8").endswith(" orch-reviewer done\n")


def test_an_unknown_hook_is_a_usage_error(run: Run) -> None:
    code, out = run("nosuch", "{}")
    assert code == 64
    assert json.loads(out)["error"] == "usage"


# a real process, as Claude Code runs it


def test_a_process_denies_without_any_orch_setup(tmp_path: Path) -> None:
    environ = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "ORCH_HOME": "relative",
    }
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            "from orch.cli import main; raise SystemExit(main())",
            "hook",
            "pre-bash",
        ],
        input=bash("codex exec x"),
        capture_output=True,
        text=True,
        env=environ,
        cwd=tmp_path,
    )
    assert process.returncode == 0, process.stderr
    assert decision(process.stdout) == "deny"


def test_a_process_exits_2_when_stdout_is_a_closed_pipe(tmp_path: Path) -> None:
    read_end, write_end = os.pipe()
    os.close(read_end)
    try:
        process = subprocess.run(
            [
                sys.executable,
                "-c",
                "from orch.cli import main; raise SystemExit(main())",
                "hook",
                "pre-bash",
            ],
            input=bash("codex exec x").encode(),
            stdout=write_end,
            stderr=subprocess.PIPE,
            env={
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(tmp_path),
                "ORCH_HOME": "relative",
            },
            cwd=tmp_path,
        )
    finally:
        os.close(write_end)
    assert process.returncode == 2, process.stderr
    assert re.search("[가-힣]", process.stderr.decode("utf-8")), process.stderr
