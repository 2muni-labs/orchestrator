import ast
import json
import logging
import os
import sys
import time
from pathlib import Path

import pytest

from orch import github
from orch.github import (
    GitHubError,
    PullRequestStatus,
    add_label,
    comment_on_pull_request,
    create_pull_request,
    label_exists,
    pull_request_status,
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
