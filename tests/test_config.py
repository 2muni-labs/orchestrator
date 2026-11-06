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
    add_target,
    config_path,
    ensure_target,
    home_relative,
    load_config,
    load_targets,
    notify_state_path,
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


def test_notify_state_sits_directly_in_orch_home() -> None:
    assert notify_state_path(Path("/x/.orch")) == Path("/x/.orch/notify.json")


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


def test_safety_paths_are_loaded(tmp_path: Path) -> None:
    text = (
        VALID_TARGET + 'safety_paths = ["CLAUDE.md", "scripts/**", ".claude/agents/"]\n'
    )
    target = load_targets(write(tmp_path / "targets.toml", text))["sample-app"]
    assert target.safety_paths == ("CLAUDE.md", "scripts/**", ".claude/agents/")
    assert target.shared_paths == ("package-lock.json", "migrations/", ".claude/**")


def test_safety_paths_is_optional_and_empty_by_default(tmp_path: Path) -> None:
    targets = load_targets(write(tmp_path / "targets.toml", VALID_TARGET))
    assert targets["sample-app"].safety_paths == ()
    assert [f.name for f in dataclasses.fields(Target)][-2:] == [
        "shared_paths",
        "safety_paths",
    ]


@pytest.mark.parametrize(
    "value",
    [
        '"CLAUDE.md"',
        "[1]",
        '["../x"]',
        '["/abs"]',
        '["*.md"]',
        '["src/*/x.py"]',
        '["src/?.py"]',
        '[""]',
    ],
)
def test_invalid_safety_paths_raise(tmp_path: Path, value: str) -> None:
    text = VALID_TARGET + f"safety_paths = {value}\n"
    with pytest.raises(ConfigError, match="safety"):
        load_targets(write(tmp_path / "targets.toml", text))


def test_an_empty_safety_paths_list_is_allowed(tmp_path: Path) -> None:
    text = VALID_TARGET + "safety_paths = []\n"
    targets = load_targets(write(tmp_path / "targets.toml", text))
    assert targets["sample-app"].safety_paths == ()


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


# Review and triage paths


def test_review_and_triage_paths(tmp_path: Path) -> None:
    paths = target_paths(tmp_path / "missing", "hub")
    reviews = tmp_path / "missing" / "hub" / "reviews"
    assert paths.review_path("feat") == reviews / "feat.json"
    assert paths.triage_path("feat") == reviews / "feat.triage.md"
    assert paths.review_path("feat", 1) == reviews / "attempts" / "feat.1.json"
    assert paths.triage_path("feat", 2) == reviews / "attempts" / "feat.2.triage.md"
    assert not (tmp_path / "missing").exists()


def test_an_archived_review_cannot_collide_with_another_tasks_current_one(
    tmp_path: Path,
) -> None:
    """An id may hold a dot, so a.1 and attempt 1 of a must stay apart."""
    paths = target_paths(tmp_path, "hub")
    assert paths.review_path("a", 1) != paths.review_path("a.1")
    assert paths.triage_path("a", 1) != paths.triage_path("a.1")
    assert paths.review_path("a.1", 1) != paths.review_path("a", 1)


@pytest.mark.parametrize("task_id", ["", "../x", "a/b", ".hidden", "a..b", "a."])
@pytest.mark.parametrize("method", ["review_path", "triage_path"])
def test_review_paths_refuse_an_invalid_task_id(
    tmp_path: Path, method: str, task_id: str
) -> None:
    paths = target_paths(tmp_path, "hub")
    with pytest.raises(ConfigError):
        getattr(paths, method)(task_id)


@pytest.mark.parametrize("attempt", [0, -1, True, "1", 1.0])
@pytest.mark.parametrize("method", ["review_path", "triage_path"])
def test_review_paths_refuse_an_invalid_attempt(
    tmp_path: Path, method: str, attempt: object
) -> None:
    paths = target_paths(tmp_path, "hub")
    with pytest.raises(ConfigError):
        getattr(paths, method)("feat", attempt)


# home_relative


def test_home_relative_returns_the_tilde_form(tmp_path: Path) -> None:
    directory = tmp_path / "home" / "work" / "app"
    directory.mkdir(parents=True)
    assert home_relative(directory, tmp_path / "home") == "~/work/app"


def test_home_relative_normalizes_parent_segments(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "work" / "app").mkdir(parents=True)
    (home / "other").mkdir()
    path = home / "other" / ".." / "work" / "app"
    assert home_relative(path, home) == "~/work/app"


def test_home_relative_refuses_a_parent_segment_that_leaves_home(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "outside").mkdir()
    with pytest.raises(ConfigError):
        home_relative(home / ".." / "outside", home)


def test_home_relative_refuses_a_path_outside_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ConfigError):
        home_relative(outside, home)


def test_home_relative_refuses_a_sibling_with_the_same_prefix(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    sibling = tmp_path / "home-other"
    sibling.mkdir()
    with pytest.raises(ConfigError):
        home_relative(sibling, home)


def test_home_relative_refuses_home_itself(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    with pytest.raises(ConfigError):
        home_relative(home, home)


def test_home_relative_refuses_a_symlink_that_leaves_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "link").symlink_to(outside)
    with pytest.raises(ConfigError):
        home_relative(home / "link", home)


def test_home_relative_stores_the_real_location_of_a_symlink(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "real" / "app").mkdir(parents=True)
    outside = tmp_path / "outside-link"
    outside.symlink_to(home / "real" / "app")
    assert home_relative(outside, home) == "~/real/app"


def test_home_relative_follows_a_symlinked_home(tmp_path: Path) -> None:
    real = tmp_path / "real-home"
    (real / "app").mkdir(parents=True)
    linked = tmp_path / "linked-home"
    linked.symlink_to(real)
    assert home_relative(real / "app", linked) == "~/app"
    assert home_relative(linked / "app", real) == "~/app"


def test_home_relative_refuses_a_missing_directory(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    with pytest.raises(ConfigError):
        home_relative(home / "missing", home)


def test_home_relative_refuses_a_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "file").write_text("x", encoding="utf-8")
    with pytest.raises(ConfigError):
        home_relative(home / "file", home)


# add_target


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    home = tmp_path / "home"
    (home / "work" / "app").mkdir(parents=True)
    hub = tmp_path / "hub"
    hub.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return {
        "targets_file": hub / "targets.toml",
        "home": home,
        "orch_home": tmp_path / "runtime",
    }


def add(workspace: dict[str, object], name: str = "app", **overrides: object) -> Target:
    values: dict[str, object] = {
        "path": workspace["home"] / "work" / "app",  # type: ignore[operator]
        "github": "owner/app",
        "default_branch": "main",
        "setup_command": "uv sync --frozen",
        "test_command": ".venv/bin/python -m pytest",
        "lint_command": ".venv/bin/python -m ruff check .",
        "home": workspace["home"],
        "orch_home": workspace["orch_home"],
    }
    values.update(overrides)
    return add_target(workspace["targets_file"], name, **values)  # type: ignore[arg-type]


def test_add_target_stores_only_the_tilde_form(workspace: dict[str, object]) -> None:
    add(workspace)
    text = workspace["targets_file"].read_text(encoding="utf-8")  # type: ignore[attr-defined]
    entry = tomllib.loads(text)["targets"]["app"]
    assert entry["path"] == "~/work/app"
    assert str(workspace["home"]) not in text


def test_add_target_round_trips_through_load_targets(
    workspace: dict[str, object],
) -> None:
    returned = add(workspace, shared_paths=("uv.lock", "migrations/"))
    loaded = load_targets(workspace["targets_file"])  # type: ignore[arg-type]
    assert loaded == {"app": returned}
    assert returned == Target(
        name="app",
        path=workspace["home"] / "work" / "app",  # type: ignore[operator]
        github="owner/app",
        default_branch="main",
        setup_command="uv sync --frozen",
        test_command=".venv/bin/python -m pytest",
        lint_command=".venv/bin/python -m ruff check .",
        shared_paths=("uv.lock", "migrations/"),
    )


def test_add_target_creates_the_runtime_directories(
    workspace: dict[str, object],
) -> None:
    add(workspace)
    root = workspace["orch_home"] / "app"  # type: ignore[operator]
    assert SUBDIRS <= {entry.name for entry in root.iterdir()}
    assert marker_github(workspace["orch_home"], "app") == "owner/app"  # type: ignore[arg-type]


def test_add_target_escapes_values(workspace: dict[str, object]) -> None:
    home = workspace["home"]
    (home / "작업 공간").mkdir()  # type: ignore[operator]
    command = 'sh -c "echo \\"a\\" \\\\ b"\ttail'
    returned = add(workspace, path=home / "작업 공간", setup_command=command)  # type: ignore[operator]
    loaded = load_targets(workspace["targets_file"])  # type: ignore[arg-type]
    assert loaded["app"] == returned
    assert loaded["app"].setup_command == command
    text = workspace["targets_file"].read_text(encoding="utf-8")  # type: ignore[attr-defined]
    assert tomllib.loads(text)["targets"]["app"]["path"] == "~/작업 공간"


def test_add_target_keeps_existing_text_and_entries(
    workspace: dict[str, object],
) -> None:
    targets_file = workspace["targets_file"]
    original = "# Registered targets\n" + VALID_TARGET.rstrip("\n")
    targets_file.write_text(original, encoding="utf-8")  # type: ignore[attr-defined]
    add(workspace)
    text = targets_file.read_text(encoding="utf-8")  # type: ignore[attr-defined]
    assert text.startswith(original)
    assert set(load_targets(targets_file)) == {"sample-app", "app"}  # type: ignore[arg-type]


def test_add_target_refuses_a_duplicate_and_changes_nothing(
    workspace: dict[str, object],
) -> None:
    add(workspace)
    before = workspace["targets_file"].read_bytes()  # type: ignore[attr-defined]
    with pytest.raises(TargetNameError):
        add(workspace)
    with pytest.raises(TargetNameError):
        add(workspace, "APP")
    assert workspace["targets_file"].read_bytes() == before  # type: ignore[attr-defined]


def test_add_target_refuses_a_path_outside_home(
    workspace: dict[str, object], tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ConfigError):
        add(workspace, path=outside)
    assert not workspace["targets_file"].exists()  # type: ignore[attr-defined]
    assert not (workspace["orch_home"] / "app").exists()  # type: ignore[operator]


@pytest.mark.parametrize(
    "field",
    ["setup_command", "test_command", "lint_command", "default_branch"],
)
@pytest.mark.parametrize(
    "value",
    [
        "/Users/someone/bin/run",
        "/home/someone/bin/run",
        "/private/tmp/run",
        "/var/folders/ab/run",
        "ghp_" + "a" * 40,
    ],
)
def test_add_target_refuses_a_personal_path_or_secret_in_a_field(
    workspace: dict[str, object], field: str, value: str
) -> None:
    with pytest.raises(ConfigError):
        add(workspace, **{field: value})
    assert not workspace["targets_file"].exists()  # type: ignore[attr-defined]


@pytest.mark.parametrize("base", ["home", "orch_home"])
def test_add_target_refuses_the_known_paths_in_a_field(
    workspace: dict[str, object], base: str
) -> None:
    """redact would replace these, and the replaced text must not be stored."""
    with pytest.raises(ConfigError):
        add(workspace, setup_command=f"{workspace[base]}/bin/setup")
    assert not workspace["targets_file"].exists()  # type: ignore[attr-defined]


def test_add_target_refuses_a_personal_path_in_shared_paths(
    workspace: dict[str, object],
) -> None:
    with pytest.raises(ConfigError):
        add(workspace, shared_paths=("docs/home/page.md",))
    assert not workspace["targets_file"].exists()  # type: ignore[attr-defined]


@pytest.mark.parametrize("shared", [("/abs",), ("../up",), ("*.lock",), ("",)])
def test_add_target_refuses_an_invalid_shared_path(
    workspace: dict[str, object], shared: tuple[str, ...]
) -> None:
    with pytest.raises(ConfigError):
        add(workspace, shared_paths=shared)
    assert not workspace["targets_file"].exists()  # type: ignore[attr-defined]
    assert not (workspace["orch_home"] / "app").exists()  # type: ignore[operator]


@pytest.mark.parametrize(
    "overrides",
    [
        {"github": "not a repo"},
        {"default_branch": ""},
        {"setup_command": ""},
        {"test_command": ""},
        {"lint_command": ""},
    ],
)
def test_add_target_refuses_invalid_fields(
    workspace: dict[str, object], overrides: dict[str, object]
) -> None:
    with pytest.raises(ConfigError):
        add(workspace, **overrides)
    assert not workspace["targets_file"].exists()  # type: ignore[attr-defined]


@pytest.mark.parametrize("name", ["bin", "../x", ""])
def test_add_target_refuses_an_invalid_name(
    workspace: dict[str, object], name: str
) -> None:
    with pytest.raises(TargetNameError):
        add(workspace, name)
    assert not workspace["targets_file"].exists()  # type: ignore[attr-defined]


def test_add_target_refuses_a_malformed_file_and_keeps_it(
    workspace: dict[str, object],
) -> None:
    workspace["targets_file"].write_text("not toml [", encoding="utf-8")  # type: ignore[attr-defined]
    with pytest.raises(ConfigError):
        add(workspace)
    assert workspace["targets_file"].read_text(encoding="utf-8") == "not toml ["  # type: ignore[attr-defined]


def test_add_target_refuses_a_runtime_owned_by_another_repository(
    workspace: dict[str, object],
) -> None:
    ensure_target(workspace["orch_home"], "app", "owner/other")  # type: ignore[arg-type]
    with pytest.raises(TargetNameError):
        add(workspace)
    assert not workspace["targets_file"].exists()  # type: ignore[attr-defined]


def test_add_target_keeps_files_in_an_existing_runtime(
    workspace: dict[str, object],
) -> None:
    """The hub's runtime directory exists from the manual phase, without a marker."""
    logs = workspace["orch_home"] / "app" / "logs"  # type: ignore[operator]
    logs.mkdir(parents=True)
    (logs / "old.1.log").write_text("x", encoding="utf-8")
    add(workspace)
    assert (logs / "old.1.log").read_text(encoding="utf-8") == "x"


def test_add_target_leaves_no_temporary_file(workspace: dict[str, object]) -> None:
    add(workspace)
    with pytest.raises(TargetNameError):
        add(workspace)
    hub = workspace["targets_file"].parent  # type: ignore[attr-defined]
    assert [entry.name for entry in hub.iterdir()] == ["targets.toml"]
