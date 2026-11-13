import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from orch import launcher
from orch.config import Target
from orch.launcher import (
    AGENT_KEYS,
    MAIN_MODEL,
    PINNED_CLAUDE_VERSION,
    LaunchError,
    agents_json,
    build_plan,
    load_agents,
    parse_agent,
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


PROBE_AGENT = (
    "---\nname: probe\ndescription: Probe agent.\nmodel: haiku\n"
    'tools: Read, Grep, Glob\n---\n\nSay "it\'s $HOME" twice.\n'
)
PROBE_JSON = {
    "probe": {
        "description": "Probe agent.",
        "prompt": 'Say "it\'s $HOME" twice.',
        "tools": ["Read", "Grep", "Glob"],
        "model": "haiku",
    }
}


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    hub = tmp_path / "hub"
    (hub / "config").mkdir(parents=True)
    (hub / "config" / "settings.json").write_text("{}")
    (hub / "config" / "main.md").write_text("# Main rules\n")
    (hub / "config" / "agents").mkdir()
    (hub / "config" / "agents" / "probe.md").write_text(PROBE_AGENT)
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
        "--append-system-prompt-file",
        str(layout["hub"] / "config" / "main.md"),
        "--agents",
        json.dumps(PROBE_JSON, ensure_ascii=False, sort_keys=True),
        "--add-dir",
        str(layout["orch_home"] / "orchestrator"),
        "--remote-control",
    )
    assert json.loads(result.argv[result.argv.index("--agents") + 1]) == PROBE_JSON
    assert result.cwd == layout["repo"]
    assert dict(result.env) == {
        "ORCH_TARGET": "orchestrator",
        "ORCH_HUB": str(layout["hub"]),
    }
    assert result.warnings == ()


def test_plan_never_replaces_the_system_prompt(layout: dict[str, Path]) -> None:
    argv = plan(layout).argv
    assert "--append-system-prompt" not in argv
    assert "--system-prompt" not in argv
    assert "--system-prompt-file" not in argv
    assert argv.count("--append-system-prompt-file") == 1
    assert argv.count("--agents") == 1


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


# main rules and agent definitions (M16)


@pytest.mark.parametrize("content", [None, "dir", "", " \n\t\n"])
def test_plan_refuses_a_missing_or_empty_main_rules_file(
    layout: dict[str, Path], content: str | None
) -> None:
    main = layout["hub"] / "config" / "main.md"
    main.unlink()
    if content == "dir":
        main.mkdir()
    elif content is not None:
        main.write_text(content)
    with pytest.raises(LaunchError):
        plan(layout)


def test_plan_refuses_unreadable_main_rules(layout: dict[str, Path]) -> None:
    (layout["hub"] / "config" / "main.md").write_bytes(b"\xff\xfe rules")
    with pytest.raises(LaunchError):
        plan(layout)


def test_plan_refuses_a_missing_agents_directory(layout: dict[str, Path]) -> None:
    agents = layout["hub"] / "config" / "agents"
    (agents / "probe.md").unlink()
    agents.rmdir()
    with pytest.raises(LaunchError):
        plan(layout)


def test_plan_refuses_an_agents_file_instead_of_a_directory(
    layout: dict[str, Path],
) -> None:
    agents = layout["hub"] / "config" / "agents"
    (agents / "probe.md").unlink()
    agents.rmdir()
    agents.write_text(PROBE_AGENT)
    with pytest.raises(LaunchError):
        plan(layout)


def test_plan_refuses_an_agents_directory_without_definitions(
    layout: dict[str, Path],
) -> None:
    agents = layout["hub"] / "config" / "agents"
    (agents / "probe.md").unlink()
    (agents / "README.txt").write_text("not a definition\n")
    with pytest.raises(LaunchError):
        plan(layout)


def test_plan_refuses_a_malformed_definition(layout: dict[str, Path]) -> None:
    (layout["hub"] / "config" / "agents" / "probe.md").write_text(
        "---\nname: probe\ndescription: x\ntools: Read\n---\n\n"
    )
    with pytest.raises(LaunchError):
        plan(layout)


def test_plan_writes_nothing_with_definitions(
    layout: dict[str, Path], tmp_path: Path
) -> None:
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    plan(layout)
    after = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before


def test_agent_keys() -> None:
    assert frozenset({"name", "description", "model", "tools"}) == AGENT_KEYS


def test_parse_agent_converts_the_front_matter_and_body() -> None:
    assert parse_agent(PROBE_AGENT, stem="probe") == PROBE_JSON["probe"]


def test_parse_agent_strips_keys_values_and_tool_names() -> None:
    text = (
        "---\n name :  probe \ndescription:  A: colon in value  \n"
        "tools:Read ,Grep,  Glob \n---\n\n\n  Line one.\n\nLine two.  \n\n"
    )
    assert parse_agent(text, stem="probe") == {
        "description": "A: colon in value",
        "prompt": "Line one.\n\nLine two.",
        "tools": ["Read", "Grep", "Glob"],
    }


def test_parse_agent_leaves_out_missing_optional_keys() -> None:
    text = "---\nname: probe\ndescription: Probe.\n---\nBody.\n"
    result = parse_agent(text, stem="probe")
    assert result == {"description": "Probe.", "prompt": "Body."}


def test_parse_agent_keeps_a_single_tool_as_a_list() -> None:
    text = "---\nname: probe\ndescription: Probe.\ntools: Read\n---\nBody.\n"
    assert parse_agent(text, stem="probe")["tools"] == ["Read"]


N = "name: probe\n"
D = "description: x\n"
MALFORMED = {
    "empty": "",
    "no-opening": N + D + "---\nBody\n",
    "indented": " ---\n" + N + D + "---\nBody\n",
    "no-closing": "---\n" + N + D + "Body\n",
    "no-body": "---\n" + N + D + "---\n",
    "blank-body": "---\n" + N + D + "---\n \n\t\n",
    "no-colon": "---\nname probe\n" + D + "---\nBody\n",
    "blank-line": "---\n" + N + "\n" + D + "---\nBody\n",
    "key": "---\n" + N + "color: red\n" + D + "---\nB\n",
    "case": "---\n" + N + "Name: probe\n" + D + "---\nB\n",
    "dup": "---\n" + N + N + D + "---\nB\n",
    "empty-value": "---\n" + N + "description: \n---\nBody\n",
    "empty-name": "---\nname:\n" + D + "---\nBody\n",
    "no-name": "---\n" + D + "---\nBody\n",
    "no-description": "---\n" + N + "---\nBody\n",
    "name-stem": "---\nname: other\n" + D + "---\nBody\n",
    "t1": "---\n" + N + D + "tools: Read,\n---\nB\n",
    "t2": "---\n" + N + D + "tools: ,Read\n---\nB\n",
    "t3": "---\n" + N + D + "tools: A, ,B\n---\nB\n",
}


@pytest.mark.parametrize("text", list(MALFORMED.values()), ids=list(MALFORMED))
def test_parse_agent_refuses_malformed_definitions(text: str) -> None:
    with pytest.raises(LaunchError):
        parse_agent(text, stem="probe")


def test_load_agents_reads_md_files_by_name(tmp_path: Path) -> None:
    (tmp_path / "beta.md").write_text("---\nname: beta\ndescription: B.\n---\nB body\n")
    (tmp_path / "alpha.md").write_text(
        "---\nname: alpha\ndescription: A.\n---\nA body\n"
    )
    (tmp_path / "notes.txt").write_text("ignored")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "gamma.md").write_text("---\nname: gamma\ndescription: G.\n---\nG\n")
    agents = load_agents(tmp_path)
    assert list(agents) == ["alpha", "beta"]
    assert agents == {
        "alpha": {"description": "A.", "prompt": "A body"},
        "beta": {"description": "B.", "prompt": "B body"},
    }


def test_load_agents_refuses_a_directory_named_like_a_definition(
    tmp_path: Path,
) -> None:
    (tmp_path / "alpha.md").write_text("---\nname: alpha\ndescription: A.\n---\nA\n")
    (tmp_path / "beta.md").mkdir()
    with pytest.raises(LaunchError):
        load_agents(tmp_path)


def test_load_agents_refuses_one_bad_definition_among_good_ones(
    tmp_path: Path,
) -> None:
    (tmp_path / "alpha.md").write_text("---\nname: alpha\ndescription: A.\n---\nA\n")
    (tmp_path / "beta.md").write_text("---\nname: alpha\ndescription: B.\n---\nB\n")
    with pytest.raises(LaunchError):
        load_agents(tmp_path)


def test_load_agents_refuses_undecodable_bytes(tmp_path: Path) -> None:
    (tmp_path / "alpha.md").write_bytes(
        b"---\nname: alpha\ndescription: \xff\n---\nA\n"
    )
    with pytest.raises(LaunchError):
        load_agents(tmp_path)


@pytest.mark.parametrize("make", ["missing", "file"])
def test_load_agents_refuses_a_non_directory(tmp_path: Path, make: str) -> None:
    directory = tmp_path / "agents"
    if make == "file":
        directory.write_text("x")
    with pytest.raises(LaunchError):
        load_agents(directory)


def test_load_agents_refuses_an_empty_directory(tmp_path: Path) -> None:
    with pytest.raises(LaunchError):
        load_agents(tmp_path)


def test_agents_json_is_sorted_and_keeps_non_ascii() -> None:
    value = {"b": {"prompt": "한국어", "description": "d"}, "a": {"description": "e"}}
    assert agents_json(value) == json.dumps(value, ensure_ascii=False, sort_keys=True)
    assert "한국어" in agents_json(value)


def test_the_real_hub_agents_convert() -> None:
    hub = Path(__file__).resolve().parents[1]
    agents = load_agents(hub / "config" / "agents")
    assert sorted(agents) == ["orch-explorer", "orch-reviewer"]
    for definition in agents.values():
        assert definition["tools"] == ["Read", "Grep", "Glob"]
        assert isinstance(definition["prompt"], str)
        assert definition["prompt"]
    assert agents["orch-explorer"]["model"] == "haiku"
    assert agents["orch-reviewer"]["model"] == "sonnet"
    assert (hub / "config" / "main.md").read_text().strip()
