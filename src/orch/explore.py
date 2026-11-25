"""orch explore: read-only Codex exploration with checked answers (design 4.16)."""

import fcntl
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from orch import proxy, review, runner
from orch.config import Config, Target, TargetPaths
from orch.lanes import base, codex

EXPLORE_TIMEOUT_SEC = 900
QUESTION_LIMIT_BYTES = 8192


class ExploreError(Exception):
    """A refused explore: nothing ran, no slot or log was made."""


class ExploreOutcome(StrEnum):
    """How an explore run ended."""

    SAVED = "saved"
    INVALID = "invalid"
    FAILED = "failed"
    QUOTA = "quota"
    TIMEOUT = "timeout"
    BUSY = "busy"


@dataclass(frozen=True, slots=True, kw_only=True)
class ExploreRun:
    """The result of one explore run. It never holds answer text."""

    explore_id: str
    outcome: ExploreOutcome
    exit_code: int | None
    answer_path: Path
    log_path: Path | None
    claims: int
    unresolved: int
    proxy_denials: int


def explore_root(paths: TargetPaths) -> Path:
    """Return the target's runtime explore/ directory."""
    return paths.root / "explore"


def next_explore_log(logs: Path, explore_id: str) -> Path:
    """Return logs/explore/<n>.<k>.log for the next run, without creating it."""
    return runner.next_log_path(logs / "explore", explore_id)


def read_question(question: Path, paths: TargetPaths) -> tuple[str, str]:
    """Check a question file and return its id and text."""
    try:
        if not stat.S_ISREG(question.lstat().st_mode):
            raise ExploreError("Question must be a regular file, not a symlink")
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}\.md", question.name) is None:
            raise ExploreError("Question must be named <id>.md with a valid id")
        if question.resolve().parent != explore_root(paths).resolve():
            raise ExploreError("Question must be directly inside explore/")
        with question.open("rb") as stream:
            content = stream.read(QUESTION_LIMIT_BYTES + 1)
        if len(content) > QUESTION_LIMIT_BYTES:
            raise ExploreError("Question exceeds the byte limit")
        text = content.decode("utf-8")
        if not text.strip():
            raise ExploreError("Question must not be empty")
    except (OSError, UnicodeError, RuntimeError) as exc:
        raise ExploreError("Cannot read question as a regular UTF-8 file") from exc
    return question.stem, text


def resolve_dir(dir_arg: Path | None, *, target: Target, paths: TargetPaths) -> Path:
    """Return the real directory to explore: the target or one of its worktrees."""
    try:
        root = target.path.resolve()
        if dir_arg is None:
            return root
        directory = dir_arg.resolve()
        if directory == root or (
            directory.is_dir() and directory.parent == paths.worktrees.resolve()
        ):
            return directory
    except (OSError, RuntimeError) as exc:
        raise ExploreError("Cannot resolve explore directory") from exc
    raise ExploreError("Directory must be the target or one of its worktrees")


def explore_prompt(explore_id: str, question: str) -> str:
    """Return the English prompt for an explore run."""
    return (
        f'Explore id: "{explore_id}"\n'
        "Read the repository to answer the question below. "
        "Do not modify any files.\n"
        "Your final message must be exactly one JSON object with these fields:\n"
        f'- "explore_id": "{explore_id}"\n'
        '- "claims": an array of at most 40 objects, each containing '
        '"statement" (one fact in Korean, 1-600 characters), '
        '"path" (a path relative to the working directory using forward slashes, '
        '1-300 characters), "line" (an integer line number starting at 1), and '
        '"method" ("read" or "grep"). Cite existing files and lines.\n'
        '- "unresolved": an array of at most 20 strings in Korean '
        "(1-300 characters each) describing what could not be confirmed.\n"
        "Use exactly these fields, with no Markdown fences or other text in "
        "the final message.\n\nQuestion:\n"
        f"{question}"
    )


def check_answer(text: str, *, explore_id: str, root: Path) -> dict[str, object]:
    """Return the parsed answer, or raise ValueError."""
    try:
        data = json.loads(text)
    except RecursionError as exc:
        raise ValueError("Answer JSON is nested too deeply") from exc
    if not isinstance(data, dict) or data.keys() != {
        "explore_id",
        "claims",
        "unresolved",
    }:
        raise ValueError("Invalid answer fields")
    answer_id = data["explore_id"]
    if (
        not isinstance(answer_id, str)
        or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", answer_id) is None
        or answer_id != explore_id
    ):
        raise ValueError("Answer id must match the question")
    claims = data["claims"]
    unresolved = data["unresolved"]
    if not isinstance(claims, list) or len(claims) > 40:
        raise ValueError("Invalid claims array")
    if not isinstance(unresolved, list) or len(unresolved) > 20:
        raise ValueError("Invalid unresolved array")
    for item in unresolved:
        if not isinstance(item, str) or not 1 <= len(item) <= 300:
            raise ValueError("Invalid unresolved item")
    for claim in claims:
        if not isinstance(claim, dict) or claim.keys() != {
            "statement",
            "path",
            "line",
            "method",
        }:
            raise ValueError("Invalid claim fields")
        statement = claim["statement"]
        path = claim["path"]
        line = claim["line"]
        if not isinstance(statement, str) or not 1 <= len(statement) <= 600:
            raise ValueError("Invalid claim statement")
        if not isinstance(path, str) or not 1 <= len(path) <= 300:
            raise ValueError("Invalid claim path")
        if type(line) is not int or line < 1:
            raise ValueError("Claim line must be a positive integer")
        if claim["method"] not in ("read", "grep"):
            raise ValueError("Invalid claim method")
        if path.startswith("/") or any(p in ("", ".", "..") for p in path.split("/")):
            raise ValueError("Claim path must be relative without dot or empty parts")
        try:
            file = (root / path).resolve()
            if not file.is_relative_to(root.resolve()) or not file.is_file():
                raise ValueError("Claim path must name a regular file inside the root")
            content = file.read_bytes()
        except (OSError, RuntimeError) as exc:
            raise ValueError("Cannot read claim file") from exc
        lines = content.count(b"\n") + int(
            bool(content) and not content.endswith(b"\n")
        )
        if line > lines:
            raise ValueError("Claim line is past the end of the file")
    return data


def acquire_slot(paths: TargetPaths, explore_id: str, limit: int) -> int | None:
    """Hold <n>.lock and return its descriptor, or None when busy."""
    root = explore_root(paths)
    fd: int | None = None
    try:
        root.mkdir(parents=True, exist_ok=True)
        with (root / ".slots.lock").open("a+b") as guard:
            fcntl.flock(guard.fileno(), fcntl.LOCK_EX)
            active = 0
            same_question = False
            for lock in root.glob("*.lock"):
                if lock.name == ".slots.lock":
                    continue
                try:
                    handle = lock.open("r+b")
                except FileNotFoundError:
                    continue
                with handle:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        active += 1
                        same_question |= lock.name == f"{explore_id}.lock"
                    else:
                        lock.unlink(missing_ok=True)
            if same_question or active >= limit:
                return None
            lock = root / f"{explore_id}.lock"
            try:
                fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.ftruncate(fd, 0)
                os.write(fd, f"{os.getpid()}\n".encode("ascii"))
                return fd
            except BaseException:
                if fd is not None:
                    with suppress(OSError):
                        lock.unlink(missing_ok=True)
                    with suppress(OSError):
                        os.close(fd)
                raise
    except BaseException as exc:
        if isinstance(exc, OSError):
            raise ExploreError("Cannot acquire explore slot") from exc
        raise


def release_slot(paths: TargetPaths, explore_id: str, fd: int) -> None:
    """Remove <n>.lock, then close the descriptor that held it."""
    try:
        (explore_root(paths) / f"{explore_id}.lock").unlink(missing_ok=True)
    finally:
        os.close(fd)


def _save_answer(path: Path, answer: dict[str, object]) -> None:
    """Replace an answer atomically, removing a temporary file on failure."""
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(answer, stream, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run_explore(
    question: Path,
    *,
    dir_arg: Path | None,
    target: Target,
    config: Config,
    paths: TargetPaths,
    environ: Mapping[str, str],
    timeout_sec: int | None = None,
) -> ExploreRun:
    """Run one explore question under the Codex review sandbox."""
    explore_id, question_text = read_question(question, paths)
    worktree = resolve_dir(dir_arg, target=target, paths=paths)
    fd = acquire_slot(paths, explore_id, config.limits.lane_a)
    answer_path = explore_root(paths) / f"{explore_id}.json"
    log_path: Path | None = None
    px: proxy.AllowlistProxy | None = None
    tmpdir: Path | None = None
    process: subprocess.Popen[bytes] | None = None

    def finish(
        outcome: ExploreOutcome,
        exit_code: int | None,
        *,
        claims: int = 0,
        unresolved: int = 0,
    ) -> ExploreRun:
        if px is not None:
            px.stop()
        return ExploreRun(
            explore_id=explore_id,
            outcome=outcome,
            exit_code=exit_code,
            answer_path=answer_path,
            log_path=log_path,
            claims=claims,
            unresolved=unresolved,
            proxy_denials=len(px.denials()) if px is not None else 0,
        )

    if fd is None:
        return finish(ExploreOutcome.BUSY, None)
    try:
        try:
            log_path = next_explore_log(paths.logs, explore_id)
            output_path = log_path.with_suffix(".out")
            proxy_log = log_path.with_suffix(".proxy.log")
            log_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.unlink(missing_ok=True)
            hub = Path(environ["ORCH_HUB"])
            if not hub.is_absolute():
                raise base.LaneError("Codex exploration requires an absolute ORCH_HUB")
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
                prompt=explore_prompt(explore_id, question_text),
                output_path=output_path,
                model=review.REVIEW_MODEL,
                effort=review.REVIEW_EFFORT,
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
            return finish(ExploreOutcome.FAILED, None)

        timed_out = False
        proxy_stopped = False
        deadline = time.monotonic() + (
            EXPLORE_TIMEOUT_SEC if timeout_sec is None else timeout_sec
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
            return finish(ExploreOutcome.TIMEOUT, exit_code)
        if proxy_stopped:
            return finish(ExploreOutcome.FAILED, exit_code)
        if exit_code == 0 and output_path.is_file():
            try:
                if (
                    codex.command_events(
                        log_path.read_text(encoding="utf-8", errors="replace")
                    )
                    == 0
                ):
                    return finish(ExploreOutcome.INVALID, exit_code)
                answer = check_answer(
                    output_path.read_text(encoding="utf-8"),
                    explore_id=explore_id,
                    root=worktree,
                )
            except OSError, ValueError:
                return finish(ExploreOutcome.INVALID, exit_code)
            try:
                _save_answer(answer_path, answer)
            except OSError, UnicodeError:
                return finish(ExploreOutcome.FAILED, exit_code)
            return finish(
                ExploreOutcome.SAVED,
                exit_code,
                claims=len(answer["claims"]),
                unresolved=len(answer["unresolved"]),
            )
        if codex.hit_usage_limit(
            log_path,
            patterns=base.patterns_for(config, codex.PROVIDER),
            environ=environ,
        ):
            return finish(ExploreOutcome.QUOTA, exit_code)
        return finish(ExploreOutcome.FAILED, exit_code)
    except BaseException:
        if process is not None:
            runner.stop_process(process)
        raise
    finally:
        try:
            if px is not None:
                px.stop()
        finally:
            try:
                if tmpdir is not None:
                    shutil.rmtree(tmpdir, ignore_errors=True)
            finally:
                release_slot(paths, explore_id, fd)
