"""The fake reviewer's BLOCK paths: retry, discard on the test side, and triage."""

import json
from pathlib import Path

import pytest
from conftest import CHANGED, GITHUB, LABEL, TARGET, TEST_SCRIPT, Sandbox

TASK = "block-task"
FOLLOW_UP = "follow-task"
TITLE = "Add the fake change"
DEFECT = [("P1", CHANGED)]
TEST_FINDING = [("P1", TEST_SCRIPT)]
WRONG = "The finding misreads the spec."


def triage_text(*decisions: tuple[str, str, str], follow_up: str | None = None) -> str:
    """Build a triage file from (severity, path, verdict) decisions."""
    lines = ["+++"]
    if follow_up is not None:
        lines.append(f'follow_up = "{follow_up}"')
    for severity, path, verdict in decisions:
        lines.extend(
            [
                "[[decisions]]",
                f'severity = "{severity}"',
                f'path = "{path}"',
                f'verdict = "{verdict}"',
                f'decision = "{WRONG}"',
            ]
        )
    lines.extend(["+++", "Checked against the spec and the code.", ""])
    return "\n".join(lines)


def triage_file(sandbox: Sandbox, text: str) -> str:
    path = sandbox.drafts / "triage.md"
    sandbox.write(path, text)
    return str(path)


def review_path(sandbox: Sandbox, task_id: str = TASK) -> Path:
    return sandbox.orch_home / TARGET / "reviews" / f"{task_id}.json"


def triage_path(sandbox: Sandbox, task_id: str = TASK) -> Path:
    return sandbox.orch_home / TARGET / "reviews" / f"{task_id}.triage.md"


def assert_refused(result: object, word: str | None = None) -> None:
    assert result.code == 64, result
    assert result.data["ok"] is False and result.data["error"] == "refused", result
    if word is not None:
        assert word in result.data["message"], result


def create_calls(sandbox: Sandbox) -> list[dict]:
    return [c for c in sandbox.gh_calls() if c["argv"][:2] == ["pr", "create"]]


# Retry


def test_a_blocked_implementation_retries_and_reaches_a_pull_request(
    sandbox: Sandbox,
) -> None:
    worktree = sandbox.reviewing(TASK)
    test_sha = sandbox.task_row(TASK)["test_sha"]
    assert sandbox.head(worktree) != test_sha

    # BLOCK without a triage goes back to judging, and the review is kept.
    result = sandbox.save_review(TASK, DEFECT)
    assert (result.code, result.data["state"]) == (0, "judging")
    assert sandbox.state(TASK) == "judging"
    assert json.loads(review_path(sandbox).read_text())["verdict"] == "BLOCK"

    # A standing BLOCK stops the gate and the pull request.
    assert_refused(sandbox.orch("gate", TASK), "Blocking review")
    assert_refused(sandbox.orch("pr", TASK), "ready or conflict")
    assert sandbox.remote_sha(f"task/{TASK}") is None
    assert sandbox.gh_calls() == []

    # retry: back to the test commit, the finding in the spec, the review archived.
    data = sandbox.ok("retry", TASK)
    assert data["state"] == "reserved"
    assert sandbox.status(TASK)["attempt"] == 2
    assert sandbox.head(worktree) == test_sha
    assert not (worktree / CHANGED).exists()
    spec = (sandbox.orch_home / TARGET / "tasks" / f"{TASK}.md").read_text()
    assert "# Previous failure (attempt 1)" in spec
    assert f"P1 {CHANGED}: 가짜 리뷰 지적" in spec
    archived = sandbox.orch_home / TARGET / "reviews" / "attempts" / f"{TASK}.1.json"
    assert json.loads(archived.read_text())["verdict"] == "BLOCK"
    assert not review_path(sandbox).exists()

    # The second attempt passes and opens a plain pull request.
    assert sandbox.ok("run", TASK)["status"] == "DONE"
    assert sandbox.ok("gate", TASK)["state"] == "reviewing"
    second = sandbox.save_review(TASK)
    assert (second.code, second.data["state"]) == (0, "ready")
    data = sandbox.ok("pr", TASK)
    assert data["state"] == "pr_open"
    # A retry can recreate the first commit exactly: the same parent, tree, message and
    # second give the same id. So check what it guarantees, not a different id.
    head = sandbox.head(worktree)
    assert sandbox.remote_sha(f"task/{TASK}") == head
    assert sandbox.task_row(TASK)["pushed_sha"] == head
    assert sandbox.git(worktree, "rev-parse", "HEAD^") == test_sha
    assert sandbox.git(worktree, "show", "--name-only", "--format=", "HEAD") == CHANGED
    assert sandbox.status(TASK)["attempt"] == 2
    runs = sandbox.query(
        "SELECT attempt, outcome FROM runs WHERE task_id = ? ORDER BY run_id", TASK
    )
    assert [(run["attempt"], run["outcome"]) for run in runs] == [
        (1, "DONE"),
        (2, "DONE"),
    ]
    assert json.loads(archived.read_text())["verdict"] == "BLOCK"
    assert json.loads(review_path(sandbox).read_text())["verdict"] == "PASS"
    pull_request = sandbox.pull_request(data["pr_url"])
    assert pull_request["labels"] == []
    assert "overridden" not in pull_request["body"]
    assert "Verdict: PASS" in pull_request["body"]


def test_a_second_block_refuses_retry_and_ends_in_discard(sandbox: Sandbox) -> None:
    worktree = sandbox.reviewing(TASK)
    assert sandbox.save_review(TASK, DEFECT).code == 0
    assert sandbox.ok("retry", TASK)["state"] == "reserved"
    assert sandbox.ok("run", TASK)["status"] == "DONE"
    assert sandbox.ok("gate", TASK)["state"] == "reviewing"
    assert sandbox.save_review(TASK, DEFECT).data["state"] == "judging"

    assert_refused(sandbox.orch("retry", TASK), "attempt 1")
    assert sandbox.state(TASK) == "judging"
    assert sandbox.status(TASK)["attempt"] == 2

    assert sandbox.ok("discard", TASK)["state"] == "discarded"
    assert not worktree.exists()
    assert not sandbox.local_branch_exists(f"task/{TASK}")
    assert sandbox.remote_sha(f"task/{TASK}") is None
    assert sandbox.gh_calls() == []


# A confirmed finding on the test side: discard


def test_a_discard_on_the_test_side_blocks_dependents_and_frees_locks(
    sandbox: Sandbox,
) -> None:
    sandbox.reviewing(TASK)

    # One task waits on the discarded task's result, another on its lock.
    dependent = sandbox.prepare("dependent-task", depends_on=[TASK], modify="lib/**")
    sandbox.commit_tests(dependent)
    waiting = sandbox.ok("dispatch", "dependent-task")
    assert (waiting["state"], waiting["waiting_for"]) == ("queued", [f"dep:{TASK}"])
    locked = sandbox.prepare("locked-task")
    sandbox.commit_tests(locked)
    waiting = sandbox.ok("dispatch", "locked-task")
    assert (waiting["state"], waiting["waiting_for"]) == ("queued", [f"lock:{TASK}"])
    assert sandbox.ok("next")["reserved"] == []

    result = sandbox.save_review(TASK, TEST_FINDING)
    assert result.data["state"] == "judging"
    assert sandbox.ok("discard", TASK)["state"] == "discarded"

    report = sandbox.ok("next")
    assert report["reserved"] == ["locked-task"]
    assert report["newly_blocked"] == ["dependent-task"]
    assert report["blocked_chains"] == [
        {"task_id": "dependent-task", "state": "blocked", "causes": [TASK]}
    ]
    assert sandbox.state("dependent-task") == "blocked"
    assert sandbox.state("locked-task") == "reserved"
    assert sandbox.gh_calls() == []


# Triage


def test_a_wrong_finding_overrides_the_block_with_the_label_and_section(
    sandbox: Sandbox,
) -> None:
    worktree = sandbox.reviewing(TASK)
    triage = triage_file(sandbox, triage_text(("P1", CHANGED, "wrong_finding")))
    result = sandbox.orch_review(TASK, DEFECT + [("P3", "src/x.py")], triage)
    assert (result.code, result.data["state"]) == (0, "ready")
    assert triage_path(sandbox).read_text() == triage_text(
        ("P1", CHANGED, "wrong_finding")
    )

    data = sandbox.ok("pr", TASK)
    assert data["state"] == "pr_open"
    assert sandbox.remote_sha(f"task/{TASK}") == sandbox.head(worktree)

    # The label was checked first, then given on create, in gh's real call format.
    calls = sandbox.gh_calls()
    assert calls[0]["argv"] == [
        "label", "list", "--repo", GITHUB, "--search", LABEL, "--json", "name",
        "--limit", "100",
    ]  # fmt: skip
    assert [call["refused"] for call in calls] == [None, None]
    assert calls[1]["argv"] == [
        "pr", "create", "--repo", GITHUB, "--head", f"task/{TASK}", "--base", "main",
        "--title", TITLE, "--label", LABEL, "--body-file", "-",
    ]  # fmt: skip

    pull_request = sandbox.pull_request(data["pr_url"])
    assert pull_request["labels"] == [LABEL]
    body = pull_request["body"]
    assert body.startswith("## Review BLOCK overridden\n")
    assert f"- P1 `{CHANGED}` (wrong_finding): {WRONG}" in body
    assert "Checked against the spec and the code." in body
    assert "Follow-up task" not in body
    assert "Verdict: BLOCK, P1: 1, P3: 1" in body
    assert body.index("## Review BLOCK overridden") < body.index("## Summary")


def test_insufficient_tests_override_names_the_follow_up(sandbox: Sandbox) -> None:
    sandbox.reviewing(TASK)
    sandbox.prepare(FOLLOW_UP, modify="lib/**")
    triage = triage_file(
        sandbox,
        triage_text(("P1", TEST_SCRIPT, "insufficient_tests"), follow_up=FOLLOW_UP),
    )
    result = sandbox.orch_review(TASK, TEST_FINDING, triage)
    assert (result.code, result.data["state"]) == (0, "ready")

    data = sandbox.ok("pr", TASK)
    pull_request = sandbox.pull_request(data["pr_url"])
    assert pull_request["labels"] == [LABEL]
    assert pull_request["body"].startswith("## Review BLOCK overridden\n")
    assert f"Follow-up task: `{FOLLOW_UP}`" in pull_request["body"]
    assert f"- P1 `{TEST_SCRIPT}` (insufficient_tests): {WRONG}" in pull_request["body"]


EXACT = "Triage must match every P0 and P1 finding exactly"


@pytest.mark.parametrize(
    ("findings", "text", "reason"),
    [
        (
            [("P1", CHANGED), ("P1", "src/other.py")],
            triage_text(("P1", CHANGED, "wrong_finding")),
            EXACT,
        ),
        ([("P1", CHANGED)], triage_text(("P0", CHANGED, "wrong_finding")), EXACT),
        (
            [("P1", CHANGED)],
            triage_text(("P1", CHANGED, "insufficient_tests"), follow_up=FOLLOW_UP),
            "Insufficient tests must name a tests/ path",
        ),
        (
            [("P1", TEST_SCRIPT)],
            triage_text(("P1", TEST_SCRIPT, "insufficient_tests")),
            "Insufficient tests need a valid follow-up",
        ),
        (
            [("P1", TEST_SCRIPT)],
            triage_text(
                ("P1", TEST_SCRIPT, "insufficient_tests"), follow_up="not-registered"
            ),
            "Invalid follow-up task",
        ),
        (
            [("P1", TEST_SCRIPT)],
            triage_text(("P1", TEST_SCRIPT, "insufficient_tests"), follow_up=TASK),
            "Invalid follow-up task",
        ),
        (
            [("P1", CHANGED)],
            triage_text(("P1", CHANGED, "wrong_finding"), follow_up=FOLLOW_UP),
            "Follow-up requires an insufficient_tests decision",
        ),
        (
            [],
            triage_text(("P1", CHANGED, "wrong_finding")),
            "Triage requires a BLOCK review",
        ),
    ],
    ids=[
        "unhandled-p1",
        "wrong-severity",
        "insufficient-outside-tests",
        "no-follow-up",
        "unregistered-follow-up",
        "self-follow-up",
        "follow-up-without-insufficient-tests",
        "triage-on-a-pass",
    ],
)
def test_a_bad_triage_is_refused_and_nothing_is_saved(
    sandbox: Sandbox, findings: list[tuple[str, str]], text: str, reason: str
) -> None:
    sandbox.reviewing(TASK)
    result = sandbox.orch_review(TASK, findings, triage_file(sandbox, text))
    assert result.code == 64, result
    assert result.data["ok"] is False
    assert reason in result.data["message"], result
    assert sandbox.state(TASK) == "reviewing"
    assert not review_path(sandbox).exists()
    assert not triage_path(sandbox).exists()


def test_a_discarded_follow_up_is_refused(sandbox: Sandbox) -> None:
    sandbox.reviewing(TASK)
    sandbox.prepare(FOLLOW_UP, modify="lib/**")
    assert sandbox.ok("discard", FOLLOW_UP)["state"] == "discarded"
    text = triage_text(("P1", TEST_SCRIPT, "insufficient_tests"), follow_up=FOLLOW_UP)
    result = sandbox.orch_review(TASK, TEST_FINDING, triage_file(sandbox, text))
    assert result.code == 64, result
    assert "Invalid follow-up task" in result.data["message"], result
    assert sandbox.state(TASK) == "reviewing"
    assert not review_path(sandbox).exists()


def test_a_personal_path_in_a_triage_is_refused(sandbox: Sandbox) -> None:
    sandbox.reviewing(TASK)
    text = triage_text(("P1", CHANGED, "wrong_finding")).replace(
        WRONG, f"See {sandbox.home}/notes for why."
    )
    result = sandbox.orch_review(TASK, DEFECT, triage_file(sandbox, text))
    assert result.code == 64, result
    assert "personal path" in result.data["message"]
    assert sandbox.state(TASK) == "reviewing"
    assert not review_path(sandbox).exists()


def test_a_missing_label_stops_before_any_push(sandbox: Sandbox) -> None:
    worktree = sandbox.reviewing(TASK)
    head = sandbox.head(worktree)
    triage = triage_file(sandbox, triage_text(("P1", CHANGED, "wrong_finding")))
    assert sandbox.orch_review(TASK, DEFECT, triage).data["state"] == "ready"

    state = sandbox.gh_state()
    state["labels"] = []
    sandbox.write_gh_state(state)
    assert_refused(sandbox.orch("pr", TASK), LABEL)
    assert sandbox.state(TASK) == "ready"
    assert sandbox.remote_sha(f"task/{TASK}") is None
    assert create_calls(sandbox) == []
    assert sandbox.head(worktree) == head
    assert sandbox.gh_state()["labels"] == []  # orch never creates the label

    # With the label back, the same task goes through.
    state["labels"] = [LABEL]
    sandbox.write_gh_state(state)
    assert sandbox.ok("pr", TASK)["state"] == "pr_open"


def test_a_triage_that_went_missing_stops_the_pull_request(sandbox: Sandbox) -> None:
    sandbox.reviewing(TASK)
    triage = triage_file(sandbox, triage_text(("P1", CHANGED, "wrong_finding")))
    assert sandbox.orch_review(TASK, DEFECT, triage).data["state"] == "ready"
    triage_path(sandbox).unlink()
    assert_refused(sandbox.orch("pr", TASK), "triage")
    assert sandbox.remote_sha(f"task/{TASK}") is None
    assert sandbox.gh_calls() == []


def test_minor_findings_do_not_block(sandbox: Sandbox) -> None:
    sandbox.reviewing(TASK)
    result = sandbox.save_review(TASK, [("P2", CHANGED), ("P3", CHANGED)])
    assert (result.code, result.data["state"]) == (0, "ready")
    data = sandbox.ok("pr", TASK)
    pull_request = sandbox.pull_request(data["pr_url"])
    assert pull_request["labels"] == []
    assert "Verdict: PASS, P2: 1, P3: 1" in pull_request["body"]


# The environment of each call


def test_an_orch_call_with_a_token_in_its_environment_never_runs(
    sandbox: Sandbox,
) -> None:
    with pytest.raises(pytest.fail.Exception, match="GH_TOKEN is set"):
        sandbox.orch("status", env={"GH_TOKEN": "real-token"})
    with pytest.raises(pytest.fail.Exception, match="GIT_CONFIG_NOSYSTEM"):
        sandbox.orch("status", unset=("GIT_CONFIG_NOSYSTEM",))
    with pytest.raises(pytest.fail.Exception, match="not the fake gh"):
        sandbox.orch("status", env={"PATH": "/usr/bin:/bin"})
