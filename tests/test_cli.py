import io
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from orch import config, github, prflow, store
from orch.cli import main
from orch.gate import GateReport, GateStatus
from orch.github import GitHubError, PullRequestStatus
from orch.gitops import GitError, LeaseRejectedError
from orch.models import Effort, Grade, Lane, Task, TaskState
from orch.prflow import PullRequestFlowError, PullRequestReport, UpdateReport

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
    def call(*args: str, stdin: str = "") -> tuple[int, dict[str, object]]:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        code = main(list(args))
        out = capsys.readouterr().out
        assert out.endswith("\n") and out.count("\n") == 1, out
        return code, json.loads(out)

    return call


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
    assert_error(orch(*args), USAGE, "refused")
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
    assert_error(orch("run", TASK), USAGE, "refused")
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
    assert_error(orch("gate", TASK), USAGE, "refused")


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
    assert_error(orch("review", "save", TASK, stdin=review_text()), USAGE, "refused")


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
    result = orch("update", TASK)
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


def fake_status(
    tasks: list[str], chains: list[tuple[str, list[str]]], secret: str | None
) -> Callable[[object, list[str]], tuple[int, dict[str, object]]]:
    """Replace _execute: status issues no token, so this is the only way to get one."""

    def execute(_args: object, secrets: list[str]) -> tuple[int, dict[str, object]]:
        if secret is not None:
            secrets.append(secret)
        return 0, {
            "tasks": [{"id": item, "state": "ready"} for item in tasks],
            "blocked_chains": [
                {"task_id": item, "state": "blocked", "causes": causes}
                for item, causes in chains
            ],
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
    code, data = orch("update", TASK)
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
