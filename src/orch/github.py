"""All gh calls. The token reaches gh only through its environment."""

import json
import os
import re
import signal
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from orch.redact import redact

GH_TIMEOUT_SEC = 120
_GITHUB_PATTERN = r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._][A-Za-z0-9._-]*"
_LABEL_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]*"
_URL_PATTERN = rf"https://github\.com/{_GITHUB_PATTERN}/pull/[0-9]+"

REVIEW_LABELS = ("verify-light", "verify-deep")
USER_ONLY_LABEL = "user-approved"
NEVER_REMOVED_LABEL = "review-overridden"
# Measured on gh 2.102.0 (2026-11-16). One call reads everything orch wait decides on.
REVIEW_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!){"
    "repository(owner:$owner,name:$name){pullRequest(number:$number){"
    "state mergedAt headRefOid labels(first:100){nodes{name}} "
    "timelineItems(last:100,itemTypes:[LABELED_EVENT,UNLABELED_EVENT]){nodes{"
    "__typename ... on LabeledEvent{createdAt label{name}} "
    "... on UnlabeledEvent{createdAt label{name}}}} "
    "commits(last:1){nodes{commit{oid statusCheckRollup{contexts(first:100){nodes{"
    "__typename ... on CheckRun{name status conclusion} "
    "... on StatusContext{context state}}}}}}}}}}"
)


class GitHubError(Exception):
    """A gh command failed, timed out or was refused by a safety check."""


@dataclass(frozen=True, slots=True, kw_only=True)
class PullRequestStatus:
    """What gh reports about a pull request."""

    state: str
    merged: bool
    merged_at: str | None = None
    mergeable: str | None = None


class CheckResult(StrEnum):
    """What a check on the head commit says."""

    PASSED = "passed"
    FAILED = "failed"
    PENDING = "pending"


@dataclass(frozen=True, slots=True, kw_only=True)
class LabelEvent:
    """One label added to or removed from a pull request."""

    name: str
    added: bool
    at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewState:
    """A pull request's state, labels, label events and head commit checks."""

    state: str
    merged: bool
    merged_at: str | None
    head_sha: str
    labels: tuple[str, ...]
    events: tuple[LabelEvent, ...]
    checks: Mapping[str, CheckResult]


@dataclass(frozen=True, slots=True, kw_only=True)
class RequiredChecks:
    """A branch's required checks, and whether a head must be up to date to merge."""

    names: tuple[str, ...]
    strict: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class Comparison:
    """How a head compares with a base branch.

    files holds every changed path, a renamed file's old path too. truncated is True
    when GitHub may have left files out (300 or more files listed).
    """

    behind_by: int
    files: tuple[str, ...]
    truncated: bool


def _validate_token(token: str) -> None:
    if not token or any(character.isspace() for character in token):
        raise GitHubError("Invalid token")


def _validate_url(url: str) -> None:
    if re.fullmatch(_URL_PATTERN, url) is None:
        raise GitHubError("Invalid pull request URL")


def _gh(token: str, *args: str, input_text: str | None = None) -> str:
    """Run gh with isolated credentials and kill its group on timeout."""
    _validate_token(token)
    env = os.environ.copy()
    for name in (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
    ):
        env.pop(name, None)
    env.update(GH_TOKEN=token, GH_PROMPT_DISABLED="1")
    failed = False
    stdout = stderr = ""
    try:
        process = subprocess.Popen(
            ["gh", *args],
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            env=env,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(
                input=input_text, timeout=GH_TIMEOUT_SEC
            )
        except subprocess.TimeoutExpired:
            failed = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            _, stderr = process.communicate()
        failed = failed or process.returncode != 0
    except OSError:
        failed = True
    if failed:
        safe_stderr = stderr.replace(token, "[REDACTED]")[:500]
        raise GitHubError(f"{' '.join(args[:2])}: {safe_stderr}")
    return stdout


def label_exists(*, github: str, name: str, token: str) -> bool:
    """Return whether a repository has a label. The label is never created here."""
    _validate_token(token)
    if re.fullmatch(_GITHUB_PATTERN, github) is None:
        raise GitHubError("Invalid repository")
    _validate_label(name)
    stdout = _gh(
        token,
        "label",
        "list",
        "--repo",
        github,
        "--search",
        name,
        "--json",
        "name",
        "--limit",
        "100",
    )
    try:
        answer = json.loads(stdout)
    except ValueError as exc:
        raise GitHubError("label list: Invalid response") from exc
    if not isinstance(answer, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("name"), str)
        for item in answer
    ):
        raise GitHubError("label list: Invalid response")
    return any(item["name"].casefold() == name.casefold() for item in answer)


def _validate_label(name: str) -> None:
    if not isinstance(name, str) or re.fullmatch(_LABEL_PATTERN, name) is None:
        raise GitHubError("Invalid label name")


def create_pull_request(
    *,
    github: str,
    head: str,
    base: str,
    title: str,
    body: str,
    token: str,
    home: Path,
    orch_home: Path,
    labels: Iterable[str] = (),
) -> str:
    """Create a pull request from redacted text and return its URL."""
    _validate_token(token)
    if re.fullmatch(_GITHUB_PATTERN, github) is None:
        raise GitHubError("Invalid repository")
    if re.fullmatch(r"task/[A-Za-z0-9][A-Za-z0-9._-]*", head) is None:
        raise GitHubError("Invalid head branch")
    if base.startswith("-") or re.fullmatch(r"[A-Za-z0-9._/-]+", base) is None:
        raise GitHubError("Invalid base branch")
    if not title.strip() or not body.strip():
        raise GitHubError("Title and body must not be empty")
    label_args = []
    for label in labels:
        _validate_label(label)
        if label.casefold() == USER_ONLY_LABEL:
            raise GitHubError("The bot cannot add user-approved")
        label_args.extend(("--label", label))
    title = redact(title, home=home, orch_home=orch_home, secrets=(token,))
    body = redact(body, home=home, orch_home=orch_home, secrets=(token,))
    stdout = _gh(
        token,
        "pr",
        "create",
        "--repo",
        github,
        "--head",
        head,
        "--base",
        base,
        "--title",
        title,
        *label_args,
        "--body-file",
        "-",
        input_text=body,
    )
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise GitHubError("pr create: Invalid response")
    url = lines[-1]
    _validate_url(url)
    if not url.startswith(f"https://github.com/{github}/pull/"):
        raise GitHubError("pr create: Unexpected repository")
    return url


def add_label(*, url: str, name: str, token: str) -> None:
    """Add an existing label to a pull request. The label is never created here."""
    _validate_token(token)
    _validate_url(url)
    _validate_label(name)
    if name.casefold() == USER_ONLY_LABEL:
        raise GitHubError("The bot cannot add user-approved")
    _gh(token, "pr", "edit", url, "--add-label", name)


def comment_on_pull_request(
    *,
    url: str,
    body: str,
    token: str,
    home: Path,
    orch_home: Path,
) -> None:
    """Add a redacted comment to a pull request."""
    _validate_token(token)
    _validate_url(url)
    if not body.strip():
        raise GitHubError("Body must not be empty")
    body = redact(body, home=home, orch_home=orch_home, secrets=(token,))
    _gh(token, "pr", "comment", url, "--body-file", "-", input_text=body)


def pull_request_status(*, url: str, token: str) -> PullRequestStatus:
    """Return a pull request's state."""
    _validate_token(token)
    _validate_url(url)
    stdout = _gh(token, "pr", "view", url, "--json", "state,mergedAt,mergeable")
    answer = None
    try:
        answer = json.loads(stdout)
    except ValueError:
        pass
    if not isinstance(answer, dict):
        raise GitHubError("pr view: Invalid response")
    state = answer.get("state")
    merged_at = answer.get("mergedAt")
    mergeable = answer.get("mergeable")
    if (
        state not in ("OPEN", "MERGED", "CLOSED")
        or (merged_at is not None and not isinstance(merged_at, str))
        or (mergeable is not None and not isinstance(mergeable, str))
        or (state == "MERGED" and not merged_at)
    ):
        raise GitHubError("pr view: Invalid response")
    return PullRequestStatus(
        state=state,
        merged=state == "MERGED",
        merged_at=merged_at,
        mergeable=mergeable,
    )


def remove_label(*, url: str, name: str, token: str) -> None:
    """Remove a label from a pull request. Some labels are never removed."""
    _validate_token(token)
    _validate_url(url)
    _validate_label(name)
    if name.casefold() in (USER_ONLY_LABEL, NEVER_REMOVED_LABEL):
        raise GitHubError("The bot cannot remove this label")
    _gh(token, "pr", "edit", url, "--remove-label", name)


def _nodes(connection: object) -> list[dict]:
    """Read a GraphQL connection, rejecting missing or malformed nodes."""
    if not isinstance(connection, dict):
        raise ValueError
    nodes = connection["nodes"]
    if not isinstance(nodes, list) or any(not isinstance(node, dict) for node in nodes):
        raise ValueError
    return nodes


def _check_result(node: dict) -> tuple[str, CheckResult]:
    """Classify one check, rejecting unknown GitHub enum values."""
    kind = node["__typename"]
    if kind == "CheckRun":
        name = node["name"]
        status = node["status"]
        conclusion = node["conclusion"]
        conclusions = {
            "SUCCESS": CheckResult.PASSED,
            "NEUTRAL": CheckResult.PASSED,
            "SKIPPED": CheckResult.PASSED,
            "STALE": CheckResult.PENDING,
            "FAILURE": CheckResult.FAILED,
            "CANCELLED": CheckResult.FAILED,
            "TIMED_OUT": CheckResult.FAILED,
            "ACTION_REQUIRED": CheckResult.FAILED,
            "STARTUP_FAILURE": CheckResult.FAILED,
        }
        if status == "COMPLETED":
            result = conclusions[conclusion]
        elif status in ("IN_PROGRESS", "QUEUED", "PENDING", "WAITING", "REQUESTED"):
            if conclusion not in (None, "") and conclusion not in conclusions:
                raise ValueError
            result = CheckResult.PENDING
        else:
            raise ValueError
    elif kind == "StatusContext":
        name = node["context"]
        result = {
            "SUCCESS": CheckResult.PASSED,
            "PENDING": CheckResult.PENDING,
            "EXPECTED": CheckResult.PENDING,
            "FAILURE": CheckResult.FAILED,
            "ERROR": CheckResult.FAILED,
        }[node["state"]]
    else:
        raise ValueError
    if not isinstance(name, str):
        raise ValueError
    return name, result


def review_state(*, url: str, token: str) -> ReviewState:
    """Return a pull request's state, labels, label events and head checks."""
    _validate_token(token)
    _validate_url(url)
    owner, repo, _, number = url.removeprefix("https://github.com/").split("/")
    stdout = _gh(
        token,
        "api",
        "graphql",
        "-f",
        f"query={REVIEW_QUERY}",
        "-f",
        f"owner={owner}",
        "-f",
        f"name={repo}",
        "-F",
        f"number={number}",
    )
    try:
        answer = json.loads(stdout)["data"]["repository"]["pullRequest"]
        state = answer["state"]
        merged_at = answer["mergedAt"]
        head_sha = answer["headRefOid"]
        if (
            state not in ("OPEN", "MERGED", "CLOSED")
            or (merged_at is not None and not isinstance(merged_at, str))
            or (state == "MERGED" and not merged_at)
            or not isinstance(head_sha, str)
        ):
            raise ValueError
        labels = tuple(node["name"] for node in _nodes(answer["labels"]))
        if any(not isinstance(name, str) for name in labels):
            raise ValueError
        events = []
        for node in _nodes(answer["timelineItems"]):
            kind = node["__typename"]
            name = node["label"]["name"]
            if kind not in ("LabeledEvent", "UnlabeledEvent") or not isinstance(
                name, str
            ):
                raise ValueError
            at = datetime.fromisoformat(node["createdAt"])
            if at.utcoffset() is None:
                raise ValueError
            events.append(
                LabelEvent(
                    name=name,
                    added=kind == "LabeledEvent",
                    at=at.astimezone(UTC),
                )
            )
        commits = _nodes(answer["commits"])
        if len(commits) != 1:
            raise ValueError
        if commits[0]["commit"]["oid"] != head_sha:
            raise ValueError
        rollup = commits[0]["commit"]["statusCheckRollup"]
        checks: dict[str, CheckResult] = {}
        severity = {
            CheckResult.PASSED: 0,
            CheckResult.PENDING: 1,
            CheckResult.FAILED: 2,
        }
        if rollup is not None:
            for node in _nodes(rollup["contexts"]):
                name, result = _check_result(node)
                if name not in checks or severity[result] > severity[checks[name]]:
                    checks[name] = result
        return ReviewState(
            state=state,
            merged=state == "MERGED",
            merged_at=merged_at,
            head_sha=head_sha,
            labels=labels,
            events=tuple(sorted(events, key=lambda event: event.at)),
            checks=checks,
        )
    except KeyError, TypeError, ValueError, OverflowError:
        pass
    raise GitHubError("api graphql: Invalid response")


def required_checks(*, github: str, branch: str, token: str) -> tuple[str, ...]:
    """Return the names of the checks a branch's rules require."""
    return check_rules(github=github, branch=branch, token=token).names


def check_rules(*, github: str, branch: str, token: str) -> RequiredChecks:
    """Return a branch's required checks and its strict up-to-date policy."""
    _validate_token(token)
    if re.fullmatch(_GITHUB_PATTERN, github) is None:
        raise GitHubError("Invalid repository")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch) is None or ".." in branch:
        raise GitHubError("Invalid branch")
    stdout = _gh(token, "api", f"repos/{github}/rules/branches/{branch}?per_page=100")
    try:
        rules = json.loads(stdout)
        if not isinstance(rules, list):
            raise ValueError
        names: dict[str, None] = {}
        strict = False
        for rule in rules:
            if not isinstance(rule, dict):
                raise ValueError
            if rule["type"] != "required_status_checks":
                continue
            parameters = rule["parameters"]
            if not isinstance(parameters, dict):
                raise ValueError
            policy = parameters.get("strict_required_status_checks_policy", False)
            if not isinstance(policy, bool):
                raise ValueError
            strict = strict or policy
            checks = parameters["required_status_checks"]
            if not isinstance(checks, list):
                raise ValueError
            for check in checks:
                if not isinstance(check, dict) or not isinstance(check["context"], str):
                    raise ValueError
                names[check["context"]] = None
        return RequiredChecks(names=tuple(names), strict=strict)
    except KeyError, TypeError, ValueError:
        pass
    raise GitHubError("api: Invalid response")


def compare(*, github: str, base: str, head: str, token: str) -> Comparison:
    """Compare a head commit with a base branch."""
    _validate_token(token)
    if re.fullmatch(_GITHUB_PATTERN, github) is None:
        raise GitHubError("Invalid repository")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", base) is None or ".." in base:
        raise GitHubError("Invalid branch")
    if re.fullmatch(r"[0-9a-f]{40}", head) is None:
        raise GitHubError("Invalid head commit")
    stdout = _gh(token, "api", f"repos/{github}/compare/{base}...{head}")
    try:
        answer = json.loads(stdout)
        behind_by = answer["behind_by"]
        files = answer["files"]
        if (
            not isinstance(behind_by, int)
            or isinstance(behind_by, bool)
            or behind_by < 0
            or not isinstance(files, list)
        ):
            raise ValueError
        paths: dict[str, None] = {}
        for item in files:
            if not isinstance(item, dict):
                raise ValueError
            filename = item["filename"]
            if not isinstance(filename, str) or not filename:
                raise ValueError
            paths[filename] = None
            if "previous_filename" in item:
                previous = item["previous_filename"]
                if not isinstance(previous, str):
                    raise ValueError
                paths[previous] = None
        return Comparison(
            behind_by=behind_by, files=tuple(paths), truncated=len(files) >= 300
        )
    except KeyError, TypeError, ValueError:
        pass
    raise GitHubError("api: Invalid response")


# GitHub work tracking: issues and Projects v2 (measured on gh 2.102.0, 2026-11-18)

ISSUE_MARKER_PATTERN = (
    r"<!-- orch-task:[A-Za-z0-9][A-Za-z0-9._-]*:[A-Za-z0-9][A-Za-z0-9._-]* -->"
)
BLOCK_BEGIN = "<!-- orch-progress:begin -->"
BLOCK_END = "<!-- orch-progress:end -->"
ISSUE_REASONS = ("completed", "not_planned")
ISSUE_LIST_LIMIT = 100

PROJECT_QUERY = (
    "query($owner:String!,$number:Int!){organization(login:$owner){"
    'projectV2(number:$number){id number field(name:"Status"){'
    "... on ProjectV2SingleSelectField{id options{id name}}}}}}"
)
ADD_ITEM_MUTATION = (
    "mutation($project:ID!,$content:ID!){addProjectV2ItemById("
    "input:{projectId:$project,contentId:$content}){item{id}}}"
)
ISSUE_ITEMS_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!){"
    "repository(owner:$owner,name:$name){issue(number:$number){"
    'projectItems(first:50){nodes{id project{id} fieldValueByName(name:"Status"){'
    "... on ProjectV2ItemFieldSingleSelectValue{name}}}}}}}"
)
SET_STATUS_MUTATION = (
    "mutation($project:ID!,$item:ID!,$field:ID!,$option:String!){"
    "updateProjectV2ItemFieldValue(input:{projectId:$project,itemId:$item,"
    "fieldId:$field,value:{singleSelectOptionId:$option}}){projectV2Item{id}}}"
)


class GitHubNotFoundError(GitHubError):
    """gh reported HTTP 404 for a REST call."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Issue:
    """An issue as GitHub reports it."""

    number: int
    node_id: str
    url: str
    title: str
    state: str
    state_reason: str | None
    body: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Project:
    """An organization project and its Status field."""

    id: str
    owner: str
    number: int
    status_field_id: str
    status_options: Mapping[str, str]


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectItem:
    """An issue's item in one project and its Status, if set."""

    id: str
    project_id: str
    status: str | None


def issue_number(*, github: str, url: str) -> int:
    """Return the number of an issue URL in the repository, or raise."""
    raise NotImplementedError


def project_number(*, owner: str, url: str) -> int:
    """Return the number of an organization project URL of the owner, or raise."""
    raise NotImplementedError


def replace_managed_block(body: str, block: str) -> str:
    """Replace or append the orch progress block, keeping the rest of the body."""
    raise NotImplementedError


def get_issue(*, github: str, number: int, token: str) -> Issue:
    """Read one issue of the repository."""
    raise NotImplementedError


def find_issues_by_marker(*, github: str, marker: str, token: str) -> tuple[int, ...]:
    """Return the numbers of recent issues whose body has the marker line."""
    raise NotImplementedError


def create_issue(
    *, github: str, title: str, body: str, token: str, home: Path, orch_home: Path
) -> Issue:
    """Create an issue from redacted text."""
    raise NotImplementedError


def update_issue_body(
    *, github: str, number: int, body: str, token: str, home: Path, orch_home: Path
) -> Issue:
    """Replace an issue's body with redacted text."""
    raise NotImplementedError


def set_issue_state(
    *, github: str, number: int, state: str, reason: str | None, token: str
) -> Issue:
    """Open an issue, or close it with a reason."""
    raise NotImplementedError


def get_project(*, owner: str, number: int, token: str) -> Project:
    """Read an organization project and its Status options."""
    raise NotImplementedError


def add_project_item(*, project: Project, issue: Issue, token: str) -> str:
    """Add an issue to a project and return the item id. Adding twice is harmless."""
    raise NotImplementedError


def issue_project_item(
    *, github: str, number: int, project: Project, token: str
) -> ProjectItem | None:
    """Return the issue's item in the project with its Status, or None."""
    raise NotImplementedError


def set_item_status(*, project: Project, item_id: str, status: str, token: str) -> None:
    """Set an item's Status by option name."""
    raise NotImplementedError
