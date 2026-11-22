import json
from pathlib import Path

import pytest

from orch import launcher
from orch.config import Target
from orch.launcher import (
    AGENT_KEYS,
    MAIN_MODEL,
    LaunchError,
    agents_json,
    build_plan,
    load_agents,
    parse_agent,
    parse_claude_version,
    selfcheck_warnings,
)
from orch.selfcheck import CurrentCheck


def test_pinned_values() -> None:
    assert MAIN_MODEL == "claude-opus-5-5"


def test_the_fixed_version_check_is_gone() -> None:
    for name in (
        "PINNED_CLAUDE_VERSION",
        "probe_claude_version",
        "version_warning",
        "VersionRunner",
    ):
        assert not hasattr(launcher, name)


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


# selfcheck warnings

NO_RECORD = (
    "Claude Code 지금 버전 기록이 없어 새 배분을 멈췄습니다. "
    "selfcheck가 초기 기록을 만들면 풀립니다."
)


def ready_warning(version: str) -> str:
    return (
        f"Claude Code {version} 후보가 검증을 통과했습니다. "
        "전환하려면 사용자가 orch selfcheck --apply를 실행하세요."
    )


OK = CurrentCheck("ok", None, "2.1.319")
CODEX_OK = CurrentCheck("ok", None, "0.160.0")


def claude(current: CurrentCheck) -> dict[str, CurrentCheck]:
    return {"claude": current, "codex": CODEX_OK}


def test_an_ok_check_without_a_candidate_has_no_warning() -> None:
    assert selfcheck_warnings(claude(OK), None) == []


def test_a_ready_candidate_is_announced() -> None:
    assert selfcheck_warnings(claude(OK), "2.1.326") == [ready_warning("2.1.326")]


def test_no_record_warns_that_dispatch_is_paused() -> None:
    assert selfcheck_warnings(claude(CurrentCheck("no_record", None, None)), None) == [
        NO_RECORD
    ]


def test_a_mismatch_names_the_reason() -> None:
    current = CurrentCheck("mismatch", "sha256", "2.1.319")
    assert selfcheck_warnings(claude(current), None) == [
        "Claude Code가 마지막 전환 기록과 다릅니다(sha256). 새 배분을 멈췄습니다."
    ]


def test_an_error_names_the_reason() -> None:
    assert selfcheck_warnings(claude(CurrentCheck("error", "state", None)), None) == [
        "Claude Code 버전 기록을 확인하지 못했습니다(state). 새 배분을 멈췄습니다."
    ]


def test_the_ready_warning_follows_the_pause_warning() -> None:
    current = CurrentCheck("mismatch", "path", "2.1.319")
    assert selfcheck_warnings(claude(current), "2.1.326") == [
        "Claude Code가 마지막 전환 기록과 다릅니다(path). 새 배분을 멈췄습니다.",
        ready_warning("2.1.326"),
    ]


# selfcheck_warnings for Codex (m-selfcheck-codex-cli)

CODEX_PATH_HINT = (
    "~/.orch/config의 [agents] codex가 ~/.orch/bin/codex를 가리키는지 확인하세요."
)


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        (
            CurrentCheck("no_record", None, None),
            [
                "Codex 지금 버전 기록이 없어 새 배분을 멈췄습니다. "
                "selfcheck가 초기 기록을 만들면 풀립니다."
            ],
        ),
        (
            CurrentCheck("mismatch", "sha256", "0.160.0"),
            ["Codex가 마지막 전환 기록과 다릅니다(sha256). 새 배분을 멈췄습니다."],
        ),
        (
            CurrentCheck("mismatch", "path", "0.160.0"),
            [
                "Codex가 마지막 전환 기록과 다릅니다(path). 새 배분을 멈췄습니다.",
                CODEX_PATH_HINT,
            ],
        ),
        (
            CurrentCheck("error", "not_configured", None),
            [
                "Codex 버전 기록을 확인하지 못했습니다(not_configured). "
                "새 배분을 멈췄습니다."
            ],
        ),
    ],
)
def test_codex_warnings(current: CurrentCheck, expected: list[str]) -> None:
    assert selfcheck_warnings({"claude": OK, "codex": current}, None) == expected


def test_claude_comes_first_and_the_ready_warning_last() -> None:
    currents = {
        "codex": CurrentCheck("mismatch", "path", "0.160.0"),
        "claude": CurrentCheck("no_record", None, None),
    }
    assert selfcheck_warnings(currents, "2.1.326") == [
        NO_RECORD,
        "Codex가 마지막 전환 기록과 다릅니다(path). 새 배분을 멈췄습니다.",
        CODEX_PATH_HINT,
        ready_warning("2.1.326"),
    ]


def test_a_missing_agent_is_skipped() -> None:
    only_codex = {"codex": CurrentCheck("no_record", None, None)}
    assert selfcheck_warnings(only_codex, None)[0].startswith("Codex ")
    assert selfcheck_warnings({}, None) == []


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
        "selfcheck_warnings": [],
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


def test_plan_puts_selfcheck_warnings_before_dirty(layout: dict[str, Path]) -> None:
    result = plan(layout, selfcheck_warnings=["one", "two"], dirty=True)
    assert len(result.warnings) == 3
    assert result.warnings[:2] == ("one", "two")
    assert result.warnings[2] not in ("one", "two")
    assert result.warnings[2]


def test_plan_passes_selfcheck_warnings(layout: dict[str, Path]) -> None:
    result = plan(layout, selfcheck_warnings=[NO_RECORD])
    assert result.warnings == (NO_RECORD,)


def test_plan_warns_only_on_dirty(layout: dict[str, Path]) -> None:
    result = plan(layout, dirty=True)
    assert len(result.warnings) == 1
    assert result.warnings[0] != NO_RECORD


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
    plan(layout, selfcheck_warnings=[NO_RECORD], dirty=True)
    assert sorted(p for p in tmp_path.rglob("*")) == before
    assert not layout["orch_home"].exists()


def test_the_real_hub_settings_file_exists() -> None:
    hub = Path(__file__).resolve().parents[2]
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
    hub = Path(__file__).resolve().parents[2]
    agents = load_agents(hub / "config" / "agents")
    assert sorted(agents) == ["orch-explorer", "orch-reviewer"]
    for definition in agents.values():
        assert definition["tools"] == ["Read", "Grep", "Glob"]
        assert isinstance(definition["prompt"], str)
        assert definition["prompt"]
    assert agents["orch-explorer"]["model"] == "haiku"
    assert agents["orch-reviewer"]["model"] == "sonnet"
    assert (hub / "config" / "main.md").read_text().strip()
