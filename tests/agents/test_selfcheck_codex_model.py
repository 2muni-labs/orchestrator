"""m-codex-model-select: the Codex model follows the version in selfcheck (ADR 0032).

Uses the helpers of test_selfcheck_codex.py and test_selfcheck_codex_switch.py.
Nothing here uses the network or runs the real codex, mise or codesign. The
`codex debug models` answers follow the codex-cli 0.171.0 measurement (2026-11-26).
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

import test_selfcheck_codex as runs
import test_selfcheck_codex_switch as switches
from orch import selfcheck
from orch.selfcheck import SelfcheckError, SwitchReport
from orch.selfcheck_codex import (
    MODELS_TIMEOUT_SEC,
    REQUIRED_EFFORTS,
    current_model,
    list_model,
    load_codex_pins,
    select_model,
)
from test_selfcheck_codex import (
    NOW,
    PASSED_CHECKS,
    RELEASES,
    FakeCommands,
    model_entry,
    models_json,
)
from test_selfcheck_codex_switch import LATER

NEW_MODEL = "gpt-6.2-sol"
OLD_MODEL = "gpt-6.1-sol"
# The measured order: 6.2 sol first, then astra and terra; two hidden entries.
MEASURED = models_json(
    model_entry("gpt-reserve", 0, visibility="hide"),
    model_entry("gpt-6.2-sol", 1),
    model_entry("gpt-6.2-astra", 2),
    model_entry("gpt-6.1-terra", 3),
    model_entry("codex-auto-review", 4, visibility="hide"),
)
NEW_FIRST = models_json(model_entry(NEW_MODEL, 1), model_entry(OLD_MODEL, 2))


@pytest.fixture
def run_world(tmp_path: Path) -> runs.World:
    home = tmp_path / "home"
    (home / ".orch" / "secrets").mkdir(parents=True)
    w = runs.World(tmp_path, runs.make_hub(tmp_path), tmp_path / "orch", home)
    w.publish(runs.PAGE_ONE, runs.PAGE_TWO)
    w.publish_sums("0.160.0", "0.170.0", "0.171.0")
    return w


@pytest.fixture
def switch_world(tmp_path: Path) -> switches.World:
    return switches.World(tmp_path)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network used")

    monkeypatch.setattr("urllib.request.urlopen", refuse)


# Pins and constants


def test_the_real_pins_name_the_default_model() -> None:
    assert load_codex_pins(runs.REPO).default_model == OLD_MODEL


def test_constants() -> None:
    assert MODELS_TIMEOUT_SEC == 60
    assert REQUIRED_EFFORTS == ("low", "medium", "high", "xhigh")


@pytest.mark.parametrize(
    "line",
    [None, 'default_model = ""', "default_model = 6", "default_model = []"],
)
def test_a_missing_or_empty_default_model_is_refused(
    tmp_path: Path, line: str | None
) -> None:
    text = (runs.REPO / "config" / "pins.toml").read_text(encoding="utf-8")
    old = f'default_model = "{OLD_MODEL}"\n'
    assert text.count(old) == 1
    text = text.replace(old, "" if line is None else line + "\n")
    hub = runs.make_hub(tmp_path, text)
    with pytest.raises(SelfcheckError):
        load_codex_pins(hub)


# select_model


def test_the_measured_list_selects_the_first_listed_model() -> None:
    assert select_model(MEASURED) == NEW_MODEL


def test_any_family_can_be_selected() -> None:
    body = models_json(model_entry("gpt-6.3-luna", 5), model_entry("gpt-6.3-astra", 2))
    assert select_model(body) == "gpt-6.3-astra"


def test_equal_priorities_keep_the_list_order() -> None:
    body = models_json(model_entry("b-model", 1), model_entry("a-model", 1))
    assert select_model(body) == "b-model"


@pytest.mark.parametrize(
    "skipped",
    [
        model_entry("hidden", 0, visibility="hide"),
        model_entry("no-api", 0, api=False),
        model_entry("api-string", 0, api="true"),
        model_entry("no-xhigh", 0, efforts=("low", "medium", "high")),
        model_entry("no-low", 0, efforts=("medium", "high", "xhigh")),
        {**model_entry("bool-priority", 0), "priority": False},
        {**model_entry("float-priority", 0), "priority": 0.5},
        {**model_entry("str-priority", 0), "priority": "0"},
        {**model_entry("x", 0), "slug": ""},
        {**model_entry("x", 0), "slug": 7},
        {**model_entry("no-levels", 0), "supported_reasoning_levels": None},
        {
            **model_entry("odd-levels", 0),
            "supported_reasoning_levels": ["low", "medium", "high", "xhigh"],
        },
        {k: v for k, v in model_entry("no-visibility", 0).items() if k != "visibility"},
        "gpt-7",
        None,
    ],
)
def test_entries_that_do_not_qualify_are_skipped(skipped: object) -> None:
    body = json.dumps({"models": [skipped, model_entry("fallback", 9)]})
    assert select_model(body) == "fallback"


def test_extra_efforts_do_not_matter() -> None:
    body = models_json(
        model_entry("more", 1, efforts=("minimal", "low", "medium", "high", "xhigh"))
    )
    assert select_model(body) == "more"


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "not json",
        "[]",
        '{"models": {}}',
        '{"data": []}',
        '{"models": []}',
        models_json(model_entry("hidden", 1, visibility="hide")),
    ],
)
def test_no_qualifying_model_is_none(stdout: str) -> None:
    assert select_model(stdout) is None


# list_model


def test_list_model_runs_debug_models_on_the_binary() -> None:
    commands = FakeCommands(models_default=MEASURED)
    assert list_model(Path("/b/codex"), commands) == NEW_MODEL
    assert commands.calls == [(["/b/codex", "debug", "models"], 60)]


@pytest.mark.parametrize(
    "commands",
    [
        FakeCommands(models_default=MEASURED, models_exit=1),
        FakeCommands(models_raises=OSError("no codex")),
        FakeCommands(models_raises=subprocess.TimeoutExpired("codex", 60)),
        FakeCommands(models_default="not json"),
    ],
)
def test_list_model_failures_are_none(commands: FakeCommands) -> None:
    assert list_model(Path("/b/codex"), commands) is None


# current_model


def test_current_model() -> None:
    pins = load_codex_pins(runs.REPO)
    assert current_model({"version": "0.171.0", "model": NEW_MODEL}, pins) == (
        NEW_MODEL,
        "record",
    )
    for entry in (None, {"version": "0.171.0"}, {"model": ""}, {"model": 5}):
        assert current_model(entry, pins) == (OLD_MODEL, "default")


# A new version: the candidate binary picks the model


def _models_calls(commands: FakeCommands) -> list[str]:
    return [argv[0] for argv, _ in commands.calls if argv[1:] == ["debug", "models"]]


def _model_of(argv: list[str]) -> str:
    return argv[argv.index("-m") + 1]


def test_a_new_version_is_checked_with_the_model_its_binary_lists(
    run_world: runs.World,
) -> None:
    run_world.record_current()
    run_world.commands.models_default = MEASURED
    report = run_world.run()
    binary = run_world.layout.installs / "0.170.0" / "bin" / "codex"
    assert (report.status, report.candidate, report.model) == (
        "ready",
        "0.170.0",
        NEW_MODEL,
    )
    assert report.to_json()["model"] == NEW_MODEL
    assert _models_calls(run_world.commands) == [str(binary)]
    [(probe_argv, *_)] = run_world.probe.calls
    assert _model_of(probe_argv) == NEW_MODEL
    assert [_model_of(call[0]) for call in run_world.isolation.calls] == [
        NEW_MODEL,
        NEW_MODEL,
    ]
    candidate = run_world.codex()["candidate"]
    assert candidate["model"] == NEW_MODEL  # type: ignore[index]


def test_a_new_version_without_a_model_fails_before_the_checks(
    run_world: runs.World,
) -> None:
    run_world.record_current()
    run_world.commands.models_exit = 1
    report = run_world.run()
    assert (report.status, report.reason, report.model) == ("failed", "model", None)
    assert [(c.id, c.result) for c in report.checks] == [
        ("FL-M4", "pass"),
        ("S-M2", "skipped"),
        ("S-M2b", "skipped"),
        ("isolation", "skipped"),
    ]
    assert run_world.probe.calls == []
    assert run_world.isolation.calls == []
    candidate = run_world.codex()["candidate"]
    assert candidate["status"] == "failed"  # type: ignore[index]
    assert candidate["model"] is None  # type: ignore[index]
    assert runs.installed(run_world) == ["0.160.0"]
    assert [(e, kw["target"]) for e, kw in run_world.send.calls] == [
        ("selfcheck_failed", "codex@0.170.0")
    ]


def test_a_failure_to_pick_a_model_is_tried_again(run_world: runs.World) -> None:
    run_world.record_current()
    run_world.commands.models_exit = 1
    run_world.run()
    run_world.commands.models_exit = 0
    run_world.commands.models_default = MEASURED
    report = run_world.run()
    assert (report.status, report.reused, report.model) == ("ready", False, NEW_MODEL)


def test_a_ready_candidate_is_reused_only_for_the_same_model(
    run_world: runs.World,
) -> None:
    run_world.record_current()
    first = run_world.run()
    assert (first.status, first.model) == ("ready", OLD_MODEL)
    run_world.probe.calls.clear()
    again = run_world.run()
    assert (again.status, again.reused, again.model) == ("ready", True, OLD_MODEL)
    assert run_world.probe.calls == []

    run_world.commands.models_default = MEASURED
    report = run_world.run()
    assert (report.status, report.reused, report.model) == ("ready", False, NEW_MODEL)
    assert len(run_world.probe.calls) == 1
    assert run_world.codex()["candidate"]["model"] == NEW_MODEL  # type: ignore[index]


def test_the_bootstrap_records_no_model(run_world: runs.World) -> None:
    run_world.run()
    assert "model" not in run_world.codex()["current"]  # type: ignore[operator]


# The same version with a new model


def _with_model(world: runs.World, model: str) -> None:
    state = world.state()
    state["agents"]["codex"]["current"]["model"] = model  # type: ignore[index]
    selfcheck.save_agents_state(world.state_path, state)


def test_an_up_to_date_version_with_a_new_model_makes_a_model_candidate(
    run_world: runs.World,
) -> None:
    binary = run_world.record_current("0.171.0")
    run_world.commands.models_default = NEW_FIRST
    report = run_world.run()
    assert (report.status, report.reason) == ("ready", None)
    assert (report.current, report.latest, report.candidate, report.model) == (
        "0.171.0",
        "0.171.0",
        "0.171.0",
        NEW_MODEL,
    )
    assert report.reused is False
    assert [c.__dict__ for c in report.checks] == PASSED_CHECKS
    assert _models_calls(run_world.commands) == [str(binary)]
    # The current binary is checked with the new model; nothing is installed.
    assert run_world.mise.calls == []
    [(probe_argv, *_)] = run_world.probe.calls
    assert str(binary) in probe_argv
    assert _model_of(probe_argv) == NEW_MODEL
    assert [call[0][0] for call in run_world.isolation.calls] == [str(binary)] * 2
    assert [_model_of(call[0]) for call in run_world.isolation.calls] == [
        NEW_MODEL,
        NEW_MODEL,
    ]
    pins = load_codex_pins(run_world.hub)
    codex = run_world.codex()
    assert codex["candidate"] == {
        "version": "0.171.0",
        "model": NEW_MODEL,
        "status": "ready",
        "sha256": selfcheck.sha256_file(binary),
        "path": str(binary),
        "pins_sha256": pins.sha256,
        "checks": PASSED_CHECKS,
        "warnings": list(pins.warn),
        "at": NOW.isoformat(),
    }
    assert "model" not in codex["current"]  # type: ignore[operator]
    assert [(e, kw["target"]) for e, kw in run_world.send.calls] == [
        ("selfcheck_ready", "codex@0.171.0")
    ]
    assert runs.installed(run_world) == ["0.171.0"]


def test_no_candidate_also_reselects_the_model(run_world: runs.World) -> None:
    run_world.record_current("0.169.1")
    run_world.publish([runs.release("rust-v0.171.0", "2026-11-20T00:44:47Z")])
    run_world.commands.models_default = NEW_FIRST
    report = run_world.run()
    assert (report.status, report.candidate, report.model) == (
        "ready",
        "0.169.1",
        NEW_MODEL,
    )


def test_the_recorded_model_keeps_an_up_to_date_version_as_it_is(
    run_world: runs.World,
) -> None:
    run_world.record_current("0.171.0")
    _with_model(run_world, NEW_MODEL)
    run_world.commands.models_default = NEW_FIRST
    saved = run_world.state_path.read_bytes()
    report = run_world.run()
    assert (report.status, report.reason, report.model) == (
        "up_to_date",
        "latest",
        None,
    )
    assert report.candidate is None
    assert run_world.state_path.read_bytes() == saved
    assert run_world.send.calls == []
    assert run_world.probe.calls == []


def test_the_default_model_counts_for_a_record_without_one(
    run_world: runs.World,
) -> None:
    run_world.record_current("0.171.0")
    saved = run_world.state_path.read_bytes()
    report = run_world.run()
    assert (report.status, report.reason, report.model) == (
        "up_to_date",
        "latest",
        None,
    )
    assert run_world.state_path.read_bytes() == saved


def test_a_model_list_failure_keeps_the_up_to_date_report(
    run_world: runs.World,
) -> None:
    run_world.record_current("0.171.0")
    run_world.commands.models_exit = 1
    saved = run_world.state_path.read_bytes()
    report = run_world.run()
    assert (report.status, report.reason, report.model) == (
        "up_to_date",
        "latest",
        None,
    )
    assert run_world.state_path.read_bytes() == saved
    assert run_world.send.calls == []


def test_an_unknown_latest_does_not_reselect(run_world: runs.World) -> None:
    run_world.record_current("0.171.0")
    run_world.commands.models_default = NEW_FIRST
    run_world.fetch.errors[RELEASES.format(1)] = selfcheck.FetchError("504")
    report = run_world.run()
    assert (report.status, report.reason) == ("unknown", "fetch")
    assert _models_calls(run_world.commands) == []


def test_a_changed_current_binary_fails_the_model_candidate(
    run_world: runs.World,
) -> None:
    binary = run_world.record_current("0.171.0")
    with binary.open("ab") as stream:
        stream.write(b"tampered")
    run_world.commands.models_default = NEW_FIRST
    report = run_world.run()
    assert (report.status, report.candidate, report.model) == (
        "failed",
        "0.171.0",
        NEW_MODEL,
    )
    assert [(c.id, c.result, c.detail) for c in report.checks] == [
        ("FL-M4", "fail", "binary_changed"),
        ("S-M2", "skipped", ""),
        ("S-M2b", "skipped", ""),
        ("isolation", "skipped", ""),
    ]
    assert run_world.probe.calls == []
    assert run_world.isolation.calls == []
    candidate = run_world.codex()["candidate"]
    assert (candidate["status"], candidate["sha256"], candidate["path"]) == (  # type: ignore[index]
        "failed",
        None,
        None,
    )
    # The current install is never removed.
    assert binary.is_file()
    assert runs.installed(run_world) == ["0.171.0"]


def test_a_failed_model_candidate_keeps_the_current_install(
    run_world: runs.World,
) -> None:
    binary = run_world.record_current("0.171.0")
    run_world.commands.models_default = NEW_FIRST
    run_world.probe.direct_exit = 0
    report = run_world.run()
    assert report.status == "failed"
    assert binary.is_file()
    assert runs.installed(run_world) == ["0.171.0"]
    assert [(e, kw["target"]) for e, kw in run_world.send.calls] == [
        ("selfcheck_failed", "codex@0.171.0")
    ]


def test_a_model_candidate_is_reused(run_world: runs.World) -> None:
    run_world.record_current("0.171.0")
    run_world.commands.models_default = NEW_FIRST
    run_world.run()
    saved = run_world.state_path.read_bytes()
    run_world.probe.calls.clear()
    run_world.send.calls.clear()
    report = run_world.run()
    assert (report.status, report.reused, report.model) == ("ready", True, NEW_MODEL)
    assert run_world.probe.calls == []
    assert run_world.send.calls == []
    assert run_world.state_path.read_bytes() == saved


def test_a_model_candidate_with_an_unknown_blocking_id_is_not_automated(
    run_world: runs.World,
) -> None:
    runs.add_unknown_blocking_id(run_world)
    run_world.record_current("0.171.0")
    run_world.commands.models_default = NEW_FIRST
    report = run_world.run()
    assert (report.status, report.candidate) == ("not_automated", "0.171.0")
    assert [c.__dict__ for c in report.checks] == runs.NOT_AUTOMATED_CHECKS
    assert run_world.probe.calls == []
    assert run_world.isolation.calls == []


# Apply and rollback of a model candidate


SAME = "0.171.0"


def model_applyable(world: switches.World, *, record_model: str | None = None) -> None:
    """current 0.171.0, previous 0.160.0, a ready 0.171.0 candidate with a model."""
    current = switches.entry(world, SAME, "apply")
    if record_model is not None:
        current = {**current, "model": record_model}
    candidate = {
        "version": SAME,
        "model": NEW_MODEL,
        "status": "ready",
        "sha256": current["sha256"],
        "path": current["path"],
        "pins_sha256": load_codex_pins(world.hub).sha256,
        "checks": PASSED_CHECKS,
        "warnings": [],
        "at": "2026-11-21T00:00:00+00:00",
    }
    world.write_state(
        {
            "format": 1,
            "agents": {
                "codex": {
                    "current": current,
                    "previous": switches.entry(world, "0.160.0", "bootstrap"),
                    "candidate": candidate,
                }
            },
        }
    )
    world.point_link(SAME)
    switches.write_mise_files(world.layout.root, SAME)
    switches.write_mise_files(world.previous_dir, "0.160.0")


def _files(world: switches.World) -> dict[str, object]:
    snap = switches.snapshot(world)
    return {key: snap[key] for key in ("link", "bin", "files")}


def test_apply_of_a_model_candidate_changes_only_the_record(
    switch_world: switches.World,
) -> None:
    model_applyable(switch_world)
    old = switch_world.codex()
    before = _files(switch_world)
    report = switches.apply(switch_world)
    assert report == SwitchReport("applied", None, SAME, SAME, None, ("0.160.0",))
    assert _files(switch_world) == before
    codex = switch_world.codex()
    assert codex["current"] == {
        "version": SAME,
        "sha256": old["current"]["sha256"],  # type: ignore[index]
        "path": old["current"]["path"],  # type: ignore[index]
        "model": NEW_MODEL,
        "at": LATER.isoformat(),
        "source": "apply",
    }
    assert codex["previous"] == old["current"]
    assert codex["candidate"] is None
    switches.lock_is_free(switch_world)


def test_apply_of_a_model_candidate_needs_no_mise_files(
    switch_world: switches.World,
) -> None:
    model_applyable(switch_world)
    for directory in (switch_world.layout.root, switch_world.previous_dir):
        for name in ("config.toml", "mise.lock"):
            (directory / name).unlink()
    report = switches.apply(switch_world)
    assert report.status == "applied"
    assert switch_world.codex()["current"]["model"] == NEW_MODEL  # type: ignore[index]


@pytest.mark.parametrize("record_model", [NEW_MODEL, None])
def test_the_same_model_is_not_newer(
    switch_world: switches.World, record_model: str | None
) -> None:
    model_applyable(switch_world, record_model=record_model)
    if record_model is None:
        state = switch_world.state()
        state["agents"]["codex"]["candidate"]["model"] = OLD_MODEL  # type: ignore[index]
        switch_world.write_state(state)
    before = switches.snapshot(switch_world)
    report = switches.apply(switch_world)
    assert (report.status, report.reason) == ("refused", "not_newer")
    switches.unchanged(switch_world, before, report)


@pytest.mark.parametrize("version", [SAME, "0.170.0"])
@pytest.mark.parametrize("model", ["absent", None, ""])
def test_a_ready_candidate_without_a_model_is_not_applied(
    switch_world: switches.World, version: str, model: str | None
) -> None:
    if version == SAME:
        model_applyable(switch_world)
    else:
        switches.applyable(switch_world)
    state = switch_world.state()
    candidate = state["agents"]["codex"]["candidate"]  # type: ignore[index]
    if model == "absent":
        del candidate["model"]
    else:
        candidate["model"] = model
    switch_world.write_state(state)
    before = switches.snapshot(switch_world)
    report = switches.apply(switch_world)
    assert (report.status, report.reason) == ("refused", "no_ready_candidate")
    switches.unchanged(switch_world, before, report)


def test_a_model_candidate_elsewhere_is_refused(switch_world: switches.World) -> None:
    model_applyable(switch_world)
    other = switch_world.install("0.170.0")
    state = switch_world.state()
    candidate = state["agents"]["codex"]["candidate"]  # type: ignore[index]
    candidate["path"] = str(other)
    candidate["sha256"] = switches.sha(other)
    switch_world.write_state(state)
    before = switches.snapshot(switch_world)
    report = switches.apply(switch_world)
    assert (report.status, report.reason) == ("refused", "path")
    switches.unchanged(switch_world, before, report)


def test_a_changed_binary_refuses_the_model_candidate(
    switch_world: switches.World,
) -> None:
    model_applyable(switch_world)
    state = switch_world.state()
    state["agents"]["codex"]["candidate"]["sha256"] = "0" * 64  # type: ignore[index]
    switch_world.write_state(state)
    before = switches.snapshot(switch_world)
    report = switches.apply(switch_world)
    assert (report.status, report.reason) == ("refused", "binary_changed")
    switches.unchanged(switch_world, before, report)


def test_a_model_candidate_still_needs_the_link_on_the_current_record(
    switch_world: switches.World,
) -> None:
    model_applyable(switch_world)
    switch_world.point_link("0.160.0")
    before = switches.snapshot(switch_world)
    report = switches.apply(switch_world)
    assert (report.status, report.reason) == ("refused", "link")
    switches.unchanged(switch_world, before, report)


def test_rollback_of_a_model_switch_changes_only_the_record(
    switch_world: switches.World,
) -> None:
    model_applyable(switch_world)
    assert switches.apply(switch_world).status == "applied"
    applied = switch_world.codex()
    before = _files(switch_world)
    report = switches.rollback(switch_world)
    assert report == SwitchReport("rolled_back", None, SAME, SAME, None, ())
    assert _files(switch_world) == before
    codex = switch_world.codex()
    previous = applied["previous"]
    assert codex["current"] == {
        "version": SAME,
        "sha256": previous["sha256"],  # type: ignore[index]
        "path": previous["path"],  # type: ignore[index]
        "at": LATER.isoformat(),
        "source": "rollback",
    }
    assert codex["previous"] == applied["current"]
    # Rolling back again returns to the new model.
    report = switches.rollback(switch_world)
    assert report.status == "rolled_back"
    assert switch_world.codex()["current"]["model"] == NEW_MODEL  # type: ignore[index]
    assert _files(switch_world) == before


def test_a_same_version_rollback_still_checks_the_binary(
    switch_world: switches.World,
) -> None:
    model_applyable(switch_world)
    switches.apply(switch_world)
    state = switch_world.state()
    state["agents"]["codex"]["previous"]["sha256"] = "0" * 64  # type: ignore[index]
    switch_world.write_state(state)
    before = switches.snapshot(switch_world)
    report = switches.rollback(switch_world)
    assert (report.status, report.reason) == ("refused", "binary_changed")
    switches.unchanged(switch_world, before, report)


def test_rollback_across_versions_carries_the_model(
    switch_world: switches.World,
) -> None:
    current = {**switches.entry(switch_world, "0.170.0", "apply"), "model": NEW_MODEL}
    previous = {
        **switches.entry(switch_world, "0.160.0", "apply"),
        "model": OLD_MODEL,
    }
    switch_world.write_state(
        {
            "format": 1,
            "agents": {
                "codex": {"current": current, "previous": previous, "candidate": None}
            },
        }
    )
    switch_world.point_link("0.170.0")
    switches.write_mise_files(switch_world.layout.root, "0.170.0")
    switches.write_mise_files(switch_world.previous_dir, "0.160.0")
    report = switches.rollback(switch_world)
    assert (report.status, report.current, report.hold) == (
        "rolled_back",
        "0.160.0",
        "codex@0.170.0",
    )
    codex = switch_world.codex()
    assert codex["current"] == {
        "version": "0.160.0",
        "sha256": previous["sha256"],
        "path": previous["path"],
        "model": OLD_MODEL,
        "at": LATER.isoformat(),
        "source": "rollback",
    }
    assert codex["previous"] == current
    assert os.readlink(switch_world.layout.link) == str(
        Path(str(previous["path"])).resolve()
    )
