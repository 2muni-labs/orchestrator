"""m-claude-model-alias: Claude tier aliases and the models they resolve to (ADR 0032).

Uses the World of test_selfcheck.py and FakeClaude of selfcheck_support.py, whose
alias sessions answer as Claude Code 2.1.330 measured on 2026-11-26: the first
stream-json event is system/init and names the model. Nothing here uses the
network or runs the real claude.
"""

import json
import subprocess
from pathlib import Path

import pytest

import test_selfcheck_switch as switches
from orch import selfcheck
from orch.selfcheck import (
    ALIAS_PROMPT,
    ALIASES,
    SESSION_MODEL,
    SESSION_TIMEOUT_SEC,
    SelfcheckError,
    alias_argv,
    resolve_aliases,
)
from orch.taskfile import TaskFileError, parse_task
from selfcheck_support import RESOLVED, FakeClaude, alias_stream, make_hub
from test_selfcheck import World

ALIAS_KEYS = ["alias-haiku", "alias-sonnet", "alias-opus"]
ENV = {"HOME": "/h", "PATH": selfcheck.SESSION_PATH}


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network used")

    monkeypatch.setattr("urllib.request.urlopen", refuse)


@pytest.fixture
def world(tmp_path: Path) -> World:
    home = tmp_path / "home"
    orch_home = home / ".orch"
    orch_home.mkdir(parents=True)
    return World(tmp_path, make_hub(tmp_path), home, orch_home)


# Constants and argv


def test_constants() -> None:
    assert SESSION_MODEL == "haiku"
    assert ALIASES == ("haiku", "sonnet", "opus")
    assert ALIAS_PROMPT == "Reply with the single word ok."


def test_alias_argv() -> None:
    assert alias_argv(Path("/v/2.1.330"), "opus") == [
        "/v/2.1.330",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        "opus",
        "--max-turns",
        "1",
        "Reply with the single word ok.",
    ]


def test_the_session_argv_uses_the_haiku_alias() -> None:
    argv = selfcheck.session_argv(Path("/v/c"), [], "hi")
    assert argv[argv.index("--model") + 1] == "haiku"


# resolve_aliases


def test_each_alias_resolves_from_its_init_event(tmp_path: Path) -> None:
    claude = FakeClaude()
    assert resolve_aliases(Path("/v/c"), claude, tmp_path, ENV) == RESOLVED
    assert list(RESOLVED) == list(ALIASES)
    assert claude.keys() == ALIAS_KEYS
    for (_, argv, cwd, env, timeout), alias in zip(claude.calls, ALIASES, strict=True):
        assert argv == alias_argv(Path("/v/c"), alias)
        assert cwd == tmp_path
        assert env == ENV
        assert timeout == SESSION_TIMEOUT_SEC


def test_other_lines_before_the_init_event_are_skipped(tmp_path: Path) -> None:
    claude = FakeClaude()
    for alias, model in RESOLVED.items():
        claude.stdout[f"alias-{alias}"] = (
            "not json\n"
            + json.dumps(["list"])
            + "\n"
            + json.dumps({"type": "system", "subtype": "hook_started"})
            + "\n"
            + alias_stream(model.replace("5-5", "6-0"))
        )
    assert resolve_aliases(Path("/v/c"), claude, tmp_path, ENV) == {
        "haiku": "claude-haiku-6-0",
        "sonnet": "claude-sonnet-6-0",
        "opus": "claude-opus-6-0",
    }


def _init_with(model: object) -> str:
    return json.dumps({"type": "system", "subtype": "init", "model": model}) + "\n"


@pytest.mark.parametrize(
    ("knob", "value"),
    [
        ("exit_codes", 1),
        ("raises", OSError("no claude")),
        ("raises", subprocess.TimeoutExpired("claude", 120)),
        ("stdout", ""),
        ("stdout", "not json\n"),
        ("stdout", json.dumps({"type": "result", "result": "ok"}) + "\n"),
        ("stdout", _init_with("")),
        ("stdout", _init_with(None)),
        ("stdout", _init_with(5)),
        ("stdout", json.dumps({"type": "system", "model": "x"}) + "\n"),
    ],
)
def test_a_failed_alias_session_is_none_and_stops(
    tmp_path: Path, knob: str, value: object
) -> None:
    claude = FakeClaude()
    getattr(claude, knob)["alias-sonnet"] = value
    assert resolve_aliases(Path("/v/c"), claude, tmp_path, ENV) is None
    assert claude.keys() == ["alias-haiku", "alias-sonnet"]


# The state format


def _state(claude: dict[str, object], codex: dict[str, object] | None = None) -> str:
    agents: dict[str, object] = {"claude": claude}
    if codex is not None:
        agents["codex"] = codex
    return json.dumps({"format": 1, "agents": agents})


CURRENT = {
    "version": "2.1.330",
    "sha256": "a" * 64,
    "path": "/x/versions/2.1.330",
    "at": "2026-11-26T00:00:00+00:00",
    "source": "apply",
}
CANDIDATE = {
    "version": "2.1.332",
    "status": "failed",
    "sha256": None,
    "path": None,
    "pins_sha256": "b" * 64,
    "checks": [],
    "warnings": [],
    "at": "2026-11-26T00:00:00+00:00",
}


@pytest.mark.parametrize(
    "claude",
    [
        {"current": {**CURRENT, "models": dict(RESOLVED)}},
        {"current": CURRENT, "previous": {**CURRENT, "models": dict(RESOLVED)}},
        {"current": CURRENT, "candidate": {**CANDIDATE, "models": None}},
        {"current": CURRENT, "candidate": {**CANDIDATE, "models": dict(RESOLVED)}},
        {"current": CURRENT, "candidate": CANDIDATE},
    ],
)
def test_valid_models_load(tmp_path: Path, claude: dict[str, object]) -> None:
    path = tmp_path / "agents.json"
    path.write_text(_state(claude), encoding="utf-8")
    assert selfcheck.load_agents_state(path)["agents"]["claude"] == claude  # type: ignore[index]


BAD_MODELS = [
    None,
    "claude-opus-5-5",
    {"haiku": "h", "sonnet": "s"},
    {**RESOLVED, "fable": "f"},
    {**RESOLVED, "opus": ""},
    {**RESOLVED, "opus": 5},
]


@pytest.mark.parametrize("models", BAD_MODELS)
@pytest.mark.parametrize("where", ["current", "previous", "candidate"])
def test_invalid_models_are_refused(tmp_path: Path, where: str, models: object) -> None:
    if where == "candidate" and models is None:
        return  # a candidate may have no models
    claude: dict[str, object] = {"current": CURRENT}
    if where == "candidate":
        claude["candidate"] = {**CANDIDATE, "models": models}
    else:
        claude[where] = {**CURRENT, "models": models}
    path = tmp_path / "agents.json"
    path.write_text(_state(claude), encoding="utf-8")
    with pytest.raises(SelfcheckError):
        selfcheck.load_agents_state(path)


def test_codex_entries_are_not_checked_for_models(tmp_path: Path) -> None:
    codex = {"current": {**CURRENT, "models": "anything"}}
    path = tmp_path / "agents.json"
    path.write_text(_state({"current": CURRENT}, codex), encoding="utf-8")
    selfcheck.load_agents_state(path)


# The current record is filled once


def _without_models(world: World) -> None:
    state = world.state()
    del state["agents"]["claude"]["current"]["models"]  # type: ignore[index]
    world.write_state(state)


def test_a_record_without_models_is_filled_before_the_latest_check(
    world: World,
) -> None:
    world.release.latest("2.1.319\n")
    world.record_current()
    _without_models(world)
    report = world.run()
    assert (report.status, report.reason, report.models) == (
        "up_to_date",
        "latest",
        None,
    )
    assert world.claude_state()["current"]["models"] == RESOLVED  # type: ignore[index]
    assert world.claude.keys() == ALIAS_KEYS
    binary = world.versions / "2.1.319"
    for _, argv, cwd, env, _ in world.claude.calls:
        assert argv[0] == str(binary)
        assert cwd.name.startswith("orch-selfcheck-")
        assert not cwd.exists()
        assert "GH_TOKEN" not in env
        assert env["DISABLE_AUTOUPDATER"] == "1"


def test_the_record_is_filled_even_when_the_latest_is_unknown(world: World) -> None:
    world.release.latest("not a version\n")
    world.record_current()
    _without_models(world)
    report = world.run()
    assert (report.status, report.reason) == ("unknown", "latest")
    assert world.claude_state()["current"]["models"] == RESOLVED  # type: ignore[index]


def test_a_failed_fill_changes_nothing_and_goes_on(world: World) -> None:
    world.release.latest("2.1.319\n")
    world.record_current()
    _without_models(world)
    world.claude.exit_codes["alias-opus"] = 1
    saved = world.state_path.read_bytes()
    report = world.run()
    assert (report.status, report.reason) == ("up_to_date", "latest")
    assert world.state_path.read_bytes() == saved
    assert world.claude.keys() == ALIAS_KEYS


def test_a_record_with_models_starts_no_session(world: World) -> None:
    world.release.latest("2.1.319\n")
    world.record_current()
    saved = world.state_path.read_bytes()
    world.run()
    assert world.claude.calls == []
    assert world.state_path.read_bytes() == saved


# The candidate


def test_a_ready_candidate_records_the_models_of_its_binary(world: World) -> None:
    world.release.publish_measured()
    world.record_current()
    world.claude.resolved = {
        "haiku": "claude-haiku-6-0",
        "sonnet": "claude-sonnet-6-0",
        "opus": "claude-opus-6-0",
    }
    report = world.run()
    assert (report.status, report.reason) == ("ready", None)
    assert report.models == world.claude.resolved
    assert report.to_json()["models"] == world.claude.resolved
    assert world.claude_state()["candidate"]["models"] == world.claude.resolved  # type: ignore[index]
    keys = world.claude.keys()
    # The aliases resolve after every session check passed.
    assert keys[-3:] == ALIAS_KEYS
    assert ALIAS_KEYS[0] not in keys[:-3]
    session_cwds = {cwd for key, _, cwd, _, _ in world.claude.calls}
    assert len(session_cwds) == 1
    for _, argv, _, _, _ in world.claude.calls[-3:]:
        assert argv[0] == str((world.versions / "2.1.326").resolve())


def test_a_candidate_whose_aliases_do_not_resolve_fails(world: World) -> None:
    world.release.publish_measured()
    world.record_current()
    world.claude.raises["alias-haiku"] = OSError("no claude")
    report = world.run()
    assert (report.status, report.reason, report.models) == ("failed", "model", None)
    assert all(check.result == "pass" for check in report.checks)
    candidate = world.claude_state()["candidate"]
    assert candidate["status"] == "failed"  # type: ignore[index]
    assert candidate["models"] is None  # type: ignore[index]
    assert not (world.versions / "2.1.326").exists()
    assert [(e, kw["target"]) for e, kw in world.sent.calls] == [
        ("selfcheck_failed", "claude@2.1.326")
    ]


def test_a_failed_session_check_resolves_nothing(world: World) -> None:
    world.release.publish_measured()
    world.record_current()
    world.claude.hook_blocks = {"exit2", "deny", "exit1"}
    report = world.run()
    assert (report.status, report.reason, report.models) == ("failed", None, None)
    assert not any(key.startswith("alias-") for key in world.claude.keys())
    assert world.claude_state()["candidate"]["models"] is None  # type: ignore[index]


def test_a_ready_candidate_is_reused_with_its_models(world: World) -> None:
    world.release.publish_measured()
    world.record_current()
    world.run()
    world.claude.calls.clear()
    report = world.run()
    assert (report.status, report.reused, report.models) == ("ready", True, RESOLVED)
    assert world.claude.calls == []


def test_a_ready_candidate_without_models_is_checked_again(world: World) -> None:
    world.release.publish_measured()
    world.record_current()
    world.run()
    state = world.state()
    del state["agents"]["claude"]["candidate"]["models"]  # type: ignore[index]
    world.write_state(state)
    world.claude.calls.clear()
    report = world.run()
    assert (report.status, report.reused, report.models) == ("ready", False, RESOLVED)
    assert world.claude.keys()[-3:] == ALIAS_KEYS


# Apply and rollback


@pytest.mark.parametrize("models", ["absent", None])
def test_a_ready_candidate_without_models_is_not_applied(
    world: World, models: str | None
) -> None:
    switches.applyable(world)
    state = world.state()
    candidate = state["agents"]["claude"]["candidate"]  # type: ignore[index]
    if models == "absent":
        del candidate["models"]
    else:
        candidate["models"] = None
    world.write_state(state)
    before = switches.snapshot(world)
    report = switches.apply(world)
    assert (report.status, report.reason) == ("refused", "no_ready_candidate")
    switches.unchanged(world, before, report)


def test_a_rollback_to_a_record_without_models_has_none(world: World) -> None:
    switches.applyable(world)
    state = world.state()
    del state["agents"]["claude"]["current"]["models"]  # type: ignore[index]
    world.write_state(state)
    assert switches.apply(world).status == "applied"
    report = switches.rollback(world)
    assert report.status == "rolled_back"
    current = world.claude_state()["current"]
    assert "models" not in current  # type: ignore[operator]
    assert world.claude_state()["previous"]["models"] == RESOLVED  # type: ignore[index]


# Task specs use the tier aliases


SPEC = """+++
id = "t-alias"
title = "Alias"
target = "orchestrator"
grade = "{grade}"
lane = "{lane}"
model = "{model}"
effort = "{effort}"
timeout_min = 30
modify = ["src/orch/x.py"]
read = ["src/orch/**"]
verify = ".venv/bin/python -m pytest tests/core"
depends_on = []
+++

# Purpose
"""


@pytest.mark.parametrize(
    ("grade", "model", "effort"),
    [
        ("L1", "haiku", "low"),
        ("L2", "sonnet", "medium"),
        ("L3", "sonnet", "high"),
        ("L3R", "opus", "xhigh"),
    ],
)
@pytest.mark.parametrize("lane", ["C", "D"])
def test_claude_specs_take_the_aliases(
    grade: str, model: str, effort: str, lane: str
) -> None:
    text = SPEC.format(grade=grade, lane=lane, model=model, effort=effort)
    if effort != "high":
        text = text.replace(f'effort = "{effort}"\n', "")
    assert parse_task(text).model == model


@pytest.mark.parametrize(
    ("grade", "model", "effort"),
    [
        ("L1", "claude-haiku-5-5", None),
        ("L2", "claude-sonnet-5-5", None),
        ("L3", "claude-sonnet-5-5", "high"),
        ("L3R", "claude-opus-5-5", None),
    ],
)
def test_claude_model_ids_are_refused(
    grade: str, model: str, effort: str | None
) -> None:
    text = SPEC.format(grade=grade, lane="C", model=model, effort=effort or "")
    if effort is None:
        text = text.replace('effort = ""\n', "")
    with pytest.raises(TaskFileError):
        parse_task(text)
