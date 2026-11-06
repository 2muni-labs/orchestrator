"""All git calls. Other modules go through these functions."""

import os
import re
import signal
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from orch.redact import redact

GIT_TIMEOUT_SEC = 300


class GitError(Exception):
    """A git command failed, timed out or was refused by a safety check."""


class LeaseRejectedError(GitError):
    """The remote branch is no longer at the commit the lease expected."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RebaseResult:
    """Outcome of a rebase. A conflict is an outcome, not an error."""

    ok: bool
    conflicts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class MergeResult:
    """Outcome of a temporary merge. A conflict is an outcome, not an error."""

    ok: bool
    conflicts: tuple[str, ...] = ()


def _git(cwd: Path, *args: str, input_text: str | None = None) -> str:
    """Run git with an isolated repository environment and a group timeout."""
    command = ["git", "-C", str(cwd), *args]
    env = os.environ.copy()
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_PREFIX",
    ):
        env.pop(name, None)
    env.update(GIT_TERMINAL_PROMPT="0", GIT_EDITOR="true", LC_ALL="C")
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            env=env,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(
                input=input_text, timeout=GIT_TIMEOUT_SEC
            )
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            _, stderr = process.communicate()
            raise GitError(f"{command!r}: timed out: {stderr[:500]}") from None
    except OSError as exc:
        raise GitError(f"{command!r}: {str(exc)[:500]}") from exc
    if process.returncode != 0:
        raise GitError(f"{command!r}: {stderr[:500]}")
    return stdout


def has_uncommitted_changes(repo: Path) -> bool:
    """Return True if the work tree has staged, unstaged or untracked changes."""
    raise NotImplementedError


def _validate_value(value: str) -> None:
    if not value or value.startswith("-"):
        raise GitError(f"Invalid revision or remote: {value!r}")


def _validate_branch(branch: str) -> None:
    if re.fullmatch(r"task/[A-Za-z0-9][A-Za-z0-9._-]*", branch) is None:
        raise GitError(f"Invalid task branch: {branch!r}")


def _validate_remote(remote: str) -> None:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", remote) is None:
        raise GitError(f"Invalid remote name: {remote!r}")


def _git_path(worktree: Path, value: str) -> Path:
    """Resolve a git-reported path relative to the command's working tree."""
    return (worktree / value).resolve()


def fetch(repo: Path, remote: str = "origin") -> None:
    """Fetch from a remote."""
    _validate_remote(remote)
    _git(repo, "fetch", remote)


def rev_parse(cwd: Path, rev: str) -> str:
    """Return the full commit id of a revision."""
    _validate_value(rev)
    return _git(cwd, "rev-parse", "--verify", f"{rev}^{{commit}}").strip()


def merge_base(cwd: Path, first: str, second: str = "HEAD") -> str:
    """Return the merge base of two revisions."""
    _validate_value(first)
    _validate_value(second)
    return _git(cwd, "merge-base", first, second).strip()


def commits_since(cwd: Path, base: str) -> tuple[str, ...]:
    """Return commit ids in base..HEAD, oldest first."""
    _validate_value(base)
    return tuple(_git(cwd, "rev-list", "--reverse", f"{base}..HEAD").splitlines())


def changed_paths(cwd: Path, since: str) -> frozenset[str]:
    """Return all changes since a revision, including uncommitted and new files."""
    _validate_value(since)
    tracked = _git(cwd, "diff", "--name-only", "--no-renames", "-z", since)
    untracked = _git(cwd, "ls-files", "--others", "--exclude-standard", "-z")
    return frozenset(path for path in (tracked + untracked).split("\0") if path)


def diff(cwd: Path, base: str) -> str:
    """Return the diff of base..HEAD."""
    _validate_value(base)
    return _git(cwd, "diff", "--no-color", "--no-ext-diff", f"{base}..HEAD")


def create_worktree(repo: Path, worktree: Path, branch: str, start_point: str) -> None:
    """Create a task branch and its worktree."""
    _validate_branch(branch)
    _validate_value(start_point)
    _git(repo, "worktree", "add", "-b", branch, str(worktree), start_point)


def remove_worktree(repo: Path, worktree: Path, *, force: bool = False) -> None:
    """Remove a worktree. Without force, git refuses a dirty one."""
    options = ["--force"] if force else []
    _git(repo, "worktree", "remove", *options, str(worktree))


def delete_branch(repo: Path, branch: str) -> None:
    """Delete a local task branch, merged or not."""
    _validate_branch(branch)
    _git(repo, "branch", "-D", branch)


def prune_worktrees(repo: Path) -> None:
    """Remove records of worktrees that no longer exist."""
    _git(repo, "worktree", "prune")


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
    message = redact(message, home=home, orch_home=orch_home, secrets=secrets)
    selected_paths = list(paths)
    if not selected_paths:
        raise GitError("No paths supplied for commit")
    _git(worktree, "add", "--", *selected_paths)
    _git(worktree, "commit", "-F", "-", "--", *selected_paths, input_text=message)
    return rev_parse(worktree, "HEAD")


def rebase(worktree: Path, onto: str) -> RebaseResult:
    """Rebase the current branch. On a conflict, abort and report the files."""
    _validate_value(onto)
    try:
        _git(worktree, "rebase", onto)
    except GitError:
        conflicts = _git(worktree, "diff", "--name-only", "--diff-filter=U", "-z")
        if conflicts:
            _git(worktree, "rebase", "--abort")
            return RebaseResult(
                ok=False,
                conflicts=tuple(sorted(path for path in conflicts.split("\0") if path)),
            )
        for state in ("rebase-merge", "rebase-apply"):
            path = _git(worktree, "rev-parse", "--git-path", state).strip()
            if _git_path(worktree, path).exists():
                _git(worktree, "rebase", "--abort")
                break
        raise
    return RebaseResult(ok=True)


def push_task_branch(worktree: Path, branch: str, remote: str = "origin") -> None:
    """Push a task branch without force. A differing remote branch is an error."""
    _validate_branch(branch)
    _validate_remote(remote)
    current = _git(worktree, "symbolic-ref", "--short", "HEAD").strip()
    if current != branch:
        raise GitError(f"Current branch {current!r} differs from {branch!r}")
    remote_ref = _git(worktree, "ls-remote", remote, f"refs/heads/{branch}")
    if remote_ref and remote_ref.split()[0] != rev_parse(worktree, "HEAD"):
        raise GitError(f"Remote branch {branch!r} differs from local HEAD")
    _git(worktree, "push", "-u", remote, f"{branch}:refs/heads/{branch}")


def update_task_branch(
    worktree: Path, task_id: str, expected_remote_sha: str, remote: str = "origin"
) -> str:
    """Replace task/<task_id> on the remote, leased on the last pushed commit."""
    if (
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", task_id) is None
        or ".." in task_id
        or task_id.endswith((".", ".lock"))
    ):
        raise GitError(f"Invalid task id: {task_id!r}")
    branch = f"task/{task_id}"
    if re.fullmatch(r"[0-9a-f]{40}", expected_remote_sha) is None:
        raise GitError(f"Invalid expected remote commit id: {expected_remote_sha!r}")
    _validate_remote(remote)
    current = _git(worktree, "symbolic-ref", "--short", "HEAD").strip()
    if current != branch:
        raise GitError(f"Current branch {current!r} differs from {branch!r}")
    head = rev_parse(worktree, "HEAD")
    if head == expected_remote_sha:
        return head
    try:
        _git(
            worktree,
            "push",
            f"--force-with-lease={branch}:{expected_remote_sha}",
            remote,
            f"{branch}:refs/heads/{branch}",
        )
    except GitError as exc:
        if "stale info" in str(exc):
            raise LeaseRejectedError(
                f"Remote branch moved since the last push: {branch}"
            ) from None
        raise
    return rev_parse(worktree, "HEAD")


def reset_to(worktree: Path, sha: str) -> None:
    """Return a linked worktree to a commit and remove untracked files."""
    _validate_value(sha)
    git_dir, common_dir = _git(
        worktree, "rev-parse", "--git-dir", "--git-common-dir"
    ).splitlines()
    if _git_path(worktree, git_dir) == _git_path(worktree, common_dir):
        raise GitError("Refusing to reset the main working tree")
    rev_parse(worktree, sha)
    _git(worktree, "reset", "--hard", sha)
    _git(worktree, "clean", "-fd")
    (worktree / ".orch-result.json").unlink(missing_ok=True)


def temporary_merge(worktree: Path, ref: str) -> MergeResult:
    """Merge a ref into a linked worktree to check it. Discard it with reset_to."""
    _validate_value(ref)
    git_dir, common_dir = _git(
        worktree, "rev-parse", "--git-dir", "--git-common-dir"
    ).splitlines()
    if _git_path(worktree, git_dir) == _git_path(worktree, common_dir):
        raise GitError("Refusing to merge in the main working tree")
    try:
        _git(worktree, "merge", "--no-ff", "--no-edit", ref)
    except GitError:
        conflicts = _git(worktree, "diff", "--name-only", "--diff-filter=U", "-z")
        if conflicts:
            _git(worktree, "merge", "--abort")
            return MergeResult(
                ok=False,
                conflicts=tuple(sorted(path for path in conflicts.split("\0") if path)),
            )
        path = _git(worktree, "rev-parse", "--git-path", "MERGE_HEAD").strip()
        if _git_path(worktree, path).exists():
            _git(worktree, "merge", "--abort")
        raise
    return MergeResult(ok=True)


def task_tip(worktree: Path, main_ref: str) -> str:
    """Return the task's last commit, looking through a temporary merge of main_ref."""
    _validate_value(main_ref)
    commits = _git(worktree, "rev-list", "--parents", "-n", "1", "HEAD").split()
    if len(commits) == 3 and commits[2] == rev_parse(worktree, main_ref):
        return commits[1]
    return commits[0]


def task_changed_paths(worktree: Path, since: str, main_ref: str) -> frozenset[str]:
    """Return the task's changes since a revision, without main_ref's merged files."""
    _validate_value(since)
    _validate_value(main_ref)
    tip = task_tip(worktree, main_ref)
    if tip == rev_parse(worktree, "HEAD"):
        return changed_paths(worktree, since)
    committed = _git(worktree, "diff", "--name-only", "--no-renames", "-z", since, tip)
    uncommitted = _git(worktree, "diff", "--name-only", "--no-renames", "-z", "HEAD")
    untracked = _git(worktree, "ls-files", "--others", "--exclude-standard", "-z")
    return frozenset(
        path for path in (committed + uncommitted + untracked).split("\0") if path
    )
