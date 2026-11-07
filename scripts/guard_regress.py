"""Guard false-positive regression check over the main agent's local transcripts.

Usage: PYTHON scripts/guard_regress.py [--transcripts DIR ...] [--show]

PYTHON decides which guard is checked, because the script imports orch.guard from
the interpreter's environment:

- A branch's guard: run the worktree's script with the worktree's interpreter,
  `.venv/bin/python scripts/guard_regress.py`, from the worktree root.
- The installed guard: run the hub clone's script with the interpreter of the
  installed orch tool, `<uv tool dir>/orch/bin/python scripts/guard_regress.py`,
  from the hub clone root. `uv tool dir` prints the tool directory.

Collects every Bash command the main agent ran (subagent sidechains excluded) from
the Claude Code transcripts (*.jsonl) under each DIR, judges each unique command
with guard.judge, and prints a summary: totals, then counts per decision and per
reason. No transcript data is committed; the transcripts are read at run time.

--show also prints each denied or asked command, truncated, so that a denial can
be sorted into legitimate or false positive. Every printed line goes through
redact; a line that holds a secret is replaced by a placeholder.

The default DIR is the transcript directory of the hub clone: $ORCH_HUB when it is
set, as in a session that orch open started, otherwise the repository this script
lives in. Exit codes: 0 done, 64 bad usage or no transcripts found.
"""

import argparse
import json
import os
import re
import sys
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path

from orch import config
from orch.guard import judge
from orch.redact import RedactionError, redact

SHOW_LIMIT = 300
REPO = Path(__file__).resolve().parent.parent


def transcript_dir(repo: Path, home: Path) -> Path:
    """Return Claude Code's transcript directory for a working directory."""
    return home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(repo))


def collect(directories: Iterable[Path]) -> list[str]:
    """Return the main agent's Bash commands, in transcript order."""
    commands: list[str] = []
    for directory in directories:
        for path in sorted(directory.glob("*.jsonl")):
            commands.extend(_commands_in(path))
    return commands


def _commands_in(path: Path) -> Iterator[str]:
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict) or entry.get("isSidechain"):
                continue
            message = entry.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and block.get("name") == "Bash"
                    and isinstance(block.get("input"), dict)
                    and isinstance(block["input"].get("command"), str)
                ):
                    yield block["input"]["command"]


def safe(text: str, *, home: Path, orch_home: Path) -> str:
    """Return text through redact, or a placeholder if it must not be printed."""
    try:
        return redact(text, home=home, orch_home=orch_home)
    except RedactionError:
        return "[withheld by redact]"


def report(
    commands: Sequence[str], *, home: Path, orch_home: Path, show: bool
) -> list[str]:
    """Judge the unique commands and return the summary lines."""
    unique = sorted(set(commands))
    decisions: Counter[str] = Counter()
    reasons: Counter[tuple[str, str]] = Counter()
    flagged: list[tuple[str, str, str]] = []
    for command in unique:
        verdict = judge(command, home=str(home))
        decisions[verdict.decision] += 1
        if verdict.decision != "allow":
            reasons[(verdict.decision, verdict.reason)] += 1
            flagged.append((verdict.decision, verdict.reason, command))
    lines = [
        f"commands: {len(commands)}, unique: {len(unique)}",
        "decisions: "
        + ", ".join(f"{name} {decisions[name]}" for name in ("allow", "deny", "ask")),
    ]
    for (decision, reason), count in sorted(reasons.items()):
        lines.append(f"  {decision} x{count}: {reason}")
    if show:
        for decision, reason, command in flagged:
            text = command if len(command) <= SHOW_LIMIT else command[:SHOW_LIMIT] + "…"
            lines.append(f"--- {decision}: {reason}")
            lines.append(text)
    return [safe(line, home=home, orch_home=orch_home) for line in lines]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--transcripts", type=Path, action="append")
    parser.add_argument("--show", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 64
    home = Path.home()
    hub = os.environ.get("ORCH_HUB")
    repo = Path(hub) if hub else REPO
    directories = args.transcripts or [transcript_dir(repo, home)]
    missing = [d for d in directories if not d.is_dir()]
    if missing:
        print("transcript directory not found", file=sys.stderr)
        return 64
    commands = collect(directories)
    if not commands:
        print("no Bash commands found in the transcripts", file=sys.stderr)
        return 64
    orch_home = config.orch_home(os.environ)
    for line in report(commands, home=home, orch_home=orch_home, show=args.show):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
