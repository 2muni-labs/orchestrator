"""Choose the test groups a change needs from its paths and Python imports."""

import ast
import os
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from orch.config import SelectConfig, SelectGroup
from orch.gate import RESULT_FILE, glob_matches


@dataclass(frozen=True, slots=True, kw_only=True)
class Selection:
    """The groups to run, by name, or a full run, with the reasons."""

    full: bool
    groups: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()


def _module_names(path: str, roots: tuple[str, ...]) -> set[str]:
    names = set()
    if path.endswith(".py"):
        for root in roots:
            prefix = root + "/"
            if path.startswith(prefix):
                parts = path[len(prefix) : -3].split("/")
                if parts[-1] == "__init__":
                    parts.pop()
                names.add(".".join(parts))
    return names


def _prefixes(name: str) -> set[str]:
    parts = name.split(".") if name else []
    return {".".join(parts[:end]) for end in range(1, len(parts) + 1)}


def _imports(tree: ast.AST, module: str, *, package: bool) -> set[str]:
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                used.update(_prefixes(alias.name))
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = module.split(".") if module else []
                if not package:
                    parts = parts[:-1]
                if node.level > len(parts):
                    continue
                parts = parts[: len(parts) - node.level + 1]
                if base:
                    parts.extend(base.split("."))
                base = ".".join(parts)
            used.update(_prefixes(base))
            for alias in node.names:
                used.update(_prefixes(f"{base}.{alias.name}" if base else alias.name))
    return used


def _python_files(worktree: Path, root: str) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    errors: set[str] = set()
    directory = worktree / root

    def scan_error(exc: OSError) -> None:
        if isinstance(exc, FileNotFoundError) and exc.filename == str(directory):
            return
        try:
            path = Path(exc.filename).relative_to(worktree).as_posix()
        except TypeError, ValueError:
            path = root
        errors.add(f"parse_error:{path}")

    try:
        for parent, directories, filenames in os.walk(directory, onerror=scan_error):
            directories[:] = [
                name
                for name in directories
                if not name.startswith(".") and name != "__pycache__"
            ]
            files.update(
                (Path(parent) / name).relative_to(worktree).as_posix()
                for name in filenames
                if name.endswith(".py")
            )
    except OSError, ValueError:
        errors.add(f"parse_error:{root}")
    return files, errors


def _reverse_imports(
    worktree: Path, roots: tuple[str, ...], changed: Iterable[str]
) -> tuple[dict[str, set[str]], set[str]]:
    """Index each file's users, retaining deleted changes as dependency nodes."""
    files: set[str] = set()
    errors: set[str] = set()
    for root in roots:
        root_files, root_errors = _python_files(worktree, root)
        files.update(root_files)
        errors.update(root_errors)

    modules: dict[str, set[str]] = defaultdict(set)
    aliases = {path: _module_names(path, roots) for path in files | set(changed)}
    for path, names in aliases.items():
        for name in names:
            modules[name].add(path)

    reverse: dict[str, set[str]] = defaultdict(set)
    for path in sorted(files):
        try:
            tree = ast.parse((worktree / path).read_text(encoding="utf-8"))
        except OSError, SyntaxError, ValueError, UnicodeDecodeError:
            errors.add(f"parse_error:{path}")
            continue
        used = set()
        for name in aliases[path]:
            used.update(_imports(tree, name, package=path.endswith("/__init__.py")))
        for name in used:
            for dependency in modules.get(name, ()):
                reverse[dependency].add(path)
    return reverse, errors


def _affected(path: str, reverse: dict[str, set[str]]) -> set[str]:
    affected = {path}
    pending = [path]
    while pending:
        for user in reverse.get(pending.pop(), ()):
            if user not in affected:
                affected.add(user)
                pending.append(user)
    return affected


def _matching_groups(path: str, groups: tuple[SelectGroup, ...]) -> Iterable[str]:
    for group in groups:
        if any(
            glob_matches(path, pattern) for pattern in (*group.sources, *group.tests)
        ):
            yield group.name


def select(
    worktree: Path, changed: Iterable[str], test_select: SelectConfig
) -> Selection:
    """Choose the test groups a change needs, or a full run."""
    paths = []
    full_reasons = set()
    for path in sorted(set(changed) - {RESULT_FILE}):
        if any(glob_matches(path, pattern) for pattern in test_select.full_paths):
            full_reasons.add(f"full_path:{path}")
        elif not any(
            glob_matches(path, pattern) for pattern in test_select.ignore_paths
        ):
            paths.append(path)

    python_paths = {
        path for path in paths if _module_names(path, test_select.import_roots)
    }
    reverse = {}
    parse_errors = set()
    if python_paths:
        reverse, parse_errors = _reverse_imports(
            worktree, test_select.import_roots, python_paths
        )
        full_reasons.update(parse_errors)

    reasons: dict[str, tuple[int, str]] = {}

    def record(name: str, priority: int, reason: str) -> None:
        candidate = (priority, reason)
        if name not in reasons or candidate < reasons[name]:
            reasons[name] = candidate

    for path in paths:
        matched = False
        for name in _matching_groups(path, test_select.groups):
            record(name, 0, f"path:{path}")
            matched = True
        if path in python_paths and not parse_errors:
            for affected in _affected(path, reverse):
                for name in _matching_groups(affected, test_select.groups):
                    record(name, 1, f"import:{path}:{affected}")
                    matched = True
        if not matched and not (path in python_paths and parse_errors):
            full_reasons.add(f"unmatched:{path}")

    if full_reasons:
        return Selection(full=True, reasons=tuple(sorted(full_reasons)))

    groups = {group.name: group for group in test_select.groups}
    pending = list(reasons)
    visited = set()
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        visited.add(name)
        for other in groups[name].also:
            record(other, 2, f"also:{name}")
            pending.append(other)
    names = tuple(sorted(reasons))
    return Selection(
        full=False,
        groups=names,
        reasons=tuple(f"{name}: {reasons[name][1]}" for name in names),
    )
