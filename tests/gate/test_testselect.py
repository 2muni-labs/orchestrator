"""Choosing test groups from changed paths and Python imports."""

import os
from pathlib import Path

import pytest

from orch.config import SelectConfig, SelectGroup, load_targets
from orch.testselect import Selection, select

FILES = {
    "src/orch/__init__.py": "",
    "src/orch/models.py": "VALUE = 1\n",
    "src/orch/store.py": "from orch.models import VALUE\n",
    "src/orch/gate.py": "from orch import store\n",
    "src/orch/cli.py": "from . import gate\n",
    "src/orch/late.py": "def f() -> None:\n    import orch.cli\n",
    "src/orch/unused.py": "X = 1\n",
    "src/orch/weird.py": "from .... import nothing\n",
    "src/orch/lanes/__init__.py": "",
    "src/orch/lanes/codex.py": "from ..models import VALUE\n",
    "src/orch/__pycache__/cached.py": "this is not python(\n",
    "src/.hidden/x.py": "this is not python(\n",
    "tests/conftest.py": "",
    "tests/helpers.py": "",
    "tests/core/test_models.py": "from orch.models import VALUE\n",
    "tests/core/test_store.py": "from orch import store\n",
    "tests/core/test_old.py": "import orch.old\n",
    "tests/gate/test_gate.py": "import orch.gate\n",
    "tests/cli/test_cli.py": "from orch.cli import main\nimport helpers\n",
    "tests/cli/test_more.py": "from test_cli import main\n",
    "tests/integration/test_flow.py": "from orch import cli\n",
    "tests/integration/test_names.py": "import core.test_store\n",
    "scripts/tool.py": "",
    "docs/guide.md": "x\n",
}

GROUPS = (
    SelectGroup(
        name="cli",
        tests=("tests/cli/**",),
        command="pytest tests/cli",
        also=("integration",),
    ),
    SelectGroup(
        name="core",
        sources=("src/orch/models.py", "src/orch/store.py"),
        tests=("tests/core/**",),
        command="pytest tests/core",
        also=("integration",),
    ),
    SelectGroup(
        name="gate",
        sources=("src/orch/gate.py",),
        tests=("tests/gate/**",),
        command="pytest tests/gate",
        also=("integration",),
    ),
    SelectGroup(name="integration", tests=("tests/integration/**",), command="pytest"),
    SelectGroup(
        name="lanes",
        sources=("src/orch/lanes/**",),
        tests=("tests/lanes/**",),
        command="pytest tests/lanes",
        also=("integration",),
    ),
    SelectGroup(
        name="ops",
        sources=("scripts/tool.py",),
        tests=("tests/ops/**",),
        command="pytest tests/ops",
    ),
)

CONFIG = SelectConfig(
    groups=GROUPS,
    import_roots=(
        "src",
        "tests",
        "tests/core",
        "tests/gate",
        "tests/cli",
        "tests/integration",
        "absent",
    ),
    full_paths=("pyproject.toml", "tests/*.py"),
    ignore_paths=("docs/**",),
)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "wt"
    for name, text in FILES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def chosen(groups: dict[str, str]) -> Selection:
    return Selection(
        full=False,
        groups=tuple(sorted(groups)),
        reasons=tuple(f"{name}: {groups[name]}" for name in sorted(groups)),
    )


# Imports


def test_a_core_module_reaches_every_group_that_uses_it(tree: Path) -> None:
    changed = "src/orch/models.py"
    assert select(tree, [changed], CONFIG) == chosen(
        {
            "cli": f"import:{changed}:tests/cli/test_cli.py",
            "core": f"path:{changed}",
            "gate": f"import:{changed}:src/orch/gate.py",
            "integration": f"import:{changed}:tests/integration/test_flow.py",
            "lanes": f"import:{changed}:src/orch/lanes/codex.py",
        }
    )


def test_a_module_reaches_only_the_files_that_use_it(tree: Path) -> None:
    changed = "src/orch/gate.py"
    assert select(tree, [changed], CONFIG) == chosen(
        {
            "cli": f"import:{changed}:tests/cli/test_cli.py",
            "gate": f"path:{changed}",
            "integration": f"import:{changed}:tests/integration/test_flow.py",
        }
    )


def test_an_import_inside_a_function_counts(tree: Path) -> None:
    (tree / "tests/gate/test_late.py").write_text("import orch.late\n", "utf-8")
    changed = "src/orch/cli.py"
    assert select(tree, [changed], CONFIG) == chosen(
        {
            "cli": f"import:{changed}:tests/cli/test_cli.py",
            "gate": f"import:{changed}:tests/gate/test_late.py",
            "integration": f"import:{changed}:tests/integration/test_flow.py",
        }
    )


def test_the_package_init_reaches_everything_below_it(tree: Path) -> None:
    changed = "src/orch/__init__.py"
    assert select(tree, [changed], CONFIG) == chosen(
        {
            "cli": f"import:{changed}:tests/cli/test_cli.py",
            "core": f"import:{changed}:src/orch/store.py",
            "gate": f"import:{changed}:src/orch/gate.py",
            "integration": f"import:{changed}:tests/integration/test_flow.py",
            "lanes": f"import:{changed}:src/orch/lanes/codex.py",
        }
    )


def test_a_test_module_imported_by_bare_name_reaches_its_users(tree: Path) -> None:
    changed = "tests/core/test_store.py"
    assert select(tree, [changed], CONFIG) == chosen(
        {
            "core": f"path:{changed}",
            "integration": f"import:{changed}:tests/integration/test_names.py",
        }
    )


def test_a_test_change_selects_its_group_and_the_also_groups(tree: Path) -> None:
    changed = "tests/cli/test_cli.py"
    assert select(tree, [changed], CONFIG) == chosen(
        {"cli": f"path:{changed}", "integration": "also:cli"}
    )


def test_a_deleted_module_still_reaches_the_files_that_import_it(tree: Path) -> None:
    changed = "src/orch/old.py"
    assert not (tree / changed).exists()
    assert select(tree, [changed], CONFIG) == chosen(
        {
            "core": f"import:{changed}:tests/core/test_old.py",
            "integration": "also:core",
        }
    )


def test_a_relative_import_beyond_the_top_is_ignored(tree: Path) -> None:
    assert select(tree, ["src/orch/weird.py"], CONFIG) == Selection(
        full=True, reasons=("unmatched:src/orch/weird.py",)
    )


# Paths


def test_a_source_path_outside_the_import_roots(tree: Path) -> None:
    assert select(tree, ["scripts/tool.py"], CONFIG) == chosen(
        {"ops": "path:scripts/tool.py"}
    )


def test_several_changes_are_combined(tree: Path) -> None:
    assert select(
        tree, ["scripts/tool.py", "tests/cli/test_more.py", "docs/guide.md"], CONFIG
    ) == chosen(
        {
            "cli": "path:tests/cli/test_more.py",
            "integration": "also:cli",
            "ops": "path:scripts/tool.py",
        }
    )


def test_input_order_and_duplicates_do_not_matter(tree: Path) -> None:
    changed = ["tests/cli/test_more.py", "scripts/tool.py"]
    once = select(tree, changed, CONFIG)
    assert select(tree, [*reversed(changed), *changed], CONFIG) == once
    assert select(tree, iter(changed), CONFIG) == once


@pytest.mark.parametrize(
    "changed", [[], ["docs/guide.md"], [".orch-result.json"], ["docs/a.md", "docs/b"]]
)
def test_ignored_paths_select_nothing(tree: Path, changed: list[str]) -> None:
    assert select(tree, changed, CONFIG) == Selection(full=False, groups=(), reasons=())


# Full runs


@pytest.mark.parametrize(
    ("changed", "reasons"),
    [
        (["pyproject.toml"], ("full_path:pyproject.toml",)),
        (["tests/conftest.py"], ("full_path:tests/conftest.py",)),
        (["Makefile"], ("unmatched:Makefile",)),
        (["src/orch/unused.py"], ("unmatched:src/orch/unused.py",)),
        (
            ["scripts/tool.py", "pyproject.toml", "README.txt", "pyproject.toml"],
            ("full_path:pyproject.toml", "unmatched:README.txt"),
        ),
    ],
)
def test_full_runs(tree: Path, changed: list[str], reasons: tuple[str, ...]) -> None:
    assert select(tree, changed, CONFIG) == Selection(
        full=True, groups=(), reasons=reasons
    )


@pytest.mark.parametrize("content", [b"def broken(:\n", b"\xff\xfe\x00bad"])
def test_a_file_that_cannot_be_parsed_means_a_full_run(
    tree: Path, content: bytes
) -> None:
    (tree / "src/orch/broken.py").write_bytes(content)
    assert select(tree, ["src/orch/models.py"], CONFIG) == Selection(
        full=True, reasons=("parse_error:src/orch/broken.py",)
    )


def test_an_unparsable_file_does_not_matter_without_a_python_change(
    tree: Path,
) -> None:
    (tree / "src/orch/broken.py").write_bytes(b"def broken(:\n")
    assert select(tree, ["scripts/tool.py"], CONFIG) == chosen(
        {"ops": "path:scripts/tool.py"}
    )


def test_an_unreadable_file_means_a_full_run(tree: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads files whatever their mode")
    path = tree / "src/orch/locked.py"
    path.write_text("X = 1\n", "utf-8")
    path.chmod(0)
    try:
        assert select(tree, ["src/orch/models.py"], CONFIG) == Selection(
            full=True, reasons=("parse_error:src/orch/locked.py",)
        )
    finally:
        path.chmod(0o600)


# Also chains and reason order


def test_also_is_followed_to_the_end() -> None:
    config = SelectConfig(
        groups=(
            SelectGroup(
                name="x", sources=("x.txt",), tests=("tx/**",), command="c", also=("y",)
            ),
            SelectGroup(name="y", tests=("ty/**",), command="c", also=("z", "x")),
            SelectGroup(name="z", tests=("tz/**",), command="c"),
        )
    )
    assert select(Path("/nonexistent"), ["x.txt"], config) == chosen(
        {"x": "path:x.txt", "y": "also:x", "z": "also:y"}
    )


def test_a_path_reason_wins_over_an_import_reason(tree: Path) -> None:
    changed = ["src/orch/models.py", "tests/integration/test_flow.py"]
    selection = select(tree, changed, CONFIG)
    assert "integration: path:tests/integration/test_flow.py" in selection.reasons
    assert "core: path:src/orch/models.py" in selection.reasons


def test_the_smallest_reason_of_a_kind_wins(tree: Path) -> None:
    changed = ["tests/core/test_store.py", "tests/core/test_models.py"]
    selection = select(tree, changed, CONFIG)
    assert selection.reasons[0] == "core: path:tests/core/test_models.py"


# Through the real parser


def test_a_loaded_configuration_selects_the_same_way(
    tree: Path, tmp_path: Path
) -> None:
    text = """
[targets.sample]
path = "/srv/sample"
github = "owner/sample"
default_branch = "main"
setup_command = "true"
test_command = "true"
lint_command = "true"

[targets.sample.test_select]
import_roots = [
    "src", "tests", "tests/core", "tests/gate", "tests/cli", "tests/integration",
    "absent",
]
full_paths = ["pyproject.toml", "tests/*.py"]
ignore_paths = ["docs/**"]

[targets.sample.test_groups.cli]
tests = ["tests/cli/**"]
command = "pytest tests/cli"
also = ["integration"]

[targets.sample.test_groups.core]
sources = ["src/orch/models.py", "src/orch/store.py"]
tests = ["tests/core/**"]
command = "pytest tests/core"
also = ["integration"]

[targets.sample.test_groups.gate]
sources = ["src/orch/gate.py"]
tests = ["tests/gate/**"]
command = "pytest tests/gate"
also = ["integration"]

[targets.sample.test_groups.integration]
tests = ["tests/integration/**"]
command = "pytest"

[targets.sample.test_groups.lanes]
sources = ["src/orch/lanes/**"]
tests = ["tests/lanes/**"]
command = "pytest tests/lanes"
also = ["integration"]

[targets.sample.test_groups.ops]
sources = ["scripts/tool.py"]
tests = ["tests/ops/**"]
command = "pytest tests/ops"
"""
    path = tmp_path / "targets.toml"
    path.write_text(text, encoding="utf-8")
    loaded = load_targets(path)["sample"].test_select
    assert loaded == CONFIG
    for changed in (["src/orch/models.py"], ["scripts/tool.py"], ["Makefile"]):
        assert select(tree, changed, loaded) == select(tree, changed, CONFIG)
