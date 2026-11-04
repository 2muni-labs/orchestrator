import ast
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from orch import gitops
from orch.gitops import (
    GitError,
    MergeResult,
    RebaseResult,
    changed_paths,
    commit,
    commits_since,
    create_worktree,
    delete_branch,
    diff,
    fetch,
    merge_base,
    prune_worktrees,
    push_task_branch,
    rebase,
    remove_worktree,
    reset_to,
    rev_parse,
    task_changed_paths,
    task_tip,
    temporary_merge,
    update_task_branch,
)
from orch.redact import ResidualPathError, SecretFoundError

BRANCH = "task/m1"
# Assembled at run time so that no path of this shape is written in the repository.
FAKE_HOME = Path("/", "Users", "tester")
FAKE_ORCH = FAKE_HOME / ".orch"
REDACT = {"home": FAKE_HOME, "orch_home": FAKE_ORCH}


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def write(path: Path, text: str = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def add_commit(cwd: Path, name: str, text: str = "x\n") -> str:
    write(cwd / name, text)
    git(cwd, "add", "--", name)
    git(cwd, "commit", "-qm", f"change {name}")
    return git(cwd, "rev-parse", "HEAD")


@pytest.fixture(autouse=True)
def isolated_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "gitconfig"
    config.write_text(
        "[user]\n\tname = t\n\temail = t@example.com\n[init]\n\tdefaultBranch = main\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    path = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True)
    return path


@pytest.fixture
def repo(tmp_path: Path, remote: Path) -> Path:
    path = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    write(path / ".gitignore", ".venv/\n.orch-result.json\n")
    add_commit(path, "base.txt")
    git(path, "add", ".gitignore")
    git(path, "commit", "-qm", "ignore")
    git(path, "remote", "add", "origin", str(remote))
    git(path, "push", "-q", "origin", "main")
    git(path, "fetch", "-q", "origin")
    return path


@pytest.fixture
def worktree(repo: Path, tmp_path: Path) -> Path:
    path = tmp_path / "wt"
    create_worktree(repo, path, BRANCH, "origin/main")
    return path


# Queries


def test_fetch_updates_the_remote_tracking_branch(
    repo: Path, tmp_path: Path, remote: Path
) -> None:
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
    sha = add_commit(other, "new.txt")
    git(other, "push", "-q", "origin", "main")
    fetch(repo)
    assert git(repo, "rev-parse", "origin/main") == sha


def test_fetch_from_unknown_remote_raises(repo: Path) -> None:
    with pytest.raises(GitError):
        fetch(repo, "nope")


def test_rev_parse_returns_the_full_id(repo: Path) -> None:
    assert rev_parse(repo, "HEAD") == git(repo, "rev-parse", "HEAD")
    assert len(rev_parse(repo, "HEAD")) == 40


def test_rev_parse_unknown_revision_raises(repo: Path) -> None:
    with pytest.raises(GitError):
        rev_parse(repo, "no-such-branch")


def test_merge_base_and_commits_since_are_oldest_first(
    worktree: Path, repo: Path
) -> None:
    base = git(repo, "rev-parse", "origin/main")
    first = add_commit(worktree, "a.txt")
    second = add_commit(worktree, "b.txt")
    assert merge_base(worktree, "origin/main") == base
    assert commits_since(worktree, base) == (first, second)
    assert commits_since(worktree, second) == ()


def test_changed_paths_includes_every_kind_of_change(worktree: Path) -> None:
    since = rev_parse(worktree, "HEAD")
    add_commit(worktree, "committed.txt")
    write(worktree / "base.txt", "modified\n")
    write(worktree / "staged.txt")
    git(worktree, "add", "staged.txt")
    write(worktree / "new dir" / "이름 있는 파일.txt")
    write(worktree / ".venv" / "ignored.txt")
    write(worktree / ".orch-result.json", "{}")
    assert changed_paths(worktree, since) == {
        "committed.txt",
        "base.txt",
        "staged.txt",
        "new dir/이름 있는 파일.txt",
    }


def test_changed_paths_reports_both_sides_of_a_rename(worktree: Path) -> None:
    since = rev_parse(worktree, "HEAD")
    git(worktree, "mv", "base.txt", "moved.txt")
    git(worktree, "commit", "-qm", "move")
    assert changed_paths(worktree, since) == {"base.txt", "moved.txt"}


def test_changed_paths_reports_a_deletion(worktree: Path) -> None:
    since = rev_parse(worktree, "HEAD")
    (worktree / "base.txt").unlink()
    assert changed_paths(worktree, since) == {"base.txt"}


def test_diff_covers_only_committed_changes_since_base(worktree: Path) -> None:
    base = rev_parse(worktree, "HEAD")
    add_commit(worktree, "a.txt", "committed-line\n")
    write(worktree / "b.txt", "uncommitted-line\n")
    text = diff(worktree, base)
    assert "committed-line" in text
    assert "uncommitted-line" not in text


# Worktrees and branches


def test_create_worktree_starts_at_the_start_point(worktree: Path, repo: Path) -> None:
    assert git(worktree, "rev-parse", "--abbrev-ref", "HEAD") == BRANCH
    assert git(worktree, "rev-parse", "HEAD") == git(repo, "rev-parse", "origin/main")


def test_create_worktree_on_an_existing_branch_raises(
    worktree: Path, repo: Path, tmp_path: Path
) -> None:
    with pytest.raises(GitError):
        create_worktree(repo, tmp_path / "wt2", BRANCH, "origin/main")


def test_remove_worktree_refuses_a_dirty_one_without_force(
    worktree: Path, repo: Path
) -> None:
    write(worktree / "dirty.txt")
    with pytest.raises(GitError):
        remove_worktree(repo, worktree)
    assert worktree.is_dir()
    remove_worktree(repo, worktree, force=True)
    assert not worktree.exists()


def test_delete_branch_removes_an_unmerged_task_branch(
    worktree: Path, repo: Path
) -> None:
    add_commit(worktree, "a.txt")
    remove_worktree(repo, worktree)
    delete_branch(repo, BRANCH)
    assert git(repo, "branch", "--list", BRANCH) == ""


def test_prune_forgets_a_worktree_that_is_gone(
    repo: Path, tmp_path: Path, worktree: Path
) -> None:
    subprocess.run(["rm", "-rf", str(worktree)], check=True)
    prune_worktrees(repo)
    assert str(worktree) not in git(repo, "worktree", "list")
    delete_branch(repo, BRANCH)


# Task branch rule


@pytest.mark.parametrize(
    "branch", ["main", "master", "feature/x", "task/", "task/a b", "task/-x", "-x", ""]
)
def test_only_task_branches_are_accepted(
    repo: Path, tmp_path: Path, branch: str
) -> None:
    with pytest.raises(GitError):
        create_worktree(repo, tmp_path / "wt3", branch, "origin/main")
    assert not (tmp_path / "wt3").exists()
    with pytest.raises(GitError):
        delete_branch(repo, branch)
    with pytest.raises(GitError):
        push_task_branch(repo, branch)
    assert git(repo, "rev-parse", "--verify", "main")


@pytest.mark.parametrize("value", ["--all", "-x", "--upload-pack=x", ""])
def test_values_that_look_like_options_are_refused(
    repo: Path, tmp_path: Path, value: str
) -> None:
    with pytest.raises(GitError):
        fetch(repo, value)
    with pytest.raises(GitError):
        rev_parse(repo, value)
    with pytest.raises(GitError):
        commits_since(repo, value)
    with pytest.raises(GitError):
        changed_paths(repo, value)
    with pytest.raises(GitError):
        diff(repo, value)
    with pytest.raises(GitError):
        merge_base(repo, value)
    with pytest.raises(GitError):
        rebase(repo, value)
    with pytest.raises(GitError):
        create_worktree(repo, tmp_path / "wt4", BRANCH, value)


# Commit


def test_commit_returns_the_new_id_and_keeps_a_multiline_message(
    worktree: Path,
) -> None:
    write(worktree / "a.txt")
    message = "Add a file\n\n본문 설명입니다.\n"
    sha = commit(worktree, message, paths=["a.txt"], **REDACT)
    assert sha == git(worktree, "rev-parse", "HEAD")
    assert git(worktree, "log", "-1", "--format=%B") == message.strip()


def test_commit_message_goes_through_redact(worktree: Path) -> None:
    write(worktree / "a.txt")
    tree = f"{FAKE_ORCH}/hub/worktrees/m1/src/a.py"
    commit(worktree, f"Fix {tree} from {FAKE_HOME}/x", paths=["a.txt"], **REDACT)
    assert git(worktree, "log", "-1", "--format=%s") == "Fix src/a.py from ~/x"


def fake_token() -> str:
    return "gh" + "s_" + "a" * 36


@pytest.mark.parametrize(
    ("make_message", "kwargs", "error"),
    [
        (fake_token, {}, SecretFoundError),
        (lambda: "contains hunter2", {"secrets": ("hunter2",)}, SecretFoundError),
        (lambda: "/" + "home" + "/someone/x", {}, ResidualPathError),
    ],
    ids=["token", "exact_secret", "residual_path"],
)
def test_rejected_message_commits_and_stages_nothing(
    worktree: Path,
    make_message: Callable[[], str],
    kwargs: dict,
    error: type[Exception],
) -> None:
    head = git(worktree, "rev-parse", "HEAD")
    write(worktree / "a.txt")
    with pytest.raises(error):
        commit(worktree, make_message(), paths=["a.txt"], **REDACT, **kwargs)
    assert git(worktree, "rev-parse", "HEAD") == head
    assert git(worktree, "diff", "--cached", "--name-only") == ""


def test_commit_includes_only_the_given_paths(worktree: Path) -> None:
    write(worktree / "a.txt")
    write(worktree / "other-staged.txt")
    write(worktree / "unstaged.txt")
    git(worktree, "add", "other-staged.txt")
    commit(worktree, "Add a", paths=["a.txt"], **REDACT)
    assert git(worktree, "show", "--name-only", "--format=", "HEAD") == "a.txt"
    assert git(worktree, "diff", "--cached", "--name-only") == "other-staged.txt"


def test_commit_stages_new_modified_and_deleted_paths(worktree: Path) -> None:
    write(worktree / "new file.txt", "different content\n")
    (worktree / "base.txt").unlink()
    commit(worktree, "Change", paths=["new file.txt", "base.txt"], **REDACT)
    shown = git(worktree, "show", "--name-status", "--format=", "HEAD").splitlines()
    assert sorted(shown) == ["A\tnew file.txt", "D\tbase.txt"]


def test_commit_path_that_looks_like_an_option_is_a_path(worktree: Path) -> None:
    write(worktree / "-weird.txt")
    commit(worktree, "Add", paths=["-weird.txt"], **REDACT)
    assert git(worktree, "show", "--name-only", "--format=", "HEAD") == "-weird.txt"


@pytest.mark.parametrize("paths", [[], ["missing.txt"]])
def test_commit_without_usable_paths_raises(worktree: Path, paths: list[str]) -> None:
    head = git(worktree, "rev-parse", "HEAD")
    with pytest.raises(GitError):
        commit(worktree, "Change", paths=paths, **REDACT)
    assert git(worktree, "rev-parse", "HEAD") == head


def test_commit_with_no_change_raises(worktree: Path) -> None:
    with pytest.raises(GitError):
        commit(worktree, "Change", paths=["base.txt"], **REDACT)


@pytest.mark.parametrize("message", ["", "  \n"])
def test_commit_with_an_empty_message_raises(worktree: Path, message: str) -> None:
    write(worktree / "a.txt")
    with pytest.raises(GitError):
        commit(worktree, message, paths=["a.txt"], **REDACT)


def test_commit_runs_hooks_and_reports_their_failure(
    worktree: Path, repo: Path
) -> None:
    hook = repo / ".git" / "hooks" / "pre-commit"
    write(hook, "#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    write(worktree / "a.txt")
    with pytest.raises(GitError):
        commit(worktree, "Add", paths=["a.txt"], **REDACT)


# Rebase


def advance_main(repo: Path, name: str, text: str = "x\n") -> str:
    sha = add_commit(repo, name, text)
    git(repo, "push", "-q", "origin", "main")
    git(repo, "fetch", "-q", "origin")
    return sha


def test_rebase_without_conflict(worktree: Path, repo: Path) -> None:
    add_commit(worktree, "a.txt")
    main = advance_main(repo, "b.txt")
    assert rebase(worktree, "origin/main") == RebaseResult(ok=True)
    assert git(worktree, "merge-base", "HEAD", "origin/main") == main


def test_conflict_aborts_and_reports_the_files(worktree: Path, repo: Path) -> None:
    add_commit(worktree, "same file.txt", "branch\n")
    head = git(worktree, "rev-parse", "HEAD")
    advance_main(repo, "same file.txt", "main\n")
    assert rebase(worktree, "origin/main") == RebaseResult(
        ok=False, conflicts=("same file.txt",)
    )
    assert git(worktree, "rev-parse", "HEAD") == head
    assert git(worktree, "status", "--porcelain") == ""
    state = git(worktree, "rev-parse", "--git-path", "rebase-merge")
    assert not (worktree / state).exists()


def test_unknown_onto_raises(worktree: Path) -> None:
    with pytest.raises(GitError):
        rebase(worktree, "no-such-branch")


def test_rebase_of_a_dirty_tree_raises_and_leaves_no_rebase_state(
    worktree: Path, repo: Path
) -> None:
    add_commit(worktree, "a.txt")
    advance_main(repo, "b.txt")
    write(worktree / "base.txt", "dirty\n")
    with pytest.raises(GitError):
        rebase(worktree, "origin/main")
    assert (worktree / "base.txt").read_text(encoding="utf-8") == "dirty\n"
    state = git(worktree, "rev-parse", "--git-path", "rebase-merge")
    assert not (worktree / state).exists()


# Push


def test_push_creates_the_remote_branch_and_sets_upstream(
    worktree: Path, remote: Path
) -> None:
    sha = add_commit(worktree, "a.txt")
    push_task_branch(worktree, BRANCH)
    assert git(remote, "rev-parse", f"refs/heads/{BRANCH}") == sha
    assert git(worktree, "rev-parse", "--abbrev-ref", "@{u}") == f"origin/{BRANCH}"


def test_push_of_the_same_commit_again_succeeds(worktree: Path, remote: Path) -> None:
    sha = add_commit(worktree, "a.txt")
    push_task_branch(worktree, BRANCH)
    push_task_branch(worktree, BRANCH)
    assert git(remote, "rev-parse", f"refs/heads/{BRANCH}") == sha


def test_push_refuses_when_the_remote_branch_is_behind(
    worktree: Path, remote: Path
) -> None:
    first = add_commit(worktree, "a.txt")
    push_task_branch(worktree, BRANCH)
    add_commit(worktree, "b.txt")
    with pytest.raises(GitError):
        push_task_branch(worktree, BRANCH)
    assert git(remote, "rev-parse", f"refs/heads/{BRANCH}") == first


def test_push_refuses_a_diverged_remote_branch(worktree: Path, remote: Path) -> None:
    first = add_commit(worktree, "a.txt")
    push_task_branch(worktree, BRANCH)
    git(worktree, "commit", "-q", "--amend", "-m", "rewritten")
    with pytest.raises(GitError):
        push_task_branch(worktree, BRANCH)
    assert git(remote, "rev-parse", f"refs/heads/{BRANCH}") == first


def test_push_refuses_a_branch_other_than_the_current_one(
    worktree: Path, remote: Path
) -> None:
    with pytest.raises(GitError):
        push_task_branch(worktree, "task/other")
    assert git(remote, "for-each-ref", "refs/heads/task") == ""


def test_push_failure_raises(worktree: Path, repo: Path) -> None:
    git(repo, "remote", "set-url", "origin", "/nonexistent/remote.git")
    with pytest.raises(GitError):
        push_task_branch(worktree, BRANCH)


# Branch update


@pytest.fixture
def git_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put a logging git first on PATH. Each line of the log is one call's arguments."""
    real_git = shutil.which("git")
    assert real_git is not None
    directory = tmp_path / "shim"
    log = tmp_path / "git-calls.log"
    write(
        directory / "git",
        f"""#!/bin/sh
printf '%s\\037' "$@" >> '{log}'
printf '\\n' >> '{log}'
exec '{real_git}' "$@"
""",
    )
    (directory / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")
    return log


def push_calls(log: Path) -> list[list[str]]:
    """Return the arguments after "push" of every logged push."""
    if not log.exists():
        return []
    calls = [line.split("\037")[:-1] for line in log.read_text().splitlines()]
    return [call[call.index("push") + 1 :] for call in calls if "push" in call]


@pytest.fixture
def pushed(worktree: Path) -> str:
    """Push the task branch, rewrite the local commit and return the pushed id."""
    sha = add_commit(worktree, "a.txt")
    push_task_branch(worktree, BRANCH)
    git(worktree, "commit", "-q", "--amend", "-m", "rewritten")
    return sha


def test_update_replaces_the_remote_branch_and_returns_the_new_id(
    worktree: Path, remote: Path, pushed: str
) -> None:
    main_before = git(remote, "rev-parse", "refs/heads/main")
    new = git(worktree, "rev-parse", "HEAD")
    assert new != pushed
    assert update_task_branch(worktree, "m1", pushed) == new
    assert git(remote, "rev-parse", f"refs/heads/{BRANCH}") == new
    assert git(remote, "rev-parse", "refs/heads/main") == main_before


def test_update_runs_exactly_the_allowed_push_command(
    worktree: Path, pushed: str, git_calls: Path
) -> None:
    update_task_branch(worktree, "m1", pushed)
    assert push_calls(git_calls) == [
        [
            f"--force-with-lease={BRANCH}:{pushed}",
            "origin",
            f"{BRANCH}:refs/heads/{BRANCH}",
        ]
    ]


def test_update_refuses_when_someone_else_pushed(
    worktree: Path, remote: Path, tmp_path: Path, pushed: str
) -> None:
    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", "-q", "-b", BRANCH, str(remote), str(other)], check=True
    )
    theirs = add_commit(other, "theirs.txt")
    git(other, "push", "-q", "origin", BRANCH)
    head = git(worktree, "rev-parse", "HEAD")
    with pytest.raises(GitError):
        update_task_branch(worktree, "m1", pushed)
    assert git(remote, "rev-parse", f"refs/heads/{BRANCH}") == theirs
    assert git(worktree, "rev-parse", "HEAD") == head


def test_lease_uses_the_given_id_even_after_a_fetch_moved_the_tracking_branch(
    worktree: Path, remote: Path, tmp_path: Path, pushed: str
) -> None:
    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", "-q", "-b", BRANCH, str(remote), str(other)], check=True
    )
    theirs = add_commit(other, "theirs.txt")
    git(other, "push", "-q", "origin", BRANCH)
    git(worktree, "fetch", "-q", "origin")
    with pytest.raises(GitError):
        update_task_branch(worktree, "m1", pushed)
    assert git(remote, "rev-parse", f"refs/heads/{BRANCH}") == theirs


def test_update_refuses_a_remote_branch_that_does_not_exist(
    worktree: Path, remote: Path
) -> None:
    sha = add_commit(worktree, "a.txt")
    add_commit(worktree, "b.txt")
    with pytest.raises(GitError):
        update_task_branch(worktree, "m1", sha)
    assert git(remote, "for-each-ref", "refs/heads/task") == ""


def test_update_with_nothing_to_change_does_not_push(
    worktree: Path, git_calls: Path
) -> None:
    sha = add_commit(worktree, "a.txt")
    push_task_branch(worktree, BRANCH)
    git_calls.unlink()
    assert update_task_branch(worktree, "m1", sha) == sha
    assert push_calls(git_calls) == []


@pytest.mark.parametrize(
    "task_id",
    [
        "",
        "../main",
        "a/b",
        "-x",
        "+x",
        "x:refs/heads/main",
        "HEAD:refs/heads/main",
        "a b",
        "a..b",
        "a.",
        "a.lock",
        ".x",
        "m1\n",
    ],
)
def test_update_refuses_bad_task_ids_without_pushing(
    worktree: Path, remote: Path, pushed: str, git_calls: Path, task_id: str
) -> None:
    git_calls.unlink(missing_ok=True)
    main_before = git(remote, "rev-parse", "refs/heads/main")
    with pytest.raises(GitError):
        update_task_branch(worktree, task_id, pushed)
    assert push_calls(git_calls) == []
    assert git(remote, "rev-parse", f"refs/heads/{BRANCH}") == pushed
    assert git(remote, "rev-parse", "refs/heads/main") == main_before


@pytest.mark.parametrize(
    "expected",
    [
        "",
        "--force",
        "abc",
        "A" * 40,
        "g" * 40,
        "a" * 39,
        "a" * 41,
        "a" * 40 + "\n",
        "HEAD",
        "origin/task/m1",
    ],
)
def test_update_requires_a_full_commit_id_as_the_lease(
    worktree: Path, remote: Path, pushed: str, git_calls: Path, expected: str
) -> None:
    git_calls.unlink(missing_ok=True)
    with pytest.raises(GitError):
        update_task_branch(worktree, "m1", expected)
    assert push_calls(git_calls) == []
    assert git(remote, "rev-parse", f"refs/heads/{BRANCH}") == pushed


def test_update_refuses_a_task_other_than_the_current_branch(
    worktree: Path, remote: Path, pushed: str, git_calls: Path
) -> None:
    git_calls.unlink(missing_ok=True)
    with pytest.raises(GitError):
        update_task_branch(worktree, "other", pushed)
    assert push_calls(git_calls) == []
    assert git(remote, "for-each-ref", "refs/heads/task/other") == ""


def test_update_refuses_the_main_working_tree(
    repo: Path, remote: Path, git_calls: Path
) -> None:
    git_calls.unlink(missing_ok=True)
    main = git(repo, "rev-parse", "HEAD")
    with pytest.raises(GitError):
        update_task_branch(repo, "main", main)
    assert push_calls(git_calls) == []
    assert git(remote, "for-each-ref", "refs/heads/task") == ""


def test_update_refuses_a_detached_head(
    worktree: Path, pushed: str, git_calls: Path
) -> None:
    git(worktree, "checkout", "-q", "--detach")
    git_calls.unlink(missing_ok=True)
    with pytest.raises(GitError):
        update_task_branch(worktree, "m1", pushed)
    assert push_calls(git_calls) == []


@pytest.mark.parametrize("value", ["", "-x", "--upload-pack=x"])
def test_update_refuses_option_shaped_remotes(
    worktree: Path, pushed: str, git_calls: Path, value: str
) -> None:
    git_calls.unlink(missing_ok=True)
    with pytest.raises(GitError):
        update_task_branch(worktree, "m1", pushed, value)
    assert push_calls(git_calls) == []


def test_push_task_branch_still_refuses_after_a_rewrite(
    worktree: Path, remote: Path, pushed: str, git_calls: Path
) -> None:
    git_calls.unlink(missing_ok=True)
    with pytest.raises(GitError):
        push_task_branch(worktree, BRANCH)
    assert push_calls(git_calls) == []
    assert git(remote, "rev-parse", f"refs/heads/{BRANCH}") == pushed


def test_only_update_task_branch_uses_a_force_option() -> None:
    tree = ast.parse(Path(gitops.__file__).read_text(encoding="utf-8"))
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef):
            continue
        for node in ast.walk(function):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if node.value.startswith("--force-with-lease"):
                assert function.name == "update_task_branch"
            assert node.value not in {"-f", "--force-if-includes"}
            if node.value == "--force":
                assert function.name == "remove_worktree"


# Temporary merge


MAIN = "origin/main"


def task_commit(worktree: Path, name: str = "task.txt", text: str = "task\n") -> str:
    return add_commit(worktree, name, text)


def move_main(repo: Path, worktree: Path, name: str, text: str = "main\n") -> str:
    sha = advance_main(repo, name, text)
    fetch(worktree)
    return sha


def merge_head_exists(worktree: Path) -> bool:
    path = git(worktree, "rev-parse", "--git-path", "MERGE_HEAD")
    return (worktree / path).exists()


def test_temporary_merge_creates_a_merge_with_the_task_as_first_parent(
    worktree: Path, repo: Path
) -> None:
    tip = task_commit(worktree)
    main = move_main(repo, worktree, "main.txt")
    assert temporary_merge(worktree, MAIN) == MergeResult(ok=True)
    parents = git(worktree, "rev-list", "--parents", "-n", "1", "HEAD").split()
    assert parents[1:] == [tip, main]
    assert (worktree / "main.txt").exists()


def test_temporary_merge_runs_the_exact_command(
    worktree: Path, repo: Path, git_calls: Path
) -> None:
    task_commit(worktree)
    move_main(repo, worktree, "main.txt")
    git_calls.unlink(missing_ok=True)
    temporary_merge(worktree, MAIN)
    calls = [line.split("\037")[:-1] for line in git_calls.read_text().splitlines()]
    merges = [call for call in calls if "merge" in call and "--no-ff" in call]
    assert len(merges) == 1
    assert merges[0][-4:] == ["merge", "--no-ff", "--no-edit", MAIN]


def test_temporary_merge_of_a_contained_ref_changes_nothing(worktree: Path) -> None:
    tip = task_commit(worktree)
    assert temporary_merge(worktree, MAIN) == MergeResult(ok=True)
    assert git(worktree, "rev-parse", "HEAD") == tip


def test_temporary_merge_conflict_aborts_and_reports_sorted_files(
    worktree: Path, repo: Path
) -> None:
    add_commit(worktree, "z.txt", "task z\n")
    tip = add_commit(worktree, "a.txt", "task a\n")
    advance_main(repo, "z.txt", "main z\n")
    move_main(repo, worktree, "a.txt", "main a\n")
    result = temporary_merge(worktree, MAIN)
    assert result == MergeResult(ok=False, conflicts=("a.txt", "z.txt"))
    assert git(worktree, "rev-parse", "HEAD") == tip
    assert not merge_head_exists(worktree)
    assert git(worktree, "status", "--porcelain") == ""


def test_temporary_merge_of_an_unknown_ref_raises(worktree: Path) -> None:
    with pytest.raises(GitError):
        temporary_merge(worktree, "origin/nope")
    assert not merge_head_exists(worktree)


def test_temporary_merge_of_a_dirty_tree_raises_and_leaves_no_merge_state(
    worktree: Path, repo: Path
) -> None:
    tip = task_commit(worktree)
    move_main(repo, worktree, "shared.txt", "main\n")
    write(worktree / "shared.txt", "untracked\n")
    with pytest.raises(GitError):
        temporary_merge(worktree, MAIN)
    assert git(worktree, "rev-parse", "HEAD") == tip
    assert not merge_head_exists(worktree)
    assert (worktree / "shared.txt").read_text() == "untracked\n"


def test_temporary_merge_stopped_by_a_hook_raises_and_cleans_up(
    worktree: Path, repo: Path
) -> None:
    tip = task_commit(worktree)
    move_main(repo, worktree, "main.txt")
    hook = repo / ".git" / "hooks" / "pre-merge-commit"
    write(hook, "#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    with pytest.raises(GitError):
        temporary_merge(worktree, MAIN)
    assert git(worktree, "rev-parse", "HEAD") == tip
    assert not merge_head_exists(worktree)
    assert git(worktree, "status", "--porcelain") == ""


def test_temporary_merge_refuses_the_main_working_tree(repo: Path) -> None:
    head = git(repo, "rev-parse", "HEAD")
    with pytest.raises(GitError):
        temporary_merge(repo, "HEAD~1")
    assert git(repo, "rev-parse", "HEAD") == head


@pytest.mark.parametrize("value", ["", "-x", "--abort"])
def test_temporary_merge_refuses_option_shaped_refs(worktree: Path, value: str) -> None:
    with pytest.raises(GitError):
        temporary_merge(worktree, value)


def test_reset_to_discards_a_temporary_merge(worktree: Path, repo: Path) -> None:
    tip = task_commit(worktree)
    move_main(repo, worktree, "main.txt")
    temporary_merge(worktree, MAIN)
    reset_to(worktree, tip)
    assert git(worktree, "rev-parse", "HEAD") == tip
    assert not (worktree / "main.txt").exists()


# The task's last commit and changes, looking through a temporary merge


def test_task_tip_without_a_merge_is_head(worktree: Path) -> None:
    tip = task_commit(worktree)
    assert task_tip(worktree, MAIN) == tip


def test_task_tip_of_a_temporary_merge_is_its_first_parent(
    worktree: Path, repo: Path
) -> None:
    tip = task_commit(worktree)
    move_main(repo, worktree, "main.txt")
    temporary_merge(worktree, MAIN)
    assert task_tip(worktree, MAIN) == tip


def test_task_tip_of_another_merge_is_head(worktree: Path, repo: Path) -> None:
    task_commit(worktree)
    git(worktree, "branch", "side", "HEAD~1")
    git(worktree, "checkout", "-q", "side")
    add_commit(worktree, "side.txt")
    side = git(worktree, "rev-parse", "HEAD")
    git(worktree, "checkout", "-q", BRANCH)
    git(worktree, "merge", "-q", "--no-ff", "--no-edit", "side")
    head = git(worktree, "rev-parse", "HEAD")
    assert git(worktree, "rev-parse", "HEAD^2") == side
    assert task_tip(worktree, MAIN) == head


def test_task_tip_refuses_option_shaped_refs(worktree: Path) -> None:
    with pytest.raises(GitError):
        task_tip(worktree, "-x")


def test_task_changed_paths_without_a_merge_matches_changed_paths(
    worktree: Path,
) -> None:
    since = task_commit(worktree, "first.txt")
    add_commit(worktree, "committed.txt")
    write(worktree / "first.txt", "edited\n")
    write(worktree / "new.txt")
    expected = {"committed.txt", "first.txt", "new.txt"}
    assert task_changed_paths(worktree, since, MAIN) == expected
    assert task_changed_paths(worktree, since, MAIN) == changed_paths(worktree, since)


def test_task_changed_paths_leaves_out_main_files_of_a_temporary_merge(
    worktree: Path, repo: Path
) -> None:
    since = task_commit(worktree, "first.txt")
    add_commit(worktree, "committed.txt")
    move_main(repo, worktree, "main.txt")
    temporary_merge(worktree, MAIN)
    write(worktree / "untracked.txt")
    write(worktree / "committed.txt", "edited after the merge\n")
    write(worktree / "first.txt", "edited after the merge\n")
    assert task_changed_paths(worktree, since, MAIN) == {
        "committed.txt",
        "first.txt",
        "untracked.txt",
    }


def test_task_changed_paths_reports_a_deletion_through_a_merge(
    worktree: Path, repo: Path
) -> None:
    since = task_commit(worktree, "first.txt")
    git(worktree, "rm", "-q", "first.txt")
    git(worktree, "commit", "-qm", "delete")
    move_main(repo, worktree, "main.txt")
    temporary_merge(worktree, MAIN)
    assert task_changed_paths(worktree, since, MAIN) == {"first.txt"}


def test_task_changed_paths_ignores_ignored_files(worktree: Path) -> None:
    since = task_commit(worktree)
    write(worktree / ".orch-result.json", "{}\n")
    assert task_changed_paths(worktree, since, MAIN) == frozenset()


@pytest.mark.parametrize(
    ("since", "main_ref"), [("-x", MAIN), ("HEAD", "-x"), ("", MAIN), ("HEAD", "")]
)
def test_task_changed_paths_refuses_option_shaped_values(
    worktree: Path, since: str, main_ref: str
) -> None:
    with pytest.raises(GitError):
        task_changed_paths(worktree, since, main_ref)


# Remote names


@pytest.mark.parametrize(
    "value",
    [
        "ext::sh -c touch%20pwned",
        "fd::3",
        "https://example.com/x.git",
        "ssh://example.com/x.git",
        "git@example.com:x.git",
        "../other.git",
        "/tmp/other.git",
        "origin\n",
        "",
        "-x",
        ".hidden",
    ],
)
def test_only_remote_names_are_accepted(
    worktree: Path,
    repo: Path,
    pushed: str,
    git_calls: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    # A transport helper such as fd:: would wait for input if it ever reached git.
    monkeypatch.setattr(gitops, "GIT_TIMEOUT_SEC", 5)
    git_calls.unlink(missing_ok=True)
    with pytest.raises(GitError):
        fetch(repo, value)
    with pytest.raises(GitError):
        push_task_branch(worktree, BRANCH, value)
    with pytest.raises(GitError):
        update_task_branch(worktree, "m1", pushed, value)
    assert not git_calls.exists()


@pytest.fixture
def upstream(worktree: Path, repo: Path, tmp_path: Path) -> Path:
    path = tmp_path / "upstream.git"
    subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True)
    git(repo, "remote", "add", "upstream", str(path))
    git(repo, "push", "-q", "upstream", "main")
    return path


def test_fetch_from_another_remote_name(repo: Path, upstream: Path) -> None:
    fetch(repo, "upstream")
    assert git(repo, "rev-parse", "upstream/main") == git(repo, "rev-parse", "main")


def test_push_and_update_use_the_given_remote_name(
    worktree: Path, remote: Path, upstream: Path
) -> None:
    first = add_commit(worktree, "a.txt")
    push_task_branch(worktree, BRANCH, "upstream")
    assert git(upstream, "rev-parse", f"refs/heads/{BRANCH}") == first
    git(worktree, "commit", "-q", "--amend", "-m", "rewritten")
    second = update_task_branch(worktree, "m1", first, "upstream")
    assert git(upstream, "rev-parse", f"refs/heads/{BRANCH}") == second
    with pytest.raises(subprocess.CalledProcessError):
        git(remote, "rev-parse", "--verify", f"refs/heads/{BRANCH}")


# Reset


def test_reset_to_restores_the_commit_and_keeps_ignored_files(worktree: Path) -> None:
    test_commit = add_commit(worktree, "tests.txt")
    add_commit(worktree, "later.txt")
    write(worktree / "base.txt", "modified\n")
    write(worktree / "staged.txt")
    git(worktree, "add", "staged.txt")
    write(worktree / "untracked" / "file.txt")
    write(worktree / ".venv" / "kept.txt")
    write(worktree / ".orch-result.json", "{}")

    reset_to(worktree, test_commit)

    assert git(worktree, "rev-parse", "HEAD") == test_commit
    assert git(worktree, "status", "--porcelain") == ""
    assert (worktree / "base.txt").read_text(encoding="utf-8") == "x\n"
    assert not (worktree / "later.txt").exists()
    assert not (worktree / "untracked").exists()
    assert not (worktree / ".orch-result.json").exists()
    assert (worktree / ".venv" / "kept.txt").exists()


def test_reset_to_works_without_a_result_file(worktree: Path) -> None:
    head = git(worktree, "rev-parse", "HEAD")
    reset_to(worktree, head)
    assert git(worktree, "rev-parse", "HEAD") == head


def test_reset_to_refuses_the_main_working_tree(repo: Path) -> None:
    head = git(repo, "rev-parse", "HEAD")
    write(repo / "precious.txt", "keep\n")
    write(repo / "base.txt", "user edit\n")
    with pytest.raises(GitError):
        reset_to(repo, "HEAD~1")
    assert git(repo, "rev-parse", "HEAD") == head
    assert (repo / "precious.txt").exists()
    assert (repo / "base.txt").read_text(encoding="utf-8") == "user edit\n"


def test_reset_to_unknown_commit_changes_nothing(worktree: Path) -> None:
    write(worktree / "untracked.txt")
    with pytest.raises(GitError):
        reset_to(worktree, "0" * 40)
    assert (worktree / "untracked.txt").exists()


@pytest.mark.parametrize("value", ["--hard", "-x", ""])
def test_reset_to_refuses_option_shaped_values(worktree: Path, value: str) -> None:
    write(worktree / "untracked.txt")
    with pytest.raises(GitError):
        reset_to(worktree, value)
    assert (worktree / "untracked.txt").exists()


# Process rules


def test_git_environment_variables_do_not_redirect_commands(
    repo: Path, worktree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = git(worktree, "rev-parse", "HEAD")
    other = tmp_path / "other"
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    add_commit(other, "o.txt")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other / ".git" / "index"))
    assert rev_parse(worktree, "HEAD") == expected


def test_missing_directory_raises_git_error(tmp_path: Path) -> None:
    with pytest.raises(GitError):
        rev_parse(tmp_path / "missing", "HEAD")


def test_timeout_kills_the_whole_process_group(
    worktree: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook = repo / ".git" / "hooks" / "pre-commit"
    write(hook, "#!/bin/sh\nsleep 30\n")
    hook.chmod(0o755)
    monkeypatch.setattr(gitops, "GIT_TIMEOUT_SEC", 0.5)
    write(worktree / "a.txt")
    started = time.monotonic()
    with pytest.raises(GitError):
        commit(worktree, "Add", paths=["a.txt"], **REDACT)
    assert time.monotonic() - started < 10


def test_module_imports_redact_but_not_config() -> None:
    tree = ast.parse(Path(gitops.__file__).read_text(encoding="utf-8"))
    orch_imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.split(".")[0] == "orch"
    }
    assert orch_imports == {"orch.redact"}
