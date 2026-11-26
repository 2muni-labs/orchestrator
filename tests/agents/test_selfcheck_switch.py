"""m-selfcheck-switch: apply, rollback and the check of the version in use.

Uses the World fixture of test_selfcheck.py and the fakes in selfcheck_support.py.
Nothing here uses the network or runs the real claude or codesign.
"""

import fcntl
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orch import notify, selfcheck
from orch.selfcheck import CurrentCheck, SwitchReport
from selfcheck_support import RESOLVED, binary_bytes, make_hub
from test_selfcheck import World, lock_is_free, pins_sha

LATER = datetime(2026, 11, 21, 1, 0, tzinfo=UTC)


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


# Helpers


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ready_candidate(world: World, version: str = "2.1.326") -> dict[str, object]:
    path = world.install(version)
    return {
        "version": version,
        "models": dict(RESOLVED),
        "status": "ready",
        "sha256": sha(path),
        "path": str(path),
        "pins_sha256": pins_sha(world),
        "checks": [{"id": "FL-M3", "result": "pass", "detail": ""}],
        "warnings": ["w"],
        "at": "2026-11-20T00:00:00+00:00",
    }


def installed_entry(world: World, version: str, source: str) -> dict[str, object]:
    path = world.install(version)
    return {
        "version": version,
        "sha256": sha(path),
        "path": str(path),
        "at": "2026-11-18T00:00:00+00:00",
        "source": source,
    }


def applyable(world: World) -> None:
    """current 2.1.319 (bootstrap) with a ready 2.1.326 candidate."""
    world.record_current()
    state = world.state()
    state["agents"]["claude"]["candidate"] = ready_candidate(world)  # type: ignore[index]
    world.write_state(state)


def apply(world: World) -> SwitchReport:
    return selfcheck.apply_claude(
        hub=world.hub, orch_home=world.orch_home, home=world.home, now=LATER
    )


def rollback(world: World) -> SwitchReport:
    return selfcheck.rollback_claude(
        orch_home=world.orch_home, home=world.home, now=LATER
    )


def check(world: World, claude_path: Path | None = None) -> CurrentCheck:
    return selfcheck.check_current_claude(
        orch_home=world.orch_home,
        claude_path=world.link if claude_path is None else claude_path,
        command=world.commands,
    )


def snapshot(world: World) -> tuple[str, str]:
    return world.state_path.read_text(encoding="utf-8"), os.readlink(world.link)


def unchanged(world: World, before: tuple[str, str], report: SwitchReport) -> None:
    assert snapshot(world) == before
    assert report.hold is None
    assert report.removed == ()
    assert not [p for p in world.link.parent.iterdir() if p.name != "claude"]
    lock_is_free(world)


def hold_lock(world: World):  # type: ignore[no-untyped-def]
    stream = (world.orch_home / "agents.json.lock").open("a")
    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return stream


# notify


def test_agent_unverified_is_a_known_event() -> None:
    notify.validate_event("agent_unverified")
    assert (
        notify.dedup_key("agent_unverified", target="claude")
        == "claude|agent_unverified|"
    )


# State format


def test_a_rollback_source_is_accepted(world: World) -> None:
    world.record_current()
    state = world.state()
    state["agents"]["claude"]["current"]["source"] = "rollback"  # type: ignore[index]
    state["agents"]["claude"]["previous"] = installed_entry(  # type: ignore[index]
        world, "2.1.326", "rollback"
    )
    world.write_state(state)
    loaded = selfcheck.load_agents_state(world.state_path)
    assert loaded["agents"]["claude"]["current"]["source"] == "rollback"  # type: ignore[index]


def test_another_source_is_still_refused(world: World) -> None:
    world.record_current()
    state = world.state()
    state["agents"]["claude"]["current"]["source"] = "manual"  # type: ignore[index]
    world.write_state(state)
    with pytest.raises(selfcheck.SelfcheckError):
        selfcheck.load_agents_state(world.state_path)


# Report shapes


def test_switch_report_json() -> None:
    report = SwitchReport(
        "rolled_back", None, "2.1.319", "2.1.326", "claude@2.1.326", ("2.1.317",)
    )
    assert report.to_json() == {
        "status": "rolled_back",
        "reason": None,
        "current": "2.1.319",
        "previous": "2.1.326",
        "hold": "claude@2.1.326",
        "removed": ["2.1.317"],
    }
    json.dumps(report.to_json())


def test_current_check_json() -> None:
    assert CurrentCheck("mismatch", "sha256", "2.1.319").to_json() == {
        "status": "mismatch",
        "reason": "sha256",
        "version": "2.1.319",
    }


# apply


def test_apply_switches_the_link_and_records_the_versions(world: World) -> None:
    applyable(world)
    old_current = world.claude_state()["current"]
    report = apply(world)
    target = (world.versions / "2.1.326").resolve()
    assert (report.status, report.reason) == ("applied", None)
    assert (report.current, report.previous, report.hold) == (
        "2.1.326",
        "2.1.319",
        None,
    )
    assert os.readlink(world.link) == str(target)
    claude = world.claude_state()
    assert claude["current"] == {
        "version": "2.1.326",
        "sha256": sha(target),
        "path": str(target),
        "models": RESOLVED,
        "at": LATER.isoformat(),
        "source": "apply",
    }
    assert claude["previous"] == old_current
    assert claude["candidate"] is None
    assert (world.versions / "2.1.319").is_file()
    assert not [p for p in world.link.parent.iterdir() if p.name != "claude"]
    lock_is_free(world)


def test_apply_keeps_only_the_current_and_previous_installs(world: World) -> None:
    applyable(world)
    world.install("2.1.317")
    world.install("2.1.318")
    (world.versions / "notes.txt").write_bytes(b"keep")
    (world.versions / ".2.1.330.selfcheck-abcd1234").write_bytes(b"keep")
    (world.versions / "2.1.300").mkdir()
    (world.versions / "2.1.200").symlink_to(world.versions / "2.1.319")
    report = apply(world)
    assert report.status == "applied"
    assert report.removed == ("2.1.317", "2.1.318")
    assert sorted(p.name for p in world.versions.iterdir()) == [
        ".2.1.330.selfcheck-abcd1234",
        "2.1.200",
        "2.1.300",
        "2.1.319",
        "2.1.326",
        "notes.txt",
    ]


def test_apply_removes_the_install_of_the_old_previous(world: World) -> None:
    applyable(world)
    state = world.state()
    state["agents"]["claude"]["previous"] = installed_entry(  # type: ignore[index]
        world, "2.1.318", "bootstrap"
    )
    world.write_state(state)
    report = apply(world)
    assert report.status == "applied"
    assert report.removed == ("2.1.318",)
    assert world.claude_state()["previous"]["version"] == "2.1.319"  # type: ignore[index]


def test_a_relative_link_to_the_current_install_is_accepted(world: World) -> None:
    applyable(world)
    world.link.unlink()
    world.link.symlink_to(
        os.path.relpath(world.versions / "2.1.319", world.link.parent)
    )
    assert apply(world).status == "applied"
    assert world.link.resolve() == (world.versions / "2.1.326").resolve()


def refuse_no_current(world: World) -> None:
    world.record_current()
    state = world.state()
    state["agents"]["claude"]["current"] = None  # type: ignore[index]
    state["agents"]["claude"]["candidate"] = ready_candidate(world)  # type: ignore[index]
    world.write_state(state)


def refuse_no_candidate(world: World) -> None:
    world.record_current()


def refuse_failed_candidate(world: World) -> None:
    applyable(world)
    state = world.state()
    candidate = state["agents"]["claude"]["candidate"]  # type: ignore[index]
    candidate.update({"status": "failed", "sha256": None, "path": None})
    world.write_state(state)


def refuse_pins_changed(world: World) -> None:
    applyable(world)
    state = world.state()
    state["agents"]["claude"]["candidate"]["pins_sha256"] = "0" * 64  # type: ignore[index]
    world.write_state(state)


def refuse_not_newer(world: World) -> None:
    world.record_current()
    state = world.state()
    state["agents"]["claude"]["candidate"] = ready_candidate(world, "2.1.300")  # type: ignore[index]
    world.write_state(state)


def refuse_link_elsewhere(world: World) -> None:
    applyable(world)
    world.install("2.1.318")
    world.point_link("2.1.318")


def refuse_link_is_a_file(world: World) -> None:
    applyable(world)
    world.link.unlink()
    world.link.write_bytes(binary_bytes("2.1.319"))


def refuse_link_missing(world: World) -> None:
    applyable(world)
    world.link.unlink()


def refuse_path_outside(world: World) -> None:
    applyable(world)
    outside = world.root / "elsewhere" / "2.1.326"
    outside.parent.mkdir()
    outside.write_bytes(binary_bytes("2.1.326"))
    state = world.state()
    state["agents"]["claude"]["candidate"]["path"] = str(outside)  # type: ignore[index]
    world.write_state(state)


def refuse_path_name(world: World) -> None:
    applyable(world)
    other = world.install("2.1.327", binary_bytes("2.1.326"))
    state = world.state()
    state["agents"]["claude"]["candidate"]["path"] = str(other)  # type: ignore[index]
    world.write_state(state)


def refuse_binary_changed(world: World) -> None:
    applyable(world)
    (world.versions / "2.1.326").write_bytes(b"changed")


def refuse_binary_missing(world: World) -> None:
    applyable(world)
    (world.versions / "2.1.326").unlink()


def refuse_binary_is_a_directory(world: World) -> None:
    applyable(world)
    (world.versions / "2.1.326").unlink()
    (world.versions / "2.1.326").mkdir()


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        (refuse_no_current, "no_current"),
        (refuse_no_candidate, "no_ready_candidate"),
        (refuse_failed_candidate, "no_ready_candidate"),
        (refuse_pins_changed, "pins_changed"),
        (refuse_not_newer, "not_newer"),
        (refuse_link_elsewhere, "link"),
        (refuse_path_outside, "path"),
        (refuse_path_name, "path"),
        (refuse_binary_changed, "binary_changed"),
        (refuse_binary_missing, "binary_changed"),
        (refuse_binary_is_a_directory, "binary_changed"),
    ],
)
def test_apply_refuses_and_changes_nothing(world: World, setup, reason: str) -> None:  # type: ignore[no-untyped-def]
    setup(world)
    before = snapshot(world)
    files = sorted(p.name for p in world.versions.iterdir())
    report = apply(world)
    assert (report.status, report.reason) == ("refused", reason)
    unchanged(world, before, report)
    assert sorted(p.name for p in world.versions.iterdir()) == files


@pytest.mark.parametrize("setup", [refuse_link_is_a_file, refuse_link_missing])
def test_apply_refuses_when_the_link_is_not_a_symlink(world: World, setup) -> None:  # type: ignore[no-untyped-def]
    setup(world)
    state = world.state_path.read_text(encoding="utf-8")
    report = apply(world)
    assert (report.status, report.reason) == ("refused", "link")
    assert world.state_path.read_text(encoding="utf-8") == state
    assert not world.link.is_symlink()
    assert report.removed == ()


def test_a_refused_apply_reports_the_recorded_versions(world: World) -> None:
    refuse_pins_changed(world)
    report = apply(world)
    assert (report.current, report.previous) == ("2.1.319", None)


def test_apply_without_a_state_file_is_refused(world: World) -> None:
    report = apply(world)
    assert (report.status, report.reason) == ("refused", "no_current")
    assert not world.state_path.exists()


def test_apply_with_an_unreadable_state_is_an_error(world: World) -> None:
    applyable(world)
    world.state_path.write_text("{not json", encoding="utf-8")
    report = apply(world)
    assert (report.status, report.reason) == ("error", "state")
    assert (report.current, report.previous) == (None, None)
    assert world.link.resolve() == (world.versions / "2.1.319").resolve()


def test_apply_with_unreadable_pins_is_an_error(world: World) -> None:
    applyable(world)
    (world.hub / "config" / "pins.toml").write_text("[claude\n", encoding="utf-8")
    before = snapshot(world)
    report = apply(world)
    assert (report.status, report.reason) == ("error", "pins")
    unchanged(world, before, report)


def test_apply_while_locked_is_busy(world: World) -> None:
    applyable(world)
    before = snapshot(world)
    stream = hold_lock(world)
    try:
        report = apply(world)
    finally:
        stream.close()
    assert (report.status, report.reason) == ("busy", None)
    unchanged(world, before, report)


def test_a_link_that_cannot_be_written_is_an_error(world: World) -> None:
    applyable(world)
    before = snapshot(world)
    world.link.parent.chmod(0o555)
    try:
        report = apply(world)
    finally:
        world.link.parent.chmod(0o755)
    assert (report.status, report.reason) == ("error", "link_write")
    unchanged(world, before, report)


def test_a_failed_state_write_restores_the_link(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    applyable(world)
    before = snapshot(world)

    def fail(path: Path, state: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(selfcheck, "save_agents_state", fail)
    report = apply(world)
    assert (report.status, report.reason) == ("error", "state_write")
    unchanged(world, before, report)
    assert (world.versions / "2.1.326").is_file()


def test_an_unexpected_exception_is_an_internal_error(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    applyable(world)
    before = snapshot(world)

    def boom(path: Path) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(selfcheck, "sha256_file", boom)
    report = apply(world)
    assert (report.status, report.reason) == ("error", "internal")
    unchanged(world, before, report)


def test_an_unexpected_os_error_is_an_io_error(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    applyable(world)
    before = snapshot(world)

    def boom(path: Path) -> str:
        raise PermissionError("denied")

    monkeypatch.setattr(selfcheck, "sha256_file", boom)
    report = apply(world)
    assert (report.status, report.reason) == ("error", "io")
    unchanged(world, before, report)


# rollback


def test_rollback_swaps_current_and_previous(world: World) -> None:
    applyable(world)
    assert apply(world).status == "applied"
    applied = world.claude_state()["current"]
    old = world.claude_state()["previous"]
    report = rollback(world)
    assert (report.status, report.reason) == ("rolled_back", None)
    assert (report.current, report.previous) == ("2.1.319", "2.1.326")
    assert report.hold == "claude@2.1.326"
    target = (world.versions / "2.1.319").resolve()
    assert os.readlink(world.link) == str(target)
    claude = world.claude_state()
    assert claude["current"] == {
        "version": "2.1.319",
        "sha256": old["sha256"],  # type: ignore[index]
        "path": old["path"],  # type: ignore[index]
        # The rolled-back record carries its own resolved models.
        "models": old["models"],  # type: ignore[index]
        "at": LATER.isoformat(),
        "source": "rollback",
    }
    assert claude["previous"] == applied
    assert claude["candidate"] is None
    assert (world.versions / "2.1.326").is_file()
    assert not [p for p in world.link.parent.iterdir() if p.name != "claude"]
    lock_is_free(world)


def test_a_second_rollback_returns(world: World) -> None:
    applyable(world)
    apply(world)
    rollback(world)
    report = rollback(world)
    assert (report.status, report.current, report.previous) == (
        "rolled_back",
        "2.1.326",
        "2.1.319",
    )
    assert report.hold == "claude@2.1.319"
    assert world.link.resolve() == (world.versions / "2.1.326").resolve()
    assert world.claude_state()["current"]["source"] == "rollback"  # type: ignore[index]


def rolled_world(world: World) -> None:
    """current 2.1.326 (apply), previous 2.1.319, link on 2.1.326."""
    world.record_current()
    state = world.state()
    claude = state["agents"]["claude"]  # type: ignore[index]
    claude["previous"] = claude["current"]
    claude["current"] = installed_entry(world, "2.1.326", "apply")
    world.write_state(state)
    world.point_link("2.1.326")


def test_rollback_keeps_the_candidate_and_its_install(world: World) -> None:
    rolled_world(world)
    world.install("2.1.318")
    state = world.state()
    candidate = ready_candidate(world, "2.1.327")
    state["agents"]["claude"]["candidate"] = candidate  # type: ignore[index]
    world.write_state(state)
    report = rollback(world)
    assert report.status == "rolled_back"
    assert report.removed == ("2.1.318",)
    assert world.claude_state()["candidate"] == candidate
    assert sorted(p.name for p in world.versions.iterdir()) == [
        "2.1.319",
        "2.1.326",
        "2.1.327",
    ]


def test_rollback_removes_a_failed_candidates_leftover(world: World) -> None:
    rolled_world(world)
    leftover = world.install("2.1.327")
    state = world.state()
    candidate = ready_candidate(world, "2.1.327")
    candidate.update({"status": "failed", "sha256": None, "path": None})
    state["agents"]["claude"]["candidate"] = candidate  # type: ignore[index]
    world.write_state(state)
    report = rollback(world)
    assert report.removed == ("2.1.327",)
    assert not leftover.exists()


def rb_no_previous(world: World) -> None:
    world.record_current()


def rb_no_current(world: World) -> None:
    rolled_world(world)
    state = world.state()
    state["agents"]["claude"]["current"] = None  # type: ignore[index]
    world.write_state(state)


def rb_link_elsewhere(world: World) -> None:
    rolled_world(world)
    world.point_link("2.1.319")


def rb_binary_changed(world: World) -> None:
    rolled_world(world)
    (world.versions / "2.1.319").write_bytes(b"changed")


def rb_binary_missing(world: World) -> None:
    rolled_world(world)
    (world.versions / "2.1.319").unlink()


def rb_path_outside(world: World) -> None:
    rolled_world(world)
    outside = world.root / "elsewhere" / "2.1.319"
    outside.parent.mkdir()
    outside.write_bytes(binary_bytes("2.1.319"))
    state = world.state()
    state["agents"]["claude"]["previous"]["path"] = str(outside)  # type: ignore[index]
    world.write_state(state)


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        (rb_no_previous, "no_previous"),
        (rb_no_current, "no_current"),
        (rb_link_elsewhere, "link"),
        (rb_binary_changed, "binary_changed"),
        (rb_binary_missing, "binary_changed"),
        (rb_path_outside, "path"),
    ],
)
def test_rollback_refuses_and_changes_nothing(world: World, setup, reason: str) -> None:  # type: ignore[no-untyped-def]
    setup(world)
    before = snapshot(world)
    files = sorted(p.name for p in world.versions.iterdir())
    report = rollback(world)
    assert (report.status, report.reason) == ("refused", reason)
    unchanged(world, before, report)
    assert sorted(p.name for p in world.versions.iterdir()) == files


def test_rollback_does_not_read_the_pins(world: World) -> None:
    rolled_world(world)
    (world.hub / "config" / "pins.toml").write_text("[claude\n", encoding="utf-8")
    assert rollback(world).status == "rolled_back"


def test_rollback_while_locked_is_busy(world: World) -> None:
    rolled_world(world)
    before = snapshot(world)
    stream = hold_lock(world)
    try:
        report = rollback(world)
    finally:
        stream.close()
    assert (report.status, report.reason) == ("busy", None)
    unchanged(world, before, report)


def test_rollback_with_an_unreadable_state_is_an_error(world: World) -> None:
    rolled_world(world)
    world.state_path.write_text("[]", encoding="utf-8")
    report = rollback(world)
    assert (report.status, report.reason) == ("error", "state")
    assert world.link.resolve() == (world.versions / "2.1.326").resolve()


def test_a_failed_rollback_state_write_restores_the_link(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    rolled_world(world)
    before = snapshot(world)

    def fail(path: Path, state: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(selfcheck, "save_agents_state", fail)
    report = rollback(world)
    assert (report.status, report.reason) == ("error", "state_write")
    unchanged(world, before, report)


# check_current_claude


def test_the_recorded_binary_is_ok(world: World) -> None:
    world.record_current()
    assert check(world) == CurrentCheck("ok", None, "2.1.319")
    target = str((world.versions / "2.1.319").resolve())
    assert [call for call in world.commands.calls] == [([target, "--version"], 30)]


def test_the_check_neither_writes_nor_locks(world: World) -> None:
    world.record_current()
    before = world.state_path.read_bytes()
    stream = hold_lock(world)
    try:
        assert check(world).status == "ok"
    finally:
        stream.close()
    assert world.state_path.read_bytes() == before


def test_the_check_after_apply_follows_the_link(world: World) -> None:
    applyable(world)
    apply(world)
    assert check(world) == CurrentCheck("ok", None, "2.1.326")


def test_no_state_file_has_no_record(world: World) -> None:
    assert check(world) == CurrentCheck("no_record", None, None)
    assert not world.state_path.exists()


def test_a_state_without_current_has_no_record(world: World) -> None:
    world.write_state({"format": 1, "agents": {}})
    assert check(world) == CurrentCheck("no_record", None, None)


def test_an_unreadable_state_is_an_error(world: World) -> None:
    world.state_path.write_text("{", encoding="utf-8")
    assert check(world) == CurrentCheck("error", "state", None)


def test_another_binary_is_a_path_mismatch(world: World) -> None:
    world.record_current()
    world.install("2.1.318")
    world.point_link("2.1.318")
    assert check(world) == CurrentCheck("mismatch", "path", "2.1.319")
    assert world.commands.calls == []


def test_the_configured_path_is_compared_resolved(world: World) -> None:
    world.record_current()
    other = world.root / "claude"
    other.symlink_to(world.versions / "2.1.319")
    assert check(world, other).status == "ok"


def test_a_missing_binary_is_a_mismatch(world: World) -> None:
    world.record_current()
    (world.versions / "2.1.319").unlink()
    assert check(world) == CurrentCheck("mismatch", "missing", "2.1.319")


def test_a_changed_binary_is_a_sha256_mismatch(world: World) -> None:
    world.record_current()
    (world.versions / "2.1.319").write_bytes(b"updated outside selfcheck")
    assert check(world) == CurrentCheck("mismatch", "sha256", "2.1.319")


def test_a_different_version_output_is_a_mismatch(world: World) -> None:
    world.record_current()
    world.commands.versions["2.1.319"] = "2.1.318"
    assert check(world) == CurrentCheck("mismatch", "version", "2.1.319")


def test_a_failing_version_command_is_a_mismatch(world: World) -> None:
    world.record_current()
    world.commands.version_codes["2.1.319"] = 1
    assert check(world) == CurrentCheck("mismatch", "version", "2.1.319")


@pytest.mark.parametrize(
    "error", [OSError("exec"), subprocess.TimeoutExpired(["claude"], 30)]
)
def test_a_version_command_that_cannot_run_is_a_mismatch(
    world: World, error: Exception
) -> None:
    world.record_current()
    world.commands.raises["version:2.1.319"] = error
    assert check(world) == CurrentCheck("mismatch", "version", "2.1.319")


def test_an_unexpected_exception_in_the_check_is_internal(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.record_current()

    def boom(path: Path) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(selfcheck, "sha256_file", boom)
    assert check(world) == CurrentCheck("error", "internal", None)


def test_an_unexpected_os_error_in_the_check_is_io(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.record_current()

    def boom(path: Path) -> str:
        raise PermissionError("denied")

    monkeypatch.setattr(selfcheck, "sha256_file", boom)
    assert check(world) == CurrentCheck("error", "io", None)


# P2: a failed candidate is never the link's target when it is removed


def test_an_unrestored_link_keeps_the_candidate_it_points_to(world: World) -> None:
    world.release.publish_measured()
    world.record_current()
    bin_dir = world.link.parent

    def relink(key: str) -> None:
        if key == "inject":
            world.link.unlink()
            world.link.symlink_to(world.versions / "2.1.326")
            bin_dir.chmod(0o555)

    world.claude.on_call = relink
    try:
        report = world.run()
    finally:
        bin_dir.chmod(0o755)
    assert (report.status, report.reason) == ("failed", "link_changed")
    assert world.link.resolve() == (world.versions / "2.1.326").resolve()
    assert (world.versions / "2.1.326").is_file()
    assert (world.versions / "2.1.319").is_file()
    candidate = world.claude_state()["candidate"]
    assert (candidate["status"], candidate["sha256"], candidate["path"]) == (  # type: ignore[index]
        "failed",
        None,
        None,
    )


def test_a_restored_link_still_lets_the_failed_candidate_go(world: World) -> None:
    world.release.publish_measured()
    world.record_current()

    def relink(key: str) -> None:
        if key == "inject":
            world.link.unlink()
            world.link.symlink_to(world.versions / "2.1.326")

    world.claude.on_call = relink
    report = world.run()
    assert (report.status, report.reason) == ("failed", "link_changed")
    assert world.link.resolve() == (world.versions / "2.1.319").resolve()
    assert not (world.versions / "2.1.326").exists()
