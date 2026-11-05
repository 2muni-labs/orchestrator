"""TODO.md and orch watch through a real orch process."""

from conftest import TARGET, Sandbox

TASK = "todo-task"


def todo(sandbox: Sandbox) -> str:
    return (sandbox.orch_home / TARGET / "TODO.md").read_text(encoding="utf-8")


def test_todo_follows_the_flow_and_stays_in_the_runtime_directory(
    sandbox: Sandbox,
) -> None:
    worktree = sandbox.ready(TASK)
    assert f"| {TASK} |" in todo(sandbox)
    assert "| ready |" in todo(sandbox)

    data = sandbox.ok("pr", TASK)
    head = sandbox.remote_sha(f"task/{TASK}")
    assert data["head_sha"] == head
    assert sandbox.status(TASK)["head_sha"] == head
    assert "| pr_open |" in todo(sandbox)
    assert f"| {head[:7]} | {data['pr_url']} |" in todo(sandbox)

    for place in (sandbox.clone, worktree, sandbox.hub):
        assert not list(place.rglob("TODO.md")), place
    assert sandbox.git(sandbox.clone, "status", "--porcelain") == ""
    assert sandbox.git(worktree, "status", "--porcelain") == ""

    sandbox.merge_pull_request(data["pr_url"])
    sandbox.ok("close", TASK)
    assert "| closed |" in todo(sandbox)
    assert sandbox.leaks() == []


def test_watch_once_draws_the_board_without_a_token_or_a_state_change(
    sandbox: Sandbox,
) -> None:
    sandbox.ready(TASK)
    calls = len(sandbox.gh_calls())
    before = todo(sandbox)

    result = sandbox.orch("watch", "--once")

    assert result.code == 0, result
    assert f"| {TASK} |" in result.stdout
    assert "| ready |" in result.stdout
    assert len(sandbox.gh_calls()) == calls
    assert sandbox.state(TASK) == "ready"
    assert todo(sandbox) == before
