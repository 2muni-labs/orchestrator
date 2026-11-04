import dataclasses
import re
import threading
import tomllib
from pathlib import Path

import pytest

from orch.config import (
    MARKER_FILE,
    RESERVED_TARGET_NAMES,
    Config,
    ConfigError,
    Limits,
    Target,
    TargetNameError,
    config_path,
    ensure_target,
    load_config,
    load_targets,
    orch_home,
    state_db_path,
    target_paths,
    validate_target_name,
)

GITHUB = "owner/repo"
THREADS = 8
SUBDIRS = {"tasks", "results", "reviews", "logs", "worktrees"}

VALID_TARGET = """
[targets.sample-app]
path = "/srv/sample-app"
github = "owner/sample-app"
default_branch = "main"
setup_command = "npm ci"
test_command = "npm test"
lint_command = "npm run lint"
shared_paths = ["package-lock.json", "migrations/", ".claude/**"]
"""


# orch_home


def test_orch_home_defaults_to_dot_orch_in_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", "/fake/home")
    assert orch_home({}) == Path("/fake/home/.orch")
    assert orch_home({"ORCH_HOME": ""}) == Path("/fake/home/.orch")


def test_orch_home_uses_the_variable_and_expands_tilde(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", "/fake/home")
    assert orch_home({"ORCH_HOME": "/opt/orch"}) == Path("/opt/orch")
    assert orch_home({"ORCH_HOME": "~/other"}) == Path("/fake/home/other")


def test_orch_home_reads_the_process_environment_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ORCH_HOME", "/opt/from-env")
    assert orch_home() == Path("/opt/from-env")


def test_orch_home_must_be_absolute() -> None:
    with pytest.raises(ConfigError):
        orch_home({"ORCH_HOME": "relative/path"})


def test_state_db_and_config_sit_directly_in_orch_home() -> None:
    home = Path("/opt/orch")
    assert state_db_path(home) == home / "state.db"
    assert config_path(home) == home / "config"


# Target names


@pytest.mark.parametrize(
    "name", ["orchestrator", "sample-app", "a", "A1", "hub.v2", "my_app"]
)
def test_valid_target_names(name: str) -> None:
    validate_target_name(name)


def test_reserved_names_match_the_design() -> None:
    assert RESERVED_TARGET_NAMES == {"bin", "secrets", "config", "state.db"}


@pytest.mark.parametrize(
    "name",
    ["bin", "secrets", "config", "state.db", "BIN", "Secrets", "CONFIG", "State.DB"],
)
def test_reserved_names_raise_in_any_case(name: str) -> None:
    with pytest.raises(TargetNameError):
        validate_target_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        ".",
        "..",
        ".hidden",
        "-flag",
        "a/b",
        "../x",
        "/abs",
        "a\\b",
        "a b",
        "a\nb",
        "a\0b",
        "이름",
        "a/../b",
    ],
)
def test_names_that_escape_or_are_unsafe_raise(name: str) -> None:
    with pytest.raises(TargetNameError):
        validate_target_name(name)


def test_target_name_error_is_a_config_error() -> None:
    assert issubclass(TargetNameError, ConfigError)


# target_paths


def test_target_paths_layout_without_touching_the_filesystem(tmp_path: Path) -> None:
    home = tmp_path / "missing"
    paths = target_paths(home, "hub")
    root = home / "hub"
    assert paths.root == root
    assert paths.tasks == root / "tasks"
    assert paths.results == root / "results"
    assert paths.reviews == root / "reviews"
    assert paths.logs == root / "logs"
    assert paths.worktrees == root / "worktrees"
    assert paths.todo == root / "TODO.md"
    assert not home.exists()


@pytest.mark.parametrize("name", ["bin", "../x"])
def test_target_paths_validates_the_name(tmp_path: Path, name: str) -> None:
    with pytest.raises(TargetNameError):
        target_paths(tmp_path, name)


# ensure_target


def marker_github(home: Path, name: str) -> str:
    return tomllib.loads((home / name / MARKER_FILE).read_text(encoding="utf-8"))[
        "github"
    ]


def test_ensure_target_creates_everything_including_missing_home(
    tmp_path: Path,
) -> None:
    home = tmp_path / "a" / "orch"
    paths = ensure_target(home, "hub", GITHUB)
    assert paths == target_paths(home, "hub")
    assert {p.name for p in paths.root.iterdir()} == SUBDIRS | {MARKER_FILE}
    assert marker_github(home, "hub") == GITHUB


def test_ensure_target_is_idempotent(tmp_path: Path) -> None:
    first = ensure_target(tmp_path, "hub", GITHUB)
    marker = first.root / MARKER_FILE
    before = marker.read_bytes()
    assert ensure_target(tmp_path, "hub", GITHUB) == first
    assert marker.read_bytes() == before


def test_existing_directory_without_marker_gets_one_and_keeps_its_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "hub"
    (root / "logs").mkdir(parents=True)
    kept = root / "logs" / "old.log"
    kept.write_text("keep", encoding="utf-8")
    ensure_target(tmp_path, "hub", GITHUB)
    assert kept.read_text(encoding="utf-8") == "keep"
    assert marker_github(tmp_path, "hub") == GITHUB


def test_different_github_raises_and_changes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "hub"
    root.mkdir()
    (root / MARKER_FILE).write_text('github = "owner/first"\n', encoding="utf-8")
    with pytest.raises(TargetNameError):
        ensure_target(tmp_path, "hub", "owner/second")
    assert marker_github(tmp_path, "hub") == "owner/first"
    assert {p.name for p in root.iterdir()} == {MARKER_FILE}


def test_github_comparison_ignores_case(tmp_path: Path) -> None:
    ensure_target(tmp_path, "hub", "Owner/Repo")
    ensure_target(tmp_path, "hub", "owner/repo")
    assert marker_github(tmp_path, "hub") == "Owner/Repo"


@pytest.mark.parametrize("content", ["not toml [", "other = 1\n", "github = 1\n"])
def test_unreadable_marker_raises(tmp_path: Path, content: str) -> None:
    root = tmp_path / "hub"
    root.mkdir()
    (root / MARKER_FILE).write_text(content, encoding="utf-8")
    with pytest.raises(TargetNameError):
        ensure_target(tmp_path, "hub", GITHUB)


@pytest.mark.parametrize("existing", ["Hub", "HUB"])
def test_names_differing_only_by_case_raise(tmp_path: Path, existing: str) -> None:
    (tmp_path / existing).mkdir()
    with pytest.raises(TargetNameError):
        ensure_target(tmp_path, "hub", GITHUB)


def test_case_collision_is_found_from_a_real_registration(tmp_path: Path) -> None:
    ensure_target(tmp_path, "Hub", GITHUB)
    with pytest.raises(TargetNameError):
        ensure_target(tmp_path, "hub", GITHUB)


def test_a_different_name_does_not_collide(tmp_path: Path) -> None:
    ensure_target(tmp_path, "hub", GITHUB)
    ensure_target(tmp_path, "hub2", "owner/other")


@pytest.mark.parametrize("name", ["bin", "SECRETS", "state.db", "../x", "a/b", ""])
def test_ensure_target_rejects_bad_names_and_creates_nothing(
    tmp_path: Path, name: str
) -> None:
    home = tmp_path / "orch"
    with pytest.raises(TargetNameError):
        ensure_target(home, name, GITHUB)
    assert not home.exists()


def test_target_that_is_a_symlink_raises(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    home = tmp_path / "orch"
    home.mkdir()
    (home / "hub").symlink_to(outside, target_is_directory=True)
    with pytest.raises(TargetNameError):
        ensure_target(home, "hub", GITHUB)
    assert list(outside.iterdir()) == []


def test_target_that_is_a_file_raises(tmp_path: Path) -> None:
    (tmp_path / "hub").write_text("file", encoding="utf-8")
    with pytest.raises(TargetNameError):
        ensure_target(tmp_path, "hub", GITHUB)


@pytest.mark.parametrize("github", ["", "repo", "a/b/c", "a b/c", "/repo", "owner/"])
def test_malformed_github_raises(tmp_path: Path, github: str) -> None:
    with pytest.raises(ConfigError):
        ensure_target(tmp_path, "hub", github)
    assert not (tmp_path / "hub").exists()


def test_concurrent_first_use_has_exactly_one_winner(tmp_path: Path) -> None:
    barrier = threading.Barrier(THREADS)
    winners: list[str] = []
    errors: list[BaseException] = []

    def attempt(number: int) -> None:
        github = f"owner/repo{number}"
        barrier.wait()
        try:
            ensure_target(tmp_path, "hub", github)
            winners.append(github)
        except TargetNameError:
            pass
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=attempt, args=(n,)) for n in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(winners) == 1
    assert marker_github(tmp_path, "hub") == winners[0]
    leftovers = {p.name for p in (tmp_path / "hub").iterdir()} - SUBDIRS - {MARKER_FILE}
    assert leftovers == set()


# targets.toml


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_load_targets(tmp_path: Path) -> None:
    targets = load_targets(write(tmp_path / "targets.toml", VALID_TARGET))
    assert targets == {
        "sample-app": Target(
            name="sample-app",
            path=Path("/srv/sample-app"),
            github="owner/sample-app",
            default_branch="main",
            setup_command="npm ci",
            test_command="npm test",
            lint_command="npm run lint",
            shared_paths=("package-lock.json", "migrations/", ".claude/**"),
        )
    }


def test_load_targets_missing_file_means_no_targets(tmp_path: Path) -> None:
    assert load_targets(tmp_path / "missing.toml") == {}


def test_load_targets_empty_file_means_no_targets(tmp_path: Path) -> None:
    assert load_targets(write(tmp_path / "targets.toml", "")) == {}


def test_load_targets_expands_tilde(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", "/fake/home")
    text = VALID_TARGET.replace("/srv/sample-app", "~/work/app")
    targets = load_targets(write(tmp_path / "targets.toml", text))
    assert targets["sample-app"].path == Path("/fake/home/work/app")


def test_shared_paths_is_optional(tmp_path: Path) -> None:
    text = "\n".join(
        line
        for line in VALID_TARGET.splitlines()
        if not line.startswith("shared_paths")
    )
    targets = load_targets(write(tmp_path / "targets.toml", text))
    assert targets["sample-app"].shared_paths == ()


@pytest.mark.parametrize(
    "key",
    [
        "path",
        "github",
        "default_branch",
        "setup_command",
        "test_command",
        "lint_command",
    ],
)
def test_missing_required_target_key_raises(tmp_path: Path, key: str) -> None:
    text = "\n".join(
        line for line in VALID_TARGET.splitlines() if not line.startswith(key)
    )
    with pytest.raises(ConfigError):
        load_targets(write(tmp_path / "targets.toml", text))


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('path = "/srv/sample-app"', 'path = "relative"'),
        ('path = "/srv/sample-app"', "path = 1"),
        ('github = "owner/sample-app"', 'github = "nope"'),
        ('default_branch = "main"', 'default_branch = ""'),
        ('test_command = "npm test"', 'test_command = ""'),
        ('test_command = "npm test"', "test_command = 1"),
        ('"package-lock.json"', "1"),
        ('"package-lock.json"', '"../x"'),
        ('"package-lock.json"', '"/abs"'),
        ('"package-lock.json"', '"*.json"'),
        ('"package-lock.json"', '"a/*/b"'),
        ('"package-lock.json"', '""'),
        ('default_branch = "main"', 'default_branch = "main"\nunknown = 1'),
    ],
)
def test_invalid_target_values_raise(tmp_path: Path, old: str, new: str) -> None:
    assert old in VALID_TARGET
    with pytest.raises(ConfigError):
        load_targets(write(tmp_path / "targets.toml", VALID_TARGET.replace(old, new)))


@pytest.mark.parametrize(
    "text", ["not toml [", "other = 1\n", "targets = 1\n", "[targets]\nhub = 1\n"]
)
def test_malformed_targets_file_raises(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigError):
        load_targets(write(tmp_path / "targets.toml", text))


@pytest.mark.parametrize("name", ["bin", '"state.db"', "Config"])
def test_reserved_target_name_in_file_raises(tmp_path: Path, name: str) -> None:
    text = VALID_TARGET.replace("[targets.sample-app]", f"[targets.{name}]")
    with pytest.raises(TargetNameError):
        load_targets(write(tmp_path / "targets.toml", text))


def test_targets_differing_only_by_case_raise(tmp_path: Path) -> None:
    second = VALID_TARGET.replace("[targets.sample-app]", "[targets.Sample-App]")
    with pytest.raises(TargetNameError):
        load_targets(write(tmp_path / "targets.toml", VALID_TARGET + second))


# ~/.orch/config


def test_missing_config_means_design_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path)
    assert config.terminal_backend == "tmux"
    assert config.retention_days == 30
    assert config.reservation_ttl_min == 10
    assert config.limits == Limits(total=3, lane_b=2, lane_a=2)
    assert config.notify_method == "none"
    assert config.notify_topic is None
    assert config.codex_path is None
    assert config.claude_path is None


def test_default_usage_limit_patterns_cover_both_providers(tmp_path: Path) -> None:
    patterns = load_config(tmp_path).usage_limit_patterns
    assert set(patterns) == {"codex", "claude"}
    for provider in patterns:
        assert isinstance(patterns[provider], tuple)
        assert patterns[provider]
        for pattern in patterns[provider]:
            re.compile(pattern)
        assert any(
            re.search(p, "You hit your usage limit", re.I) for p in patterns[provider]
        )


def test_full_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/fake/home")
    write(
        config_path(tmp_path),
        """
terminal_backend = "tmux"
retention_days = 7
reservation_ttl_min = 5

[limits]
total = 4
lane_b = 1
lane_a = 3

[notify]
method = "ntfy"
topic = "hard-to-guess"

[agents]
codex = "~/bin/codex"
claude = "/opt/claude"

[usage_limit_patterns]
codex = ["limit one", "limit two"]
""",
    )
    config = load_config(tmp_path)
    assert config.retention_days == 7
    assert config.reservation_ttl_min == 5
    assert config.limits == Limits(total=4, lane_b=1, lane_a=3)
    assert config.notify_method == "ntfy"
    assert config.notify_topic == "hard-to-guess"
    assert config.codex_path == Path("/fake/home/bin/codex")
    assert config.claude_path == Path("/opt/claude")
    assert config.usage_limit_patterns["codex"] == ("limit one", "limit two")
    assert (
        config.usage_limit_patterns["claude"]
        == load_config(tmp_path / "none").usage_limit_patterns["claude"]
    )


def test_partial_limits_keep_other_defaults(tmp_path: Path) -> None:
    write(config_path(tmp_path), "[limits]\nlane_b = 1\n")
    assert load_config(tmp_path).limits == Limits(total=3, lane_b=1, lane_a=2)


def test_notify_topic_stays_out_of_repr(tmp_path: Path) -> None:
    write(config_path(tmp_path), '[notify]\nmethod = "ntfy"\ntopic = "hard-to-guess"\n')
    assert "hard-to-guess" not in repr(load_config(tmp_path))


def test_config_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        Config().retention_days = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    "text",
    [
        "not toml [",
        "unknown = 1\n",
        'terminal_backend = "tern"\n',
        "terminal_backend = 1\n",
        "retention_days = 0\n",
        "retention_days = -1\n",
        "retention_days = true\n",
        'retention_days = "30"\n',
        "reservation_ttl_min = 0\n",
        "limits = 1\n",
        "[limits]\nunknown = 1\n",
        "[limits]\ntotal = 0\n",
        "[limits]\ntotal = true\n",
        "[limits]\ntotal = 1.5\n",
        "[limits]\ntotal = 1\nlane_b = 2\n",
        "[limits]\nlane_a = 0\n",
        '[notify]\nmethod = "mail"\n',
        '[notify]\nmethod = "ntfy"\n',
        '[notify]\nmethod = "ntfy"\ntopic = ""\n',
        '[notify]\nmethod = "none"\ntopic = "x"\n',
        "[notify]\nunknown = 1\n",
        '[agents]\ncodex = "relative/codex"\n',
        "[agents]\ncodex = 1\n",
        '[agents]\ngemini = "/opt/g"\n',
        '[usage_limit_patterns]\ngemini = ["x"]\n',
        '[usage_limit_patterns]\ncodex = "x"\n',
        "[usage_limit_patterns]\ncodex = [1]\n",
        "[usage_limit_patterns]\ncodex = []\n",
        '[usage_limit_patterns]\ncodex = ["("]\n',
    ],
)
def test_invalid_config_raises(tmp_path: Path, text: str) -> None:
    write(config_path(tmp_path), text)
    with pytest.raises(ConfigError):
        load_config(tmp_path)
