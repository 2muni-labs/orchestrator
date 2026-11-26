import ast
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orch import github
from orch.github import (
    CheckResult,
    Comparison,
    GitHubError,
    LabelEvent,
    PullRequestStatus,
    RequiredChecks,
    ReviewState,
    add_label,
    check_rules,
    comment_on_pull_request,
    compare,
    create_pull_request,
    label_exists,
    pull_request_status,
    remove_label,
    required_checks,
    review_state,
)
from orch.redact import ResidualPathError, SecretFoundError

# Assembled at run time so that no path or token of this shape is in the repository.
FAKE_HOME = Path("/", "Users", "tester")
FAKE_ORCH = FAKE_HOME / ".orch"
REDACT = {"home": FAKE_HOME, "orch_home": FAKE_ORCH}
TOKEN = "gh" + "s_" + "t0ken" * 8
URL = "https://github.com/owner/sample/pull/7"


class FakeGh:
    """A gh on PATH that records its calls and answers as the test says."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.stdout = URL + "\n"
        self.stderr = ""
        self.code = 0
        self.sleep = 0

    def write(self) -> None:
        script = self.directory / "gh"
        (self.directory / "out").write_text(self.stdout, encoding="utf-8")
        (self.directory / "err").write_text(self.stderr, encoding="utf-8")
        script.write_text(
            f"""#!/bin/sh
d='{self.directory}'
for arg in "$@"; do printf '%s\\037' "$arg" >> "$d/argv"; done
printf '\\n' >> "$d/argv"
env > "$d/env"
cat > "$d/stdin"
sleep {self.sleep}
cat "$d/out"
cat "$d/err" >&2
exit {self.code}
""",
            encoding="utf-8",
        )
        script.chmod(0o755)

    def calls(self) -> list[list[str]]:
        path = self.directory / "argv"
        if not path.exists():
            return []
        return [
            line.split("\037")[:-1]
            for line in path.read_text(encoding="utf-8").splitlines()
        ]

    def env(self) -> dict[str, str]:
        lines = (self.directory / "env").read_text(encoding="utf-8").splitlines()
        return dict(line.split("=", 1) for line in lines if "=" in line)

    def stdin(self) -> str:
        return (self.directory / "stdin").read_text(encoding="utf-8")


@pytest.fixture
def gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    directory = tmp_path / "shim"
    directory.mkdir()
    fake = FakeGh(directory)
    fake.write()
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")
    return fake


def create(**overrides: object) -> str:
    values: dict[str, object] = {
        "github": "owner/sample",
        "head": "task/feat-x",
        "base": "main",
        "title": "Add feature",
        "body": "Body text",
        "token": TOKEN,
        **REDACT,
    }
    values.update(overrides)
    return create_pull_request(**values)


def comment(**overrides: object) -> None:
    values: dict[str, object] = {
        "url": URL,
        "body": "Resolved",
        "token": TOKEN,
        **REDACT,
    }
    values.update(overrides)
    comment_on_pull_request(**values)


def error_chain_text(error: BaseException) -> str:
    parts: list[str] = []
    current: BaseException | None = error
    while current is not None:
        parts.extend([str(current), repr(current), repr(current.args)])
        for value in vars(current).values():
            parts.append(repr(value))
        current = current.__cause__ or current.__context__
    return "\n".join(parts)


# Creating a pull request


def test_create_runs_the_exact_command_and_returns_the_url(gh: FakeGh) -> None:
    assert create() == URL
    assert gh.calls() == [
        [
            "pr",
            "create",
            "--repo",
            "owner/sample",
            "--head",
            "task/feat-x",
            "--base",
            "main",
            "--title",
            "Add feature",
            "--body-file",
            "-",
        ]
    ]
    assert gh.stdin() == "Body text"


def test_create_returns_the_url_from_the_last_line(gh: FakeGh) -> None:
    gh.stdout = "Creating pull request\n" + URL + "\n"
    gh.write()
    assert create() == URL


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "no url\n",
        "https://github.com/other/repo/pull/7\n",
        "https://example.com/owner/sample/pull/7\n",
        URL + "/files\n",
    ],
)
def test_create_refuses_an_unexpected_answer(gh: FakeGh, stdout: str) -> None:
    gh.stdout = stdout
    gh.write()
    with pytest.raises(GitHubError):
        create()


def test_title_and_body_go_through_redact(gh: FakeGh) -> None:
    worktree = FAKE_ORCH / "sample" / "worktrees" / "feat-x" / "src" / "a.py"
    create(title=f"Fix {FAKE_HOME}/x", body=f"See {worktree}")
    assert str(FAKE_HOME) not in gh.calls()[0][gh.calls()[0].index("--title") + 1]
    assert str(FAKE_HOME) not in gh.stdin()


@pytest.mark.parametrize("field", ["title", "body"])
@pytest.mark.parametrize(
    ("value", "error"),
    [
        (TOKEN, SecretFoundError),
        ("/" + "home" + "/someone/x", ResidualPathError),
    ],
    ids=["token", "residual_path"],
)
def test_text_that_must_not_leave_stops_before_gh_runs(
    gh: FakeGh, field: str, value: str, error: type[Exception]
) -> None:
    with pytest.raises(error):
        create(**{field: f"text {value}"})
    assert gh.calls() == []


def test_the_exact_token_is_blocked_whatever_its_shape(gh: FakeGh) -> None:
    with pytest.raises(SecretFoundError):
        create(token="plain-installation-value", body="has plain-installation-value")
    assert gh.calls() == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"github": "owner"},
        {"github": "-owner/sample"},
        {"github": "owner/sample/extra"},
        {"github": "owner/sample\n"},
        {"head": "main"},
        {"head": "task/-x"},
        {"head": "task/x y"},
        {"base": ""},
        {"base": "-x"},
        {"base": "--help"},
        {"title": ""},
        {"title": "  "},
        {"body": ""},
    ],
    ids=str,
)
def test_create_refuses_bad_arguments_without_running_gh(
    gh: FakeGh, overrides: dict[str, str]
) -> None:
    with pytest.raises(GitHubError):
        create(**overrides)
    assert gh.calls() == []


# Commenting


def test_comment_runs_the_exact_command_with_a_redacted_body(gh: FakeGh) -> None:
    comment(body=f"Fixed {FAKE_HOME}/x")
    assert gh.calls() == [["pr", "comment", URL, "--body-file", "-"]]
    assert str(FAKE_HOME) not in gh.stdin()


def test_comment_that_must_not_leave_stops_before_gh_runs(gh: FakeGh) -> None:
    with pytest.raises(SecretFoundError):
        comment(body=TOKEN)
    assert gh.calls() == []


def test_comment_refuses_an_empty_body(gh: FakeGh) -> None:
    with pytest.raises(GitHubError):
        comment(body=" ")
    assert gh.calls() == []


# Reading status


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (
            {
                "state": "MERGED",
                "mergedAt": "2026-11-04T00:00:00Z",
                "mergeable": "UNKNOWN",
            },
            PullRequestStatus(
                state="MERGED",
                merged=True,
                merged_at="2026-11-04T00:00:00Z",
                mergeable="UNKNOWN",
            ),
        ),
        (
            {"state": "OPEN", "mergedAt": None, "mergeable": "CONFLICTING"},
            PullRequestStatus(state="OPEN", merged=False, mergeable="CONFLICTING"),
        ),
        (
            {"state": "CLOSED", "mergedAt": None, "mergeable": "MERGEABLE"},
            PullRequestStatus(state="CLOSED", merged=False, mergeable="MERGEABLE"),
        ),
    ],
    ids=["merged", "open", "closed"],
)
def test_status_reads_gh_json(
    gh: FakeGh, answer: dict[str, object], expected: PullRequestStatus
) -> None:
    gh.stdout = json.dumps(answer)
    gh.write()
    assert pull_request_status(url=URL, token=TOKEN) == expected
    assert gh.calls() == [["pr", "view", URL, "--json", "state,mergedAt,mergeable"]]


@pytest.mark.parametrize(
    "stdout",
    [
        "not json",
        "[]",
        "{}",
        '{"state": 5, "mergedAt": null, "mergeable": "UNKNOWN"}',
        '{"state": "WEIRD", "mergedAt": null, "mergeable": "UNKNOWN"}',
        '{"state": "MERGED", "mergedAt": null, "mergeable": "UNKNOWN"}',
        '{"state": "OPEN", "mergedAt": 5, "mergeable": "UNKNOWN"}',
    ],
)
def test_status_refuses_an_unexpected_answer(gh: FakeGh, stdout: str) -> None:
    gh.stdout = stdout
    gh.write()
    with pytest.raises(GitHubError):
        pull_request_status(url=URL, token=TOKEN)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "--help",
        "7",
        "http://github.com/owner/sample/pull/7",
        "https://example.com/owner/sample/pull/7",
        "https://github.com/owner/sample/issues/7",
        "https://github.com/owner/sample/pull/7\n",
        "https://github.com/owner/sample/pull/x",
    ],
)
def test_bad_urls_are_refused_without_running_gh(gh: FakeGh, url: str) -> None:
    with pytest.raises(GitHubError):
        pull_request_status(url=url, token=TOKEN)
    with pytest.raises(GitHubError):
        comment(url=url)
    assert gh.calls() == []


# The token


def test_token_reaches_gh_only_through_gh_token(gh: FakeGh) -> None:
    gh.stdout = URL + "\n"
    gh.write()
    create()
    assert gh.env()["GH_TOKEN"] == TOKEN
    for call in gh.calls():
        assert not any(TOKEN in arg for arg in call)
    assert TOKEN not in gh.stdin()


def test_token_is_in_gh_token_and_not_in_arguments_for_each_command(
    gh: FakeGh,
) -> None:
    comment()
    assert gh.env()["GH_TOKEN"] == TOKEN
    gh.stdout = '{"state": "OPEN", "mergedAt": null, "mergeable": "UNKNOWN"}'
    gh.write()
    pull_request_status(url=URL, token=TOKEN)
    assert gh.env()["GH_TOKEN"] == TOKEN
    for call in gh.calls():
        assert not any(TOKEN in arg for arg in call)


def test_token_does_not_stay_in_this_process_environment(gh: FakeGh) -> None:
    create()
    assert "GH_TOKEN" not in os.environ or os.environ["GH_TOKEN"] != TOKEN


def test_other_tokens_do_not_reach_gh(
    gh: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "other-value")
    monkeypatch.setenv("GH_ENTERPRISE_TOKEN", "other-value")
    monkeypatch.setenv("GITHUB_ENTERPRISE_TOKEN", "other-value")
    monkeypatch.setenv("GH_TOKEN", "other-value")
    create()
    env = gh.env()
    assert env["GH_TOKEN"] == TOKEN
    assert "other-value" not in env.values()


def test_gh_does_not_prompt(gh: FakeGh) -> None:
    create()
    assert gh.env()["GH_PROMPT_DISABLED"] == "1"


@pytest.mark.parametrize("token", ["", " ", "a\nb", "a b"])
def test_unusable_tokens_are_refused_without_running_gh(gh: FakeGh, token: str) -> None:
    with pytest.raises(GitHubError):
        pull_request_status(url=URL, token=token)
    assert gh.calls() == []


def test_failure_message_has_gh_output_without_the_token(gh: FakeGh) -> None:
    gh.code = 1
    gh.stderr = f"HTTP 401 for token {TOKEN}\n"
    gh.stdout = f"debug {TOKEN}\n"
    gh.write()
    with pytest.raises(GitHubError) as caught:
        create()
    assert "HTTP 401" in str(caught.value)
    assert TOKEN not in error_chain_text(caught.value)


def test_failure_of_each_command_hides_the_token(gh: FakeGh) -> None:
    gh.code = 1
    gh.stderr = f"bad {TOKEN}\n"
    gh.write()
    for call in (create, comment, lambda: pull_request_status(url=URL, token=TOKEN)):
        with pytest.raises(GitHubError) as caught:
            call()
        assert TOKEN not in error_chain_text(caught.value)


def test_failure_message_does_not_include_the_body(gh: FakeGh) -> None:
    gh.code = 1
    gh.write()
    with pytest.raises(GitHubError) as caught:
        create(body="private body words")
    assert "private body words" not in error_chain_text(caught.value)


def test_timeout_hides_the_token_and_stops_gh(
    gh: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    gh.stderr = f"late {TOKEN}\n"
    gh.sleep = 30
    gh.write()
    monkeypatch.setattr(github, "GH_TIMEOUT_SEC", 1)
    started = time.monotonic()
    with pytest.raises(GitHubError) as caught:
        create()
    assert time.monotonic() - started < 15
    assert TOKEN not in error_chain_text(caught.value)


def test_missing_gh_is_a_github_error_without_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(GitHubError) as caught:
        pull_request_status(url=URL, token=TOKEN)
    assert TOKEN not in error_chain_text(caught.value)


def test_nothing_is_logged_or_printed_with_the_token(
    gh: FakeGh,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    gh.stdout = f"{URL}\n"
    gh.stderr = f"noise {TOKEN}\n"
    gh.write()
    create()
    gh.code = 1
    gh.write()
    with pytest.raises(GitHubError):
        create()
    captured = capfd.readouterr()
    assert TOKEN not in captured.out
    assert TOKEN not in captured.err
    assert TOKEN not in caplog.text


# Conventions


def test_module_imports_only_the_standard_library_and_redact() -> None:
    tree = ast.parse(Path(github.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    orch_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module is not None
            imported.add(node.module.split(".")[0])
            if node.module.startswith("orch"):
                orch_modules.add(node.module)
    assert imported - {"orch"} <= sys.stdlib_module_names
    assert orch_modules == {"orch.redact"}


def test_commands_have_no_merge_subcommand() -> None:
    tree = ast.parse(Path(github.__file__).read_text(encoding="utf-8"))
    constants = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "merge" not in constants


# Labels


def test_create_passes_each_label(gh: FakeGh) -> None:
    create(labels=("review-overridden", "other"))
    assert gh.calls() == [
        [
            "pr",
            "create",
            "--repo",
            "owner/sample",
            "--head",
            "task/feat-x",
            "--base",
            "main",
            "--title",
            "Add feature",
            "--label",
            "review-overridden",
            "--label",
            "other",
            "--body-file",
            "-",
        ]
    ]


@pytest.mark.parametrize("label", ["", "-x", "a b", "a,b", "--repo"])
def test_create_refuses_a_bad_label_without_running_gh(gh: FakeGh, label: str) -> None:
    with pytest.raises(GitHubError):
        create(labels=(label,))
    assert gh.calls() == []


def set_answer(gh: FakeGh, answer: object) -> None:
    gh.stdout = json.dumps(answer) if not isinstance(answer, str) else answer
    gh.write()


def exists(name: str = "review-overridden") -> bool:
    return label_exists(github="owner/sample", name=name, token=TOKEN)


def test_label_check_runs_the_exact_read_only_command(gh: FakeGh) -> None:
    set_answer(gh, [{"name": "review-overridden"}])
    assert exists() is True
    assert gh.calls() == [
        [
            "label",
            "list",
            "--repo",
            "owner/sample",
            "--search",
            "review-overridden",
            "--json",
            "name",
            "--limit",
            "100",
        ]
    ]


def test_label_check_never_creates_a_label(gh: FakeGh) -> None:
    set_answer(gh, [])
    assert exists() is False
    assert all("create" not in call for call in gh.calls())


def test_label_check_needs_the_exact_name(gh: FakeGh) -> None:
    """gh's search also returns partial matches."""
    set_answer(
        gh, [{"name": "review-overridden-old"}, {"name": "not-review-overridden"}]
    )
    assert exists() is False


def test_label_check_ignores_case_as_github_does(gh: FakeGh) -> None:
    set_answer(gh, [{"name": "Review-Overridden"}])
    assert exists() is True


@pytest.mark.parametrize(
    "answer", ["not json", "{}", '[{"title": "x"}]', "[1]", '[{"name": 1}]']
)
def test_label_check_refuses_an_unexpected_answer(gh: FakeGh, answer: str) -> None:
    set_answer(gh, answer)
    with pytest.raises(GitHubError):
        exists()


def test_label_check_raises_when_gh_fails(gh: FakeGh) -> None:
    """A failure is not the same as a missing label."""
    gh.code = 1
    gh.stderr = "HTTP 500"
    gh.write()
    with pytest.raises(GitHubError):
        exists()


@pytest.mark.parametrize(
    "options",
    [
        {"name": ""},
        {"name": "-x"},
        {"name": "a b"},
        {"github": "not a repo"},
        {"token": ""},
    ],
)
def test_label_check_refuses_bad_arguments_without_running_gh(
    gh: FakeGh, options: dict[str, str]
) -> None:
    values = {"github": "owner/sample", "name": "review-overridden", "token": TOKEN}
    values.update(options)
    with pytest.raises(GitHubError):
        label_exists(**values)
    assert gh.calls() == []


# Adding a label to an open pull request


def add(**options: str) -> None:
    values = {"url": URL, "name": "review-overridden", "token": TOKEN}
    values.update(options)
    add_label(**values)


def test_add_label_runs_the_exact_edit_command(gh: FakeGh) -> None:
    gh.stdout = URL + "\n"
    gh.write()
    assert add() is None
    assert gh.calls() == [["pr", "edit", URL, "--add-label", "review-overridden"]]


def test_add_label_never_creates_or_removes_a_label(gh: FakeGh) -> None:
    add()
    for call in gh.calls():
        assert "create" not in call
        assert "--remove-label" not in call
        assert "label" not in call[:1]


def test_add_label_passes_the_token_only_through_gh_token(gh: FakeGh) -> None:
    add()
    assert gh.env()["GH_TOKEN"] == TOKEN
    assert gh.env()["GH_PROMPT_DISABLED"] == "1"
    for call in gh.calls():
        assert not any(TOKEN in arg for arg in call)


def test_add_label_raises_without_the_token_when_gh_fails(gh: FakeGh) -> None:
    gh.code = 1
    gh.stderr = f"could not add label: {TOKEN}\n"
    gh.stdout = f"debug {TOKEN}\n"
    gh.write()
    with pytest.raises(GitHubError) as caught:
        add()
    assert "could not add label" in str(caught.value)
    assert TOKEN not in error_chain_text(caught.value)


def test_add_label_timeout_hides_the_token(
    gh: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    gh.stderr = f"late {TOKEN}\n"
    gh.sleep = 30
    gh.write()
    monkeypatch.setattr(github, "GH_TIMEOUT_SEC", 1)
    with pytest.raises(GitHubError) as caught:
        add()
    assert TOKEN not in error_chain_text(caught.value)


@pytest.mark.parametrize(
    "options",
    [
        {"name": ""},
        {"name": "-x"},
        {"name": "a b"},
        {"name": "--remove-label"},
        {"url": ""},
        {"url": "--help"},
        {"url": "https://github.com/owner/sample/issues/7"},
        {"token": ""},
        {"token": "a b"},
    ],
)
def test_add_label_refuses_bad_arguments_without_running_gh(
    gh: FakeGh, options: dict[str, str]
) -> None:
    with pytest.raises(GitHubError) as caught:
        add(**options)
    assert gh.calls() == []
    assert TOKEN not in error_chain_text(caught.value)


# The bot never adds or removes user-approved (ADR 0020)


@pytest.mark.parametrize("name", ["user-approved", "User-Approved", "USER-APPROVED"])
def test_add_label_refuses_user_approved_without_running_gh(
    gh: FakeGh, name: str
) -> None:
    with pytest.raises(GitHubError):
        add(name=name)
    assert gh.calls() == []


@pytest.mark.parametrize("name", ["user-approved", "User-Approved"])
def test_create_refuses_user_approved_without_running_gh(gh: FakeGh, name: str) -> None:
    with pytest.raises(GitHubError):
        create(labels=("verify-deep", name))
    assert gh.calls() == []


def test_add_label_still_adds_the_review_labels(gh: FakeGh) -> None:
    for name in github.REVIEW_LABELS:
        add(name=name)
    assert gh.calls() == [
        ["pr", "edit", URL, "--add-label", "verify-light"],
        ["pr", "edit", URL, "--add-label", "verify-deep"],
    ]


# Removing a label


def remove(**options: str) -> None:
    values = {"url": URL, "name": "verify-light", "token": TOKEN}
    values.update(options)
    remove_label(**values)


def test_remove_label_runs_the_exact_edit_command(gh: FakeGh) -> None:
    assert remove() is None
    remove(name="changes-requested")
    assert gh.calls() == [
        ["pr", "edit", URL, "--remove-label", "verify-light"],
        ["pr", "edit", URL, "--remove-label", "changes-requested"],
    ]
    assert gh.env()["GH_TOKEN"] == TOKEN


@pytest.mark.parametrize(
    "name",
    ["user-approved", "User-Approved", "review-overridden", "Review-Overridden"],
)
def test_remove_label_refuses_the_labels_the_bot_never_removes(
    gh: FakeGh, name: str
) -> None:
    with pytest.raises(GitHubError):
        remove(name=name)
    assert gh.calls() == []


@pytest.mark.parametrize(
    "options",
    [
        {"url": "https://example.com/owner/sample/pull/7"},
        {"name": "-x"},
        {"name": "two words"},
        {"token": ""},
    ],
)
def test_remove_label_refuses_bad_arguments_without_running_gh(
    gh: FakeGh, options: dict[str, str]
) -> None:
    with pytest.raises(GitHubError):
        remove(**options)
    assert gh.calls() == []


def test_remove_label_raises_without_the_token_when_gh_fails(gh: FakeGh) -> None:
    gh.code = 1
    gh.stderr = f"'x' not found {TOKEN}\n"
    gh.write()
    with pytest.raises(GitHubError) as caught:
        remove()
    assert "not found" in str(caught.value)
    assert TOKEN not in error_chain_text(caught.value)


# Reading the review state


SHA = "d0f05422f4b72ff9df4d76762e42a42060a501f4"


def review_answer(**pull_request: object) -> str:
    values: dict[str, object] = {
        "state": "OPEN",
        "mergedAt": None,
        "headRefOid": SHA,
        "labels": {"nodes": []},
        "timelineItems": {"nodes": []},
        "commits": {"nodes": [{"commit": {"oid": SHA, "statusCheckRollup": None}}]},
    }
    values.update(pull_request)
    return json.dumps({"data": {"repository": {"pullRequest": values}}})


def rollup(*nodes: dict[str, object]) -> dict[str, object]:
    commit = {"oid": SHA, "statusCheckRollup": {"contexts": {"nodes": list(nodes)}}}
    return {"nodes": [{"commit": commit}]}


def run(name: str, status: str, conclusion: str | None) -> dict[str, object]:
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion,
    }


def read_review(gh: FakeGh, **pull_request: object) -> ReviewState:
    gh.stdout = review_answer(**pull_request)
    gh.write()
    return review_state(url=URL, token=TOKEN)


def test_review_state_runs_the_exact_graphql_command(gh: FakeGh) -> None:
    read_review(gh)
    assert gh.calls() == [
        [
            "api",
            "graphql",
            "-f",
            f"query={github.REVIEW_QUERY}",
            "-f",
            "owner=owner",
            "-f",
            "name=sample",
            "-F",
            "number=7",
        ]
    ]
    assert gh.env()["GH_TOKEN"] == TOKEN
    assert not any(TOKEN in arg for arg in gh.calls()[0])


def test_review_state_reads_the_measured_answer(gh: FakeGh) -> None:
    state = read_review(
        gh,
        state="MERGED",
        mergedAt="2026-11-16T12:46:05Z",
        labels={"nodes": [{"name": "needs-user"}, {"name": "verify-deep"}]},
        timelineItems={
            "nodes": [
                {
                    "__typename": "LabeledEvent",
                    "createdAt": "2026-11-16T13:09:45Z",
                    "label": {"name": "needs-user"},
                },
                {
                    "__typename": "UnlabeledEvent",
                    "createdAt": "2026-11-16T13:09:47Z",
                    "label": {"name": "needs-user"},
                },
            ]
        },
        commits=rollup(run("ci", "COMPLETED", "SUCCESS")),
    )
    assert state == ReviewState(
        state="MERGED",
        merged=True,
        merged_at="2026-11-16T12:46:05Z",
        head_sha=SHA,
        labels=("needs-user", "verify-deep"),
        events=(
            LabelEvent(
                name="needs-user",
                added=True,
                at=datetime(2026, 11, 16, 13, 9, 45, tzinfo=UTC),
            ),
            LabelEvent(
                name="needs-user",
                added=False,
                at=datetime(2026, 11, 16, 13, 9, 47, tzinfo=UTC),
            ),
        ),
        checks={"ci": CheckResult.PASSED},
    )
    assert state.events[0].at.utcoffset() is not None


def test_review_state_of_an_open_pull_request_without_checks(gh: FakeGh) -> None:
    state = read_review(gh)
    assert state == ReviewState(
        state="OPEN",
        merged=False,
        merged_at=None,
        head_sha=SHA,
        labels=(),
        events=(),
        checks={},
    )


def test_review_state_of_a_closed_pull_request(gh: FakeGh) -> None:
    state = read_review(gh, state="CLOSED")
    assert (state.state, state.merged, state.merged_at) == ("CLOSED", False, None)


def test_review_state_sorts_events_oldest_first(gh: FakeGh) -> None:
    def event(kind: str, at: str, name: str) -> dict[str, object]:
        return {"__typename": kind, "createdAt": at, "label": {"name": name}}

    state = read_review(
        gh,
        timelineItems={
            "nodes": [
                event("LabeledEvent", "2026-11-16T13:00:02Z", "c"),
                event("UnlabeledEvent", "2026-11-16T13:00:01Z", "b"),
                event("LabeledEvent", "2026-11-16T13:00:01Z", "a"),
            ]
        },
    )
    assert [(item.name, item.added) for item in state.events] == [
        ("b", False),
        ("a", True),
        ("c", True),
    ]


@pytest.mark.parametrize(
    ("status", "conclusion", "expected"),
    [
        ("COMPLETED", "SUCCESS", CheckResult.PASSED),
        ("COMPLETED", "NEUTRAL", CheckResult.PASSED),
        ("COMPLETED", "SKIPPED", CheckResult.PASSED),
        ("COMPLETED", "STALE", CheckResult.PENDING),
        ("COMPLETED", "FAILURE", CheckResult.FAILED),
        ("COMPLETED", "CANCELLED", CheckResult.FAILED),
        ("COMPLETED", "TIMED_OUT", CheckResult.FAILED),
        ("COMPLETED", "ACTION_REQUIRED", CheckResult.FAILED),
        ("COMPLETED", "STARTUP_FAILURE", CheckResult.FAILED),
        ("IN_PROGRESS", None, CheckResult.PENDING),
        ("QUEUED", None, CheckResult.PENDING),
        ("PENDING", None, CheckResult.PENDING),
        ("WAITING", None, CheckResult.PENDING),
        ("REQUESTED", None, CheckResult.PENDING),
        ("IN_PROGRESS", "", CheckResult.PENDING),
    ],
)
def test_review_state_classifies_a_check_run(
    gh: FakeGh, status: str, conclusion: str | None, expected: CheckResult
) -> None:
    state = read_review(gh, commits=rollup(run("ci", status, conclusion)))
    assert state.checks == {"ci": expected}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("SUCCESS", CheckResult.PASSED),
        ("PENDING", CheckResult.PENDING),
        ("EXPECTED", CheckResult.PENDING),
        ("FAILURE", CheckResult.FAILED),
        ("ERROR", CheckResult.FAILED),
    ],
)
def test_review_state_classifies_a_status_context(
    gh: FakeGh, value: str, expected: CheckResult
) -> None:
    node = {"__typename": "StatusContext", "context": "legacy", "state": value}
    state = read_review(gh, commits=rollup(node, run("ci", "COMPLETED", "SUCCESS")))
    assert state.checks == {"legacy": expected, "ci": CheckResult.PASSED}


@pytest.mark.parametrize(
    ("conclusions", "expected"),
    [
        (("SUCCESS", "FAILURE"), CheckResult.FAILED),
        (("FAILURE", "SUCCESS"), CheckResult.FAILED),
        (("SUCCESS", None), CheckResult.PENDING),
        ((None, "SUCCESS"), CheckResult.PENDING),
        ((None, "FAILURE"), CheckResult.FAILED),
        (("SUCCESS", "SUCCESS"), CheckResult.PASSED),
    ],
)
def test_review_state_takes_the_worst_result_of_one_name(
    gh: FakeGh, conclusions: tuple[str | None, ...], expected: CheckResult
) -> None:
    nodes = [
        run("ci", "COMPLETED" if conclusion else "IN_PROGRESS", conclusion)
        for conclusion in conclusions
    ]
    assert read_review(gh, commits=rollup(*nodes)).checks == {"ci": expected}


def bad_event(**changes: object) -> dict[str, object]:
    node: dict[str, object] = {
        "__typename": "LabeledEvent",
        "createdAt": "2026-11-16T13:09:45Z",
        "label": {"name": "needs-user"},
    }
    node.update(changes)
    return {"nodes": [node]}


@pytest.mark.parametrize(
    "stdout",
    [
        "not json",
        "[]",
        "{}",
        '{"data": {"repository": {"pullRequest": null}}}',
        '{"data": {"repository": null}}',
        review_answer(state="WEIRD"),
        review_answer(state="MERGED"),
        review_answer(mergedAt=5),
        review_answer(headRefOid=None),
        review_answer(labels={"nodes": [{"name": 5}]}),
        review_answer(labels=None),
        review_answer(timelineItems=bad_event(__typename="ClosedEvent")),
        review_answer(timelineItems=bad_event(createdAt="yesterday")),
        review_answer(timelineItems=bad_event(createdAt=None)),
        review_answer(timelineItems=bad_event(label=None)),
        review_answer(commits={"nodes": []}),
        review_answer(commits=rollup(run("ci", "COMPLETED", "WEIRD"))),
        review_answer(commits=rollup(run("ci", "COMPLETED", None))),
        review_answer(commits=rollup(run("ci", "WEIRD", None))),
        review_answer(commits=rollup(run(5, "COMPLETED", "SUCCESS"))),  # type: ignore[arg-type]
        review_answer(commits=rollup({"__typename": "Other", "name": "ci"})),
        review_answer(
            commits=rollup(
                {"__typename": "StatusContext", "context": "x", "state": "WEIRD"}
            )
        ),
    ],
)
def test_review_state_refuses_an_unexpected_answer(gh: FakeGh, stdout: str) -> None:
    gh.stdout = stdout
    gh.write()
    with pytest.raises(GitHubError) as caught:
        review_state(url=URL, token=TOKEN)
    assert str(caught.value) == "api graphql: Invalid response"


def test_review_state_failure_hides_the_token(gh: FakeGh) -> None:
    gh.code = 1
    gh.stderr = f"HTTP 401 {TOKEN}\n"
    gh.stdout = review_answer()
    gh.write()
    with pytest.raises(GitHubError) as caught:
        review_state(url=URL, token=TOKEN)
    assert "HTTP 401" in str(caught.value)
    assert TOKEN not in error_chain_text(caught.value)


def test_review_state_refuses_a_last_commit_that_is_not_the_head(
    gh: FakeGh,
) -> None:
    # The head moved between the two parts of the answer: its checks are not the head's.
    other = {"nodes": [{"commit": {"oid": "f" * 40, "statusCheckRollup": None}}]}
    gh.stdout = review_answer(commits=other)
    gh.write()
    with pytest.raises(GitHubError) as caught:
        review_state(url=URL, token=TOKEN)
    assert str(caught.value) == "api graphql: Invalid response"


@pytest.mark.parametrize(
    "options",
    [{"url": "https://example.com/owner/sample/pull/7"}, {"token": "a b"}],
)
def test_review_state_refuses_bad_arguments_without_running_gh(
    gh: FakeGh, options: dict[str, str]
) -> None:
    values = {"url": URL, "token": TOKEN}
    values.update(options)
    with pytest.raises(GitHubError):
        review_state(**values)
    assert gh.calls() == []


# Reading the required checks of a branch


RULES = [
    {"type": "deletion", "ruleset_id": 1},
    {"type": "pull_request", "parameters": {"required_approving_review_count": 1}},
    {
        "type": "required_status_checks",
        "parameters": {
            "do_not_enforce_on_create": False,
            "required_status_checks": [{"context": "ci"}],
            "strict_required_status_checks_policy": True,
        },
        "ruleset_id": 1,
    },
]


def required(gh: FakeGh, answer: object, **options: str) -> tuple[str, ...]:
    gh.stdout = answer if isinstance(answer, str) else json.dumps(answer)
    gh.write()
    values = {"github": "owner/sample", "branch": "main", "token": TOKEN}
    values.update(options)
    return required_checks(**values)


def test_required_checks_runs_the_exact_read_only_command(gh: FakeGh) -> None:
    assert required(gh, RULES) == ("ci",)
    assert gh.calls() == [
        ["api", "repos/owner/sample/rules/branches/main?per_page=100"]
    ]
    assert gh.env()["GH_TOKEN"] == TOKEN


def test_required_checks_is_empty_without_the_rule(gh: FakeGh) -> None:
    assert required(gh, RULES[:2]) == ()
    assert required(gh, []) == ()


def test_required_checks_joins_every_rule_without_duplicates(gh: FakeGh) -> None:
    def rule(*names: str) -> dict[str, object]:
        checks = [{"context": name, "integration_id": 15368} for name in names]
        return {
            "type": "required_status_checks",
            "parameters": {"required_status_checks": checks},
        }

    assert required(gh, [rule("ci", "lint"), rule("lint", "docs")]) == (
        "ci",
        "lint",
        "docs",
    )


def test_required_checks_accepts_a_branch_with_a_slash(gh: FakeGh) -> None:
    required(gh, [], branch="release/1.x")
    assert gh.calls() == [
        ["api", "repos/owner/sample/rules/branches/release/1.x?per_page=100"]
    ]


@pytest.mark.parametrize(
    "answer",
    [
        "not json",
        "{}",
        "[5]",
        [{"type": "required_status_checks"}],
        [{"type": "required_status_checks", "parameters": {}}],
        [
            {
                "type": "required_status_checks",
                "parameters": {"required_status_checks": [{"context": 5}]},
            }
        ],
        [
            {
                "type": "required_status_checks",
                "parameters": {"required_status_checks": ["ci"]},
            }
        ],
    ],
)
def test_required_checks_refuses_an_unexpected_answer(
    gh: FakeGh, answer: object
) -> None:
    with pytest.raises(GitHubError) as caught:
        required(gh, answer)
    assert str(caught.value) == "api: Invalid response"


@pytest.mark.parametrize(
    "options",
    [
        {"github": "owner"},
        {"github": "owner/sample/x"},
        {"branch": ""},
        {"branch": "-main"},
        {"branch": "a..b"},
        {"branch": "main?x=1"},
        {"branch": "two words"},
        {"token": ""},
    ],
)
def test_required_checks_refuses_bad_arguments_without_running_gh(
    gh: FakeGh, options: dict[str, str]
) -> None:
    with pytest.raises(GitHubError):
        required(gh, RULES, **options)
    assert gh.calls() == []


def test_required_checks_failure_hides_the_token(gh: FakeGh) -> None:
    gh.code = 1
    gh.stderr = f"HTTP 403 {TOKEN}\n"
    gh.write()
    with pytest.raises(GitHubError) as caught:
        required_checks(github="owner/sample", branch="main", token=TOKEN)
    assert TOKEN not in error_chain_text(caught.value)


# Reading the rules of a branch, with the strict policy


def rules_of(gh: FakeGh, answer: object, **options: str) -> RequiredChecks:
    gh.stdout = answer if isinstance(answer, str) else json.dumps(answer)
    gh.write()
    values = {"github": "owner/sample", "branch": "main", "token": TOKEN}
    values.update(options)
    return check_rules(**values)


def checks_rule(*names: str, strict: object = None) -> dict[str, object]:
    parameters: dict[str, object] = {
        "required_status_checks": [{"context": name} for name in names]
    }
    if strict is not None:
        parameters["strict_required_status_checks_policy"] = strict
    return {"type": "required_status_checks", "parameters": parameters}


def test_check_rules_runs_the_same_read_only_command(gh: FakeGh) -> None:
    assert rules_of(gh, RULES) == RequiredChecks(names=("ci",), strict=True)
    assert gh.calls() == [
        ["api", "repos/owner/sample/rules/branches/main?per_page=100"]
    ]
    assert gh.env()["GH_TOKEN"] == TOKEN


def test_check_rules_without_the_rule_is_empty_and_not_strict(gh: FakeGh) -> None:
    assert rules_of(gh, RULES[:2]) == RequiredChecks(names=(), strict=False)
    assert rules_of(gh, []) == RequiredChecks(names=(), strict=False)


@pytest.mark.parametrize("strict", [None, False])
def test_check_rules_without_the_policy_is_not_strict(
    gh: FakeGh, strict: object
) -> None:
    answer = [checks_rule("ci", strict=strict)]
    assert rules_of(gh, answer) == RequiredChecks(names=("ci",), strict=False)


def test_check_rules_is_strict_if_any_rule_is_strict(gh: FakeGh) -> None:
    answer = [checks_rule("ci", "lint", strict=False), checks_rule("lint", strict=True)]
    assert rules_of(gh, answer) == RequiredChecks(names=("ci", "lint"), strict=True)


@pytest.mark.parametrize(
    "answer",
    [
        "not json",
        "{}",
        "[5]",
        [{"type": "required_status_checks"}],
        [{"type": "required_status_checks", "parameters": {}}],
        [checks_rule("ci", strict="yes")],
        [checks_rule("ci", strict=1)],
        [
            {
                "type": "required_status_checks",
                "parameters": {"required_status_checks": [{"context": 5}]},
            }
        ],
    ],
)
def test_check_rules_refuses_an_unexpected_answer(gh: FakeGh, answer: object) -> None:
    with pytest.raises(GitHubError) as caught:
        rules_of(gh, answer)
    assert str(caught.value) == "api: Invalid response"


@pytest.mark.parametrize(
    "options",
    [
        {"github": "owner"},
        {"branch": "-main"},
        {"branch": "a..b"},
        {"branch": "main?x=1"},
        {"token": ""},
    ],
)
def test_check_rules_refuses_bad_arguments_without_running_gh(
    gh: FakeGh, options: dict[str, str]
) -> None:
    with pytest.raises(GitHubError):
        rules_of(gh, RULES, **options)
    assert gh.calls() == []


def test_check_rules_failure_hides_the_token(gh: FakeGh) -> None:
    gh.code = 1
    gh.stderr = f"HTTP 403 {TOKEN}\n"
    gh.write()
    with pytest.raises(GitHubError) as caught:
        check_rules(github="owner/sample", branch="main", token=TOKEN)
    assert TOKEN not in error_chain_text(caught.value)


# Comparing a head with the default branch

# The shape measured on gh 2.102.0 with the bot token (2026-11-17, #143).
COMPARED = {
    "status": "diverged",
    "ahead_by": 2,
    "behind_by": 3,
    "total_commits": 2,
    "files": [
        {"filename": "src/a.py", "status": "modified", "additions": 1},
        {
            "filename": "docs/new.md",
            "status": "renamed",
            "previous_filename": "docs/old.md",
        },
        {"filename": "gone.py", "status": "removed"},
    ],
}


def compared(gh: FakeGh, answer: object, **options: str) -> Comparison:
    gh.stdout = answer if isinstance(answer, str) else json.dumps(answer)
    gh.write()
    values = {"github": "owner/sample", "base": "main", "head": SHA, "token": TOKEN}
    values.update(options)
    return compare(**values)


def with_files(files: object, **values: object) -> dict[str, object]:
    return {**COMPARED, "files": files, **values}


def test_compare_runs_the_exact_read_only_command(gh: FakeGh) -> None:
    assert compared(gh, COMPARED) == Comparison(
        behind_by=3,
        files=("src/a.py", "docs/new.md", "docs/old.md", "gone.py"),
        truncated=False,
    )
    assert gh.calls() == [["api", f"repos/owner/sample/compare/main...{SHA}"]]
    assert gh.env()["GH_TOKEN"] == TOKEN


def test_compare_of_an_up_to_date_head(gh: FakeGh) -> None:
    answer = with_files([], status="identical", ahead_by=0, behind_by=0)
    assert compared(gh, answer) == Comparison(behind_by=0, files=(), truncated=False)


def test_compare_lists_a_path_once(gh: FakeGh) -> None:
    files = [
        {"filename": "a.py", "status": "renamed", "previous_filename": "b.py"},
        {"filename": "b.py", "status": "added"},
    ]
    assert compared(gh, with_files(files)).files == ("a.py", "b.py")


@pytest.mark.parametrize(("count", "truncated"), [(299, False), (300, True)])
def test_compare_marks_a_list_of_300_files_as_truncated(
    gh: FakeGh, count: int, truncated: bool
) -> None:
    files = [{"filename": f"f{n}.py", "status": "added"} for n in range(count)]
    result = compared(gh, with_files(files))
    assert result.truncated is truncated
    assert len(result.files) == count


@pytest.mark.parametrize(
    "answer",
    [
        "not json",
        "[]",
        "{}",
        {key: value for key, value in COMPARED.items() if key != "behind_by"},
        {**COMPARED, "behind_by": "3"},
        {**COMPARED, "behind_by": -1},
        {**COMPARED, "behind_by": True},
        with_files(None),
        with_files([5]),
        with_files([{"status": "added"}]),
        with_files([{"filename": 5, "status": "added"}]),
        with_files([{"filename": "", "status": "added"}]),
        with_files([{"filename": "a.py", "previous_filename": 5}]),
    ],
)
def test_compare_refuses_an_unexpected_answer(gh: FakeGh, answer: object) -> None:
    with pytest.raises(GitHubError) as caught:
        compared(gh, answer)
    assert str(caught.value) == "api: Invalid response"


@pytest.mark.parametrize(
    "options",
    [
        {"github": "owner"},
        {"base": "-main"},
        {"base": "a..b"},
        {"base": "main?x=1"},
        {"head": "abc"},
        {"head": "-" + "a" * 39},
        {"head": "A" * 40},
        {"head": SHA + "?x=1"},
        {"token": ""},
    ],
)
def test_compare_refuses_bad_arguments_without_running_gh(
    gh: FakeGh, options: dict[str, str]
) -> None:
    with pytest.raises(GitHubError):
        compared(gh, COMPARED, **options)
    assert gh.calls() == []


def test_compare_failure_hides_the_token(gh: FakeGh) -> None:
    # Measured: an unknown commit makes gh exit 1 with HTTP 404.
    gh.code = 1
    gh.stderr = f"HTTP 404: No commit found {TOKEN}\n"
    gh.write()
    with pytest.raises(GitHubError) as caught:
        compare(github="owner/sample", base="main", head=SHA, token=TOKEN)
    assert "HTTP 404" in str(caught.value)
    assert TOKEN not in error_chain_text(caught.value)


# Base endings (m-compare-base-dot, #501)


@pytest.mark.parametrize("base", ["a.", "main.", "release/", "release/1.2.", "v1/"])
def test_compare_refuses_a_base_ending_with_a_dot_or_a_slash(
    gh: FakeGh, base: str
) -> None:
    with pytest.raises(GitHubError):
        compared(gh, COMPARED, base=base)
    assert gh.calls() == []


@pytest.mark.parametrize("base", ["release/1.2", "v1.0", "a", "feature/x-y_z"])
def test_compare_accepts_a_base_with_inner_dots_and_slashes(
    gh: FakeGh, base: str
) -> None:
    compared(gh, COMPARED, base=base)
    assert gh.calls() == [["api", f"repos/owner/sample/compare/{base}...{SHA}"]]


# Finding an open pull request (m-pr-adopt)

LIST_ARGV = [
    "pr",
    "list",
    "--repo",
    "owner/sample",
    "--head",
    "task/feat-x",
    "--base",
    "main",
    "--state",
    "open",
    "--json",
    "url,headRefName,baseRefName,isCrossRepository",
    "--limit",
    "10",
]


def listed(
    url: str = URL,
    head: str = "task/feat-x",
    base: str = "main",
    cross: object = False,
) -> dict[str, object]:
    return {
        "baseRefName": base,
        "headRefName": head,
        "isCrossRepository": cross,
        "url": url,
    }


def find(gh: FakeGh, answer: object, **overrides: object) -> str | None:
    gh.stdout = (answer if isinstance(answer, str) else json.dumps(answer)) + "\n"
    gh.write()
    values: dict[str, object] = {
        "github": "owner/sample",
        "head": "task/feat-x",
        "base": "main",
        "token": TOKEN,
    }
    values.update(overrides)
    return github.find_open_pull_request(**values)


def test_find_runs_the_exact_command_and_returns_the_url(gh: FakeGh) -> None:
    assert find(gh, [listed()]) == URL
    assert gh.calls() == [LIST_ARGV]
    assert gh.env()["GH_TOKEN"] == TOKEN


def test_find_returns_none_without_an_open_pull_request(gh: FakeGh) -> None:
    assert find(gh, []) is None
    assert gh.calls() == [LIST_ARGV]


@pytest.mark.parametrize(
    "item",
    [
        listed(cross=True),
        listed(head="task/feat-y"),
        listed(base="develop"),
    ],
    ids=["cross_repository", "other_head", "other_base"],
)
def test_find_ignores_pull_requests_that_do_not_match(
    gh: FakeGh, item: dict[str, object]
) -> None:
    assert find(gh, [item]) is None


def test_find_picks_the_match_among_others(gh: FakeGh) -> None:
    other = "https://github.com/owner/sample/pull/9"
    answer = [listed(url=other, cross=True), listed(), listed(url=other, base="dev")]
    assert find(gh, answer) == URL


def test_find_refuses_two_open_pull_requests(gh: FakeGh) -> None:
    other = "https://github.com/owner/sample/pull/9"
    with pytest.raises(GitHubError, match="Multiple open pull requests"):
        find(gh, [listed(), listed(url=other)])


@pytest.mark.parametrize(
    "answer",
    [
        "not json",
        "",
        {"url": URL},
        ["text"],
        [{**listed(), "url": 7}],
        [{**listed(), "headRefName": None}],
        [{**listed(), "baseRefName": 1}],
        [{**listed(), "isCrossRepository": "false"}],
        [{k: v for k, v in listed().items() if k != "isCrossRepository"}],
        [{k: v for k, v in listed().items() if k != "url"}],
    ],
    ids=str,
)
def test_find_refuses_an_invalid_answer(gh: FakeGh, answer: object) -> None:
    with pytest.raises(GitHubError, match="pr list: Invalid response"):
        find(gh, answer)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/other/repo/pull/7",
        "https://example.com/owner/sample/pull/7",
        URL + "/files",
        "https://github.com/owner/sample/issues/7",
    ],
)
def test_find_refuses_a_url_outside_the_repository(gh: FakeGh, url: str) -> None:
    with pytest.raises(GitHubError, match="pr list: Unexpected repository"):
        find(gh, [listed(url=url)])


@pytest.mark.parametrize(
    "overrides",
    [
        {"github": "owner"},
        {"github": "-owner/sample"},
        {"github": "owner/sample\n"},
        {"head": "main"},
        {"head": "task/-x"},
        {"head": "task/x y"},
        {"base": ""},
        {"base": "-x"},
        {"base": "--help"},
        {"token": ""},
        {"token": "has space"},
    ],
    ids=str,
)
def test_find_refuses_bad_arguments_without_running_gh(
    gh: FakeGh, overrides: dict[str, str]
) -> None:
    with pytest.raises(GitHubError):
        find(gh, [listed()], **overrides)
    assert gh.calls() == []


def test_a_failing_find_raises_without_the_token(gh: FakeGh) -> None:
    gh.code = 1
    gh.stderr = f"GraphQL: Something went wrong {TOKEN}\n"
    with pytest.raises(GitHubError) as caught:
        find(gh, [listed()])
    assert TOKEN not in error_chain_text(caught.value)


# Editing a pull request body (m-pr-adopt)


def edit_body(**overrides: object) -> None:
    values: dict[str, object] = {
        "url": URL,
        "body": "New body",
        "token": TOKEN,
        **REDACT,
    }
    values.update(overrides)
    github.edit_pull_request_body(**values)


def test_edit_runs_the_exact_command_with_a_redacted_body(gh: FakeGh) -> None:
    worktree = FAKE_ORCH / "sample" / "worktrees" / "feat-x" / "src" / "a.py"
    edit_body(body=f"See {worktree}")
    assert gh.calls() == [["pr", "edit", URL, "--body-file", "-"]]
    assert gh.stdin().startswith("See ")
    assert str(FAKE_HOME) not in gh.stdin()
    assert gh.env()["GH_TOKEN"] == TOKEN


@pytest.mark.parametrize(
    ("value", "error"),
    [
        (TOKEN, SecretFoundError),
        ("/" + "home" + "/someone/x", ResidualPathError),
    ],
    ids=["token", "residual_path"],
)
def test_edit_text_that_must_not_leave_stops_before_gh_runs(
    gh: FakeGh, value: str, error: type[Exception]
) -> None:
    with pytest.raises(error):
        edit_body(body=f"text {value}")
    assert gh.calls() == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"body": ""},
        {"body": " \n"},
        {"url": "https://github.com/owner/sample/issues/7"},
        {"url": URL + "/files"},
        {"url": "--help"},
        {"token": ""},
    ],
    ids=str,
)
def test_edit_refuses_bad_arguments_without_running_gh(
    gh: FakeGh, overrides: dict[str, str]
) -> None:
    with pytest.raises(GitHubError):
        edit_body(**overrides)
    assert gh.calls() == []


def test_a_failing_edit_raises(gh: FakeGh) -> None:
    gh.code = 1
    gh.stderr = "HTTP 502\n"
    gh.write()
    with pytest.raises(GitHubError):
        edit_body()
