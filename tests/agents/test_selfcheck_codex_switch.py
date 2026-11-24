"""m-selfcheck-codex-switch: orch selfcheck --apply codex and --rollback codex.

Uses the helpers of test_selfcheck_codex.py. Nothing here uses the network or runs
the real codex, mise or codesign.
"""

import fcntl
import hashlib
import json
import os
from pathlib import Path

import pytest

from orch import selfcheck, selfcheck_codex
from orch.selfcheck import CurrentCheck, SwitchReport
from orch.selfcheck_codex import (
    MISE_FILES,
    PREVIOUS_DIR,
    CodexLayout,
    codex_layout,
    config_text,
    load_codex_pins,
)
from test_selfcheck_codex import (
    LOCK_HEADER,
    MACHINE,
    NOW,
    PASSED_CHECKS,
    FakeCommands,
    build_package,
    lock_entry,
    make_hub,
)

LATER = NOW.replace(hour=13)
CURRENT = "0.160.0"
NEW = "0.170.0"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network used")

    monkeypatch.setattr("urllib.request.urlopen", refuse)


class World:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.hub = make_hub(root)
        self.home = root / "home"
        self.home.mkdir()
        self.orch_home = root / "orch"
        self.orch_home.mkdir()

    @property
    def layout(self) -> CodexLayout:
        return codex_layout(self.orch_home)

    @property
    def state_path(self) -> Path:
        return self.orch_home / "agents.json"

    @property
    def previous_dir(self) -> Path:
        return self.layout.root / PREVIOUS_DIR

    def install(self, version: str) -> Path:
        binary = self.layout.installs / version / "bin" / "codex"
        build_package(binary.parents[1], version)
        return binary

    def state(self) -> dict[str, object]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def codex(self) -> dict[str, object]:
        return self.state()["agents"]["codex"]  # type: ignore[index]

    def write_state(self, state: dict[str, object]) -> None:
        selfcheck.save_agents_state(self.state_path, state)

    def point_link(self, version: str) -> None:
        link = self.layout.link
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(self.layout.installs / version / "bin" / "codex")


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


# Helpers


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_mise_files(directory: Path, version: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.toml").write_text(config_text(version), encoding="utf-8")
    (directory / "mise.lock").write_text(
        LOCK_HEADER + lock_entry(version), encoding="utf-8"
    )


def mise_version(directory: Path) -> str | None:
    """The version both mise files of a directory name, or None."""
    config = directory / "config.toml"
    lock = directory / "mise.lock"
    if not config.is_file() or not lock.is_file():
        return None
    text = config.read_text(encoding="utf-8")
    for version in ("0.150.0", CURRENT, NEW, "0.171.0"):
        if text == config_text(version):
            assert f'version = "{version}"' in lock.read_text(encoding="utf-8")
            return version
    return None


def entry(world: World, version: str, source: str) -> dict[str, object]:
    binary = world.install(version)
    return {
        "version": version,
        "sha256": sha(binary),
        "path": str(binary),
        "at": "2026-11-20T00:00:00+00:00",
        "source": source,
    }


def ready_candidate(world: World, version: str = NEW) -> dict[str, object]:
    binary = world.install(version)
    return {
        "version": version,
        "status": "ready",
        "sha256": sha(binary),
        "path": str(binary),
        "pins_sha256": load_codex_pins(world.hub).sha256,
        "checks": PASSED_CHECKS,
        "warnings": [],
        "at": "2026-11-21T00:00:00+00:00",
    }


CLAUDE_ENTRY = {
    "current": {
        "version": "2.1.319",
        "sha256": "a" * 64,
        "path": "/x/2.1.319",
        "at": "2026-11-18T00:00:00+00:00",
        "source": "bootstrap",
    },
    "previous": None,
    "candidate": None,
}


def applyable(world: World) -> None:
    """current 0.160.0 (bootstrap), a ready 0.170.0 candidate, link on 0.160.0."""
    state = {
        "format": 1,
        "agents": {
            "claude": CLAUDE_ENTRY,
            "codex": {
                "current": entry(world, CURRENT, "bootstrap"),
                "previous": None,
                "candidate": ready_candidate(world),
            },
        },
    }
    world.write_state(state)
    world.point_link(CURRENT)
    write_mise_files(world.layout.root, CURRENT)
    write_mise_files(world.layout.candidate, NEW)


def rolled(world: World) -> None:
    """current 0.170.0 (apply), previous 0.160.0, link on 0.170.0."""
    state = {
        "format": 1,
        "agents": {
            "codex": {
                "current": entry(world, NEW, "apply"),
                "previous": entry(world, CURRENT, "bootstrap"),
                "candidate": None,
            },
        },
    }
    world.write_state(state)
    world.point_link(NEW)
    write_mise_files(world.layout.root, NEW)
    write_mise_files(world.previous_dir, CURRENT)


def apply(world: World, machine: str = MACHINE) -> SwitchReport:
    return selfcheck_codex.apply_codex(
        hub=world.hub,
        orch_home=world.orch_home,
        home=world.home,
        now=LATER,
        machine=machine,
    )


def rollback(world: World, machine: str = MACHINE) -> SwitchReport:
    return selfcheck_codex.rollback_codex(
        orch_home=world.orch_home, home=world.home, now=LATER, machine=machine
    )


def snapshot(world: World) -> dict[str, object]:
    """Everything a switch may change, outside the install contents."""
    files: dict[str, object] = {}
    root = world.layout.root
    if root.exists():
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if relative.parts[0] == "data":
                continue
            if path.is_symlink():
                files[str(relative)] = ("link", os.readlink(path))
            elif path.is_file():
                files[str(relative)] = path.read_bytes()
            else:
                files[str(relative)] = "dir"
    link = world.layout.link
    return {
        "state": world.state_path.read_bytes() if world.state_path.exists() else None,
        "link": os.readlink(link) if link.is_symlink() else link.exists(),
        "bin": sorted(p.name for p in link.parent.iterdir())
        if link.parent.exists()
        else [],
        "installs": sorted(p.name for p in world.layout.installs.iterdir())
        if world.layout.installs.exists()
        else [],
        "files": files,
    }


def lock_is_free(world: World) -> None:
    with (world.orch_home / "agents.json.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)


def unchanged(world: World, before: dict[str, object], report: SwitchReport) -> None:
    assert snapshot(world) == before
    assert report.hold is None
    assert report.removed == ()
    lock_is_free(world)


def hold_lock(world: World):  # type: ignore[no-untyped-def]
    stream = (world.orch_home / "agents.json.lock").open("a")
    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return stream


def installed(world: World) -> list[str]:
    return sorted(p.name for p in world.layout.installs.iterdir())


def no_leftovers(world: World) -> None:
    assert [p.name for p in world.layout.link.parent.iterdir()] == ["codex"]
    assert not [p for p in world.layout.root.iterdir() if p.name.startswith(".")], (
        "temporary files left in the mise root"
    )


# Constants


def test_the_previous_directory_and_files() -> None:
    assert PREVIOUS_DIR == "previous"
    assert MISE_FILES == ("config.toml", "mise.lock")


# apply


def test_apply_switches_the_link_the_files_and_the_record(world: World) -> None:
    applyable(world)
    old_current = world.codex()["current"]
    candidate = world.codex()["candidate"]
    report = apply(world)
    target = (world.layout.installs / NEW / "bin" / "codex").resolve()
    assert report == SwitchReport("applied", None, NEW, CURRENT, None, ())
    assert os.readlink(world.layout.link) == str(target)
    codex = world.codex()
    assert codex["current"] == {
        "version": NEW,
        "sha256": candidate["sha256"],  # type: ignore[index]
        "path": str(target),
        "at": LATER.isoformat(),
        "source": "apply",
    }
    assert codex["previous"] == old_current
    assert codex["candidate"] is None
    assert world.state()["agents"]["claude"] == CLAUDE_ENTRY  # type: ignore[index]
    assert mise_version(world.layout.root) == NEW
    assert mise_version(world.previous_dir) == CURRENT
    assert not (world.layout.candidate / "config.toml").exists()
    assert not (world.layout.candidate / "mise.lock").exists()
    assert installed(world) == [CURRENT, NEW]
    no_leftovers(world)
    lock_is_free(world)


def test_apply_keeps_only_the_current_and_previous_installs(world: World) -> None:
    applyable(world)
    world.install("0.150.0")
    world.install("0.165.0")
    (world.layout.installs / "notes.txt").write_bytes(b"keep")
    (world.layout.installs / "0.140.0").write_bytes(b"a file, not a directory")
    (world.layout.installs / "latest").symlink_to(f"./{CURRENT}")
    (world.layout.installs / "0.130.0").symlink_to(f"./{CURRENT}")
    report = apply(world)
    assert report.status == "applied"
    assert report.removed == ("0.150.0", "0.165.0")
    assert installed(world) == [
        "0.130.0",
        "0.140.0",
        CURRENT,
        NEW,
        "latest",
        "notes.txt",
    ]


def test_apply_replaces_the_old_previous_files_and_install(world: World) -> None:
    applyable(world)
    state = world.state()
    state["agents"]["codex"]["previous"] = entry(world, "0.150.0", "bootstrap")  # type: ignore[index]
    world.write_state(state)
    write_mise_files(world.previous_dir, "0.150.0")
    (world.previous_dir / "stray").write_bytes(b"x")
    report = apply(world)
    assert (report.status, report.removed) == ("applied", ("0.150.0",))
    assert world.codex()["previous"]["version"] == CURRENT  # type: ignore[index]
    assert mise_version(world.previous_dir) == CURRENT
    assert sorted(p.name for p in world.previous_dir.iterdir()) == list(
        sorted(MISE_FILES)
    )
    no_leftovers(world)


def test_a_relative_link_to_the_current_install_is_accepted(world: World) -> None:
    applyable(world)
    link = world.layout.link
    link.unlink()
    link.symlink_to(
        os.path.relpath(world.layout.installs / CURRENT / "bin" / "codex", link.parent)
    )
    assert apply(world).status == "applied"
    assert link.resolve() == (world.layout.installs / NEW / "bin" / "codex").resolve()


def test_the_check_after_apply_is_ok(world: World) -> None:
    applyable(world)
    apply(world)
    commands = FakeCommands()
    assert selfcheck_codex.check_current_codex(
        orch_home=world.orch_home, codex_path=world.layout.link, command=commands
    ) == CurrentCheck("ok", None, NEW)


# apply refusals


def _set_candidate(world: World, **fields: object) -> None:
    state = world.state()
    state["agents"]["codex"]["candidate"].update(fields)  # type: ignore[index]
    world.write_state(state)


def refuse_no_current(world: World) -> None:
    applyable(world)
    state = world.state()
    state["agents"]["codex"]["current"] = None  # type: ignore[index]
    world.write_state(state)


def refuse_no_codex_entry(world: World) -> None:
    applyable(world)
    state = world.state()
    del state["agents"]["codex"]  # type: ignore[attr-defined]
    world.write_state(state)


def refuse_no_candidate(world: World) -> None:
    applyable(world)
    state = world.state()
    state["agents"]["codex"]["candidate"] = None  # type: ignore[index]
    world.write_state(state)


def refuse_failed_candidate(world: World) -> None:
    applyable(world)
    _set_candidate(world, status="failed", sha256=None, path=None)


def refuse_not_automated_candidate(world: World) -> None:
    applyable(world)
    _set_candidate(world, status="not_automated", sha256=None, path=None)


def refuse_pins_changed(world: World) -> None:
    applyable(world)
    _set_candidate(world, pins_sha256="0" * 64)


def refuse_not_newer(world: World) -> None:
    applyable(world)
    binary = world.install("0.150.0")
    _set_candidate(world, version="0.150.0", path=str(binary), sha256=sha(binary))


def refuse_same_version(world: World) -> None:
    applyable(world)
    binary = world.layout.installs / CURRENT / "bin" / "codex"
    _set_candidate(world, version=CURRENT, path=str(binary), sha256=sha(binary))


def refuse_link_elsewhere(world: World) -> None:
    applyable(world)
    world.point_link(NEW)


def refuse_link_is_a_file(world: World) -> None:
    applyable(world)
    world.layout.link.unlink()
    world.layout.link.write_bytes(b"codex")


def refuse_link_missing(world: World) -> None:
    applyable(world)
    world.layout.link.unlink()


def refuse_path_outside(world: World) -> None:
    applyable(world)
    outside = world.root / "elsewhere" / NEW / "bin" / "codex"
    build_package(outside.parents[1], NEW)
    _set_candidate(world, path=str(outside))


def refuse_path_other_version(world: World) -> None:
    applyable(world)
    other = world.install("0.171.0")
    other.write_bytes((world.layout.installs / NEW / "bin" / "codex").read_bytes())
    _set_candidate(world, path=str(other))


def refuse_path_other_file(world: World) -> None:
    applyable(world)
    other = world.layout.installs / NEW / "bin" / "codex-code-mode-host"
    _set_candidate(world, path=str(other), sha256=sha(other))


def refuse_binary_changed(world: World) -> None:
    applyable(world)
    (world.layout.installs / NEW / "bin" / "codex").write_bytes(b"changed")


def refuse_binary_missing(world: World) -> None:
    applyable(world)
    (world.layout.installs / NEW / "bin" / "codex").unlink()


def refuse_binary_is_a_directory(world: World) -> None:
    applyable(world)
    binary = world.layout.installs / NEW / "bin" / "codex"
    binary.unlink()
    binary.mkdir()


def refuse_root_config_other_version(world: World) -> None:
    applyable(world)
    (world.layout.root / "config.toml").write_text(
        config_text("0.150.0"), encoding="utf-8"
    )


def refuse_root_lock_missing(world: World) -> None:
    applyable(world)
    (world.layout.root / "mise.lock").unlink()


def refuse_root_lock_without_entry(world: World) -> None:
    applyable(world)
    (world.layout.root / "mise.lock").write_text(LOCK_HEADER, encoding="utf-8")


def refuse_candidate_config_other_version(world: World) -> None:
    applyable(world)
    (world.layout.candidate / "config.toml").write_text(
        config_text("0.171.0"), encoding="utf-8"
    )


def refuse_candidate_config_missing(world: World) -> None:
    applyable(world)
    (world.layout.candidate / "config.toml").unlink()


def refuse_candidate_lock_other_version(world: World) -> None:
    applyable(world)
    (world.layout.candidate / "mise.lock").write_text(
        LOCK_HEADER + lock_entry("0.171.0"), encoding="utf-8"
    )


def refuse_candidate_lock_without_the_platform(world: World) -> None:
    applyable(world)
    # lock_entry(arm=False) still has the x86_64 entry, so drop it by hand.
    text = (LOCK_HEADER + lock_entry(NEW)).replace("platforms.macos-x64", "platforms.x")
    (world.layout.candidate / "mise.lock").write_text(text, encoding="utf-8")


def refuse_candidate_config_is_a_link(world: World) -> None:
    applyable(world)
    config = world.layout.candidate / "config.toml"
    real = world.root / "real-config.toml"
    real.write_text(config.read_text(encoding="utf-8"), encoding="utf-8")
    config.unlink()
    config.symlink_to(real)


def refuse_root_lock_is_a_link(world: World) -> None:
    applyable(world)
    lock = world.layout.root / "mise.lock"
    real = world.root / "real.lock"
    real.write_text(lock.read_text(encoding="utf-8"), encoding="utf-8")
    lock.unlink()
    lock.symlink_to(real)


APPLY_REFUSALS = [
    (refuse_no_current, "no_current"),
    (refuse_no_codex_entry, "no_current"),
    (refuse_no_candidate, "no_ready_candidate"),
    (refuse_failed_candidate, "no_ready_candidate"),
    (refuse_not_automated_candidate, "no_ready_candidate"),
    (refuse_pins_changed, "pins_changed"),
    (refuse_not_newer, "not_newer"),
    (refuse_same_version, "not_newer"),
    (refuse_link_elsewhere, "link"),
    (refuse_link_is_a_file, "link"),
    (refuse_link_missing, "link"),
    (refuse_path_outside, "path"),
    (refuse_path_other_version, "path"),
    (refuse_path_other_file, "path"),
    (refuse_binary_changed, "binary_changed"),
    (refuse_binary_missing, "binary_changed"),
    (refuse_binary_is_a_directory, "binary_changed"),
    (refuse_root_config_other_version, "config"),
    (refuse_root_lock_missing, "config"),
    (refuse_root_lock_without_entry, "config"),
    (refuse_candidate_config_other_version, "config"),
    (refuse_candidate_config_missing, "config"),
    (refuse_candidate_lock_other_version, "config"),
    (refuse_candidate_lock_without_the_platform, "config"),
    (refuse_candidate_config_is_a_link, "config"),
    (refuse_root_lock_is_a_link, "config"),
]


@pytest.mark.parametrize(
    ("setup", "reason"), APPLY_REFUSALS, ids=[s.__name__ for s, _ in APPLY_REFUSALS]
)
def test_apply_refuses_and_changes_nothing(world: World, setup, reason: str) -> None:  # type: ignore[no-untyped-def]
    setup(world)
    before = snapshot(world)
    report = apply(world)
    assert (report.status, report.reason) == ("refused", reason)
    unchanged(world, before, report)


def test_a_refused_apply_reports_the_recorded_versions(world: World) -> None:
    refuse_pins_changed(world)
    report = apply(world)
    assert (report.current, report.previous) == (CURRENT, None)


def test_the_candidate_lock_is_checked_for_the_given_machine(world: World) -> None:
    applyable(world)
    # An x86_64-only lock passes on x86_64 and is refused on arm64.
    text = (LOCK_HEADER + lock_entry(NEW)).replace(
        "platforms.macos-arm64", "platforms.x"
    )
    (world.layout.candidate / "mise.lock").write_text(text, encoding="utf-8")
    (world.layout.root / "mise.lock").write_text(
        LOCK_HEADER + lock_entry(CURRENT), encoding="utf-8"
    )
    before = snapshot(world)
    report = apply(world, machine="arm64")
    assert (report.status, report.reason) == ("refused", "config")
    unchanged(world, before, report)
    assert apply(world, machine="x86_64").status == "applied"


def test_apply_without_a_state_file_is_refused(world: World) -> None:
    report = apply(world)
    assert (report.status, report.reason) == ("refused", "no_current")
    assert not world.state_path.exists()
    lock_is_free(world)


def test_apply_with_an_unreadable_state_is_an_error(world: World) -> None:
    applyable(world)
    world.state_path.write_text("{not json", encoding="utf-8")
    before = snapshot(world)
    report = apply(world)
    assert (report.status, report.reason) == ("error", "state")
    assert (report.current, report.previous) == (None, None)
    unchanged(world, before, report)


def test_apply_with_unreadable_pins_is_an_error(world: World) -> None:
    applyable(world)
    (world.hub / "config" / "pins.toml").write_text("[codex\n", encoding="utf-8")
    before = snapshot(world)
    report = apply(world)
    assert (report.status, report.reason) == ("error", "pins")
    unchanged(world, before, report)


def test_apply_on_an_unsupported_machine_is_an_error(world: World) -> None:
    applyable(world)
    before = snapshot(world)
    report = apply(world, machine="sparc")
    assert (report.status, report.reason) == ("error", "platform")
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
    assert snapshot(world) == before


# apply failures


def with_old_previous(world: World) -> None:
    applyable(world)
    state = world.state()
    state["agents"]["codex"]["previous"] = entry(world, "0.150.0", "bootstrap")  # type: ignore[index]
    world.write_state(state)
    write_mise_files(world.previous_dir, "0.150.0")


@pytest.mark.parametrize("setup", [applyable, with_old_previous])
def test_mise_files_that_cannot_move_are_an_error(world: World, setup) -> None:  # type: ignore[no-untyped-def]
    setup(world)
    before = snapshot(world)
    world.layout.root.chmod(0o555)
    try:
        report = apply(world)
    finally:
        world.layout.root.chmod(0o755)
    assert (report.status, report.reason) == ("error", "config_write")
    unchanged(world, before, report)


@pytest.mark.parametrize("setup", [applyable, with_old_previous])
def test_a_link_that_cannot_be_written_restores_the_files(world: World, setup) -> None:  # type: ignore[no-untyped-def]
    setup(world)
    before = snapshot(world)
    world.layout.link.parent.chmod(0o555)
    try:
        report = apply(world)
    finally:
        world.layout.link.parent.chmod(0o755)
    assert (report.status, report.reason) == ("error", "link_write")
    unchanged(world, before, report)


@pytest.mark.parametrize("setup", [applyable, with_old_previous])
def test_a_failed_state_write_restores_the_link_and_files(
    world: World, monkeypatch: pytest.MonkeyPatch, setup
) -> None:  # type: ignore[no-untyped-def]
    setup(world)
    before = snapshot(world)

    def fail(path: Path, state: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(selfcheck_codex, "save_agents_state", fail)
    report = apply(world)
    assert (report.status, report.reason) == ("error", "state_write")
    unchanged(world, before, report)
    assert (world.layout.installs / NEW / "bin" / "codex").is_file()


def test_an_unexpected_exception_is_an_internal_error(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    applyable(world)
    before = snapshot(world)

    def boom(path: Path) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(selfcheck_codex, "sha256_file", boom)
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

    monkeypatch.setattr(selfcheck_codex, "sha256_file", boom)
    report = apply(world)
    assert (report.status, report.reason) == ("error", "io")
    unchanged(world, before, report)


# rollback


def test_rollback_swaps_current_and_previous(world: World) -> None:
    rolled(world)
    old_current = world.codex()["current"]
    old_previous = world.codex()["previous"]
    report = rollback(world)
    assert report == SwitchReport("rolled_back", None, CURRENT, NEW, f"codex@{NEW}", ())
    target = (world.layout.installs / CURRENT / "bin" / "codex").resolve()
    assert os.readlink(world.layout.link) == str(target)
    codex = world.codex()
    assert codex["current"] == {
        "version": CURRENT,
        "sha256": old_previous["sha256"],  # type: ignore[index]
        "path": old_previous["path"],  # type: ignore[index]
        "at": LATER.isoformat(),
        "source": "rollback",
    }
    assert codex["previous"] == old_current
    assert codex["candidate"] is None
    assert mise_version(world.layout.root) == CURRENT
    assert mise_version(world.previous_dir) == NEW
    assert installed(world) == [CURRENT, NEW]
    no_leftovers(world)
    lock_is_free(world)


def test_a_second_rollback_returns(world: World) -> None:
    rolled(world)
    rollback(world)
    report = rollback(world)
    assert (report.status, report.current, report.previous, report.hold) == (
        "rolled_back",
        NEW,
        CURRENT,
        f"codex@{CURRENT}",
    )
    assert (
        world.layout.link.resolve()
        == (world.layout.installs / NEW / "bin" / "codex").resolve()
    )
    assert mise_version(world.layout.root) == NEW
    assert mise_version(world.previous_dir) == CURRENT


def test_apply_then_rollback_round_trip(world: World) -> None:
    applyable(world)
    assert apply(world).status == "applied"
    report = rollback(world)
    assert (report.status, report.current, report.previous) == (
        "rolled_back",
        CURRENT,
        NEW,
    )
    assert (
        world.layout.link.resolve()
        == (world.layout.installs / CURRENT / "bin" / "codex").resolve()
    )
    assert mise_version(world.layout.root) == CURRENT
    assert mise_version(world.previous_dir) == NEW
    commands = FakeCommands()
    assert selfcheck_codex.check_current_codex(
        orch_home=world.orch_home, codex_path=world.layout.link, command=commands
    ) == CurrentCheck("ok", None, CURRENT)


def test_rollback_keeps_a_ready_candidate_and_its_install(world: World) -> None:
    rolled(world)
    world.install("0.150.0")
    state = world.state()
    candidate = ready_candidate(world, "0.171.0")
    state["agents"]["codex"]["candidate"] = candidate  # type: ignore[index]
    world.write_state(state)
    report = rollback(world)
    assert (report.status, report.removed) == ("rolled_back", ("0.150.0",))
    assert world.codex()["candidate"] == candidate
    assert installed(world) == [CURRENT, NEW, "0.171.0"]


def test_rollback_removes_a_failed_candidates_leftover(world: World) -> None:
    rolled(world)
    world.install("0.171.0")
    state = world.state()
    candidate = ready_candidate(world, "0.171.0")
    candidate.update({"status": "failed", "sha256": None, "path": None})
    state["agents"]["codex"]["candidate"] = candidate  # type: ignore[index]
    world.write_state(state)
    report = rollback(world)
    assert report.removed == ("0.171.0",)
    assert installed(world) == [CURRENT, NEW]


def rb_no_previous(world: World) -> None:
    rolled(world)
    state = world.state()
    state["agents"]["codex"]["previous"] = None  # type: ignore[index]
    world.write_state(state)


def rb_no_current(world: World) -> None:
    rolled(world)
    state = world.state()
    state["agents"]["codex"]["current"] = None  # type: ignore[index]
    world.write_state(state)


def rb_link_elsewhere(world: World) -> None:
    rolled(world)
    world.point_link(CURRENT)


def rb_binary_changed(world: World) -> None:
    rolled(world)
    (world.layout.installs / CURRENT / "bin" / "codex").write_bytes(b"changed")


def rb_binary_missing(world: World) -> None:
    rolled(world)
    (world.layout.installs / CURRENT / "bin" / "codex").unlink()


def rb_path_outside(world: World) -> None:
    rolled(world)
    outside = world.root / "elsewhere" / CURRENT / "bin" / "codex"
    build_package(outside.parents[1], CURRENT)
    state = world.state()
    state["agents"]["codex"]["previous"]["path"] = str(outside)  # type: ignore[index]
    world.write_state(state)


def rb_previous_files_missing(world: World) -> None:
    rolled(world)
    for name in MISE_FILES:
        (world.previous_dir / name).unlink()
    world.previous_dir.rmdir()


def rb_previous_lock_missing(world: World) -> None:
    rolled(world)
    (world.previous_dir / "mise.lock").unlink()


def rb_previous_files_other_version(world: World) -> None:
    rolled(world)
    write_mise_files(world.previous_dir, "0.150.0")


def rb_previous_dir_is_a_link(world: World) -> None:
    rolled(world)
    real = world.root / "real-previous"
    world.previous_dir.rename(real)
    world.previous_dir.symlink_to(real)


def rb_previous_dir_is_a_file(world: World) -> None:
    rolled(world)
    for name in MISE_FILES:
        (world.previous_dir / name).unlink()
    world.previous_dir.rmdir()
    world.previous_dir.write_bytes(b"x")


def rb_root_files_other_version(world: World) -> None:
    rolled(world)
    write_mise_files(world.layout.root, CURRENT)


RB_REFUSALS = [
    (rb_no_previous, "no_previous"),
    (rb_no_current, "no_current"),
    (rb_link_elsewhere, "link"),
    (rb_binary_changed, "binary_changed"),
    (rb_binary_missing, "binary_changed"),
    (rb_path_outside, "path"),
    (rb_previous_files_missing, "config"),
    (rb_previous_lock_missing, "config"),
    (rb_previous_files_other_version, "config"),
    (rb_previous_dir_is_a_link, "config"),
    (rb_previous_dir_is_a_file, "config"),
    (rb_root_files_other_version, "config"),
]


@pytest.mark.parametrize(
    ("setup", "reason"), RB_REFUSALS, ids=[s.__name__ for s, _ in RB_REFUSALS]
)
def test_rollback_refuses_and_changes_nothing(world: World, setup, reason: str) -> None:  # type: ignore[no-untyped-def]
    setup(world)
    before = snapshot(world)
    report = rollback(world)
    assert (report.status, report.reason) == ("refused", reason)
    unchanged(world, before, report)


def test_rollback_does_not_read_the_pins(world: World) -> None:
    rolled(world)
    (world.hub / "config" / "pins.toml").write_text("[codex\n", encoding="utf-8")
    assert rollback(world).status == "rolled_back"


def test_rollback_on_an_unsupported_machine_is_an_error(world: World) -> None:
    rolled(world)
    before = snapshot(world)
    report = rollback(world, machine="sparc")
    assert (report.status, report.reason) == ("error", "platform")
    unchanged(world, before, report)


def test_rollback_while_locked_is_busy(world: World) -> None:
    rolled(world)
    before = snapshot(world)
    stream = hold_lock(world)
    try:
        report = rollback(world)
    finally:
        stream.close()
    assert (report.status, report.reason) == ("busy", None)
    assert snapshot(world) == before


def test_rollback_with_an_unreadable_state_is_an_error(world: World) -> None:
    rolled(world)
    world.state_path.write_text("[]", encoding="utf-8")
    before = snapshot(world)
    report = rollback(world)
    assert (report.status, report.reason) == ("error", "state")
    unchanged(world, before, report)


def test_rollback_files_that_cannot_move_are_an_error(world: World) -> None:
    rolled(world)
    before = snapshot(world)
    world.layout.root.chmod(0o555)
    try:
        report = rollback(world)
    finally:
        world.layout.root.chmod(0o755)
    assert (report.status, report.reason) == ("error", "config_write")
    unchanged(world, before, report)


def test_a_rollback_link_that_cannot_be_written_restores_the_files(
    world: World,
) -> None:
    rolled(world)
    before = snapshot(world)
    world.layout.link.parent.chmod(0o555)
    try:
        report = rollback(world)
    finally:
        world.layout.link.parent.chmod(0o755)
    assert (report.status, report.reason) == ("error", "link_write")
    unchanged(world, before, report)


def test_a_failed_rollback_state_write_restores_the_link_and_files(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    rolled(world)
    before = snapshot(world)

    def fail(path: Path, state: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(selfcheck_codex, "save_agents_state", fail)
    report = rollback(world)
    assert (report.status, report.reason) == ("error", "state_write")
    unchanged(world, before, report)
