"""Failures: usage limits, timeouts, scope, discarded dependencies and open PRs."""

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest
from conftest import CHANGED, TARGET, Sandbox

TASK = "fault-task"
INTEGRATION_FAILED = "Integration check failed against the latest main. Do not merge."
# The value under test: the limit itself, for an agent that only has to be stopped.
SHORT_LIMIT_SEC = 2
# Waits not under test: an agent starting and writing a file, a killed process going
# away. These only have to be generous, so a slow machine does not fail them.
AGENT_WAIT_SEC = 10
RUN_BOUND_SEC = 60


def result_file(sandbox: Sandbox, task_id: str = TASK) -> dict:
    path = sandbox.orch_home / TARGET / "results" / f"{task_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def set_timeout(sandbox: Sandbox, task_id: str, seconds: int) -> None:
    """Shorten the limit in the database: a spec's unit is minutes."""
    conn = sqlite3.connect(sandbox.orch_home / "state.db")
    try:
        with conn:
            conn.execute(
                "UPDATE tasks SET timeout_sec = ? WHERE id = ?", (seconds, task_id)
            )
    finally:
        conn.close()


def wait_until_gone(pid: int) -> bool:
    deadline = time.monotonic() + AGENT_WAIT_SEC
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def comments(sandbox: Sandbox, url: str) -> list[str]:
    return sandbox.pull_request(url)["comments"]


def calls_named(sandbox: Sandbox, *command: str) -> list[dict]:
    return [c for c in sandbox.gh_calls() if c["argv"][: len(command)] == list(command)]


def make_conflict(sandbox: Sandbox, url: str) -> None:
    """Change the agent's file on main, so the open pull request conflicts."""
    assert sandbox.pull_request(url)["state"] == "OPEN"
    sandbox.advance_main(CHANGED, "# Changed on main.\n")


def resolve_by_hand(sandbox: Sandbox, worktree: Path) -> str:
    """Do what the main agent does: rebase and resolve the conflict in the worktree."""
    sandbox.git(worktree, "fetch", "-q", "origin")
    done = subprocess.run(
        ["git", "rebase", "origin/main"],
        cwd=worktree,
        env=sandbox.env,
        capture_output=True,
        text=True,
    )
    assert done.returncode != 0, "The rebase was expected to conflict"
    sandbox.write(worktree / CHANGED, "# Resolved.\n")
    sandbox.git(worktree, "add", CHANGED)
    subprocess.run(
        ["git", "rebase", "--continue"],
        cwd=worktree,
        env={**sandbox.env, "GIT_EDITOR": "true"},
        check=True,
        capture_output=True,
    )
    return sandbox.head(worktree)


# Usage limit and resume


@pytest.mark.parametrize("keep", [False, True], ids=["reset", "keep"])
def test_a_usage_limit_blocks_and_resume_keeps_the_attempt(
    sandbox: Sandbox, keep: bool
) -> None:
    worktree = sandbox.reserve(TASK)
    test_sha = sandbox.head(worktree)

    result = sandbox.orch("run", TASK, env={"ORCH_FAKE_SCENARIO": "usage_limit"})
    assert result.code == 40, result
    assert result.data["status"] == "BLOCKED_QUOTA"
    assert result.data["failure_reason"] is None
    assert result.data["state"] == "blocked"
    assert sandbox.state(TASK) == "blocked"
    assert sandbox.status(TASK)["attempt"] == 1
    assert result_file(sandbox)["status"] == "BLOCKED_QUOTA"

    # A blocked task cannot retry or go through the gates.
    assert sandbox.orch("retry", TASK).code == 64
    assert sandbox.orch("gate", TASK).code == 64
    assert sandbox.state(TASK) == "blocked"

    # Leave an untracked file, to tell a reset from --keep.
    leftover = worktree / "src" / "leftover.py"
    sandbox.write(leftover, "# Left by the stopped run.\n")
    args = ("resume", TASK, "--keep") if keep else ("resume", TASK)
    assert sandbox.ok(*args)["state"] == "reserved"
    assert leftover.exists() is keep
    assert sandbox.head(worktree) == test_sha
    assert sandbox.status(TASK)["attempt"] == 1

    # The next run succeeds, still on attempt 1, in a log of its own.
    assert sandbox.ok("run", TASK)["status"] == "DONE"
    assert sandbox.status(TASK)["attempt"] == 1
    runs = sandbox.query(
        "SELECT attempt, outcome FROM runs WHERE task_id = ? ORDER BY run_id", TASK
    )
    assert [(run["attempt"], run["outcome"]) for run in runs] == [
        (1, "BLOCKED_QUOTA"),
        (1, "DONE"),
    ]
    logs = sandbox.orch_home / TARGET / "logs"
    first_log = (logs / f"{TASK}.1.log").read_text(encoding="utf-8")
    assert "usage limit" in first_log
    assert (logs / f"{TASK}.2.log").exists()


def test_a_limit_message_beside_a_valid_report_is_not_a_block(
    sandbox: Sandbox,
) -> None:
    sandbox.reserve(TASK)
    data = sandbox.ok(
        "run", TASK, env={"ORCH_FAKE_SCENARIO": "report_with_limit_message"}
    )
    assert (data["status"], data["state"]) == ("DONE", "judging")


def test_resume_needs_a_blocked_task(sandbox: Sandbox) -> None:
    sandbox.reserve(TASK)
    result = sandbox.orch("resume", TASK)
    assert result.code == 64 and result.data["error"] == "refused"
    assert sandbox.state(TASK) == "reserved"


# Timeout


@pytest.mark.parametrize(
    ("scenario", "limit"),
    [("timeout", SHORT_LIMIT_SEC), ("report_then_hang", AGENT_WAIT_SEC)],
    ids=["no-report", "report-before-the-limit"],
)
def test_a_timeout_stops_the_agent_and_rejects_its_report(
    sandbox: Sandbox, scenario: str, limit: int
) -> None:
    # The second scenario needs its report written before the limit, so the limit must
    # also cover the agent's start.
    worktree = sandbox.reserve(TASK)
    set_timeout(sandbox, TASK, limit)

    started = time.monotonic()
    result = sandbox.orch("run", TASK, env={"ORCH_FAKE_SCENARIO": scenario})
    assert time.monotonic() - started < RUN_BOUND_SEC
    assert result.code == 20, result
    assert (result.data["status"], result.data["failure_reason"]) == (
        "FAILED",
        "timeout",
    )
    assert result.data["state"] == "judging"

    report = result_file(sandbox)
    assert (report["status"], report["failure_reason"]) == ("FAILED", "timeout")
    assert not (worktree / ".orch-result.json").exists()
    rejected = sandbox.orch_home / TARGET / "logs" / f"{TASK}.rejected-result.json"
    assert rejected.exists() is (scenario == "report_then_hang")

    run = sandbox.query("SELECT pid, outcome FROM runs WHERE task_id = ?", TASK)[0]
    assert run["outcome"] == "FAILED"
    assert wait_until_gone(run["pid"])

    # A timeout is a failure with evidence: the gate refuses, retry is open.
    assert sandbox.orch("gate", TASK).code == 64
    assert sandbox.ok("retry", TASK)["state"] == "reserved"
    assert sandbox.status(TASK)["attempt"] == 2


def test_a_timeout_also_stops_a_child_in_another_session(sandbox: Sandbox) -> None:
    # The limit must cover the agent's start, since the agent writes the child's pid.
    sandbox.reserve(TASK)
    set_timeout(sandbox, TASK, AGENT_WAIT_SEC)
    pid_file = sandbox.root / "child-pid"
    result = sandbox.orch(
        "run",
        TASK,
        env={
            "ORCH_FAKE_SCENARIO": "detached_child",
            "ORCH_FAKE_PID_FILE": str(pid_file),
        },
    )
    assert result.data["failure_reason"] == "timeout", result
    child = int(pid_file.read_text(encoding="utf-8"))
    assert wait_until_gone(child)


# Out of scope


@pytest.mark.parametrize(
    ("scenario", "path"),
    [
        ("out_of_scope", "outside/fake_change.py"),
        ("test_file_change", "tests/test_fake_change.py"),
    ],
)
def test_a_change_out_of_scope_fails_the_run_and_records_the_path(
    sandbox: Sandbox, scenario: str, path: str
) -> None:
    worktree = sandbox.reserve(TASK)
    test_sha = sandbox.head(worktree)

    result = sandbox.orch("run", TASK, env={"ORCH_FAKE_SCENARIO": scenario})
    assert result.code == 20, result
    assert (result.data["status"], result.data["failure_reason"]) == (
        "FAILED",
        "out_of_scope",
    )
    assert result.data["state"] == "judging"
    assert result_file(sandbox)["out_of_scope_changes"] == [path]

    # Nothing is committed or pushed, and a retry removes the stray file.
    assert sandbox.orch("gate", TASK).code == 64
    assert sandbox.head(worktree) == test_sha
    assert sandbox.remote_sha(f"task/{TASK}") is None
    assert (worktree / path).exists()
    assert sandbox.ok("retry", TASK)["state"] == "reserved"
    assert not (worktree / path).exists()


# Blocked by a discarded dependency


def test_a_discarded_dependency_blocks_the_chain_and_resume_keeps_it_blocked(
    sandbox: Sandbox,
) -> None:
    sandbox.reserve(TASK)
    dependent = sandbox.prepare("dep-task", depends_on=[TASK], modify="lib/**")
    sandbox.commit_tests(dependent)
    assert sandbox.ok("dispatch", "dep-task")["waiting_for"] == [f"dep:{TASK}"]
    last = sandbox.prepare("last-task", depends_on=["dep-task"], modify="etc/**")
    sandbox.commit_tests(last)
    assert sandbox.ok("dispatch", "last-task")["waiting_for"] == ["dep:dep-task"]

    # Before the discard, nothing is blocked.
    report = sandbox.ok("next")
    assert (report["newly_blocked"], report["blocked_chains"]) == ([], [])

    assert sandbox.ok("discard", TASK)["state"] == "discarded"
    report = sandbox.ok("next")
    assert report["newly_blocked"] == ["dep-task"]
    assert report["reserved"] == []
    assert sandbox.state("dep-task") == "blocked"
    assert sandbox.state("last-task") == "queued"

    # status shows the chain, in JSON and for a person.
    chains = {
        (item["task_id"], item["state"], tuple(item["causes"]))
        for item in sandbox.ok("status")["blocked_chains"]
    }
    assert chains == {
        ("dep-task", "blocked", (TASK,)),
        ("last-task", "queued", ("dep-task",)),
    }
    human = sandbox.orch("status", "--human")
    assert human.code == 0
    lines = human.stdout.splitlines()
    assert f"dep-task blocked: {TASK}" in lines
    assert "last-task queued: dep-task" in lines
    assert f"{TASK} discarded" in lines

    # resume cannot get past the discarded task, and the worktree is left alone.
    marker = dependent / "lib" / "work.py"
    sandbox.write(marker, "# Work in progress.\n")
    data = sandbox.ok("resume", "dep-task")
    assert (data["state"], data["waiting_for"]) == ("blocked", [f"dep:{TASK}"])
    assert sandbox.state("dep-task") == "blocked"
    assert marker.exists()
    assert sandbox.status("dep-task")["attempt"] == 0


# An open pull request when main moves


def test_main_moving_without_a_conflict_pushes_nothing(sandbox: Sandbox) -> None:
    worktree, url = sandbox.open_pull_request(TASK)
    pushed = sandbox.head(worktree)
    sandbox.advance_main("docs/other.md", "other\n")

    data = sandbox.ok("update", TASK)
    assert data["state"] == "pr_open"
    assert data["gates"]["passed"] is True
    assert sandbox.remote_sha(f"task/{TASK}") == pushed
    assert sandbox.head(worktree) == pushed
    assert not (worktree / "docs" / "other.md").exists()
    assert sandbox.git(worktree, "status", "--porcelain") == ""
    assert comments(sandbox, url) == []
    assert sandbox.state(TASK) == "pr_open"


def test_main_breaking_the_tests_warns_on_the_pull_request(sandbox: Sandbox) -> None:
    worktree, url = sandbox.open_pull_request(TASK)
    pushed = sandbox.head(worktree)
    sandbox.advance_main("tests/base.sh", "exit 1\n")

    result = sandbox.orch("update", TASK)
    assert result.code == 20, result
    assert result.data["state"] == "pr_open"
    assert result.data["gates"]["failed_gate"] == "G2"
    assert comments(sandbox, url) == [INTEGRATION_FAILED]
    assert sandbox.remote_sha(f"task/{TASK}") == pushed
    assert sandbox.head(worktree) == pushed
    assert sandbox.git(worktree, "status", "--porcelain") == ""


def test_a_conflict_is_resolved_and_replaced_with_a_lease_push(
    sandbox: Sandbox,
) -> None:
    worktree, url = sandbox.open_pull_request(TASK)
    pushed = sandbox.head(worktree)
    make_conflict(sandbox, url)

    # update finds the conflict, restores the worktree and pushes nothing.
    result = sandbox.orch("update", TASK)
    assert result.code == 20, result
    assert result.data["state"] == "conflict"
    assert result.data["conflicts"] == [CHANGED]
    assert sandbox.state(TASK) == "conflict"
    assert sandbox.head(worktree) == pushed
    assert sandbox.remote_sha(f"task/{TASK}") == pushed
    assert comments(sandbox, url) == []

    # While it conflicts, a pr without the resolution reports the conflict again.
    again = sandbox.orch("pr", TASK)
    assert again.code == 20, again
    assert again.data["state"] == "conflict"
    assert again.data["conflicts"] == [CHANGED]
    assert sandbox.remote_sha(f"task/{TASK}") == pushed

    # After a manual resolution, pr replaces the branch and explains in a comment.
    resolved = resolve_by_hand(sandbox, worktree)
    assert resolved != pushed
    data = sandbox.ok("pr", TASK, "--resolution", "Kept both changes.")
    assert data["state"] == "pr_open"
    assert data["pr_url"] == url
    assert sandbox.remote_sha(f"task/{TASK}") == resolved
    assert sandbox.task_row(TASK)["pushed_sha"] == resolved
    assert comments(sandbox, url) == [f"Kept both changes.\n\nCommit: `{resolved}`"]
    assert len(calls_named(sandbox, "pr", "create")) == 1
    assert sandbox.state(TASK) == "pr_open"

    # The new base holds: the branch sits on main, and the merge works.
    main = sandbox.remote_sha("main")
    assert sandbox.task_row(TASK)["base_sha"] == main
    assert sandbox.git(worktree, "merge-base", "origin/main", "HEAD") == main
    sandbox.merge_pull_request(url)
    assert sandbox.ok("close", TASK)["state"] == "closed"


def test_a_lease_push_is_refused_when_the_remote_branch_moved(
    sandbox: Sandbox,
) -> None:
    worktree, url = sandbox.open_pull_request(TASK)
    pushed = sandbox.head(worktree)
    make_conflict(sandbox, url)
    assert sandbox.orch("update", TASK).data["state"] == "conflict"
    resolved = resolve_by_hand(sandbox, worktree)

    # Someone else pushes to the task branch after orch's last push.
    other = sandbox.root / "other-clone"
    sandbox.git(sandbox.root, "clone", "-q", str(sandbox.bare), str(other))
    sandbox.git(other, "checkout", "-q", f"task/{TASK}")
    sandbox.write(other / "src" / "theirs.py", "# Theirs.\n")
    sandbox.git(other, "add", ".")
    sandbox.git(other, "commit", "-qm", "Their change")
    sandbox.git(other, "push", "-q", "origin", f"task/{TASK}")
    theirs = sandbox.head(other)
    assert theirs != pushed

    result = sandbox.orch("pr", TASK, "--resolution", "Should not land.")
    assert result.code == 64, result
    assert result.data["ok"] is False
    assert result.data["error"] == "refused"
    assert f"task/{TASK}" in result.data["message"]
    assert sandbox.remote_sha(f"task/{TASK}") == theirs
    assert sandbox.task_row(TASK)["pushed_sha"] == pushed
    assert sandbox.head(worktree) == resolved
    assert comments(sandbox, url) == []
    assert sandbox.state(TASK) == "conflict"


def test_a_plain_push_is_never_a_force_push(sandbox: Sandbox) -> None:
    # A branch already on the remote with other commits, and no lease to rely on.
    worktree = sandbox.ready(TASK)
    other = sandbox.root / "other-clone"
    sandbox.git(sandbox.root, "clone", "-q", str(sandbox.bare), str(other))
    sandbox.git(other, "checkout", "-qb", f"task/{TASK}")
    sandbox.write(other / "src" / "theirs.py", "# Theirs.\n")
    sandbox.git(other, "add", ".")
    sandbox.git(other, "commit", "-qm", "Their change")
    sandbox.git(other, "push", "-q", "origin", f"task/{TASK}")
    theirs = sandbox.head(other)

    result = sandbox.orch("pr", TASK)
    assert result.code != 0, result
    assert sandbox.remote_sha(f"task/{TASK}") == theirs
    assert sandbox.head(worktree) != theirs
    assert calls_named(sandbox, "pr", "create") == []
