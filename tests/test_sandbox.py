"""The gate sandbox: the wrapper, the start check, and the real profile on macOS."""

import json
import os
import socket
import stat
import subprocess
import sys
import threading
import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest

from orch import gate, sandbox
from orch.sandbox import SandboxError
from sandbox_env import FAKE_SANDBOX_EXEC, REAL_SANDBOX_EXEC, real_sandbox_reason


def make_executable(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def recording_fake(
    tmp_path: Path,
    *,
    check_exit: int | None = None,
    check_stderr: str = "",
    check_sleep: float = 0,
) -> tuple[Path, Path]:
    """A stand-in that records each call, then fails the check or runs the command."""
    record = tmp_path / "fake-calls.jsonl"
    script = make_executable(
        tmp_path / "fake-bin" / "sandbox-exec",
        f"""#!{sys.executable}
import json, os, sys, time
record = {str(record)!r}
check_command = {sandbox.CHECK_COMMAND!r}
check_exit = {check_exit!r}
check_stderr = {check_stderr!r}
check_sleep = {check_sleep!r}
argv = sys.argv[1:]
with open(record, "a", encoding="utf-8") as out:
    out.write(json.dumps({{"argv": argv, "tmpdir": os.environ.get("TMPDIR")}}) + "\\n")
command = list(argv)
while command and command[0] in ("-p", "-f", "-D"):
    command = command[2:]
if command == [check_command]:
    time.sleep(check_sleep)
    if check_exit is not None:
        sys.stderr.write(check_stderr)
        sys.exit(check_exit)
os.execvp(command[0], command)
""",
    )
    return script, record


@pytest.mark.parametrize("check_exit", [None, 0, 65])
def test_the_recording_stand_in_compiles_without_warnings(
    tmp_path: Path, check_exit: int | None
) -> None:
    """A warning would reach stderr first and change what the check reports."""
    script, _ = recording_fake(tmp_path, check_exit=check_exit, check_stderr="x\n")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        compile(script.read_text(encoding="utf-8"), str(script), "exec")


def calls(record: Path) -> list[dict]:
    if not record.exists():
        return []
    return [
        json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()
    ]


def params(argv: list[str]) -> dict[str, str]:
    found = {}
    for index, item in enumerate(argv[:-1]):
        if item == "-D":
            name, _, value = argv[index + 1].partition("=")
            found[name] = value
    return found


@pytest.fixture
def dirs(tmp_path: Path) -> dict[str, Path]:
    found = {}
    for name in ("worktree", "tmpdir", "home"):
        path = tmp_path / "real" / name
        path.mkdir(parents=True)
        found[name] = path
    return found


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "logs" / "gate.log"


# Which sandbox-exec


def test_the_default_executable_is_the_system_sandbox_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.undo()
    assert sandbox.SANDBOX_EXEC == "/usr/bin/sandbox-exec"
    assert sandbox.executable({}) == "/usr/bin/sandbox-exec"


def test_the_executable_is_read_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "SANDBOX_EXEC", "/elsewhere/sandbox-exec")
    assert sandbox.executable({}) == "/elsewhere/sandbox-exec"


@pytest.mark.parametrize(
    "environ",
    [
        {"ORCH_FAKE_SANDBOX_EXEC": "/fake/sandbox-exec"},
        {"ORCH_FAKE_SANDBOX_EXEC": "/fake/sandbox-exec", "ORCH_FAKE_AGENTS": "0"},
        {"ORCH_FAKE_SANDBOX_EXEC": "/fake/sandbox-exec", "ORCH_FAKE_AGENTS": "true"},
        {"ORCH_FAKE_SANDBOX_EXEC": "", "ORCH_FAKE_AGENTS": "1"},
        {"ORCH_FAKE_AGENTS": "1"},
    ],
)
def test_the_stand_in_is_ignored_outside_fake_agent_mode(
    environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "SANDBOX_EXEC", "/usr/bin/sandbox-exec")
    assert sandbox.executable(environ) == "/usr/bin/sandbox-exec"


def test_fake_agent_mode_may_name_a_stand_in() -> None:
    environ = {"ORCH_FAKE_AGENTS": "1", "ORCH_FAKE_SANDBOX_EXEC": "/fake/sandbox-exec"}
    assert sandbox.executable(environ) == "/fake/sandbox-exec"


def test_the_executable_reads_the_process_environment_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ORCH_FAKE_AGENTS", "1")
    monkeypatch.setenv("ORCH_FAKE_SANDBOX_EXEC", "/fake/sandbox-exec")
    assert sandbox.executable() == "/fake/sandbox-exec"
    monkeypatch.delenv("ORCH_FAKE_AGENTS")
    monkeypatch.setattr(sandbox, "SANDBOX_EXEC", "/usr/bin/sandbox-exec")
    assert sandbox.executable() == "/usr/bin/sandbox-exec"


# Wrapping a command


def test_wrap_passes_the_profile_and_resolved_paths(
    tmp_path: Path, dirs: dict[str, Path]
) -> None:
    links = {}
    for name, path in dirs.items():
        link = tmp_path / f"link-{name}"
        link.symlink_to(path)
        links[name] = link
    argv = sandbox.wrap(
        ["bash", "-c", "echo hi"],
        executable="/x/sandbox-exec",
        worktree=links["worktree"],
        tmpdir=links["tmpdir"],
        home=links["home"],
    )
    assert argv == [
        "/x/sandbox-exec",
        "-p",
        sandbox.PROFILE,
        "-D",
        f"WORKTREE={dirs['worktree'].resolve()}",
        "-D",
        f"TMPDIR={dirs['tmpdir'].resolve()}",
        "-D",
        f"HOME={dirs['home'].resolve()}",
        "bash",
        "-c",
        "echo hi",
    ]


def test_wrap_keeps_the_command_arguments_as_they_are(dirs: dict[str, Path]) -> None:
    command = ["/bin/echo", "two words", "-D", "-p", ""]
    argv = sandbox.wrap(command, executable="sx", **dirs)
    assert argv[-len(command) :] == command


def test_the_profile_names_its_three_parameters() -> None:
    for name in ("WORKTREE", "TMPDIR", "HOME"):
        assert f'(param "{name}")' in sandbox.PROFILE


# The start check


def test_the_check_passes_when_the_sandbox_starts(dirs: dict[str, Path]) -> None:
    assert sandbox.check(executable=str(FAKE_SANDBOX_EXEC), **dirs) is None


def test_the_check_runs_the_no_op_command_under_the_profile(
    tmp_path: Path, dirs: dict[str, Path]
) -> None:
    script, record = recording_fake(tmp_path)
    sandbox.check(executable=str(script), **dirs)
    found = calls(record)
    assert [call["argv"] for call in found] == [
        sandbox.wrap([sandbox.CHECK_COMMAND], executable=str(script), **dirs)[1:]
    ]


def test_the_check_fails_without_sandbox_exec(
    tmp_path: Path, dirs: dict[str, Path]
) -> None:
    with pytest.raises(SandboxError):
        sandbox.check(executable=str(tmp_path / "missing" / "sandbox-exec"), **dirs)


def test_the_check_fails_when_sandbox_exec_cannot_run(
    tmp_path: Path, dirs: dict[str, Path]
) -> None:
    path = tmp_path / "bin" / "sandbox-exec"
    path.parent.mkdir()
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(SandboxError):
        sandbox.check(executable=str(path), **dirs)


@pytest.mark.parametrize(
    ("code", "stderr"),
    [
        (65, "sandbox-exec: syntax error: expecting ')'\nmore\n"),
        (71, "sandbox-exec: sandbox_apply: Operation not permitted\n"),
        (1, ""),
    ],
)
def test_the_check_fails_when_the_profile_does_not_start(
    tmp_path: Path, dirs: dict[str, Path], code: int, stderr: str
) -> None:
    script, _ = recording_fake(tmp_path, check_exit=code, check_stderr=stderr)
    with pytest.raises(SandboxError) as caught:
        sandbox.check(executable=str(script), **dirs)
    message = str(caught.value)
    assert str(code) in message
    assert "\n" not in message
    if stderr:
        assert stderr.splitlines()[0] in message
        assert "more" not in message


def test_the_check_fails_on_its_time_limit(
    tmp_path: Path, dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "CHECK_TIMEOUT_SEC", 1)
    script, _ = recording_fake(tmp_path, check_sleep=30)
    with pytest.raises(SandboxError):
        sandbox.check(executable=str(script), **dirs)


# Gate commands through the sandbox


def test_a_gate_command_runs_after_a_check_under_the_profile(
    tmp_path: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, record = recording_fake(tmp_path)
    monkeypatch.setattr(sandbox, "SANDBOX_EXEC", str(script))
    worktree = tmp_path / "wt"
    worktree.mkdir()
    assert gate.run_command("echo hi > out.txt", worktree, log_path) == 0
    assert (worktree / "out.txt").read_text(encoding="utf-8") == "hi\n"
    found = [call["argv"] for call in calls(record)]
    assert len(found) == 2
    assert found[0][:2] == ["-p", sandbox.PROFILE]
    assert found[0][-1] == sandbox.CHECK_COMMAND
    assert found[1][:2] == ["-p", sandbox.PROFILE]
    assert found[1][-3:] == ["bash", "-c", "echo hi > out.txt"]
    assert params(found[0]) == params(found[1])
    assert "echo hi > out.txt" in log_path.read_text(encoding="utf-8")


def test_each_command_gets_a_new_resolved_tmpdir_that_is_removed(
    tmp_path: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, record = recording_fake(tmp_path)
    monkeypatch.setattr(sandbox, "SANDBOX_EXEC", str(script))
    worktree = tmp_path / "wt"
    worktree.mkdir()
    for name in ("one", "two"):
        command = f'printf %s "$TMPDIR" > {name}.txt; touch "$TMPDIR/scratch"'
        assert gate.run_command(command, worktree, log_path) == 0
    seen = [(worktree / f"{n}.txt").read_text(encoding="utf-8") for n in ("one", "two")]
    assert seen[0] != seen[1]
    found = calls(record)
    for value in seen:
        assert value == str(Path(value).resolve())
        assert not Path(value).exists()
    assert [params(call["argv"])["TMPDIR"] for call in found] == [
        seen[0],
        seen[0],
        seen[1],
        seen[1],
    ]
    assert [call["tmpdir"] for call in found][1::2] == seen


def test_the_worktree_and_home_parameters_are_resolved(
    tmp_path: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, record = recording_fake(tmp_path)
    monkeypatch.setattr(sandbox, "SANDBOX_EXEC", str(script))
    worktree = tmp_path / "real-wt"
    home = tmp_path / "real-home"
    worktree.mkdir()
    home.mkdir()
    (tmp_path / "wt").symlink_to(worktree)
    (tmp_path / "home").symlink_to(home)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert (
        gate.run_command('printf %s "$HOME" > home.txt', tmp_path / "wt", log_path) == 0
    )
    assert len(calls(record)) == 2
    for call in calls(record):
        found = params(call["argv"])
        assert found["WORKTREE"] == str(worktree.resolve())
        assert found["HOME"] == str(home.resolve())
    assert (worktree / "home.txt").read_text(encoding="utf-8") == str(tmp_path / "home")


def test_a_missing_sandbox_exec_raises_and_runs_nothing(
    tmp_path: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "SANDBOX_EXEC", str(tmp_path / "missing" / "sx"))
    with pytest.raises(SandboxError):
        gate.run_command("touch ran", tmp_path, log_path)
    assert not (tmp_path / "ran").exists()


def test_a_failed_check_raises_and_runs_nothing(
    tmp_path: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, record = recording_fake(tmp_path, check_exit=65, check_stderr="bad\n")
    monkeypatch.setattr(sandbox, "SANDBOX_EXEC", str(script))
    with pytest.raises(SandboxError):
        gate.run_command("touch ran", tmp_path, log_path)
    assert not (tmp_path / "ran").exists()
    assert len(calls(record)) == 1


def test_a_failed_check_leaves_no_tmpdir(
    tmp_path: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, record = recording_fake(tmp_path, check_exit=65)
    monkeypatch.setattr(sandbox, "SANDBOX_EXEC", str(script))
    with pytest.raises(SandboxError):
        gate.run_command("true", tmp_path, log_path)
    tmpdir = params(calls(record)[0]["argv"])["TMPDIR"]
    assert not Path(tmpdir).exists()


@pytest.mark.parametrize("code", [65, 71])
def test_after_the_check_an_exit_code_belongs_to_the_command(
    tmp_path: Path, log_path: Path, code: int
) -> None:
    assert gate.run_command(f"exit {code}", tmp_path, log_path) == code


def test_the_tmpdir_is_removed_after_a_timeout(
    tmp_path: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gate, "COMMAND_TIMEOUT_SEC", 1)
    command = 'printf %s "$TMPDIR" > tmpdir.txt; sleep 30'
    assert gate.run_command(command, tmp_path, log_path) is None
    tmpdir = (tmp_path / "tmpdir.txt").read_text(encoding="utf-8")
    assert tmpdir
    assert not Path(tmpdir).exists()


def test_without_the_sandbox_the_command_runs_as_before(
    tmp_path: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "SANDBOX_EXEC", str(tmp_path / "missing" / "sx"))
    own = tmp_path / "own-tmp"
    own.mkdir()
    monkeypatch.setenv("TMPDIR", str(own))
    command = 'printf %s "$TMPDIR" > tmpdir.txt'
    assert gate.run_command(command, tmp_path, log_path, sandbox=False) == 0
    assert (tmp_path / "tmpdir.txt").read_text(encoding="utf-8") == str(own)


# The real sandbox-exec and profile (macOS, outside any other sandbox)


@pytest.fixture
def real_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    reason = real_sandbox_reason()
    if reason is not None:
        pytest.skip(reason)
    monkeypatch.setattr(sandbox, "SANDBOX_EXEC", REAL_SANDBOX_EXEC)


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    path = tmp_path / "wt"
    path.mkdir()
    return path


@pytest.mark.usefixtures("real_sandbox")
def test_the_real_profile_starts(dirs: dict[str, Path]) -> None:
    assert sandbox.check(executable=REAL_SANDBOX_EXEC, **dirs) is None


@pytest.mark.usefixtures("real_sandbox")
def test_a_real_sandboxed_command_writes_the_worktree_and_keeps_its_code(
    worktree: Path, log_path: Path
) -> None:
    assert gate.run_command("echo hi > out.txt; exit 3", worktree, log_path) == 3
    assert (worktree / "out.txt").read_text(encoding="utf-8") == "hi\n"


@pytest.mark.usefixtures("real_sandbox")
def test_a_real_sandboxed_command_writes_its_tmpdir(
    worktree: Path, log_path: Path
) -> None:
    command = (
        f'echo x > "$TMPDIR/f" && cat "$TMPDIR/f" > copy.txt && '
        f'{sys.executable} -c "import tempfile; tempfile.mkstemp()"'
    )
    assert gate.run_command(command, worktree, log_path) == 0
    assert (worktree / "copy.txt").read_text(encoding="utf-8") == "x\n"


@pytest.mark.usefixtures("real_sandbox")
def test_a_real_sandboxed_command_cannot_write_outside(
    tmp_path: Path, worktree: Path, log_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    assert gate.run_command(f"touch {outside}/escape", worktree, log_path) != 0
    assert not (outside / "escape").exists()


@pytest.fixture
def listener() -> Iterator[int]:
    server = socket.create_server(("127.0.0.1", 0))
    server.settimeout(10)

    def accept() -> None:
        while True:
            try:
                connection, _ = server.accept()
            except OSError:
                return
            connection.close()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield server.getsockname()[1]
    server.close()


@pytest.mark.usefixtures("real_sandbox")
def test_a_real_sandboxed_command_has_no_network(
    worktree: Path, log_path: Path, listener: int
) -> None:
    connect = (
        f'{sys.executable} -c "import socket; '
        f"socket.create_connection(('127.0.0.1', {listener}), timeout=5).close()\""
    )
    assert gate.run_command(connect, worktree, log_path, sandbox=False) == 0
    assert gate.run_command(connect, worktree, log_path) != 0


SECRET_PATHS = [
    ".orch/secrets/token",
    ".orch/config",
    ".ssh/id_test",
    ".config/gh/hosts.yml",
    ".codex/auth.json",
    ".claude/settings.json",
    ".claude.json",
]


@pytest.fixture
def decoy_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "decoy-home"
    for name in [*SECRET_PATHS, "plain.txt"]:
        path = home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"decoy {name}\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.mark.usefixtures("real_sandbox")
def test_a_real_sandboxed_command_reads_ordinary_home_files(
    decoy_home: Path, worktree: Path, log_path: Path
) -> None:
    command = f"cat {decoy_home}/plain.txt > copy.txt"
    assert gate.run_command(command, worktree, log_path) == 0
    assert (worktree / "copy.txt").read_text(encoding="utf-8") == "decoy plain.txt\n"


@pytest.mark.parametrize("name", SECRET_PATHS)
@pytest.mark.usefixtures("real_sandbox")
def test_a_real_sandboxed_command_cannot_read_credentials(
    decoy_home: Path, worktree: Path, log_path: Path, name: str
) -> None:
    command = f"cat {decoy_home / name} > copy.txt"
    assert gate.run_command(command, worktree, log_path) != 0
    assert "decoy" not in (worktree / "copy.txt").read_text(encoding="utf-8")


@pytest.mark.usefixtures("real_sandbox")
def test_a_real_sandboxed_command_cannot_list_the_secrets_directory(
    decoy_home: Path, worktree: Path, log_path: Path
) -> None:
    command = f"ls {decoy_home}/.orch/secrets"
    assert gate.run_command(command, worktree, log_path) != 0


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    done = subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@e", *args],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return done.stdout.strip()


@pytest.fixture
def linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """A linked worktree whose git directory lives in the main clone's .git."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-qm", "a")
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", "--detach", str(linked))
    return repo / ".git", linked


@pytest.mark.usefixtures("real_sandbox")
def test_a_real_sandboxed_git_reads_a_linked_worktree(
    linked_worktree: tuple[Path, Path], log_path: Path
) -> None:
    _, linked = linked_worktree
    (linked / "new.txt").write_text("n\n", encoding="utf-8")
    command = "git status --short > status.txt && git log -1 --format=%s > log.txt"
    assert gate.run_command(command, linked, log_path) == 0
    assert "new.txt" in (linked / "status.txt").read_text(encoding="utf-8")
    assert (linked / "log.txt").read_text(encoding="utf-8") == "a\n"


@pytest.mark.parametrize(
    "command",
    [
        "git config orchprobe.value 1",
        "touch {common}/hooks/orch-probe",
        "echo >> {common}/config",
        "touch new.txt && git add new.txt",
    ],
)
@pytest.mark.usefixtures("real_sandbox")
def test_a_real_sandboxed_command_cannot_write_the_shared_git_directory(
    linked_worktree: tuple[Path, Path], log_path: Path, command: str
) -> None:
    common, linked = linked_worktree
    before = (common / "config").read_text(encoding="utf-8")
    code = gate.run_command(command.format(common=common), linked, log_path)
    assert code != 0
    assert (common / "config").read_text(encoding="utf-8") == before
    assert not (common / "hooks" / "orch-probe").exists()
    assert "new.txt" not in git(linked, "ls-files")


@pytest.mark.usefixtures("real_sandbox")
def test_a_real_sandboxed_command_can_run_ps(worktree: Path, log_path: Path) -> None:
    command = "ps -o pid= -p $$ > ps.txt && ps -axo pid=,ppid= > all.txt"
    assert gate.run_command(command, worktree, log_path) == 0
    assert (worktree / "ps.txt").read_text(encoding="utf-8").strip()
    assert len((worktree / "all.txt").read_text(encoding="utf-8").splitlines()) > 1


@pytest.mark.usefixtures("real_sandbox")
def test_a_real_sandboxed_command_cannot_reach_the_keychain(
    worktree: Path, log_path: Path
) -> None:
    command = "security default-keychain"
    assert gate.run_command(command, worktree, log_path, sandbox=False) == 0
    assert gate.run_command(command, worktree, log_path) != 0


@pytest.mark.usefixtures("real_sandbox")
def test_a_real_sandboxed_command_keeps_process_group_signals(
    worktree: Path, log_path: Path
) -> None:
    command = (
        f'{sys.executable} -c "import os, signal, subprocess; '
        "p = subprocess.Popen(['sleep', '30'], start_new_session=True); "
        "os.killpg(p.pid, signal.SIGTERM); "
        'raise SystemExit(0 if p.wait() == -15 else 1)"'
    )
    assert gate.run_command(command, worktree, log_path) == 0


def test_sandbox_env_reports_this_process() -> None:
    """The helper must not report a sandbox where there is none, or skips hide tests."""
    reason = real_sandbox_reason()
    if sys.platform != "darwin":
        assert reason is not None
        return
    probe = subprocess.run(
        [REAL_SANDBOX_EXEC, "-p", "(version 1)(allow default)", "/usr/bin/true"],
        capture_output=True,
    )
    assert (reason is None) == (probe.returncode == 0)
    assert os.path.exists(REAL_SANDBOX_EXEC)
