"""All gh calls. The token reaches gh only through its environment."""

import json
import os
import re
import signal
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from orch.redact import redact

GH_TIMEOUT_SEC = 120
_GITHUB_PATTERN = r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._][A-Za-z0-9._-]*"
_LABEL_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]*"
_URL_PATTERN = rf"https://github\.com/{_GITHUB_PATTERN}/pull/[0-9]+"


class GitHubError(Exception):
    """A gh command failed, timed out or was refused by a safety check."""


@dataclass(frozen=True, slots=True, kw_only=True)
class PullRequestStatus:
    """What gh reports about a pull request."""

    state: str
    merged: bool
    merged_at: str | None = None
    mergeable: str | None = None


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
