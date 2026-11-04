"""Stand-in for the codex and claude CLIs, used by tests instead of real agents.

The scenario comes from ORCH_FAKE_SCENARIO. Like the real CLIs, it reads the task
spec from standard input, takes the worktree from -C (or the current directory),
and ignores other arguments. It uses only what old system Pythons have.
"""

import json
import os
import re
import signal
import subprocess
import sys
import time

RESULT_FILE = ".orch-result.json"
CHANGED_FILE = "src/fake_change.py"
OUTSIDE_FILE = "outside/fake_change.py"
TEST_FILE = "tests/test_fake_change.py"
USAGE_LIMIT_MESSAGE = "You hit your usage limit. Try again later."
DEFAULT_SLEEP_SEC = 3600


def worktree_from_args(args):
    if "-C" in args:
        position = args.index("-C") + 1
        if position < len(args):
            return args[position]
    return os.getcwd()


def task_id_from_spec(spec):
    match = re.search(r'^id\s*=\s*"([^"]+)"', spec, re.MULTILINE)
    return match.group(1) if match else "unknown-task"


def touch(worktree, relative):
    path = os.path.join(worktree, relative)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("# Changed by the fake agent.\n")


def report(
    worktree,
    task_id,
    status="DONE",
    changed=(),
    outside=(),
    unresolved=(),
    failure_reason=None,
):
    data = {
        "task_id": task_id,
        "status": status,
        "failure_reason": failure_reason,
        "summary": "가짜 에이전트의 결과입니다.",
        "changed_paths": list(changed),
        "out_of_scope_changes": list(outside),
        "verification": {"command": "fake verify", "passed": status == "DONE"},
        "unresolved": list(unresolved),
        "log_path": "~/.orch/fake/logs/" + task_id + ".log",
    }
    with open(os.path.join(worktree, RESULT_FILE), "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)


def sleep_forever():
    print("started", flush=True)
    time.sleep(float(os.environ.get("ORCH_FAKE_SLEEP", DEFAULT_SLEEP_SEC)))


def start_child(**options):
    child = subprocess.Popen(["sleep", str(DEFAULT_SLEEP_SEC)], **options)
    with open(os.environ["ORCH_FAKE_PID_FILE"], "w", encoding="utf-8") as handle:
        handle.write(str(child.pid))


def main():
    scenario = os.environ.get("ORCH_FAKE_SCENARIO", "success")
    worktree = worktree_from_args(sys.argv[1:])
    task_id = task_id_from_spec(sys.stdin.read())

    if scenario == "success":
        touch(worktree, CHANGED_FILE)
        report(worktree, task_id, changed=[CHANGED_FILE])
    elif scenario == "partial":
        touch(worktree, CHANGED_FILE)
        report(
            worktree,
            task_id,
            status="PARTIAL",
            changed=[CHANGED_FILE],
            unresolved=["남은 항목"],
        )
    elif scenario == "report_missing":
        touch(worktree, CHANGED_FILE)
    elif scenario == "malformed_report":
        with open(os.path.join(worktree, RESULT_FILE), "w", encoding="utf-8") as handle:
            handle.write("{not json")
    elif scenario == "timeout":
        sleep_forever()
    elif scenario == "ignore_term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        sleep_forever()
    elif scenario == "usage_limit":
        print(USAGE_LIMIT_MESSAGE, flush=True)
        return 1
    elif scenario == "out_of_scope":
        touch(worktree, CHANGED_FILE)
        touch(worktree, OUTSIDE_FILE)
        report(worktree, task_id, changed=[CHANGED_FILE, OUTSIDE_FILE])
    elif scenario == "test_file_change":
        touch(worktree, TEST_FILE)
        report(worktree, task_id, changed=[TEST_FILE])
    elif scenario == "immediate_exit":
        return 1
    elif scenario == "killed":
        os.kill(os.getpid(), signal.SIGKILL)
    elif scenario == "blocked":
        report(worktree, task_id, status="BLOCKED", failure_reason="spec_mismatch")
    elif scenario == "partial_out_of_scope":
        touch(worktree, OUTSIDE_FILE)
        report(worktree, task_id, status="PARTIAL", changed=[OUTSIDE_FILE])
    elif scenario == "report_nonzero":
        touch(worktree, CHANGED_FILE)
        report(worktree, task_id, changed=[CHANGED_FILE])
        return 3
    elif scenario == "report_with_limit_message":
        print(USAGE_LIMIT_MESSAGE, flush=True)
        touch(worktree, CHANGED_FILE)
        report(worktree, task_id, changed=[CHANGED_FILE])
    elif scenario == "report_then_hang":
        report(worktree, task_id)
        sleep_forever()
    elif scenario == "report_then_killed":
        report(worktree, task_id)
        os.kill(os.getpid(), signal.SIGKILL)
    elif scenario == "env_dump":
        with open(os.environ["ORCH_FAKE_ENV_FILE"], "w", encoding="utf-8") as handle:
            json.dump(sorted(os.environ), handle)
        report(worktree, task_id)
    elif scenario == "leftover_child":
        start_child()
        report(worktree, task_id)
    elif scenario == "detached_child":
        start_child(start_new_session=True)
        sleep_forever()
    else:
        print("unknown scenario: " + scenario, file=sys.stderr)
        return 64
    return 0


if __name__ == "__main__":
    sys.exit(main())
