import ast
import json
import stat
import sys
from pathlib import Path

import pytest

from orch import lanes
from orch.config import Config, Target
from orch.lanes import base, claude, codex
from orch.lanes.base import LaneCommand, LaneError
from orch.models import Effort, Grade, Lane
from orch.taskfile import TaskSpec

SPEC_TEXT = '+++\nid = "feat-x"\n+++\n\n# Purpose\n\nDo the thing.\n'
LOG_LABEL = "~/.orch/sample/logs/feat-x.log"
THREAD = "01a1a500-74e7-78d2-b6b3-56a6d2a73346"
LIMIT_MESSAGE = "You’ve hit your usage limit. Upgrade to Pro or try again later."
PATTERNS = ("usage limit", "rate limit", "hit your .*limit", "quota")


def make_spec(lane: Lane = Lane.B, **overrides: object) -> TaskSpec:
    values: dict[str, object] = {
        "id": "feat-x",
        "title": "Add the feature",
        "target": "sample",
        "grade": Grade.L3,
        "lane": lane,
        "model": "gpt-6.1-sol" if lane is Lane.B else "claude-sonnet-5-5",
        "effort": Effort.HIGH,
        "timeout_min": 45,
        "modify": ("src/**",),
        "read": ("src/**",),
        "verify": ".venv/bin/python -m pytest tests/test_x.py",
        "depends_on": (),
        "body": "# Purpose\n",
    }
    values.update(overrides)
    return TaskSpec(**values)


def make_target(**overrides: object) -> Target:
    values: dict[str, object] = {
        "name": "sample",
        "path": Path("/", "repo"),
        "github": "owner/sample",
        "default_branch": "main",
        "setup_command": "uv sync --frozen",
        "test_command": "uv run --locked pytest",
        "lint_command": "uv run --locked ruff check .",
    }
    values.update(overrides)
    return Target(**values)


@pytest.fixture
def agents(tmp_path: Path) -> Config:
    return Config(
        codex_path=tmp_path / "bin" / "codex",
        claude_path=tmp_path / "bin" / "claude",
        usage_limit_patterns={"codex": PATTERNS, "claude": PATTERNS},
    )


def build(spec: TaskSpec, config: Config, environ: dict[str, str] | None = None):
    return lanes.build_command(
        spec=spec,
        spec_text=SPEC_TEXT,
        worktree=Path("/", "w", "feat-x"),
        target=make_target(),
        config=config,
        environ=environ if environ is not None else {"PATH": "/usr/bin"},
        log_label=LOG_LABEL,
    )


# Lane B command


def test_lane_b_command_is_exact(agents: Config) -> None:
    command = build(make_spec(), agents)
    assert isinstance(command, LaneCommand)
    assert command.provider == "codex"
    assert command.argv == (
        str(agents.codex_path),
        "exec",
        "--json",
        "-C",
        str(Path("/", "w", "feat-x")),
        "-m",
        "gpt-6.1-sol",
        "-c",
        "model_reasoning_effort=high",
        "--sandbox",
        "workspace-write",
        "-",
    )
    assert command.cwd == Path("/", "w", "feat-x")


def test_lane_b_never_adds_writable_directories(agents: Config) -> None:
    argv = build(make_spec(), agents).argv
    assert "--add-dir" not in argv
    assert not any("dangerously" in arg for arg in argv)


def test_lane_b_requires_an_effort(agents: Config) -> None:
    with pytest.raises(LaneError):
        build(make_spec(effort=None), agents)


# Lane C command


def test_lane_c_command_is_exact(agents: Config) -> None:
    command = build(make_spec(Lane.C), agents)
    assert command.provider == "claude"
    assert command.argv == (
        str(agents.claude_path),
        "-p",
        "--model",
        "claude-sonnet-5-5",
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        "Read,Edit,Write,Grep,Glob,"
        "Bash(uv run --locked pytest:*),"
        "Bash(.venv/bin/python -m pytest tests/test_x.py:*)",
        "--effort",
        "high",
    )
    assert command.cwd == Path("/", "w", "feat-x")


def test_lane_c_without_an_effort_leaves_out_the_option(agents: Config) -> None:
    assert "--effort" not in build(make_spec(Lane.C, effort=None), agents).argv


@pytest.mark.parametrize("verify", ["a)b", "a,b", "a\nb"])
def test_lane_c_refuses_commands_that_break_the_tool_list(
    agents: Config, verify: str
) -> None:
    with pytest.raises(LaneError):
        build(make_spec(Lane.C, verify=verify), agents)


def test_lane_c_refuses_a_test_command_that_breaks_the_tool_list(
    agents: Config,
) -> None:
    with pytest.raises(LaneError):
        lanes.build_command(
            spec=make_spec(Lane.C),
            spec_text=SPEC_TEXT,
            worktree=Path("/", "w"),
            target=make_target(test_command="pytest), Bash(rm:*"),
            config=agents,
            environ={},
            log_label=LOG_LABEL,
        )


# Lanes without an adapter


def test_lane_d_has_no_command(agents: Config) -> None:
    with pytest.raises(LaneError):
        build(make_spec(Lane.D), agents)


def test_lane_d_has_no_limit_check(agents: Config, tmp_path: Path) -> None:
    log = tmp_path / "x.log"
    log.write_text("", encoding="utf-8")
    with pytest.raises(LaneError):
        lanes.hit_usage_limit(
            Lane.D, log, spec_text=SPEC_TEXT, config=agents, environ={}
        )


# Environment


@pytest.mark.parametrize("lane", [Lane.B, Lane.C])
def test_tokens_and_unrelated_variables_do_not_reach_the_agent(
    agents: Config, lane: Lane
) -> None:
    environ = {
        "PATH": "/usr/bin",
        "HOME": "/h",
        "GH_TOKEN": "secret",
        "GITHUB_TOKEN": "secret",
        "ANTHROPIC_API_KEY": "secret",
        "OPENAI_API_KEY": "secret",
        "AWS_SECRET_ACCESS_KEY": "secret",
        "VIRTUAL_ENV": "/venv",
        "UNRELATED": "x",
    }
    env = build(make_spec(lane), agents, environ).env
    assert "secret" not in env.values()
    assert env == {"PATH": "/usr/bin", "HOME": "/h"}


def test_child_env_keeps_what_the_clis_need() -> None:
    environ = {
        "PATH": "p",
        "HOME": "h",
        "USER": "u",
        "LOGNAME": "l",
        "TMPDIR": "t",
        "LANG": "en",
        "LC_ALL": "en",
        "LC_CTYPE": "en",
        "CODEX_HOME": "c",
        "CLAUDE_CONFIG_DIR": "d",
    }
    assert base.child_env(environ) == environ


def test_child_env_does_not_pass_lc_lookalikes() -> None:
    assert base.child_env({"LCX": "1", "LC": "1", "lc_all": "1"}) == {}


def test_fake_variables_pass_only_in_fake_mode() -> None:
    fake = {"ORCH_FAKE_SCENARIO": "success", "ORCH_FAKE_SLEEP": "1"}
    assert base.child_env(fake) == {}
    assert base.child_env({"ORCH_FAKE_AGENTS": "1", **fake}) == {
        "ORCH_FAKE_AGENTS": "1",
        **fake,
    }


def test_child_env_passes_the_fake_python_only_in_fake_mode() -> None:
    assert base.child_env({"ORCH_FAKE_PYTHON": "p"}) == {}
    assert base.child_env({"ORCH_FAKE_AGENTS": "1", "ORCH_FAKE_PYTHON": "p"}) == {
        "ORCH_FAKE_AGENTS": "1",
        "ORCH_FAKE_PYTHON": "p",
    }


# Prompt


@pytest.mark.parametrize("lane", [Lane.B, Lane.C])
def test_prompt_is_the_whole_spec_then_the_common_rules(
    agents: Config, lane: Lane
) -> None:
    prompt = build(make_spec(lane), agents).stdin_text
    assert prompt.startswith(SPEC_TEXT)
    rules = prompt[len(SPEC_TEXT) :]
    assert ".orch-result.json" in rules
    assert "schemas/result.json" in rules
    assert "feat-x" in rules
    assert LOG_LABEL in rules
    for phrase in ("git commit", "background", "tests/"):
        assert phrase in rules


def test_prompt_does_not_put_the_spec_on_the_command_line(agents: Config) -> None:
    command = build(make_spec(), agents)
    assert not any("Do the thing" in arg for arg in command.argv)


# Finding the agent binaries


def test_missing_agent_path_is_refused_without_searching_path(tmp_path: Path) -> None:
    (tmp_path / "codex").write_text("", encoding="utf-8")
    environ = {"PATH": str(tmp_path)}
    with pytest.raises(LaneError):
        base.resolve_agent("codex", None, environ)


def test_configured_path_is_used(tmp_path: Path) -> None:
    assert base.resolve_agent("codex", tmp_path / "x", {}) == tmp_path / "x"


def test_fake_mode_uses_the_hubs_fake_agents(tmp_path: Path) -> None:
    environ = {"ORCH_FAKE_AGENTS": "1", "ORCH_HUB": str(tmp_path)}
    found = base.resolve_agent("claude", tmp_path / "real", environ)
    assert found == tmp_path / "tests" / "fake_agents" / "claude"


@pytest.mark.parametrize(
    "environ", [{"ORCH_FAKE_AGENTS": "1"}, {"ORCH_FAKE_AGENTS": "1", "ORCH_HUB": "rel"}]
)
def test_fake_mode_needs_an_absolute_hub(environ: dict[str, str]) -> None:
    with pytest.raises(LaneError):
        base.resolve_agent("codex", Path("/", "real"), environ)


def test_fake_mode_is_on_only_for_the_value_one(tmp_path: Path) -> None:
    environ = {"ORCH_FAKE_AGENTS": "0", "ORCH_HUB": str(tmp_path)}
    assert base.resolve_agent("codex", tmp_path / "x", environ) == tmp_path / "x"


def test_repository_fake_agents_are_executable() -> None:
    root = Path(__file__).parent / "fake_agents"
    for name in ("codex", "claude"):
        assert (root / name).stat().st_mode & stat.S_IXUSR


# Limit text


def test_limit_patterns_ignore_case() -> None:
    assert base.matches_limit("QUOTA exceeded", PATTERNS)
    assert base.matches_limit(LIMIT_MESSAGE, PATTERNS)
    assert not base.matches_limit("429 Too Many Requests", PATTERNS)
    assert not base.matches_limit(LIMIT_MESSAGE, ())


def test_tail_ignores_the_echoed_spec_and_old_lines() -> None:
    spec = "Mention quota here.\nAnother line\n"
    assert not base.tail_hits_limit(
        "Mention quota here.\nfinished\n", spec_text=spec, patterns=PATTERNS
    )
    old = "quota\n" + "line\n" * base.LOG_TAIL_LINES
    assert not base.tail_hits_limit(old, spec_text=spec, patterns=PATTERNS)
    recent = "line\n" * 30 + LIMIT_MESSAGE + "\n"
    assert base.tail_hits_limit(recent, spec_text=spec, patterns=PATTERNS)


def test_patterns_for_a_provider(agents: Config) -> None:
    assert base.patterns_for(agents, "codex") == PATTERNS
    assert base.patterns_for(agents, "other") == ()


# Codex structured errors


def jsonl(*events: dict[str, object]) -> str:
    return "".join(json.dumps(event) + "\n" for event in events)


def started(thread: str = THREAD) -> dict[str, object]:
    return {"type": "thread.started", "thread_id": thread}


def failed(message: str) -> list[dict[str, object]]:
    return [
        {"type": "turn.started"},
        {"type": "error", "message": message},
        {"type": "turn.failed", "error": {"message": message}},
    ]


def task_complete(info: object = None, message: str = "m") -> dict[str, object]:
    error = None if info is None else {"message": message, "codex_error_info": info}
    return {
        "timestamp": "2026-11-04T05:20:33.098Z",
        "ordinal": 11,
        "type": "event_msg",
        "payload": {"type": "task_complete", "turn_id": "t", "error": error},
    }


def write_rollout(codex_home: Path, *events: dict[str, object]) -> Path:
    day = codex_home / "sessions" / "2026" / "11" / "04"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-2026-11-04T14-20-33-{THREAD}.jsonl"
    path.write_text(jsonl(*events), encoding="utf-8")
    return path


def test_thread_id_comes_from_the_first_started_event() -> None:
    log = "Reading additional input from stdin...\n" + jsonl(
        started(), started("ffffffff")
    )
    assert codex.thread_id_from_log(log) == THREAD


@pytest.mark.parametrize(
    "log",
    [
        "",
        "not json\n",
        jsonl({"type": "turn.started"}),
        jsonl({"type": "thread.started"}),
        jsonl({"type": "thread.started", "thread_id": 5}),
        jsonl({"type": "thread.started", "thread_id": "../../etc/passwd"}),
        jsonl({"type": "thread.started", "thread_id": "*"}),
        "[1, 2]\n",
    ],
)
def test_no_usable_thread_id(log: str) -> None:
    assert codex.thread_id_from_log(log) is None


def test_usage_limit_error_is_structured(tmp_path: Path) -> None:
    write_rollout(tmp_path, task_complete("usage_limit_exceeded"))
    assert codex.structured_limit(jsonl(started()), tmp_path) is True


@pytest.mark.parametrize(
    "info",
    [
        "internal_server_error",
        {"response_too_many_failed_attempts": {"http_status_code": 429}},
    ],
)
def test_other_known_errors_are_not_limits(tmp_path: Path, info: object) -> None:
    write_rollout(tmp_path, task_complete(info))
    assert codex.structured_limit(jsonl(started()), tmp_path) is False


def test_the_last_turn_decides(tmp_path: Path) -> None:
    write_rollout(
        tmp_path,
        task_complete("internal_server_error"),
        task_complete("usage_limit_exceeded"),
    )
    assert codex.structured_limit(jsonl(started()), tmp_path) is True
    write_rollout(
        tmp_path,
        task_complete("usage_limit_exceeded"),
        task_complete("internal_server_error"),
    )
    assert codex.structured_limit(jsonl(started()), tmp_path) is False


@pytest.mark.parametrize(
    "events",
    [
        [],
        [task_complete()],
        [task_complete("other")],
        [{"type": "event_msg", "payload": {"type": "task_started"}}],
        [{"type": "event_msg", "payload": "x"}],
        [{"type": "event_msg", "payload": {"type": "task_complete", "error": "x"}}],
    ],
)
def test_rollout_that_cannot_tell_gives_none(
    tmp_path: Path, events: list[dict[str, object]]
) -> None:
    write_rollout(tmp_path, *events)
    assert codex.structured_limit(jsonl(started()), tmp_path) is None


def test_missing_rollout_or_thread_gives_none(tmp_path: Path) -> None:
    assert codex.structured_limit(jsonl(started()), tmp_path) is None
    write_rollout(tmp_path, task_complete("usage_limit_exceeded"))
    assert codex.structured_limit("", tmp_path) is None


def test_broken_rollout_lines_are_skipped(tmp_path: Path) -> None:
    path = write_rollout(tmp_path, task_complete("usage_limit_exceeded"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{broken\n[1]\n")
    assert codex.structured_limit(jsonl(started()), tmp_path) is True


def test_rollout_with_invalid_utf8_gives_a_verdict_or_none(tmp_path: Path) -> None:
    path = write_rollout(tmp_path)
    path.write_bytes(b"\xff\xfe\n")
    assert codex.structured_limit(jsonl(started()), tmp_path) is None


# Codex judgment with its fallback


def write_log(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "feat-x.log"
    path.write_text(text, encoding="utf-8")
    return path


def judge(tmp_path: Path, text: str, home: Path | None = None) -> bool:
    return codex.hit_usage_limit(
        write_log(tmp_path, text),
        patterns=PATTERNS,
        environ={"CODEX_HOME": str(home if home is not None else tmp_path / "ch")},
    )


def test_structured_limit_wins_over_the_message(tmp_path: Path) -> None:
    home = tmp_path / "ch"
    write_rollout(home, task_complete("usage_limit_exceeded"))
    assert judge(tmp_path, jsonl(started(), *failed("Something odd")), home) is True


def test_structured_non_limit_wins_over_the_message(tmp_path: Path) -> None:
    home = tmp_path / "ch"
    write_rollout(home, task_complete("internal_server_error"))
    assert judge(tmp_path, jsonl(started(), *failed("usage limit")), home) is False


def test_other_error_falls_back_to_the_message(tmp_path: Path) -> None:
    home = tmp_path / "ch"
    write_rollout(home, task_complete("other"))
    log = jsonl(started(), *failed("stream disconnected: usage limit reached"))
    assert judge(tmp_path, log, home) is True


def test_missing_rollout_falls_back_to_error_and_turn_failed_messages(
    tmp_path: Path,
) -> None:
    assert judge(tmp_path, jsonl(started(), *failed(LIMIT_MESSAGE))) is True
    only_error = jsonl(started(), {"type": "error", "message": LIMIT_MESSAGE})
    assert judge(tmp_path, only_error) is True
    only_failed = jsonl(
        started(), {"type": "turn.failed", "error": {"message": LIMIT_MESSAGE}}
    )
    assert judge(tmp_path, only_failed) is True


def test_agent_text_and_tool_output_never_count(tmp_path: Path) -> None:
    log = jsonl(
        started(),
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "usage limit quota rate limit"},
        },
        {
            "type": "item.completed",
            "item": {"type": "command_execution", "aggregated_output": "quota"},
        },
        {"type": "turn.completed", "usage": {}},
    )
    assert judge(tmp_path, log) is False


def test_non_json_tail_lines_count(tmp_path: Path) -> None:
    log = "Reading additional input from stdin...\nERROR: " + LIMIT_MESSAGE + "\n"
    assert judge(tmp_path, log) is True


def test_old_non_json_lines_do_not_count(tmp_path: Path) -> None:
    log = "quota\n" + "line\n" * base.LOG_TAIL_LINES
    assert judge(tmp_path, log) is False


def test_old_error_events_still_count(tmp_path: Path) -> None:
    log = jsonl(started(), *failed(LIMIT_MESSAGE)) + "line\n" * 50
    assert judge(tmp_path, log) is True


def test_malformed_events_do_not_crash(tmp_path: Path) -> None:
    log = jsonl(
        {"type": "error"},
        {"type": "error", "message": 5},
        {"type": "turn.failed"},
        {"type": "turn.failed", "error": "x"},
        {"type": "turn.failed", "error": {"message": None}},
    )
    assert judge(tmp_path, log) is False


def test_missing_log_is_not_a_limit(tmp_path: Path) -> None:
    assert (
        codex.hit_usage_limit(
            tmp_path / "none.log", patterns=PATTERNS, environ={"HOME": str(tmp_path)}
        )
        is False
    )


def test_codex_home_defaults_to_the_home_directory(tmp_path: Path) -> None:
    write_rollout(tmp_path / ".codex", task_complete("usage_limit_exceeded"))
    log = write_log(tmp_path, jsonl(started()))
    assert (
        codex.hit_usage_limit(log, patterns=(), environ={"HOME": str(tmp_path)}) is True
    )


def test_log_with_invalid_utf8_still_gets_judged(tmp_path: Path) -> None:
    path = tmp_path / "x.log"
    path.write_bytes(b"\xff\xfe\n" + LIMIT_MESSAGE.encode() + b"\n")
    assert codex.hit_usage_limit(path, patterns=PATTERNS, environ={}) is True


# Claude judgment


def test_claude_finds_a_limit_at_the_end_of_the_log(tmp_path: Path) -> None:
    log = write_log(tmp_path, "working\n" + LIMIT_MESSAGE + "\n")
    assert claude.hit_usage_limit(log, spec_text=SPEC_TEXT, patterns=PATTERNS) is True


def test_claude_ignores_the_echoed_spec(tmp_path: Path) -> None:
    spec = "The quota rule.\n"
    log = write_log(tmp_path, "The quota rule.\ndone\n")
    assert claude.hit_usage_limit(log, spec_text=spec, patterns=PATTERNS) is False


def test_claude_missing_log_is_not_a_limit(tmp_path: Path) -> None:
    path = tmp_path / "none.log"
    assert claude.hit_usage_limit(path, spec_text="", patterns=PATTERNS) is False


# Routing


def test_router_uses_each_provider_s_patterns(tmp_path: Path) -> None:
    config = Config(
        usage_limit_patterns={"codex": ("only-codex",), "claude": ("only-claude",)}
    )
    log = write_log(tmp_path, "only-claude\n")
    args = {"spec_text": "", "config": config, "environ": {"CODEX_HOME": str(tmp_path)}}
    assert lanes.hit_usage_limit(Lane.C, log, **args) is True
    assert lanes.hit_usage_limit(Lane.B, log, **args) is False


# Conventions


@pytest.mark.parametrize(
    "module", [lanes, base, codex, claude], ids=lambda module: module.__name__
)
def test_modules_import_only_the_standard_library_and_orch(module: object) -> None:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))  # type: ignore[attr-defined]
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module is not None
            imported.add(node.module.split(".")[0])
    assert imported - {"orch"} <= sys.stdlib_module_names
    assert "subprocess" not in imported
