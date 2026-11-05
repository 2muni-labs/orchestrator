"""orch wait --merged through a real orch process and the fake gh.

The fake gh applies view_states in order, one for each pr view. The state changes at
an exact check, so these tests have no race with the polling process. The wait limit
is in minutes, so its path is tested in test_waiter.py and test_cli.py instead of
spending a minute here.
"""

from conftest import Sandbox

TASK = "wait-task"
MERGED = {"state": "MERGED", "mergedAt": "2026-01-01T00:00:00Z"}
CLOSED = {"state": "CLOSED"}


def views(sandbox: Sandbox) -> list[dict]:
    return [call for call in sandbox.gh_calls() if call["argv"][:2] == ["pr", "view"]]


def test_wait_ends_on_the_merge_and_close_follows(sandbox: Sandbox) -> None:
    _, url = sandbox.open_pull_request(TASK)
    sandbox.set_pull_request(url, view_states=[{}, MERGED])
    before = len(views(sandbox))

    result = sandbox.orch("wait", TASK, "--merged", "--interval", "1")

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
    }
    assert len(views(sandbox)) - before == 2
    assert sandbox.state(TASK) == "pr_open"
    assert sandbox.ok("close", TASK)["state"] == "closed"


def test_wait_ends_on_a_close_without_a_merge_and_discard_follows(
    sandbox: Sandbox,
) -> None:
    _, url = sandbox.open_pull_request(TASK)
    sandbox.set_pull_request(url, view_states=[{}, CLOSED])

    result = sandbox.orch("wait", TASK, "--merged", "--interval", "1")

    assert result.code == 50, result
    assert result.data["ok"] is True
    assert result.data["pr_state"] == "CLOSED"
    assert result.data["merged"] is False
    assert sandbox.state(TASK) == "pr_open"
    assert sandbox.ok("discard", TASK)["state"] == "discarded"


def test_wait_does_not_need_the_hub(sandbox: Sandbox) -> None:
    _, url = sandbox.open_pull_request(TASK)
    sandbox.set_pull_request(url, view_states=[MERGED])

    result = sandbox.orch("wait", TASK, "--merged", unset=("ORCH_HUB",))

    assert result.code == 0, result


def test_wait_refuses_a_task_without_a_pull_request(sandbox: Sandbox) -> None:
    sandbox.prepare(TASK)

    result = sandbox.orch("wait", TASK, "--merged")

    assert result.code == 64, result
    assert result.data["error"] == "refused"
    assert views(sandbox) == []
    assert sandbox.state(TASK) == "prepared"


def test_wait_needs_the_merged_flag(sandbox: Sandbox) -> None:
    sandbox.prepare(TASK)

    result = sandbox.orch("wait", TASK)

    assert result.code == 64, result
    assert result.data["error"] == "usage"
