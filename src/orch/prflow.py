"""orch pr, update and close: the flow around a task's pull request."""

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from orch import gate, github, gitops, results, store
from orch.config import Target
from orch.gate import GateReport
from orch.models import Gate, TaskState, Verdict
from orch.render import build_body
from orch.review import (
    OVERRIDE_LABEL,
    ReviewFlowError,
    check_triage,
    load_triage,
)
from orch.taskfile import TaskSpec

INTEGRATION_FAILED = "Integration check failed against the latest main. Do not merge."


class PullRequestFlowError(Exception):
    """A pull request step cannot proceed."""


@dataclass(frozen=True, slots=True, kw_only=True)
class PullRequestReport:
    """How orch pr ended."""

    state: TaskState
    pr_url: str | None = None
    conflicts: tuple[str, ...] = ()
    detail: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class UpdateReport:
    """How orch update ended. Gates are None when the merge conflicted."""

    state: TaskState
    conflicts: tuple[str, ...] = ()
    gates: GateReport | None = None


def open_pull_request(
    conn: sqlite3.Connection,
    spec: TaskSpec,
    target: Target,
    *,
    token: str,
    log_path: Path,
    review_path: Path,
    triage_path: Path,
    home: Path,
    orch_home: Path,
    resolution: str | None = None,
) -> PullRequestReport:
    """Rebase, test and push a task, then open or update its pull request."""
    task = store.get_task(conn, spec.id)
    if task is None:
        raise PullRequestFlowError(f"Task not found: {spec.id}")
    if task.state not in (TaskState.READY, TaskState.CONFLICT):
        raise PullRequestFlowError(f"Task must be ready or conflict: {spec.id}")
    if not task.worktree or not task.test_sha:
        raise PullRequestFlowError(f"Task needs a worktree and test commit: {spec.id}")

    review = None
    if review_path.exists():
        try:
            review = results.load_review(review_path, spec.id)
        except results.ReportError:
            raise PullRequestFlowError(f"Invalid review: {spec.id}") from None
    elif gate.review_required(task.grade, flag=spec.review):
        raise PullRequestFlowError(f"Task needs a saved review: {spec.id}")

    triage = None
    if review is not None and review.verdict is Verdict.BLOCK:
        try:
            triage = load_triage(triage_path)
            check_triage(conn, triage, review, task)
        except ReviewFlowError as exc:
            raise PullRequestFlowError(f"Invalid triage: {spec.id}") from exc
        if not task.pr_url and not github.label_exists(
            github=target.github, name=OVERRIDE_LABEL, token=token
        ):
            raise PullRequestFlowError(f"Repository needs label: {OVERRIDE_LABEL}")

    worktree = Path(task.worktree)
    main_ref = f"origin/{target.default_branch}"
    branch = f"task/{spec.id}"
    gitops.fetch(worktree)
    rebased = gitops.rebase(worktree, main_ref)
    if not rebased.ok:
        if task.state == TaskState.READY:
            store.transition(
                conn, spec.id, TaskState.READY, TaskState.CONFLICT, actor="orch"
            )
        return PullRequestReport(state=TaskState.CONFLICT, conflicts=rebased.conflicts)

    base = gitops.rev_parse(worktree, main_ref)
    if spec.tests == "none":
        test_sha = base
    else:
        commits = gitops.commits_since(worktree, main_ref)
        if not commits:
            raise PullRequestFlowError(
                f"Task has no test commit after rebase: {spec.id}"
            )
        test_sha = commits[0]
    store.update_task(conn, spec.id, base_sha=base, test_sha=test_sha)

    head = gitops.rev_parse(worktree, "HEAD")
    detail = gate.run_tests(spec, target, worktree, log_path)
    if gitops.rev_parse(worktree, "HEAD") != head:
        detail = "G2 실행 중 커밋이 생겼습니다"
    store.record_gate_result(
        conn,
        spec.id,
        task.attempt,
        Gate.G2_PRE_PR,
        passed=detail is None,
        detail=detail,
    )
    if detail is not None:
        store.transition(conn, spec.id, task.state, TaskState.JUDGING, actor="orch")
        return PullRequestReport(state=TaskState.JUDGING, detail=detail)

    if task.pushed_sha:
        gitops.update_task_branch(worktree, spec.id, task.pushed_sha)
    else:
        gitops.push_task_branch(worktree, branch)
    store.update_task(conn, spec.id, pushed_sha=head)

    if task.pr_url:
        body = (
            resolution
            if resolution is not None
            else "Updated after resolving conflicts."
        )
        github.comment_on_pull_request(
            url=task.pr_url,
            body=f"{body}\n\nCommit: `{head}`",
            token=token,
            home=home,
            orch_home=orch_home,
        )
        url = task.pr_url
    else:
        body = build_body(
            task,
            changed=gitops.changed_paths(worktree, main_ref),
            gates=store.gate_results(conn, spec.id, task.attempt),
            review=review,
            triage=triage,
        )
        url = github.create_pull_request(
            github=target.github,
            head=branch,
            base=target.default_branch,
            title=task.title,
            body=body,
            token=token,
            home=home,
            orch_home=orch_home,
            labels=(OVERRIDE_LABEL,) if triage is not None else (),
        )

    with store.transaction(conn):
        store.update_task(conn, spec.id, pr_url=url)
        store.transition(conn, spec.id, task.state, TaskState.PR_OPEN, actor="orch")
    return PullRequestReport(state=TaskState.PR_OPEN, pr_url=url)


def update_pull_request(
    conn: sqlite3.Connection,
    spec: TaskSpec,
    target: Target,
    *,
    token: str,
    log_path: Path,
    home: Path,
    orch_home: Path,
) -> UpdateReport:
    """Check an open pull request against the moved default branch, without pushing."""
    task = store.get_task(conn, spec.id)
    if task is None:
        raise PullRequestFlowError(f"Task not found: {spec.id}")
    if task.state != TaskState.PR_OPEN:
        raise PullRequestFlowError(f"Task must be pr_open: {spec.id}")
    if not task.worktree or not task.test_sha or not task.pushed_sha or not task.pr_url:
        raise PullRequestFlowError(
            f"Task needs a worktree, test commit, pushed commit and PR URL: {spec.id}"
        )

    worktree = Path(task.worktree)
    main_ref = f"origin/{target.default_branch}"
    gitops.fetch(worktree)
    merged = gitops.temporary_merge(worktree, main_ref)
    if not merged.ok:
        store.transition(
            conn, spec.id, TaskState.PR_OPEN, TaskState.CONFLICT, actor="orch"
        )
        return UpdateReport(state=TaskState.CONFLICT, conflicts=merged.conflicts)

    try:
        report = gate.run_gates(
            spec,
            target,
            worktree,
            since=task.test_sha,
            main_ref=main_ref,
            log_path=log_path,
        )
    finally:
        gitops.reset_to(worktree, task.pushed_sha)
    if not report.passed:
        github.comment_on_pull_request(
            url=task.pr_url,
            body=INTEGRATION_FAILED,
            token=token,
            home=home,
            orch_home=orch_home,
        )
    return UpdateReport(state=TaskState.PR_OPEN, gates=report)


def close_task(
    conn: sqlite3.Connection, task_id: str, target: Target, *, token: str
) -> None:
    """Clean up a task whose pull request was merged, and move it to closed."""
    task = store.get_task(conn, task_id)
    if task is None:
        raise PullRequestFlowError(f"Task not found: {task_id}")
    if task.state != TaskState.PR_OPEN or not task.pr_url:
        raise PullRequestFlowError(f"Task needs an open pull request: {task_id}")

    status = github.pull_request_status(url=task.pr_url, token=token)
    if not status.merged:
        raise PullRequestFlowError(f"Pull request has not been merged: {task_id}")

    if task.worktree:
        worktree = Path(task.worktree)
        if worktree.exists():
            gitops.remove_worktree(target.path, worktree, force=True)
    gitops.prune_worktrees(target.path)
    branch = f"task/{task_id}"
    try:
        gitops.rev_parse(target.path, f"refs/heads/{branch}")
    except gitops.GitError:
        pass
    else:
        gitops.delete_branch(target.path, branch)
    store.transition(conn, task_id, TaskState.PR_OPEN, TaskState.CLOSED, actor="orch")
