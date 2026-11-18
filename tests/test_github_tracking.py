"""Tests for the issue and Projects v2 calls in github.py (GitHub work tracking).

Answers are shaped like the ones measured on gh 2.102.0 on 2026-11-18
(~/.orch/orchestrator/measure/gh-track-api.md).
"""

import json
import os
from pathlib import Path

import pytest

from orch import github
from orch.github import (
    ADD_ITEM_MUTATION,
    BLOCK_BEGIN,
    BLOCK_END,
    ISSUE_ITEMS_QUERY,
    PROJECT_QUERY,
    SET_STATUS_MUTATION,
    GitHubError,
    GitHubNotFoundError,
    Issue,
    Project,
    ProjectItem,
)
from orch.redact import ResidualPathError, SecretFoundError
from test_github import REDACT, TOKEN, FakeGh, error_chain_text

REPO = "2muni-labs/orch-sample"
ISSUE_URL = f"https://github.com/{REPO}/issues/9"
NODE = "I_kwDOVt-LrM8AAAABgLMfNA"
MARKER = "<!-- orch-task:orch-sample:m-x -->"
PROJECT = Project(
    id="PVT_kwDOFEULLc4Br-0n",
    owner="2muni-labs",
    number=7,
    status_field_id="PVTSSF_lADOFEULLc4Br-0nzhqO_VY",
    status_options={
        "Todo": "f75ad846",
        "In Progress": "47fc9ee4",
        "Done": "98236657",
        "Blocked": "66282893",
        "Cancelled": "c1a2b3d4",
    },
)
ITEM = "PVTI_lADOFEULLc4Br-0nzhJny4g"


@pytest.fixture
def gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    directory = tmp_path / "shim"
    directory.mkdir()
    fake = FakeGh(directory)
    fake.write()
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")
    return fake


def answer(gh: FakeGh, value: object, *, code: int = 0, stderr: str = "") -> None:
    gh.stdout = value if isinstance(value, str) else json.dumps(value)
    gh.code = code
    gh.stderr = stderr
    gh.write()


def issue_json(**overrides: object) -> dict:
    value = {
        "url": f"https://api.github.com/repos/{REPO}/issues/9",
        "repository_url": f"https://api.github.com/repos/{REPO}",
        "html_url": ISSUE_URL,
        "id": 6454181482,
        "node_id": NODE,
        "number": 9,
        "title": "[track] 제목",
        "state": "open",
        "state_reason": None,
        "body": f"{MARKER}\n본문",
    }
    value.update(overrides)
    return value


ISSUE = Issue(
    number=9,
    node_id=NODE,
    url=ISSUE_URL,
    title="[track] 제목",
    state="open",
    state_reason=None,
    body=f"{MARKER}\n본문",
)


def stdin_json(gh: FakeGh) -> dict:
    return json.loads(gh.stdin())


# issue_number and project_number


def test_issue_number_accepts_the_repository_in_any_case() -> None:
    assert github.issue_number(github=REPO, url=ISSUE_URL) == 9
    assert (
        github.issue_number(
            github=REPO, url=ISSUE_URL.replace("orch-sample", "Orch-Sample")
        )
        == 9
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/2muni-labs/orchestrator/issues/9",
        f"https://github.com/{REPO}/pull/9",
        f"https://github.com/{REPO}/issues/0",
        f"https://github.com/{REPO}/issues/9/",
        f"https://github.com/{REPO}/issues/9#issuecomment-1",
        f"http://github.com/{REPO}/issues/9",
        f"https://github.com/{REPO}/issues/x",
        f"https://github.com/{REPO}/issues/9\n",
    ],
)
def test_issue_number_refuses_other_urls(url: str) -> None:
    with pytest.raises(GitHubError):
        github.issue_number(github=REPO, url=url)


def test_issue_number_matches_the_repository_literally() -> None:
    with pytest.raises(GitHubError):
        github.issue_number(
            github="2muni-labs/orch.sample",
            url="https://github.com/2muni-labs/orchXsample/issues/9",
        )


def test_project_number() -> None:
    url = "https://github.com/orgs/2muni-labs/projects/7"
    assert github.project_number(owner="2muni-labs", url=url) == 7
    assert github.project_number(owner="2Muni-Labs", url=url) == 7


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/orgs/someone/projects/7",
        "https://github.com/users/2muni-labs/projects/7",
        "https://github.com/orgs/2muni-labs/projects/0",
        "https://github.com/orgs/2muni-labs/projects/7/views/1",
        "https://github.com/2muni-labs/orchestrator/projects/7",
    ],
)
def test_project_number_refuses_other_urls(url: str) -> None:
    with pytest.raises(GitHubError):
        github.project_number(owner="2muni-labs", url=url)


# replace_managed_block


def test_block_is_appended_after_a_blank_line() -> None:
    assert github.replace_managed_block("Intro\n\n", "\nState: open\n") == (
        f"Intro\n\n{BLOCK_BEGIN}\nState: open\n{BLOCK_END}\n"
    )


def test_block_alone_in_an_empty_body() -> None:
    assert github.replace_managed_block(" \n", "State: open") == (
        f"{BLOCK_BEGIN}\nState: open\n{BLOCK_END}\n"
    )


def test_block_replacement_keeps_everything_outside_byte_for_byte() -> None:
    head = "<!-- m -->\n## 목적\n  text  \n\n"
    tail = "\ntail\r\n- [x] done"
    body = f"{head}{BLOCK_BEGIN}\nold\nlines\n{BLOCK_END}{tail}"
    assert github.replace_managed_block(body, "new") == (
        f"{head}{BLOCK_BEGIN}\nnew\n{BLOCK_END}{tail}"
    )


def test_block_replacement_of_the_real_issue_shape() -> None:
    body = f"## 완료 기준\n- [ ] x\n\n{BLOCK_BEGIN}\nTask: a\nState: b\n{BLOCK_END}\n"
    assert github.replace_managed_block(body, "State: c") == (
        f"## 완료 기준\n- [ ] x\n\n{BLOCK_BEGIN}\nState: c\n{BLOCK_END}\n"
    )


def test_replacing_with_the_same_block_is_stable() -> None:
    once = github.replace_managed_block("Intro", "State: a")
    assert github.replace_managed_block(once, "State: a") == once


@pytest.mark.parametrize(
    "body",
    [
        f"{BLOCK_BEGIN}\nx",
        f"x\n{BLOCK_END}",
        f"{BLOCK_END}\nx\n{BLOCK_BEGIN}",
        f"{BLOCK_BEGIN}\n{BLOCK_END}\n{BLOCK_BEGIN}\n{BLOCK_END}",
        f"{BLOCK_BEGIN}\n{BLOCK_BEGIN}\n{BLOCK_END}",
    ],
)
def test_malformed_blocks_are_refused(body: str) -> None:
    with pytest.raises(ValueError):
        github.replace_managed_block(body, "State: a")


@pytest.mark.parametrize("block", [f"a {BLOCK_BEGIN}", f"{BLOCK_END}"])
def test_a_block_holding_a_marker_is_refused(block: str) -> None:
    with pytest.raises(ValueError):
        github.replace_managed_block("Intro", block)


# get_issue


def test_get_issue_runs_the_exact_command(gh: FakeGh) -> None:
    answer(gh, issue_json())
    assert github.get_issue(github=REPO, number=9, token=TOKEN) == ISSUE
    assert gh.calls() == [["api", f"repos/{REPO}/issues/9"]]
    assert gh.env()["GH_TOKEN"] == TOKEN


def test_get_issue_reads_a_closed_issue_and_a_null_body(gh: FakeGh) -> None:
    answer(gh, issue_json(state="closed", state_reason="not_planned", body=None))
    issue = github.get_issue(github=REPO, number=9, token=TOKEN)
    assert (issue.state, issue.state_reason, issue.body) == (
        "closed",
        "not_planned",
        "",
    )


def test_a_missing_issue_is_not_found(gh: FakeGh) -> None:
    answer(
        gh,
        '{"message":"Not Found","status":"404"}',
        code=1,
        stderr="gh: Not Found (HTTP 404)\n",
    )
    with pytest.raises(GitHubNotFoundError):
        github.get_issue(github=REPO, number=9, token=TOKEN)


@pytest.mark.parametrize(
    "stderr",
    [
        "gh: Repository was archived so is read-only. (HTTP 403)\n",
        "gh: Please specify a valid state reason. (HTTP 422)\n",
        "gh: Could not resolve to an Issue with the number of 9.\n",
        "gh: Not Found (HTTP 404) retry later\n",
    ],
)
def test_other_failures_are_not_not_found(gh: FakeGh, stderr: str) -> None:
    answer(gh, "", code=1, stderr=stderr)
    with pytest.raises(GitHubError) as caught:
        github.get_issue(github=REPO, number=9, token=TOKEN)
    assert not isinstance(caught.value, GitHubNotFoundError)


def test_existing_calls_also_raise_not_found_on_404(gh: FakeGh) -> None:
    answer(gh, "", code=1, stderr=f"gh: Not Found (HTTP 404) {TOKEN}\n")
    with pytest.raises(GitHubNotFoundError):
        github.pull_request_status(
            url="https://github.com/owner/sample/pull/7", token=TOKEN
        )


def test_not_found_hides_the_token_and_keeps_the_message_format(gh: FakeGh) -> None:
    answer(gh, "", code=1, stderr=f"{TOKEN} gh: Not Found (HTTP 404)\n")
    with pytest.raises(GitHubNotFoundError) as caught:
        github.get_issue(github=REPO, number=9, token=TOKEN)
    assert TOKEN not in error_chain_text(caught.value)
    assert str(caught.value).startswith(f"api repos/{REPO}/issues/9: ")


def test_a_timeout_is_not_not_found(
    gh: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    gh.stderr = "gh: Not Found (HTTP 404)\n"
    gh.sleep = 30
    gh.write()
    monkeypatch.setattr(github, "GH_TIMEOUT_SEC", 1)
    with pytest.raises(GitHubError) as caught:
        github.get_issue(github=REPO, number=9, token=TOKEN)
    assert not isinstance(caught.value, GitHubNotFoundError)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {
                "pull_request": {"url": "x"},
                "html_url": f"https://github.com/{REPO}/pull/9",
            },
            "Not an issue",
        ),
        (
            {"html_url": "https://github.com/2muni-labs/orchestrator/issues/9"},
            "Unexpected repository",
        ),
        ({"html_url": f"https://github.com/{REPO}/issues/10"}, "Unexpected repository"),
        (
            {"number": 10, "html_url": f"https://github.com/{REPO}/issues/10"},
            "Invalid response",
        ),
        ({"number": True}, "Invalid response"),
        ({"number": "9"}, "Invalid response"),
        ({"node_id": "bad id"}, "Invalid response"),
        ({"node_id": None}, "Invalid response"),
        ({"state": "OPEN"}, "Invalid response"),
        ({"state_reason": 3}, "Invalid response"),
        ({"title": None}, "Invalid response"),
        ({"body": 5}, "Invalid response"),
    ],
)
def test_get_issue_refuses_unexpected_answers(
    gh: FakeGh, overrides: dict, message: str
) -> None:
    answer(gh, issue_json(**overrides))
    with pytest.raises(GitHubError, match=message):
        github.get_issue(github=REPO, number=9, token=TOKEN)


@pytest.mark.parametrize("stdout", ["", "not json", "[]", "null"])
def test_get_issue_refuses_a_non_object(gh: FakeGh, stdout: str) -> None:
    answer(gh, stdout)
    with pytest.raises(GitHubError):
        github.get_issue(github=REPO, number=9, token=TOKEN)


def test_get_issue_refuses_a_missing_key(gh: FakeGh) -> None:
    value = issue_json()
    del value["node_id"]
    answer(gh, value)
    with pytest.raises(GitHubError):
        github.get_issue(github=REPO, number=9, token=TOKEN)


@pytest.mark.parametrize(
    "arguments",
    [
        {"github": "owner", "number": 9},
        {"github": "-o/r", "number": 9},
        {"github": REPO, "number": 0},
        {"github": REPO, "number": -1},
        {"github": REPO, "number": True},
        {"github": REPO, "number": "9"},
    ],
    ids=str,
)
def test_get_issue_refuses_bad_arguments_without_running_gh(
    gh: FakeGh, arguments: dict
) -> None:
    with pytest.raises(GitHubError):
        github.get_issue(token=TOKEN, **arguments)
    assert gh.calls() == []


# find_issues_by_marker


def listed(number: int, body: object, *, pull: bool = False) -> dict:
    value = {"number": number, "body": body, "node_id": f"I_{number}"}
    if pull:
        value["pull_request"] = {"url": "x"}
    return value


def test_find_runs_the_exact_list_command_and_matches_whole_lines(gh: FakeGh) -> None:
    answer(
        gh,
        [
            listed(12, f"{MARKER}\nbody"),
            listed(11, f"intro\n  {MARKER}  \nmore"),
            listed(10, f"{MARKER}x\n"),
            listed(9, f"see {MARKER}"),
            listed(8, None),
            listed(7, f"{MARKER}\n", pull=True),
            listed(6, MARKER.replace("m-x", "m-xy")),
            listed(5, f"{MARKER}"),
        ],
    )
    assert github.find_issues_by_marker(github=REPO, marker=MARKER, token=TOKEN) == (
        5,
        11,
        12,
    )
    assert gh.calls() == [
        [
            "api",
            f"repos/{REPO}/issues?state=all&sort=created&direction=desc&per_page=100",
        ]
    ]


def test_find_returns_nothing_for_no_match(gh: FakeGh) -> None:
    answer(gh, [])
    assert github.find_issues_by_marker(github=REPO, marker=MARKER, token=TOKEN) == ()


@pytest.mark.parametrize(
    "marker",
    [
        "orch-task:a:b",
        "<!-- orch-task:a -->",
        "<!-- orch-task:a:b c -->",
        f"{MARKER}\n",
        "",
    ],
)
def test_find_refuses_a_bad_marker_without_running_gh(gh: FakeGh, marker: str) -> None:
    with pytest.raises(GitHubError):
        github.find_issues_by_marker(github=REPO, marker=marker, token=TOKEN)
    assert gh.calls() == []


@pytest.mark.parametrize(
    "value",
    [
        {},
        None,
        [1],
        [{"number": "1", "body": ""}],
        [{"number": 1, "body": 2}],
        [{"body": ""}],
    ],
    ids=str,
)
def test_find_refuses_unexpected_answers(gh: FakeGh, value: object) -> None:
    answer(gh, value)
    with pytest.raises(GitHubError):
        github.find_issues_by_marker(github=REPO, marker=MARKER, token=TOKEN)


# create_issue


def create(**overrides: object) -> Issue:
    values: dict[str, object] = {
        "github": REPO,
        "title": "[track] 제목",
        "body": f"{MARKER}\n본문",
        "token": TOKEN,
        **REDACT,
    }
    values.update(overrides)
    return github.create_issue(**values)


def test_create_issue_posts_json_on_standard_input(gh: FakeGh) -> None:
    answer(gh, issue_json())
    assert create() == ISSUE
    assert gh.calls() == [
        ["api", "--method", "POST", f"repos/{REPO}/issues", "--input", "-"]
    ]
    assert stdin_json(gh) == {"title": "[track] 제목", "body": f"{MARKER}\n본문"}
    assert "제목" in gh.stdin()
    assert TOKEN not in gh.stdin()


def test_create_issue_redacts_title_and_body(gh: FakeGh) -> None:
    answer(gh, issue_json())
    path = REDACT["orch_home"] / "orchestrator" / "logs" / "a.log"
    create(title=f"see {path}", body=f"{MARKER}\nlog {path}")
    sent = stdin_json(gh)
    assert str(REDACT["home"]) not in sent["title"]
    assert str(REDACT["home"]) not in sent["body"]


@pytest.mark.parametrize("field", ["title", "body"])
@pytest.mark.parametrize(
    ("value", "error"),
    [(TOKEN, SecretFoundError), ("/" + "home" + "/someone/x", ResidualPathError)],
    ids=["token", "residual_path"],
)
def test_create_issue_text_that_must_not_leave_stops_before_gh_runs(
    gh: FakeGh, field: str, value: str, error: type[Exception]
) -> None:
    text = f"t {value}" if field == "title" else f"{MARKER}\nb {value}"
    with pytest.raises(error):
        create(**{field: text})
    assert gh.calls() == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"title": ""},
        {"title": "  "},
        {"title": "a\nb"},
        {"title": "x" * 257},
        {"body": "no marker"},
        {"body": f"intro\n{MARKER}"},
        {"body": ""},
        {"github": "owner"},
    ],
    ids=str,
)
def test_create_issue_refuses_bad_arguments_without_running_gh(
    gh: FakeGh, overrides: dict
) -> None:
    with pytest.raises(GitHubError):
        create(**overrides)
    assert gh.calls() == []


def test_create_issue_accepts_a_256_character_title(gh: FakeGh) -> None:
    answer(gh, issue_json())
    create(title="x" * 256)


def test_create_issue_refuses_an_issue_of_another_repository(gh: FakeGh) -> None:
    answer(
        gh, issue_json(html_url="https://github.com/2muni-labs/orchestrator/issues/9")
    )
    with pytest.raises(GitHubError, match="Unexpected repository"):
        create()


def test_create_issue_failure_does_not_include_the_body(gh: FakeGh) -> None:
    answer(
        gh,
        "",
        code=1,
        stderr="gh: Repository was archived so is read-only. (HTTP 403)\n",
    )
    with pytest.raises(GitHubError) as caught:
        create(body=f"{MARKER}\nprivate body words")
    assert "private body words" not in error_chain_text(caught.value)
    assert "HTTP 403" in str(caught.value)


# update_issue_body


def update(**overrides: object) -> Issue:
    values: dict[str, object] = {
        "github": REPO,
        "number": 9,
        "body": f"{MARKER}\n새 본문",
        "token": TOKEN,
        **REDACT,
    }
    values.update(overrides)
    return github.update_issue_body(**values)


def test_update_issue_body_patches_json_on_standard_input(gh: FakeGh) -> None:
    answer(gh, issue_json(body=f"{MARKER}\n새 본문"))
    assert update().body == f"{MARKER}\n새 본문"
    assert gh.calls() == [
        ["api", "--method", "PATCH", f"repos/{REPO}/issues/9", "--input", "-"]
    ]
    assert stdin_json(gh) == {"body": f"{MARKER}\n새 본문"}


def test_update_issue_body_redacts(gh: FakeGh) -> None:
    answer(gh, issue_json())
    with pytest.raises(SecretFoundError):
        update(body=f"x {TOKEN}")
    assert gh.calls() == []
    path = REDACT["home"] / "Repositories" / "x"
    update(body=f"see {path}")
    assert str(REDACT["home"]) not in stdin_json(gh)["body"]


@pytest.mark.parametrize(
    "overrides", [{"body": ""}, {"body": " \n"}, {"number": 0}], ids=str
)
def test_update_issue_body_refuses_bad_arguments(gh: FakeGh, overrides: dict) -> None:
    with pytest.raises(GitHubError):
        update(**overrides)
    assert gh.calls() == []


def test_update_issue_body_refuses_another_issue_in_the_answer(gh: FakeGh) -> None:
    answer(gh, issue_json(number=10, html_url=f"https://github.com/{REPO}/issues/10"))
    with pytest.raises(GitHubError):
        update()


def test_update_of_a_missing_issue_is_not_found(gh: FakeGh) -> None:
    answer(gh, "", code=1, stderr="gh: Not Found (HTTP 404)\n")
    with pytest.raises(GitHubNotFoundError):
        update()


# set_issue_state


@pytest.mark.parametrize("reason", ["completed", "not_planned"])
def test_close_with_a_reason(gh: FakeGh, reason: str) -> None:
    answer(gh, issue_json(state="closed", state_reason=reason))
    issue = github.set_issue_state(
        github=REPO, number=9, state="closed", reason=reason, token=TOKEN
    )
    assert (issue.state, issue.state_reason) == ("closed", reason)
    assert gh.calls() == [
        ["api", "--method", "PATCH", f"repos/{REPO}/issues/9", "--input", "-"]
    ]
    assert stdin_json(gh) == {"state": "closed", "state_reason": reason}


def test_reopen(gh: FakeGh) -> None:
    answer(gh, issue_json(state="open", state_reason="reopened"))
    issue = github.set_issue_state(
        github=REPO, number=9, state="open", reason=None, token=TOKEN
    )
    assert (issue.state, issue.state_reason) == ("open", "reopened")
    assert stdin_json(gh) == {"state": "open"}


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ("closed", None),
        ("closed", "duplicate"),
        ("closed", "reopened"),
        ("open", "completed"),
        ("reopened", None),
        ("CLOSED", "completed"),
    ],
)
def test_set_issue_state_refuses_bad_pairs_without_running_gh(
    gh: FakeGh, state: str, reason: str | None
) -> None:
    with pytest.raises(GitHubError):
        github.set_issue_state(
            github=REPO, number=9, state=state, reason=reason, token=TOKEN
        )
    assert gh.calls() == []


@pytest.mark.parametrize(
    ("state", "reason", "returned"),
    [
        ("closed", "not_planned", {"state": "closed", "state_reason": "completed"}),
        ("closed", "completed", {"state": "open", "state_reason": None}),
        ("open", None, {"state": "closed", "state_reason": "completed"}),
    ],
)
def test_set_issue_state_refuses_an_answer_that_did_not_apply(
    gh: FakeGh, state: str, reason: str | None, returned: dict
) -> None:
    answer(gh, issue_json(**returned))
    with pytest.raises(GitHubError, match="Invalid response"):
        github.set_issue_state(
            github=REPO, number=9, state=state, reason=reason, token=TOKEN
        )


# get_project


def project_json(**overrides: object) -> dict:
    project = {
        "id": "PVT_kwDOFEULLc4Br-0n",
        "number": 7,
        "field": {
            "id": "PVTSSF_lADOFEULLc4Br-0nzhqO_VY",
            "options": [
                {"id": "f75ad846", "name": "Todo"},
                {"id": "47fc9ee4", "name": "In Progress"},
                {"id": "98236657", "name": "Done"},
                {"id": "66282893", "name": "Blocked"},
                {"id": "c1a2b3d4", "name": "Cancelled"},
            ],
        },
    }
    project.update(overrides)
    return {"data": {"organization": {"projectV2": project}}}


def test_get_project_runs_the_exact_query(gh: FakeGh) -> None:
    answer(gh, project_json())
    assert github.get_project(owner="2muni-labs", number=7, token=TOKEN) == PROJECT
    assert gh.calls() == [
        [
            "api",
            "graphql",
            "-f",
            f"query={PROJECT_QUERY}",
            "-f",
            "owner=2muni-labs",
            "-F",
            "number=7",
        ]
    ]


def test_the_queries_are_the_measured_ones() -> None:
    assert PROJECT_QUERY == (
        "query($owner:String!,$number:Int!){organization(login:$owner)"
        '{projectV2(number:$number){id number field(name:"Status")'
        "{... on ProjectV2SingleSelectField{id options{id name}}}}}}"
    )
    assert ADD_ITEM_MUTATION == (
        "mutation($project:ID!,$content:ID!){addProjectV2ItemById("
        "input:{projectId:$project,contentId:$content}){item{id}}}"
    )
    assert ISSUE_ITEMS_QUERY == (
        "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name)"
        "{issue(number:$number){projectItems(first:50){nodes{id project{id} "
        'fieldValueByName(name:"Status")'
        "{... on ProjectV2ItemFieldSingleSelectValue{name}}}}}}}"
    )
    assert SET_STATUS_MUTATION == (
        "mutation($project:ID!,$item:ID!,$field:ID!,$option:String!){"
        "updateProjectV2ItemFieldValue(input:{projectId:$project,itemId:$item,fieldId:$field,"
        "value:{singleSelectOptionId:$option}}){projectV2Item{id}}}"
    )


@pytest.mark.parametrize(
    "value",
    [
        project_json(field=None),
        project_json(field={}),
        project_json(number=8),
        project_json(id="bad id"),
        project_json(
            field={
                "id": "F_1",
                "options": [{"id": "a", "name": "Todo"}, {"id": "b", "name": "Todo"}],
            }
        ),
        project_json(field={"id": "F_1", "options": [{"id": "a", "name": ""}]}),
        project_json(
            field={"id": "F_1", "options": [{"id": "bad id", "name": "Todo"}]}
        ),
        project_json(field={"id": "F_1", "options": "Todo"}),
        {"data": {"organization": {"projectV2": None}}},
        {"data": {"organization": None}},
        {"data": None},
        [],
    ],
    ids=str,
)
def test_get_project_refuses_unexpected_answers(gh: FakeGh, value: object) -> None:
    answer(gh, value)
    with pytest.raises(GitHubError):
        github.get_project(owner="2muni-labs", number=7, token=TOKEN)


@pytest.mark.parametrize(
    ("owner", "number"),
    [("-x", 7), ("a/b", 7), ("", 7), ("2muni-labs", 0), ("2muni-labs", True)],
)
def test_get_project_refuses_bad_arguments_without_running_gh(
    gh: FakeGh, owner: str, number: object
) -> None:
    with pytest.raises(GitHubError):
        github.get_project(owner=owner, number=number, token=TOKEN)
    assert gh.calls() == []


def test_a_missing_project_is_a_github_error(gh: FakeGh) -> None:
    answer(
        gh,
        {
            "data": {"organization": {"projectV2": None}},
            "errors": [{"type": "NOT_FOUND"}],
        },
        code=1,
        stderr="gh: Could not resolve to a ProjectV2 with the number 9999.\n",
    )
    with pytest.raises(GitHubError) as caught:
        github.get_project(owner="2muni-labs", number=9999, token=TOKEN)
    assert not isinstance(caught.value, GitHubNotFoundError)


# add_project_item


def test_add_project_item_runs_the_exact_mutation(gh: FakeGh) -> None:
    answer(gh, {"data": {"addProjectV2ItemById": {"item": {"id": ITEM}}}})
    assert github.add_project_item(project=PROJECT, issue=ISSUE, token=TOKEN) == ITEM
    assert gh.calls() == [
        [
            "api",
            "graphql",
            "-f",
            f"query={ADD_ITEM_MUTATION}",
            "-f",
            f"project={PROJECT.id}",
            "-f",
            f"content={NODE}",
        ]
    ]


@pytest.mark.parametrize(
    "value",
    [
        {"data": {"addProjectV2ItemById": None}},
        {"data": {"addProjectV2ItemById": {"item": {"id": "bad id"}}}},
        {"data": {"addProjectV2ItemById": {"item": None}}},
        "",
    ],
    ids=str,
)
def test_add_project_item_refuses_unexpected_answers(gh: FakeGh, value: object) -> None:
    answer(gh, value)
    with pytest.raises(GitHubError):
        github.add_project_item(project=PROJECT, issue=ISSUE, token=TOKEN)


def test_add_project_item_refuses_bad_ids_without_running_gh(gh: FakeGh) -> None:
    for project, issue in (
        (Project(**{**_fields(PROJECT), "id": "x y"}), ISSUE),
        (PROJECT, Issue(**{**_fields(ISSUE), "node_id": "-f\nquery"})),
    ):
        with pytest.raises(GitHubError):
            github.add_project_item(project=project, issue=issue, token=TOKEN)
    assert gh.calls() == []


def _fields(value: object) -> dict:
    return {name: getattr(value, name) for name in value.__dataclass_fields__}


# issue_project_item


def items_json(*nodes: dict) -> dict:
    return {"data": {"repository": {"issue": {"projectItems": {"nodes": list(nodes)}}}}}


def node(
    item: str = ITEM, project: str = PROJECT.id, status: object = {"name": "Todo"}
) -> dict:
    return {"id": item, "project": {"id": project}, "fieldValueByName": status}


def test_issue_project_item_runs_the_exact_query(gh: FakeGh) -> None:
    answer(gh, items_json(node(project="PVT_other", item="PVTI_other"), node()))
    assert github.issue_project_item(
        github=REPO, number=9, project=PROJECT, token=TOKEN
    ) == (ProjectItem(id=ITEM, project_id=PROJECT.id, status="Todo"))
    assert gh.calls() == [
        [
            "api",
            "graphql",
            "-f",
            f"query={ISSUE_ITEMS_QUERY}",
            "-f",
            "owner=2muni-labs",
            "-f",
            "name=orch-sample",
            "-F",
            "number=9",
        ]
    ]


@pytest.mark.parametrize("status", [None, {}])
def test_issue_project_item_without_a_status(gh: FakeGh, status: object) -> None:
    answer(gh, items_json(node(status=status)))
    item = github.issue_project_item(
        github=REPO, number=9, project=PROJECT, token=TOKEN
    )
    assert item == ProjectItem(id=ITEM, project_id=PROJECT.id, status=None)


def test_issue_project_item_not_in_the_project(gh: FakeGh) -> None:
    answer(gh, items_json(node(project="PVT_other")))
    assert (
        github.issue_project_item(github=REPO, number=9, project=PROJECT, token=TOKEN)
        is None
    )
    answer(gh, items_json())
    assert (
        github.issue_project_item(github=REPO, number=9, project=PROJECT, token=TOKEN)
        is None
    )


@pytest.mark.parametrize(
    "value",
    [
        items_json(node(), node(item="PVTI_second")),
        items_json(node(item="bad id")),
        items_json(node(status={"name": 3})),
        items_json(node(status="Todo")),
        items_json({"id": ITEM, "project": None, "fieldValueByName": None}),
        {"data": {"repository": {"issue": None}}},
        {"data": {"repository": {"issue": {"projectItems": {"nodes": "x"}}}}},
    ],
    ids=str,
)
def test_issue_project_item_refuses_unexpected_answers(
    gh: FakeGh, value: object
) -> None:
    answer(gh, value)
    with pytest.raises(GitHubError):
        github.issue_project_item(github=REPO, number=9, project=PROJECT, token=TOKEN)


# set_item_status


def test_set_item_status_runs_the_exact_mutation(gh: FakeGh) -> None:
    answer(
        gh, {"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": ITEM}}}}
    )
    assert (
        github.set_item_status(
            project=PROJECT, item_id=ITEM, status="Cancelled", token=TOKEN
        )
        is None
    )
    assert gh.calls() == [
        [
            "api",
            "graphql",
            "-f",
            f"query={SET_STATUS_MUTATION}",
            "-f",
            f"project={PROJECT.id}",
            "-f",
            f"item={ITEM}",
            "-f",
            f"field={PROJECT.status_field_id}",
            "-f",
            "option=c1a2b3d4",
        ]
    ]


@pytest.mark.parametrize(
    ("item", "status"), [(ITEM, "Needs Decision"), (ITEM, "todo"), ("bad id", "Todo")]
)
def test_set_item_status_refuses_bad_arguments_without_running_gh(
    gh: FakeGh, item: str, status: str
) -> None:
    with pytest.raises(GitHubError):
        github.set_item_status(
            project=PROJECT, item_id=item, status=status, token=TOKEN
        )
    assert gh.calls() == []


@pytest.mark.parametrize(
    "value",
    [
        {
            "data": {
                "updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "PVTI_other"}}
            }
        },
        {"data": {"updateProjectV2ItemFieldValue": None}},
        "not json",
    ],
    ids=str,
)
def test_set_item_status_refuses_unexpected_answers(gh: FakeGh, value: object) -> None:
    answer(gh, value)
    with pytest.raises(GitHubError):
        github.set_item_status(
            project=PROJECT, item_id=ITEM, status="Done", token=TOKEN
        )


def test_a_rejected_option_is_a_github_error(gh: FakeGh) -> None:
    answer(
        gh,
        {
            "data": {"updateProjectV2ItemFieldValue": None},
            "errors": [{"type": "VALIDATION"}],
        },
        code=1,
        stderr="gh: The single select option Id does not belong to the field\n",
    )
    with pytest.raises(GitHubError):
        github.set_item_status(
            project=PROJECT, item_id=ITEM, status="Done", token=TOKEN
        )


# Token handling for every new call


def every_call(gh: FakeGh) -> list:
    return [
        lambda: github.get_issue(github=REPO, number=9, token=TOKEN),
        lambda: github.find_issues_by_marker(github=REPO, marker=MARKER, token=TOKEN),
        lambda: create(),
        lambda: update(),
        lambda: github.set_issue_state(
            github=REPO, number=9, state="open", reason=None, token=TOKEN
        ),
        lambda: github.get_project(owner="2muni-labs", number=7, token=TOKEN),
        lambda: github.add_project_item(project=PROJECT, issue=ISSUE, token=TOKEN),
        lambda: github.issue_project_item(
            github=REPO, number=9, project=PROJECT, token=TOKEN
        ),
        lambda: github.set_item_status(
            project=PROJECT, item_id=ITEM, status="Done", token=TOKEN
        ),
    ]


def test_every_call_passes_the_token_only_through_gh_token(gh: FakeGh) -> None:
    for call in every_call(gh):
        answer(gh, "", code=1, stderr=f"gh: boom {TOKEN} (HTTP 500)\n")
        with pytest.raises(GitHubError) as caught:
            call()
        assert TOKEN not in error_chain_text(caught.value)
        assert gh.env()["GH_TOKEN"] == TOKEN
        assert gh.env()["GH_PROMPT_DISABLED"] == "1"
    assert len(gh.calls()) == 9
    for argv in gh.calls():
        assert not any(TOKEN in arg for arg in argv)


@pytest.mark.parametrize("token", ["", "a b", "a\nb"])
def test_every_call_refuses_an_unusable_token(gh: FakeGh, token: str) -> None:
    calls = [
        lambda: github.get_issue(github=REPO, number=9, token=token),
        lambda: github.find_issues_by_marker(github=REPO, marker=MARKER, token=token),
        lambda: github.set_issue_state(
            github=REPO, number=9, state="open", reason=None, token=token
        ),
        lambda: github.get_project(owner="2muni-labs", number=7, token=token),
        lambda: github.add_project_item(project=PROJECT, issue=ISSUE, token=token),
        lambda: github.issue_project_item(
            github=REPO, number=9, project=PROJECT, token=token
        ),
        lambda: github.set_item_status(
            project=PROJECT, item_id=ITEM, status="Done", token=token
        ),
    ]
    for call in calls:
        with pytest.raises(GitHubError):
            call()
    assert gh.calls() == []


def test_no_new_call_deletes_or_touches_labels_comments_or_pulls() -> None:
    import ast

    tree = ast.parse(Path(github.__file__).read_text(encoding="utf-8"))
    constants = {
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    assert "DELETE" not in constants
    assert not any("deleteIssue" in c or "deleteProjectV2" in c for c in constants)
