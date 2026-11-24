"""The CLI output contract files and the pipeline/workspace boundary (4.13)."""

import ast
import json
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "schemas" / "cli"
PIPELINE = ROOT / "src" / "orch"
# (command, version). events.v1.json stays until the version pull request that
# follows m-p5-input-wait removes it (technical design 4.13, ADR 0030).
SCHEMAS = (
    ("status", 1),
    ("show", 1),
    ("events", 1),
    ("events", 2),
    ("doctor", 1),
    ("versions", 1),
    ("roadmap", 1),
)
WORKSPACE = "workspace"


def load(name: str) -> dict[str, object]:
    return json.loads((CONTRACT / name).read_text(encoding="utf-8"))


def test_the_contract_holds_exactly_the_listed_schemas() -> None:
    assert sorted(path.name for path in CONTRACT.iterdir()) == sorted(
        f"{command}.v{version}.json" for command, version in SCHEMAS
    )


@pytest.mark.parametrize(("command", "version"), SCHEMAS)
def test_each_schema_is_a_valid_2020_12_schema(command: str, version: int) -> None:
    schema = load(f"{command}.v{version}.json")
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    jsonschema.Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize(("command", "version"), SCHEMAS)
def test_each_schema_versions_its_objects_and_errors(
    command: str, version: int
) -> None:
    schema = load(f"{command}.v{version}.json")
    assert schema["$id"].endswith(f"/schemas/cli/{command}.v{version}.json")
    validator = jsonschema.Draft202012Validator(schema)
    error = {"ok": False, "schema_version": version, "error": "usage", "message": "m"}
    assert list(validator.iter_errors(error)) == []
    unversioned = {"ok": False, "error": "usage", "message": "m"}
    assert list(validator.iter_errors(unversioned)) != []
    other = {**error, "schema_version": version + 1}
    assert list(validator.iter_errors(other)) != []


# Pipeline modules never import workspace code.


def workspace_imports(source: str, module: str) -> list[str]:
    """Return the imports of the workspace package in one module's source.

    module is the dotted name of the module, used to resolve relative imports.
    """
    found: list[str] = []
    package = module.split(".")[:-1]
    for node in ast.walk(ast.parse(source)):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)]
                prefix = ".".join([*base, node.module] if node.module else base)
            else:
                prefix = node.module or ""
            names = [prefix] + [f"{prefix}.{alias.name}" for alias in node.names]
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, (ast.Name, ast.Attribute))
            and (node.func.id if isinstance(node.func, ast.Name) else node.func.attr)
            in {"import_module", "__import__"}
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            names = [node.args[0].value]
        for name in names:
            parts = name.split(".")
            if parts[0] == WORKSPACE or parts[:2] == ["orch", WORKSPACE]:
                found.append(name)
                break
    return found


@pytest.mark.parametrize(
    ("source", "module"),
    [
        ("import workspace\n", "orch.cli"),
        ("import workspace.server as s\n", "orch.cli"),
        ("from workspace import server\n", "orch.cli"),
        ("from workspace.server import app\n", "orch.cli"),
        ("from orch.workspace import app\n", "orch.cli"),
        ("from orch import workspace\n", "orch.cli"),
        ("from . import workspace\n", "orch.cli"),
        ("from .workspace import app\n", "orch.cli"),
        ("from ..workspace import app\n", "orch.lanes.codex"),
        ("import importlib\nimportlib.import_module('workspace.server')\n", "orch.x"),
        ("__import__('workspace')\n", "orch.x"),
        ("def f():\n    import workspace\n", "orch.x"),
    ],
)
def test_the_scanner_finds_workspace_imports(source: str, module: str) -> None:
    assert workspace_imports(source, module) != []


@pytest.mark.parametrize(
    "source",
    [
        "import json\n",
        "from orch import store, workspaces_note\n",
        "from . import store\n",
        "workspace = 'x'\n",
        "import workspacex\n",
        "subprocess.run(['--sandbox', 'workspace-write'])\n",
    ],
)
def test_the_scanner_ignores_other_imports(source: str) -> None:
    assert workspace_imports(source, "orch.cli") == []


def test_pipeline_modules_never_import_workspace_code() -> None:
    modules = sorted(PIPELINE.rglob("*.py"))
    assert PIPELINE / "cli.py" in modules
    found = {}
    for path in modules:
        relative = path.relative_to(PIPELINE.parent).with_suffix("")
        module = ".".join(relative.parts)
        if path.name == "__init__.py":
            module += ".__init__"
        imports = workspace_imports(path.read_text(encoding="utf-8"), module)
        if imports:
            found[str(path.relative_to(ROOT))] = imports
    assert found == {}
