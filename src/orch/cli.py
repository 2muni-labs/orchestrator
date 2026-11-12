"""The orch command line: subcommands, one-line JSON output and exit codes."""

import argparse
import importlib.metadata
import json
import os
import shutil
import sqlite3
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
    gate,
    gc,
    gitops,
    hooks,
    launcher,
    lifecycle,
    notify,
    prflow,
    render,
    review,
    runner,
    scheduler,
    service,
    store,
    taskfile,
    waiter,
    watch,
)
from orch.models import Task, TaskState
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


_HOOK_NAMES = ("session-start", "pre-bash", "subagent-stop", "notification")


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
    wait = commands.add_parser("wait")
    wait.add_argument("id")
    wait.add_argument("--merged", action="store_true")
    wait.add_argument("--interval", type=_positive_int, default=60)
    wait.add_argument("--max-wait", type=_positive_int, default=90)
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
    commands.add_parser("gc").add_argument("--dry-run", action="store_true")
    service_commands = commands.add_parser("service").add_subparsers(
        dest="service_command", required=True
    )
    for name in ("install", "uninstall", "restore"):
        service_commands.add_parser(name)
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


def _execute(
    args: argparse.Namespace, secrets: list[str]
) -> tuple[int, dict[str, object]]:
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
        version = launcher.probe_claude_version(settings.claude_path)
        plan = launcher.build_plan(
            target=target,
            hub=hub,
            orch_home=orch_home,
            default_orch_home=config.orch_home({}),
            claude=settings.claude_path,
            claude_version=version,
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
        if args.human:
            lines = render.status_lines(tasks, chains, now)
            if pauses:
                lines.insert(0, "새 배분 중단: " + ", ".join(p.reason for p in pauses))
            return 0, {"lines": lines}
        return 0, {
            "tasks": [render.status_row(task, now) for task in tasks],
            "blocked_chains": [asdict(chain) for chain in chains],
            "paused": [asdict(pause) for pause in pauses],
        }

    # Check task ownership before loading target configuration or issuing a token.
    if hasattr(args, "id"):
        task = _get_task(conn, args.id)
        _check_target(task.target)
        paths = config.target_paths(orch_home, task.target)
        if hub is not None:
            target = _target(hub, task.target)

    settings = config.load_config(orch_home)
    if settings.notify_topic:
        secrets.append(settings.notify_topic)
    if command == "doctor":
        assert hub is not None
        report = doctor.run_doctor(
            conn,
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
        report = waiter.wait_for_merge(
            conn,
            task_id,
            token=lambda: credentials.issue_token(orch_home, secrets),
            interval_sec=args.interval,
            max_wait_min=args.max_wait,
        )
        code = 0 if report.merged else 50 if report.pr_state == "CLOSED" else 60
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
                model=review.REVIEW_MODEL,
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
    try:
        try:
            args = _parser().parse_args(argv)
            if args.command == "hook":
                return hooks.run_hook(args.name)
            code, data = _execute(args, secrets)
            if args.command in {"open", "watch"}:
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
            gate.GateError,
            gitops.LeaseRejectedError,
            launcher.LaunchError,
            TerminalError,
            runner.RunError,
            scheduler.DispatchError,
            prflow.PullRequestFlowError,
            config.ConfigError,
            taskfile.TaskFileError,
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
            code = 70
            output = {
                "ok": False,
                "error": "internal",
                "message": f"내부 오류가 발생했습니다: {exc}",
            }
        print(
            credentials.redact_secrets(json.dumps(output, ensure_ascii=False), secrets)
        )
        return code
    except BaseException as exc:
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
            print(
                credentials.redact_secrets(
                    json.dumps(output, ensure_ascii=False), secrets
                )
            )
        except BaseException:
            pass
        return 70
