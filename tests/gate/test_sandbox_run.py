"""gate.run_sandboxed: the main agent's checks under the gate sandbox profile."""

import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from orch import gate, sandbox
from orch.gate import GateError, SandboxRunReport, run_sandboxed
from orch.gitops import create_worktree

SECRET = "contents-that-must-not-leak"


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def write(path: Path, text: str = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture(autouse=True)
def isolated_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "gitconfig"
    config.write_text(
        "[user]\n\tname = t\n\temail = t@example.com\n[init]\n\tdefaultBranch = main\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    write(path / ".gitignore", "__pycache__/\n.pytest_cache/\n")
    write(path / "base.txt")
    git(path, "add", ".")
    git(path, "commit", "-qm", "base")
    return path


@pytest.fixture
def worktree(repo: Path, tmp_path: Path) -> Path:
    """A linked worktree, as the main agent's harness and combined worktrees are."""
    path = tmp_path / "wt"
    create_worktree(repo, path, "task/check", "main")
    return path


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "logs" / "nested" / "sandbox-run.log"


def py(code: str, *args: str) -> list[str]:
    return [sys.executable, "-c", code, *args]


def record_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []
    original = gate.run_command

    def spy(command: str, worktree: Path, log_path: Path, **kwargs: object) -> object:
        calls.append((command, dict(kwargs)))
        return original(command, worktree, log_path, **kwargs)

    monkeypatch.setattr(gate, "run_command", spy)
    return calls


# Passing and failing commands


def test_a_passing_command_gives_an_ok_report(worktree: Path, log_path: Path) -> None:
    report = run_sandboxed(py("print('hello from the check')"), worktree, log_path)
    assert report == SandboxRunReport(
        ok=True,
        error=None,
        exit_code=0,
        detail=None,
        changed=(),
        log_path=str(log_path),
    )
    assert "hello from the check" in log_path.read_text(encoding="utf-8")


def test_the_log_starts_with_the_quoted_command(worktree: Path, log_path: Path) -> None:
    argv = py("print(1)")
    run_sandboxed(argv, worktree, log_path)
    assert log_path.read_text(encoding="utf-8").startswith(f"$ {shlex.join(argv)}\n")


def test_arguments_reach_the_command_unchanged(
    tmp_path: Path, worktree: Path, log_path: Path
) -> None:
    out = tmp_path / "args.txt"
    tricky = ["a b", "'quoted'", '"double"', "$HOME", "semi;colon", "*"]
    code = "import sys; open(sys.argv[1], 'w').write('|'.join(sys.argv[2:]))"
    report = run_sandboxed(py(code, str(out), *tricky), worktree, log_path)
    assert report.ok is True
    assert out.read_text(encoding="utf-8") == "|".join(tricky)


def test_the_command_runs_in_the_worktree(
    tmp_path: Path, worktree: Path, log_path: Path
) -> None:
    out = tmp_path / "cwd.txt"
    code = "import os, sys; open(sys.argv[1], 'w').write(os.getcwd())"
    run_sandboxed(py(code, str(out)), worktree, log_path)
    assert Path(out.read_text(encoding="utf-8")).resolve() == worktree.resolve()


def test_a_failing_command_reports_its_code(worktree: Path, log_path: Path) -> None:
    argv = py("raise SystemExit(3)")
    report = run_sandboxed(argv, worktree, log_path)
    assert report.ok is False
    assert report.error == "command_failed"
    assert report.exit_code == 3
    assert report.detail == f"종료 코드 3: {shlex.join(argv)}"[: gate.DETAIL_LIMIT]
    assert report.changed == ()


def test_a_timeout_is_reported(worktree: Path, log_path: Path) -> None:
    argv = ["sleep", "30"]
    report = run_sandboxed(argv, worktree, log_path, timeout_sec=1)
    assert report.ok is False
    assert report.error == "timeout"
    assert report.exit_code is None
    assert report.detail == f"시간 초과: {shlex.join(argv)}"


def test_a_long_detail_is_limited(worktree: Path, log_path: Path) -> None:
    report = run_sandboxed(py("raise SystemExit(1)", "x" * 2000), worktree, log_path)
    assert report.detail is not None
    assert len(report.detail) <= gate.DETAIL_LIMIT


# The sandbox


def test_the_command_goes_through_the_gate_sandbox_with_the_limit(
    worktree: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = record_calls(monkeypatch)
    argv = py("print(1)")
    run_sandboxed(argv, worktree, log_path, timeout_sec=77)
    assert calls == [(shlex.join(argv), {"timeout_sec": 77})]


def test_without_a_limit_the_gate_default_applies(
    worktree: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = record_calls(monkeypatch)
    run_sandboxed(py("print(1)"), worktree, log_path)
    assert calls == [(shlex.join(py("print(1)")), {"timeout_sec": None})]


def test_a_sandbox_that_cannot_start_fails_closed(
    tmp_path: Path, worktree: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "SANDBOX_EXEC", str(tmp_path / "missing" / "sx"))
    marker = tmp_path / "ran"
    report = run_sandboxed(["touch", str(marker)], worktree, log_path)
    assert report.ok is False
    assert report.error == "sandbox_failed"
    assert report.exit_code is None
    assert report.detail is not None
    assert report.detail.startswith("샌드박스 실패: ")
    assert len(report.detail) <= gate.DETAIL_LIMIT
    assert not marker.exists()


# Changes to the worktree


def test_a_change_fails_the_run_with_paths_only(worktree: Path, log_path: Path) -> None:
    code = f"open('base.txt', 'a').write({SECRET!r})"
    report = run_sandboxed(py(code), worktree, log_path)
    assert report.ok is False
    assert report.error == "worktree_changed"
    assert report.changed == ("base.txt",)
    assert report.detail == gate.CHANGED_DETAIL + "base.txt"
    assert report.exit_code == 0
    assert SECRET not in (report.detail or "")


def test_a_change_wins_over_a_failing_command(worktree: Path, log_path: Path) -> None:
    code = "open('new.txt', 'w').write('x'); raise SystemExit(2)"
    report = run_sandboxed(py(code), worktree, log_path)
    assert report.error == "worktree_changed"
    assert report.changed == ("new.txt",)
    assert report.exit_code == 2


def test_a_change_wins_over_a_timeout(worktree: Path, log_path: Path) -> None:
    argv = ["bash", "-c", "echo y >> base.txt; sleep 30"]
    report = run_sandboxed(argv, worktree, log_path, timeout_sec=1)
    assert report.error == "worktree_changed"
    assert report.changed == ("base.txt",)
    assert report.exit_code is None


def test_several_changes_are_sorted_and_limited(worktree: Path, log_path: Path) -> None:
    argv = ["bash", "-c", "for i in $(seq 1 300); do echo x > f$i.txt; done"]
    report = run_sandboxed(argv, worktree, log_path)
    assert report.error == "worktree_changed"
    assert len(report.changed) == 300
    assert list(report.changed) == sorted(report.changed)
    assert report.detail is not None
    assert report.detail.startswith(gate.CHANGED_DETAIL + "f1.txt, f10.txt")
    assert len(report.detail) <= gate.DETAIL_LIMIT


def test_ignored_files_do_not_count(worktree: Path, log_path: Path) -> None:
    argv = ["bash", "-c", "mkdir -p .pytest_cache __pycache__ && touch .pytest_cache/v"]
    assert run_sandboxed(argv, worktree, log_path).ok is True


def test_work_present_before_the_run_does_not_count(
    worktree: Path, log_path: Path
) -> None:
    write(worktree / "pending.py")
    write(worktree / "base.txt", "edited before the check\n")
    assert run_sandboxed(py("print(1)"), worktree, log_path).ok is True


def test_a_tampered_git_file_is_restored_and_reported(
    tmp_path: Path, worktree: Path, log_path: Path
) -> None:
    original = (worktree / ".git").read_bytes()
    target = tmp_path / "elsewhere"
    argv = ["bash", "-c", f"printf 'gitdir: {target}\\n' > .git"]
    report = run_sandboxed(argv, worktree, log_path)
    assert report.error == "worktree_changed"
    assert report.changed == (".git",)
    assert (worktree / ".git").read_bytes() == original


# Refusals


def test_an_empty_command_is_refused(worktree: Path, log_path: Path) -> None:
    with pytest.raises(GateError):
        run_sandboxed([], worktree, log_path)
    assert not log_path.exists()


def test_the_main_working_tree_is_refused_without_running(
    tmp_path: Path, repo: Path, log_path: Path
) -> None:
    marker = tmp_path / "ran"
    with pytest.raises(GateError):
        run_sandboxed(["touch", str(marker)], repo, log_path)
    assert not marker.exists()


def test_a_plain_directory_is_refused_without_running(
    tmp_path: Path, log_path: Path
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    marker = tmp_path / "ran"
    with pytest.raises(GateError):
        run_sandboxed(["touch", str(marker)], plain, log_path)
    assert not marker.exists()
