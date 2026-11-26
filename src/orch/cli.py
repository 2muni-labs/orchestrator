"""The orch command line: subcommands, one-line JSON output and exit codes."""

import argparse
import importlib.metadata
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import traceback
from contextlib import closing
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from orch import (
    config,
    credentials,
    doctor,
    events,
    explore,
    fulltest,
    gate,
    gc,
    gitops,
    hooks,
    launcher,
    lifecycle,
    notify,
    plans,
    prflow,
    render,
    review,
    roadmap,
    runner,
    scheduler,
    selfcheck,
    selfcheck_codex,
    service,
    show,
    store,
    taskfile,
    tracking,
    tracksync,
    versions,
    waiter,
    watch,
)
from orch.models import Lane, Task, TaskState
from orch.terminal import (
    MainPaneStatus,
    TerminalError,
    TmuxTerminal,
    load_record,
    record_path,
)

_REFRESH_COMMANDS = {
    "prepare",
    "dispatch",
    "run",
    "gate",
    "review",
    "pr",
    "update",
    "close",
    "discard",
    "retry",
    "resume",
    "kill",
    "next",
}


AGENT_PAUSE = "agent_unverified"
_HOOK_NAMES = (
    "session-start",
    "pre-bash",
    "subagent-stop",
    "notification",
    "permission-request",
    "post-tool-use",
    "post-tool-use-failure",
    "user-prompt-submit",
    "stop",
    "session-end",
)


class _UsageError(Exception):
    """Invalid command arguments or environment."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _UsageError(message)


class _VersionAction(argparse.Action):
    def __init__(self, option_strings: list[str], dest: str, **kwargs: object) -> None:
        super().__init__(option_strings, dest, nargs=0, **kwargs)

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        print(f"orch {importlib.metadata.version('orch')}")
        parser.exit()


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _nonnegative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be a nonnegative integer")
    return number


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="orch")
    parser.add_argument(
        "--version", action=_VersionAction, help="Show the package version and exit"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    target = commands.add_parser("target")
    add = target.add_subparsers(dest="target_command", required=True).add_parser("add")
    add.add_argument("path")
    for option in ("name", "github", "setup", "test", "lint"):
        add.add_argument(f"--{option}", required=True)
    add.add_argument("--default-branch", default="main")
    add.add_argument("--shared", action="append", default=[])
    commands.add_parser("prepare").add_argument("spec")
    commands.add_parser("show").add_argument("id")
    events_parser = commands.add_parser("events")
    events_parser.add_argument("--follow", action="store_true")
    events_parser.add_argument("--since", type=_nonnegative_int, default=0)
    events_parser.add_argument("--target")
    commands.add_parser("roadmap").add_argument("--target")
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
        "kill",
    ):
        command = commands.add_parser(name)
        command.add_argument("id")
        if name == "pr":
            command.add_argument("--resolution")
        elif name == "resume":
            command.add_argument("--keep", action="store_true")
    review_parser = commands.add_parser(
        "review",
        usage="orch review ID --by codex | orch review save ID [--triage FILE]",
    )
    review_parser.add_argument("id", metavar="ID|save")
    review_parser.add_argument("save_id", metavar="ID", nargs="?")
    review_parser.add_argument("--by", choices=("codex",))
    review_parser.add_argument("--triage", type=Path, metavar="FILE")
    explore_parser = commands.add_parser("explore")
    explore_parser.add_argument("question", type=Path, metavar="QUESTION")
    explore_parser.add_argument("--dir", type=Path, metavar="DIR")
    wait = commands.add_parser("wait")
    wait.add_argument("id", nargs="?")
    wait.add_argument("--pr")
    wait.add_argument("--merged", action="store_true")
    wait.add_argument("--interval", type=_positive_int, default=60)
    wait.add_argument("--max-wait", type=_positive_int, default=720)
    commands.add_parser("next")
    commands.add_parser("notify").add_subparsers(
        dest="notify_command", required=True
    ).add_parser("test")
    commands.add_parser("status").add_argument("--human", action="store_true")
    watch_parser = commands.add_parser("watch")
    watch_parser.add_argument("--interval", type=_positive_int, default=2)
    watch_parser.add_argument("--once", action="store_true")
    watch_parser.add_argument("--panels", action="store_true")
    open_parser = commands.add_parser("open")
    open_parser.add_argument("name")
    open_parser.add_argument("--no-attach", action="store_true")
    commands.add_parser("hook").add_argument("name", choices=_HOOK_NAMES)
    commands.add_parser("doctor")
    commands.add_parser("versions")
    selfcheck_parser = commands.add_parser("selfcheck")
    switch = selfcheck_parser.add_mutually_exclusive_group()
    switch.add_argument(
        "--apply", nargs="?", const="claude", choices=("claude", "codex")
    )
    switch.add_argument("--rollback", choices=("claude", "codex"))
    commands.add_parser("gc").add_argument("--dry-run", action="store_true")
    service_commands = commands.add_parser("service").add_subparsers(
        dest="service_command", required=True
    )
    for name in ("install", "uninstall", "restore"):
        service_commands.add_parser(name)
    sandbox_run = (
        commands.add_parser("sandbox")
        .add_subparsers(dest="sandbox_command", required=True)
        .add_parser("run")
    )
    sandbox_run.add_argument("--dir")
    sandbox_run.add_argument("argv", nargs=argparse.REMAINDER)
    track = commands.add_parser("track").add_subparsers(
        dest="track_command", required=True
    )
    track_sync = track.add_parser("sync")
    track_sync.add_argument("--dry-run", action="store_true")
    track_sync.add_argument("--all", action="store_true")
    track_sync.add_argument("--target")
    track.add_parser("import").add_argument("--target")
    track_bind = track.add_parser("bind")
    track_bind.add_argument("task_id")
    track_bind.add_argument("record")
    track_bind.add_argument("--target")
    track_accept = track.add_parser("accept")
    track_accept.add_argument("record")
    track_accept.add_argument("--target")
    track.add_parser("status").add_argument("--target")
    fulltest_parser = commands.add_parser("fulltest")
    fulltest_parser.add_argument("--all", action="store_true")
    fulltest_parser.add_argument("--target")
    return parser


def _terminal(session: str, record: Path) -> TmuxTerminal:
    return TmuxTerminal(session, record)


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _which_absolute(name: str) -> Path:
    value = shutil.which(name)
    if value is None or not Path(value).is_absolute():
        raise config.ConfigError(f"Cannot locate an absolute path for {name}")
    return Path(value)


def _codex_model(hub: Path, orch_home: Path) -> str:
    """Read the Codex model selected by selfcheck."""
    try:
        return selfcheck_codex.recorded_model(hub, orch_home)[0]
    except selfcheck.SelfcheckError as exc:
        raise config.ConfigError("Cannot read the recorded Codex model") from exc


def _environment(
    command: str, *, require_hub: bool = False
) -> tuple[Path, Path | None]:
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
    if require_hub or command in {
        "target",
        "open",
        "selfcheck",
        "prepare",
        "run",
        "gate",
        "pr",
        "update",
        "close",
        "discard",
        "wait",
    }:
        value = os.environ.get("ORCH_HUB")
        if not value or not Path(value).is_absolute():
            raise _UsageError("ORCH_HUB must be an explicit absolute path")
        hub = Path(value)
    return orch_home, hub


def _check_agent(
    conn: sqlite3.Connection,
    *,
    settings: config.Config,
    orch_home: Path,
    home: Path,
    now: datetime,
    secrets: list[str],
) -> dict[str, selfcheck.CurrentCheck]:
    currents = {
        "claude": (
            selfcheck.CurrentCheck("error", "not_configured", None)
            if settings.claude_path is None
            else selfcheck.check_current_claude(
                orch_home=orch_home, claude_path=settings.claude_path
            )
        ),
        "codex": (
            selfcheck.CurrentCheck("error", "not_configured", None)
            if settings.codex_path is None
            else selfcheck_codex.check_current_codex(
                orch_home=orch_home, codex_path=settings.codex_path
            )
        ),
    }
    unverified = {
        agent: current for agent, current in currents.items() if current.status != "ok"
    }
    if not unverified:
        store.remove_pause(conn, AGENT_PAUSE)
    else:
        details = []
        for agent, current in unverified.items():
            detail = (
                "no_record"
                if current.status == "no_record"
                else f"{current.status}:{current.reason}"
            )
            details.append(f"{agent}={detail}")
        if store.add_pause(
            conn, AGENT_PAUSE, since=now.isoformat(), note=",".join(details)
        ):
            _send_notification(
                "agent_unverified",
                settings=settings,
                orch_home=orch_home,
                home=home,
                secrets=secrets,
                target=",".join(unverified),
            )
    return currents


def _ready_candidate(orch_home: Path) -> str | None:
    try:
        state = selfcheck.load_agents_state(selfcheck.agents_state_path(orch_home))
    except selfcheck.SelfcheckError:
        return None
    candidate = state["agents"].get("claude", {}).get("candidate")
    if isinstance(candidate, dict) and candidate.get("status") == "ready":
        return candidate["version"]
    return None


def _start_selfcheck(orch_home: Path, hub: Path) -> None:
    with (orch_home / "selfcheck.log").open("a", encoding="utf-8") as log:
        subprocess.Popen(
            [str(_which_absolute("orch")), "selfcheck"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={**os.environ, "ORCH_HUB": str(hub), "ORCH_HOME": str(orch_home)},
        )


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


def _send_notification(
    event: str,
    *,
    settings: config.Config,
    orch_home: Path,
    home: Path,
    secrets: list[str],
    target: str = "",
    task_id: str = "",
    dedup: bool = True,
) -> notify.Outcome:
    try:
        return notify.send(
            event,
            settings=settings,
            orch_home=orch_home,
            home=home,
            now=datetime.now(UTC),
            target=target,
            task_id=task_id,
            dedup=dedup,
        )
    except Exception as exc:
        try:
            message = credentials.redact_secrets(f"알림 전송 실패: {exc}", secrets)
            print(" ".join(message.splitlines()), file=sys.stderr)
        except Exception:
            pass
        return notify.Outcome.FAILED


def _refresh_target_plans(
    conn: sqlite3.Connection,
    orch_home: Path,
    target: str,
    now: datetime,
    secrets: list[str],
) -> None:
    """Refresh one target's plans, reporting failures without failing the command."""
    try:
        plans.refresh_plans(conn, orch_home, target, now)
    except Exception as exc:
        try:
            message = credentials.redact_secrets(f"계획 파일 갱신 실패: {exc}", secrets)
            print(" ".join(message.splitlines()), file=sys.stderr)
        except Exception:
            pass


def _refresh_todos(
    conn: sqlite3.Connection, orch_home: Path, secrets: list[str]
) -> None:
    def warn(exc: Exception) -> None:
        try:
            message = credentials.redact_secrets(f"TODO.md 갱신 실패: {exc}", secrets)
            print(" ".join(message.splitlines()), file=sys.stderr)
        except Exception:
            pass

    try:
        tasks_by_target: dict[str, list[Task]] = {}
        for task in store.list_tasks(conn):
            tasks_by_target.setdefault(task.target, []).append(task)
        now = datetime.now(UTC)
    except Exception as exc:
        warn(exc)
        return
    for target, tasks in tasks_by_target.items():
        try:
            text = render.todo_text(
                tasks, scheduler.blocked_chains(conn, target=target), now
            )
            render.write_todo(config.target_paths(orch_home, target).todo, text)
        except Exception as exc:
            warn(exc)
    try:
        targets = tracking.list_targets(conn)
    except Exception as exc:
        try:
            message = credentials.redact_secrets(f"계획 파일 갱신 실패: {exc}", secrets)
            print(" ".join(message.splitlines()), file=sys.stderr)
        except Exception:
            pass
        return
    for target in targets:
        _refresh_target_plans(conn, orch_home, target, now, secrets)


def _fulltest(
    args: argparse.Namespace, secrets: list[str]
) -> tuple[int, dict[str, object]]:
    """Run orch fulltest for one target or, with --all, for every target."""
    orch_home, hub = _environment("fulltest", require_hub=True)
    assert hub is not None
    if args.all:
        if args.target is not None or "ORCH_TARGET" in os.environ:
            raise _UsageError("--all cannot be combined with --target or ORCH_TARGET")
        registered = config.load_targets(hub / "targets.toml")
        targets = [registered[name] for name in sorted(registered)]
    else:
        name = args.target or os.environ.get("ORCH_TARGET")
        if not name:
            raise _UsageError("--target or ORCH_TARGET is required")
        if args.target is not None:
            _check_target(name)
        targets = [_target(hub, name)]
    settings = config.load_config(orch_home)
    if settings.notify_topic:
        secrets.append(settings.notify_topic)
    reports: list[dict[str, object]] = []
    failed = False
    with closing(store.connect(config.state_db_path(orch_home))) as conn:
        for target in targets:
            report = fulltest.run_fulltest(
                conn,
                target,
                config.target_paths(orch_home, target.name),
                timeout_sec=settings.gate_timeout_min * 60,
            )
            reports.append(asdict(report))
            if report.outcome not in {
                fulltest.FulltestOutcome.PASSED,
                fulltest.FulltestOutcome.BUSY,
            }:
                failed = True
                _send_notification(
                    "fulltest_failed",
                    settings=settings,
                    orch_home=orch_home,
                    home=Path.home(),
                    secrets=secrets,
                    target=target.name,
                )
    if failed:
        return 20, {
            "ok": False,
            "error": "fulltest_failed",
            "message": "기본 브랜치 전체 테스트가 통과하지 못한 대상이 있습니다.",
            "results": reports,
        }
    return 0, {"ok": True, "results": reports}


def _track(
    args: argparse.Namespace, secrets: list[str]
) -> tuple[int, dict[str, object]]:
    """Run one orch track command."""
    orch_home, hub = _environment("track", require_hub=True)
    assert hub is not None
    all_targets = args.track_command == "sync" and args.all
    if all_targets:
        if args.target is not None or "ORCH_TARGET" in os.environ:
            raise _UsageError("--all cannot be combined with --target or ORCH_TARGET")
        registered = config.load_targets(hub / "targets.toml")
        targets = [registered[name] for name in sorted(registered)]
    else:
        name = args.target or os.environ.get("ORCH_TARGET")
        if not name:
            raise _UsageError("--target or ORCH_TARGET is required")
        if args.target is not None:
            _check_target(name)
        targets = [_target(hub, name)]

    with closing(store.connect(config.state_db_path(orch_home))) as conn:
        if args.track_command == "sync":
            settings = config.load_config(orch_home)
            if settings.notify_topic:
                secrets.append(settings.notify_topic)
            reports: list[dict[str, object]] = []
            for target in targets:
                try:
                    if not args.dry_run:
                        config.target_paths(orch_home, target.name).root.mkdir(
                            parents=True, exist_ok=True
                        )
                    report = tracksync.sync(
                        conn=conn,
                        target=target.name,
                        github_repo=target.github,
                        orch_home=orch_home,
                        home=Path.home(),
                        now=datetime.now(UTC),
                        token_provider=lambda: credentials.issue_token(
                            orch_home, secrets
                        ),
                        notify_failure=lambda name=target.name: _send_notification(
                            "doctor_track_failed",
                            settings=settings,
                            orch_home=orch_home,
                            home=Path.home(),
                            secrets=secrets,
                            target=name,
                        ),
                        dry_run=args.dry_run,
                    )
                    reports.append(asdict(report))
                except Exception:
                    if not all_targets:
                        raise
                    reports.append({"target": target.name, "outcome": "error"})
            if any(
                report["outcome"] in {"failed", "error"} or report.get("failed")
                for report in reports
            ):
                return 20, {
                    "ok": False,
                    "error": "track_failed",
                    "message": "GitHub 작업 기록 동기화에 실패한 기록이 있습니다.",
                    "reports": reports,
                }
            return 0, {"ok": True, "reports": reports}

        target = targets[0]
        name = target.name
        if args.track_command == "import":
            path = config.target_paths(orch_home, name).root / tracksync.FILE_NAME
            if not path.is_file():
                raise tracking.TrackingError("Tracking file not found")
            contents = tracking.load_file(path, target=name)
            if contents.repository.lower() != target.github.lower():
                raise tracking.TrackingError("Tracking repository differs from target")
            imported = tracking.import_file(
                conn, path, target=name, at=datetime.now(UTC).isoformat()
            )
            _refresh_target_plans(conn, orch_home, name, datetime.now(UTC), secrets)
            return 0, {"ok": True, "target": name, **asdict(imported)}

        if args.track_command in {"bind", "accept"}:
            record = tracking.resolve(conn, name, args.record)
            if record is None:
                raise tracking.TrackingError(f"Record not found: {args.record}")
            if args.track_command == "bind":
                changed = tracking.bind_task(
                    conn,
                    target=name,
                    task_id=args.task_id,
                    record_id=record.record_id,
                )
                _refresh_target_plans(conn, orch_home, name, datetime.now(UTC), secrets)
                return 0, {
                    "ok": True,
                    "target": name,
                    "task_id": args.task_id,
                    "record_id": record.record_id,
                    "changed": changed,
                }
            changed = tracking.accept_record(
                conn,
                target=name,
                record_id=record.record_id,
                at=datetime.now(UTC).isoformat(),
            )
            accepted = tracking.resolve(conn, name, record.record_id)
            assert accepted is not None
            _refresh_target_plans(conn, orch_home, name, datetime.now(UTC), secrets)
            return 0, {
                "ok": True,
                "target": name,
                "record_id": accepted.record_id,
                "accepted_at": accepted.accepted_at,
                "changed": changed,
            }

        records = tracking.list_records(conn, name)
        by_owner: dict[str, int] = {}
        failing: list[dict[str, object]] = []
        for record in sorted(records, key=lambda record: record.record_id):
            owner = record.completion_owner
            by_owner[owner] = by_owner.get(owner, 0) + 1
            state = tracking.get_state(conn, name, record.record_id)
            if state is not None and state.failures > 0:
                failing.append(
                    {
                        "record_id": record.record_id,
                        "failures": state.failures,
                        "last_error": state.last_error,
                    }
                )
        run = tracking.get_run(conn, name)
        return 0, {
            "ok": True,
            "target": name,
            "repository": tracking.repository(conn, name),
            "records": len(records),
            "by_owner": dict(sorted(by_owner.items())),
            "accepted": sum(record.accepted_at is not None for record in records),
            "failing": failing,
            "run": asdict(run) if run is not None else None,
        }


def _events(args: argparse.Namespace) -> tuple[int, dict[str, object]]:
    target = args.target
    if target is not None:
        try:
            config.validate_target_name(target)
        except config.TargetNameError as exc:
            raise _UsageError(str(exc)) from exc
        _check_target(target)
    else:
        target = os.environ.get("ORCH_TARGET")
    orch_home, _ = _environment(args.command)
    stopped = False

    def stop(signum: int, frame: object) -> None:
        nonlocal stopped
        stopped = True

    def write(line: str) -> None:
        sys.stdout.write(line)
        sys.stdout.flush()

    previous = signal.signal(signal.SIGTERM, stop) if args.follow else None
    try:
        with closing(store.connect(config.state_db_path(orch_home))) as conn:
            events.stream(
                conn,
                write,
                since=args.since,
                target=target,
                follow=args.follow,
                stopped=lambda: stopped,
            )
    finally:
        if args.follow:
            signal.signal(signal.SIGTERM, previous)
    return 0, {}


def _roadmap(args: argparse.Namespace) -> tuple[int, dict[str, object]]:
    name = args.target
    if name is not None:
        try:
            config.validate_target_name(name)
        except config.TargetNameError as exc:
            raise _UsageError(str(exc)) from exc
        _check_target(name)
    else:
        name = os.environ.get("ORCH_TARGET")
    if not name:
        return 64, {
            "ok": False,
            "error": "no_target",
            "message": "Specify --target or ORCH_TARGET",
        }
    orch_home, hub = _environment("roadmap", require_hub=True)
    assert hub is not None
    try:
        target = _target(hub, name)
    except config.ConfigError as exc:
        return 64, {"ok": False, "error": "unknown_target", "message": str(exc)}
    try:
        with closing(store.connect(config.state_db_path(orch_home))) as conn:
            return 0, roadmap.build_report(
                conn,
                target=name,
                repo=target.path,
                default_branch=target.default_branch,
            )
    except roadmap.RoadmapError as exc:
        data: dict[str, object] = {
            "ok": False,
            "error": exc.error,
            "message": exc.message,
        }
        if exc.error == "invalid_roadmap":
            data["problems"] = [asdict(problem) for problem in exc.problems]
        return 64, data


def _execute(
    args: argparse.Namespace, secrets: list[str]
) -> tuple[int, dict[str, object]]:
    if args.command == "versions":
        orch_home, hub = _environment("versions", require_hub=True)
        assert hub is not None
        try:
            settings = config.load_config(orch_home)
            tmux = shutil.which("tmux")
            return 0, versions.build_report(
                orch_home=orch_home,
                hub=hub,
                claude_path=settings.claude_path,
                codex_path=settings.codex_path,
                tmux_path=Path(tmux) if tmux and Path(tmux).is_absolute() else None,
                orch_version=importlib.metadata.version("orch"),
            )
        except (config.ConfigError, versions.VersionsError) as exc:
            return 64, {"ok": False, "error": "config", "message": str(exc)}
    if args.command == "events":
        return _events(args)
    if args.command == "roadmap":
        return _roadmap(args)
    if args.command == "show":
        orch_home, _ = _environment(args.command)
        with closing(store.connect(config.state_db_path(orch_home))) as conn:
            task = _get_task(conn, args.id)
            _check_target(task.target)
            paths = config.target_paths(orch_home, task.target)
            return 0, show.show_task(conn, task, paths)
    if args.command == "track":
        return _track(args, secrets)
    if args.command == "fulltest":
        return _fulltest(args, secrets)
    if args.command == "explore":
        name = os.environ.get("ORCH_TARGET")
        if not name:
            raise _UsageError("ORCH_TARGET is required for explore")
        orch_home, hub = _environment(args.command, require_hub=True)
        assert hub is not None
        target = _target(hub, name)
        report = explore.run_explore(
            args.question,
            dir_arg=args.dir,
            target=target,
            config=config.load_config(orch_home),
            paths=config.target_paths(orch_home, name),
            environ=os.environ,
            model=_codex_model(hub, orch_home),
        )
        code = {
            explore.ExploreOutcome.SAVED: 0,
            explore.ExploreOutcome.INVALID: 20,
            explore.ExploreOutcome.FAILED: 20,
            explore.ExploreOutcome.TIMEOUT: 20,
            explore.ExploreOutcome.BUSY: 30,
            explore.ExploreOutcome.QUOTA: 40,
        }[report.outcome]
        return code, {
            "ok": report.outcome == explore.ExploreOutcome.SAVED,
            **asdict(report),
            "answer_path": str(report.answer_path),
            "log_path": str(report.log_path) if report.log_path is not None else None,
        }
    if args.command == "sandbox":
        argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        if not argv:
            raise _UsageError("sandbox run requires a command")
        target = os.environ.get("ORCH_TARGET")
        if not target:
            raise _UsageError("ORCH_TARGET is required for sandbox run")
        orch_home, _ = _environment(args.command)
        paths = config.target_paths(orch_home, target)
        settings = config.load_config(orch_home)
        worktree = Path(args.dir).resolve() if args.dir is not None else Path.cwd()
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        log_path = paths.logs / (
            f"{gate.SANDBOX_RUN_LOG_PREFIX}.{timestamp}.{os.getpid()}.log"
        )
        report = gate.run_sandboxed(
            argv, worktree, log_path, timeout_sec=settings.gate_timeout_min * 60
        )
        data = {
            "ok": report.ok,
            "exit_code": report.exit_code,
            "changed": list(report.changed),
            "log_path": report.log_path,
        }
        if report.ok:
            return 0, data
        return 20, {**data, "error": report.error, "message": report.detail}
    if args.command == "review":
        if args.id == "save":
            if args.save_id is None or args.by is not None:
                raise _UsageError("review save requires an id and does not accept --by")
            args.id = args.save_id
        elif args.save_id is not None or args.by is None or args.triage is not None:
            raise _UsageError(
                "review requires an id and --by codex; --triage is for save"
            )
    orch_home, hub = _environment(
        args.command,
        require_hub=(
            args.command in {"doctor", "gc"}
            or (args.command == "review" and args.by == "codex")
            or (args.command == "service" and args.service_command != "uninstall")
        ),
    )
    home = Path.home()
    if args.command == "service":
        if args.service_command == "uninstall":
            report = service.uninstall(home=home, uid=os.getuid())
            ok = report.ok
        elif args.service_command == "install":
            assert hub is not None
            report = service.install(
                orch=_which_absolute("orch"),
                tmux=_which_absolute("tmux"),
                hub=hub,
                home=home,
                orch_home=orch_home,
                uid=os.getuid(),
                custom_orch_home=orch_home != config.orch_home({}),
            )
            ok = report.ok
        else:
            assert hub is not None
            report = service.restore(
                config.load_targets(hub / "targets.toml"),
                orch=_which_absolute("orch"),
                orch_home=orch_home,
                terminal=_terminal,
            )
            ok = not report.failed
        if ok:
            return 0, {"ok": True, **asdict(report)}
        return 20, {
            **asdict(report),
            "ok": False,
            "error": "service_failed",
            "message": "서비스 명령을 완료하지 못했습니다.",
        }
    if args.command == "open":
        assert hub is not None
        target = _target(hub, args.name)
        paths = config.ensure_target(orch_home, target.name, target.github)
        settings = config.load_config(orch_home)
        if settings.notify_topic:
            secrets.append(settings.notify_topic)
        if settings.claude_path is None:
            raise config.ConfigError("Claude Code path is not configured")
        try:
            dirty = gitops.has_uncommitted_changes(target.path)
        except gitops.GitError:
            dirty = True
        with closing(store.connect(config.state_db_path(orch_home))) as conn:
            currents = _check_agent(
                conn,
                settings=settings,
                orch_home=orch_home,
                home=home,
                now=datetime.now(UTC),
                secrets=secrets,
            )
        ready = _ready_candidate(orch_home)
        plan = launcher.build_plan(
            target=target,
            hub=hub,
            orch_home=orch_home,
            default_orch_home=config.orch_home({}),
            claude=settings.claude_path,
            selfcheck_warnings=launcher.selfcheck_warnings(currents, ready),
            dirty=dirty,
        )
        session = f"orch-{target.name}"
        terminal = _terminal(session, record_path(paths.root))
        opened = not terminal.has_session()
        warnings = list(plan.warnings)
        if opened:
            main_pane = terminal.open_session(
                cwd=plan.cwd, env=plan.env, command=plan.argv
            )
            watch_pane = terminal.terminal_split(
                ["orch", "watch", "--panels"], target=main_pane, percent=40
            )
            terminal.terminal_split(
                ["tail", "-F", str(paths.logs / "subagents.log")],
                target=watch_pane,
                vertical=True,
                percent=30,
            )
            try:
                _start_selfcheck(orch_home, hub)
            except OSError, config.ConfigError:
                warnings.append("selfcheck를 시작하지 못했습니다.")
        else:
            status = terminal.main_pane_status(settings.claude_path)
            if status is not MainPaneStatus.ALIVE:
                warnings.append(f"기존 세션의 메인 창 상태: {status.value}")
        attach = not args.no_attach and _interactive()
        output = {
            "ok": True,
            "target": target.name,
            "session": session,
            "opened": opened,
            "warnings": warnings,
            "attach": attach,
        }
        print(
            credentials.redact_secrets(json.dumps(output, ensure_ascii=False), secrets)
        )
        if attach:
            sys.stdout.flush()
            argv = terminal.attach_argv(os.environ)
            try:
                os.execvp(argv[0], argv)
            except OSError as exc:
                print(" ".join(f"세션 연결 실패: {exc}".splitlines()), file=sys.stderr)
                return 70, {}
        return 0, {}
    if args.command == "notify":
        settings = config.load_config(orch_home)
        if settings.notify_topic:
            secrets.append(settings.notify_topic)
        outcome = _send_notification(
            "test",
            settings=settings,
            orch_home=orch_home,
            home=home,
            secrets=secrets,
            dedup=False,
        )
        if outcome == notify.Outcome.SENT:
            return 0, {"result": outcome.value}
        messages = {
            notify.Outcome.DISABLED: "알림이 비활성화되어 있습니다.",
            notify.Outcome.REFUSED_PERMISSIONS: "설정 파일 권한을 600으로 제한하세요.",
            notify.Outcome.FAILED: "시험 알림을 전송하지 못했습니다.",
        }
        return 20, {
            "ok": False,
            "error": outcome.value,
            "result": outcome.value,
            "message": messages[outcome],
        }
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
        try:
            return _command(conn, args, orch_home, hub, home, secrets)
        finally:
            if args.command in _REFRESH_COMMANDS:
                _refresh_todos(conn, orch_home, secrets)


def _kill(
    conn: sqlite3.Connection, task_id: str, paths: config.TargetPaths
) -> dict[str, object]:
    """Stop a running task through lifecycle.kill_task and return its report."""
    report = lifecycle.kill_task(conn, task_id, paths=paths, environ=os.environ)
    return asdict(report)


def _command(
    conn: sqlite3.Connection,
    args: argparse.Namespace,
    orch_home: Path,
    hub: Path | None,
    home: Path,
    secrets: list[str],
) -> tuple[int, dict[str, object]]:
    command = args.command
    if command == "wait" and not args.merged:
        raise _UsageError("wait requires --merged")
    if command == "wait" and (args.id is None) == (args.pr is None):
        raise _UsageError("wait requires exactly one of a task id and --pr")
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
        if spec.lane == Lane.B or (
            spec.lane == Lane.D and spec.model not in ("haiku", "sonnet", "opus")
        ):
            recorded = _codex_model(hub, orch_home)
            if spec.model != recorded:
                raise taskfile.TaskFileError(
                    f"Codex model must be the recorded model: {recorded}"
                )
        paths = config.ensure_target(orch_home, target.name, target.github)
        task = lifecycle.prepare_task(conn, Path(args.spec), target, paths)
        return 0, {
            "task_id": task.id,
            "state": task.state,
            "worktree": task.worktree,
            "base_sha": task.base_sha,
        }
    if command == "watch":

        def frame() -> str:
            now = datetime.now(UTC)
            return render.todo_text(
                store.list_tasks(conn, target=session_target),
                scheduler.blocked_chains(conn, target=session_target),
                now,
            )

        if args.panels:
            if not os.environ.get("TMUX") or not session_target:
                raise _UsageError("watch --panels requires TMUX and ORCH_TARGET")
            paths = config.target_paths(orch_home, session_target)
            record = record_path(paths.root)
            terminal = _terminal(f"orch-{session_target}", record)
            try:
                panes = load_record(record)
            except TerminalError:
                panes = None
            pane = os.environ.get("TMUX_PANE")
            anchor = (
                pane
                if panes is not None
                and (pane == panes.main_pane or pane in panes.created)
                else None
            )

            def tick() -> None:
                running = {}
                for task in store.list_tasks(
                    conn, states=[TaskState.RUNNING], target=session_target
                ):
                    log = watch.current_log(paths.logs, task.id)
                    if log is not None:
                        running[task.id] = log
                watch.sync_panels(
                    terminal,
                    running=running,
                    state=paths.root / "panels.json",
                    anchor=anchor,
                )

            return watch.watch(
                frame, interval_sec=args.interval, once=args.once, tick=tick
            ), {}
        return watch.watch(frame, interval_sec=args.interval, once=args.once), {}
    if command == "status":
        tasks = store.list_tasks(conn, target=session_target)
        chains = scheduler.blocked_chains(conn, target=session_target)
        pauses = store.list_pauses(conn)
        now = datetime.now(UTC)
        doctor_status = render.doctor_state(store.list_doctor_checks(conn), now)
        if args.human:
            lines = render.status_lines(tasks, chains, now)
            if pauses:
                lines.insert(0, "새 배분 중단: " + ", ".join(p.reason for p in pauses))
            doctor_line = render.doctor_stale_line(doctor_status)
            if doctor_line is not None:
                lines.append(doctor_line)
            return 0, {"lines": lines}
        return 0, {
            "tasks": [render.status_row(task, now) for task in tasks],
            "blocked_chains": [asdict(chain) for chain in chains],
            "paused": [asdict(pause) for pause in pauses],
            "doctor": doctor_status,
        }

    # Check task ownership before loading target configuration or issuing a token.
    if command == "wait" and args.pr is not None:
        if not session_target:
            raise config.ConfigError("wait --pr requires ORCH_TARGET")
        assert hub is not None
        target = _target(hub, session_target)
    elif hasattr(args, "id"):
        task = _get_task(conn, args.id)
        _check_target(task.target)
        paths = config.target_paths(orch_home, task.target)
        if hub is not None:
            target = _target(hub, task.target)

    settings = config.load_config(orch_home)
    if settings.notify_topic:
        secrets.append(settings.notify_topic)
    if command == "selfcheck":
        assert hub is not None
        now = datetime.now(UTC)
        if args.apply is not None or args.rollback is not None:
            if args.apply == "codex":
                switch = selfcheck_codex.apply_codex(
                    hub=hub, orch_home=orch_home, home=home, now=now
                )
            elif args.rollback == "codex":
                switch = selfcheck_codex.rollback_codex(
                    orch_home=orch_home, home=home, now=now
                )
            elif args.apply is not None:
                switch = selfcheck.apply_claude(
                    hub=hub, orch_home=orch_home, home=home, now=now
                )
            else:
                switch = selfcheck.rollback_claude(
                    orch_home=orch_home, home=home, now=now
                )
            if switch.status not in {"applied", "rolled_back"}:
                if switch.status == "refused":
                    code, error, message = (
                        64,
                        "refused",
                        f"전환할 수 없습니다: {switch.reason}",
                    )
                elif switch.status == "busy":
                    code, error, message = (
                        20,
                        "busy",
                        "다른 selfcheck가 실행 중입니다.",
                    )
                else:
                    code, error, message = (
                        20,
                        "switch_error",
                        f"전환 중 오류가 발생했습니다: {switch.reason}",
                    )
                return code, {
                    "ok": False,
                    "error": error,
                    "message": message,
                    "switch": switch.to_json(),
                }
            output: dict[str, object] = {"ok": True, "switch": switch.to_json()}
            code = 0
        else:
            report = selfcheck.run_claude(
                hub=hub, orch_home=orch_home, home=home, settings=settings, now=now
            )
            codex_report = selfcheck_codex.run_codex(
                hub=hub, orch_home=orch_home, home=home, settings=settings, now=now
            )
            output = {
                "ok": True,
                "claude": report.to_json(),
                "codex": codex_report.to_json(),
            }
            code = 0
            if report.status == "error" or codex_report.status == "error":
                output.update(
                    ok=False,
                    error="selfcheck_error",
                    message="selfcheck를 완료하지 못했습니다.",
                )
                code = 20
        currents = _check_agent(
            conn,
            settings=settings,
            orch_home=orch_home,
            home=home,
            now=now,
            secrets=secrets,
        )
        output.update(
            current={agent: current.to_json() for agent, current in currents.items()},
            paused=[pause.reason for pause in store.list_pauses(conn)],
        )
        return code, output
    if command == "doctor":
        assert hub is not None
        report = doctor.run_doctor(
            conn,
            hub=hub,
            settings=settings,
            targets=config.load_targets(hub / "targets.toml"),
            orch_home=orch_home,
            home=home,
            now=datetime.now(UTC),
            environ=os.environ,
            disk_warn_gb=settings.disk_warn_gb,
            disk_fail_gb=settings.disk_fail_gb,
            terminal=_terminal,
        )
        if report.killed:
            _refresh_todos(conn, orch_home, secrets)
        if report.errors:
            return 70, {
                "ok": False,
                "error": "internal",
                "message": "상태 점검 중 내부 오류가 발생했습니다.",
                **asdict(report),
            }
        return 0, {"ok": True, **asdict(report)}
    if command == "gc":
        assert hub is not None
        report = gc.collect(
            conn,
            targets=config.load_targets(hub / "targets.toml"),
            orch_home=orch_home,
            now=datetime.now(UTC),
            retention_days=settings.retention_days,
            dry_run=args.dry_run,
        )
        if report.errors:
            return 70, {
                "ok": False,
                "error": "internal",
                "message": "정리 중 내부 오류가 발생했습니다.",
                **asdict(report),
            }
        return 0, {"ok": True, **asdict(report)}
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
            "paused": list(report.paused),
            "blocked_chains": [
                asdict(item) for item in report.chains if visible(item.task_id)
            ],
        }
    task_id = args.id
    if command == "wait":
        if args.pr is not None:
            wait = waiter.wait_for_pull_request
            value = args.pr
            pr_url = waiter.pull_request_url(value, github=target.github)
            notification_task_id = f"pr-{pr_url.rsplit('/', 1)[1]}"
            notification_target = session_target
        else:
            wait = waiter.wait_for_merge
            value = task_id
            notification_task_id = task_id
            notification_target = task.target
        report = wait(
            conn,
            value,
            target=target,
            token=lambda: credentials.issue_token(orch_home, secrets),
            notify=lambda event: _send_notification(
                event,
                settings=settings,
                orch_home=orch_home,
                home=home,
                secrets=secrets,
                target=notification_target,
                task_id=notification_task_id,
                dedup=True,
            ),
            interval_sec=args.interval,
            max_wait_min=args.max_wait,
        )
        code = {
            waiter.Outcome.MERGED: 0,
            waiter.Outcome.CLOSED: 50,
            waiter.Outcome.LIMIT: 60,
            waiter.Outcome.CHECKS_FAILED: 61,
            waiter.Outcome.CHANGES_REQUESTED: 62,
            waiter.Outcome.BEHIND: 63,
        }[report.outcome]
        return code, asdict(report)
    if command == "kill":
        return 0, _kill(conn, task_id, paths)
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
            on_started=lambda: _refresh_todos(conn, orch_home, secrets),
        )
        state = lifecycle.settle_run(conn, task_id)
        if state == TaskState.BLOCKED and result.status in {"BLOCKED_QUOTA", "BLOCKED"}:
            _send_notification(
                "quota" if result.status == "BLOCKED_QUOTA" else "blocked",
                settings=settings,
                orch_home=orch_home,
                home=home,
                secrets=secrets,
                target=task.target,
                task_id=task_id,
            )
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
            timeout_sec=settings.gate_timeout_min * 60,
        )
        return (0 if report.passed else 20), {
            "task_id": task_id,
            "state": _get_task(conn, task_id).state,
            **asdict(report),
        }
    if command == "review":
        if args.by == "codex":
            report = review.run_codex_review(
                conn,
                task_id,
                target=target,
                config=settings,
                paths=paths,
                environ=os.environ,
                model=_codex_model(hub, orch_home),
                effort=review.REVIEW_EFFORT,
                home=home,
                orch_home=orch_home,
            )
            code = {
                review.ReviewOutcome.SAVED: 0,
                review.ReviewOutcome.INVALID: 20,
                review.ReviewOutcome.FAILED: 20,
                review.ReviewOutcome.QUOTA: 40,
                review.ReviewOutcome.TIMEOUT: 20,
            }[report.outcome]
            return code, {
                **asdict(report),
                "log_path": str(report.log_path),
                "output_path": str(report.output_path),
                "diff_path": str(report.diff_path),
            }
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
        token = credentials.issue_token(orch_home, secrets)
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
            timeout_sec=settings.gate_timeout_min * 60,
        )
        if report.state == TaskState.PR_OPEN and report.user_check:
            _send_notification(
                "needs_review",
                settings=settings,
                orch_home=orch_home,
                home=home,
                secrets=secrets,
                target=task.target,
                task_id=task_id,
            )
        return (0 if report.state == TaskState.PR_OPEN else 20), {
            "task_id": task_id,
            **asdict(report),
            "head_sha": _get_task(conn, task_id).pushed_sha,
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
            timeout_sec=settings.gate_timeout_min * 60,
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


def main(argv: list[str] | None = None) -> int:
    """Run one orch command and return its exit code."""
    secrets: list[str] = []
    command = (sys.argv[1:] if argv is None else argv)[:1]
    is_contract_command = bool(command) and command[0] in {
        "versions",
        "status",
        "show",
        "events",
        "doctor",
        "roadmap",
    }
    is_events = command == ["events"]
    try:
        try:
            args = _parser().parse_args(argv)
            if args.command == "hook":
                return hooks.run_hook(args.name)
            code, data = _execute(args, secrets)
            if args.command in {"open", "watch", "events"}:
                return code
            if args.command == "status" and args.human:
                for line in data["lines"]:
                    print(credentials.redact_secrets(line, secrets))
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
            explore.ExploreError,
            gate.GateError,
            gitops.LeaseRejectedError,
            launcher.LaunchError,
            TerminalError,
            runner.RunError,
            scheduler.DispatchError,
            prflow.PullRequestFlowError,
            config.ConfigError,
            taskfile.TaskFileError,
            tracking.TrackingError,
            store.TaskNotFoundError,
            store.StateMismatchError,
            store.IllegalTransitionError,
            waiter.MergeWaitError,
        ) as exc:
            code = 64
            output = {
                "ok": False,
                "error": "refused",
                "message": f"명령을 수행할 수 없습니다: {exc}",
            }
        except Exception as exc:
            if is_events and isinstance(exc, BrokenPipeError):
                raise
            code = 70
            output = {
                "ok": False,
                "error": "internal",
                "message": f"내부 오류가 발생했습니다: {exc}",
            }
        if is_contract_command:
            output = {
                "ok": output["ok"],
                "schema_version": 2 if is_events else 1,
                **output,
            }
        print(
            credentials.redact_secrets(json.dumps(output, ensure_ascii=False), secrets)
        )
        return code
    except BaseException as exc:
        if is_events and isinstance(exc, (KeyboardInterrupt, BrokenPipeError)):
            if isinstance(exc, BrokenPipeError):
                with open(os.devnull, "w") as sink:
                    os.dup2(sink.fileno(), sys.stdout.fileno())
            return 0
        if isinstance(exc, SystemExit) and not secrets:
            raise
        try:
            sys.stderr.write(
                credentials.redact_secrets(
                    "".join(traceback.format_exception(exc)), secrets
                )
            )
        except BaseException:
            pass
        try:
            output = {
                "ok": False,
                "error": "internal",
                "message": "내부 오류가 발생했습니다.",
            }
            if is_contract_command:
                output = {
                    "ok": output["ok"],
                    "schema_version": 2 if is_events else 1,
                    **output,
                }
            print(
                credentials.redact_secrets(
                    json.dumps(output, ensure_ascii=False), secrets
                )
            )
        except BaseException:
            pass
        return 70
