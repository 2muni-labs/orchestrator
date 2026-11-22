"""The real sandbox-exec and gate profile, on macOS outside any other sandbox.

Every test here takes the autouse real_sandbox fixture, so all of them skip inside a
sandbox and none skip outside one. The main agent runs the full suite through
orch sandbox run, where exactly these tests skip, then runs this file and
tests/integration/test_sandbox_gate.py outside the sandbox.
"""

import os
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from orch import gate, sandbox
from sandbox_env import REAL_SANDBOX_EXEC, real_sandbox_reason


@pytest.fixture(autouse=True)
def real_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    reason = real_sandbox_reason()
    if reason is not None:
        pytest.skip(reason)
    monkeypatch.setattr(sandbox, "SANDBOX_EXEC", REAL_SANDBOX_EXEC)


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


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    path = tmp_path / "wt"
    path.mkdir()
    return path


def test_the_real_profile_starts(dirs: dict[str, Path]) -> None:
    assert sandbox.check(executable=REAL_SANDBOX_EXEC, **dirs) is None


def test_a_real_sandboxed_command_writes_the_worktree_and_keeps_its_code(
    worktree: Path, log_path: Path
) -> None:
    assert gate.run_command("echo hi > out.txt; exit 3", worktree, log_path) == 3
    assert (worktree / "out.txt").read_text(encoding="utf-8") == "hi\n"


def test_a_real_sandboxed_command_writes_its_tmpdir(
    worktree: Path, log_path: Path
) -> None:
    command = (
        f'echo x > "$TMPDIR/f" && cat "$TMPDIR/f" > copy.txt && '
        f'{sys.executable} -c "import tempfile; tempfile.mkstemp()"'
    )
    assert gate.run_command(command, worktree, log_path) == 0
    assert (worktree / "copy.txt").read_text(encoding="utf-8") == "x\n"


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
    ".git-credentials",
    ".config/git/credentials",
    ".netrc",
    ".aws/credentials",
    ".docker/config.json",
    ".npmrc",
    ".pypirc",
    ".kube/config",
    ".gnupg/private-keys-v1.d/key",
    ".config/gcloud/credentials.db",
    ".azure/msal_token_cache.json",
    ".config/hub",
    ".config/glab-cli/config.yml",
    ".cargo/credentials",
    ".cargo/credentials.toml",
    ".gem/credentials",
    ".terraform.d/credentials.tfrc.json",
    ".vault-token",
    ".pgpass",
    ".boto",
    ".s3cfg",
    ".password-store/site.gpg",
    ".config/op/config",
    ".huggingface/token",
    ".cache/huggingface/token",
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


def test_a_real_sandboxed_command_reads_ordinary_home_files(
    decoy_home: Path, worktree: Path, log_path: Path
) -> None:
    command = f"cat {decoy_home}/plain.txt > copy.txt"
    assert gate.run_command(command, worktree, log_path) == 0
    assert (worktree / "copy.txt").read_text(encoding="utf-8") == "decoy plain.txt\n"


@pytest.mark.parametrize("name", SECRET_PATHS)
def test_a_real_sandboxed_command_cannot_read_credentials(
    decoy_home: Path, worktree: Path, log_path: Path, name: str
) -> None:
    command = f"cat {decoy_home / name} > copy.txt"
    assert gate.run_command(command, worktree, log_path) != 0
    assert "decoy" not in (worktree / "copy.txt").read_text(encoding="utf-8")


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


def test_a_real_sandboxed_command_can_run_ps(worktree: Path, log_path: Path) -> None:
    command = "ps -o pid= -p $$ > ps.txt && ps -axo pid=,ppid= > all.txt"
    assert gate.run_command(command, worktree, log_path) == 0
    assert (worktree / "ps.txt").read_text(encoding="utf-8").strip()
    assert len((worktree / "all.txt").read_text(encoding="utf-8").splitlines()) > 1


def test_a_real_sandboxed_command_cannot_reach_the_keychain(
    worktree: Path, log_path: Path
) -> None:
    command = "security default-keychain"
    assert gate.run_command(command, worktree, log_path, sandbox=False) == 0
    assert gate.run_command(command, worktree, log_path) != 0


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


@pytest.mark.parametrize(
    "command",
    ["echo x >> .git", "mv .git moved", "rm .git", "touch .venv/probe"],
)
def test_a_real_sandboxed_command_cannot_change_the_git_file_or_venv(
    linked_worktree: tuple[Path, Path], log_path: Path, command: str
) -> None:
    """orch runs git through the .git file outside the sandbox, and later commands
    run code from .venv."""
    _, linked = linked_worktree
    original = (linked / ".git").read_bytes()
    (linked / ".venv").mkdir()
    assert gate.run_command(command, linked, log_path) != 0
    assert (linked / ".git").read_bytes() == original
    assert not (linked / ".venv" / "probe").exists()
    assert not (linked / "moved").exists()


def test_a_real_sandboxed_command_reads_the_venv(
    linked_worktree: tuple[Path, Path], log_path: Path
) -> None:
    _, linked = linked_worktree
    (linked / ".venv").mkdir()
    (linked / ".venv" / "marker.txt").write_text("venv\n", encoding="utf-8")
    command = "cat .venv/marker.txt > copy.txt"
    assert gate.run_command(command, linked, log_path) == 0
    assert (linked / "copy.txt").read_text(encoding="utf-8") == "venv\n"


SPAWN_TOOLS = [
    "/usr/bin/open -h",
    "/usr/bin/osascript -e 'return 1'",
    "/bin/launchctl list",
]


@pytest.mark.parametrize("command", SPAWN_TOOLS)
def test_a_real_sandboxed_command_cannot_exec_the_spawn_tools(
    worktree: Path, log_path: Path, command: str
) -> None:
    """These start processes outside the sandbox through launchd or LaunchServices.
    The arguments do nothing harmful in case the denial ever stops working."""
    assert gate.run_command(command, worktree, log_path) == 126


LOOKUP = """\
import ctypes, sys
libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
port = ctypes.c_uint.in_dll(libc, "bootstrap_port")
out = ctypes.c_uint(0)
print(libc.bootstrap_look_up(port, sys.argv[1].encode(), ctypes.byref(out)))
"""
BOOTSTRAP_NOT_PRIVILEGED = 1100
LAUNCH_SERVICES = [
    "com.apple.coreservices.launchservicesd",
    "com.apple.coreservices.quarantine-resolver",
    "com.apple.lsd.mapdb",
    "com.apple.lsd.modifydb",
    "com.apple.lsd.open",
    "com.apple.lsd.xpc",
]


def look_up(worktree: Path, log_path: Path, name: str, *, sandboxed: bool) -> int:
    """Return bootstrap_look_up's code for a service, asked from a gate command."""
    script = worktree / "lookup.py"
    script.write_text(LOOKUP, encoding="utf-8")
    command = f"{sys.executable} -I lookup.py {name} > code.txt"
    assert gate.run_command(command, worktree, log_path, sandbox=sandboxed) == 0
    return int((worktree / "code.txt").read_text(encoding="utf-8"))


def test_the_lookup_probe_reaches_launch_services_outside_the_sandbox(
    worktree: Path, log_path: Path
) -> None:
    name = "com.apple.coreservices.launchservicesd"
    assert look_up(worktree, log_path, name, sandboxed=False) == 0


def test_a_real_sandboxed_command_still_looks_up_other_services(
    worktree: Path, log_path: Path
) -> None:
    name = "com.apple.system.notification_center"
    assert look_up(worktree, log_path, name, sandboxed=True) == 0


@pytest.mark.parametrize("name", LAUNCH_SERVICES)
def test_a_real_sandboxed_command_cannot_reach_launch_services(
    worktree: Path, log_path: Path, name: str
) -> None:
    """Calling LSOpen directly needs these services, so it fails without the tool."""
    code = look_up(worktree, log_path, name, sandboxed=True)
    assert code == BOOTSTRAP_NOT_PRIVILEGED


def test_a_real_sandbox_run_keeps_the_gate_profile(
    linked_worktree: tuple[Path, Path], tmp_path: Path, log_path: Path
) -> None:
    """orch sandbox run uses the same profile: no writes outside, no .git rewrite."""
    _, linked = linked_worktree
    outside = tmp_path / "outside"
    outside.mkdir()
    escape = gate.run_sandboxed(["touch", str(outside / "escape")], linked, log_path)
    assert escape.error == "command_failed"
    assert not (outside / "escape").exists()
    rewrite = gate.run_sandboxed(["bash", "-c", "echo x >> .git"], linked, log_path)
    assert rewrite.error == "command_failed"
    assert rewrite.changed == ()
