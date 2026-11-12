"""orch gate under the real sandbox-exec, through a real orch process (macOS only)."""

import pytest

from conftest import CHANGED, TEST_SCRIPT, Sandbox
from sandbox_env import real_sandbox_reason

TASK = "sandbox-task"


def test_a_gate_runs_its_commands_under_the_real_sandbox(sandbox: Sandbox) -> None:
    """G2 passes only if writing outside the worktree fails, as in the sandbox."""
    reason = real_sandbox_reason()
    if reason is not None:
        pytest.skip(reason)
    worktree = sandbox.prepare(TASK)
    escape = sandbox.home / "escape"
    sandbox.write(
        worktree / TEST_SCRIPT,
        f'set -e\ntest -f {CHANGED}\n! touch "{escape}"\n',
    )
    sandbox.git(worktree, "add", TEST_SCRIPT)
    sandbox.git(worktree, "commit", "-qm", "Add the tests")
    assert sandbox.ok("dispatch", TASK)["state"] == "reserved"
    data = sandbox.ok("run", TASK)
    assert (data["status"], data["state"]) == ("DONE", "judging"), data
    data = sandbox.ok("gate", TASK, unset=("ORCH_FAKE_SANDBOX_EXEC",))
    assert data["gates"] == {"G1": "passed", "G2": "passed", "G3": "passed"}, data
    assert not escape.exists()


def test_the_same_gate_fails_without_the_sandbox(sandbox: Sandbox) -> None:
    """The control: with the pass-through stand-in, the write succeeds and G2 fails."""
    worktree = sandbox.prepare(TASK)
    escape = sandbox.home / "escape"
    sandbox.write(
        worktree / TEST_SCRIPT,
        f'set -e\ntest -f {CHANGED}\n! touch "{escape}"\n',
    )
    sandbox.git(worktree, "add", TEST_SCRIPT)
    sandbox.git(worktree, "commit", "-qm", "Add the tests")
    assert sandbox.ok("dispatch", TASK)["state"] == "reserved"
    data = sandbox.ok("run", TASK)
    assert (data["status"], data["state"]) == ("DONE", "judging"), data
    result = sandbox.orch("gate", TASK)
    assert result.code == 20, result
    assert result.data["failed_gate"] == "G2"
    assert result.data["state"] == "judging"
    assert sandbox.gate_rows(TASK) == {"G1": True, "G2": False}
    assert escape.exists()
