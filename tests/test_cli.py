import functools
import importlib.metadata
import io
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tomllib
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from orch import (
    config,
    credentials,
    github,
    notify,
    prflow,
    render,
    scheduler,
    store,
    waiter,
    watch,
)
from orch.cli import main
from orch.gate import GateReport, GateStatus
from orch.github import GitHubError, PullRequestStatus
from orch.gitops import GitError, LeaseRejectedError
from orch.models import Effort, Grade, Lane, Task, TaskState
from orch.prflow import PullRequestFlowError, PullRequestReport, UpdateReport
from transitions import check_call

HUB_SOURCE = Path(__file__).resolve().parents[1]
TARGET = "sample"
TASK = "feat-x"
TITLE = "Add feature X"
URL = "https://github.com/owner/sample/pull/7"
TOKEN = "gh" + "s_" + "t0ken" * 8
CHANGED = "src/fake_change.py"
USAGE = 64
INTERNAL = 70

Call = Callable[..., tuple[int, dict[str, object]]]


def spec_text(
    task_id: str = TASK,
    *,
    target: str = TARGET,
    depends_on: tuple[str, ...] = (),
    review: bool = False,
    modify: str = "src/**",
) -> str:
    dependencies = ", ".join(f'"{item}"' for item in depends_on)
    lines = [
        "+++",
        f'id = "{task_id}"',
        f'title = "{TITLE}"',
        f'target = "{target}"',
        'grade = "L2"',
        'lane = "B"',
        'model = "gpt-6.1-sol"',
        'effort = "medium"',
        "timeout_min = 5",
        f'modify = ["{modify}"]',
        'read = ["src/**"]',
        'verify = "true"',
        f"depends_on = [{dependencies}]",
    ]
    if review:
        lines.append("review = true")
    lines.extend(["+++", "", "# Purpose", "", "Test.", ""])
    return "\n".join(lines)


def review_text(task_id: str = TASK, *, block: bool = False) -> str:
    return json.dumps(
        {
            "task_id": task_id,
            "reviewer": "claude-reviewer",
            "verdict": "BLOCK" if block else "PASS",
            "findings": (
                [{"severity": "P1", "path": CHANGED, "summary": "치명적 지적"}]
                if block
                else []
            ),
        },
        ensure_ascii=False,
    )


TRIAGE = f"""+++
[[decisions]]
severity = "P1"
path = "{CHANGED}"
verdict = "wrong_finding"
decision = "The finding misreads the spec."
+++
Checked against the spec.
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def write(path: Path, text: str = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@dataclass(frozen=True, slots=True)
class Env:
    home: Path
    orch_home: Path
    hub: Path
    drafts: Path

    @property
    def paths(self) -> config.TargetPaths:
        return config.target_paths(self.orch_home, TARGET)

    @property
    def token_calls(self) -> Path:
        return self.orch_home / "token-calls"


@pytest.fixture(autouse=True)
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    home = tmp_path / "home"
    home.mkdir()
    hub = tmp_path / "hub"
    (hub / "tests").mkdir(parents=True)
    (hub / "tests" / "fake_agents").symlink_to(HUB_SOURCE / "tests" / "fake_agents")
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(
        "[user]\n\tname = t\n\temail = t@example.com\n[init]\n\tdefaultBranch = main\n",
        encoding="utf-8",
    )
    values = {
        "HOME": str(home),
        "ORCH_HOME": str(home / "runtime"),
        "ORCH_HUB": str(hub),
        "ORCH_FAKE_AGENTS": "1",
        "ORCH_FAKE_SCENARIO": "success",
        "ORCH_FAKE_PYTHON": sys.executable,
        "CODEX_HOME": str(home / "codex"),
        "GIT_CONFIG_GLOBAL": str(gitconfig),
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    for name in ("ORCH_TARGET", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    return Env(
        home=home, orch_home=home / "runtime", hub=hub, drafts=tmp_path / "drafts"
    )


@pytest.fixture
def orch(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> Call:
    def call(
        *args: str, stdin: str = "", unchecked: bool = False
    ) -> tuple[int, dict[str, object]]:
        orch_home = Path(os.environ.get("ORCH_HOME", "relative"))
        if orch_home.is_absolute():
            check_call(config.state_db_path(orch_home), args, unchecked=unchecked)
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        code = main(list(args))
        captured = capsys.readouterr()
        # Keep stderr for the test: reading the output must not discard warnings.
        sys.stderr.write(captured.err)
        out = captured.out
        assert out.endswith("\n") and out.count("\n") == 1, out
        return code, json.loads(out)

    return call


def test_the_orch_fixture_keeps_stderr_for_the_test(
    orch: Call,
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    insert(env, state=TaskState.PREPARED)

    def failed(path: Path, text: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(render, "write_todo", failed)
    code, data = orch("next")
    assert code == 0, data
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "TODO.md 갱신 실패" in captured.err
    assert "disk full" in captured.err


def assert_error(result: tuple[int, dict[str, object]], code: int, error: str) -> None:
    assert result[0] == code, result
    data = result[1]
    assert data["ok"] is False
    assert data["error"] == error
    assert re.search("[가-힣]", str(data["message"]))


@pytest.fixture
def repo(env: Env, tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    path = env.home / "repo"
    path.mkdir()
    git(path, "init", "-q")
    write(path / "src" / "app.py", "base\n")
    git(path, "add", ".")
    git(path, "commit", "-qm", "base")
    git(path, "remote", "add", "origin", str(remote))
    git(path, "push", "-q", "origin", "main")
    return path


def add_args(repo: Path, name: str = TARGET, setup: str = "true") -> tuple[str, ...]:
    return (
        "target",
        "add",
        str(repo),
        "--name",
        name,
        "--github",
        "owner/sample",
        "--setup",
        setup,
        "--test",
        "true",
        "--lint",
        "true",
    )


@pytest.fixture
def target(orch: Call, repo: Path) -> None:
    code, data = orch(*add_args(repo))
    assert code == 0, data


def draft(env: Env, text: str | None = None, name: str = "draft.md") -> Path:
    path = env.drafts / name
    write(path, spec_text() if text is None else text)
    return path


def add_token_script(env: Env, *, output: str = TOKEN, code: int = 0) -> None:
    script = env.orch_home / "bin" / "ghapp-token.sh"
    write(
        script,
        "#!/bin/sh\n"
        f"echo called >> '{env.token_calls}'\n"
        f"printf '%s\\n' '{output}'\n"
        "echo 'stderr-secret' >&2\n"
        f"exit {code}\n",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)


def prepare(orch: Call, env: Env, text: str | None = None) -> Path:
    spec = spec_text() if text is None else text
    name = re.search(r'^id = "(.+)"$', spec, re.MULTILINE).group(1)
    code, data = orch("prepare", str(draft(env, spec, f"{name}.draft.md")))
    assert code == 0, data
    return Path(str(data["worktree"]))


def commit_tests(worktree: Path) -> str:
    write(worktree / "tests" / "test_x.py")
    git(worktree, "add", ".")
    git(worktree, "commit", "-qm", "tests")
    return git(worktree, "rev-parse", "HEAD")


def reserve(orch: Call, env: Env, text: str | None = None) -> Path:
    worktree = prepare(orch, env, text)
    commit_tests(worktree)
    task_id = worktree.name
    code, data = orch("dispatch", task_id)
    assert code == 0, data
    assert data["state"] == "reserved"
    return worktree


def judge(orch: Call, env: Env, text: str | None = None) -> Path:
    worktree = reserve(orch, env, text)
    code, data = orch("run", worktree.name)
    assert code == 0, data
    return worktree


def make_ready(orch: Call, env: Env) -> Path:
    worktree = judge(orch, env)
    code, data = orch("gate", TASK)
    assert code == 0, data
    assert data["state"] == "ready"
    return worktree


def make_reviewing(orch: Call, env: Env) -> Path:
    worktree = judge(orch, env, spec_text(review=True))
    code, data = orch("gate", TASK)
    assert code == 0, data
    assert data["state"] == "reviewing"
    return worktree


def connect(env: Env) -> sqlite3.Connection:
    return store.connect(config.state_db_path(env.orch_home))


def state_of(env: Env, task_id: str = TASK) -> TaskState:
    conn = connect(env)
    try:
        task = store.get_task(conn, task_id)
    finally:
        conn.close()
    assert task is not None
    return task.state


def insert(
    env: Env,
    task_id: str = TASK,
    state: TaskState = TaskState.PR_OPEN,
    *,
    target: str = TARGET,
    pr_url: str | None = None,
    depends_on: tuple[str, ...] = (),
) -> None:
    conn = connect(env)
    try:
        store.insert_task(
            conn,
            Task(
                id=task_id,
                target=target,
                title=TITLE,
                grade=Grade.L2,
                lane=Lane.B,
                model="gpt-6.1-sol",
                effort=Effort.MEDIUM,
                state=state,
                timeout_sec=300,
                depends_on=depends_on,
                pr_url=pr_url,
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
            ),
        )
    finally:
        conn.close()


# Output contract and entry point


def test_the_console_script_points_at_main() -> None:
    data = tomllib.loads((HUB_SOURCE / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["scripts"]["orch"] == "orch.cli:main"


def run_process(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from orch.cli import main; raise SystemExit(main())",
            *args,
        ],
        capture_output=True,
        text=True,
        env=os.environ,
    )


def test_a_process_prints_one_json_line_and_exits_zero() -> None:
    process = run_process("status")
    assert process.returncode == 0, process.stderr
    assert process.stdout.count("\n") == 1
    assert json.loads(process.stdout)["ok"] is True


def test_a_process_with_a_bad_command_prints_json_and_exits_64() -> None:
    process = run_process("nosuch")
    assert process.returncode == USAGE
    assert process.stdout.count("\n") == 1
    assert json.loads(process.stdout)["error"] == "usage"


def test_help_exits_zero() -> None:
    assert run_process("--help").returncode == 0


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("nosuch",),
        ("gate",),
        ("review",),
        ("review", "save"),
        ("target",),
        ("target", "add"),
        ("status", "extra"),
        ("resume", TASK, "--nosuch"),
    ],
)
def test_bad_arguments_are_a_usage_error(orch: Call, args: tuple[str, ...]) -> None:
    assert_error(orch(*args), USAGE, "usage")


def test_an_unexpected_error_is_internal(
    orch: Call, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failed(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(store, "list_tasks", failed)
    assert_error(orch("status"), INTERNAL, "internal")


@pytest.mark.parametrize(
    "args",
    [
        ("dispatch", TASK),
        ("run", TASK),
        ("gate", TASK),
        ("pr", TASK),
        ("update", TASK),
        ("close", TASK),
        ("discard", TASK),
        ("retry", TASK),
        ("resume", TASK),
        ("review", "save", TASK),
    ],
)
def test_an_unknown_task_is_refused_before_a_token_is_issued(
    orch: Call, env: Env, args: tuple[str, ...]
) -> None:
    add_token_script(env)
    assert_error(orch(*args), USAGE, "refused")
    assert not env.token_calls.exists()


# Hub location


NEEDS_HUB = [
    ("target", "add", "/x", "--name", "n", "--github", "o/r"),
    ("prepare", "/x.md"),
    ("run", TASK),
    ("gate", TASK),
    ("pr", TASK),
    ("update", TASK),
    ("close", TASK),
    ("discard", TASK),
]


@pytest.mark.parametrize("args", NEEDS_HUB)
def test_commands_reading_targets_need_the_hub(
    orch: Call, monkeypatch: pytest.MonkeyPatch, args: tuple[str, ...]
) -> None:
    monkeypatch.delenv("ORCH_HUB")
    monkeypatch.delenv("ORCH_FAKE_AGENTS")
    full = (*args, "--setup", "s", "--test", "t", "--lint", "l")
    assert_error(orch(*(full if args[0] == "target" else args)), USAGE, "usage")


def test_a_relative_hub_is_refused(orch: Call, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCH_HUB", "relative/hub")
    assert_error(orch("gate", TASK), USAGE, "usage")


@pytest.mark.usefixtures("target")
def test_other_commands_work_without_the_hub(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = prepare(orch, env)
    commit_tests(worktree)
    monkeypatch.delenv("ORCH_HUB")
    monkeypatch.delenv("ORCH_FAKE_AGENTS")
    assert orch("status")[0] == 0
    assert orch("next")[0] == 0
    assert orch("dispatch", TASK)[0] == 0


# Fake mode


@pytest.mark.parametrize("value", [None, "", "~/.orch", "~/.orch/", "HOME/.orch"])
def test_fake_agents_need_a_home_other_than_the_default(
    orch: Call,
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    value: str | None,
) -> None:
    if value is None:
        monkeypatch.delenv("ORCH_HOME")
    else:
        monkeypatch.setenv("ORCH_HOME", value.replace("HOME", str(env.home)))
    assert_error(orch("status"), USAGE, "usage")
    assert not (env.home / ".orch").exists()


def test_fake_agents_refuse_a_link_to_the_default_home(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    (env.home / ".orch").mkdir()
    link = env.home / "link"
    link.symlink_to(env.home / ".orch")
    monkeypatch.setenv("ORCH_HOME", str(link))
    assert_error(orch("status"), USAGE, "usage")
    assert list((env.home / ".orch").iterdir()) == []


def test_fake_agents_with_another_home_are_allowed(orch: Call, env: Env) -> None:
    assert orch("status")[0] == 0
    assert (env.orch_home / "state.db").exists()


@pytest.mark.parametrize("value", ["0", ""])
def test_the_default_home_is_allowed_without_fake_agents(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("ORCH_FAKE_AGENTS", value)
    monkeypatch.delenv("ORCH_HOME")
    assert orch("status")[0] == 0
    assert (env.home / ".orch" / "state.db").exists()


def test_a_relative_home_is_refused(
    orch: Call, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ORCH_HOME", "relative/home")
    assert_error(orch("status"), USAGE, "usage")


# target add


def test_target_add_stores_a_home_relative_path(
    orch: Call, env: Env, repo: Path
) -> None:
    code, data = orch(*add_args(repo), "--shared", "uv.lock", "--shared", "docs/**")
    assert code == 0, data
    assert data == {"ok": True, "target": TARGET}
    stored = tomllib.loads((env.hub / "targets.toml").read_text(encoding="utf-8"))
    entry = stored["targets"][TARGET]
    assert entry["path"] == "~/repo"
    assert entry["default_branch"] == "main"
    assert entry["shared_paths"] == ["uv.lock", "docs/**"]
    assert env.paths.tasks.is_dir()


def test_target_add_takes_a_default_branch_and_a_relative_path(
    orch: Call, env: Env, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    args = list(add_args(repo))
    args[2] = "."
    code, data = orch(*args, "--default-branch", "trunk")
    assert code == 0, data
    stored = tomllib.loads((env.hub / "targets.toml").read_text(encoding="utf-8"))
    assert stored["targets"][TARGET]["path"] == "~/repo"
    assert stored["targets"][TARGET]["default_branch"] == "trunk"


def test_target_add_refuses_a_path_outside_the_home(
    orch: Call, env: Env, tmp_path: Path
) -> None:
    assert_error(orch(*add_args(tmp_path)), USAGE, "refused")
    assert not (env.hub / "targets.toml").exists()


@pytest.mark.usefixtures("target")
def test_target_add_refuses_a_registered_name(orch: Call, repo: Path) -> None:
    assert_error(orch(*add_args(repo)), USAGE, "refused")


# prepare


@pytest.mark.usefixtures("target")
def test_prepare_reports_the_worktree_and_saves_the_spec(orch: Call, env: Env) -> None:
    code, data = orch("prepare", str(draft(env)))
    assert code == 0, data
    assert data["ok"] is True
    assert data["task_id"] == TASK
    assert data["state"] == "prepared"
    assert data["worktree"] == str(env.paths.worktrees / TASK)
    assert re.fullmatch("[0-9a-f]{40}", str(data["base_sha"]))
    assert Path(str(data["worktree"])).is_dir()
    assert (env.paths.tasks / f"{TASK}.md").read_text(encoding="utf-8") == spec_text()


def test_prepare_keeps_setup_output_out_of_stdout(
    orch: Call, env: Env, repo: Path
) -> None:
    assert orch(*add_args(repo, setup="echo noise; echo noise >&2"))[0] == 0
    assert orch("prepare", str(draft(env)))[0] == 0


def test_prepare_creates_runtime_directories_of_a_committed_target(
    orch: Call, env: Env, repo: Path
) -> None:
    write(
        env.hub / "targets.toml",
        f'[targets."{TARGET}"]\n'
        'path = "~/repo"\n'
        'github = "owner/sample"\n'
        'default_branch = "main"\n'
        'setup_command = "true"\n'
        'test_command = "true"\n'
        'lint_command = "true"\n',
    )
    assert not env.paths.root.exists()
    code, data = orch("prepare", str(draft(env)))
    assert code == 0, data
    assert env.paths.tasks.is_dir()


@pytest.mark.usefixtures("target")
def test_prepare_refuses_an_unregistered_target(orch: Call, env: Env) -> None:
    text = spec_text(target="other")
    assert_error(orch("prepare", str(draft(env, text))), USAGE, "refused")
    assert not (env.orch_home / "other").exists()


@pytest.mark.usefixtures("target")
def test_prepare_refuses_an_unreadable_spec(orch: Call, env: Env) -> None:
    assert_error(orch("prepare", str(env.drafts / "none.md")), USAGE, "refused")
    assert_error(orch("prepare", str(draft(env, "no front matter"))), USAGE, "refused")


@pytest.mark.usefixtures("target")
def test_prepare_refuses_a_spec_for_another_session_target(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ORCH_TARGET", "other")
    assert_error(orch("prepare", str(draft(env))), USAGE, "refused")
    assert not (env.paths.worktrees / TASK).exists()


@pytest.mark.usefixtures("target")
def test_prepare_accepts_the_session_target(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ORCH_TARGET", TARGET)
    assert orch("prepare", str(draft(env)))[0] == 0


@pytest.mark.usefixtures("target")
def test_prepare_refuses_a_registered_id(orch: Call, env: Env) -> None:
    prepare(orch, env)
    assert_error(orch("prepare", str(draft(env))), USAGE, "refused")


def test_prepare_can_run_again_after_a_failed_setup(
    orch: Call, env: Env, repo: Path
) -> None:
    assert orch(*add_args(repo, setup="exit 3"))[0] == 0
    spec = draft(env)
    assert_error(orch("prepare", str(spec)), USAGE, "refused")
    assert not (env.paths.tasks / f"{TASK}.md").exists()
    targets = env.hub / "targets.toml"
    targets.write_text(
        targets.read_text(encoding="utf-8").replace("exit 3", "true"),
        encoding="utf-8",
    )
    assert orch("prepare", str(spec))[0] == 0


# dispatch


@pytest.mark.usefixtures("target")
def test_dispatch_reserves_a_tested_task(orch: Call, env: Env) -> None:
    commit_tests(prepare(orch, env))
    code, data = orch("dispatch", TASK)
    assert code == 0, data
    assert data == {
        "ok": True,
        "task_id": TASK,
        "state": "reserved",
        "waiting_for": [],
    }


@pytest.mark.usefixtures("target")
def test_dispatch_needs_a_test_commit(orch: Call, env: Env) -> None:
    prepare(orch, env)
    assert_error(orch("dispatch", TASK), USAGE, "refused")
    assert state_of(env) is TaskState.PREPARED


@pytest.mark.usefixtures("target")
def test_dispatch_refuses_a_reserved_task(orch: Call, env: Env) -> None:
    reserve(orch, env)
    assert_error(orch("dispatch", TASK), USAGE, "refused")
    assert state_of(env) is TaskState.RESERVED


@pytest.mark.usefixtures("target")
def test_dispatch_queues_a_task_waiting_on_a_dependency(orch: Call, env: Env) -> None:
    prepare(orch, env)
    text = spec_text("feat-y", depends_on=(TASK,))
    commit_tests(prepare(orch, env, text))
    code, data = orch("dispatch", "feat-y")
    assert code == 0, data
    assert data["state"] == "queued"
    assert data["waiting_for"] == [f"dep:{TASK}"]


@pytest.mark.usefixtures("target")
@pytest.mark.parametrize(
    "args",
    [
        ("dispatch", TASK),
        ("run", TASK),
        ("gate", TASK),
        ("retry", TASK),
        ("resume", TASK),
        ("discard", TASK),
        ("pr", TASK),
        ("update", TASK),
        ("close", TASK),
        ("review", "save", TASK),
    ],
)
def test_commands_refuse_a_task_of_another_session_target(
    orch: Call,
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    args: tuple[str, ...],
) -> None:
    add_token_script(env)
    commit_tests(prepare(orch, env))
    monkeypatch.setenv("ORCH_TARGET", "other")
    assert_error(orch(*args, unchecked=True), USAGE, "refused")
    assert state_of(env) is TaskState.PREPARED
    assert not env.token_calls.exists()


# run


@pytest.mark.usefixtures("target")
@pytest.mark.parametrize(
    ("scenario", "code", "status", "reason", "state"),
    [
        ("success", 0, "DONE", None, "judging"),
        ("partial", 10, "PARTIAL", None, "judging"),
        ("report_missing", 20, "FAILED", "report_missing", "judging"),
        ("out_of_scope", 20, "FAILED", "out_of_scope", "judging"),
        ("blocked", 30, "BLOCKED", "spec_mismatch", "blocked"),
        ("usage_limit", 40, "BLOCKED_QUOTA", None, "blocked"),
    ],
)
def test_run_exits_by_outcome_and_settles_a_blocked_run(
    orch: Call,
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    code: int,
    status: str,
    reason: str | None,
    state: str,
) -> None:
    reserve(orch, env)
    monkeypatch.setenv("ORCH_FAKE_SCENARIO", scenario)
    result = orch("run", TASK)
    assert result[0] == code, result
    data = result[1]
    assert data["ok"] is True
    assert data["task_id"] == TASK
    assert data["status"] == status
    assert data["failure_reason"] == reason
    assert data["state"] == state
    assert state_of(env).value == state
    assert (env.paths.results / f"{TASK}.json").exists()


@pytest.mark.usefixtures("target")
def test_run_refuses_a_task_that_is_not_reserved(orch: Call, env: Env) -> None:
    prepare(orch, env)
    assert_error(orch("run", TASK, unchecked=True), USAGE, "refused")
    assert state_of(env) is TaskState.PREPARED


# gate


@pytest.mark.usefixtures("target")
def test_gate_passes_and_commits(orch: Call, env: Env) -> None:
    worktree = judge(orch, env)
    code, data = orch("gate", TASK)
    assert code == 0, data
    assert data["ok"] is True
    assert data["task_id"] == TASK
    assert data["passed"] is True
    assert data["state"] == "ready"
    assert data["failed_gate"] is None
    assert data["gates"] == {"G1": "passed", "G2": "passed", "G3": "passed"}
    assert git(worktree, "log", "-1", "--format=%s") == TITLE
    assert Path(str(data["log_path"])).parent == env.paths.logs


@pytest.mark.usefixtures("target")
def test_gate_sends_a_task_needing_review_to_reviewing(orch: Call, env: Env) -> None:
    make_reviewing(orch, env)
    assert state_of(env) is TaskState.REVIEWING


@pytest.mark.usefixtures("target")
def test_gate_failure_exits_20_and_returns_to_judging(orch: Call, env: Env) -> None:
    worktree = judge(orch, env)
    write(worktree / "outside" / "change.py")
    code, data = orch("gate", TASK)
    assert code == 20
    assert data["ok"] is True
    assert data["passed"] is False
    assert data["failed_gate"] == "G1"
    assert data["detail"]
    assert data["state"] == "judging"
    assert data["gates"]["G2"] == "not_run"


@pytest.mark.usefixtures("target")
def test_gate_refuses_a_task_with_the_wrong_state(orch: Call, env: Env) -> None:
    prepare(orch, env)
    assert_error(orch("gate", TASK, unchecked=True), USAGE, "refused")


# review save


@pytest.mark.usefixtures("target")
def test_review_save_with_pass_moves_to_ready(orch: Call, env: Env) -> None:
    make_reviewing(orch, env)
    code, data = orch("review", "save", TASK, stdin=review_text())
    assert code == 0, data
    assert data == {"ok": True, "task_id": TASK, "state": "ready"}
    assert env.paths.review_path(TASK).exists()


@pytest.mark.usefixtures("target")
def test_a_block_without_triage_stops_the_gate_until_retry(
    orch: Call, env: Env
) -> None:
    make_reviewing(orch, env)
    code, data = orch("review", "save", TASK, stdin=review_text(block=True))
    assert code == 0, data
    assert data["state"] == "judging"

    assert_error(orch("gate", TASK), USAGE, "refused")
    assert state_of(env) is TaskState.JUDGING

    code, data = orch("retry", TASK)
    assert code == 0, data
    assert data["state"] == "reserved"
    assert env.paths.review_path(TASK, 1).exists()
    assert orch("run", TASK)[0] == 0
    code, data = orch("gate", TASK)
    assert code == 0, data
    assert data["state"] == "reviewing"


@pytest.mark.usefixtures("target")
def test_review_save_with_triage_moves_to_ready(orch: Call, env: Env) -> None:
    make_reviewing(orch, env)
    triage = env.drafts / "triage.md"
    write(triage, TRIAGE)
    code, data = orch(
        "review",
        "save",
        TASK,
        "--triage",
        str(triage),
        stdin=review_text(block=True),
    )
    assert code == 0, data
    assert data["state"] == "ready"
    assert env.paths.triage_path(TASK).read_text(encoding="utf-8") == TRIAGE


@pytest.mark.usefixtures("target")
def test_review_save_refuses_bad_input(orch: Call, env: Env) -> None:
    make_reviewing(orch, env)
    assert_error(orch("review", "save", TASK, stdin="{not json"), USAGE, "refused")
    assert_error(
        orch(
            "review",
            "save",
            TASK,
            "--triage",
            str(env.drafts / "none.md"),
            stdin=review_text(block=True),
        ),
        USAGE,
        "refused",
    )
    assert state_of(env) is TaskState.REVIEWING
    assert not env.paths.review_path(TASK).exists()


@pytest.mark.usefixtures("target")
def test_review_save_needs_a_reviewing_task(orch: Call, env: Env) -> None:
    prepare(orch, env)
    assert_error(
        orch("review", "save", TASK, stdin=review_text(), unchecked=True),
        USAGE,
        "refused",
    )


# retry and resume


@pytest.mark.usefixtures("target")
def test_retry_starts_the_second_attempt_from_the_test_commit(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = prepare(orch, env)
    test_sha = commit_tests(worktree)
    assert orch("dispatch", TASK)[0] == 0
    monkeypatch.setenv("ORCH_FAKE_SCENARIO", "report_missing")
    assert orch("run", TASK)[0] == 20
    assert (worktree / CHANGED).exists()

    code, data = orch("retry", TASK)
    assert code == 0, data
    assert data == {
        "ok": True,
        "task_id": TASK,
        "state": "reserved",
        "waiting_for": [],
    }
    assert git(worktree, "rev-parse", "HEAD") == test_sha
    assert not (worktree / CHANGED).exists()
    status = orch("status")[1]
    assert status["tasks"][0]["attempt"] == 2


@pytest.mark.usefixtures("target")
def test_retry_refuses_a_task_that_cannot_retry(orch: Call, env: Env) -> None:
    prepare(orch, env)
    assert_error(orch("retry", TASK), USAGE, "refused")


@pytest.fixture
def blocked(orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch) -> Path:
    worktree = reserve(orch, env)
    with monkeypatch.context() as patch:
        patch.setenv("ORCH_FAKE_SCENARIO", "blocked")
        assert orch("run", TASK)[0] == 30
    write(worktree / "src" / "partial.py")
    return worktree


@pytest.mark.usefixtures("target")
def test_resume_resets_by_default(orch: Call, blocked: Path) -> None:
    code, data = orch("resume", TASK)
    assert code == 0, data
    assert data == {
        "ok": True,
        "task_id": TASK,
        "state": "reserved",
        "waiting_for": [],
    }
    assert not (blocked / "src" / "partial.py").exists()
    assert orch("status")[1]["tasks"][0]["attempt"] == 1


@pytest.mark.usefixtures("target")
def test_resume_with_keep_leaves_the_worktree_alone(orch: Call, blocked: Path) -> None:
    code, data = orch("resume", TASK, "--keep")
    assert code == 0, data
    assert (blocked / "src" / "partial.py").exists()


@pytest.mark.usefixtures("target")
def test_resume_refuses_a_task_that_is_not_stopped(orch: Call, env: Env) -> None:
    prepare(orch, env)
    assert_error(orch("resume", TASK), USAGE, "refused")


# pr


@pytest.fixture
def pr_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []
    report = PullRequestReport(state=TaskState.PR_OPEN, pr_url=URL)

    def fake(conn: object, spec: object, target: object, **kwargs: object) -> object:
        calls.append({"spec": spec, "target": target, **kwargs})
        return calls_report[0]

    calls_report = [report]
    monkeypatch.setattr(prflow, "open_pull_request", fake)
    calls.append({"report": calls_report})
    return calls


@pytest.mark.usefixtures("target")
def test_pr_passes_the_paths_and_the_token(
    orch: Call, env: Env, pr_calls: list[dict[str, object]]
) -> None:
    make_ready(orch, env)
    add_token_script(env)
    code, data = orch("pr", TASK)
    assert code == 0, data
    assert data == {
        "ok": True,
        "task_id": TASK,
        "state": "pr_open",
        "pr_url": URL,
        "conflicts": [],
        "detail": None,
        "user_check": False,
        "head_sha": None,
    }
    call = pr_calls[1]
    assert call["spec"].id == TASK
    assert call["target"].name == TARGET
    assert call["token"] == TOKEN
    assert call["review_path"] == env.paths.review_path(TASK)
    assert call["triage_path"] == env.paths.triage_path(TASK)
    assert Path(call["log_path"]).parent == env.paths.logs
    assert call["home"] == env.home
    assert call["orch_home"] == env.orch_home
    assert call["resolution"] is None


@pytest.mark.usefixtures("target")
def test_pr_passes_the_resolution(
    orch: Call, env: Env, pr_calls: list[dict[str, object]]
) -> None:
    make_ready(orch, env)
    add_token_script(env)
    assert orch("pr", TASK, "--resolution", "Kept both changes.")[0] == 0
    assert pr_calls[1]["resolution"] == "Kept both changes."


@pytest.mark.usefixtures("target")
@pytest.mark.parametrize(
    "report",
    [
        PullRequestReport(state=TaskState.CONFLICT, conflicts=("src/app.py",)),
        PullRequestReport(state=TaskState.JUDGING, detail="G2 failed"),
    ],
)
def test_pr_exits_20_when_no_pull_request_opened(
    orch: Call,
    env: Env,
    pr_calls: list[dict[str, object]],
    report: PullRequestReport,
) -> None:
    make_ready(orch, env)
    add_token_script(env)
    pr_calls[0]["report"][0] = report
    code, data = orch("pr", TASK)
    assert code == 20
    assert data["ok"] is True
    assert data["state"] == report.state.value
    assert data["conflicts"] == list(report.conflicts)
    assert data["detail"] == report.detail


@pytest.mark.usefixtures("target")
def test_pr_flow_refusal_is_refused(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_ready(orch, env)
    add_token_script(env)

    def failed(*args: object, **kwargs: object) -> None:
        raise PullRequestFlowError("Repository needs label: review-overridden")

    monkeypatch.setattr(prflow, "open_pull_request", failed)
    assert_error(orch("pr", TASK), USAGE, "refused")


@pytest.mark.usefixtures("target")
def test_a_lease_rejection_is_refused_and_named(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_ready(orch, env)
    add_token_script(env)

    def failed(*args: object, **kwargs: object) -> None:
        raise LeaseRejectedError(f"Remote branch moved: task/{TASK}")

    monkeypatch.setattr(prflow, "open_pull_request", failed)
    result = orch("pr", TASK)
    assert_error(result, USAGE, "refused")
    assert f"task/{TASK}" in str(result[1]["message"])
    assert TOKEN not in json.dumps(result[1])


@pytest.mark.usefixtures("target")
def test_another_push_failure_stays_internal(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_ready(orch, env)
    add_token_script(env)

    def failed(*args: object, **kwargs: object) -> None:
        raise GitError("push failed")

    monkeypatch.setattr(prflow, "open_pull_request", failed)
    assert_error(orch("pr", TASK), INTERNAL, "internal")


@pytest.mark.usefixtures("target")
def test_a_github_failure_is_internal_and_hides_the_token(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_ready(orch, env)
    add_token_script(env)

    def failed(*args: object, **kwargs: object) -> None:
        raise GitHubError("gh failed")

    monkeypatch.setattr(prflow, "open_pull_request", failed)
    result = orch("pr", TASK)
    assert_error(result, INTERNAL, "internal")
    assert TOKEN not in json.dumps(result[1])


@pytest.mark.usefixtures("target")
@pytest.mark.parametrize(
    ("error", "code", "kind"),
    [
        (GitHubError, INTERNAL, "internal"),
        (GitError, INTERNAL, "internal"),
        (RuntimeError, INTERNAL, "internal"),
        (PullRequestFlowError, USAGE, "refused"),
    ],
)
def test_a_token_inside_an_exception_message_is_removed(
    orch: Call,
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    error: type[Exception],
    code: int,
    kind: str,
) -> None:
    make_ready(orch, env)
    add_token_script(env)

    def failed(*args: object, **kwargs: object) -> None:
        raise error(f"bad {TOKEN} here")

    monkeypatch.setattr(prflow, "open_pull_request", failed)
    result = orch("pr", TASK)
    assert_error(result, code, kind)
    assert TOKEN not in json.dumps(result[1])
    assert "bad [REDACTED] here" in str(result[1]["message"])


@pytest.mark.usefixtures("target")
@pytest.mark.parametrize(
    ("output", "code"),
    [("printed-by-a-failing-script", 1), ("", 0)],
)
def test_a_bad_token_script_is_internal_and_prints_nothing_of_it(
    orch: Call,
    env: Env,
    pr_calls: list[dict[str, object]],
    output: str,
    code: int,
) -> None:
    make_ready(orch, env)
    add_token_script(env, output=output, code=code)
    result = orch("pr", TASK)
    assert_error(result, INTERNAL, "internal")
    text = json.dumps(result[1])
    assert "stderr-secret" not in text
    assert output == "" or output not in text
    assert len(pr_calls) == 1


@pytest.mark.usefixtures("target")
def test_a_missing_token_script_is_internal(
    orch: Call, env: Env, pr_calls: list[dict[str, object]]
) -> None:
    make_ready(orch, env)
    assert_error(orch("pr", TASK), INTERNAL, "internal")
    assert len(pr_calls) == 1


def test_the_token_is_not_taken_from_the_environment(
    orch: Call, env: Env, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert orch(*add_args(repo))[0] == 0
    insert(env, pr_url=URL)
    monkeypatch.setenv("GH_TOKEN", "from-the-environment")
    assert_error(orch("close", TASK), INTERNAL, "internal")


# update


@pytest.mark.usefixtures("target")
@pytest.mark.parametrize(
    ("report", "code", "gates"),
    [
        (
            UpdateReport(
                state=TaskState.PR_OPEN,
                gates=GateReport(
                    passed=True,
                    gates={"G1": GateStatus.PASSED},
                    log_path="log",
                ),
            ),
            0,
            {
                "passed": True,
                "gates": {"G1": "passed"},
                "failed_gate": None,
                "detail": None,
                "log_path": "log",
            },
        ),
        (
            UpdateReport(
                state=TaskState.PR_OPEN,
                gates=GateReport(
                    passed=False,
                    gates={"G1": GateStatus.FAILED},
                    failed_gate="G1",
                    detail="scope",
                    log_path="log",
                ),
            ),
            20,
            {
                "passed": False,
                "gates": {"G1": "failed"},
                "failed_gate": "G1",
                "detail": "scope",
                "log_path": "log",
            },
        ),
        (UpdateReport(state=TaskState.CONFLICT, conflicts=("a.py",)), 20, None),
    ],
)
def test_update_reports_the_check_and_exits_by_it(
    orch: Call,
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    report: UpdateReport,
    code: int,
    gates: object,
) -> None:
    prepare(orch, env)
    add_token_script(env)
    calls: list[dict[str, object]] = []

    def fake(conn: object, spec: object, target: object, **kwargs: object) -> object:
        calls.append({"spec": spec, "target": target, **kwargs})
        return report

    monkeypatch.setattr(prflow, "update_pull_request", fake)
    result = orch("update", TASK, unchecked=True)
    assert result[0] == code, result
    assert result[1] == {
        "ok": True,
        "task_id": TASK,
        "state": report.state.value,
        "conflicts": list(report.conflicts),
        "gates": gates,
    }
    assert calls[0]["spec"].id == TASK
    assert calls[0]["token"] == TOKEN
    assert Path(calls[0]["log_path"]).parent == env.paths.logs
    assert calls[0]["home"] == env.home
    assert calls[0]["orch_home"] == env.orch_home


# close and discard


def merged_status(state: str = "MERGED") -> Callable[..., PullRequestStatus]:
    def status(*, url: str, token: str) -> PullRequestStatus:
        assert url == URL
        assert token == TOKEN
        return PullRequestStatus(
            state=state,
            merged=state == "MERGED",
            merged_at=None,
            mergeable="UNKNOWN",
        )

    return status


@pytest.mark.usefixtures("target")
def test_close_closes_a_merged_task(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    insert(env, pr_url=URL)
    add_token_script(env)
    monkeypatch.setattr(github, "pull_request_status", merged_status())
    code, data = orch("close", TASK)
    assert code == 0, data
    assert data == {"ok": True, "task_id": TASK, "state": "closed"}
    assert state_of(env) is TaskState.CLOSED


@pytest.mark.usefixtures("target")
def test_close_refuses_an_unmerged_pull_request(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    insert(env, pr_url=URL)
    add_token_script(env)
    monkeypatch.setattr(github, "pull_request_status", merged_status("OPEN"))
    assert_error(orch("close", TASK), USAGE, "refused")
    assert state_of(env) is TaskState.PR_OPEN


@pytest.mark.usefixtures("target")
def test_discard_without_a_pull_request_issues_no_token(orch: Call, env: Env) -> None:
    worktree = prepare(orch, env)
    add_token_script(env)
    code, data = orch("discard", TASK)
    assert code == 0, data
    assert data == {"ok": True, "task_id": TASK, "state": "discarded"}
    assert not worktree.exists()
    assert not env.token_calls.exists()


@pytest.mark.usefixtures("target")
def test_discard_with_a_pull_request_checks_it_with_a_token(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    insert(env, pr_url=URL)
    add_token_script(env)
    monkeypatch.setattr(github, "pull_request_status", merged_status("CLOSED"))
    assert orch("discard", TASK)[0] == 0
    assert state_of(env) is TaskState.DISCARDED


@pytest.mark.usefixtures("target")
def test_discard_refuses_an_open_pull_request(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    insert(env, pr_url=URL)
    add_token_script(env)
    monkeypatch.setattr(github, "pull_request_status", merged_status("OPEN"))
    assert_error(orch("discard", TASK), USAGE, "refused")
    assert state_of(env) is TaskState.PR_OPEN


# next


@pytest.mark.usefixtures("target")
def test_next_reserves_a_queued_task_when_its_lock_clears(orch: Call, env: Env) -> None:
    commit_tests(prepare(orch, env))
    assert orch("dispatch", TASK)[1]["state"] == "reserved"
    commit_tests(prepare(orch, env, spec_text("feat-y")))
    assert orch("dispatch", "feat-y")[1]["waiting_for"] == [f"lock:{TASK}"]

    assert orch("discard", TASK)[0] == 0
    code, data = orch("next")
    assert code == 0, data
    assert data == {
        "ok": True,
        "reserved": ["feat-y"],
        "newly_blocked": [],
        "blocked_chains": [],
    }
    assert state_of(env, "feat-y") is TaskState.RESERVED


@pytest.mark.usefixtures("target")
def test_next_reports_blocked_tasks_with_their_causes(orch: Call, env: Env) -> None:
    prepare(orch, env)
    text = spec_text("feat-y", depends_on=(TASK,))
    commit_tests(prepare(orch, env, text))
    assert orch("dispatch", "feat-y")[1]["state"] == "queued"
    assert orch("discard", TASK)[0] == 0

    code, data = orch("next")
    assert code == 0, data
    assert data["reserved"] == []
    assert data["newly_blocked"] == ["feat-y"]
    assert data["blocked_chains"] == [
        {"task_id": "feat-y", "state": "blocked", "causes": [TASK]}
    ]


def set_pushed_sha(env: Env, sha: str, task_id: str = TASK) -> None:
    conn = connect(env)
    try:
        store.update_task(conn, task_id, pushed_sha=sha, at="2026-01-02T00:00:00+00:00")
    finally:
        conn.close()


def add_blocked_chain(env: Env, target: str, prefix: str) -> None:
    paths = config.ensure_target(env.orch_home, target, "owner/sample")
    cause, waiting = f"{prefix}-cause", f"{prefix}-waiting"
    insert(env, cause, TaskState.DISCARDED, target=target)
    insert(env, waiting, TaskState.QUEUED, target=target, depends_on=(cause,))
    write(
        paths.tasks / f"{waiting}.md",
        spec_text(waiting, target=target, depends_on=(cause,)),
    )


def test_next_and_status_show_only_the_session_target(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_blocked_chain(env, TARGET, "mine")
    add_blocked_chain(env, "other", "theirs")
    monkeypatch.setenv("ORCH_TARGET", TARGET)

    code, data = orch("next")
    assert code == 0, data
    assert data["newly_blocked"] == ["mine-waiting"]
    assert [item["task_id"] for item in data["blocked_chains"]] == ["mine-waiting"]

    code, data = orch("status")
    assert code == 0, data
    assert {item["id"] for item in data["tasks"]} == {"mine-cause", "mine-waiting"}
    assert [item["task_id"] for item in data["blocked_chains"]] == ["mine-waiting"]


def test_next_and_status_without_a_session_target_show_all(
    orch: Call, env: Env
) -> None:
    add_blocked_chain(env, TARGET, "mine")
    add_blocked_chain(env, "other", "theirs")
    data = orch("next")[1]
    assert sorted(data["newly_blocked"]) == ["mine-waiting", "theirs-waiting"]
    data = orch("status")[1]
    assert len(data["tasks"]) == 4
    assert len(data["blocked_chains"]) == 2


# status


def test_status_on_a_new_home_is_empty(orch: Call) -> None:
    assert orch("status") == (0, {"ok": True, "tasks": [], "blocked_chains": []})


def test_status_lists_each_task(orch: Call, env: Env) -> None:
    insert(env, pr_url=URL)
    code, data = orch("status")
    assert code == 0
    elapsed = data["tasks"][0]["elapsed_sec"]
    assert isinstance(elapsed, int) and elapsed > 0
    assert data["tasks"] == [
        {
            "id": TASK,
            "target": TARGET,
            "title": TITLE,
            "state": "pr_open",
            "grade": "L2",
            "lane": "B",
            "attempt": 0,
            "pr_url": URL,
            "elapsed_sec": elapsed,
            "head_sha": None,
        }
    ]


def test_status_human_prints_tasks_and_blocked_chains(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    add_blocked_chain(env, TARGET, "mine")
    assert main(["next"]) == 0
    capsys.readouterr()
    assert main(["status", "--human"]) == 0
    out = capsys.readouterr().out
    assert "mine-waiting" in out
    assert "blocked" in out
    assert "mine-cause" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_status_human_shows_elapsed_and_head(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    insert(env)
    set_pushed_sha(env, "318f3dd" + "0" * 33)
    assert main(["status", "--human"]) == 0
    assert re.fullmatch(
        rf"{TASK} pr_open \d+h \d\dm 318f3dd\n", capsys.readouterr().out
    )


def test_status_gives_the_full_head(orch: Call, env: Env) -> None:
    insert(env)
    sha = "318f3dd" + "0" * 33
    set_pushed_sha(env, sha)
    assert orch("status")[1]["tasks"][0]["head_sha"] == sha


def fake_status(
    tasks: list[str], chains: list[tuple[str, list[str]]], secret: str | None
) -> Callable[[object, list[str]], tuple[int, dict[str, object]]]:
    """Replace _execute: status issues no token, so this is the only way to get one."""

    def execute(_args: object, secrets: list[str]) -> tuple[int, dict[str, object]]:
        if secret is not None:
            secrets.append(secret)
        return 0, {
            "lines": [
                *(f"{item} ready" for item in tasks),
                *(f"{item} blocked: {', '.join(causes)}" for item, causes in chains),
            ]
        }

    return execute


@pytest.mark.parametrize("secret", [TOKEN, 'ab"cd\\ef'], ids=["plain", "quoted"])
@pytest.mark.parametrize("escape", [False, True], ids=["raw", "json-escaped"])
def test_status_human_removes_an_issued_token_from_every_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    secret: str,
    escape: bool,
) -> None:
    shown = json.dumps(secret, ensure_ascii=False)[1:-1] if escape else secret
    monkeypatch.setattr(
        "orch.cli._execute",
        fake_status(
            [f"task-{shown}"], [(f"chain-{shown}", [f"cause-{shown}", "other"])], secret
        ),
    )
    assert main(["status", "--human"]) == 0
    captured = capsys.readouterr()
    assert captured.out == (
        "task-[REDACTED] ready\nchain-[REDACTED] blocked: cause-[REDACTED], other\n"
    )
    assert captured.err == ""


def test_status_human_keeps_its_format_without_a_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "orch.cli._execute",
        fake_status(["one", "two"], [("three", ["four", "five"])], None),
    )
    assert main(["status", "--human"]) == 0
    assert (
        capsys.readouterr().out == "one ready\ntwo ready\nthree blocked: four, five\n"
    )


def test_status_human_prints_nothing_for_no_tasks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("orch.cli._execute", fake_status([], [], TOKEN))
    assert main(["status", "--human"]) == 0
    assert capsys.readouterr().out == ""


# Token removal from all output


def main_without_escape(args: list[str]) -> int:
    """Fail one test, instead of letting an interrupt stop the pytest session."""
    try:
        return main(args)
    except BaseException as exc:
        pytest.fail(f"main let {type(exc).__name__} escape", pytrace=False)


def fake_pull_request(
    monkeypatch: pytest.MonkeyPatch, report: PullRequestReport
) -> None:
    def fake(conn: object, spec: object, target: object, **kwargs: object) -> object:
        return report

    monkeypatch.setattr(prflow, "open_pull_request", fake)


@pytest.mark.usefixtures("target")
def test_a_token_in_success_fields_is_removed(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_ready(orch, env)
    add_token_script(env)
    fake_pull_request(
        monkeypatch,
        PullRequestReport(
            state=TaskState.PR_OPEN, pr_url=f"{URL}?t={TOKEN}", detail=f"used {TOKEN}"
        ),
    )
    code, data = orch("pr", TASK)
    assert code == 0, data
    assert TOKEN not in json.dumps(data)
    assert data["pr_url"] == f"{URL}?t=[REDACTED]"
    assert data["detail"] == "used [REDACTED]"


@pytest.mark.usefixtures("target")
def test_a_token_in_nested_fields_is_removed(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare(orch, env)
    add_token_script(env)
    report = UpdateReport(
        state=TaskState.PR_OPEN,
        conflicts=(f"src/{TOKEN}.py",),
        gates=GateReport(
            passed=False,
            gates={"G1": GateStatus.FAILED},
            failed_gate="G1",
            detail=f"scope {TOKEN}",
            log_path="log",
        ),
    )

    def fake(conn: object, spec: object, target: object, **kwargs: object) -> object:
        return report

    monkeypatch.setattr(prflow, "update_pull_request", fake)
    code, data = orch("update", TASK, unchecked=True)
    assert code == 20
    assert TOKEN not in json.dumps(data)
    assert data["conflicts"] == ["src/[REDACTED].py"]
    assert data["gates"]["detail"] == "scope [REDACTED]"


@pytest.mark.usefixtures("target")
def test_a_token_needing_json_escapes_is_removed(
    orch: Call,
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_ready(orch, env)
    secret = 'ab"cd'
    add_token_script(env, output=secret)
    fake_pull_request(
        monkeypatch,
        PullRequestReport(state=TaskState.PR_OPEN, pr_url=URL, detail=f"used {secret}"),
    )
    assert main(["pr", TASK]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["detail"] == "used [REDACTED]"
    assert 'ab\\"cd' not in out


def interrupted() -> BaseException:
    return KeyboardInterrupt(f"bad {TOKEN} here")


def exit_with_token() -> BaseException:
    return SystemExit(f"bad {TOKEN} here")


def interrupted_after_error() -> BaseException:
    error = KeyboardInterrupt("stopped")
    error.__cause__ = RuntimeError(f"bad {TOKEN} here")
    return error


@pytest.mark.usefixtures("target")
@pytest.mark.parametrize(
    "make_error", [interrupted, exit_with_token, interrupted_after_error]
)
def test_a_token_in_a_traceback_is_removed(
    orch: Call,
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    make_error: Callable[[], BaseException],
) -> None:
    make_ready(orch, env)
    add_token_script(env)

    def failed(*args: object, **kwargs: object) -> None:
        raise make_error()

    monkeypatch.setattr(prflow, "open_pull_request", failed)
    capsys.readouterr()
    assert main_without_escape(["pr", TASK]) == INTERNAL
    captured = capsys.readouterr()
    assert captured.out.count("\n") == 1
    assert json.loads(captured.out)["error"] == "internal"
    assert TOKEN not in captured.out
    assert "Traceback" in captured.err
    assert "[REDACTED]" in captured.err
    assert TOKEN not in captured.err


@pytest.mark.usefixtures("target")
def test_a_failure_while_writing_the_output_hides_the_token(
    orch: Call,
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_ready(orch, env)
    add_token_script(env)
    fake_pull_request(
        monkeypatch, PullRequestReport(state=TaskState.PR_OPEN, pr_url=URL)
    )
    dumps = json.dumps
    calls = []

    def failing_once(*args: object, **kwargs: object) -> str:
        calls.append(args)
        if len(calls) == 1:
            raise ValueError(f"bad {TOKEN} here")
        return dumps(*args, **kwargs)

    monkeypatch.setattr(json, "dumps", failing_once)
    capsys.readouterr()
    code = main(["pr", TASK])
    monkeypatch.setattr(json, "dumps", dumps)
    captured = capsys.readouterr()
    assert code == INTERNAL
    assert json.loads(captured.out)["error"] == "internal"
    assert TOKEN not in captured.out
    assert TOKEN not in captured.err


def test_an_interrupt_without_a_token_is_also_one_json_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def failed(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(store, "list_tasks", failed)
    assert main_without_escape(["status"]) == INTERNAL
    captured = capsys.readouterr()
    assert captured.out.count("\n") == 1
    assert json.loads(captured.out)["error"] == "internal"
    assert "KeyboardInterrupt" in captured.err


@pytest.mark.usefixtures("target")
def test_output_without_a_token_is_unchanged(orch: Call, env: Env) -> None:
    worktree = prepare(orch, env)
    code, data = orch("status")
    assert code == 0
    assert data["tasks"][0]["title"] == TITLE
    assert "REDACTED" not in json.dumps(data)
    assert worktree.is_dir()


INSTALLED_ORCH = Path(sys.executable).parent / "orch"


def test_version_prints_the_package_metadata_version() -> None:
    done = subprocess.run(
        [str(INSTALLED_ORCH), "--version"], capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == f"orch {importlib.metadata.version('orch')}\n"
    assert done.stderr == ""


def test_help_lists_the_version_option() -> None:
    done = subprocess.run(
        [str(INSTALLED_ORCH), "--help"], capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
    assert "--version" in done.stdout


def test_the_declared_versions_agree() -> None:
    project = tomllib.loads((HUB_SOURCE / "pyproject.toml").read_text("utf-8"))
    lock = tomllib.loads((HUB_SOURCE / "uv.lock").read_text("utf-8"))
    locked = [p["version"] for p in lock["package"] if p["name"] == "orch"]
    assert locked == [project["project"]["version"]]
    assert importlib.metadata.version("orch") == project["project"]["version"]


def missing_metadata(name: str) -> str:
    raise importlib.metadata.PackageNotFoundError(name)


@pytest.mark.usefixtures("env")
def test_other_commands_do_not_need_the_package_metadata(
    orch: Call, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(importlib.metadata, "version", missing_metadata)
    code, data = orch("status")
    assert code == 0, data


@pytest.mark.usefixtures("env")
def test_version_without_metadata_is_an_internal_error(
    orch: Call, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(importlib.metadata, "version", missing_metadata)
    assert_error(orch("--version"), INTERNAL, "internal")


# orch wait --merged


class WaitTime:
    """Time that only sleeping moves, for the real waiter behind the CLI."""

    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def fast_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the real waiter through the CLI without real sleeping."""
    moment = WaitTime()
    monkeypatch.setattr(
        waiter,
        "wait_for_merge",
        functools.partial(
            waiter.wait_for_merge, sleep=moment.sleep, clock=moment.clock
        ),
    )


def token_call_count(env: Env) -> int:
    if not env.token_calls.exists():
        return 0
    return len(env.token_calls.read_text(encoding="utf-8").splitlines())


def test_wait_exits_0_on_a_merge(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    insert(env, pr_url=URL)
    add_token_script(env)
    monkeypatch.setattr(github, "pull_request_status", merged_status())
    code, data = orch("wait", TASK, "--merged")
    assert code == 0, data
    assert data == {
        "ok": True,
        "task_id": TASK,
        "state": "pr_open",
        "pr_url": URL,
        "pr_state": "MERGED",
        "merged": True,
        "merged_at": None,
        "checks": 1,
    }
    assert token_call_count(env) == 1
    assert state_of(env) is TaskState.PR_OPEN


def test_wait_exits_50_on_a_close_without_a_merge(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    insert(env, pr_url=URL)
    add_token_script(env)
    monkeypatch.setattr(github, "pull_request_status", merged_status("CLOSED"))
    code, data = orch("wait", TASK, "--merged")
    assert code == 50, data
    assert data["ok"] is True
    assert data["pr_state"] == "CLOSED"
    assert data["merged"] is False
    assert state_of(env) is TaskState.PR_OPEN


@pytest.mark.usefixtures("fast_wait")
def test_wait_exits_60_at_the_limit_and_the_same_command_can_wait_again(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    insert(env, pr_url=URL)
    add_token_script(env)
    monkeypatch.setattr(github, "pull_request_status", merged_status("OPEN"))
    code, data = orch("wait", TASK, "--merged", "--max-wait", "1")
    assert code == 60, data
    assert data == {
        "ok": True,
        "task_id": TASK,
        "state": "pr_open",
        "pr_url": URL,
        "pr_state": "OPEN",
        "merged": False,
        "merged_at": None,
        "checks": 2,
    }
    assert token_call_count(env) == 2
    assert state_of(env) is TaskState.PR_OPEN

    monkeypatch.setattr(github, "pull_request_status", merged_status())
    code, data = orch("wait", TASK, "--merged", "--max-wait", "1")
    assert code == 0, data
    assert data["merged"] is True


@pytest.mark.usefixtures("fast_wait")
def test_wait_exits_60_with_no_known_state_when_every_check_failed(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    insert(env, pr_url=URL)
    add_token_script(env)

    def failing(*, url: str, token: str) -> PullRequestStatus:
        raise GitHubError("pr view: failed")

    monkeypatch.setattr(github, "pull_request_status", failing)
    code, data = orch("wait", TASK, "--merged", "--max-wait", "1")
    assert code == 60, data
    assert data["ok"] is True
    assert data["pr_state"] is None
    assert data["merged"] is False


@pytest.mark.parametrize(
    ("options", "interval", "limit"),
    [
        ((), 60, 90),
        (("--interval", "5"), 5, 90),
        (("--max-wait", "7"), 60, 7),
        (("--interval", "5", "--max-wait", "7"), 5, 7),
    ],
)
def test_wait_passes_its_options_to_the_waiter(
    orch: Call,
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    options: tuple[str, ...],
    interval: int,
    limit: int,
) -> None:
    insert(env, pr_url=URL)
    seen: dict[str, object] = {}

    def fake(
        conn: sqlite3.Connection, task_id: str, **kwargs: object
    ) -> waiter.MergeWaitReport:
        seen.update(kwargs, task_id=task_id)
        return waiter.MergeWaitReport(
            task_id=task_id,
            state=TaskState.PR_OPEN,
            pr_url=URL,
            pr_state="MERGED",
            merged=True,
            merged_at=None,
            checks=1,
        )

    monkeypatch.setattr(waiter, "wait_for_merge", fake)
    code, data = orch("wait", TASK, "--merged", *options)
    assert code == 0, data
    assert seen["task_id"] == TASK
    assert seen["interval_sec"] == interval
    assert seen["max_wait_min"] == limit
    assert callable(seen["token"])
    # Nothing is issued until the waiter asks for a token.
    assert token_call_count(env) == 0


def test_wait_without_merged_is_a_usage_error(orch: Call, env: Env) -> None:
    insert(env, pr_url=URL)
    add_token_script(env)
    assert_error(orch("wait", TASK), USAGE, "usage")
    assert not env.token_calls.exists()


def test_wait_without_merged_is_a_usage_error_even_for_an_unknown_task(
    orch: Call,
) -> None:
    assert_error(orch("wait", "no-such-task"), USAGE, "usage")


@pytest.mark.parametrize("flag", ["--interval", "--max-wait"])
@pytest.mark.parametrize("value", ["0", "-1", "1.5", "abc", ""])
def test_wait_refuses_a_bad_number(orch: Call, env: Env, flag: str, value: str) -> None:
    insert(env, pr_url=URL)
    add_token_script(env)
    assert_error(orch("wait", TASK, "--merged", flag, value), USAGE, "usage")
    assert not env.token_calls.exists()


def test_wait_refuses_a_task_in_another_state(orch: Call, env: Env) -> None:
    insert(env, state=TaskState.READY, pr_url=URL)
    add_token_script(env)
    assert_error(orch("wait", TASK, "--merged"), USAGE, "refused")
    assert not env.token_calls.exists()


def test_wait_refuses_a_task_without_a_pull_request(orch: Call, env: Env) -> None:
    insert(env)
    add_token_script(env)
    assert_error(orch("wait", TASK, "--merged"), USAGE, "refused")
    assert not env.token_calls.exists()


def test_wait_refuses_an_unknown_task_before_a_token_is_issued(
    orch: Call, env: Env
) -> None:
    add_token_script(env)
    assert_error(orch("wait", "no-such-task", "--merged"), USAGE, "refused")
    assert not env.token_calls.exists()


def test_wait_refuses_a_task_of_another_session_target(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    insert(env, pr_url=URL)
    add_token_script(env)
    monkeypatch.setenv("ORCH_TARGET", "other")
    assert_error(orch("wait", TASK, "--merged"), USAGE, "refused")
    assert not env.token_calls.exists()


def test_wait_does_not_need_the_hub(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    insert(env, pr_url=URL)
    add_token_script(env)
    monkeypatch.delenv("ORCH_HUB")
    monkeypatch.setattr(github, "pull_request_status", merged_status())
    code, data = orch("wait", TASK, "--merged")
    assert code == 0, data


@pytest.mark.usefixtures("fast_wait")
def test_wait_exits_70_after_ten_failed_checks_without_the_token(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    insert(env, pr_url=URL)
    add_token_script(env)

    def failing(*, url: str, token: str) -> PullRequestStatus:
        raise GitHubError(f"pr view: failed with {token}")

    monkeypatch.setattr(github, "pull_request_status", failing)
    result = orch("wait", TASK, "--merged")
    assert_error(result, INTERNAL, "internal")
    assert TOKEN not in str(result[1])
    assert token_call_count(env) == 10
    assert state_of(env) is TaskState.PR_OPEN


@pytest.mark.usefixtures("fast_wait")
def test_wait_exits_70_after_ten_token_failures_without_the_script_output(
    orch: Call, env: Env
) -> None:
    insert(env, pr_url=URL)
    add_token_script(env, code=1)
    result = orch("wait", TASK, "--merged")
    assert_error(result, INTERNAL, "internal")
    assert TOKEN not in str(result[1])
    assert "stderr-secret" not in str(result[1])
    assert token_call_count(env) == 10


# TODO.md


def todo_of(env: Env, target: str = TARGET) -> str:
    return config.target_paths(env.orch_home, target).todo.read_text(encoding="utf-8")


def todo_state(env: Env, task_id: str = TASK) -> str | None:
    """Return a task's state in TODO.md, or None before TODO.md shows it."""
    if not env.paths.todo.exists():
        return None
    found = re.search(rf"\| {task_id} \| {TITLE} \| (\w+) \|", todo_of(env))
    return None if found is None else found.group(1)


# A flow makes every call before it asserts on new behavior, so that a stub
# cannot hide a later call from the transition check in transitions.py.


@pytest.mark.usefixtures("target")
def test_each_state_change_rewrites_todo(orch: Call, env: Env) -> None:
    worktree = prepare(orch, env)
    seen = [todo_state(env)]
    commit_tests(worktree)
    codes = [orch("dispatch", TASK)[0]]
    seen.append(todo_state(env))
    codes.append(orch("run", TASK)[0])
    seen.append(todo_state(env))
    codes.append(orch("gate", TASK)[0])
    seen.append(todo_state(env))
    assert codes == [0, 0, 0]
    assert seen == ["prepared", "reserved", "judging", "ready"]


@pytest.mark.usefixtures("target")
def test_discard_rewrites_todo(orch: Call, env: Env) -> None:
    judge(orch, env)
    code = orch("discard", TASK)[0]
    assert code == 0
    assert todo_state(env) == "discarded"


@pytest.mark.usefixtures("target")
def test_a_run_shows_running_while_the_agent_works(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    reserve(orch, env)
    states: list[str] = []
    real = render.write_todo

    def record(path: Path, text: str) -> None:
        states.append(re.search(rf"\| {TASK} \| {TITLE} \| (\w+) \|", text).group(1))
        real(path, text)

    monkeypatch.setattr(render, "write_todo", record)
    assert orch("run", TASK)[0] == 0
    assert states == ["running", "judging"]


@pytest.mark.usefixtures("target")
def test_a_blocked_run_writes_the_settled_state(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    reserve(orch, env)
    monkeypatch.setenv("ORCH_FAKE_SCENARIO", "blocked")
    assert orch("run", TASK)[0] == 30
    assert f"| {TASK} | {TITLE} | blocked |" in todo_of(env)


def test_next_rewrites_each_target_with_its_own_tasks(orch: Call, env: Env) -> None:
    add_blocked_chain(env, TARGET, "mine")
    add_blocked_chain(env, "other", "theirs")
    assert orch("next")[0] == 0
    mine, theirs = todo_of(env), todo_of(env, "other")
    assert "mine-waiting" in mine and "theirs-waiting" not in mine
    assert "theirs-waiting" in theirs and "mine-waiting" not in theirs
    assert "- mine-waiting blocked: mine-cause" in mine


def test_a_refused_command_still_rewrites_and_keeps_its_answer(
    orch: Call, env: Env
) -> None:
    insert(env)
    assert_error(orch("dispatch", "missing"), USAGE, "refused")
    assert f"| {TASK} | {TITLE} | pr_open |" in todo_of(env)


def test_reading_commands_do_not_write_todo(orch: Call, env: Env) -> None:
    insert(env)
    assert orch("status")[0] == 0
    assert_error(orch("wait", TASK), USAGE, "usage")
    assert not env.paths.todo.exists()


@pytest.mark.usefixtures("target")
def test_todo_stays_out_of_every_work_tree(orch: Call, env: Env, repo: Path) -> None:
    worktree = judge(orch, env)
    assert env.paths.todo.is_file()
    for place in (repo, worktree, env.hub):
        assert not list(place.rglob("TODO.md")), place
    assert git(repo, "status", "--porcelain") == ""
    assert git(worktree, "status", "--porcelain", "--", "TODO.md") == ""


@pytest.mark.usefixtures("target")
def test_a_failed_rewrite_only_warns(
    orch: Call,
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reserve(orch, env)

    def fail(path: Path, text: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(render, "write_todo", fail)
    assert main(["run", TASK]) == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert captured.out.count("\n") == 1
    assert (data["ok"], data["status"], data["state"]) == (True, "DONE", "judging")
    assert state_of(env) is TaskState.JUDGING
    assert "TODO.md 갱신 실패: disk full" in captured.err


def test_one_failed_target_does_not_stop_the_others(
    orch: Call,
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    insert(env, "mine", target=TARGET)
    insert(env, "theirs", target="other")
    bad = config.target_paths(env.orch_home, TARGET).todo
    real = render.write_todo

    def fail_one(path: Path, text: str) -> None:
        if path == bad:
            raise OSError("disk full")
        real(path, text)

    monkeypatch.setattr(render, "write_todo", fail_one)
    assert main(["next"]) == 0
    assert "disk full" in capsys.readouterr().err
    assert "theirs" in todo_of(env, "other")


@pytest.mark.usefixtures("target")
def test_a_failed_rewrite_does_not_show_the_token(
    env: Env,
    orch: Call,
    pr_calls: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_ready(orch, env)
    add_token_script(env)

    def fail(path: Path, text: str) -> None:
        raise OSError(f"bad {TOKEN}")

    monkeypatch.setattr(render, "write_todo", fail)
    assert main(["pr", TASK]) == 0
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err
    assert "[REDACTED]" in captured.err


# orch pr head


@pytest.mark.usefixtures("target")
def test_pr_reports_the_pushed_commit(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_ready(orch, env)
    add_token_script(env)
    sha = "318f3dd" + "0" * 33

    def fake(conn: sqlite3.Connection, spec: object, target: object, **kwargs: object):
        store.update_task(conn, TASK, pushed_sha=sha, at="2026-01-02T00:00:00+00:00")
        return PullRequestReport(state=TaskState.PR_OPEN, pr_url=URL)

    monkeypatch.setattr(prflow, "open_pull_request", fake)
    assert orch("pr", TASK)[1]["head_sha"] == sha


# orch watch


def test_watch_once_prints_the_table_and_touches_nothing(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    insert(env)
    add_token_script(env)
    monkeypatch.delenv("ORCH_HUB")
    assert main(["watch", "--once"]) == 0
    captured = capsys.readouterr()
    assert f"| {TASK} | {TITLE} | pr_open |" in captured.out
    with pytest.raises(json.JSONDecodeError):
        json.loads(captured.out)
    assert captured.err == ""
    assert not env.token_calls.exists()
    assert not env.paths.todo.exists()
    assert state_of(env) is TaskState.PR_OPEN


def test_watch_shows_only_the_session_target(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    add_blocked_chain(env, TARGET, "mine")
    add_blocked_chain(env, "other", "theirs")
    monkeypatch.setenv("ORCH_TARGET", TARGET)
    assert main(["watch", "--once"]) == 0
    out = capsys.readouterr().out
    assert "mine-waiting" in out
    assert "theirs-waiting" not in out


@pytest.mark.parametrize(
    ("args", "interval", "once"),
    [((), 2, False), (("--interval", "7"), 7, False), (("--once",), 2, True)],
)
def test_watch_passes_its_options(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    args: tuple[str, ...],
    interval: int,
    once: bool,
) -> None:
    seen: dict[str, object] = {}

    def fake(frame: object, *, interval_sec: int, once: bool) -> int:
        seen.update(interval_sec=interval_sec, once=once)
        return 0

    monkeypatch.setattr(watch, "watch", fake)
    assert main(["watch", *args]) == 0
    assert seen == {"interval_sec": interval, "once": once}
    assert capsys.readouterr().out == ""


def test_watch_returns_the_loop_code_without_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(watch, "watch", lambda frame, **kwargs: 70)
    assert main(["watch", "--once"]) == 70
    assert capsys.readouterr().out == ""


def test_each_frame_reads_the_current_state(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    texts: list[str] = []

    def fake(frame, **kwargs: object) -> int:
        texts.append(frame())
        insert(env)
        texts.append(frame())
        return 0

    monkeypatch.setattr(watch, "watch", fake)
    assert main(["watch"]) == 0
    assert TASK not in texts[0]
    assert TASK in texts[1]


@pytest.mark.parametrize("value", ["0", "-1", "x"])
def test_watch_refuses_a_bad_interval(orch: Call, value: str) -> None:
    assert_error(orch("watch", "--interval", value), USAGE, "usage")


# notifications

TOPIC = "zq7Kx9mP2vLw4RtY8nBcHs3D"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> object:
    """Fail the test if anything reaches the real urlopen."""
    calls: list[object] = []

    def guard(*args: object, **kwargs: object) -> None:
        calls.append(args)
        raise AssertionError("network used")

    monkeypatch.setattr(urllib.request, "urlopen", guard)
    yield calls
    assert calls == []


def write_notify_config(env: Env, mode: int = 0o600, method: str = "ntfy") -> None:
    text = f'[notify]\nmethod = "{method}"\n'
    if method == "ntfy":
        text += f'topic = "{TOPIC}"\n'
    path = config.config_path(env.orch_home)
    write(path, text)
    path.chmod(mode)


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def fake(event: str, **kwargs: object) -> notify.Outcome:
        calls.append({"event": event, **kwargs})
        return notify.Outcome.SENT

    monkeypatch.setattr(notify, "send", fake)
    return calls


def assert_notified(
    call: dict[str, object], env: Env, event: str, *, task_id: str = TASK
) -> None:
    assert call["event"] == event
    assert call["target"] == TARGET
    assert call["task_id"] == task_id
    assert call["orch_home"] == env.orch_home
    assert call["home"] == env.home
    assert isinstance(call["settings"], config.Config)
    now = call["now"]
    assert now.tzinfo is not None
    assert call.get("dedup", True) is True
    assert "transport" not in call


@pytest.mark.usefixtures("target")
@pytest.mark.parametrize(
    ("scenario", "code", "event"),
    [
        ("blocked", 30, "blocked"),
        ("usage_limit", 40, "quota"),
        ("success", 0, None),
        ("partial", 10, None),
        ("report_missing", 20, None),
        ("out_of_scope", 20, None),
    ],
)
def test_run_notifies_only_when_it_moves_to_blocked(
    orch: Call,
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    sent: list[dict[str, object]],
    scenario: str,
    code: int,
    event: str | None,
) -> None:
    reserve(orch, env)
    monkeypatch.setenv("ORCH_FAKE_SCENARIO", scenario)
    result = orch("run", TASK)
    assert result[0] == code, result
    if event is None:
        assert sent == []
    else:
        assert len(sent) == 1
        assert_notified(sent[0], env, event)
        assert state_of(env) is TaskState.BLOCKED


@pytest.mark.usefixtures("target")
@pytest.mark.parametrize("scenario", ["blocked", "usage_limit"])
def test_a_failing_notification_does_not_change_the_run(
    orch: Call,
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: str,
) -> None:
    write_notify_config(env)
    calls: list[str] = []

    def failed(event: str, **kwargs: object) -> None:
        calls.append(event)
        raise RuntimeError(f"cannot reach https://ntfy.sh/{TOPIC}")

    monkeypatch.setattr(notify, "send", failed)
    reserve(orch, env)
    monkeypatch.setenv("ORCH_FAKE_SCENARIO", scenario)
    code, data = orch("run", TASK)
    err = capsys.readouterr().err
    assert len(calls) == 1
    assert code == (30 if scenario == "blocked" else 40)
    assert data["ok"] is True
    assert data["state"] == "blocked"
    assert state_of(env) is TaskState.BLOCKED
    assert TOPIC not in json.dumps(data)
    assert TOPIC not in err
    assert re.search("[가-힣]", err)


@pytest.mark.usefixtures("target")
def test_a_blocked_run_with_notifications_disabled_sends_nothing(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    reserve(orch, env)
    monkeypatch.setenv("ORCH_FAKE_SCENARIO", "blocked")
    code, data = orch("run", TASK)
    assert code == 30, data
    assert data["state"] == "blocked"


@pytest.mark.usefixtures("target")
def test_pr_notifies_a_user_check_and_reports_it(
    orch: Call,
    env: Env,
    pr_calls: list[dict[str, object]],
    sent: list[dict[str, object]],
) -> None:
    make_ready(orch, env)
    add_token_script(env)
    pr_calls[0]["report"][0] = PullRequestReport(
        state=TaskState.PR_OPEN, pr_url=URL, user_check=True
    )
    code, data = orch("pr", TASK)
    assert code == 0, data
    assert data["user_check"] is True
    assert len(sent) == 1
    assert_notified(sent[0], env, "needs_review")


@pytest.mark.usefixtures("target")
@pytest.mark.parametrize(
    "report",
    [
        PullRequestReport(state=TaskState.PR_OPEN, pr_url=URL),
        PullRequestReport(state=TaskState.CONFLICT, conflicts=("src/app.py",)),
        PullRequestReport(state=TaskState.JUDGING, detail="G2 failed"),
        PullRequestReport(state=TaskState.CONFLICT, user_check=True),
    ],
)
def test_pr_does_not_notify_without_an_open_user_check(
    orch: Call,
    env: Env,
    pr_calls: list[dict[str, object]],
    sent: list[dict[str, object]],
    report: PullRequestReport,
) -> None:
    make_ready(orch, env)
    add_token_script(env)
    pr_calls[0]["report"][0] = report
    code, data = orch("pr", TASK)
    assert code == (0 if report.state is TaskState.PR_OPEN else 20), data
    assert data["user_check"] is report.user_check
    assert sent == []


@pytest.mark.usefixtures("target")
def test_a_failing_needs_review_notification_does_not_change_the_pr(
    orch: Call,
    env: Env,
    pr_calls: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_notify_config(env)
    make_ready(orch, env)
    add_token_script(env)
    pr_calls[0]["report"][0] = PullRequestReport(
        state=TaskState.PR_OPEN, pr_url=URL, user_check=True
    )

    calls: list[str] = []

    def failed(event: str, **kwargs: object) -> None:
        calls.append(event)
        raise RuntimeError(f"{TOPIC} {TOKEN}")

    monkeypatch.setattr(notify, "send", failed)
    code, data = orch("pr", TASK)
    err = capsys.readouterr().err
    assert calls == ["needs_review"]
    assert code == 0, data
    assert data["state"] == "pr_open"
    assert data["pr_url"] == URL
    assert data["user_check"] is True
    assert state_of(env) is TaskState.READY
    assert TOPIC not in err and TOKEN not in err
    assert TOPIC not in json.dumps(data)


@pytest.mark.usefixtures("target")
def test_the_topic_is_removed_from_command_output(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_notify_config(env)

    def failed(*args: object, **kwargs: object) -> None:
        raise RuntimeError(f"leaked {TOPIC} here")

    monkeypatch.setattr(scheduler, "next_reservations", failed)
    result = orch("next")
    assert_error(result, INTERNAL, "internal")
    assert TOPIC not in json.dumps(result[1])
    assert "leaked [REDACTED] here" in str(result[1]["message"])


@pytest.mark.usefixtures("target")
def test_pr_issues_the_token_through_credentials(
    orch: Call,
    env: Env,
    pr_calls: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_ready(orch, env)
    issued: list[Path] = []

    def fake_issue(orch_home: Path, secrets: list[str]) -> str:
        issued.append(orch_home)
        secrets.append("fake-issued-token")
        return "fake-issued-token"

    monkeypatch.setattr(credentials, "issue_token", fake_issue)
    code, data = orch("pr", TASK)
    assert code == 0, data
    assert issued == [env.orch_home]
    assert pr_calls[1]["token"] == "fake-issued-token"


# orch notify test


class FakeUrlopen:
    def __init__(self, error: Exception | None = None) -> None:
        self.requests: list[tuple[urllib.request.Request, float]] = []
        self.error = error

    def __call__(self, request: urllib.request.Request, timeout: float) -> object:
        self.requests.append((request, timeout))
        if self.error is not None:
            raise self.error
        return io.BytesIO(b"{}")


def notify_test(
    orch: Call, monkeypatch: pytest.MonkeyPatch, fake: FakeUrlopen
) -> tuple[int, dict[str, object]]:
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return orch("notify", "test")


def test_notify_test_sends_without_deduplication_or_a_hub(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_notify_config(env)
    monkeypatch.delenv("ORCH_HUB")
    fake = FakeUrlopen()
    first = notify_test(orch, monkeypatch, fake)
    second = notify_test(orch, monkeypatch, fake)
    assert first == (0, {"ok": True, "result": "sent"})
    assert second == (0, {"ok": True, "result": "sent"})
    assert len(fake.requests) == 2
    request, timeout = fake.requests[0]
    assert request.full_url == "https://ntfy.sh/" + TOPIC
    assert request.get_method() == "POST"
    assert request.data == b"orch test"
    assert timeout == 5
    assert not config.notify_state_path(env.orch_home).exists()
    assert not config.state_db_path(env.orch_home).exists()


def test_notify_test_ignores_orch_target(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_notify_config(env)
    monkeypatch.setenv("ORCH_TARGET", TARGET)
    fake = FakeUrlopen()
    assert notify_test(orch, monkeypatch, fake) == (0, {"ok": True, "result": "sent"})
    assert fake.requests[0][0].data == b"orch test"


@pytest.mark.parametrize(
    ("setup", "result"),
    [
        ("missing", "disabled"),
        ("none", "disabled"),
        ("open", "refused_permissions"),
        ("failing", "failed"),
    ],
)
def test_notify_test_reports_why_nothing_was_sent(
    orch: Call,
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    setup: str,
    result: str,
) -> None:
    if setup == "none":
        write_notify_config(env, method="none")
    elif setup == "open":
        write_notify_config(env, 0o644)
    elif setup == "failing":
        write_notify_config(env)
    error = OSError(f"POST https://ntfy.sh/{TOPIC} failed")
    fake = FakeUrlopen(error if setup == "failing" else None)
    code, data = notify_test(orch, monkeypatch, fake)
    err = capsys.readouterr().err
    assert code == 20, data
    assert set(data) == {"ok", "error", "result", "message"}
    assert data["ok"] is False
    assert data["error"] == result
    assert data["result"] == result
    assert re.search("[가-힣]", str(data["message"]))
    assert TOPIC not in json.dumps(data)
    assert TOPIC not in err
    assert len(fake.requests) == (1 if setup == "failing" else 0)
    assert not config.notify_state_path(env.orch_home).exists()


def test_notify_test_with_a_bad_config_is_refused(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(config.config_path(env.orch_home), '[notify]\nmethod = "ntfy"\n')
    result = notify_test(orch, monkeypatch, FakeUrlopen())
    assert_error(result, USAGE, "refused")


@pytest.mark.parametrize("args", [("notify",), ("notify", "nosuch")])
def test_notify_needs_the_test_subcommand(orch: Call, args: tuple[str, ...]) -> None:
    assert_error(orch(*args), USAGE, "usage")
