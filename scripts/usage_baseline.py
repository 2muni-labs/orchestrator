"""Measure the CU0 baseline (ADR 0027 decision 6) and its CU6 comparison.

Read-only. Reads state.db, Claude Code transcripts and Codex session files, and
prints aggregate numbers only as JSON: no paths, ids, prompts or account fields.

    usage_baseline.py --start 2026-11-05 --end 2026-11-20 [--split 2026-11-17]
        [--merged-json FILE]

Dates are UTC days; --end is exclusive. --merged-json is the output of
`gh pr list --state merged --json headRefName,mergedAt`, fetched separately so
that this script never uses the network or a token.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

REWORK_EDGES = {
    "gate_failed": ("gating", "judging"),
    "review_blocked": ("reviewing", "judging"),
    "retried": ("judging", "reserved"),
    "resumed": ("blocked", "reserved"),
}


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_day(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def summarize(values: list[float]) -> dict[str, float | int]:
    """Count, median, nearest-rank p90 and max, rounded to 0.1."""
    if not values:
        return {"n": 0}
    xs = sorted(values)
    n = len(xs)
    mid = n // 2
    median = xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2
    p90 = xs[max(0, -(-9 * n // 10) - 1)]
    return {
        "n": n,
        "median": round(median, 1),
        "p90": round(p90, 1),
        "max": round(xs[-1], 1),
    }


def minutes(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    return (b - a).total_seconds() / 60


def load_merged(path: Path | None) -> dict[str, datetime]:
    if path is None:
        return {}
    merged: dict[str, datetime] = {}
    for row in json.loads(path.read_text(encoding="utf-8")):
        head = row.get("headRefName") or ""
        if row.get("mergedAt") and head.startswith("task/"):
            merged[head.removeprefix("task/")] = parse_time(row["mergedAt"])
    return merged


def task_metrics(
    db_path: Path,
    target: str,
    start: datetime,
    end: datetime,
    split: datetime | None,
    merged: dict[str, datetime],
) -> dict[str, object]:
    """Completions, rework and stage times of tasks created in [start, end)."""
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tasks = [
            {
                "id": r[0],
                "grade": r[1],
                "lane": r[2],
                "state": r[3],
                "created": parse_time(r[4]),
            }
            for r in db.execute(
                "SELECT id, grade, lane, state, created_at FROM tasks WHERE target = ?",
                (target,),
            )
        ]
        tasks = [t for t in tasks if start <= t["created"] < end]
        ids = {t["id"] for t in tasks}
        events: dict[str, list[tuple[str, str, datetime]]] = defaultdict(list)
        for tid, src, dst, at in db.execute(
            "SELECT task_id, from_state, to_state, at FROM events ORDER BY id"
        ):
            if tid in ids:
                events[tid].append((src, dst, parse_time(at)))
        runs: dict[str, list[tuple[str | None, str | None]]] = defaultdict(list)
        for tid, outcome, reason in db.execute(
            "SELECT task_id, outcome, failure_reason FROM runs ORDER BY run_id"
        ):
            if tid in ids:
                runs[tid].append((outcome, reason))
    finally:
        db.close()

    def first(tid: str, src: str, dsts: tuple[str, ...]) -> datetime | None:
        for a, b, at in events[tid]:
            if a == src and b in dsts:
                return at
        return None

    def count(tid: str, edge: tuple[str, str]) -> int:
        return sum(1 for a, b, _ in events[tid] if (a, b) == edge)

    groups: dict[str, list[dict]] = defaultdict(list)
    for t in tasks:
        groups["all"].append(t)
        groups[t["grade"]].append(t)

    result: dict[str, object] = {"lanes": sorted({t["lane"] for t in tasks})}
    for name, members in sorted(groups.items()):
        closed = [t for t in members if t["state"] == "closed"]
        outcomes: dict[str, int] = defaultdict(int)
        for t in members:
            for outcome, reason in runs[t["id"]]:
                if outcome != "DONE":
                    outcomes[f"{outcome}:{reason}" if reason else str(outcome)] += 1
        clean = [
            t
            for t in closed
            if all(o == "DONE" for o, _ in runs[t["id"]])
            and not any(count(t["id"], e) for e in REWORK_EDGES.values())
        ]
        stages: dict[str, list[float]] = defaultdict(list)
        for t in members:
            tid = t["id"]
            dispatched = first(tid, "prepared", ("reserved", "queued"))
            pairs = {
                "prepare_to_dispatch": (t["created"], dispatched),
                "run": (
                    first(tid, "reserved", ("running",)),
                    first(tid, "running", ("judging",)),
                ),
                "judging_to_gate": (
                    first(tid, "running", ("judging",)),
                    first(tid, "judging", ("gating",)),
                ),
                "g4_review": (
                    first(tid, "gating", ("reviewing",)),
                    first(tid, "reviewing", ("ready", "judging")),
                ),
            }
            for key, (a, b) in pairs.items():
                value = minutes(a, b)
                if value is not None:
                    stages[key].append(value)
        for t in closed:
            opened = first(t["id"], "ready", ("pr_open",))
            done = merged.get(t["id"])
            wait = minutes(opened, done)
            if wait is None or opened is None:
                continue
            if split is None:
                stages["pr_wait"].append(wait)
            else:
                stages[
                    "pr_wait_before_split" if opened < split else "pr_wait_from_split"
                ].append(wait)
            stages["prepare_to_merged_hours"].append(
                (minutes(t["created"], done) or 0) / 60
            )
        result[name] = {
            "tasks": len(members),
            "closed": len(closed),
            "discarded": sum(t["state"] == "discarded" for t in members),
            "open": sum(t["state"] not in ("closed", "discarded") for t in members),
            "runs": sum(len(runs[t["id"]]) for t in members),
            "non_done_runs": dict(sorted(outcomes.items())),
            "rework_events": {
                key: sum(count(t["id"], edge) for t in members)
                for key, edge in REWORK_EDGES.items()
            },
            "closed_without_rework": len(clean),
            "minutes": {key: summarize(v) for key, v in sorted(stages.items())},
        }
    return result


def iter_json_lines(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                yield value


def encode_project(path: Path) -> str:
    """Claude Code's project directory name for a working directory."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def claude_usage(
    projects: Path, hub: Path, start: datetime, end: datetime
) -> dict[str, object]:
    """Raw token sums per (scope, model), deduplicated by request id."""
    hub_name = encode_project(hub)
    seen: set[str] = set()
    sums: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(int))
    )
    sessions: set[str] = set()
    for path in sorted(projects.glob("*/**/*.jsonl")):
        project = path.relative_to(projects).parts[0]
        if project != hub_name:
            scope = "other"
        elif "subagents" in path.relative_to(projects).parts:
            scope = "hub_subagent"
        else:
            scope = "hub_main"
        for entry in iter_json_lines(path):
            message = entry.get("message")
            if not isinstance(message, dict) or not isinstance(
                message.get("usage"), dict
            ):
                continue
            if "timestamp" not in entry:
                continue
            if not start <= parse_time(entry["timestamp"]) < end:
                continue
            key = entry.get("requestId") or message.get("id")
            if not key or key in seen:
                continue
            seen.add(key)
            usage = message["usage"]
            bucket = sums[scope][message.get("model") or "unknown"]
            bucket["requests"] += 1
            bucket["input"] += usage.get("input_tokens") or 0
            bucket["cache_write"] += usage.get("cache_creation_input_tokens") or 0
            bucket["cache_read"] += usage.get("cache_read_input_tokens") or 0
            bucket["output"] += usage.get("output_tokens") or 0
            if scope == "hub_main" and entry.get("sessionId"):
                sessions.add(entry["sessionId"])
    return {
        "tokens": {
            s: {m: dict(v) for m, v in sorted(ms.items())}
            for s, ms in sorted(sums.items())
        },
        "hub_main_sessions": len(sessions),
    }


def codex_usage(
    sessions_dir: Path, worktrees: Path, start: datetime, end: datetime
) -> dict[str, object]:
    """Raw token totals per session kind and the weekly limit percentage per window."""
    prefix = str(worktrees).rstrip("/") + "/"
    sums: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    weekly: dict[str, float] = {}
    for path in sorted(sessions_dir.glob("**/*.jsonl")):
        meta: dict | None = None
        total: dict | None = None
        for entry in iter_json_lines(path):
            payload = entry.get("payload")
            if not isinstance(payload, dict):
                continue
            if entry.get("type") == "session_meta" and meta is None:
                meta = payload
            elif payload.get("type") == "token_count":
                info = payload.get("info")
                if isinstance(info, dict) and isinstance(
                    info.get("total_token_usage"), dict
                ):
                    total = info["total_token_usage"]
                limits = payload.get("rate_limits")
                at = entry.get("timestamp")
                secondary = (
                    limits.get("secondary") if isinstance(limits, dict) else None
                )
                if isinstance(secondary, dict) and at and start <= parse_time(at) < end:
                    reset = (
                        datetime.fromtimestamp(secondary["resets_at"], UTC)
                        .date()
                        .isoformat()
                    )
                    weekly[reset] = max(
                        weekly.get(reset, 0.0), float(secondary["used_percent"])
                    )
        if meta is None:
            continue
        began = meta.get("timestamp")
        if not began or not start <= parse_time(began) < end:
            continue
        source = meta.get("source")
        cwd = str(meta.get("cwd") or "")
        if source == "exec" and cwd.startswith(prefix):
            kind = "orch_run"
        elif source == "exec":
            kind = "exec_other"
        else:
            kind = "interactive"
        bucket = sums[kind]
        bucket["sessions"] += 1
        if total:
            bucket["input"] += total.get("input_tokens") or 0
            bucket["cached_input"] += total.get("cached_input_tokens") or 0
            bucket["output"] += total.get("output_tokens") or 0
            bucket["reasoning_output"] += total.get("reasoning_output_tokens") or 0
    return {
        "tokens": {k: dict(v) for k, v in sorted(sums.items())},
        "weekly_limit_max_percent_by_reset": dict(sorted(weekly.items())),
    }


def runs_started(db_path: Path, target: str, start: datetime, end: datetime) -> int:
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = db.execute(
            "SELECT r.started_at FROM runs r JOIN tasks t ON t.id = r.task_id"
            " WHERE t.target = ? AND r.started_at IS NOT NULL",
            (target,),
        ).fetchall()
    finally:
        db.close()
    return sum(1 for (at,) in rows if start <= parse_time(at) < end)


def main(argv: list[str] | None = None) -> int:
    home = Path.home()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--split")
    parser.add_argument("--target", default="orchestrator")
    parser.add_argument("--merged-json", type=Path)
    parser.add_argument(
        "--orch-home",
        type=Path,
        default=Path(os.environ.get("ORCH_HOME") or home / ".orch"),
    )
    parser.add_argument(
        "--claude-projects", type=Path, default=home / ".claude" / "projects"
    )
    parser.add_argument(
        "--codex-sessions", type=Path, default=home / ".codex" / "sessions"
    )
    parser.add_argument(
        "--hub-dir",
        type=Path,
        default=Path(
            os.environ.get("ORCH_HUB") or Path(__file__).resolve().parent.parent
        ),
    )
    args = parser.parse_args(argv)
    start, end = parse_day(args.start), parse_day(args.end)
    if end <= start:
        print("--end must be after --start", file=sys.stderr)
        return 2
    split = parse_day(args.split) if args.split else None
    db_path = args.orch_home / "state.db"
    report = {
        "window": {
            "start": args.start,
            "end_exclusive": args.end,
            "split": args.split,
            "target": args.target,
        },
        "tasks": task_metrics(
            db_path, args.target, start, end, split, load_merged(args.merged_json)
        ),
        "runs_started": runs_started(db_path, args.target, start, end),
        "claude": claude_usage(args.claude_projects, args.hub_dir, start, end),
        "codex": codex_usage(
            args.codex_sessions, args.orch_home / args.target / "worktrees", start, end
        ),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
