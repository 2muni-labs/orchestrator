"""The normal flow through a real orch process, and what guards the test sandbox."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from conftest import (
    CHANGED,
    COMMIT_DATE,
    FAKE_GH,
    GITHUB,
    LABEL,
    TARGET,
    TEST_SCRIPT,
    TOKEN,
    Sandbox,
    isolation_problems,
    require_isolation,
)

TASK = "flow-task"
TITLE = "Add the fake change"


def problems(sandbox: Sandbox, env: dict[str, str] | None = None) -> list[str]:
    return isolation_problems(
        sandbox.env if env is None else env, [sandbox.clone], sandbox.bare, FAKE_GH
    )


def run_gh(
    sandbox: Sandbox, *args: str, stdin: str = "", token: str = TOKEN
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["gh", *args],
        env={**sandbox.env, "GH_TOKEN": token},
        input=stdin,
        capture_output=True,
        text=True,
    )


# The normal flow


def test_a_task_goes_from_target_add_to_close(sandbox: Sandbox) -> None:
    # The sandbox fixture has run target add. The stored path holds no personal path.
    targets = (sandbox.hub / "targets.toml").read_text(encoding="utf-8")
    assert f'path = "~/{TARGET}"' in targets
    assert str(sandbox.root) not in targets

    # prepare: a worktree on a new branch at the remote's main.
    main_sha = sandbox.remote_sha("main")
    data = sandbox.ok("prepare", str(sandbox.spec_file(TASK)))
    assert data["state"] == "prepared"
    assert data["base_sha"] == main_sha
    worktree = Path(data["worktree"])
    assert sandbox.git(worktree, "rev-parse", "--abbrev-ref", "HEAD") == f"task/{TASK}"
    assert sandbox.head(worktree) == main_sha
    assert (sandbox.orch_home / TARGET / "tasks" / f"{TASK}.md").is_file()

    # dispatch: refused without a test commit, then records the test commit.
    refused = sandbox.orch("dispatch", TASK)
    assert refused.code == 64 and refused.data["error"] == "refused"
    assert sandbox.state(TASK) == "prepared"
    test_sha = sandbox.commit_tests(worktree)
    assert sandbox.ok("dispatch", TASK)["state"] == "reserved"
    assert sandbox.task_row(TASK)["test_sha"] == test_sha

    # run: the fake agent's change stays uncommitted, and the report is moved out.
    data = sandbox.ok("run", TASK)
    assert (data["status"], data["failure_reason"]) == ("DONE", None)
    assert data["state"] == "judging"
    assert (worktree / CHANGED).is_file()
    assert sandbox.head(worktree) == test_sha
    assert not (worktree / ".orch-result.json").exists()
    report = sandbox.orch_home / TARGET / "results" / f"{TASK}.json"
    assert json.loads(report.read_text(encoding="utf-8"))["status"] == "DONE"
    assert sandbox.status(TASK)["attempt"] == 1

    # gate: all three pass and orch commits the change under the task's title.
    data = sandbox.ok("gate", TASK)
    assert data["state"] == "reviewing"
    assert data["gates"] == {"G1": "passed", "G2": "passed", "G3": "passed"}
    assert sandbox.gate_rows(TASK) == {"G1": True, "G2": True, "G3": True}
    assert sandbox.git(worktree, "log", "-1", "--format=%s") == TITLE
    assert sandbox.git(worktree, "rev-parse", "HEAD^") == test_sha
    assert sandbox.git(worktree, "show", "--name-only", "--format=", "HEAD") == CHANGED
    assert sandbox.git(worktree, "status", "--porcelain") == ""
    gated = sandbox.head(worktree)
    assert sandbox.task_row(TASK)["gated_sha"] == gated

    # pr: refused before a review is saved, with nothing pushed or created.
    refused = sandbox.orch("pr", TASK, unchecked=True)
    assert refused.code == 64 and refused.data["error"] == "refused"
    assert sandbox.remote_sha(f"task/{TASK}") is None
    assert sandbox.gh_calls() == []

    # review save: the fake reviewer passes it.
    result = sandbox.save_review(TASK)
    assert (result.code, result.data["state"]) == (0, "ready")
    assert sandbox.state(TASK) == "ready"

    # pr: pushes the branch and creates the pull request with exact arguments.
    data = sandbox.ok("pr", TASK)
    assert data["state"] == "pr_open"
    url = data["pr_url"]
    assert url == f"https://github.com/{GITHUB}/pull/1"
    head = sandbox.head(worktree)
    assert sandbox.remote_sha(f"task/{TASK}") == head
    assert sandbox.task_row(TASK)["pushed_sha"] == head
    assert sandbox.status(TASK)["pr_url"] == url
    assert sandbox.gate_rows(TASK)["G2_PRE_PR"] is True
    # main did not move, so the pre-PR gate is skipped on the gated commit.
    assert head == gated
    pre_pr = sandbox.query(
        "SELECT detail FROM gate_results WHERE task_id = ? AND gate = 'G2_PRE_PR'",
        TASK,
    )
    assert [row["detail"] for row in pre_pr] == ["skipped"]
    creates = [
        call for call in sandbox.gh_calls() if call["argv"][:2] == ["pr", "create"]
    ]
    assert len(creates) == 1
    assert creates[0]["argv"] == [
        "pr",
        "create",
        "--repo",
        GITHUB,
        "--head",
        f"task/{TASK}",
        "--base",
        "main",
        "--title",
        TITLE,
        "--body-file",
        "-",
    ]
    assert creates[0]["refused"] is None
    pull_request = sandbox.pull_request(url)
    assert pull_request["labels"] == []
    body = pull_request["body"]
    assert body.startswith("## Summary")
    assert f"Task `{TASK}`: {TITLE}" in body
    assert f"- `{CHANGED}`" in body and f"- `{TEST_SCRIPT}`" in body
    for gate in ("G1", "G2", "G3", "G2_PRE_PR"):
        assert f"- {gate}: passed" in body
    assert "Verdict: PASS" in body
    assert "overridden" not in body

    # close: refused until the pull request is merged, and nothing is removed.
    refused = sandbox.orch("close", TASK)
    assert refused.code == 64 and refused.data["error"] == "refused"
    assert sandbox.state(TASK) == "pr_open"
    assert worktree.is_dir()
    assert sandbox.local_branch_exists(f"task/{TASK}")

    # close: after the merge, the worktree and local branch go; the remote stays.
    sandbox.merge_pull_request(url)
    assert sandbox.ok("close", TASK)["state"] == "closed"
    assert sandbox.state(TASK) == "closed"
    assert not worktree.exists()
    assert not sandbox.local_branch_exists(f"task/{TASK}")
    assert str(worktree) not in sandbox.git(sandbox.clone, "worktree", "list")
    assert sandbox.remote_sha(f"task/{TASK}") == head
    merged = sandbox.git(
        sandbox.root,
        "--git-dir",
        str(sandbox.bare),
        "ls-tree",
        "-r",
        "--name-only",
        "main",
    )
    assert CHANGED in merged.splitlines()


def test_a_done_report_without_the_work_fails_the_gate(sandbox: Sandbox) -> None:
    # This also shows the flow above passes G2 because of the change, not regardless.
    worktree = sandbox.reserve(TASK)
    test_sha = sandbox.head(worktree)
    data = sandbox.ok(
        "run",
        TASK,
        env={
            "ORCH_FAKE_SCENARIO": "env_dump",
            "ORCH_FAKE_ENV_FILE": str(sandbox.root / "agent-env"),
        },
    )
    assert data["status"] == "DONE"
    result = sandbox.orch("gate", TASK)
    assert result.code == 20
    assert result.data["failed_gate"] == "G2"
    assert result.data["state"] == "judging"
    assert sandbox.gate_rows(TASK) == {"G1": True, "G2": False}
    assert sandbox.head(worktree) == test_sha
    assert sandbox.remote_sha(f"task/{TASK}") is None
    assert sandbox.gh_calls() == []


def test_a_required_review_that_went_missing_stops_the_pull_request(
    sandbox: Sandbox,
) -> None:
    # The state alone refuses a task still in review, so this reaches the review check.
    sandbox.ready(TASK)
    (sandbox.orch_home / TARGET / "reviews" / f"{TASK}.json").unlink()
    result = sandbox.orch("pr", TASK)
    assert result.code == 64 and result.data["error"] == "refused"
    assert "saved review" in result.data["message"]
    assert sandbox.state(TASK) == "ready"
    assert sandbox.remote_sha(f"task/{TASK}") is None
    assert sandbox.gh_calls() == []


def test_a_claude_implementation_is_reviewed_by_codex_and_opened(
    sandbox: Sandbox,
) -> None:
    sandbox.reviewing(TASK, lane="C")

    result = sandbox.orch(
        "review", TASK, "--by", "codex", env={"ORCH_FAKE_SCENARIO": "review_pass"}
    )
    assert result.code == 0, result
    data = result.data
    assert (data["outcome"], data["state"], data["exit_code"]) == ("saved", "ready", 0)
    logs = sandbox.orch_home / TARGET / "logs" / "review"
    assert data["log_path"] == str(logs / f"{TASK}.1.log")
    assert Path(data["log_path"]).is_file()
    assert data["diff_path"] == str(
        sandbox.orch_home / TARGET / "reviews" / f"{TASK}.diff"
    )
    assert CHANGED in Path(data["diff_path"]).read_text(encoding="utf-8")
    saved = json.loads(
        (sandbox.orch_home / TARGET / "reviews" / f"{TASK}.json").read_text(
            encoding="utf-8"
        )
    )
    assert (saved["reviewer"], saved["verdict"]) == ("codex-review", "PASS")
    assert sandbox.state(TASK) == "ready"

    opened = sandbox.ok("pr", TASK)
    assert opened["state"] == "pr_open", opened


# Fake mode needs an explicit, separate ORCH_HOME


@pytest.mark.parametrize("variant", ["unset", "empty", "default", "link"])
def test_fake_agents_are_refused_without_a_separate_home(
    sandbox: Sandbox, variant: str
) -> None:
    worktree = sandbox.reserve(TASK)
    default = sandbox.home / ".orch"
    agent_env = sandbox.root / "agent-env"
    env = {"ORCH_FAKE_SCENARIO": "env_dump", "ORCH_FAKE_ENV_FILE": str(agent_env)}
    unset: tuple[str, ...] = ()
    if variant == "unset":
        unset = ("ORCH_HOME",)
    elif variant == "empty":
        env["ORCH_HOME"] = ""
    elif variant == "default":
        env["ORCH_HOME"] = str(default)
    else:
        link = sandbox.home / "link-to-default"
        link.symlink_to(default)
        env["ORCH_HOME"] = str(link)

    for command in (
        ("status",),
        ("next",),
        ("prepare", str(sandbox.spec_file("other-task"))),
        ("dispatch", TASK),
        ("run", TASK),
    ):
        result = sandbox.orch(*command, env=env, unset=unset)
        assert result.code == 64, (command, result)
        assert result.data["error"] == "usage", (command, result)
        assert "ORCH_HOME" in result.data["message"], (command, result)

    # Nothing was created at the default home and no agent started.
    assert not default.exists()
    assert not agent_env.exists()
    assert sandbox.state(TASK) == "reserved"
    logs = sandbox.orch_home / TARGET / "logs"
    assert not list(logs.glob(f"{TASK}.[0-9]*.log"))
    assert not (worktree / CHANGED).exists()

    # The same run with a separate home does start the agent, so the check above means
    # something.
    assert sandbox.ok("run", TASK, env=env | {"ORCH_HOME": str(sandbox.orch_home)})
    assert agent_env.exists()


# The isolation check itself


def test_the_sandbox_is_isolated_as_built(sandbox: Sandbox) -> None:
    assert problems(sandbox) == []
    assert os.environ.get("HOME") != sandbox.env["HOME"]
    assert sandbox.env["HOME"].startswith(str(sandbox.root))
    assert sandbox.env["ORCH_HOME"].startswith(str(sandbox.root))
    assert sandbox.env["ORCH_HUB"].startswith(str(sandbox.root))


def test_commits_in_the_sandbox_have_the_frozen_date(sandbox: Sandbox) -> None:
    # orch's own commit, through the gate, as well as the sandbox's.
    worktree = sandbox.reviewing(TASK)
    dates = sandbox.git(worktree, "log", "--format=%ad %cd", "--date=raw").split()
    # Raw dates are seconds and zone: 2026-01-01T00:00:00+0000 is 1767225600.
    assert COMMIT_DATE.endswith("+0000")
    assert set(dates) == {"1767225600", "+0000"}
    # The seed commit, the test commit and orch's commit.
    assert len(dates) == 3 * 4


@pytest.mark.parametrize("name", ["GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"])
def test_isolation_requires_the_frozen_commit_date(sandbox: Sandbox, name: str) -> None:
    moved = {**sandbox.env, name: "2026-06-01T00:00:00+0000"}
    assert problems(sandbox, moved) == [f"{name} is not the frozen commit date"]
    missing = {k: v for k, v in sandbox.env.items() if k != name}
    assert problems(sandbox, missing) == [f"{name} is not the frozen commit date"]
    with pytest.raises(pytest.fail.Exception, match="frozen commit date"):
        sandbox.orch("status", unset=(name,))


def test_isolation_requires_git_config_nosystem(sandbox: Sandbox) -> None:
    env = {k: v for k, v in sandbox.env.items() if k != "GIT_CONFIG_NOSYSTEM"}
    assert any("GIT_CONFIG_NOSYSTEM" in item for item in problems(sandbox, env))
    env["GIT_CONFIG_NOSYSTEM"] = "0"
    assert any("GIT_CONFIG_NOSYSTEM" in item for item in problems(sandbox, env))


@pytest.mark.parametrize("name", ["GH_TOKEN", "GITHUB_TOKEN"])
def test_isolation_refuses_a_token_in_the_environment(
    sandbox: Sandbox, name: str
) -> None:
    found = problems(sandbox, {**sandbox.env, name: "real-token"})
    assert found == [f"{name} is set"]


def test_isolation_refuses_a_credential_helper(sandbox: Sandbox) -> None:
    sandbox.git(sandbox.clone, "config", "credential.helper", "osxkeychain")
    assert any("credential.helper" in item for item in problems(sandbox))


def test_isolation_refuses_a_credential_helper_in_global_config(
    sandbox: Sandbox,
) -> None:
    sandbox.git(sandbox.clone, "config", "--global", "credential.helper", "store")
    assert any("credential.helper" in item for item in problems(sandbox))


@pytest.mark.parametrize(
    "commands",
    [
        (("remote", "set-url", "origin", "https://github.com/owner/sample.git"),),
        (("remote", "add", "extra", "https://github.com/owner/sample.git"),),
        (("remote", "set-url", "--push", "origin", "git@github.com:owner/sample.git"),),
        (("config", "url.https://github.com/.insteadOf", "/"),),
        (("remote", "remove", "origin"),),
    ],
    ids=["fetch-url", "second-remote", "push-url", "instead-of", "no-remote"],
)
def test_isolation_refuses_a_remote_that_is_not_the_bare_repository(
    sandbox: Sandbox, commands: tuple[tuple[str, ...], ...]
) -> None:
    for command in commands:
        sandbox.git(sandbox.clone, *command)
    assert any("emote" in item for item in problems(sandbox))


def test_isolation_refuses_a_directory_that_is_not_bare(sandbox: Sandbox) -> None:
    found = isolation_problems(sandbox.env, [sandbox.clone], sandbox.clone, FAKE_GH)
    assert any("Not a bare repository" in item for item in found)


def test_isolation_refuses_another_gh_on_the_path(sandbox: Sandbox) -> None:
    other = sandbox.root / "other-bin"
    other.mkdir()
    real = other / "gh"
    real.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    real.chmod(0o755)
    env = {**sandbox.env, "PATH": f"{other}:{sandbox.env['PATH']}"}
    assert any("not the fake gh" in item for item in problems(sandbox, env))


def test_isolation_refuses_a_path_without_the_fake_gh(sandbox: Sandbox) -> None:
    # git stays reachable. A real gh beside it, or no gh at all, must both be refused.
    path = sandbox.env["PATH"].split(":", 1)[1]
    env = {**sandbox.env, "PATH": path}
    assert any("not the fake gh" in item for item in problems(sandbox, env))


def test_a_problem_stops_the_test_before_it_starts(sandbox: Sandbox) -> None:
    env = {**sandbox.env, "GH_TOKEN": "real-token"}
    with pytest.raises(pytest.fail.Exception, match="GH_TOKEN is set"):
        require_isolation(env, [sandbox.clone], sandbox.bare, FAKE_GH)


def test_the_sandbox_itself_stops_on_a_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With the fake gh's directory gone from PATH, the real check must refuse to build.
    monkeypatch.setattr("conftest.FAKE_GH", tmp_path / "missing" / "gh")
    with pytest.raises(pytest.fail.Exception, match="not the fake gh"):
        Sandbox(tmp_path.resolve())


# The fake gh, since a lenient fake would prove nothing


def test_the_fake_gh_accepts_the_real_call_and_records_it(sandbox: Sandbox) -> None:
    sandbox.git(sandbox.clone, "push", "-q", "origin", "HEAD:refs/heads/task/one")
    done = run_gh(
        sandbox,
        "pr", "create", "--repo", GITHUB, "--head", "task/one", "--base", "main",
        "--title", "A title", "--label", LABEL, "--body-file", "-",
        stdin="Body\n",
    )  # fmt: skip
    assert done.returncode == 0, done
    url = done.stdout.strip()
    assert url == f"https://github.com/{GITHUB}/pull/1"
    pull_request = sandbox.pull_request(url)
    assert (pull_request["labels"], pull_request["body"]) == ([LABEL], "Body\n")
    assert sandbox.gh_calls()[-1]["refused"] is None


@pytest.mark.parametrize(
    ("args", "stdin", "reason"),
    [
        (("pr", "merge", "x"), "", "unknown command"),
        (("repo", "view"), "", "unknown command"),
        (
            ("pr", "create", "--repo", GITHUB, "--head", "task/one", "--base", "main",
             "--title", "T", "--body", "B", "--body-file", "-"),
            "B",
            "unknown flag",
        ),
        (
            ("pr", "create", "--repo", GITHUB, "--head", "task/one", "--base", "main",
             "--body-file", "-"),
            "B",
            "missing flags",
        ),
        (
            ("pr", "create", "--repo", GITHUB, "--head", "task/one", "--base", "main",
             "--title", "T", "--label", "no-such-label", "--body-file", "-"),
            "B",
            "unknown label",
        ),
        (
            ("pr", "create", "--repo", GITHUB, "--head", "task/unpushed", "--base",
             "main", "--title", "T", "--body-file", "-"),
            "B",
            "not on the remote",
        ),
        (
            ("pr", "create", "--repo", "other/repo", "--head", "task/one", "--base",
             "main", "--title", "T", "--body-file", "-"),
            "B",
            "unknown repository",
        ),
        (
            ("pr", "create", "--repo", GITHUB, "--head", "task/one", "--base", "main",
             "--title", "T", "--body-file", "-"),
            "",
            "empty body",
        ),
        (
            ("pr", "view", f"https://github.com/{GITHUB}/pull/9", "--json", "state"),
            "",
            "no pull request",
        ),
        (
            ("pr", "comment", f"https://github.com/{GITHUB}/pull/9", "--body-file",
             "-"),
            "B",
            "no pull request",
        ),
    ],
    ids=[
        "merge-command",
        "unknown-command",
        "body-flag",
        "no-title",
        "unknown-label",
        "unpushed-branch",
        "other-repository",
        "empty-body",
        "view-unknown",
        "comment-unknown",
    ],
)  # fmt: skip
def test_the_fake_gh_refuses_what_github_would(
    sandbox: Sandbox, args: tuple[str, ...], stdin: str, reason: str
) -> None:
    sandbox.git(sandbox.clone, "push", "-q", "origin", "HEAD:refs/heads/task/one")
    done = run_gh(sandbox, *args, stdin=stdin)
    assert done.returncode != 0
    assert reason in done.stderr
    assert sandbox.gh_calls()[-1]["refused"] is not None
    assert sandbox.gh_state()["pull_requests"] == []


def test_the_fake_gh_refuses_a_wrong_token(sandbox: Sandbox) -> None:
    done = run_gh(
        sandbox, "label", "list", "--repo", GITHUB, "--json", "name", token="wrong"
    )
    assert done.returncode != 0
    assert "bad credentials" in done.stderr


def test_the_fake_gh_refuses_a_second_open_pull_request(sandbox: Sandbox) -> None:
    sandbox.git(sandbox.clone, "push", "-q", "origin", "HEAD:refs/heads/task/one")
    args = (
        "pr", "create", "--repo", GITHUB, "--head", "task/one", "--base", "main",
        "--title", "T", "--body-file", "-",
    )  # fmt: skip
    assert run_gh(sandbox, *args, stdin="B").returncode == 0
    done = run_gh(sandbox, *args, stdin="B")
    assert done.returncode != 0
    assert "already exists" in done.stderr


# The leak check itself


def test_the_leak_check_is_clean_after_a_full_flow(sandbox: Sandbox) -> None:
    _, url = sandbox.open_pull_request(TASK)
    assert sandbox.gh_state()["pull_requests"][0]["url"] == url
    assert sandbox.leaks() == []


def test_the_leak_check_finds_a_token_in_a_log(sandbox: Sandbox) -> None:
    planted = sandbox.orch_home / TARGET / "logs" / "planted.log"
    sandbox.write(planted, f"token={TOKEN}\n")
    assert sandbox.leaks() == [f"token in {planted}"]
    planted.unlink()


def test_the_leak_check_finds_a_personal_path_in_a_pull_request(
    sandbox: Sandbox,
) -> None:
    _, url = sandbox.open_pull_request(TASK)
    body = sandbox.pull_request(url)["body"]
    sandbox.set_pull_request(url, body=f"{body}\n{sandbox.root}/x\n")
    assert sandbox.leaks() == [f"personal path sent to GitHub: {sandbox.root}"]
    sandbox.set_pull_request(url, body=body)
