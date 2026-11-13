import functools
import importlib.metadata
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tomllib
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest

from orch import (
    config,
    credentials,
    doctor,
    gate,
    gc,
    github,
    lifecycle,
    notify,
    prflow,
    render,
    review,
    sandbox,
    scheduler,
    service,
    store,
    waiter,
    watch,
)
from orch.cli import main
from orch.doctor import DoctorReport
from orch.gate import GateReport, GateStatus
from orch.gc import GcReport
from orch.github import GitHubError, PullRequestStatus
from orch.gitops import GitError, LeaseRejectedError
from orch.models import (
    CheckLevel,
    DoctorCheck,
    Effort,
    FailureReason,
    Grade,
    Lane,
    Outcome,
    Run,
    Task,
    TaskState,
)
from orch.prflow import PullRequestFlowError, PullRequestReport, UpdateReport
from orch.service import RestoreReport, ServiceReport
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
    lane: str = "B",
) -> str:
    dependencies = ", ".join(f'"{item}"' for item in depends_on)
    agent = (
        ['model = "gpt-6.1-sol"', 'effort = "medium"']
        if lane == "B"
        else ['model = "claude-sonnet-5-5"']
    )
    lines = [
        "+++",
        f'id = "{task_id}"',
        f'title = "{TITLE}"',
        f'target = "{target}"',
        'grade = "L2"',
        f'lane = "{lane}"',
        *agent,
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
        ("kill",),
        ("kill", TASK, "extra"),
        ("review", TASK),
        ("review", TASK, "--by"),
        ("review", TASK, "--by", "claude"),
        ("review", "--by", "codex"),
        ("review", TASK, "--by", "codex", "extra"),
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
    ("review", TASK, "--by", "codex"),
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


# review --by codex


def make_codex_reviewing(orch: Call, env: Env) -> Path:
    """A Claude implementation (lane C) waiting for its cross-review."""
    worktree = judge(orch, env, spec_text(review=True, lane="C"))
    code, data = orch("gate", TASK)
    assert code == 0, data
    assert data["state"] == "reviewing"
    return worktree


def review_paths(env: Env) -> dict[str, str]:
    logs = env.paths.logs / "review"
    return {
        "log_path": str(logs / f"{TASK}.1.log"),
        "output_path": str(logs / f"{TASK}.1.out"),
        "diff_path": str(env.paths.reviews / f"{TASK}.diff"),
    }


def test_the_review_model_is_policy_in_review_py() -> None:
    assert (review.REVIEW_MODEL, review.REVIEW_EFFORT) == ("gpt-6.1-sol", "medium")


@pytest.mark.usefixtures("target")
def test_a_codex_pass_is_saved_and_moves_to_ready(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_codex_reviewing(orch, env)
    monkeypatch.setenv("ORCH_FAKE_SCENARIO", "review_pass")

    def no_token(*args: object, **kwargs: object) -> str:
        raise AssertionError("review must not issue a token")

    monkeypatch.setattr(credentials, "issue_token", no_token)
    code, data = orch("review", TASK, "--by", "codex")
    assert code == 0, data
    assert data == {
        "ok": True,
        "task_id": TASK,
        "outcome": "saved",
        "state": "ready",
        "exit_code": 0,
        **review_paths(env),
    }
    assert state_of(env) is TaskState.READY
    saved = json.loads(env.paths.review_path(TASK).read_text(encoding="utf-8"))
    assert (saved["reviewer"], saved["verdict"]) == ("codex-review", "PASS")
    assert todo_state(env) == "ready"


@pytest.mark.usefixtures("target")
def test_a_codex_block_is_saved_with_exit_zero_and_moves_to_judging(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_codex_reviewing(orch, env)
    monkeypatch.setenv("ORCH_FAKE_SCENARIO", "review_block")
    code, data = orch("review", TASK, "--by", "codex")
    assert code == 0, data
    assert (data["outcome"], data["state"]) == ("saved", "judging")
    assert state_of(env) is TaskState.JUDGING
    assert todo_state(env) == "judging"


@pytest.mark.usefixtures("target")
@pytest.mark.parametrize(
    ("scenario", "outcome", "expected"),
    [
        ("review_invalid", "invalid", 20),
        ("review_fail", "failed", 20),
        ("review_usage_limit", "quota", 40),
    ],
)
def test_a_review_that_is_not_saved_keeps_reviewing(
    orch: Call,
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    outcome: str,
    expected: int,
) -> None:
    make_codex_reviewing(orch, env)
    monkeypatch.setenv("ORCH_FAKE_SCENARIO", scenario)
    code, data = orch("review", TASK, "--by", "codex", unchecked=True)
    assert code == expected, data
    assert data["ok"] is True
    assert (data["outcome"], data["state"]) == (outcome, "reviewing")
    assert state_of(env) is TaskState.REVIEWING
    assert not env.paths.review_path(TASK).exists()


@pytest.mark.usefixtures("target")
def test_the_review_runs_codex_with_the_policy_model_and_effort(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_codex_reviewing(orch, env)
    args_file = env.home / "review-args.json"
    monkeypatch.setenv("ORCH_FAKE_SCENARIO", "review_args")
    monkeypatch.setenv("ORCH_FAKE_ARGS_FILE", str(args_file))
    code, data = orch("review", TASK, "--by", "codex")
    assert code == 0, data
    argv = json.loads(args_file.read_text(encoding="utf-8"))["argv"]
    assert argv[argv.index("-m") + 1] == "gpt-6.1-sol"
    assert "model_reasoning_effort=medium" in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"


@pytest.mark.usefixtures("target")
@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("saved", 0),
        ("invalid", 20),
        ("failed", 20),
        ("timeout", 20),
        ("quota", 40),
    ],
)
def test_each_outcome_has_its_exit_code_and_the_call_is_exact(
    orch: Call,
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    expected: int,
) -> None:
    make_codex_reviewing(orch, env)
    calls: list[dict[str, object]] = []

    def fake(
        conn: sqlite3.Connection, task_id: str, **options: object
    ) -> review.ReviewRun:
        calls.append({"task_id": task_id, **options})
        paths = env.paths
        return review.ReviewRun(
            task_id=task_id,
            outcome=review.ReviewOutcome(outcome),
            state=TaskState.REVIEWING,
            exit_code=None if outcome == "timeout" else 1,
            log_path=paths.logs / "review" / f"{task_id}.1.log",
            output_path=paths.logs / "review" / f"{task_id}.1.out",
            diff_path=paths.reviews / f"{task_id}.diff",
        )

    monkeypatch.setattr(review, "run_codex_review", fake)
    code, data = orch("review", TASK, "--by", "codex", unchecked=True)
    assert code == expected, data
    assert data == {
        "ok": True,
        "task_id": TASK,
        "outcome": outcome,
        "state": "reviewing",
        "exit_code": None if outcome == "timeout" else 1,
        **review_paths(env),
    }
    assert len(calls) == 1
    call = calls[0]
    assert set(call) == {
        "task_id",
        "target",
        "config",
        "paths",
        "environ",
        "model",
        "effort",
        "home",
        "orch_home",
    }
    assert call["task_id"] == TASK
    assert call["model"] == review.REVIEW_MODEL
    assert call["effort"] == review.REVIEW_EFFORT
    assert call["paths"] == env.paths
    assert call["environ"] is os.environ
    assert call["home"] == env.home
    assert call["orch_home"] == env.orch_home
    assert call["target"].name == TARGET
    assert isinstance(call["config"], config.Config)


@pytest.mark.usefixtures("target")
def test_codex_does_not_review_a_codex_implementation(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_reviewing(orch, env)
    monkeypatch.setenv("ORCH_FAKE_SCENARIO", "review_pass")
    assert_error(
        orch("review", TASK, "--by", "codex", unchecked=True), USAGE, "refused"
    )
    assert state_of(env) is TaskState.REVIEWING
    assert not (env.paths.logs / "review").exists()


@pytest.mark.usefixtures("target")
def test_codex_review_needs_a_reviewing_task(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    judge(orch, env, spec_text(review=True, lane="C"))
    monkeypatch.setenv("ORCH_FAKE_SCENARIO", "review_pass")
    assert_error(
        orch("review", TASK, "--by", "codex", unchecked=True), USAGE, "refused"
    )
    assert state_of(env) is TaskState.JUDGING


@pytest.mark.usefixtures("target")
def test_codex_review_without_the_hub_runs_nothing(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_codex_reviewing(orch, env)
    monkeypatch.setenv("ORCH_FAKE_SCENARIO", "review_pass")
    monkeypatch.delenv("ORCH_HUB")
    assert_error(orch("review", TASK, "--by", "codex", unchecked=True), USAGE, "usage")
    assert state_of(env) is TaskState.REVIEWING
    assert not (env.paths.logs / "review").exists()
    assert not (env.paths.reviews / f"{TASK}.diff").exists()


@pytest.mark.usefixtures("target")
def test_review_save_still_works_without_the_hub(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_reviewing(orch, env)
    monkeypatch.delenv("ORCH_HUB")
    code, data = orch("review", "save", TASK, stdin=review_text())
    assert code == 0, data
    assert data == {"ok": True, "task_id": TASK, "state": "ready"}


@pytest.mark.usefixtures("target")
def test_codex_review_refuses_a_task_of_another_session_target(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_codex_reviewing(orch, env)
    monkeypatch.setenv("ORCH_FAKE_SCENARIO", "review_pass")
    monkeypatch.setenv("ORCH_TARGET", "other")
    assert_error(
        orch("review", TASK, "--by", "codex", unchecked=True), USAGE, "refused"
    )
    assert state_of(env) is TaskState.REVIEWING


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
        "paused": [],
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
    assert orch("status") == (
        0,
        {"ok": True, "tasks": [], "blocked_chains": [], "paused": []},
    )


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


# orch open and orch watch --panels (M13)

from orch import cli as cli_module  # noqa: E402
from orch import launcher  # noqa: E402
from orch.terminal import (  # noqa: E402
    MainPaneStatus,
    PaneRecord,
    TerminalError,
    record_path,
    save_record,
)

CLAUDE_BIN = "/opt/claude/bin/claude"


class FakeTerminal:
    def __init__(self, session: str, record: Path, *, exists: bool = False) -> None:
        self.session = session
        self.record = record
        self.exists = exists
        self.calls: list[tuple[object, ...]] = []
        self.next_id = 1
        self.fail_open = False
        self.pane_status = MainPaneStatus.ALIVE

    def _pane(self) -> str:
        pane = f"%{self.next_id}"
        self.next_id += 1
        return pane

    def has_session(self) -> bool:
        self.calls.append(("has_session",))
        return self.exists

    def open_session(self, *, cwd: Path, env: dict[str, str], command: object) -> str:
        self.calls.append(("open", cwd, dict(env), list(command)))  # type: ignore[call-overload]
        if self.fail_open:
            raise TerminalError("duplicate session")
        return self._pane()

    def terminal_split(
        self,
        command: object,
        *,
        target: str | None = None,
        vertical: bool = False,
        percent: int | None = None,
    ) -> str:
        self.calls.append(("split", list(command), target, vertical, percent))  # type: ignore[call-overload]
        return self._pane()

    def main_pane_status(self, claude: object) -> MainPaneStatus:
        self.calls.append(("main_pane_status", claude))
        return self.pane_status

    def close_pane(self, pane: str) -> None:
        self.calls.append(("close", pane))

    def live_panes(self) -> frozenset[str]:
        self.calls.append(("live",))
        return frozenset()

    def attach_argv(self, environ: object) -> list[str]:
        return ["tmux", "attach-session", "-t", f"={self.session}"]


class Terminals(list[FakeTerminal]):
    """Terminals made by the patched factory, with options for the next one."""

    def __init__(self) -> None:
        super().__init__()
        self.options: dict[str, bool] = {"exists": False, "fail_open": False}
        self.pane_status = MainPaneStatus.ALIVE


@pytest.fixture
def terminals(monkeypatch: pytest.MonkeyPatch) -> Terminals:
    made = Terminals()

    def factory(session: str, record: Path) -> FakeTerminal:
        terminal = FakeTerminal(session, record, exists=made.options["exists"])
        terminal.fail_open = made.options["fail_open"]
        terminal.pane_status = made.pane_status
        made.append(terminal)
        return terminal

    monkeypatch.setattr(cli_module, "_terminal", factory)
    monkeypatch.setattr(cli_module, "_interactive", lambda: False)
    return made


OPEN_AGENT = "---\nname: probe\ndescription: Probe.\ntools: Read\n---\nBody.\n"
OPEN_AGENTS_JSON = json.dumps(
    {"probe": {"description": "Probe.", "prompt": "Body.", "tools": ["Read"]}},
    ensure_ascii=False,
    sort_keys=True,
)


@pytest.fixture
def openable(env: Env, monkeypatch: pytest.MonkeyPatch, target: None) -> None:
    write(env.hub / "config" / "settings.json", "{}\n")
    write(env.hub / "config" / "main.md", "# Main rules\n")
    write(env.hub / "config" / "agents" / "probe.md", OPEN_AGENT)
    write(env.orch_home / "config", f'[agents]\nclaude = "{CLAUDE_BIN}"\n')
    monkeypatch.setattr(launcher, "probe_claude_version", lambda claude: "2.1.319")


def open_options(terminals: list[FakeTerminal]) -> dict[str, bool]:
    assert isinstance(terminals, Terminals)
    return terminals.options


@pytest.mark.usefixtures("openable")
def test_open_starts_the_main_session_and_panels(
    orch: Call, env: Env, repo: Path, terminals: list[FakeTerminal]
) -> None:
    code, data = orch("open", TARGET)
    assert code == 0, data
    assert data == {
        "ok": True,
        "target": TARGET,
        "session": f"orch-{TARGET}",
        "opened": True,
        "warnings": [],
        "attach": False,
    }
    [terminal] = terminals
    assert terminal.session == f"orch-{TARGET}"
    assert terminal.record == record_path(env.paths.root)
    assert terminal.calls == [
        ("has_session",),
        (
            "open",
            repo,
            {
                "ORCH_TARGET": TARGET,
                "ORCH_HUB": str(env.hub),
                "ORCH_HOME": str(env.orch_home),
            },
            [
                CLAUDE_BIN,
                "--model",
                "claude-opus-5-5",
                "--settings",
                str(env.hub / "config" / "settings.json"),
                "--append-system-prompt-file",
                str(env.hub / "config" / "main.md"),
                "--agents",
                OPEN_AGENTS_JSON,
                "--add-dir",
                str(env.paths.root),
                "--remote-control",
            ],
        ),
        ("split", ["orch", "watch", "--panels"], "%1", False, 40),
        (
            "split",
            ["tail", "-F", str(env.paths.logs / "subagents.log")],
            "%2",
            True,
            30,
        ),
    ]
    assert env.paths.logs.is_dir()
    assert not config.state_db_path(env.orch_home).exists()


@pytest.mark.usefixtures("openable")
def test_open_reattaches_to_an_existing_session(
    orch: Call, terminals: list[FakeTerminal]
) -> None:
    open_options(terminals)["exists"] = True
    code, data = orch("open", TARGET)
    assert code == 0, data
    assert data["opened"] is False
    assert data["warnings"] == []
    assert terminals[0].calls == [
        ("has_session",),
        ("main_pane_status", Path(CLAUDE_BIN)),
    ]


@pytest.mark.usefixtures("openable")
def test_open_reports_version_and_dirty_warnings(
    orch: Call,
    repo: Path,
    terminals: list[FakeTerminal],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(launcher, "probe_claude_version", lambda claude: "2.2.0")
    write(repo / "scratch.txt")
    code, data = orch("open", TARGET)
    assert code == 0, data
    warnings = data["warnings"]
    assert isinstance(warnings, list) and len(warnings) == 2
    assert warnings[0] == launcher.version_warning("2.2.0")


@pytest.mark.usefixtures("openable")
def test_open_passes_the_probed_version_and_claude_path(
    orch: Call, terminals: list[FakeTerminal], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Path] = []

    def probe(claude: Path) -> None:
        seen.append(claude)
        return None

    monkeypatch.setattr(launcher, "probe_claude_version", probe)
    code, data = orch("open", TARGET)
    assert code == 0, data
    assert seen == [Path(CLAUDE_BIN)]
    assert data["warnings"] == [launcher.version_warning(None)]


@pytest.mark.usefixtures("openable")
def test_open_attaches_after_printing_json(
    terminals: list[FakeTerminal],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[str, list[str], str]] = []

    def execvp(file: str, argv: list[str]) -> None:
        calls.append((file, list(argv), capsys.readouterr().out))

    monkeypatch.setattr(cli_module, "_interactive", lambda: True)
    monkeypatch.setattr(os, "execvp", execvp)
    assert main(["open", TARGET]) == 0
    [(file, argv, printed)] = calls
    assert file == "tmux"
    assert argv == ["tmux", "attach-session", "-t", f"=orch-{TARGET}"]
    data = json.loads(printed)
    assert data["ok"] is True and data["attach"] is True


@pytest.mark.usefixtures("openable")
def test_open_attach_failure_exits_70(
    terminals: list[FakeTerminal],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def execvp(file: str, argv: list[str]) -> None:
        raise OSError("no tmux")

    monkeypatch.setattr(cli_module, "_interactive", lambda: True)
    monkeypatch.setattr(os, "execvp", execvp)
    assert main(["open", TARGET]) == INTERNAL
    captured = capsys.readouterr()
    assert json.loads(captured.out)["attach"] is True
    assert captured.err.strip()


@pytest.mark.usefixtures("openable")
def test_open_no_attach_skips_exec(
    orch: Call, terminals: list[FakeTerminal], monkeypatch: pytest.MonkeyPatch
) -> None:
    def execvp(file: str, argv: list[str]) -> None:
        raise AssertionError("must not attach")

    monkeypatch.setattr(cli_module, "_interactive", lambda: True)
    monkeypatch.setattr(os, "execvp", execvp)
    code, data = orch("open", TARGET, "--no-attach")
    assert code == 0, data
    assert data["attach"] is False


@pytest.mark.usefixtures("openable")
def test_open_refuses_without_a_claude_path(
    orch: Call, env: Env, terminals: list[FakeTerminal]
) -> None:
    write(env.orch_home / "config", "")
    assert_error(orch("open", TARGET), USAGE, "refused")
    assert all(call[0] != "open" for t in terminals for call in t.calls)


@pytest.mark.usefixtures("openable")
def test_open_refuses_an_unregistered_target(
    orch: Call, terminals: list[FakeTerminal]
) -> None:
    assert_error(orch("open", "other"), USAGE, "refused")
    assert terminals == []


@pytest.mark.usefixtures("openable")
def test_open_refuses_without_hook_settings(
    orch: Call, env: Env, terminals: list[FakeTerminal]
) -> None:
    (env.hub / "config" / "settings.json").unlink()
    assert_error(orch("open", TARGET), USAGE, "refused")
    assert all(call[0] != "open" for t in terminals for call in t.calls)


@pytest.mark.usefixtures("openable")
def test_open_refuses_without_main_rules(
    orch: Call, env: Env, terminals: list[FakeTerminal]
) -> None:
    (env.hub / "config" / "main.md").unlink()
    assert_error(orch("open", TARGET), USAGE, "refused")
    assert all(call[0] != "open" for t in terminals for call in t.calls)


@pytest.mark.usefixtures("openable")
def test_open_refuses_a_malformed_agent_definition(
    orch: Call, env: Env, terminals: list[FakeTerminal]
) -> None:
    write(env.hub / "config" / "agents" / "probe.md", "name: probe\n")
    assert_error(orch("open", TARGET), USAGE, "refused")
    assert all(call[0] != "open" for t in terminals for call in t.calls)


@pytest.mark.usefixtures("openable")
def test_open_requires_the_hub(
    orch: Call, terminals: list[FakeTerminal], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ORCH_HUB")
    assert_error(orch("open", TARGET), USAGE, "usage")
    assert terminals == []


@pytest.mark.usefixtures("openable")
def test_open_refuses_a_terminal_failure(
    orch: Call, terminals: list[FakeTerminal]
) -> None:
    open_options(terminals)["fail_open"] = True
    assert_error(orch("open", TARGET), USAGE, "refused")


def test_the_real_terminal_factory(tmp_path: Path) -> None:
    terminal = cli_module._terminal("orch-x", tmp_path / "terminal.json")
    assert isinstance(terminal, cli_module.TmuxTerminal)


def test_watch_panels_requires_tmux_and_a_target(
    orch: Call, monkeypatch: pytest.MonkeyPatch, terminals: list[FakeTerminal]
) -> None:
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("ORCH_TARGET", TARGET)
    assert_error(orch("watch", "--panels", "--once", unchecked=True), USAGE, "usage")
    monkeypatch.setenv("TMUX", "/tmp/tmux-1/default,1,0")
    monkeypatch.delenv("ORCH_TARGET")
    assert_error(orch("watch", "--panels", "--once", unchecked=True), USAGE, "usage")
    assert terminals == []


def run_panels_tick(
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    terminals: list[FakeTerminal],
    pane: str | None,
) -> FakeTerminal:
    monkeypatch.setenv("TMUX", "/tmp/tmux-1/default,1,0")
    monkeypatch.setenv("ORCH_TARGET", TARGET)
    if pane is None:
        monkeypatch.delenv("TMUX_PANE", raising=False)
    else:
        monkeypatch.setenv("TMUX_PANE", pane)
    seen: dict[str, object] = {}

    def fake(frame: object, *, interval_sec: int, once: bool, tick: object) -> int:
        seen.update(interval_sec=interval_sec, once=once)
        tick()  # type: ignore[operator]
        return 0

    monkeypatch.setattr(watch, "watch", fake)
    assert main(["watch", "--panels", "--once"]) == 0
    assert seen == {"interval_sec": 2, "once": True}
    [terminal] = terminals
    assert terminal.session == f"orch-{TARGET}"
    assert terminal.record == record_path(env.paths.root)
    return terminal


def test_watch_panels_opens_a_panel_for_a_running_task(
    env: Env, monkeypatch: pytest.MonkeyPatch, terminals: list[FakeTerminal]
) -> None:
    insert(env, state=TaskState.RUNNING)
    insert(env, "other-run", state=TaskState.RUNNING, target="other")
    insert(env, "no-log", state=TaskState.RUNNING)
    insert(env, "judged", state=TaskState.JUDGING)
    write(env.paths.logs / f"{TASK}.1.log")
    write(env.paths.logs / f"{TASK}.2.log")
    write(env.paths.logs / "judged.1.log")
    save_record(
        record_path(env.paths.root),
        PaneRecord(session=f"orch-{TARGET}", main_pane="%0", created=("%5",)),
    )
    terminal = run_panels_tick(env, monkeypatch, terminals, "%5")
    splits = [call for call in terminal.calls if call[0] == "split"]
    assert splits == [
        (
            "split",
            ["tail", "-F", str(env.paths.logs / f"{TASK}.2.log")],
            "%5",
            True,
            None,
        )
    ]
    assert watch.load_panel_map(env.paths.root / "panels.json") == {TASK: "%1"}


def test_watch_panels_anchor_is_none_for_an_unrecorded_pane(
    env: Env, monkeypatch: pytest.MonkeyPatch, terminals: list[FakeTerminal]
) -> None:
    insert(env, state=TaskState.RUNNING)
    write(env.paths.logs / f"{TASK}.1.log")
    save_record(
        record_path(env.paths.root),
        PaneRecord(session=f"orch-{TARGET}", main_pane="%0", created=("%5",)),
    )
    terminal = run_panels_tick(env, monkeypatch, terminals, "%9")
    splits = [call for call in terminal.calls if call[0] == "split"]
    assert [call[2] for call in splits] == [None]


def test_watch_panels_anchor_is_none_without_a_record(
    env: Env, monkeypatch: pytest.MonkeyPatch, terminals: list[FakeTerminal]
) -> None:
    insert(env, state=TaskState.RUNNING)
    write(env.paths.logs / f"{TASK}.1.log")
    terminal = run_panels_tick(env, monkeypatch, terminals, "%0")
    splits = [call for call in terminal.calls if call[0] == "split"]
    assert [call[2] for call in splits] == [None]


def test_watch_panels_accepts_the_main_pane_as_anchor(
    env: Env, monkeypatch: pytest.MonkeyPatch, terminals: list[FakeTerminal]
) -> None:
    insert(env, state=TaskState.RUNNING)
    write(env.paths.logs / f"{TASK}.1.log")
    save_record(
        record_path(env.paths.root),
        PaneRecord(session=f"orch-{TARGET}", main_pane="%0"),
    )
    terminal = run_panels_tick(env, monkeypatch, terminals, "%0")
    splits = [call for call in terminal.calls if call[0] == "split"]
    assert [call[2] for call in splits] == ["%0"]


# Kill


def make_running(env: Env, *, finished: bool = False) -> int:
    """Insert a running task with one run, finished or not, and no process."""
    config.ensure_target(env.orch_home, TARGET, "owner/sample")
    insert(env, state=TaskState.RUNNING)
    conn = connect(env)
    try:
        with store.transaction(conn):
            run_id = store.insert_run(
                conn,
                Run(
                    task_id=TASK,
                    attempt=1,
                    lane=Lane.B,
                    model="gpt-6.1-sol",
                    started_at="2026-01-01T00:00:00+00:00",
                ),
            )
        if finished:
            store.finish_run(
                conn,
                run_id,
                ended_at="2026-01-01T00:01:00+00:00",
                exit_code=0,
                outcome=Outcome.DONE,
                failure_reason=None,
            )
    finally:
        conn.close()
    return run_id


@pytest.fixture
def kill_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Record each kill_task call and run the real one with a short settle."""
    calls: list[dict[str, object]] = []
    real = lifecycle.kill_task

    def short(
        conn: sqlite3.Connection, task_id: str, **options: object
    ) -> lifecycle.KillReport:
        calls.append({"task_id": task_id, **options})
        return real(conn, task_id, **{**options, "settle_sec": 0.2, "poll_sec": 0.05})

    monkeypatch.setattr(lifecycle, "kill_task", short)
    return calls


def test_kill_ends_a_run_without_a_runner_and_reports_the_abort(
    orch: Call,
    env: Env,
    kill_calls: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_running(env)
    # Kill reads no target configuration and issues no token.
    monkeypatch.delenv("ORCH_HUB")

    def no_token(*args: object, **kwargs: object) -> str:
        raise AssertionError("kill must not issue a token")

    monkeypatch.setattr(credentials, "issue_token", no_token)
    code, data = orch("kill", TASK)
    assert code == 0, data
    assert data == {
        "ok": True,
        "task_id": TASK,
        "state": "judging",
        "outcome": "FAILED",
        "failure_reason": "aborted",
        "finished_by": "kill",
        "signalled": False,
    }
    assert state_of(env) == TaskState.JUDGING
    report = json.loads(
        (env.paths.results / f"{TASK}.json").read_text(encoding="utf-8")
    )
    assert (report["status"], report["failure_reason"]) == ("FAILED", "aborted")
    assert todo_state(env) == "judging"
    # The command passes exactly the paths and the environment, no timing options.
    assert len(kill_calls) == 1
    call = kill_calls[0]
    assert set(call) == {"task_id", "paths", "environ"}
    assert call["task_id"] == TASK
    assert call["paths"] == env.paths
    assert call["environ"] is os.environ


def test_kill_reports_what_the_runner_recorded(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_running(env)

    def runner_won(
        conn: sqlite3.Connection, task_id: str, **options: object
    ) -> lifecycle.KillReport:
        return lifecycle.KillReport(
            task_id=task_id,
            state=TaskState.JUDGING,
            outcome=Outcome.DONE,
            failure_reason=None,
            finished_by="runner",
            signalled=True,
        )

    monkeypatch.setattr(lifecycle, "kill_task", runner_won)
    code, data = orch("kill", TASK, unchecked=True)
    assert code == 0, data
    assert data == {
        "ok": True,
        "task_id": TASK,
        "state": "judging",
        "outcome": "DONE",
        "failure_reason": None,
        "finished_by": "runner",
        "signalled": True,
    }


def test_kill_passes_the_failure_reason_as_text(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_running(env)

    def timed_out(
        conn: sqlite3.Connection, task_id: str, **options: object
    ) -> lifecycle.KillReport:
        return lifecycle.KillReport(
            task_id=task_id,
            state=TaskState.JUDGING,
            outcome=Outcome.FAILED,
            failure_reason=FailureReason.TIMEOUT,
            finished_by="runner",
            signalled=False,
        )

    monkeypatch.setattr(lifecycle, "kill_task", timed_out)
    code, data = orch("kill", TASK, unchecked=True)
    assert code == 0, data
    assert (data["outcome"], data["failure_reason"]) == ("FAILED", "timeout")


def test_kill_refuses_a_task_that_is_not_running(
    orch: Call, env: Env, kill_calls: list[dict[str, object]]
) -> None:
    config.ensure_target(env.orch_home, TARGET, "owner/sample")
    insert(env, state=TaskState.JUDGING)
    assert_error(orch("kill", TASK, unchecked=True), USAGE, "refused")
    assert state_of(env) == TaskState.JUDGING
    assert todo_state(env) == "judging"


def test_kill_refuses_a_running_task_whose_run_finished(
    orch: Call, env: Env, kill_calls: list[dict[str, object]]
) -> None:
    run_id = make_running(env, finished=True)
    assert_error(orch("kill", TASK, unchecked=True), USAGE, "refused")
    assert state_of(env) == TaskState.RUNNING
    conn = connect(env)
    try:
        run = store.get_run(conn, run_id)
    finally:
        conn.close()
    assert run is not None and run.kill_requested_at is None
    assert not (env.paths.results / f"{TASK}.json").exists()


def test_kill_refuses_an_unknown_task(
    orch: Call, env: Env, kill_calls: list[dict[str, object]]
) -> None:
    assert_error(orch("kill", "missing", unchecked=True), USAGE, "refused")
    assert kill_calls == []


def test_kill_refuses_a_task_of_another_session_target(
    orch: Call,
    env: Env,
    kill_calls: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_running(env)
    monkeypatch.setenv("ORCH_TARGET", "other")
    assert_error(orch("kill", TASK, unchecked=True), USAGE, "refused")
    assert kill_calls == []
    assert state_of(env) == TaskState.RUNNING


# doctor

TOPIC = "abcdefghijklmnopqrstuvwxyz012345"
NO_REPORT = {"checks": [], "killed": [], "paused": [], "errors": [], "gc": None}


def fake_doctor(
    monkeypatch: pytest.MonkeyPatch, report: DoctorReport | None = None
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def run(conn: sqlite3.Connection, **kwargs: object) -> DoctorReport:
        calls.append(kwargs)
        return DoctorReport() if report is None else report

    monkeypatch.setattr(doctor, "run_doctor", run)
    return calls


@pytest.mark.usefixtures("target")
def test_doctor_hands_over_settings_targets_and_thresholds(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(env.orch_home / "config", "[doctor]\ndisk_warn_gb = 20\ndisk_fail_gb = 5\n")
    calls = fake_doctor(monkeypatch)
    assert orch("doctor") == (0, {"ok": True, **NO_REPORT})
    [call] = calls
    settings = call["settings"]
    assert isinstance(settings, config.Config)
    assert settings.disk_warn_gb == 20
    assert list(call["targets"]) == [TARGET]  # type: ignore[call-overload]
    assert call["orch_home"] == env.orch_home
    assert call["home"] == env.home
    assert call["environ"] is os.environ
    assert call["disk_warn_gb"] == 20
    assert call["disk_fail_gb"] == 5
    assert call["terminal"] is cli_module._terminal
    now = call["now"]
    assert isinstance(now, datetime)
    assert now.utcoffset() is not None


@pytest.mark.usefixtures("target")
def test_doctor_uses_the_default_thresholds(
    orch: Call, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_doctor(monkeypatch)
    assert orch("doctor")[0] == 0
    assert (calls[0]["disk_warn_gb"], calls[0]["disk_fail_gb"]) == (10, 2)


def test_doctor_needs_the_hub(orch: Call, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_doctor(monkeypatch)
    monkeypatch.delenv("ORCH_HUB")
    assert_error(orch("doctor"), USAGE, "usage")
    assert calls == []


@pytest.mark.usefixtures("target")
def test_doctor_refuses_an_invalid_threshold(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_doctor(monkeypatch)
    write(env.orch_home / "config", "[doctor]\ndisk_warn_gb = 0\n")
    assert_error(orch("doctor"), USAGE, "refused")
    assert calls == []


@pytest.mark.usefixtures("target")
@pytest.mark.parametrize("value", ["relative/tailscale", "./tailscale", "", "   "])
def test_doctor_refuses_a_tailscale_path_that_is_not_absolute(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    calls = fake_doctor(monkeypatch)
    write(env.orch_home / "config", f'[doctor]\ntailscale = "{value}"\n')
    assert_error(orch("doctor"), USAGE, "refused")
    assert calls == []


@pytest.mark.usefixtures("target")
def test_a_missing_tailscale_file_only_warns_on_network(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file is not checked in config: the check itself reports it, with exit 0."""
    tools = env.home / "tools"
    claude = add_script(tools / "claude", output='{"loggedIn": true}')
    codex = add_script(tools / "codex")
    add_token_script(env)
    write(
        env.orch_home / "config",
        f'[agents]\nclaude = "{claude}"\ncodex = "{codex}"\n'
        "[doctor]\ndisk_warn_gb = 1\ndisk_fail_gb = 1\n"
        f'tailscale = "{tools / "no-such-tailscale"}"\n',
    )
    code, data = orch("doctor")
    assert code == 0, data
    levels = {
        check["item"]: (check["level"], check["detail"])
        for check in data["checks"]  # type: ignore[attr-defined]
    }
    assert levels["network"] == ("warn", "FileNotFoundError")
    assert levels["codex_auth"] == ("ok", None)
    assert data["paused"] == []
    assert data["errors"] == []


@pytest.mark.usefixtures("target")
def test_doctor_checks_every_target_inside_a_session(
    orch: Call, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_doctor(monkeypatch)
    monkeypatch.setenv("ORCH_TARGET", "other")
    assert orch("doctor")[0] == 0
    assert list(calls[0]["targets"]) == [TARGET]  # type: ignore[call-overload]


@pytest.mark.usefixtures("target")
def test_a_failing_check_is_not_a_doctor_failure(
    orch: Call, monkeypatch: pytest.MonkeyPatch
) -> None:
    check = DoctorCheck(
        target="", item="codex_auth", level=CheckLevel.FAIL, detail="exit=1", at="t"
    )
    fake_doctor(monkeypatch, DoctorReport(checks=(check,), paused=("codex_auth",)))
    code, data = orch("doctor")
    assert code == 0, data
    assert data["ok"] is True
    assert data["paused"] == ["codex_auth"]
    assert data["checks"] == [
        {
            "target": "",
            "item": "codex_auth",
            "level": "fail",
            "detail": "exit=1",
            "at": "t",
        }
    ]


@pytest.mark.usefixtures("target")
def test_an_error_inside_doctor_exits_70_and_keeps_the_report(
    orch: Call, monkeypatch: pytest.MonkeyPatch
) -> None:
    check = DoctorCheck(
        target="", item="network", level=CheckLevel.OK, detail=None, at="t"
    )
    fake_doctor(monkeypatch, DoctorReport(checks=(check,), errors=("disk OSError",)))
    result = orch("doctor")
    assert_error(result, INTERNAL, "internal")
    assert result[1]["errors"] == ["disk OSError"]
    assert len(result[1]["checks"]) == 1  # type: ignore[arg-type]


@pytest.mark.usefixtures("target")
def test_doctor_output_is_redacted(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(env.orch_home / "config", f'[notify]\nmethod = "ntfy"\ntopic = "{TOPIC}"\n')
    fake_doctor(monkeypatch, DoctorReport(paused=(TOPIC,)))
    code, data = orch("doctor")
    assert code == 0, data
    assert TOPIC not in json.dumps(data)


@pytest.mark.usefixtures("target")
def test_doctor_refreshes_todo_only_after_a_kill(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    config.ensure_target(env.orch_home, TARGET, "owner/sample")
    insert(env)
    fake_doctor(monkeypatch)
    assert orch("doctor")[0] == 0
    assert not env.paths.todo.exists()
    fake_doctor(monkeypatch, DoctorReport(killed=(TASK,)))
    assert orch("doctor")[0] == 0
    assert TASK in env.paths.todo.read_text(encoding="utf-8")


def healthy_run(argv: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, "{}", "")


@pytest.mark.usefixtures("target")
def test_a_real_doctor_kill_moves_the_task_and_refreshes_todo(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_running(env)
    real = doctor.run_doctor
    fast_kill = functools.partial(
        lifecycle.kill_task, settle_sec=0, grace_sec=0, poll_sec=0
    )

    def run(conn: sqlite3.Connection, **kwargs: object) -> DoctorReport:
        return real(
            conn,
            run=healthy_run,
            free_bytes=lambda path: 50 * 10**9,
            kill=fast_kill,
            **kwargs,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(doctor, "run_doctor", run)
    code, data = orch("doctor")
    assert code == 0, data
    assert data["killed"] == [TASK]
    assert state_of(env) is TaskState.JUDGING
    assert "judging" in env.paths.todo.read_text(encoding="utf-8")


def add_script(path: Path, *, output: str = "", code: int = 0) -> Path:
    write(path, f"#!/bin/sh\nprintf '%s' '{output}'\nexit {code}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.mark.usefixtures("target")
def test_a_real_doctor_pass_pauses_dispatch_until_the_login_returns(
    orch: Call, env: Env
) -> None:
    """Commands run for real through config paths; only the disk limits are lowered."""
    tools = env.home / "tools"
    claude = add_script(tools / "claude", output='{"loggedIn": true}')
    codex = add_script(tools / "codex", code=1)
    tailscale = add_script(
        tools / "tailscale",
        output='{"BackendState": "Running", "Self": {"Online": true}}',
    )
    add_token_script(env)
    write(
        env.orch_home / "config",
        f'[agents]\nclaude = "{claude}"\ncodex = "{codex}"\n'
        f'[doctor]\ndisk_warn_gb = 1\ndisk_fail_gb = 1\ntailscale = "{tailscale}"\n',
    )

    code, data = orch("doctor")
    assert code == 0, data
    levels = {check["item"]: check["level"] for check in data["checks"]}  # type: ignore[attr-defined]
    assert levels == {
        "claude_auth": "ok",
        "codex_auth": "fail",
        "github_app": "ok",
        "disk": "ok",
        "network": "ok",
    }
    assert data["paused"] == ["codex_auth"]
    assert TOKEN not in json.dumps(data)
    assert orch("next")[1]["paused"] == ["codex_auth"]
    [pause] = orch("status")[1]["paused"]  # type: ignore[misc]
    assert (pause["reason"], pause["note"]) == ("codex_auth", "exit=1")

    add_script(codex)
    code, data = orch("doctor")
    assert code == 0, data
    assert data["paused"] == []
    assert orch("next")[1]["paused"] == []
    assert orch("status")[1]["paused"] == []


# gc


def fake_collect(
    monkeypatch: pytest.MonkeyPatch, report: GcReport | None = None
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def collect(conn: sqlite3.Connection, **kwargs: object) -> GcReport:
        calls.append(kwargs)
        return GcReport(dry_run=False) if report is None else report

    monkeypatch.setattr(gc, "collect", collect)
    return calls


@pytest.mark.usefixtures("target")
@pytest.mark.parametrize(
    ("args", "dry_run"), [((), False), (("--dry-run",), True)], ids=["real", "dry"]
)
def test_gc_hands_over_targets_and_retention(
    orch: Call,
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    args: tuple[str, ...],
    dry_run: bool,
) -> None:
    write(env.orch_home / "config", "retention_days = 7\n")
    report = GcReport(dry_run=dry_run, removed_files=("sample/logs/a.1.log",))
    calls = fake_collect(monkeypatch, report)
    code, data = orch("gc", *args)
    assert code == 0, data
    assert data["ok"] is True
    assert data["dry_run"] is dry_run
    assert data["removed_files"] == ["sample/logs/a.1.log"]
    [call] = calls
    assert list(call["targets"]) == [TARGET]  # type: ignore[call-overload]
    assert call["orch_home"] == env.orch_home
    assert call["retention_days"] == 7
    assert call["dry_run"] is dry_run
    now = call["now"]
    assert isinstance(now, datetime)
    assert now.utcoffset() is not None


def test_gc_needs_the_hub(orch: Call, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_collect(monkeypatch)
    monkeypatch.delenv("ORCH_HUB")
    assert_error(orch("gc"), USAGE, "usage")
    assert calls == []


@pytest.mark.usefixtures("target")
def test_a_gc_error_exits_70_and_keeps_the_report(
    orch: Call, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_collect(monkeypatch, GcReport(dry_run=False, errors=("sample OSError",)))
    result = orch("gc")
    assert_error(result, INTERNAL, "internal")
    assert result[1]["errors"] == ["sample OSError"]


@pytest.mark.usefixtures("target")
def test_a_real_gc_dry_run_finds_nothing_on_a_new_home(orch: Call) -> None:
    code, data = orch("gc", "--dry-run")
    assert code == 0, data
    assert data["dry_run"] is True
    assert data["removed_files"] == []
    assert data["errors"] == []


# service


ORCH_BIN = "/abs/tool/orch"
TMUX_BIN = "/abs/ports/tmux"


class Services:
    """Replaces every service function, so no test reaches launchctl or tmux."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.install = ServiceReport(ok=True, done=service.LABELS)
        self.uninstall = ServiceReport(ok=True, done=service.LABELS)
        self.restore = RestoreReport(opened=(TARGET,))
        self.found: dict[str, str | None] = {"orch": ORCH_BIN, "tmux": TMUX_BIN}
        self.lookups: list[str] = []

    def which(self, name: str, *args: object, **kwargs: object) -> str | None:
        self.lookups.append(name)
        return self.found[name]


@pytest.fixture
def services(monkeypatch: pytest.MonkeyPatch) -> Services:
    fake = Services()

    def recorder(name: str) -> Callable[..., object]:
        def call(*args: object, **kwargs: object) -> object:
            fake.calls.append((name, args, kwargs))
            return getattr(fake, name)

        return call

    for name in ("install", "uninstall", "restore"):
        monkeypatch.setattr(service, name, recorder(name))
    monkeypatch.setattr(shutil, "which", fake.which)
    return fake


def test_service_install_hands_over_absolute_paths(
    orch: Call, env: Env, services: Services
) -> None:
    code, data = orch("service", "install")
    assert code == 0, data
    assert data == {
        "ok": True,
        "done": list(service.LABELS),
        "failed": [],
    }
    assert services.calls == [
        (
            "install",
            (),
            {
                "orch": Path(ORCH_BIN),
                "tmux": Path(TMUX_BIN),
                "hub": env.hub,
                "home": env.home,
                "orch_home": env.orch_home,
                "uid": os.getuid(),
                "custom_orch_home": True,
            },
        )
    ]
    assert not config.state_db_path(env.orch_home).exists()


def test_service_install_marks_the_default_home_as_not_custom(
    orch: Call, env: Env, services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ORCH_HOME")
    monkeypatch.delenv("ORCH_FAKE_AGENTS")
    code, data = orch("service", "install")
    assert code == 0, data
    assert services.calls[0][2]["custom_orch_home"] is False
    assert services.calls[0][2]["orch_home"] == env.home / ".orch"


@pytest.mark.parametrize("command", ["install", "restore"])
def test_service_commands_that_read_the_hub_need_it(
    orch: Call,
    services: Services,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    monkeypatch.delenv("ORCH_HUB")
    assert_error(orch("service", command), USAGE, "usage")
    assert services.calls == []


def test_service_uninstall_needs_neither_the_hub_nor_a_lookup(
    orch: Call, env: Env, services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ORCH_HUB")
    code, data = orch("service", "uninstall")
    assert code == 0, data
    assert services.calls == [("uninstall", (), {"home": env.home, "uid": os.getuid()})]
    assert services.lookups == []


@pytest.mark.usefixtures("target")
def test_service_restore_hands_over_targets_and_terminal(
    orch: Call, env: Env, services: Services
) -> None:
    code, data = orch("service", "restore")
    assert code == 0, data
    assert data == {
        "ok": True,
        "opened": [TARGET],
        "already_open": [],
        "failed": [],
    }
    [(name, args, kwargs)] = services.calls
    assert name == "restore"
    assert [list(args[0])] == [[TARGET]]  # type: ignore[call-overload]
    assert kwargs == {
        "orch": Path(ORCH_BIN),
        "orch_home": env.orch_home,
        "terminal": cli_module._terminal,
    }
    assert services.lookups == ["orch"]
    assert not config.state_db_path(env.orch_home).exists()


@pytest.mark.usefixtures("target")
@pytest.mark.parametrize(
    ("command", "name", "value"),
    [
        ("install", "orch", None),
        ("install", "tmux", None),
        ("install", "orch", "relative/orch"),
        ("install", "tmux", "relative/tmux"),
        ("restore", "orch", None),
        ("restore", "orch", "relative/orch"),
    ],
)
def test_service_refuses_a_program_it_cannot_locate(
    orch: Call, services: Services, command: str, name: str, value: str | None
) -> None:
    services.found[name] = value
    assert_error(orch("service", command), USAGE, "refused")
    assert services.calls == []


def test_a_located_path_is_not_resolved(
    orch: Call, env: Env, services: Services, tmp_path: Path
) -> None:
    real = tmp_path / "real-orch"
    write(real)
    link = tmp_path / "link-orch"
    link.symlink_to(real)
    services.found["orch"] = str(link)
    assert orch("service", "install")[0] == 0
    assert services.calls[0][2]["orch"] == link


@pytest.mark.parametrize("command", ["install", "uninstall"])
def test_a_failed_service_change_exits_20_with_the_report(
    orch: Call, services: Services, command: str
) -> None:
    failed = ServiceReport(
        ok=False, done=("orch.session",), failed=("orch.doctor bootstrap exit=5",)
    )
    setattr(services, command, failed)
    result = orch("service", command)
    assert_error(result, 20, "service_failed")
    assert result[1]["done"] == ["orch.session"]
    assert result[1]["failed"] == ["orch.doctor bootstrap exit=5"]


@pytest.mark.usefixtures("target")
def test_a_failed_restore_exits_20_with_the_report(
    orch: Call, services: Services
) -> None:
    services.restore = RestoreReport(opened=("a",), failed=("b unavailable",))
    result = orch("service", "restore")
    assert_error(result, 20, "service_failed")
    assert result[1]["opened"] == ["a"]
    assert result[1]["failed"] == ["b unavailable"]


def test_service_without_a_subcommand_is_a_usage_error(
    orch: Call, services: Services
) -> None:
    assert_error(orch("service"), USAGE, "usage")
    assert services.calls == []


# Pause reasons


def add_pauses(env: Env, *reasons: str) -> None:
    conn = connect(env)
    try:
        for reason in reasons:
            store.add_pause(conn, reason, since="2026-11-09T00:00:00+00:00", note="n")
    finally:
        conn.close()


def test_next_reports_pause_reasons_in_any_session(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_pauses(env, "disk", "claude_auth")
    assert orch("next")[1]["paused"] == ["claude_auth", "disk"]
    monkeypatch.setenv("ORCH_TARGET", "other")
    assert orch("next")[1]["paused"] == ["claude_auth", "disk"]


def test_status_reports_pause_reasons_in_any_session(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_pauses(env, "disk")
    expected = [{"reason": "disk", "since": "2026-11-09T00:00:00+00:00", "note": "n"}]
    assert orch("status")[1]["paused"] == expected
    monkeypatch.setenv("ORCH_TARGET", "other")
    assert orch("status")[1]["paused"] == expected


def test_status_human_starts_with_the_pause_reasons(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    insert(env)
    add_pauses(env, "disk", "claude_auth")
    assert main(["status", "--human"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "새 배분 중단: claude_auth, disk"
    assert lines[1].startswith(f"{TASK} pr_open")


# orch open reusing a session


@pytest.mark.usefixtures("openable")
@pytest.mark.parametrize(
    "status",
    [status for status in MainPaneStatus if status is not MainPaneStatus.ALIVE],
)
def test_open_warns_when_the_existing_main_pane_is_not_alive(
    orch: Call, terminals: Terminals, status: MainPaneStatus
) -> None:
    open_options(terminals)["exists"] = True
    terminals.pane_status = status
    code, data = orch("open", TARGET)
    assert code == 0, data
    assert data["opened"] is False
    assert data["warnings"] == [f"기존 세션의 메인 창 상태: {status.value}"]
    assert [call[0] for call in terminals[0].calls] == [
        "has_session",
        "main_pane_status",
    ]


@pytest.mark.usefixtures("openable")
def test_open_adds_the_pane_warning_after_the_others(
    orch: Call, repo: Path, terminals: Terminals
) -> None:
    open_options(terminals)["exists"] = True
    terminals.pane_status = MainPaneStatus.PANE_DEAD
    write(repo / "scratch.txt")
    data = orch("open", TARGET)[1]
    warnings = data["warnings"]
    assert isinstance(warnings, list) and len(warnings) == 2
    assert warnings[1] == "기존 세션의 메인 창 상태: pane_dead"


@pytest.mark.usefixtures("openable")
def test_open_output_is_redacted(
    orch: Call,
    env: Env,
    terminals: Terminals,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write(
        env.orch_home / "config",
        f'[agents]\nclaude = "{CLAUDE_BIN}"\n'
        f'[notify]\nmethod = "ntfy"\ntopic = "{TOPIC}"\n',
    )
    monkeypatch.setattr(launcher, "probe_claude_version", lambda claude: TOPIC)
    code, data = orch("open", TARGET)
    assert code == 0, data
    assert TOPIC not in json.dumps(data, ensure_ascii=False)
    assert data["warnings"]


# [gate] timeout_min


def set_gate_limit(env: Env, minutes: object) -> None:
    write(config.config_path(env.orch_home), f"[gate]\ntimeout_min = {minutes}\n")


def record_gate_limits(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    limits: list[object] = []
    original = gate.run_command

    def spy(command: str, worktree: Path, log_path: Path, **kwargs: object) -> object:
        limits.append(kwargs.get("timeout_sec"))
        return original(command, worktree, log_path, **kwargs)

    monkeypatch.setattr(gate, "run_command", spy)
    return limits


@pytest.mark.usefixtures("target")
def test_gate_uses_the_configured_limit(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    judge(orch, env)
    set_gate_limit(env, 45)
    limits = record_gate_limits(monkeypatch)
    code, data = orch("gate", TASK)
    assert code == 0, data
    assert limits
    assert set(limits) == {45 * 60}


@pytest.mark.usefixtures("target")
def test_gate_uses_thirty_minutes_without_a_setting(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    judge(orch, env)
    limits = record_gate_limits(monkeypatch)
    code, data = orch("gate", TASK)
    assert code == 0, data
    assert limits
    assert set(limits) == {config.GATE_TIMEOUT_DEFAULT_MIN * 60}


@pytest.mark.parametrize("minutes", [0, 121, "true", '"30"'])
@pytest.mark.usefixtures("target")
def test_gate_refuses_an_invalid_limit_without_running(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch, minutes: object
) -> None:
    judge(orch, env)
    set_gate_limit(env, minutes)
    limits = record_gate_limits(monkeypatch)
    assert_error(orch("gate", TASK, unchecked=True), USAGE, "refused")
    assert limits == []
    assert state_of(env) is TaskState.JUDGING


@pytest.mark.usefixtures("target")
def test_pr_passes_the_configured_limit(
    orch: Call, env: Env, pr_calls: list[dict[str, object]]
) -> None:
    make_ready(orch, env)
    add_token_script(env)
    set_gate_limit(env, 50)
    code, data = orch("pr", TASK)
    assert code == 0, data
    assert pr_calls[1].get("timeout_sec") == 50 * 60


@pytest.mark.usefixtures("target")
def test_pr_passes_the_default_limit_without_a_setting(
    orch: Call, env: Env, pr_calls: list[dict[str, object]]
) -> None:
    make_ready(orch, env)
    add_token_script(env)
    code, data = orch("pr", TASK)
    assert code == 0, data
    assert pr_calls[1].get("timeout_sec") == config.GATE_TIMEOUT_DEFAULT_MIN * 60


@pytest.mark.usefixtures("target")
def test_update_passes_the_configured_limit(
    orch: Call, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare(orch, env)
    add_token_script(env)
    set_gate_limit(env, 120)
    calls: list[dict[str, object]] = []

    def fake(conn: object, spec: object, target: object, **kwargs: object) -> object:
        calls.append(kwargs)
        return UpdateReport(
            state=TaskState.PR_OPEN,
            gates=GateReport(
                passed=True, gates={"G1": GateStatus.PASSED}, log_path="log"
            ),
        )

    monkeypatch.setattr(prflow, "update_pull_request", fake)
    result = orch("update", TASK, unchecked=True)
    assert result[0] == 0, result
    assert calls[0].get("timeout_sec") == 120 * 60


# orch sandbox run


@pytest.fixture
def checked(repo: Path, env: Env, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A linked worktree like the main agent's harness or combined worktree."""
    path = env.home / "checked"
    git(repo, "worktree", "add", "-q", "--detach", str(path))
    monkeypatch.setenv("ORCH_TARGET", TARGET)
    return path


def sandbox_run(orch: Call, *args: str) -> tuple[int, dict[str, object]]:
    return orch("sandbox", "run", *args)


def test_sandbox_run_passes_a_passing_command(
    orch: Call, env: Env, checked: Path
) -> None:
    code, data = sandbox_run(
        orch, "--dir", str(checked), "--", sys.executable, "-c", "print('ran it')"
    )
    assert code == 0, data
    assert data["ok"] is True
    assert data["exit_code"] == 0
    assert data["changed"] == []
    log = Path(str(data["log_path"]))
    assert log.parent == env.paths.logs
    assert re.fullmatch(r"sandbox-run\.\d{8}T\d{6}Z\.\d+\.log", log.name)
    assert "ran it" in log.read_text(encoding="utf-8")


def test_sandbox_run_reports_a_failing_command(orch: Call, checked: Path) -> None:
    result = sandbox_run(
        orch, "--dir", str(checked), "--", sys.executable, "-c", "raise SystemExit(4)"
    )
    assert_error(result, 20, "command_failed")
    assert result[1]["exit_code"] == 4
    assert result[1]["changed"] == []
    assert Path(str(result[1]["log_path"])).is_file()


def test_sandbox_run_fails_on_a_change_with_paths_only(
    orch: Call, checked: Path
) -> None:
    code = "open('src/app.py', 'a').write('contents-that-must-not-leak')"
    result = sandbox_run(orch, "--dir", str(checked), "--", sys.executable, "-c", code)
    assert_error(result, 20, "worktree_changed")
    assert result[1]["changed"] == ["src/app.py"]
    assert result[1]["message"] == gate.CHANGED_DETAIL + "src/app.py"
    assert result[1]["exit_code"] == 0
    assert "must-not-leak" not in json.dumps(result[1], ensure_ascii=False)


def test_sandbox_run_fails_closed_without_the_sandbox(
    orch: Call, checked: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "SANDBOX_EXEC", str(tmp_path / "missing" / "sx"))
    marker = tmp_path / "ran"
    result = sandbox_run(orch, "--dir", str(checked), "--", "touch", str(marker))
    assert_error(result, 20, "sandbox_failed")
    assert result[1]["exit_code"] is None
    assert not marker.exists()


def test_sandbox_run_uses_the_current_directory_by_default(
    orch: Call, checked: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "cwd.txt"
    monkeypatch.chdir(checked)
    script = "import os, sys; open(sys.argv[1], 'w').write(os.getcwd())"
    code, data = sandbox_run(orch, "--", sys.executable, "-c", script, str(out))
    assert code == 0, data
    assert Path(out.read_text(encoding="utf-8")).resolve() == checked.resolve()


def test_sandbox_run_resolves_a_relative_dir(
    orch: Call, checked: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "cwd.txt"
    monkeypatch.chdir(checked.parent)
    script = "import os, sys; open(sys.argv[1], 'w').write(os.getcwd())"
    code, data = sandbox_run(
        orch, "--dir", checked.name, "--", sys.executable, "-c", script, str(out)
    )
    assert code == 0, data
    assert Path(out.read_text(encoding="utf-8")).resolve() == checked.resolve()


def test_sandbox_run_passes_the_configured_limit(
    orch: Call, env: Env, checked: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_gate_limit(env, 40)
    limits: list[object] = []
    original = gate.run_sandboxed

    def spy(argv: object, worktree: Path, log_path: Path, **kwargs: object) -> object:
        limits.append(kwargs.get("timeout_sec"))
        return original(argv, worktree, log_path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(gate, "run_sandboxed", spy)
    code, data = sandbox_run(orch, "--dir", str(checked), "--", "true")
    assert code == 0, data
    assert limits == [40 * 60]


def test_sandbox_run_uses_thirty_minutes_without_a_setting(
    orch: Call, checked: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    limits = record_gate_limits(monkeypatch)
    code, data = sandbox_run(orch, "--dir", str(checked), "--", "true")
    assert code == 0, data
    assert limits == [config.GATE_TIMEOUT_DEFAULT_MIN * 60]


def test_sandbox_run_refuses_an_invalid_limit_without_running(
    orch: Call, env: Env, checked: Path, tmp_path: Path
) -> None:
    set_gate_limit(env, 0)
    marker = tmp_path / "ran"
    result = sandbox_run(orch, "--dir", str(checked), "--", "touch", str(marker))
    assert_error(result, USAGE, "refused")
    assert not marker.exists()


def test_sandbox_run_needs_a_command(orch: Call, checked: Path) -> None:
    assert_error(sandbox_run(orch, "--dir", str(checked), "--"), USAGE, "usage")
    assert_error(sandbox_run(orch, "--dir", str(checked)), USAGE, "usage")


def test_sandbox_run_needs_a_target(
    orch: Call, checked: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ORCH_TARGET")
    marker = tmp_path / "ran"
    result = sandbox_run(orch, "--dir", str(checked), "--", "touch", str(marker))
    assert_error(result, USAGE, "usage")
    assert not marker.exists()


def test_sandbox_run_does_not_need_the_hub(
    orch: Call, checked: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ORCH_HUB")
    code, data = sandbox_run(orch, "--dir", str(checked), "--", "true")
    assert code == 0, data


def test_sandbox_run_refuses_a_directory_that_is_not_a_linked_worktree(
    orch: Call, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ORCH_TARGET", TARGET)
    marker = tmp_path / "ran"
    plain = tmp_path / "plain"
    plain.mkdir()
    for directory in (repo, plain):
        result = sandbox_run(orch, "--dir", str(directory), "--", "touch", str(marker))
        assert_error(result, USAGE, "refused")
    assert not marker.exists()


def test_sandbox_run_does_not_touch_the_state_database(
    orch: Call, env: Env, checked: Path
) -> None:
    code, data = sandbox_run(orch, "--dir", str(checked), "--", "true")
    assert code == 0, data
    assert not config.state_db_path(env.orch_home).exists()
    assert not env.paths.todo.exists()
