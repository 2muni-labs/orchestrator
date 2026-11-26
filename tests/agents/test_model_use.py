"""m-agent-model-use: the recorded Codex model and the models in orch versions.

ADR 0032. The CLI cases (prepare, review, explore, versions) are in
tests/cli/test_cli.py; these cover the helpers and parse_task.
"""

import json
from pathlib import Path

import pytest

from orch import selfcheck, versions
from orch.selfcheck import SelfcheckError
from orch.selfcheck_codex import recorded_model
from orch.taskfile import Effort, TaskFileError, parse_task

REPO = Path(__file__).resolve().parents[2]
DEFAULT = "gpt-6.1-sol"
RESOLVED = {
    "haiku": "claude-haiku-5-5",
    "sonnet": "claude-sonnet-5-5",
    "opus": "claude-opus-5-5",
}


def make_hub(root: Path) -> Path:
    hub = root / "hub"
    (hub / "config").mkdir(parents=True)
    (hub / "config" / "pins.toml").write_bytes(
        (REPO / "config" / "pins.toml").read_bytes()
    )
    return hub


def entry(version: str, **extra: object) -> dict[str, object]:
    return {
        "version": version,
        "sha256": "a" * 64,
        "path": f"/x/{version}",
        "at": "2026-11-26T00:00:00+00:00",
        "source": "apply",
        **extra,
    }


def write_codex(orch_home: Path, current: dict[str, object] | None) -> None:
    orch_home.mkdir(parents=True, exist_ok=True)
    state = {
        "format": 1,
        "agents": {"codex": {"current": current, "previous": None, "candidate": None}},
    }
    selfcheck.agents_state_path(orch_home).write_text(json.dumps(state))


# recorded_model


def test_the_recorded_model(tmp_path: Path) -> None:
    hub, home = make_hub(tmp_path), tmp_path / "orch"
    write_codex(home, entry("0.171.0", model="gpt-6.2-sol"))
    assert recorded_model(hub, home) == ("gpt-6.2-sol", "record")


@pytest.mark.parametrize(
    "current", [None, entry("0.171.0"), entry("0.171.0", model="")]
)
def test_without_a_recorded_model_the_pins_default(
    tmp_path: Path, current: dict[str, object] | None
) -> None:
    hub, home = make_hub(tmp_path), tmp_path / "orch"
    write_codex(home, current)
    assert recorded_model(hub, home) == (DEFAULT, "default")


def test_without_an_agents_file_the_pins_default(tmp_path: Path) -> None:
    assert recorded_model(make_hub(tmp_path), tmp_path / "orch") == (
        DEFAULT,
        "default",
    )


def test_an_unreadable_record_raises(tmp_path: Path) -> None:
    hub, home = make_hub(tmp_path), tmp_path / "orch"
    home.mkdir()
    selfcheck.agents_state_path(home).write_text("{not json")
    with pytest.raises(SelfcheckError):
        recorded_model(hub, home)


def test_unreadable_pins_raise(tmp_path: Path) -> None:
    hub = tmp_path / "hub"
    (hub / "config").mkdir(parents=True)
    with pytest.raises(SelfcheckError):
        recorded_model(hub, tmp_path / "orch")


# parse_task: any well-formed Codex model; prepare compares it with the record


SPEC = """+++
id = "t-model"
title = "Model"
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
    "model", ["gpt-6.2-sol", "gpt-6.2-astra", "gpt-6.1-terra", "gpt-7", "o9"]
)
@pytest.mark.parametrize("lane", ["B", "D"])
def test_codex_specs_take_any_well_formed_model(model: str, lane: str) -> None:
    spec = parse_task(SPEC.format(grade="L2", lane=lane, model=model, effort="medium"))
    assert (spec.model, spec.effort) == (model, Effort.MEDIUM)


@pytest.mark.parametrize(
    ("grade", "effort"),
    [("L1", "medium"), ("L2", "low"), ("L3", "medium"), ("L3R", "high")],
)
def test_the_codex_effort_still_follows_the_grade(grade: str, effort: str) -> None:
    with pytest.raises(TaskFileError):
        parse_task(
            SPEC.format(grade=grade, lane="B", model="gpt-6.2-sol", effort=effort)
        )


@pytest.mark.parametrize("model", ["sonnet", "opus", "haiku", "GPT-6", "gpt 6", ".x"])
def test_claude_aliases_and_malformed_names_are_not_codex_models(model: str) -> None:
    with pytest.raises(TaskFileError):
        parse_task(SPEC.format(grade="L2", lane="B", model=model, effort="medium"))


# versions: models


def codex_report(
    state: dict[str, object] | None, default_model: str | None = DEFAULT
) -> dict[str, object]:
    return versions.agent_report(
        "codex",
        installed="0.171.0",
        baseline="0.160.0",
        blocking=("FL-M4",),
        warn=(),
        state=state,
        record=None,
        default_model=default_model,
    )


def claude_report(state: dict[str, object] | None) -> dict[str, object]:
    return versions.agent_report(
        "claude",
        installed="2.1.330",
        baseline="2.1.319",
        blocking=("FL-M3",),
        warn=(),
        state=state,
        record=None,
    )


def test_the_codex_models_name_the_record() -> None:
    state = {"current": entry("0.171.0", model="gpt-6.2-sol")}
    assert codex_report(state)["models"] == {
        "model": "gpt-6.2-sol",
        "source": "record",
    }


@pytest.mark.parametrize(
    "state", [None, {}, {"current": entry("0.171.0")}, {"current": None}]
)
def test_the_codex_models_fall_back_to_the_default(
    state: dict[str, object] | None,
) -> None:
    assert codex_report(state)["models"] == {"model": DEFAULT, "source": "default"}


def test_without_a_default_the_codex_models_are_null() -> None:
    assert codex_report({"current": entry("0.171.0")}, None)["models"] is None


def test_the_claude_models_are_the_resolved_aliases() -> None:
    state = {"current": entry("2.1.330", models=dict(RESOLVED))}
    result = claude_report(state)
    assert result["models"] == RESOLVED
    assert list(result).index("models") == list(result).index("candidate") + 1


@pytest.mark.parametrize("state", [None, {"current": entry("2.1.330")}])
def test_claude_models_are_null_without_a_record(
    state: dict[str, object] | None,
) -> None:
    assert claude_report(state)["models"] is None


def test_build_report_passes_the_pins_default(tmp_path: Path) -> None:
    hub, home = make_hub(tmp_path), tmp_path / "orch"
    home.mkdir()

    def run(argv: object, timeout: float) -> object:
        raise OSError("not run")

    result = versions.build_report(
        orch_home=home,
        hub=hub,
        claude_path=None,
        codex_path=None,
        tmux_path=None,
        orch_version="0.26.0",
        run=run,  # type: ignore[arg-type]
    )
    assert result["agents"]["codex"]["models"] == {  # type: ignore[index]
        "model": DEFAULT,
        "source": "default",
    }
    assert result["agents"]["claude"]["models"] is None  # type: ignore[index]
