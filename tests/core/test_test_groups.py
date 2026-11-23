"""targets.toml test_groups and test_select (m-p5-test-select-core)."""

from pathlib import Path

import pytest

from orch.config import ConfigError, SelectConfig, SelectGroup, load_targets

BASE = """
[targets.sample]
path = "/srv/sample"
github = "owner/sample"
default_branch = "main"
setup_command = "uv sync --frozen"
test_command = ".venv/bin/python -m pytest"
lint_command = "ruff check ."
"""

FULL = (
    BASE
    + """
[targets.sample.test_select]
import_roots = ["src", "tests", "tests/core"]
full_paths = ["pyproject.toml", "tests/*.py", "tests/fake_*/**"]
ignore_paths = ["docs/**", "**/*.md"]

[targets.sample.test_groups.integration]
tests = ["tests/integration/**"]
command = ".venv/bin/python -m pytest tests/integration"

[targets.sample.test_groups.core]
sources = ["src/orch/models.py", "src/orch/?tore.py"]
tests = ["tests/core/**"]
command = ".venv/bin/python -m pytest tests/core"
also = ["integration"]

[targets.sample.test_groups.ops_2]
sources = ["scripts/usage_baseline.py"]
tests = ["tests/ops/**", "tests/ops_data/*.json"]
command = ".venv/bin/python -m pytest tests/ops"
also = ["integration", "core"]
"""
)


def load(tmp_path: Path, text: str) -> SelectConfig | None:
    path = tmp_path / "targets.toml"
    path.write_text(text, encoding="utf-8")
    return load_targets(path)["sample"].test_select


def test_a_target_without_groups_has_no_test_select(tmp_path: Path) -> None:
    assert load(tmp_path, BASE) is None


def test_the_full_form_is_loaded_with_groups_by_name(tmp_path: Path) -> None:
    assert load(tmp_path, FULL) == SelectConfig(
        groups=(
            SelectGroup(
                name="core",
                sources=("src/orch/models.py", "src/orch/?tore.py"),
                tests=("tests/core/**",),
                command=".venv/bin/python -m pytest tests/core",
                also=("integration",),
            ),
            SelectGroup(
                name="integration",
                sources=(),
                tests=("tests/integration/**",),
                command=".venv/bin/python -m pytest tests/integration",
                also=(),
            ),
            SelectGroup(
                name="ops_2",
                sources=("scripts/usage_baseline.py",),
                tests=("tests/ops/**", "tests/ops_data/*.json"),
                command=".venv/bin/python -m pytest tests/ops",
                also=("integration", "core"),
            ),
        ),
        import_roots=("src", "tests", "tests/core"),
        full_paths=("pyproject.toml", "tests/*.py", "tests/fake_*/**"),
        ignore_paths=("docs/**", "**/*.md"),
    )


def test_groups_without_test_select_use_empty_lists(tmp_path: Path) -> None:
    text = (
        BASE
        + """
[targets.sample.test_groups.core]
tests = ["tests/core/**"]
command = "pytest tests/core"
"""
    )
    assert load(tmp_path, text) == SelectConfig(
        groups=(
            SelectGroup(
                name="core", tests=("tests/core/**",), command="pytest tests/core"
            ),
        ),
    )


def test_the_dataclasses_default_to_empty_lists() -> None:
    group = SelectGroup(name="a", tests=("t/**",), command="c")
    assert (group.sources, group.also) == ((), ())
    select = SelectConfig(groups=(group,))
    assert (select.import_roots, select.full_paths, select.ignore_paths) == (
        (),
        (),
        (),
    )


def test_test_select_without_groups_raises(tmp_path: Path) -> None:
    text = BASE + '[targets.sample.test_select]\nimport_roots = ["src"]\n'
    with pytest.raises(ConfigError):
        load(tmp_path, text)


def group_text(body: str, name: str = "core") -> str:
    return BASE + f"[targets.sample.test_groups.{name}]\n" + body


VALID_BODY = 'tests = ["tests/core/**"]\ncommand = "pytest"\n'


@pytest.mark.parametrize("name", ["Core", "1x", "a-b", "_a", '"a b"', '""', '"é"'])
def test_invalid_group_names_raise(tmp_path: Path, name: str) -> None:
    with pytest.raises(ConfigError):
        load(tmp_path, group_text(VALID_BODY, name))


@pytest.mark.parametrize("name", ["a", "core", "review_pr", "a1"])
def test_valid_group_names(tmp_path: Path, name: str) -> None:
    select = load(tmp_path, group_text(VALID_BODY, name))
    assert select is not None and select.groups[0].name == name


@pytest.mark.parametrize(
    "body",
    [
        'command = "pytest"\n',
        'tests = []\ncommand = "pytest"\n',
        'tests = ["tests/core/**"]\n',
        'tests = ["tests/core/**"]\ncommand = ""\n',
        'tests = ["tests/core/**"]\ncommand = "  "\n',
        'tests = ["tests/core/**"]\ncommand = 1\n',
        'tests = ["tests/core/**"]\ncommand = ["pytest"]\n',
        'tests = ["tests/core/**"]\ncommand = "pytest"\nother = 1\n',
        'tests = ["tests/core/**"]\ncommand = "pytest"\nalso = "integration"\n',
        'tests = ["tests/core/**"]\ncommand = "pytest"\nalso = ["absent"]\n',
        'tests = ["tests/core/**"]\ncommand = "pytest"\nalso = ["core"]\n',
        'tests = ["tests/core/**"]\ncommand = "pytest"\nalso = [1]\n',
    ],
)
def test_invalid_group_bodies_raise(tmp_path: Path, body: str) -> None:
    with pytest.raises(ConfigError):
        load(tmp_path, group_text(body))


def test_test_groups_must_be_a_table_of_tables(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load(
            tmp_path,
            BASE.replace(
                'lint_command = "ruff check ."',
                'lint_command = "ruff check ."\ntest_groups = 1',
            ),
        )
    with pytest.raises(ConfigError):
        load(tmp_path, BASE + "[targets.sample.test_groups]\ncore = 1\n")


def test_unknown_test_select_keys_raise(tmp_path: Path) -> None:
    text = group_text(VALID_BODY) + "[targets.sample.test_select]\nroots = []\n"
    with pytest.raises(ConfigError):
        load(tmp_path, text)


BAD_PATHS = [
    '"src"',
    "[1]",
    '[""]',
    '["/abs"]',
    '["../x"]',
    '["a/../b"]',
    '["a\\\\b"]',
    '["a\\nb"]',
]


@pytest.mark.parametrize("value", BAD_PATHS)
@pytest.mark.parametrize("key", ["import_roots", "full_paths", "ignore_paths"])
def test_invalid_test_select_paths_raise(tmp_path: Path, key: str, value: str) -> None:
    text = group_text(VALID_BODY) + f"[targets.sample.test_select]\n{key} = {value}\n"
    with pytest.raises(ConfigError):
        load(tmp_path, text)


@pytest.mark.parametrize("value", BAD_PATHS)
@pytest.mark.parametrize("key", ["sources", "tests"])
def test_invalid_group_paths_raise(tmp_path: Path, key: str, value: str) -> None:
    body = {
        "sources": f'sources = {value}\ntests = ["t/**"]\ncommand = "c"\n',
        "tests": f'tests = {value}\ncommand = "c"\n',
    }[key]
    with pytest.raises(ConfigError):
        load(tmp_path, group_text(body))


@pytest.mark.parametrize("value", ['["src/*"]', '["s?c"]', '["src/**"]'])
def test_import_roots_take_no_glob(tmp_path: Path, value: str) -> None:
    text = (
        group_text(VALID_BODY)
        + f"[targets.sample.test_select]\nimport_roots = {value}\n"
    )
    with pytest.raises(ConfigError):
        load(tmp_path, text)


def test_globs_are_allowed_in_the_other_lists(tmp_path: Path) -> None:
    text = group_text(
        'sources = ["src/?.py", "**/x.py"]\ntests = ["tests/*.py"]\ncommand = "c"\n'
    ) + (
        "[targets.sample.test_select]\n"
        'full_paths = ["tests/*.py", "**/conftest.py"]\n'
        'ignore_paths = ["*.md", "docs/**"]\n'
    )
    select = load(tmp_path, text)
    assert select is not None
    assert select.groups[0].sources == ("src/?.py", "**/x.py")
    assert select.full_paths == ("tests/*.py", "**/conftest.py")
    assert select.ignore_paths == ("*.md", "docs/**")
