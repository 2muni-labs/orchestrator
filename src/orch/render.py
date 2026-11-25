"""Derived views of the state store: TODO.md, status rows, the PR body and plans."""

import os
import re
import tempfile
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from orch.models import (
    DoctorCheck,
    Gate,
    GateResult,
    Review,
    Severity,
    Task,
    TaskState,
)
from orch.review import Triage, override_section
from orch.scheduler import BlockedChain
from orch.tracking import Project, Record


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


DOCTOR_STALE_AFTER = timedelta(minutes=30)


def doctor_state(checks: Iterable[DoctorCheck], now: datetime) -> dict[str, object]:
    """Return the last doctor run and whether it is stale (B46)."""
    raise NotImplementedError


def doctor_stale_line(state: Mapping[str, object]) -> str | None:
    """Return the line that reports a stale doctor, or None."""
    raise NotImplementedError


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


def purpose_line(body: str) -> str:
    """Return the first two sentences of a spec's Purpose section on one line."""
    lines = body.splitlines()
    try:
        start = lines.index("# Purpose") + 1
    except ValueError:
        return "(none)"
    end = next(
        (index for index in range(start, len(lines)) if lines[index].startswith("# ")),
        len(lines),
    )
    text = re.sub(r"<!--.*?-->", "", "\n".join(lines[start:end]), flags=re.DOTALL)
    text = " ".join(text.split())
    if not text:
        return "(none)"
    sentences = re.split(r"(?<=[.!?。])\s+", text, maxsplit=2)
    text = " ".join(sentences[:2])
    return text[:297] + "..." if len(text) > 300 else text


def _review_summary(review: Review | None) -> str:
    """Return the review verdict and finding counts in severity order."""
    if review is None:
        return "not required"
    counts = Counter(finding.severity for finding in review.findings)
    return str(review.verdict) + "".join(
        f", {severity}: {counts[severity]}" for severity in Severity if counts[severity]
    )


def review_packet(
    task: Task,
    *,
    body: str,
    modify: Iterable[str],
    gates: Iterable[GateResult],
    review: Review | None,
) -> str:
    """Return the fixed-format review packet that the scheduled tasks read."""
    gate_status = dict.fromkeys((Gate.G1, Gate.G2, Gate.G3), "not run")
    for result in gates:
        name = Gate.G2 if result.gate == Gate.G2_PRE_PR else result.gate
        gate_status[name] = "passed" if result.passed else "failed"
    return "\n".join(
        [
            "## Review packet",
            "",
            f"- task_id: `{task.id}`",
            f"- grade: {task.grade}",
            f"- lane: {task.lane}",
            f"- purpose: {purpose_line(body)}",
            "- modify: " + ", ".join(f"`{path}`" for path in modify),
            "- gates: "
            + ", ".join(
                f"{name} {gate_status[name]}" for name in (Gate.G1, Gate.G2, Gate.G3)
            ),
            f"- review: {_review_summary(review)}",
        ]
    )


def build_body(
    task: Task,
    *,
    changed: Iterable[str],
    gates: Iterable[GateResult],
    review: Review | None,
    triage: Triage | None = None,
    packet: str | None = None,
) -> str:
    """Return the minimal English pull request body."""
    review_summary = "Not required"
    if review is not None:
        review_summary = f"Verdict: {_review_summary(review)}"

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
    if packet is not None:
        body = packet + "\n\n" + body
    if triage is not None:
        body = override_section(triage) + "\n\n" + body
    return body


# Plan files (technical design 4.11, ADR 0025)


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanEntry:
    """One tracking record with its tasks, as shown in a plan file."""

    record: Record
    tasks: tuple[Task, ...]
    priority: str | None
    issue_closed: bool


def plan_priority(body: str) -> str | None:
    """Return the work priority line's value in a task body, or None."""
    match = re.search(r"^- 작업 우선도: (P[0-3])[ \t]*$", body, re.MULTILINE)
    return match[1] if match else None


def entry_open(entry: PlanEntry) -> bool:
    """Return whether the record is still open by local evidence."""
    if entry.record.completion_owner == "task":
        return not entry.tasks or entry.tasks[-1].state not in (
            TaskState.CLOSED,
            TaskState.DISCARDED,
        )
    if entry.record.completion_owner == "harness":
        return not entry.issue_closed
    return entry.record.accepted_at is None


def plan_text(
    project: Project,
    entries: Sequence[PlanEntry],
    tasks_by_id: Mapping[str, Task],
    now: datetime,
) -> str:
    """Return a group's plan file contents."""
    opened = sorted(
        (entry for entry in entries if entry_open(entry)),
        key=lambda entry: (entry.priority or "P4", entry.record.record_id),
    )
    lines = [
        f"# 계획: {project.key}",
        "",
        "자동 생성 파일입니다. 직접 수정하지 마세요. "
        "원본은 `state.db`와 지시서의 우선도 줄입니다.",
        "",
        f"생성 시각: {now.isoformat()}",
        f"그룹: {project.title} ({project.url})",
        "",
        "## 열린 기록",
        "",
        "| 우선도 | 기록 | 제목 | 완료 책임 | 이슈 | 작업 | PR |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    waits: list[str] = []
    runnable: list[str] = []
    unregistered: list[str] = []
    for entry in opened:
        record = entry.record
        title = record.title.replace("|", "\\|")
        issue_number = (record.issue_url or "").rsplit("/", 1)[-1]
        issue = f"#{issue_number}" if issue_number.isdecimal() else "-"
        tasks = ", ".join(
            f"{task.id} {task.state} {task.grade}/{task.lane}" for task in entry.tasks
        )
        pr = (entry.tasks[-1].pr_url if entry.tasks else None) or record.pr_url
        lines.append(
            f"| {entry.priority or '-'} | {record.record_id} | {title} | "
            f"{record.completion_owner} | {issue} | {tasks or '-'} | {pr or '-'} |"
        )
        for task in entry.tasks:
            if task.state in (TaskState.CLOSED, TaskState.DISCARDED):
                continue
            ready = True
            for dep in task.depends_on:
                predecessor = tasks_by_id.get(dep)
                if predecessor is not None and predecessor.state == TaskState.CLOSED:
                    continue
                ready = False
                if predecessor is not None and predecessor.state == TaskState.DISCARDED:
                    waits.append(f"- {task.id}: 선행 폐기 {dep}")
                else:
                    state = predecessor.state if predecessor is not None else "없음"
                    waits.append(f"- {task.id}: 선행 대기 {dep} ({state})")
            if ready and task.state in (TaskState.PREPARED, TaskState.QUEUED):
                runnable.append(f"- {task.id}")
        if record.completion_owner == "task" and not entry.tasks:
            unregistered.append(record.record_id)
    if unregistered:
        runnable.append("- 작업 미등록: " + ", ".join(unregistered))
    lines.extend(["", "## 대기 사유", "", *(waits or ["- 없음"])])
    lines.extend(["", "## 실행 가능", "", *(runnable or ["- 없음"])])
    return "\n".join(lines) + "\n"
