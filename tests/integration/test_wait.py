"""orch wait --merged through a real orch process and the fake gh.

The fake gh applies review_states in order, one for each review query. The state
changes at an exact check, so these tests have no race with the polling process. The
wait limit is in minutes, so its path is tested in test_waiter.py and test_cli.py
instead of spending a minute here.
"""

from conftest import Sandbox

TASK = "wait-task"
MERGED = {"state": "MERGED", "mergedAt": "2026-01-01T00:00:00Z"}
CLOSED = {"state": "CLOSED"}


def calls(sandbox: Sandbox, *prefix: str) -> list[dict]:
    return [c for c in sandbox.gh_calls() if c["argv"][: len(prefix)] == list(prefix)]


def label_edits(sandbox: Sandbox) -> list[list[str]]:
    return [call["argv"][3:] for call in calls(sandbox, "pr", "edit")]


def wait(sandbox: Sandbox, **options: object) -> object:
    return sandbox.orch("wait", TASK, "--merged", "--interval", "1", **options)


def test_wait_labels_the_passing_head_and_ends_on_the_merge(sandbox: Sandbox) -> None:
    _, url = sandbox.open_pull_request(TASK)
    sandbox.set_pull_request(url, review_states=[{}, MERGED])

    result = wait(sandbox)

    assert result.code == 0, result
    assert result.data == {
        "ok": True,
        "task_id": TASK,
        "state": "pr_open",
        "pr_url": url,
        "pr_state": "MERGED",
        "merged": True,
        "merged_at": MERGED["mergedAt"],
        "checks": 2,
        "outcome": "merged",
        "review_label": "verify-deep",
        "failed_checks": [],
        "notified": [],
    }
    assert label_edits(sandbox) == [["--add-label", "verify-deep"]]
    assert all(call["refused"] is None for call in sandbox.gh_calls())
    assert calls(sandbox, "pr", "view") == []
    assert sandbox.state(TASK) == "pr_open"
    assert sandbox.ok("close", TASK)["state"] == "closed"


def test_wait_ends_on_a_failed_required_check(sandbox: Sandbox) -> None:
    _, url = sandbox.open_pull_request(TASK)
    sandbox.set_pull_request(url, checks={"ci": ["COMPLETED", "FAILURE"]})

    result = wait(sandbox)

    assert result.code == 61, result
    assert result.data["outcome"] == "checks_failed"
    assert result.data["failed_checks"] == ["ci"]
    assert label_edits(sandbox) == []


def test_wait_keeps_waiting_while_a_check_runs(sandbox: Sandbox) -> None:
    _, url = sandbox.open_pull_request(TASK)
    running = {"checks": {"ci": ["IN_PROGRESS", None]}}
    passed = {"checks": {"ci": ["COMPLETED", "SUCCESS"]}}
    sandbox.set_pull_request(url, review_states=[running, passed, MERGED])

    result = wait(sandbox)

    assert result.code == 0, result
    assert result.data["checks"] == 3
    assert label_edits(sandbox) == [["--add-label", "verify-deep"]]


def test_wait_ends_on_the_first_changes_requested(sandbox: Sandbox) -> None:
    _, url = sandbox.open_pull_request(TASK)
    event = {"name": "changes-requested", "added": True, "at": "2026-01-01T00:00:00Z"}
    sandbox.set_pull_request(url, labels=["changes-requested"], events=[event])

    result = wait(sandbox)

    assert result.code == 62, result
    assert result.data["outcome"] == "changes_requested"
    assert label_edits(sandbox) == []


def test_a_behind_head_ends_the_wait_and_update_brings_it_up_to_date(
    sandbox: Sandbox,
) -> None:
    worktree, url = sandbox.open_pull_request(TASK)
    sandbox.advance_main("docs/other.md", "other\n")

    behind = wait(sandbox)
    assert behind.code == 63, behind
    assert behind.data["outcome"] == "behind"
    assert label_edits(sandbox) == []

    updated = sandbox.ok("update", TASK)
    assert updated["pushed_sha"] == sandbox.head(worktree)
    sandbox.set_pull_request(url, review_states=[{}, MERGED])

    result = wait(sandbox)
    assert result.code == 0, result
    assert result.data["review_label"] == "verify-deep"
    assert ["--add-label", "verify-deep"] in label_edits(sandbox)


def test_a_rerun_after_the_label_does_not_add_it_again(sandbox: Sandbox) -> None:
    _, url = sandbox.open_pull_request(TASK)
    sandbox.set_pull_request(url, review_states=[{}, MERGED])
    assert wait(sandbox).code == 0
    sandbox.set_pull_request(
        url, state="OPEN", mergedAt=None, review_states=[{}, MERGED]
    )

    result = wait(sandbox)

    assert result.code == 0, result
    assert label_edits(sandbox) == [["--add-label", "verify-deep"]]


def test_wait_ends_on_a_close_without_a_merge_and_discard_follows(
    sandbox: Sandbox,
) -> None:
    _, url = sandbox.open_pull_request(TASK)
    sandbox.set_pull_request(url, review_states=[CLOSED])

    result = wait(sandbox)

    assert result.code == 50, result
    assert result.data["ok"] is True
    assert result.data["outcome"] == "closed"
    assert result.data["pr_state"] == "CLOSED"
    assert result.data["merged"] is False
    assert sandbox.state(TASK) == "pr_open"
    assert sandbox.ok("discard", TASK)["state"] == "discarded"


def test_wait_refuses_a_branch_without_required_checks(sandbox: Sandbox) -> None:
    sandbox.open_pull_request(TASK)
    state = sandbox.gh_state()
    state["rules"] = {"required": [], "strict": True}
    sandbox.write_gh_state(state)

    result = wait(sandbox)

    assert result.code == 64, result
    assert result.data["error"] == "refused"
    assert label_edits(sandbox) == []


def test_wait_needs_the_hub(sandbox: Sandbox) -> None:
    sandbox.open_pull_request(TASK)
    before = len(sandbox.gh_calls())

    result = wait(sandbox, unset=("ORCH_HUB",))

    assert result.code == 64, result
    assert result.data["error"] == "usage"
    assert len(sandbox.gh_calls()) == before


def test_wait_refuses_a_task_without_a_pull_request(sandbox: Sandbox) -> None:
    sandbox.prepare(TASK)

    result = sandbox.orch("wait", TASK, "--merged")

    assert result.code == 64, result
    assert result.data["error"] == "refused"
    assert sandbox.gh_calls() == []
    assert sandbox.state(TASK) == "prepared"


def test_wait_needs_the_merged_flag(sandbox: Sandbox) -> None:
    sandbox.prepare(TASK)

    result = sandbox.orch("wait", TASK)

    assert result.code == 64, result
    assert result.data["error"] == "usage"
