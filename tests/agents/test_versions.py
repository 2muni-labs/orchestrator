"""orch versions report (technical design 4.13, D2)."""

import shutil
import subprocess
import tomllib
from collections.abc import Sequence
from pathlib import Path

import pytest

from orch import latest, selfcheck, versions
from orch.latest import AgentLatest, LatestRecord
from orch.versions import VersionsError

HUB_SOURCE = Path(__file__).resolve().parents[2]
PINS = tomllib.loads((HUB_SOURCE / "config" / "pins.toml").read_text("utf-8"))
CLAUDE = Path("/opt/agents/claude")
CODEX = Path("/opt/agents/codex")
TMUX = Path("/opt/homebrew/bin/tmux")


def done(stdout: str, code: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, stdout, "")


class FakeRun:
    """Answers argv by its first two words and records every call."""

    def __init__(self, answers: dict[tuple[str, ...], object]) -> None:
        self.answers = answers
        self.calls: list[tuple[list[str], float]] = []

    def __call__(
        self, argv: Sequence[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), timeout))
        key = tuple(argv[:2])
        answer = self.answers.get(key, OSError("not found"))
        if isinstance(answer, BaseException):
            raise answer
        assert isinstance(answer, subprocess.CompletedProcess)
        return answer


def standard_answers(hub: Path) -> dict[tuple[str, ...], object]:
    return {
        (str(CLAUDE), "--version"): done("2.1.330 (Claude Code)\n"),
        (str(CODEX), "--version"): done("codex-cli 0.171.0\n"),
        (str(TMUX), "-V"): done("tmux 3.8\n"),
        (versions.SW_VERS, "-productVersion"): done("15.8.3\n"),
        (versions.GIT, "-C"): done("v0.23.0\nv0.24.0\nv0.9.1\n"),
    }


@pytest.fixture
def hub(tmp_path: Path) -> Path:
    path = tmp_path / "hub"
    (path / "config").mkdir(parents=True)
    shutil.copy(HUB_SOURCE / "config" / "pins.toml", path / "config" / "pins.toml")
    return path


@pytest.fixture
def orch_home(tmp_path: Path) -> Path:
    path = tmp_path / "runtime"
    path.mkdir()
    return path


def entry(version: str, source: str = "apply") -> dict[str, object]:
    return {
        "version": version,
        "sha256": "0" * 64,
        "path": f"/opt/versions/{version}",
        "at": "2026-11-20T00:00:00+00:00",
        "source": source,
    }


def candidate(status: str = "ready") -> dict[str, object]:
    return {
        "version": "2.1.331",
        "status": status,
        "at": "2026-11-24T00:00:00+00:00",
        "pins_sha256": "1" * 64,
        "sha256": "2" * 64,
        "path": "/opt/versions/2.1.331",
        "checks": [
            {"id": "FL-M3", "result": "pass", "detail": "ok"},
            {"id": "m14-cli", "result": "fail", "detail": "exit1 blocked"},
        ],
        "warnings": ["FL-M1: check by hand"],
    }


def write_agents(orch_home: Path, agents: dict[str, object]) -> None:
    selfcheck.save_agents_state(
        selfcheck.agents_state_path(orch_home), {"format": 1, "agents": agents}
    )


def write_latest(orch_home: Path, claude: str | None, codex: str | None) -> None:
    latest.write_record(
        latest.latest_path(orch_home),
        LatestRecord(
            "2026-11-25T00:00:00+00:00",
            {
                "claude": AgentLatest(claude, None if claude else "http"),
                "codex": AgentLatest(codex, None if codex else "fetch"),
            },
        ),
    )


# Constants


def test_constants_follow_the_design() -> None:
    assert versions.VERSION_TIMEOUT_SEC == 10
    assert versions.SW_VERS == "/usr/bin/sw_vers"
    assert versions.GIT == "/usr/bin/git"
    assert versions.AGENTS == ("claude", "codex")
    assert versions.TOOLS == ("tmux", "macos")
    assert versions.TAG_PATTERN == r"v([0-9]+\.[0-9]+\.[0-9]+)"


# load_tool_baselines


def test_tool_baselines_come_from_pins(hub: Path) -> None:
    assert versions.load_tool_baselines(hub) == {
        "tmux": PINS["tmux"]["version"],
        "macos": PINS["macos"]["version"],
    }


@pytest.mark.parametrize(
    "edit",
    [
        lambda text: text.replace("[tmux]", "[tmux_old]"),
        lambda text: text.replace(
            f'version = "{PINS["macos"]["version"]}"', "version = 15"
        ),
        lambda text: text.replace(
            f'version = "{PINS["tmux"]["version"]}"', 'version = "three"'
        ),
        lambda text: text + "\n[[[broken",
    ],
    ids=["no-tmux", "not-a-string", "not-a-version", "syntax"],
)
def test_bad_tool_baselines_raise(hub: Path, edit: object) -> None:
    pins = hub / "config" / "pins.toml"
    pins.write_text(edit(pins.read_text("utf-8")), encoding="utf-8")  # type: ignore[operator]
    with pytest.raises(VersionsError):
        versions.load_tool_baselines(hub)


def test_missing_pins_raise(tmp_path: Path) -> None:
    with pytest.raises(VersionsError):
        versions.load_tool_baselines(tmp_path)


# installed_version


@pytest.mark.parametrize(
    ("name", "binary", "argv", "stdout", "expected"),
    [
        (
            "claude",
            CLAUDE,
            [str(CLAUDE), "--version"],
            "2.1.330 (Claude Code)\n",
            "2.1.330",
        ),
        ("codex", CODEX, [str(CODEX), "--version"], "codex-cli 0.171.0\n", "0.171.0"),
        ("tmux", TMUX, [str(TMUX), "-V"], "tmux 3.8\n", "3.8"),
        ("macos", None, ["/usr/bin/sw_vers", "-productVersion"], "15.8.3\n", "15.8.3"),
    ],
)
def test_installed_versions_follow_the_measured_output(
    name: str, binary: Path | None, argv: list[str], stdout: str, expected: str
) -> None:
    run = FakeRun({tuple(argv[:2]): done(stdout)})
    assert versions.installed_version(name, binary, run=run) == expected
    assert run.calls == [(argv, 10)]


@pytest.mark.parametrize("name", ["claude", "codex", "tmux"])
def test_no_configured_binary_is_unknown_without_running(name: str) -> None:
    run = FakeRun({})
    assert versions.installed_version(name, None, run=run) is None
    assert run.calls == []


@pytest.mark.parametrize(
    ("name", "binary", "answer"),
    [
        ("claude", CLAUDE, done("2.1.330 (Claude Code)\n", code=1)),
        ("claude", CLAUDE, done("")),
        ("claude", CLAUDE, done("Claude Code 2.1.330\n")),
        ("claude", CLAUDE, done("2.1.330-beta (Claude Code)\n")),
        ("codex", CODEX, done("0.171.0\n")),
        ("codex", CODEX, done("codex-cli 0.171.0 extra\n")),
        ("codex", CODEX, done("codex 0.171.0\n")),
        ("tmux", TMUX, done("tmux next-3.9\n")),
        ("tmux", TMUX, done("3.8\n")),
        ("macos", None, done("15.8.3 beta\n")),
        ("claude", CLAUDE, OSError("gone")),
        ("codex", CODEX, subprocess.TimeoutExpired("codex", 10)),
        ("macos", None, subprocess.TimeoutExpired("sw_vers", 10)),
    ],
)
def test_failures_and_unexpected_output_are_unknown(
    name: str, binary: Path | None, answer: object
) -> None:
    argv0 = str(binary) if binary is not None else versions.SW_VERS
    flag = {"claude": "--version", "codex": "--version", "tmux": "-V"}.get(
        name, "-productVersion"
    )
    run = FakeRun({(argv0, flag): answer})
    assert versions.installed_version(name, binary, run=run) is None


# orch_latest


def test_orch_latest_is_the_highest_release_tag(hub: Path) -> None:
    run = FakeRun(
        {(versions.GIT, "-C"): done("v0.9.1\nv0.24.0\nv0.10.0\nvx\nv1.0.0-rc1\nv0.3\n")}
    )
    assert versions.orch_latest(hub, run=run) == "0.24.0"
    assert run.calls == [([versions.GIT, "-C", str(hub), "tag", "--list", "v*"], 10)]


@pytest.mark.parametrize(
    "answer",
    [done(""), done("vx\nrelease\n"), done("v0.1.0\n", code=128), OSError("no git")],
    ids=["none", "no-release", "failed", "oserror"],
)
def test_orch_latest_unknown(hub: Path, answer: object) -> None:
    assert (
        versions.orch_latest(hub, run=FakeRun({(versions.GIT, "-C"): answer})) is None
    )


def test_orch_latest_reads_a_real_clone(tmp_path: Path) -> None:
    repo = tmp_path / "clone"
    repo.mkdir()
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            env={**env, "PATH": "/usr/bin:/bin"},
        )

    git("init", "-q")
    git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "base",
    )
    for tag in ("v0.9.0", "v0.10.0", "other"):
        git("tag", tag)
    assert versions.orch_latest(repo) == "0.10.0"


# agent_report


def report(
    *,
    installed: str | None = "2.1.330",
    state: dict[str, object] | None = None,
    record: LatestRecord | None = None,
) -> dict[str, object]:
    return versions.agent_report(
        "claude",
        installed=installed,
        baseline="2.1.319",
        blocking=("FL-M3", "m14-cli"),
        warn=("FL-M1: by hand",),
        state=state,
        record=record,
    )


def record_of(claude: str | None) -> LatestRecord:
    return LatestRecord(
        "2026-11-25T00:00:00+00:00",
        {
            "claude": AgentLatest(claude, None if claude else "format"),
            "codex": AgentLatest(None, "http"),
        },
    )


def test_agent_report_with_everything_known() -> None:
    state = {
        "current": entry("2.1.330"),
        "previous": entry("2.1.329", "bootstrap"),
        "candidate": candidate("ready"),
    }
    assert report(state=state, record=record_of("2.1.331")) == {
        "installed": "2.1.330",
        "baseline": "2.1.319",
        "current": "2.1.330",
        "previous": "2.1.329",
        "latest": "2.1.331",
        "latest_checked_at": "2026-11-25T00:00:00+00:00",
        "behind": True,
        "candidate": {
            "version": "2.1.331",
            "status": "ready",
            "at": "2026-11-24T00:00:00+00:00",
            "checks": [
                {"id": "FL-M3", "result": "pass", "detail": "ok"},
                {"id": "m14-cli", "result": "fail", "detail": "exit1 blocked"},
            ],
            "warnings": ["FL-M1: check by hand"],
        },
        "models": None,
        "remeasure": {"blocking": ["FL-M3", "m14-cli"], "warn": ["FL-M1: by hand"]},
        "commands": {
            "apply": "orch selfcheck --apply claude",
            "rollback": "orch selfcheck --rollback claude",
        },
    }


def test_agent_report_keys_follow_the_schema_order() -> None:
    assert list(report()) == [
        "installed",
        "baseline",
        "current",
        "previous",
        "latest",
        "latest_checked_at",
        "behind",
        "candidate",
        "models",
        "remeasure",
        "commands",
    ]


def test_agent_report_with_nothing_recorded() -> None:
    result = report(installed=None, state=None, record=None)
    assert result["installed"] is None
    assert result["current"] is None
    assert result["previous"] is None
    assert result["latest"] is None
    assert result["latest_checked_at"] is None
    assert result["behind"] is None
    assert result["candidate"] is None
    assert result["commands"] == {"apply": None, "rollback": None}


def test_an_empty_agent_state_is_like_none() -> None:
    assert report(state={}) == report(state=None)


@pytest.mark.parametrize(
    ("current", "newest", "behind"),
    [
        ("2.1.330", "2.1.331", True),
        ("2.1.330", "2.1.330", False),
        ("2.1.330", "2.1.329", False),
        ("2.1.330", "2.2", True),
        (None, "2.1.331", None),
        ("2.1.330", None, None),
    ],
)
def test_behind_compares_current_with_latest(
    current: str | None, newest: str | None, behind: bool | None
) -> None:
    state = {"current": entry(current)} if current else {}
    result = report(state=state, record=record_of(newest))
    assert result["behind"] is behind
    assert result["latest"] == newest
    assert result["latest_checked_at"] == "2026-11-25T00:00:00+00:00"


@pytest.mark.parametrize("status", ["failed", "not_automated"])
def test_apply_is_offered_only_for_a_ready_candidate(status: str) -> None:
    result = report(state={"candidate": candidate(status)})
    assert result["commands"] == {"apply": None, "rollback": None}
    assert result["candidate"]["status"] == status  # type: ignore[index]


def test_the_candidate_never_shows_paths_or_hashes() -> None:
    result = report(state={"candidate": candidate()})
    assert set(result["candidate"]) == {  # type: ignore[arg-type]
        "version",
        "status",
        "at",
        "checks",
        "warnings",
    }


def test_codex_commands_name_codex() -> None:
    result = versions.agent_report(
        "codex",
        installed=None,
        baseline="0.160.0",
        blocking=(),
        warn=(),
        state={"previous": entry("0.160.0"), "candidate": candidate()},
        record=None,
    )
    assert result["commands"] == {
        "apply": "orch selfcheck --apply codex",
        "rollback": "orch selfcheck --rollback codex",
    }
    assert result["remeasure"] == {"blocking": [], "warn": []}


# build_report


def build(orch_home: Path, hub: Path, run: FakeRun, **paths: Path | None) -> dict:
    values: dict[str, Path | None] = {
        "claude_path": CLAUDE,
        "codex_path": CODEX,
        "tmux_path": TMUX,
    }
    values.update(paths)
    return versions.build_report(
        orch_home=orch_home, hub=hub, orch_version="0.24.0", run=run, **values
    )


def test_build_report_combines_every_source(orch_home: Path, hub: Path) -> None:
    write_agents(
        orch_home,
        {
            "claude": {"current": entry("2.1.330"), "candidate": candidate()},
            "codex": {"current": entry("0.171.0"), "previous": entry("0.160.0")},
        },
    )
    write_latest(orch_home, "2.1.331", None)
    result = build(orch_home, hub, FakeRun(standard_answers(hub)))
    assert list(result) == ["agents", "orch", "tools"]
    claude, codex = result["agents"]["claude"], result["agents"]["codex"]
    assert claude["installed"] == "2.1.330"
    assert claude["baseline"] == PINS["claude"]["version"]
    assert claude["behind"] is True
    assert claude["remeasure"] == {
        "blocking": [item["id"] for item in PINS["claude"]["blocking"]],
        "warn": PINS["claude"]["warn"],
    }
    assert claude["commands"]["apply"] == "orch selfcheck --apply claude"
    assert codex["installed"] == "0.171.0"
    assert codex["baseline"] == PINS["codex"]["version"]
    assert (codex["latest"], codex["behind"]) == (None, None)
    assert codex["previous"] == "0.160.0"
    assert codex["remeasure"]["blocking"] == [
        item["id"] for item in PINS["codex"]["blocking"]
    ]
    assert result["orch"] == {"installed": "0.24.0", "latest": "0.24.0"}
    tmux_baseline = PINS["tmux"]["version"]
    macos_baseline = PINS["macos"]["version"]
    assert result["tools"] == {
        "tmux": {
            "installed": "3.8",
            "baseline": tmux_baseline,
            "differs": tmux_baseline != "3.8",
        },
        "macos": {
            "installed": "15.8.3",
            "baseline": macos_baseline,
            "differs": macos_baseline != "15.8.3",
        },
    }


def test_unknown_installed_tools_leave_differs_null(orch_home: Path, hub: Path) -> None:
    result = build(orch_home, hub, FakeRun({}), tmux_path=None)
    assert result["tools"]["tmux"]["installed"] is None
    assert result["tools"]["tmux"]["differs"] is None
    assert result["tools"]["macos"]["differs"] is None
    assert result["orch"]["latest"] is None
    assert result["agents"]["claude"]["installed"] is None


def test_missing_paths_are_not_run(orch_home: Path, hub: Path) -> None:
    run = FakeRun(standard_answers(hub))
    build(orch_home, hub, run, claude_path=None, codex_path=None, tmux_path=None)
    assert sorted(call[0][0] for call in run.calls) == [
        versions.GIT,
        versions.SW_VERS,
    ]


def test_an_unreadable_agents_state_leaves_records_null(
    orch_home: Path, hub: Path
) -> None:
    selfcheck.agents_state_path(orch_home).write_text("{bad", encoding="utf-8")
    result = build(orch_home, hub, FakeRun(standard_answers(hub)))
    for agent in ("claude", "codex"):
        item = result["agents"][agent]
        assert (item["current"], item["previous"], item["candidate"]) == (
            None,
            None,
            None,
        )
        assert item["installed"] is not None


def test_an_unreadable_latest_record_leaves_latest_null(
    orch_home: Path, hub: Path
) -> None:
    latest.latest_path(orch_home).write_text("[]", encoding="utf-8")
    write_agents(orch_home, {"claude": {"current": entry("2.1.330")}})
    result = build(orch_home, hub, FakeRun(standard_answers(hub)))
    claude = result["agents"]["claude"]
    assert (claude["latest"], claude["latest_checked_at"], claude["behind"]) == (
        None,
        None,
        None,
    )


@pytest.mark.parametrize("table", ["claude", "codex", "tmux"])
def test_bad_pins_raise_versions_error(orch_home: Path, hub: Path, table: str) -> None:
    pins = hub / "config" / "pins.toml"
    pins.write_text(
        pins.read_text("utf-8").replace(f"[{table}]", f"[{table}_moved]"),
        encoding="utf-8",
    )
    with pytest.raises(VersionsError):
        build(orch_home, hub, FakeRun(standard_answers(hub)))


def test_build_report_writes_nothing(orch_home: Path, hub: Path) -> None:
    write_agents(orch_home, {"claude": {"current": entry("2.1.330")}})
    write_latest(orch_home, "2.1.330", "0.171.0")
    before = {p: p.read_bytes() for p in orch_home.rglob("*") if p.is_file()}
    build(orch_home, hub, FakeRun(standard_answers(hub)))
    after = {p: p.read_bytes() for p in orch_home.rglob("*") if p.is_file()}
    assert after == before
