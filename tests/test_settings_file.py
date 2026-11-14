"""config/settings.json: the hooks and deny rules that orch open injects.

Claude Code 2.1.319 blocks a PreToolUse tool call only on exit code 2, or on
exit code 0 with a deny decision. Any other exit code, a crash, a missing
command or a timeout lets the command run (measured 2026-11-07). The pre-bash
command therefore turns every other exit code into 2.
"""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

HUB = Path(__file__).resolve().parents[1]
SETTINGS = json.loads((HUB / "config" / "settings.json").read_text(encoding="utf-8"))
DENY = (
    '{"hookSpecificOutput": {"hookEventName": "PreToolUse", '
    '"permissionDecision": "deny", "permissionDecisionReason": "x"}}'
)


def commands(event: str) -> list[str]:
    return [
        hook["command"]
        for group in SETTINGS["hooks"][event]
        for hook in group["hooks"]
        if hook["type"] == "command"
    ]


def test_the_hooks_call_orch_hook() -> None:
    assert commands("SessionStart") == ["orch hook session-start"]
    assert commands("PreToolUse") == ["orch hook pre-bash || exit 2"]
    assert commands("SubagentStop") == ["orch hook subagent-stop"]
    assert commands("Notification") == ["orch hook notification"]


def test_pre_bash_matches_only_bash() -> None:
    assert [group["matcher"] for group in SETTINGS["hooks"]["PreToolUse"]] == ["Bash"]


def test_secrets_are_denied_to_the_file_tools() -> None:
    assert SETTINGS["permissions"]["deny"] == [
        f"{tool}(~/.orch/secrets/**)" for tool in ("Read", "Grep", "Edit")
    ]


def run_pre_bash(
    tmp_path: Path, script: str | None
) -> subprocess.CompletedProcess[str]:
    """Run the pre-bash command through sh with a fake orch, or with none."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if script is not None:
        fake = bin_dir / "orch"
        fake.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    (command,) = commands("PreToolUse")
    return subprocess.run(
        ["/bin/sh", "-c", command],
        input="{}",
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": os.environ.get("HOME", "")},
        cwd=tmp_path,
    )


@pytest.mark.parametrize(
    "script",
    [
        None,  # orch is not on PATH: 127
        "exit 1",  # a Python crash before the hook runs
        "exit 70",
        "kill -9 $$",
        "exit 2",
    ],
)
def test_pre_bash_turns_every_other_failure_into_exit_2(
    tmp_path: Path, script: str | None
) -> None:
    assert run_pre_bash(tmp_path, script).returncode == 2


def test_pre_bash_passes_a_deny_through(tmp_path: Path) -> None:
    process = run_pre_bash(tmp_path, f"echo '{DENY}'")
    assert process.returncode == 0
    assert json.loads(process.stdout) == json.loads(DENY)


def test_pre_bash_passes_an_allow_through(tmp_path: Path) -> None:
    process = run_pre_bash(tmp_path, "exit 0")
    assert (process.returncode, process.stdout) == (0, "")
