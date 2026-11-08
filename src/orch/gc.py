"""Remove expired files of finished tasks and the worktrees they left behind."""

import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from orch import config, gitops, store
from orch.config import Target
from orch.models import TaskState

DEFAULT_RETENTION_DAYS = 30


@dataclass(frozen=True, slots=True, kw_only=True)
class GcReport:
    """What one pass removed or left. With dry_run, what it would have removed."""

    dry_run: bool
    removed_files: tuple[str, ...] = ()
    removed_worktrees: tuple[str, ...] = ()
    skipped_worktrees: tuple[str, ...] = ()
    unknown_worktrees: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


def collect(
    conn: sqlite3.Connection,
    *,
    targets: Mapping[str, Target],
    orch_home: Path,
    now: datetime,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    dry_run: bool = False,
) -> GcReport:
    """Collect only known finished tasks, preserving links and dirty worktrees."""
    removed_files: list[str] = []
    removed_worktrees: list[str] = []
    skipped_worktrees: list[str] = []
    unknown_worktrees: list[str] = []
    errors: list[str] = []
    finished = {TaskState.CLOSED, TaskState.DISCARDED}

    for name in sorted(targets):
        target = targets[name]
        paths = config.target_paths(orch_home, name)
        tasks = {task.id: task for task in store.list_tasks(conn, target=name)}
        prune_failed = False
        if not dry_run:
            try:
                gitops.prune_worktrees(target.path)
            except (gitops.GitError, OSError) as exc:
                errors.append(f"{name} {type(exc).__name__}")
                prune_failed = True

        expired: list[str] = []
        for task in tasks.values():
            if task.state not in finished:
                continue
            try:
                age = now - datetime.fromisoformat(task.updated_at)
                if age >= timedelta(days=retention_days):
                    expired.append(re.escape(task.id))
            except (ValueError, TypeError, OverflowError) as exc:
                errors.append(f"{name}/{task.id} {type(exc).__name__}")

        file_groups = (
            (
                paths.logs,
                r"\.(?:[0-9]+\.(?:log|rejected-result\.json)|(?:gate|setup|pr|update)\.log)",
            ),
            (paths.logs / "review", r"\.[0-9]+\.log"),
            (paths.results, r"\.json"),
            (paths.reviews, r"\.(?:json|diff|triage\.md)"),
            (paths.reviews / "attempts", r"\.[0-9]+\.(?:json|triage\.md)"),
        )
        if expired:
            for directory, suffix in file_groups:
                patterns = [re.compile(task_id + suffix) for task_id in expired]
                try:
                    entries = _directory_entries(paths.root, directory)
                except OSError as exc:
                    relative = directory.relative_to(paths.root)
                    errors.append(f"{name}/{relative} {type(exc).__name__}")
                    continue
                for entry in entries:
                    if not any(pattern.fullmatch(entry.name) for pattern in patterns):
                        continue
                    relative = f"{name}/{entry.relative_to(paths.root)}"
                    try:
                        if entry.is_symlink() or not entry.is_file():
                            continue
                        if not dry_run:
                            entry.unlink()
                    except OSError as exc:
                        errors.append(f"{relative} {type(exc).__name__}")
                    else:
                        removed_files.append(relative)

        if prune_failed:
            continue
        try:
            worktrees = _directory_entries(paths.root, paths.worktrees)
        except OSError as exc:
            errors.append(f"{name}/worktrees {type(exc).__name__}")
            continue
        for worktree in worktrees:
            relative = f"{name}/{worktree.name}"
            try:
                if worktree.is_symlink() or not worktree.is_dir():
                    continue
            except OSError as exc:
                errors.append(f"{relative} {type(exc).__name__}")
                continue
            task = tasks.get(worktree.name)
            if task is None:
                unknown_worktrees.append(relative)
            elif task.state in finished:
                try:
                    if not dry_run:
                        gitops.remove_worktree(target.path, worktree)
                except gitops.GitError, OSError:
                    skipped_worktrees.append(relative)
                else:
                    removed_worktrees.append(relative)

    return GcReport(
        dry_run=dry_run,
        removed_files=tuple(sorted(removed_files)),
        removed_worktrees=tuple(sorted(removed_worktrees)),
        skipped_worktrees=tuple(sorted(skipped_worktrees)),
        unknown_worktrees=tuple(sorted(unknown_worktrees)),
        errors=tuple(sorted(errors)),
    )


def _directory_entries(root: Path, directory: Path) -> tuple[Path, ...]:
    """List a directory only if no component beneath the target root is a link."""
    current = root
    if current.is_symlink():
        return ()
    for part in directory.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            return ()
    if not directory.is_dir():
        return ()
    return tuple(sorted(directory.iterdir()))
