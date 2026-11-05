"""The orch command line: subcommands, one-line JSON output and exit codes."""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import traceback
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from orch import (
    config,
    gate,
    lifecycle,
    prflow,
    review,
    runner,
    scheduler,
    store,
    taskfile,
)
from orch.models import Task, TaskState


class _UsageError(Exception):
    """Invalid command arguments or environment."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _UsageError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="orch")
    commands = parser.add_subparsers(dest="command", required=True)
    target = commands.add_parser("target")
    add = target.add_subparsers(dest="target_command", required=True).add_parser("add")
    add.add_argument("path")
    for option in ("name", "github", "setup", "test", "lint"):
        add.add_argument(f"--{option}", required=True)
    add.add_argument("--default-branch", default="main")
    add.add_argument("--shared", action="append", default=[])
    commands.add_parser("prepare").add_argument("spec")
    for name in (
        "dispatch",
        "run",
        "gate",
        "pr",
        "update",
        "close",
        "discard",
        "retry",
        "resume",
    ):
        command = commands.add_parser(name)
        command.add_argument("id")
        if name == "pr":
            command.add_argument("--resolution")
        elif name == "resume":
            command.add_argument("--keep", action="store_true")
    save = (
        commands.add_parser("review")
        .add_subparsers(dest="review_command", required=True)
        .add_parser("save")
    )
    save.add_argument("id")
    save.add_argument("--triage", type=Path)
    commands.add_parser("next")
    commands.add_parser("status").add_argument("--human", action="store_true")
    return parser


def _environment(command: str) -> tuple[Path, Path | None]:
    try:
        orch_home = config.orch_home()
    except config.ConfigError as exc:
        raise _UsageError(str(exc)) from exc
    if not orch_home.is_absolute():
        raise _UsageError("ORCH_HOME must be absolute")
    if os.environ.get("ORCH_FAKE_AGENTS") == "1":
        if not os.environ.get("ORCH_HOME") or (
            orch_home.resolve() == Path("~/.orch").expanduser().resolve()
        ):
            raise _UsageError("Fake agents require an explicit, separate ORCH_HOME")
    hub = None
    if command in {
        "target",
        "prepare",
        "run",
        "gate",
        "pr",
        "update",
        "close",
        "discard",
    }:
        value = os.environ.get("ORCH_HUB")
        if not value or not Path(value).is_absolute():
            raise _UsageError("ORCH_HUB must be an explicit absolute path")
        hub = Path(value)
    return orch_home, hub


def _get_task(conn: sqlite3.Connection, task_id: str) -> Task:
    task = store.get_task(conn, task_id)
    if task is None:
        raise store.TaskNotFoundError(f"Task not found: {task_id}")
    return task


def _check_target(target: str) -> None:
    session_target = os.environ.get("ORCH_TARGET")
    if session_target is not None and target != session_target:
        raise lifecycle.LifecycleError("Task target differs from ORCH_TARGET")


def _target(hub: Path, name: str) -> config.Target:
    target = config.load_targets(hub / "targets.toml").get(name)
    if target is None:
        raise config.ConfigError(f"Target is not registered: {name}")
    return target


def _load_spec(paths: config.TargetPaths, task_id: str) -> taskfile.TaskSpec:
    try:
        return taskfile.load_task(paths.tasks / f"{task_id}.md")
    except (OSError, UnicodeError) as exc:
        raise taskfile.TaskFileError(str(exc)) from exc


def _token(orch_home: Path, secrets: list[str]) -> str:
    try:
        result = subprocess.run(
            [str(orch_home / "bin" / "ghapp-token.sh")],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception:
        raise RuntimeError("Token script could not be executed") from None
    token = result.stdout.strip()
    if result.returncode != 0 or not token:
        raise RuntimeError("Token script failed or returned an empty token")
    secrets.append(token)
    return token


def _execute(
    args: argparse.Namespace, secrets: list[str]
) -> tuple[int, dict[str, object]]:
    orch_home, hub = _environment(args.command)
    home = Path.home()
    if args.command == "target":
        assert hub is not None
        target = config.add_target(
            hub / "targets.toml",
            args.name,
            path=Path(args.path),
            github=args.github,
            default_branch=args.default_branch,
            setup_command=args.setup,
            test_command=args.test,
            lint_command=args.lint,
            shared_paths=args.shared,
            home=home,
            orch_home=orch_home,
        )
        return 0, {"target": target.name}
    with closing(store.connect(config.state_db_path(orch_home))) as conn:
        return _command(conn, args, orch_home, hub, home, secrets)


def _command(
    conn: sqlite3.Connection,
    args: argparse.Namespace,
    orch_home: Path,
    hub: Path | None,
    home: Path,
    secrets: list[str],
) -> tuple[int, dict[str, object]]:
    command = args.command
    session_target = os.environ.get("ORCH_TARGET")
    if command == "prepare":
        try:
            with Path(args.spec).open(encoding="utf-8", newline="") as stream:
                spec = taskfile.parse_task(stream.read())
        except (OSError, UnicodeError) as exc:
            raise taskfile.TaskFileError(str(exc)) from exc
        _check_target(spec.target)
        assert hub is not None
        target = _target(hub, spec.target)
        paths = config.ensure_target(orch_home, target.name, target.github)
        task = lifecycle.prepare_task(conn, Path(args.spec), target, paths)
        return 0, {
            "task_id": task.id,
            "state": task.state,
            "worktree": task.worktree,
            "base_sha": task.base_sha,
        }
    if command == "status":
        tasks = store.list_tasks(conn, target=session_target)
        chains = scheduler.blocked_chains(conn, target=session_target)
        return 0, {
            "tasks": [
                {
                    key: getattr(task, key)
                    for key in (
                        "id",
                        "target",
                        "title",
                        "state",
                        "grade",
                        "lane",
                        "attempt",
                        "pr_url",
                    )
                }
                for task in tasks
            ],
            "blocked_chains": [asdict(chain) for chain in chains],
        }

    # Check task ownership before loading target configuration or issuing a token.
    if hasattr(args, "id"):
        task = _get_task(conn, args.id)
        _check_target(task.target)
        paths = config.target_paths(orch_home, task.target)
        if hub is not None:
            target = _target(hub, task.target)

    settings = config.load_config(orch_home)
    scheduling = {"limits": settings.limits, "ttl_min": settings.reservation_ttl_min}
    if command == "next":

        def modify_of(task: Task) -> tuple[str, ...]:
            return _load_spec(
                config.target_paths(orch_home, task.target), task.id
            ).modify

        report = scheduler.next_reservations(conn, modify_of=modify_of, **scheduling)

        def visible(task_id: str) -> bool:
            return (
                session_target is None
                or _get_task(conn, task_id).target == session_target
            )

        return 0, {
            "reserved": [item for item in report.reserved if visible(item)],
            "newly_blocked": [item for item in report.newly_blocked if visible(item)],
            "blocked_chains": [
                asdict(item) for item in report.chains if visible(item.task_id)
            ],
        }
    task_id = args.id
    if command in {"dispatch", "retry", "resume"}:
        if command == "dispatch":
            decision = scheduler.dispatch(
                conn, _load_spec(paths, task_id), **scheduling
            )
        elif command == "retry":
            decision = lifecycle.retry_task(conn, task_id, paths=paths, **scheduling)
        else:
            decision = lifecycle.resume_task(
                conn, task_id, paths=paths, keep=args.keep, **scheduling
            )
        return 0, {"task_id": task_id, **asdict(decision)}
    if command == "run":
        result = runner.run_task(
            conn,
            task_id,
            target=target,
            config=settings,
            paths=paths,
            environ=os.environ,
        )
        state = lifecycle.settle_run(conn, task_id)
        code = {
            "DONE": 0,
            "PARTIAL": 10,
            "FAILED": 20,
            "BLOCKED": 30,
            "BLOCKED_QUOTA": 40,
        }[result.status]
        return code, {
            "task_id": task_id,
            "status": result.status,
            "failure_reason": result.failure_reason,
            "state": state,
        }
    if command == "gate":
        if review.has_blocking_review(paths, task_id):
            raise gate.GateError("Blocking review must be resolved before gating")
        report = gate.gate_task(
            conn,
            _load_spec(paths, task_id),
            target,
            main_ref=f"origin/{target.default_branch}",
            log_path=paths.logs / f"{task_id}.gate.log",
            home=home,
            orch_home=orch_home,
        )
        return (0 if report.passed else 20), {
            "task_id": task_id,
            "state": _get_task(conn, task_id).state,
            **asdict(report),
        }
    if command == "review":
        try:
            text = sys.stdin.read()
        except (OSError, UnicodeError) as exc:
            raise review.ReviewFlowError(str(exc)) from exc
        state = review.save_review(
            conn,
            task_id,
            text,
            paths=paths,
            home=home,
            orch_home=orch_home,
            triage_file=args.triage,
        )
        return 0, {"task_id": task_id, "state": state}
    token = None
    if command in {"pr", "update", "close"} or (command == "discard" and task.pr_url):
        token = _token(orch_home, secrets)
    if command == "pr":
        report = prflow.open_pull_request(
            conn,
            _load_spec(paths, task_id),
            target,
            token=token,
            log_path=paths.logs / f"{task_id}.pr.log",
            review_path=paths.review_path(task_id),
            triage_path=paths.triage_path(task_id),
            home=home,
            orch_home=orch_home,
            resolution=args.resolution,
        )
        return (0 if report.state == TaskState.PR_OPEN else 20), {
            "task_id": task_id,
            **asdict(report),
        }
    if command == "update":
        report = prflow.update_pull_request(
            conn,
            _load_spec(paths, task_id),
            target,
            token=token,
            log_path=paths.logs / f"{task_id}.update.log",
            home=home,
            orch_home=orch_home,
        )
        passed = (
            report.state == TaskState.PR_OPEN
            and report.gates is not None
            and report.gates.passed
        )
        return (0 if passed else 20), {"task_id": task_id, **asdict(report)}
    if command == "close":
        prflow.close_task(conn, task_id, target, token=token)
    else:
        lifecycle.discard_task(conn, task_id, target, token=token)
    return 0, {"task_id": task_id, "state": _get_task(conn, task_id).state}


def _redact(text: str, secrets: list[str]) -> str:
    for token in secrets:
        escaped = json.dumps(token, ensure_ascii=False)[1:-1]
        text = text.replace(escaped, "[REDACTED]")
        text = text.replace(token, "[REDACTED]")
    return text


def main(argv: list[str] | None = None) -> int:
    """Run one orch command and return its exit code."""
    secrets: list[str] = []
    try:
        try:
            args = _parser().parse_args(argv)
            code, data = _execute(args, secrets)
            if args.command == "status" and args.human:
                for task in data["tasks"]:
                    print(_redact(f"{task['id']} {task['state']}", secrets))
                for chain in data["blocked_chains"]:
                    line = (
                        f"{chain['task_id']} {chain['state']}: "
                        f"{', '.join(chain['causes'])}"
                    )
                    print(_redact(line, secrets))
                return code
            output = {"ok": True, **data}
        except _UsageError as exc:
            code = 64
            output = {
                "ok": False,
                "error": "usage",
                "message": f"인자 또는 환경 변수가 잘못되었습니다: {exc}",
            }
        except (
            lifecycle.LifecycleError,
            review.ReviewFlowError,
            gate.GateError,
            runner.RunError,
            scheduler.DispatchError,
            prflow.PullRequestFlowError,
            config.ConfigError,
            taskfile.TaskFileError,
            store.TaskNotFoundError,
            store.StateMismatchError,
            store.IllegalTransitionError,
        ) as exc:
            code = 64
            output = {
                "ok": False,
                "error": "refused",
                "message": f"명령을 수행할 수 없습니다: {exc}",
            }
        except Exception as exc:
            code = 70
            output = {
                "ok": False,
                "error": "internal",
                "message": f"내부 오류가 발생했습니다: {exc}",
            }
        print(_redact(json.dumps(output, ensure_ascii=False), secrets))
        return code
    except BaseException as exc:
        if isinstance(exc, SystemExit) and not secrets:
            raise
        try:
            sys.stderr.write(_redact("".join(traceback.format_exception(exc)), secrets))
        except BaseException:
            pass
        try:
            output = {
                "ok": False,
                "error": "internal",
                "message": "내부 오류가 발생했습니다.",
            }
            print(_redact(json.dumps(output, ensure_ascii=False), secrets))
        except BaseException:
            pass
        return 70
