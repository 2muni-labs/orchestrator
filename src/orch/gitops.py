"""All git calls. Other modules go through these functions."""

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

GIT_TIMEOUT_SEC = 300


class GitError(Exception):
    """A git command failed, timed out or was refused by a safety check."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RebaseResult:
    """Outcome of a rebase. A conflict is an outcome, not an error."""

    ok: bool
    conflicts: tuple[str, ...] = ()


def fetch(repo: Path, remote: str = "origin") -> None:
    """Fetch from a remote."""
    raise NotImplementedError


def rev_parse(cwd: Path, rev: str) -> str:
    """Return the full commit id of a revision."""
    raise NotImplementedError


def merge_base(cwd: Path, first: str, second: str = "HEAD") -> str:
    """Return the merge base of two revisions."""
    raise NotImplementedError


def commits_since(cwd: Path, base: str) -> tuple[str, ...]:
    """Return commit ids in base..HEAD, oldest first."""
    raise NotImplementedError


def changed_paths(cwd: Path, since: str) -> frozenset[str]:
    """Return all changes since a revision, including uncommitted and new files."""
    raise NotImplementedError


def diff(cwd: Path, base: str) -> str:
    """Return the diff of base..HEAD."""
    raise NotImplementedError


def create_worktree(repo: Path, worktree: Path, branch: str, start_point: str) -> None:
    """Create a task branch and its worktree."""
    raise NotImplementedError


def remove_worktree(repo: Path, worktree: Path, *, force: bool = False) -> None:
    """Remove a worktree. Without force, git refuses a dirty one."""
    raise NotImplementedError


def delete_branch(repo: Path, branch: str) -> None:
    """Delete a local task branch, merged or not."""
    raise NotImplementedError


def prune_worktrees(repo: Path) -> None:
    """Remove records of worktrees that no longer exist."""
    raise NotImplementedError


def commit(
    worktree: Path,
    message: str,
    *,
    paths: Iterable[str],
    home: Path,
    orch_home: Path,
    secrets: Iterable[str] = (),
) -> str:
    """Commit the given paths with a redacted message and return the commit id."""
    raise NotImplementedError


def rebase(worktree: Path, onto: str) -> RebaseResult:
    """Rebase the current branch. On a conflict, abort and report the files."""
    raise NotImplementedError


def push_task_branch(worktree: Path, branch: str, remote: str = "origin") -> None:
    """Push a task branch without force. A differing remote branch is an error."""
    raise NotImplementedError


def reset_to(worktree: Path, sha: str) -> None:
    """Return a linked worktree to a commit and remove untracked files."""
    raise NotImplementedError
