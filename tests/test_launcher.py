import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from orch import launcher
from orch.config import Target
from orch.launcher import (
    MAIN_MODEL,
    PINNED_CLAUDE_VERSION,
    LaunchError,
    build_plan,
    parse_claude_version,
    probe_claude_version,
    version_warning,
)


def test_pinned_values() -> None:
    assert PINNED_CLAUDE_VERSION == "2.1.319"
    assert MAIN_MODEL == "claude-opus-5-5"


# version


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("2.1.319 (Claude Code)\n", "2.1.319"),
        ("  2.2.0 (Claude Code)", "2.2.0"),
        ("2.1.319", "2.1.319"),
        ("", None),
        ("Claude Code 2.1.319", None),
        ("2.1 (Claude Code)", None),
        ("v2.1.319 (Claude Code)", None),
    ],
)
def test_parse_claude_version(output: str, expected: str | None) -> None:
    assert parse_claude_version(output) == expected


class Runner:
    def __init__(
        self, result: subprocess.CompletedProcess[str] | BaseException
    ) -> None:
        self.result = result
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def test_probe_runs_version_once() -> None:
    runner = Runner(subprocess.CompletedProcess([], 0, "2.1.319 (Claude Code)\n", ""))
    assert probe_claude_version(Path("/bin/claude"), runner=runner) == "2.1.319"
    assert runner.calls == [["/bin/claude", "--version"]]


@pytest.mark.parametrize(
    "result",
    [
        subprocess.CompletedProcess([], 1, "2.1.319 (Claude Code)\n", "boom"),
        subprocess.CompletedProcess([], 0, "garbage\n", ""),
        FileNotFoundError("claude"),
        PermissionError("claude"),
        subprocess.TimeoutExpired(["claude"], 10),
    ],
)
def test_probe_returns_none_on_any_failure(
    result: subprocess.CompletedProcess[str] | BaseException,
) -> None:
    assert probe_claude_version(Path("/bin/claude"), runner=Runner(result)) is None


def test_default_runner_uses_an_argument_list_with_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_run(argv: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen["argv"] = argv
        seen.update(kwargs)
        return subprocess.CompletedProcess([], 0, "2.1.319 (Claude Code)\n", "")

    monkeypatch.setattr(launcher.subprocess, "run", fake_run)
    assert probe_claude_version(Path("/bin/claude")) == "2.1.319"
    assert list(seen["argv"]) == ["/bin/claude", "--version"]  # type: ignore[call-overload]
    assert seen.get("shell", False) is False
    assert seen["capture_output"] is True
    assert seen["text"] is True
    assert seen["check"] is False
    assert seen["timeout"] == 10


def test_no_warning_for_the_pinned_version() -> None:
    assert version_warning("2.1.319") is None


def test_a_different_version_warns_with_both_versions() -> None:
    warning = version_warning("2.2.0")
    assert warning is not None
    assert "2.1.319" in warning
    assert "2.2.0" in warning


def test_an_unknown_version_warns() -> None:
    warning = version_warning(None)
    assert warning is not None
    assert "2.1.319" in warning


# plan


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    hub = tmp_path / "hub"
    (hub / "config").mkdir(parents=True)
    (hub / "config" / "settings.json").write_text("{}")
    repo = tmp_path / "repo"
    repo.mkdir()
    return {
        "hub": hub,
        "repo": repo,
        "orch_home": tmp_path / "home" / ".orch",
        "claude": tmp_path / "bin" / "claude",
    }


def target(path: Path) -> Target:
    return Target(
        name="orchestrator",
        path=path,
        github="owner/orchestrator",
        default_branch="main",
        setup_command="uv sync --frozen",
        test_command="pytest",
        lint_command="ruff check .",
    )


def plan(layout: dict[str, Path], **overrides: object) -> launcher.LaunchPlan:
    values: dict[str, object] = {
        "target": target(layout["repo"]),
        "hub": layout["hub"],
        "orch_home": layout["orch_home"],
        "default_orch_home": layout["orch_home"],
        "claude": layout["claude"],
        "claude_version": "2.1.319",
        "dirty": False,
    }
    values.update(overrides)
    return build_plan(**values)  # type: ignore[arg-type]


def test_plan_argv_cwd_env(layout: dict[str, Path]) -> None:
    result = plan(layout)
    assert result.argv == (
        str(layout["claude"]),
        "--model",
        "claude-opus-5-5",
        "--settings",
        str(layout["hub"] / "config" / "settings.json"),
        "--add-dir",
        str(layout["orch_home"] / "orchestrator"),
        "--remote-control",
    )
    assert result.cwd == layout["repo"]
    assert dict(result.env) == {
        "ORCH_TARGET": "orchestrator",
        "ORCH_HUB": str(layout["hub"]),
    }
    assert result.warnings == ()


def test_plan_does_not_inject_main_rules_or_agents_before_m16(
    layout: dict[str, Path],
) -> None:
    argv = plan(layout).argv
    assert "--append-system-prompt-file" not in argv
    assert "--append-system-prompt" not in argv
    assert "--agents" not in argv
    assert "--system-prompt" not in argv


def test_plan_sets_orch_home_only_when_not_default(
    layout: dict[str, Path], tmp_path: Path
) -> None:
    other = tmp_path / "sandbox" / "orch"
    result = plan(layout, orch_home=other)
    assert dict(result.env) == {
        "ORCH_TARGET": "orchestrator",
        "ORCH_HUB": str(layout["hub"]),
        "ORCH_HOME": str(other),
    }
    assert result.argv[result.argv.index("--add-dir") + 1] == str(
        other / "orchestrator"
    )


def test_plan_warns_on_version_then_dirty(layout: dict[str, Path]) -> None:
    result = plan(layout, claude_version="2.2.0", dirty=True)
    assert len(result.warnings) == 2
    assert result.warnings[0] == version_warning("2.2.0")
    assert result.warnings[1]
    assert "2.2.0" not in result.warnings[1]


def test_plan_warns_when_the_version_is_unknown(layout: dict[str, Path]) -> None:
    result = plan(layout, claude_version=None)
    assert result.warnings == (version_warning(None),)


def test_plan_warns_only_on_dirty(layout: dict[str, Path]) -> None:
    result = plan(layout, dirty=True)
    assert len(result.warnings) == 1
    assert result.warnings[0] != version_warning(None)


def test_plan_refuses_a_relative_claude(layout: dict[str, Path]) -> None:
    with pytest.raises(LaunchError):
        plan(layout, claude=Path("claude"))


def test_plan_refuses_without_hook_settings(layout: dict[str, Path]) -> None:
    (layout["hub"] / "config" / "settings.json").unlink()
    with pytest.raises(LaunchError):
        plan(layout)


def test_plan_refuses_a_settings_directory(layout: dict[str, Path]) -> None:
    settings = layout["hub"] / "config" / "settings.json"
    settings.unlink()
    settings.mkdir()
    with pytest.raises(LaunchError):
        plan(layout)


def test_plan_refuses_a_missing_target_directory(
    layout: dict[str, Path], tmp_path: Path
) -> None:
    with pytest.raises(LaunchError):
        plan(layout, target=target(tmp_path / "missing"))


def test_plan_writes_nothing(layout: dict[str, Path], tmp_path: Path) -> None:
    before = sorted(p for p in tmp_path.rglob("*"))
    plan(layout, claude_version=None, dirty=True)
    assert sorted(p for p in tmp_path.rglob("*")) == before
    assert not layout["orch_home"].exists()


def test_the_real_hub_settings_file_exists() -> None:
    hub = Path(__file__).resolve().parents[1]
    assert (hub / "config" / "settings.json").is_file()
