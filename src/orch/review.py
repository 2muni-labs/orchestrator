"""orch review: run a Codex review, store a review verdict, validate a triage."""

import os
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time
import tomllib
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from orch import config, gitops, proxy, results, runner, store
from orch.config import Config, Target, TargetPaths
from orch.lanes import base, codex
from orch.models import Lane, Review, Severity, Task, TaskState, Verdict
from orch.redact import RedactionError, redact

OVERRIDE_LABEL = "review-overridden"
# The effort of a Codex cross-review (requirements, chapter 4).
# The CLI supplies the recorded Codex model.
REVIEW_EFFORT = "medium"
OVERRIDE_HEADING = "## Review BLOCK overridden"


class ReviewFlowError(Exception):
    """A review or triage was refused. The task's state did not change."""


class TriageVerdict(StrEnum):
    """Why a P0 or P1 finding does not stop the pull request."""

    WRONG_FINDING = "wrong_finding"
    INSUFFICIENT_TESTS = "insufficient_tests"


@dataclass(frozen=True, slots=True, kw_only=True)
class TriageDecision:
    """The main agent's judgment on one P0 or P1 finding."""

    severity: Severity
    path: str
    verdict: TriageVerdict
    decision: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Triage:
    """A parsed triage file."""

    decisions: tuple[TriageDecision, ...]
    follow_up: str | None = None
    body: str = ""


def parse_triage(text: str) -> Triage:
    """Parse a triage file and check everything that needs no review or database."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "+++":
        raise ReviewFlowError("Triage needs TOML front matter")
    end = next(
        (i for i in range(1, len(lines)) if lines[i].rstrip("\r\n") == "+++"),
        None,
    )
    if end is None:
        raise ReviewFlowError("Triage front matter is not closed")
    try:
        data = tomllib.loads("".join(lines[1:end]))
    except tomllib.TOMLDecodeError as exc:
        raise ReviewFlowError("Invalid triage TOML") from exc
    if data.keys() - {"follow_up", "decisions"}:
        raise ReviewFlowError("Unknown triage key")
    tables = data.get("decisions")
    if not isinstance(tables, list) or not tables:
        raise ReviewFlowError("Triage needs decisions")
    decisions = []
    for table in tables:
        if not isinstance(table, dict) or table.keys() != {
            "severity",
            "path",
            "verdict",
            "decision",
        }:
            raise ReviewFlowError("Invalid triage decision fields")
        if table["severity"] not in ("P0", "P1"):
            raise ReviewFlowError("Triage severity must be P0 or P1")
        path = table["path"]
        if not isinstance(path, str) or not path:
            raise ReviewFlowError("Triage path must not be empty")
        if (
            any(not " " <= char <= "~" for char in path)
            or "`" in path
            or path.startswith(("/", "~"))
            or "\\" in path
            or ".." in path.split("/")
        ):
            raise ReviewFlowError("Triage path must be plain and repository relative")
        try:
            verdict = TriageVerdict(table["verdict"])
        except ValueError as exc:
            raise ReviewFlowError("Invalid triage verdict") from exc
        decision = table["decision"]
        if (
            not isinstance(decision, str)
            or not decision.strip()
            or any(not " " <= char <= "~" for char in decision)
        ):
            raise ReviewFlowError("Triage decision must be one line of English")
        if verdict is TriageVerdict.INSUFFICIENT_TESTS and not path.startswith(
            "tests/"
        ):
            raise ReviewFlowError("Insufficient tests must name a tests/ path")
        decisions.append(
            TriageDecision(
                severity=Severity(table["severity"]),
                path=path,
                verdict=verdict,
                decision=decision,
            )
        )
    needs_follow_up = any(
        item.verdict is TriageVerdict.INSUFFICIENT_TESTS for item in decisions
    )
    follow_up = data.get("follow_up")
    if needs_follow_up:
        try:
            config.validate_task_id(follow_up)
        except config.ConfigError as exc:
            raise ReviewFlowError("Insufficient tests need a valid follow-up") from exc
    elif "follow_up" in data:
        raise ReviewFlowError("Follow-up requires an insufficient_tests decision")
    body = "".join(lines[end + 1 :])
    if any(char != "\n" and not " " <= char <= "~" for char in body):
        raise ReviewFlowError("Triage body must be English text")
    return Triage(decisions=tuple(decisions), follow_up=follow_up, body=body)


def load_triage(path: Path) -> Triage:
    """Read and parse a triage file."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ReviewFlowError("Cannot read triage as UTF-8") from exc
    return parse_triage(text)


def check_triage(
    conn: sqlite3.Connection, triage: Triage, review: Review, task: Task
) -> None:
    """Raise ReviewFlowError unless the triage settles the review's BLOCK."""
    if review.verdict is not Verdict.BLOCK:
        raise ReviewFlowError("Triage requires a BLOCK review")
    findings = Counter(
        (finding.severity, finding.path)
        for finding in review.findings
        if finding.severity in (Severity.P0, Severity.P1)
    )
    decisions = Counter((item.severity, item.path) for item in triage.decisions)
    if findings != decisions:
        raise ReviewFlowError("Triage must match every P0 and P1 finding exactly")
    if triage.follow_up is not None:
        follow_up = store.get_task(conn, triage.follow_up)
        if (
            triage.follow_up == task.id
            or follow_up is None
            or follow_up.target != task.target
            or follow_up.state is TaskState.DISCARDED
        ):
            raise ReviewFlowError("Invalid follow-up task")


def override_section(triage: Triage) -> str:
    """Return the pull request section that reports an overridden BLOCK."""
    lines = [OVERRIDE_HEADING, ""]
    if triage.follow_up is not None:
        lines.extend([f"Follow-up task: `{triage.follow_up}`", ""])
    lines.extend(
        f"- {item.severity} `{item.path}` ({item.verdict}): {item.decision}"
        for item in triage.decisions
    )
    if body := triage.body.strip():
        lines.extend(["", body])
    return "\n".join(lines)


def has_blocking_review(paths: TargetPaths, task_id: str) -> bool:
    """Return whether the task's current saved review has the verdict BLOCK."""
    path = paths.review_path(task_id)
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ReviewFlowError("Cannot access saved review") from exc
    try:
        review = results.load_review(path, task_id)
    except results.ReportError as exc:
        raise ReviewFlowError("Invalid saved review") from exc
    return review.verdict is Verdict.BLOCK


def save_review(
    conn: sqlite3.Connection,
    task_id: str,
    text: str,
    *,
    paths: TargetPaths,
    home: Path,
    orch_home: Path,
    triage_file: Path | None = None,
) -> TaskState:
    """Validate and store a review, and move the task out of reviewing."""
    task = store.get_task(conn, task_id)
    if task is None or task.state is not TaskState.REVIEWING:
        raise ReviewFlowError("Task must exist and be reviewing")
    try:
        review = results.review_from_text(text, task_id)
    except results.ReportError as exc:
        raise ReviewFlowError("Invalid review") from exc
    triage = None
    if triage_file is not None:
        triage = load_triage(triage_file)
        check_triage(conn, triage, review, task)
        for value in (
            *[item.decision for item in triage.decisions],
            *[item.path for item in triage.decisions],
            triage.body,
        ):
            try:
                safe_value = redact(value, home=home, orch_home=orch_home)
            except RedactionError as exc:
                raise ReviewFlowError("Triage contains private information") from exc
            if safe_value != value:
                raise ReviewFlowError("Triage contains a personal path")

    destination = paths.triage_path(task_id)
    temporary = None
    try:
        if triage_file is None:
            destination.unlink(missing_ok=True)
        elif triage_file.resolve() != destination.resolve():
            content = triage_file.read_bytes()
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=destination.parent, delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(content)
            os.replace(temporary, destination)
    except OSError as exc:
        raise ReviewFlowError("Cannot store triage") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    results.write_review(paths.review_path(task_id), review)

    next_state = (
        TaskState.READY
        if triage is not None or review.verdict is Verdict.PASS
        else TaskState.JUDGING
    )
    notes = ["BLOCK overridden"] if triage is not None else []
    counts = Counter(finding.severity for finding in review.findings)
    minor = ", ".join(
        f"{severity}: {counts[severity]}"
        for severity in (Severity.P2, Severity.P3)
        if counts[severity]
    )
    if minor:
        notes.append(minor)
    store.transition(
        conn,
        task_id,
        TaskState.REVIEWING,
        next_state,
        actor="orch",
        note="; ".join(notes) or None,
    )
    return next_state


class ReviewOutcome(StrEnum):
    """How a Codex review run ended."""

    SAVED = "saved"
    INVALID = "invalid"
    FAILED = "failed"
    QUOTA = "quota"
    TIMEOUT = "timeout"


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewRun:
    """The result of one Codex review run. It never holds log contents."""

    task_id: str
    outcome: ReviewOutcome
    state: TaskState
    exit_code: int | None
    log_path: Path
    output_path: Path
    diff_path: Path
    proxy_denials: int


def review_prompt(task_id: str, *, spec_path: Path, diff_path: Path) -> str:
    """Return the English prompt for a Codex review."""
    return (
        f'Task id: "{task_id}"\n'
        f"Task specification: {spec_path}\n"
        f"Review diff: {diff_path}\n\n"
        "Read the specification and diff, and review the implementation. "
        "Do not modify any files.\n"
        "Your final message must be exactly one JSON object with these fields:\n"
        f'- "task_id": "{task_id}"\n'
        '- "reviewer": "codex-review"\n'
        '- "verdict": "PASS" or "BLOCK"; use "BLOCK" only when there are '
        "P0 or P1 findings.\n"
        '- "findings": an array of objects with "severity" ("P0", "P1", '
        '"P2", or "P3"), "path" (a repository-relative path), and "summary" '
        "(one or two sentences in Korean).\n"
        "Include no Markdown fences or other text in the final message.\n"
    )


def next_review_log(logs: Path, task_id: str) -> Path:
    """Return logs/review/<id>.<n>.log for the next review, without creating it."""
    return runner.next_log_path(logs / "review", task_id)


def run_codex_review(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    target: Target,
    config: Config,
    paths: TargetPaths,
    environ: Mapping[str, str],
    model: str,
    effort: str,
    home: Path,
    orch_home: Path,
    timeout_sec: int | None = None,
) -> ReviewRun:
    """Review a Claude implementation with codex exec and save a valid review."""
    task = store.get_task(conn, task_id)
    if task is None or task.state is not TaskState.REVIEWING:
        raise ReviewFlowError("Task must exist and be reviewing")
    if task.lane not in (Lane.C, Lane.D):
        raise ReviewFlowError("Codex can review only Claude implementations")
    if not task.worktree or not (worktree := Path(task.worktree)).is_dir():
        raise ReviewFlowError("Task worktree must be a directory")
    spec_path = paths.tasks / f"{task_id}.md"
    if not spec_path.is_file():
        raise ReviewFlowError("Task specification is missing")
    diff_path = paths.reviews / f"{task_id}.diff"
    try:
        merge_base = gitops.merge_base(worktree, f"origin/{target.default_branch}")
        diff_text = gitops.diff(worktree, merge_base)
        diff_path.parent.mkdir(parents=True, exist_ok=True)
        diff_path.write_text(diff_text, encoding="utf-8")
    except (gitops.GitError, OSError) as exc:
        raise ReviewFlowError("Cannot store the review diff") from exc
    log_path = next_review_log(paths.logs, task_id)
    output_path = log_path.with_suffix(".out")
    proxy_log = log_path.with_suffix(".proxy.log")
    px: proxy.AllowlistProxy | None = None
    tmpdir: Path | None = None
    process: subprocess.Popen[bytes] | None = None

    def finish(
        outcome: ReviewOutcome,
        exit_code: int | None,
        state: TaskState = task.state,
    ) -> ReviewRun:
        if px is not None:
            px.stop()
        return ReviewRun(
            task_id=task_id,
            outcome=outcome,
            state=state,
            exit_code=exit_code,
            log_path=log_path,
            output_path=output_path,
            diff_path=diff_path,
            proxy_denials=len(px.denials()) if px is not None else 0,
        )

    try:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.unlink(missing_ok=True)
            hub = Path(environ["ORCH_HUB"])
            if not hub.is_absolute():
                raise base.LaneError("Codex reviews require an absolute ORCH_HUB")
            hosts = proxy.load_proxy_hosts(hub)
            tmpdir = Path(tempfile.mkdtemp(prefix="orch-codex-")).resolve()
            listener = codex.fake_proxy_listener(environ)
            if listener is None:
                px = proxy.AllowlistProxy(hosts, proxy_log)
            else:
                px = proxy.AllowlistProxy(hosts, proxy_log, listener_factory=listener)
            port = px.start()
            command = codex.build_review_command(
                worktree=worktree,
                prompt=review_prompt(task_id, spec_path=spec_path, diff_path=diff_path),
                output_path=output_path,
                model=model,
                effort=effort,
                config=config,
                environ=environ,
                run_sandbox=codex.CodexSandbox(port=port, tmpdir=tmpdir),
            )
            with (
                tempfile.TemporaryFile(dir=log_path.parent) as stdin,
                log_path.open("xb") as log,
            ):
                stdin.write(command.stdin_text.encode("utf-8"))
                stdin.seek(0)
                process = subprocess.Popen(
                    list(command.argv),
                    cwd=command.cwd,
                    env=dict(command.env),
                    stdin=stdin,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except Exception:
            if process is not None:
                runner.stop_process(process)
            return finish(ReviewOutcome.FAILED, None)

        timed_out = False
        proxy_stopped = False
        deadline = time.monotonic() + (
            task.timeout_sec if timeout_sec is None else timeout_sec
        )
        try:
            while True:
                if not px.alive():
                    runner.stop_process(process)
                    proxy_stopped = True
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    runner.stop_process(process)
                    timed_out = True
                    break
                try:
                    process.wait(timeout=min(runner.PROXY_POLL_SEC, remaining))
                except subprocess.TimeoutExpired:
                    continue
                proxy_stopped = not px.alive()
                runner._send(process.pid, set(), signal.SIGKILL)
                break
        finally:
            px.stop()

        exit_code = process.returncode
        if timed_out:
            return finish(ReviewOutcome.TIMEOUT, exit_code)
        if proxy_stopped:
            return finish(ReviewOutcome.FAILED, exit_code)
        if exit_code == 0 and output_path.is_file():
            try:
                if (
                    codex.command_events(
                        log_path.read_text(encoding="utf-8", errors="replace")
                    )
                    == 0
                ):
                    return finish(ReviewOutcome.INVALID, exit_code)
                state = save_review(
                    conn,
                    task_id,
                    output_path.read_text(encoding="utf-8"),
                    paths=paths,
                    home=home,
                    orch_home=orch_home,
                )
            except OSError, UnicodeError, ReviewFlowError:
                return finish(ReviewOutcome.INVALID, exit_code)
            return finish(ReviewOutcome.SAVED, exit_code, state)
        if codex.hit_usage_limit(
            log_path,
            patterns=base.patterns_for(config, codex.PROVIDER),
            environ=environ,
        ):
            return finish(ReviewOutcome.QUOTA, exit_code)
        return finish(ReviewOutcome.FAILED, exit_code)
    except BaseException:
        if process is not None:
            runner.stop_process(process)
        raise
    finally:
        try:
            if px is not None:
                px.stop()
        finally:
            if tmpdir is not None:
                shutil.rmtree(tmpdir, ignore_errors=True)
