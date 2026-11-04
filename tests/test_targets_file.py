"""The hub is public, so its targets.toml must not reveal a personal home path."""

import tomllib
from pathlib import Path

import pytest

from orch.redact import ResidualPathError, redact

TARGETS_FILE = Path(__file__).resolve().parents[1] / "targets.toml"

# Values that cannot appear in a real file, so redact replaces nothing and only its
# residual path check applies. A real home would be replaced, hiding the leak.
_NO_HOME = Path("/nonexistent-orch-test-home")
_NO_ORCH_HOME = Path("/nonexistent-orch-test-runtime")


def check_targets_text(text: str, home: Path) -> None:
    """Raise AssertionError or ResidualPathError if text reveals a personal path."""
    data = tomllib.loads(text)
    for name, entry in data.get("targets", {}).items():
        path = entry["path"]
        assert path.startswith("~/"), f"{name}: path must start with ~/"
        assert ".." not in path.split("/"), f"{name}: path must stay under the home"
    assert str(home) not in text, "text contains the home path"
    encoded = str(home).replace("/", "-")
    assert encoded not in text, "text contains the encoded home path"
    redact(text, home=_NO_HOME, orch_home=_NO_ORCH_HOME)


def _entry(path: str, setup_command: str = "uv sync --frozen") -> str:
    return (
        "[targets.sample]\n"
        f'path = "{path}"\n'
        'github = "owner/sample"\n'
        'default_branch = "main"\n'
        f'setup_command = "{setup_command}"\n'
        'test_command = ".venv/bin/python -m pytest"\n'
        'lint_command = ".venv/bin/python -m ruff check ."\n'
    )


def test_repository_targets_file_has_no_personal_home_path() -> None:
    """A missing file passes: the hub registers no target until orch target add runs."""
    if TARGETS_FILE.exists():
        check_targets_text(TARGETS_FILE.read_text(encoding="utf-8"), Path.home())


def test_home_relative_path_passes() -> None:
    check_targets_text(_entry("~/work/sample"), Path("/Users/someone"))


def test_file_without_targets_passes() -> None:
    check_targets_text("", Path("/Users/someone"))


@pytest.mark.parametrize(
    "path",
    [
        "/Users/someone/work/sample",
        "/home/someone/work/sample",
        "/opt/work/sample",
        "work/sample",
        "~someone/work/sample",
        "~/../other/sample",
    ],
)
def test_path_not_under_home_form_fails(path: str) -> None:
    with pytest.raises(AssertionError):
        check_targets_text(_entry(path), Path("/Users/someone"))


@pytest.mark.parametrize(
    "command",
    [
        "/Users/someone/bin/setup",
        "/home/someone/bin/setup",
        "/private/tmp/setup",
        "/var/folders/ab/setup",
    ],
)
def test_personal_path_in_another_field_fails(command: str) -> None:
    with pytest.raises(ResidualPathError):
        check_targets_text(_entry("~/work/sample", command), Path("/nowhere/else"))


def test_home_outside_the_known_patterns_fails() -> None:
    text = _entry("~/work/sample", "/data/someone/bin/setup")
    with pytest.raises(AssertionError):
        check_targets_text(text, Path("/data/someone"))


def test_encoded_home_fails() -> None:
    text = _entry("~/work/sample", "cat .claude/projects/-data-someone-app")
    with pytest.raises(AssertionError):
        check_targets_text(text, Path("/data/someone"))
