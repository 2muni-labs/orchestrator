"""Derived views of the state store: TODO.md, status rows and the PR body."""

import os
import tempfile
from collections import Counter
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from orch.models import GateResult, Review, Severity, Task, TaskState
from orch.review import Triage, override_section
from orch.scheduler import BlockedChain


def elapsed_sec(task: Task, now: datetime) -> int:
    """Return the whole seconds a task has taken."""
    end = (
        datetime.fromisoformat(task.updated_at)
        if task.state in (TaskState.CLOSED, TaskState.DISCARDED)
        else now
    )
    return max(0, int((end - datetime.fromisoformat(task.created_at)).total_seconds()))


def format_elapsed(seconds: int) -> str:
    """Return a short human form of a duration."""
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        minutes, seconds = divmod(seconds, 60)
        return f"{minutes}m {seconds:02d}s"
    hours, remainder = divmod(seconds, 3600)
    return f"{hours}h {remainder // 60:02d}m"


def status_row(task: Task, now: datetime) -> dict[str, object]:
    """Return one task of the orch status JSON output."""
    return {
        "id": task.id,
        "target": task.target,
        "title": task.title,
        "state": task.state,
        "grade": task.grade,
        "lane": task.lane,
        "attempt": task.attempt,
        "pr_url": task.pr_url,
        "elapsed_sec": elapsed_sec(task, now),
        "head_sha": task.pushed_sha,
    }


def status_lines(
    tasks: Iterable[Task], chains: Iterable[BlockedChain], now: datetime
) -> list[str]:
    """Return the lines of orch status --human."""
    lines = [
        f"{task.id} {task.state} {format_elapsed(elapsed_sec(task, now))} "
        f"{task.pushed_sha[:7] if task.pushed_sha else '-'}"
        for task in tasks
    ]
    lines.extend(
        f"{chain.task_id} {chain.state}: {', '.join(chain.causes)}" for chain in chains
    )
    return lines


def todo_text(
    tasks: Iterable[Task], chains: Iterable[BlockedChain], now: datetime
) -> str:
    """Return the contents of TODO.md."""
    lines = [
        "# TODO",
        "",
        "자동 생성 파일입니다. 직접 수정하지 마세요. 상태의 원본은 `state.db`입니다.",
        "",
        f"생성 시각: {now.isoformat()}",
        "",
        "| id | title | state | grade | lane | attempt | elapsed | head | PR |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for task in tasks:
        title = task.title.replace("|", "\\|")
        head = task.pushed_sha[:7] if task.pushed_sha else "-"
        elapsed = format_elapsed(elapsed_sec(task, now))
        lines.append(
            f"| {task.id} | {title} | {task.state} | {task.grade} | {task.lane} "
            f"| {task.attempt} | {elapsed} | {head} | {task.pr_url or '-'} |"
        )
    chain_lines = [
        f"- {chain.task_id} {chain.state}: {', '.join(chain.causes)}"
        for chain in chains
    ]
    if chain_lines:
        lines.extend(["", "## Blocked chains", "", *chain_lines])
    return "\n".join(lines) + "\n"


def write_todo(path: Path, text: str) -> None:
    """Replace TODO.md atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        try:
            stream = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise
        with stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def build_body(
    task: Task,
    *,
    changed: Iterable[str],
    gates: Iterable[GateResult],
    review: Review | None,
    triage: Triage | None = None,
) -> str:
    """Return the minimal English pull request body."""
    review_summary = "Not required"
    if review is not None:
        counts = Counter(finding.severity for finding in review.findings)
        review_summary = f"Verdict: {review.verdict}"
        for severity in Severity:
            if counts[severity]:
                review_summary += f", {severity}: {counts[severity]}"

    body = "\n".join(
        [
            "## Summary",
            "",
            f"Task `{task.id}`: {task.title}",
            "",
            "## Changed files",
            "",
            *(f"- `{path}`" for path in sorted(changed)),
            "",
            "## Gates",
            "",
            *(
                f"- {result.gate}: {'passed' if result.passed else 'failed'}"
                for result in gates
            ),
            "",
            "## Review",
            "",
            review_summary,
            "",
        ]
    )
    if triage is not None:
        body = override_section(triage) + "\n\n" + body
    return body
