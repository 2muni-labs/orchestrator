"""Tests for scripts/usage_baseline.py, using fake data under tmp_path only."""

import importlib.util
import json
import sqlite3
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "usage_baseline.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("usage_baseline", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ub = _load()

HUB = Path("/hub/orchestrator")
ACCOUNT = "acct-secret-0001"


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ORCH_HOME", str(home / ".orch"))
    monkeypatch.setenv("ORCH_HUB", str(HUB))
    return home


def _jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n")


def _state_db(orch_home: Path) -> Path:
    orch_home.mkdir(parents=True, exist_ok=True)
    path = orch_home / "state.db"
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE tasks (id TEXT PRIMARY KEY, target TEXT, grade TEXT, lane TEXT,
            state TEXT, created_at TEXT);
        CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
            from_state TEXT, to_state TEXT, actor TEXT, note TEXT, at TEXT);
        CREATE TABLE runs (run_id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
            started_at TEXT, outcome TEXT, failure_reason TEXT);
        """
    )
    tasks = [
        ("a", "orchestrator", "L2", "B", "closed", "2026-11-10T00:00:00+00:00"),
        ("b", "orchestrator", "L3", "B", "discarded", "2026-11-10T01:00:00+00:00"),
        ("c", "orchestrator", "L3", "B", "closed", "2026-11-10T02:00:00+00:00"),
        ("old", "orchestrator", "L2", "B", "closed", "2026-11-01T00:00:00+00:00"),
        ("x", "other-target", "L2", "B", "closed", "2026-11-10T00:00:00+00:00"),
    ]
    db.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)", tasks)
    events = [
        ("a", "prepared", "reserved", "2026-11-10T00:10:00+00:00"),
        ("a", "reserved", "running", "2026-11-10T00:11:00+00:00"),
        ("a", "running", "judging", "2026-11-10T00:14:00+00:00"),
        ("a", "judging", "gating", "2026-11-10T00:15:00+00:00"),
        ("a", "gating", "ready", "2026-11-10T00:16:00+00:00"),
        ("a", "ready", "pr_open", "2026-11-10T00:20:00+00:00"),
        ("a", "pr_open", "closed", "2026-11-10T01:00:00+00:00"),
        ("b", "prepared", "reserved", "2026-11-10T01:30:00+00:00"),
        ("b", "reserved", "running", "2026-11-10T01:31:00+00:00"),
        ("b", "running", "judging", "2026-11-10T01:41:00+00:00"),
        ("b", "judging", "blocked", "2026-11-10T01:42:00+00:00"),
        ("b", "blocked", "discarded", "2026-11-10T01:50:00+00:00"),
        ("c", "prepared", "reserved", "2026-11-10T02:20:00+00:00"),
        ("c", "reserved", "running", "2026-11-10T02:21:00+00:00"),
        ("c", "running", "judging", "2026-11-10T02:26:00+00:00"),
        ("c", "judging", "gating", "2026-11-10T02:27:00+00:00"),
        ("c", "gating", "judging", "2026-11-10T02:28:00+00:00"),
        ("c", "judging", "gating", "2026-11-10T02:30:00+00:00"),
        ("c", "gating", "reviewing", "2026-11-10T02:31:00+00:00"),
        ("c", "reviewing", "ready", "2026-11-10T02:41:00+00:00"),
        ("c", "ready", "pr_open", "2026-11-12T00:00:00+00:00"),
        ("c", "pr_open", "closed", "2026-11-12T02:00:00+00:00"),
    ]
    db.executemany(
        "INSERT INTO events (task_id, from_state, to_state, actor, at)"
        " VALUES (?, ?, ?, 'orch', ?)",
        events,
    )
    runs = [
        ("a", "2026-11-10T00:11:00+00:00", "DONE", None),
        ("b", "2026-11-10T01:31:00+00:00", "BLOCKED", "out_of_scope"),
        ("c", "2026-11-10T02:21:00+00:00", "DONE", None),
        ("old", "2026-11-01T00:11:00+00:00", "DONE", None),
        ("x", "2026-11-10T00:11:00+00:00", "DONE", None),
    ]
    db.executemany(
        "INSERT INTO runs (task_id, started_at, outcome, failure_reason)"
        " VALUES (?, ?, ?, ?)",
        runs,
    )
    db.commit()
    db.close()
    return path


def _merged(tmp_path: Path) -> Path:
    path = tmp_path / "merged.json"
    rows = [
        {"headRefName": "task/a", "mergedAt": "2026-11-10T00:50:00Z"},
        {"headRefName": "task/c", "mergedAt": "2026-11-12T01:00:00Z"},
        {"headRefName": "docs-only", "mergedAt": "2026-11-10T00:00:00Z"},
        {"headRefName": "task/b", "mergedAt": None},
    ]
    path.write_text(json.dumps(rows))
    return path


def _assistant(request: str, model: str, at: str, session: str, out: int) -> dict:
    return {
        "requestId": request,
        "timestamp": at,
        "sessionId": session,
        "cwd": str(HUB),
        "message": {
            "id": "msg-" + request,
            "model": model,
            "usage": {
                "input_tokens": 1,
                "cache_creation_input_tokens": 10,
                "cache_read_input_tokens": 100,
                "output_tokens": out,
            },
        },
    }


def _claude(projects: Path) -> None:
    hub = projects / ub.encode_project(HUB)
    _jsonl(
        hub / "s1.jsonl",
        [
            _assistant("r1", "opus", "2026-11-10T00:00:00Z", "s1", 5),
            _assistant("r1", "opus", "2026-11-10T00:00:01Z", "s1", 5),
            _assistant("r2", "opus", "2026-11-10T00:01:00Z", "s1", 7),
            _assistant("r3", "opus", "2026-11-01T00:00:00Z", "s1", 1000),
            {"type": "user", "message": {"role": "user", "content": "hi"}},
        ],
    )
    _jsonl(
        hub / "s2" / "subagents" / "agent-1.jsonl",
        [_assistant("r4", "sonnet", "2026-11-10T00:02:00Z", "s2", 3)],
    )
    _jsonl(
        projects / "-tmp-elsewhere" / "s3.jsonl",
        [_assistant("r5", "opus", "2026-11-10T00:03:00Z", "s3", 2)],
    )


def _codex_session(cwd: str, source: str, began: str, totals: list[int]) -> list[dict]:
    rows: list[dict] = [
        {
            "timestamp": began,
            "type": "session_meta",
            "payload": {
                "timestamp": began,
                "cwd": cwd,
                "source": source,
                "creator_account_id": ACCOUNT,
            },
        }
    ]
    for i, total in enumerate(totals):
        rows.append(
            {
                "timestamp": began,
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": total,
                            "cached_input_tokens": total - 1,
                            "output_tokens": i + 1,
                            "reasoning_output_tokens": 0,
                        }
                    },
                    "rate_limits": {
                        "secondary": {
                            "used_percent": 10.0 * (i + 1),
                            "resets_at": 1795598601,
                        }
                    },
                },
            }
        )
    return rows


def _codex(sessions: Path, orch_home: Path) -> None:
    worktree = str(orch_home / "orchestrator" / "worktrees" / "a")
    _jsonl(
        sessions / "11" / "10" / "run.jsonl",
        _codex_session(worktree, "exec", "2026-11-10T00:11:00Z", [100, 300]),
    )
    _jsonl(
        sessions / "11" / "10" / "review.jsonl",
        _codex_session("/somewhere", "exec", "2026-11-10T00:12:00Z", [50]),
    )
    _jsonl(
        sessions / "11" / "10" / "chat.jsonl",
        _codex_session("/somewhere", "vscode", "2026-11-10T00:13:00Z", [70]),
    )
    _jsonl(
        sessions / "11" / "01" / "old.jsonl",
        _codex_session(worktree, "exec", "2026-11-01T00:11:00Z", [999]),
    )
    sibling = str(orch_home / "orchestrator" / "worktrees-x" / "a")
    _jsonl(
        sessions / "11" / "10" / "sibling.jsonl",
        _codex_session(sibling, "exec", "2026-11-10T00:14:00Z", [20]),
    )


def _run(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, home: Path, extra: list[str]
) -> tuple[int, dict, str]:
    orch_home = home / ".orch"
    _state_db(orch_home)
    _claude(home / ".claude" / "projects")
    _codex(home / ".codex" / "sessions", orch_home)
    argv = [
        "--start",
        "2026-11-05",
        "--end",
        "2026-11-20",
        "--orch-home",
        str(orch_home),
        "--claude-projects",
        str(home / ".claude" / "projects"),
        "--codex-sessions",
        str(home / ".codex" / "sessions"),
        "--merged-json",
        str(_merged(tmp_path)),
        *extra,
    ]
    code = ub.main(argv)
    out = capsys.readouterr().out
    return code, json.loads(out) if code == 0 else {}, out


def test_task_metrics(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, isolated: Path
) -> None:
    code, report, _ = _run(capsys, tmp_path, isolated, ["--split", "2026-11-11"])
    assert code == 0
    assert report["runs_started"] == 3
    tasks = report["tasks"]
    assert tasks["lanes"] == ["B"]
    every = tasks["all"]
    assert (every["tasks"], every["closed"], every["discarded"], every["open"]) == (
        3,
        2,
        1,
        0,
    )
    assert every["non_done_runs"] == {"BLOCKED:out_of_scope": 1}
    assert every["rework_events"] == {
        "gate_failed": 1,
        "resumed": 0,
        "retried": 0,
        "review_blocked": 0,
    }
    assert every["closed_without_rework"] == 1
    m = every["minutes"]
    assert m["prepare_to_dispatch"] == {
        "n": 3,
        "median": 20.0,
        "p90": 30.0,
        "max": 30.0,
    }
    assert m["run"] == {"n": 3, "median": 5.0, "p90": 10.0, "max": 10.0}
    assert m["judging_to_gate"]["n"] == 2
    assert m["g4_review"] == {"n": 1, "median": 10.0, "p90": 10.0, "max": 10.0}
    assert m["pr_wait_before_split"] == {
        "n": 1,
        "median": 30.0,
        "p90": 30.0,
        "max": 30.0,
    }
    assert m["pr_wait_from_split"] == {"n": 1, "median": 60.0, "p90": 60.0, "max": 60.0}
    assert m["prepare_to_merged_hours"]["n"] == 2
    assert tasks["L2"]["closed"] == 1
    assert tasks["L3"]["discarded"] == 1


def test_pr_wait_without_split(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, isolated: Path
) -> None:
    code, report, _ = _run(capsys, tmp_path, isolated, [])
    assert code == 0
    m = report["tasks"]["all"]["minutes"]
    assert m["pr_wait"]["n"] == 2
    assert "pr_wait_before_split" not in m


def test_claude_usage_dedup_scope_and_window(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, isolated: Path
) -> None:
    code, report, _ = _run(capsys, tmp_path, isolated, [])
    assert code == 0
    claude = report["claude"]
    assert claude["hub_main_sessions"] == 1
    main = claude["tokens"]["hub_main"]["opus"]
    assert main == {
        "requests": 2,
        "input": 2,
        "cache_write": 20,
        "cache_read": 200,
        "output": 12,
    }
    assert claude["tokens"]["hub_subagent"]["sonnet"]["output"] == 3
    assert claude["tokens"]["other"]["opus"]["output"] == 2


def test_codex_usage_kinds_and_weekly(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, isolated: Path
) -> None:
    code, report, _ = _run(capsys, tmp_path, isolated, [])
    assert code == 0
    codex = report["codex"]
    assert codex["tokens"]["orch_run"] == {
        "sessions": 1,
        "input": 300,
        "cached_input": 299,
        "output": 2,
        "reasoning_output": 0,
    }
    assert codex["tokens"]["exec_other"]["sessions"] == 2
    assert codex["tokens"]["interactive"]["sessions"] == 1
    assert codex["weekly_limit_max_percent_by_reset"] == {"2026-11-25": 20.0}


def test_output_has_no_paths_or_account_fields(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, isolated: Path
) -> None:
    code, _, out = _run(capsys, tmp_path, isolated, [])
    assert code == 0
    for secret in (str(tmp_path), str(HUB), ACCOUNT, "/somewhere", "s1", "r1"):
        assert secret not in out


def test_rejects_empty_window(capsys: pytest.CaptureFixture[str]) -> None:
    assert ub.main(["--start", "2026-11-10", "--end", "2026-11-10"]) == 2
    assert "--end" in capsys.readouterr().err
