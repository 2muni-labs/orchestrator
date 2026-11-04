import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from orch.models import Outcome
from orch.results import load_result

AGENTS = Path(__file__).parent / "fake_agents"
SPEC = '+++\nid = "fake-task"\ntarget = "t"\n+++\n\n# Purpose\n'
RESULT = ".orch-result.json"
CHANGED = "src/fake_change.py"
OUTSIDE = "outside/fake_change.py"
TEST_FILE = "tests/test_fake_change.py"


def start(cli: str, scenario: str, worktree: Path) -> subprocess.Popen[str]:
    env = {
        **os.environ,
        "ORCH_FAKE_SCENARIO": scenario,
        "ORCH_FAKE_PYTHON": sys.executable,
    }
    return subprocess.Popen(
        [str(AGENTS / cli), "exec", "-C", str(worktree), "-m", "x", "-"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        start_new_session=True,
    )


def run(cli: str, scenario: str, worktree: Path) -> tuple[int, str]:
    proc = start(cli, scenario, worktree)
    output, _ = proc.communicate(SPEC, timeout=30)
    return proc.returncode, output


@pytest.mark.parametrize("cli", ["codex", "claude"])
def test_wrappers_are_executable_and_report_success(cli: str, tmp_path: Path) -> None:
    assert os.access(AGENTS / cli, os.X_OK)
    code, _ = run(cli, "success", tmp_path)
    assert code == 0
    assert (tmp_path / CHANGED).is_file()
    result = load_result(tmp_path / RESULT, "fake-task")
    assert result.status is Outcome.DONE
    assert result.changed_paths == (CHANGED,)


def test_partial_reports_unresolved_items(tmp_path: Path) -> None:
    assert run("codex", "partial", tmp_path)[0] == 0
    result = load_result(tmp_path / RESULT, "fake-task")
    assert result.status is Outcome.PARTIAL
    assert result.unresolved


def test_report_missing_changes_files_but_writes_no_report(tmp_path: Path) -> None:
    assert run("codex", "report_missing", tmp_path)[0] == 0
    assert (tmp_path / CHANGED).is_file()
    assert not (tmp_path / RESULT).exists()


def test_malformed_report_is_not_json(tmp_path: Path) -> None:
    assert run("codex", "malformed_report", tmp_path)[0] == 0
    with pytest.raises(ValueError):
        json.loads((tmp_path / RESULT).read_text(encoding="utf-8"))


def test_usage_limit_prints_the_message_and_fails(tmp_path: Path) -> None:
    code, output = run("codex", "usage_limit", tmp_path)
    assert code == 1
    assert "usage limit" in output
    assert not (tmp_path / RESULT).exists()


def test_out_of_scope_changes_a_file_outside_and_still_reports_done(
    tmp_path: Path,
) -> None:
    assert run("codex", "out_of_scope", tmp_path)[0] == 0
    assert (tmp_path / OUTSIDE).is_file()
    assert load_result(tmp_path / RESULT, "fake-task").status is Outcome.DONE


def test_test_file_change_changes_a_test_and_still_reports_done(tmp_path: Path) -> None:
    assert run("codex", "test_file_change", tmp_path)[0] == 0
    assert (tmp_path / TEST_FILE).is_file()
    assert load_result(tmp_path / RESULT, "fake-task").status is Outcome.DONE


def test_immediate_exit_does_nothing(tmp_path: Path) -> None:
    code, output = run("codex", "immediate_exit", tmp_path)
    assert code == 1
    assert output == ""
    assert list(tmp_path.iterdir()) == []


def test_killed_dies_by_sigkill(tmp_path: Path) -> None:
    assert run("codex", "killed", tmp_path)[0] == -signal.SIGKILL


def test_unknown_scenario_fails_clearly(tmp_path: Path) -> None:
    code, output = run("codex", "nope", tmp_path)
    assert code == 64
    assert "nope" in output


def wait_until_started(proc: subprocess.Popen[str]) -> None:
    assert proc.stdin is not None
    assert proc.stdout is not None
    proc.stdin.write(SPEC)
    proc.stdin.close()
    assert proc.stdout.readline().strip() == "started"


def test_timeout_keeps_running_until_terminated(tmp_path: Path) -> None:
    proc = start("codex", "timeout", tmp_path)
    wait_until_started(proc)
    assert proc.poll() is None
    os.killpg(proc.pid, signal.SIGTERM)
    assert proc.wait(timeout=10) == -signal.SIGTERM


def test_ignore_term_survives_sigterm_until_sigkill(tmp_path: Path) -> None:
    proc = start("codex", "ignore_term", tmp_path)
    wait_until_started(proc)
    os.killpg(proc.pid, signal.SIGTERM)
    with pytest.raises(subprocess.TimeoutExpired):
        proc.wait(timeout=0.5)
    os.killpg(proc.pid, signal.SIGKILL)
    assert proc.wait(timeout=10) == -signal.SIGKILL


def test_current_directory_is_the_default_worktree(tmp_path: Path) -> None:
    done = subprocess.run(
        [str(AGENTS / "claude"), "-p"],
        input=SPEC,
        text=True,
        cwd=tmp_path,
        env={
            **os.environ,
            "ORCH_FAKE_SCENARIO": "report_missing",
            "ORCH_FAKE_PYTHON": sys.executable,
        },
        check=True,
    )
    assert done.returncode == 0
    assert (tmp_path / CHANGED).is_file()
