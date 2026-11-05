"""A sandbox for integration tests: a real orch process with no way to the network.

Every test runs the installed orch command as a separate process, against a bare
repository on disk and a fake gh. The sandbox refuses to start unless it is isolated,
and it checks for leaked tokens and personal paths when each test ends.
"""

import hashlib
import json
import shutil
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from transitions import check_call

REPO_ROOT = Path(__file__).resolve().parents[2]
FAKE_GH = REPO_ROOT / "tests" / "fake_gh" / "gh"
FAKE_AGENTS = REPO_ROOT / "tests" / "fake_agents"
TOKEN = "gh" + "s_" + "integration0sandbox0token0" * 2
GITHUB = "owner/sample"
TARGET = "sample"
LABEL = "review-overridden"
# One value, not increasing per commit. Identical commits then always get the same id,
# so a check that expects different ids fails every time, on a machine of any speed.
COMMIT_DATE = "2026-01-01T00:00:00+0000"
CHANGED = "src/fake_change.py"
TEST_SCRIPT = "tests/check.sh"
TOKEN_NAMES = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
)


def environment_problems(env: Mapping[str, str], fake_gh: Path) -> list[str]:
    """Return every way these variables alone could reach real credentials."""
    problems = []
    if env.get("GIT_CONFIG_NOSYSTEM") != "1":
        problems.append("GIT_CONFIG_NOSYSTEM is not 1")
    problems.extend(f"{name} is set" for name in TOKEN_NAMES if name in env)
    found_gh = shutil.which("gh", path=env.get("PATH", ""))
    if found_gh is None or Path(found_gh).resolve() != fake_gh.resolve():
        problems.append(f"gh on PATH is not the fake gh: {found_gh}")
    for name in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"):
        if env.get(name) != COMMIT_DATE:
            problems.append(f"{name} is not the frozen commit date")
    return problems


def isolation_problems(
    env: Mapping[str, str], repos: Iterable[Path], bare: Path, fake_gh: Path
) -> list[str]:
    """Return every way this environment could reach the network or real credentials."""

    def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            env=dict(env),
            capture_output=True,
            text=True,
        )

    problems = environment_problems(env, fake_gh)
    if git(bare, "rev-parse", "--is-bare-repository").stdout.strip() != "true":
        problems.append(f"Not a bare repository: {bare}")
    for repo in repos:
        helper = git(repo, "config", "--get-all", "credential.helper").stdout.strip()
        if helper:
            problems.append(f"credential.helper is set in {repo}: {helper}")
        names = git(repo, "remote").stdout.split()
        if not names:
            problems.append(f"No remote in {repo}")
        for name in names:
            # get-url applies pushurl and url.<base>.insteadOf, unlike the config.
            for options in (("--all",), ("--push", "--all")):
                urls = git(repo, "remote", "get-url", *options, name).stdout.split()
                if urls != [str(bare)]:
                    problems.append(f"Remote {name} of {repo} is not local: {urls}")
    return problems


def require_isolation(
    env: Mapping[str, str], repos: Iterable[Path], bare: Path, fake_gh: Path
) -> None:
    """Fail the test before it starts if the environment is not isolated."""
    problems = isolation_problems(env, repos, bare, fake_gh)
    if problems:
        pytest.fail("Sandbox is not isolated:\n" + "\n".join(problems), pytrace=False)


@dataclass(frozen=True, slots=True)
class Result:
    """How one orch process ended. data is None for human output."""

    code: int
    stdout: str
    stderr: str
    data: dict | None


class Sandbox:
    """One isolated home, hub, bare repository and fake GitHub."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "home"
        self.orch_home = self.home / "orch-runtime"
        self.hub = root / "hub"
        self.bare = root / "remote.git"
        self.clone = self.home / TARGET
        self.gh_dir = root / "fake-gh-state"
        self.drafts = root / "drafts"
        self.orch_command = Path(sys.executable).parent / "orch"
        if not self.orch_command.exists():
            pytest.fail(f"orch is not installed: {self.orch_command}", pytrace=False)
        git_command = shutil.which("git")
        if git_command is None:
            pytest.fail("git is not on PATH", pytrace=False)
        self.env = {
            "PATH": f"{FAKE_GH.parent}:{Path(git_command).parent}:/usr/bin:/bin",
            "HOME": str(self.home),
            "TMPDIR": str(root),
            "LANG": "en_US.UTF-8",
            "ORCH_HOME": str(self.orch_home),
            "ORCH_HUB": str(self.hub),
            "ORCH_FAKE_AGENTS": "1",
            "ORCH_FAKE_SCENARIO": "success",
            "ORCH_FAKE_PYTHON": sys.executable,
            "ORCH_FAKE_GH_DIR": str(self.gh_dir),
            "CODEX_HOME": str(self.home / "codex"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(self.home / ".gitconfig"),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_DATE": COMMIT_DATE,
            "GIT_COMMITTER_DATE": COMMIT_DATE,
        }
        self._build()
        require_isolation(self.env, [self.clone], self.bare, FAKE_GH)

    def _build(self) -> None:
        for path in (self.home, self.gh_dir, self.drafts, self.hub / "tests"):
            path.mkdir(parents=True)
        (self.hub / "tests" / "fake_agents").symlink_to(FAKE_AGENTS)
        (self.home / ".gitconfig").write_text(
            "[user]\n\tname = t\n\temail = t@example.com\n"
            "[init]\n\tdefaultBranch = main\n",
            encoding="utf-8",
        )
        script = self.orch_home / "bin" / "ghapp-token.sh"
        script.parent.mkdir(parents=True)
        script.write_text(f"#!/bin/sh\nprintf '%s\\n' '{TOKEN}'\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        self.write_gh_state(
            {
                "repo": GITHUB,
                "bare": str(self.bare),
                "token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
                "labels": [LABEL],
                "pull_requests": [],
            }
        )
        self.git(self.root, "init", "-q", "--bare", str(self.bare))
        seed = self.root / "seed"
        self.git(self.root, "clone", "-q", str(self.bare), str(seed))
        self.write(seed / "README.md", "sample\n")
        self.write(seed / "tests" / "base.sh", "test -f README.md\n")
        self.git(seed, "add", ".")
        self.git(seed, "commit", "-qm", "base")
        self.git(seed, "push", "-q", "origin", "HEAD:refs/heads/main")
        self.git(
            self.root,
            "--git-dir",
            str(self.bare),
            "symbolic-ref",
            "HEAD",
            "refs/heads/main",
        )
        shutil.rmtree(seed)
        self.git(self.root, "clone", "-q", str(self.bare), str(self.clone))

    # Tools

    @staticmethod
    def write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def git(self, cwd: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def orch(
        self,
        *args: str,
        stdin: str = "",
        env: Mapping[str, str] | None = None,
        unset: Iterable[str] = (),
        unchecked: bool = False,
    ) -> Result:
        """Run orch as a process and hold it to the output contract."""
        variables = {**self.env, **(env or {})}
        check_call(Path(variables["ORCH_HOME"]) / "state.db", args, unchecked=unchecked)
        for name in unset:
            variables.pop(name, None)
        problems = environment_problems(variables, FAKE_GH)
        if problems:
            pytest.fail("orch call is not isolated:\n" + "\n".join(problems))
        done = subprocess.run(
            [str(self.orch_command), *args],
            env=variables,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert TOKEN not in done.stdout + done.stderr
        data = None
        if "--human" not in args and args[0] != "watch":
            assert done.stdout.endswith("\n"), done
            assert done.stdout.count("\n") == 1, done
            data = json.loads(done.stdout)
        return Result(done.returncode, done.stdout, done.stderr, data)

    def ok(self, *args: str, **options: object) -> dict:
        """Run an orch command that must succeed."""
        result = self.orch(*args, **options)
        assert result.code == 0, result
        assert result.data is not None and result.data["ok"] is True, result
        return result.data

    # Reading state

    def status(self, task_id: str) -> dict:
        tasks = self.ok("status")["tasks"]
        return next(item for item in tasks if item["id"] == task_id)

    def state(self, task_id: str) -> str:
        return self.status(task_id)["state"]

    def query(self, sql: str, *values: object) -> list[sqlite3.Row]:
        conn = sqlite3.connect(self.orch_home / "state.db")
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, values).fetchall()
        finally:
            conn.close()

    def task_row(self, task_id: str) -> sqlite3.Row:
        return self.query("SELECT * FROM tasks WHERE id = ?", task_id)[0]

    def gate_rows(self, task_id: str) -> dict[str, bool]:
        rows = self.query(
            "SELECT gate, passed FROM gate_results WHERE task_id = ?", task_id
        )
        return {row["gate"]: bool(row["passed"]) for row in rows}

    def remote_sha(self, branch: str) -> str | None:
        done = subprocess.run(
            [
                "git",
                "--git-dir",
                str(self.bare),
                "rev-parse",
                "--verify",
                "--quiet",
                f"refs/heads/{branch}",
            ],
            env=self.env,
            capture_output=True,
            text=True,
        )
        return done.stdout.strip() if done.returncode == 0 else None

    def local_branch_exists(self, branch: str) -> bool:
        done = subprocess.run(
            [
                "git",
                "-C",
                str(self.clone),
                "rev-parse",
                "--verify",
                "--quiet",
                f"refs/heads/{branch}",
            ],
            env=self.env,
            capture_output=True,
        )
        return done.returncode == 0

    def head(self, worktree: Path) -> str:
        return self.git(worktree, "rev-parse", "HEAD")

    # The fake GitHub

    def gh_state(self) -> dict:
        return json.loads((self.gh_dir / "state.json").read_text(encoding="utf-8"))

    def write_gh_state(self, state: dict) -> None:
        self.write(self.gh_dir / "state.json", json.dumps(state, ensure_ascii=False))

    def gh_calls(self) -> list[dict]:
        path = self.gh_dir / "calls.jsonl"
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines]

    def pull_request(self, url: str) -> dict:
        items = self.gh_state()["pull_requests"]
        return next(item for item in items if item["url"] == url)

    def set_pull_request(self, url: str, **values: object) -> None:
        state = self.gh_state()
        next(item for item in state["pull_requests"] if item["url"] == url).update(
            values
        )
        self.write_gh_state(state)

    def merge_pull_request(self, url: str) -> None:
        """Do what a merge on GitHub does: a merge commit on main, on the remote."""
        head = self.pull_request(url)["head"]
        merge = self.root / "merge-clone"
        self.git(self.root, "clone", "-q", str(self.bare), str(merge))
        self.git(merge, "merge", "--no-ff", "-q", "-m", "merge", f"origin/{head}")
        self.git(merge, "push", "-q", "origin", "HEAD:refs/heads/main")
        shutil.rmtree(merge)
        self.set_pull_request(url, state="MERGED", mergedAt="2026-01-01T00:00:00Z")

    def advance_main(self, path: str, text: str) -> str:
        """Put a commit on the remote's main, as another merged pull request would."""
        other = self.root / "main-clone"
        self.git(self.root, "clone", "-q", str(self.bare), str(other))
        self.write(other / path, text)
        self.git(other, "add", ".")
        self.git(other, "commit", "-qm", f"Change {path}")
        self.git(other, "push", "-q", "origin", "HEAD:refs/heads/main")
        sha = self.head(other)
        shutil.rmtree(other)
        return sha

    # Moving a task along

    def add_target(self) -> None:
        self.ok(
            "target",
            "add",
            str(self.clone),
            "--name",
            TARGET,
            "--github",
            GITHUB,
            "--setup",
            "true",
            "--test",
            "sh tests/base.sh",
            "--lint",
            "sh -n tests/base.sh",
        )

    def spec_file(
        self,
        task_id: str,
        *,
        grade: str = "L3",
        depends_on: Iterable[str] = (),
        modify: str = "src/**",
        title: str = "Add the fake change",
    ) -> Path:
        effort = {"L2": "medium", "L3": "high"}[grade]
        dependencies = ", ".join(f'"{item}"' for item in depends_on)
        path = self.drafts / f"{task_id}.md"
        self.write(
            path,
            "\n".join(
                [
                    "+++",
                    f'id = "{task_id}"',
                    f'title = "{title}"',
                    f'target = "{TARGET}"',
                    f'grade = "{grade}"',
                    'lane = "B"',
                    'model = "gpt-6.1-sol"',
                    f'effort = "{effort}"',
                    "timeout_min = 5",
                    f'modify = ["{modify}"]',
                    'read = ["src/**"]',
                    f'verify = "sh {TEST_SCRIPT}"',
                    f"depends_on = [{dependencies}]",
                    "+++",
                    "",
                    "# Purpose",
                    "",
                    "Integration test task.",
                    "",
                ]
            ),
        )
        return path

    def prepare(self, task_id: str, **spec: object) -> Path:
        data = self.ok("prepare", str(self.spec_file(task_id, **spec)))
        assert data["state"] == "prepared"
        return Path(data["worktree"])

    def commit_tests(self, worktree: Path) -> str:
        """Commit a test that passes only once the agent has made its change."""
        self.write(worktree / TEST_SCRIPT, f"test -f {CHANGED}\n")
        self.git(worktree, "add", TEST_SCRIPT)
        self.git(worktree, "commit", "-qm", "Add the tests")
        return self.head(worktree)

    def reserve(self, task_id: str, **spec: object) -> Path:
        worktree = self.prepare(task_id, **spec)
        self.commit_tests(worktree)
        data = self.ok("dispatch", task_id)
        assert data["state"] == "reserved", data
        return worktree

    def judge(self, task_id: str, **spec: object) -> Path:
        worktree = self.reserve(task_id, **spec)
        data = self.ok("run", task_id)
        assert (data["status"], data["state"]) == ("DONE", "judging"), data
        return worktree

    def reviewing(self, task_id: str, **spec: object) -> Path:
        worktree = self.judge(task_id, **spec)
        data = self.ok("gate", task_id)
        assert data["state"] == "reviewing", data
        return worktree

    def orch_review(
        self, task_id: str, findings: Iterable[tuple[str, str]], triage: str | None
    ) -> Result:
        """Act as the fake reviewer: findings are (severity, path) pairs."""
        items = list(findings)
        blocking = any(severity in ("P0", "P1") for severity, _ in items)
        review = {
            "task_id": task_id,
            "reviewer": "claude-reviewer",
            "verdict": "BLOCK" if blocking else "PASS",
            "findings": [
                {"severity": severity, "path": path, "summary": "가짜 리뷰 지적"}
                for severity, path in items
            ],
        }
        args = ["review", "save", task_id]
        if triage is not None:
            args.extend(("--triage", triage))
        return self.orch(*args, stdin=json.dumps(review))

    def save_review(
        self, task_id: str, findings: Iterable[tuple[str, str]] = ()
    ) -> Result:
        """Save a review without a triage."""
        return self.orch_review(task_id, findings, None)

    def ready(self, task_id: str, **spec: object) -> Path:
        worktree = self.reviewing(task_id, **spec)
        result = self.save_review(task_id)
        assert result.code == 0 and result.data["state"] == "ready", result
        return worktree

    def open_pull_request(self, task_id: str, **spec: object) -> tuple[Path, str]:
        worktree = self.ready(task_id, **spec)
        data = self.ok("pr", task_id)
        assert data["state"] == "pr_open", data
        return worktree, data["pr_url"]

    # Leak checks

    def leaks(self) -> list[str]:
        """Return where the token or a personal path has reached."""
        found = []
        token = TOKEN.encode()
        script = self.orch_home / "bin" / "ghapp-token.sh"
        for path in self.root.rglob("*"):
            if path == script or path.is_symlink() or not path.is_file():
                continue
            if token in path.read_bytes():
                found.append(f"token in {path}")
        history = self.git(self.root, "--git-dir", str(self.bare), "log", "--all", "-p")
        if TOKEN in history:
            found.append("token in the bare repository's history")
        outbound = [
            argument for call in self.gh_calls() for argument in call["argv"]
        ] + [call["stdin"] for call in self.gh_calls()]
        for item in self.gh_state()["pull_requests"]:
            outbound.extend((item["title"], item["body"], *item["comments"]))
        for private in (str(self.root), str(Path.home())):
            if any(private in text for text in outbound):
                found.append(f"personal path sent to GitHub: {private}")
        return found


@pytest.fixture
def sandbox(tmp_path: Path) -> Iterator[Sandbox]:
    """An isolated sandbox that fails its test if anything leaked by the end."""
    built = Sandbox(tmp_path.resolve())
    built.add_target()
    yield built
    leaks = built.leaks()
    assert not leaks, leaks
