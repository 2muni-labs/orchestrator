"""Stand-in for the gh CLI: strict about the calls it accepts, with no network.

State lives in $ORCH_FAKE_GH_DIR/state.json and every call is appended to calls.jsonl,
including refused ones. Unknown commands and flags, a wrong token, an unknown label and
a branch that is not on the bare repository all fail, as they would on GitHub. The
flags come from the help text of gh 2.102.0.

gh api answers the three reads of orch wait in the shapes measured on gh 2.102.0: the
review GraphQL query, the rules of main and the comparison of main with a head commit.
gh pr list answers the JSON array measured on gh 2.102.0 (2026-11-26). With
fail_after_create in the state, gh pr create records the pull request and then fails
as a server error would, as #617 did.

Each pull request keeps its label events; adding a label already there or removing
one that is not there makes no event, as measured.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

VIEW_FIELDS = {"state", "mergedAt", "mergeable"}
LIST_FIELDS = {"url", "headRefName", "baseRefName", "isCrossRepository", "state"}
LABEL_FIELDS = {"name"}
COMMANDS = {
    ("label", "list"): {
        "flags": {"--repo", "--search", "--json", "--limit"},
        "required": {"--repo", "--json"},
        "positional": 0,
    },
    ("pr", "create"): {
        "flags": {"--repo", "--head", "--base", "--title", "--label", "--body-file"},
        "required": {"--repo", "--head", "--base", "--title", "--body-file"},
        "positional": 0,
    },
    ("pr", "comment"): {
        "flags": {"--body-file"},
        "required": {"--body-file"},
        "positional": 1,
    },
    ("pr", "view"): {"flags": {"--json"}, "required": {"--json"}, "positional": 1},
    ("pr", "list"): {
        "flags": {"--repo", "--head", "--base", "--state", "--json", "--limit"},
        "required": {"--repo", "--json"},
        "positional": 0,
    },
    ("pr", "edit"): {
        "flags": {"--add-label", "--remove-label", "--body-file"},
        "required": set(),
        "positional": 1,
    },
}


class Refused(Exception):
    """GitHub would refuse this call."""


class FailedAfterWrite(Exception):
    """GitHub did the work but answered with a server error (fail_after_create)."""


def parse(args: list[str]) -> tuple[tuple[str, str], dict[str, list[str]], list[str]]:
    command = (args[0], args[1]) if len(args) >= 2 else ("", "")
    rule = COMMANDS.get(command)
    if rule is None:
        raise Refused(f"unknown command: {' '.join(args[:2])}")
    options: dict[str, list[str]] = {}
    positional: list[str] = []
    rest = args[2:]
    while rest:
        item = rest.pop(0)
        if item.startswith("-") and item != "-":
            if item not in rule["flags"]:
                raise Refused(f"unknown flag: {item}")
            if not rest:
                raise Refused(f"flag needs a value: {item}")
            options.setdefault(item, []).append(rest.pop(0))
        else:
            positional.append(item)
    if len(positional) != rule["positional"]:
        raise Refused(f"wrong number of arguments: {positional}")
    missing = rule["required"] - options.keys()
    if missing:
        raise Refused(f"missing flags: {sorted(missing)}")
    return command, options, positional


def single(options: dict[str, list[str]], flag: str) -> str:
    values = options[flag]
    if len(values) != 1:
        raise Refused(f"flag given more than once: {flag}")
    return values[0]


def branch_exists(bare: str, branch: str) -> bool:
    return (
        subprocess.run(
            ["git", "--git-dir", bare, "rev-parse", "--verify", "--quiet", branch],
            capture_output=True,
        ).returncode
        == 0
    )


def find_pull_request(state: dict, url: str) -> dict:
    for item in state["pull_requests"]:
        if item["url"] == url:
            return item
    raise Refused(f"no pull request: {url}")


def read_body(options: dict[str, list[str]], stdin: str) -> str:
    if single(options, "--body-file") != "-":
        raise Refused("expected the body on standard input")
    if not stdin.strip():
        raise Refused("empty body")
    return stdin


def label_list(state: dict, options: dict[str, list[str]]) -> str:
    if single(options, "--repo") != state["repo"]:
        raise Refused("unknown repository")
    fields = set(single(options, "--json").split(","))
    if fields - LABEL_FIELDS:
        raise Refused(f"unknown fields: {sorted(fields - LABEL_FIELDS)}")
    search = options.get("--search", [""])[0].casefold()
    names = [name for name in state["labels"] if search in name.casefold()]
    return json.dumps([{"name": name} for name in names]) + "\n"


def pr_create(state: dict, options: dict[str, list[str]], stdin: str) -> str:
    if single(options, "--repo") != state["repo"]:
        raise Refused("unknown repository")
    head = single(options, "--head")
    base = single(options, "--base")
    title = single(options, "--title")
    labels = options.get("--label", [])
    body = read_body(options, stdin)
    if not title.strip():
        raise Refused("empty title")
    for name in labels:
        if name not in state["labels"]:
            raise Refused(f"unknown label: {name}")
    for branch in (head, base):
        if not branch_exists(state["bare"], f"refs/heads/{branch}"):
            raise Refused(f"branch is not on the remote: {branch}")
    if any(
        item["head"] == head and item["state"] == "OPEN"
        for item in state["pull_requests"]
    ):
        raise Refused(f"pull request already exists: {head}")
    number = len(state["pull_requests"]) + 1
    url = f"https://github.com/{state['repo']}/pull/{number}"
    state["pull_requests"].append(
        {
            "number": number,
            "url": url,
            "state": "OPEN",
            "mergedAt": None,
            "head": head,
            "base": base,
            "title": title,
            "body": body,
            "labels": [],
            "events": [],
            "checks": {"ci": ["COMPLETED", "SUCCESS"]},
            "review_states": [],
            "comments": [],
        }
    )
    edit_labels(state["pull_requests"][-1], labels, [])
    if state.get("fail_after_create"):
        raise FailedAfterWrite(
            "HTTP 502: Server Error (https://api.github.com/graphql)"
        )
    return url + "\n"


def pr_list(state: dict, options: dict[str, list[str]]) -> str:
    if single(options, "--repo") != state["repo"]:
        raise Refused("unknown repository")
    fields = single(options, "--json").split(",")
    if set(fields) - LIST_FIELDS:
        raise Refused(f"unknown fields: {sorted(set(fields) - LIST_FIELDS)}")
    wanted = options.get("--state", ["open"])[0]
    if wanted not in {"open", "closed", "merged", "all"}:
        raise Refused(f"bad state: {wanted}")
    limit = int(options.get("--limit", ["30"])[0])
    head = options.get("--head", [None])[0]
    base = options.get("--base", [None])[0]
    found = []
    for item in reversed(state["pull_requests"]):
        if head is not None and item["head"] != head:
            continue
        if base is not None and item["base"] != base:
            continue
        if wanted != "all" and item["state"] != wanted.upper():
            continue
        values = {
            "url": item["url"],
            "headRefName": item["head"],
            "baseRefName": item["base"],
            "isCrossRepository": False,
            "state": item["state"],
        }
        found.append({field: values[field] for field in fields})
    return json.dumps(found[:limit]) + "\n"


def pr_comment(state: dict, options: dict[str, list[str]], url: str, stdin: str) -> str:
    find_pull_request(state, url)["comments"].append(read_body(options, stdin))
    return url + "\n"


def pr_view(state: dict, options: dict[str, list[str]], url: str) -> str:
    fields = single(options, "--json").split(",")
    if set(fields) - VIEW_FIELDS:
        raise Refused(f"unknown fields: {sorted(set(fields) - VIEW_FIELDS)}")
    item = find_pull_request(state, url)
    # A test can change the pull request at an exact view, with no race with polling.
    if item.get("view_states"):
        item.update(item["view_states"].pop(0))
    values = {
        "state": item["state"],
        "mergedAt": item["mergedAt"],
        "mergeable": "MERGEABLE",
    }
    return json.dumps({field: values[field] for field in fields}) + "\n"


def pr_edit(state: dict, options: dict[str, list[str]], url: str, stdin: str) -> str:
    added = options.get("--add-label", [])
    removed = options.get("--remove-label", [])
    body = read_body(options, stdin) if "--body-file" in options else None
    if not added and not removed and body is None:
        raise Refused("nothing to edit")
    if body is not None:
        find_pull_request(state, url)["body"] = body
    for name in added + removed:
        if name not in state["labels"]:
            raise Refused(f"unknown label: {name}")
    edit_labels(find_pull_request(state, url), added, removed)
    return url + "\n"


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def edit_labels(item: dict, added: list[str], removed: list[str]) -> None:
    for name in added:
        if name not in item["labels"]:
            item["labels"].append(name)
            item["events"].append({"name": name, "added": True, "at": now()})
    for name in removed:
        if name in item["labels"]:
            item["labels"].remove(name)
            item["events"].append({"name": name, "added": False, "at": now()})


def git(bare: str, *args: str) -> str:
    done = subprocess.run(
        ["git", "--git-dir", bare, *args], capture_output=True, text=True
    )
    if done.returncode != 0:
        raise Refused(f"HTTP 404: git {args[0]} failed")
    return done.stdout.strip()


def api(state: dict, args: list[str]) -> str:
    if not args:
        raise Refused("api needs an endpoint")
    endpoint, rest = args[0], args[1:]
    if endpoint == "graphql":
        return graphql(state, rest)
    if rest:
        raise Refused(f"unexpected api arguments: {rest}")
    prefix = f"repos/{state['repo']}/"
    path = endpoint.removeprefix(prefix)
    if path == endpoint:
        raise Refused(f"unknown endpoint: {endpoint}")
    if path == "rules/branches/main?per_page=100":
        return rules(state)
    if path.startswith("compare/main..."):
        return compare(state, path.removeprefix("compare/main..."))
    raise Refused(f"unknown endpoint: {endpoint}")


def graphql(state: dict, rest: list[str]) -> str:
    fields: dict[str, str] = {}
    rest = list(rest)
    while rest:
        flag = rest.pop(0)
        if flag not in ("-f", "-F") or not rest:
            raise Refused(f"unknown graphql argument: {flag}")
        key, sep, value = rest.pop(0).partition("=")
        if not sep or key in fields:
            raise Refused(f"bad graphql field: {key}")
        fields[key] = value
    if set(fields) != {"query", "owner", "name", "number"}:
        raise Refused(f"unexpected graphql fields: {sorted(fields)}")
    if not fields["query"].startswith("query("):
        raise Refused("not a query")
    if f"{fields['owner']}/{fields['name']}" != state["repo"]:
        raise Refused("unknown repository")
    item = find_pull_request(
        state, f"https://github.com/{state['repo']}/pull/{fields['number']}"
    )
    # A test can change the pull request at an exact check, with no race with polling.
    if item["review_states"]:
        item.update(item["review_states"].pop(0))
    head = git(state["bare"], "rev-parse", f"refs/heads/{item['head']}")
    contexts = [
        {"__typename": "CheckRun", "name": name, "status": status, "conclusion": result}
        for name, (status, result) in item["checks"].items()
    ]
    events = [
        {
            "__typename": "LabeledEvent" if event["added"] else "UnlabeledEvent",
            "createdAt": event["at"],
            "label": {"name": event["name"]},
        }
        for event in item["events"]
    ]
    answer = {
        "state": item["state"],
        "mergedAt": item["mergedAt"],
        "headRefOid": head,
        "labels": {"nodes": [{"name": name} for name in item["labels"]]},
        "timelineItems": {"nodes": events},
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "oid": head,
                        "statusCheckRollup": (
                            {"contexts": {"nodes": contexts}} if contexts else None
                        ),
                    }
                }
            ]
        },
    }
    return json.dumps({"data": {"repository": {"pullRequest": answer}}}) + "\n"


def rules(state: dict) -> str:
    answer: list[dict] = [{"type": "deletion", "ruleset_id": 1}]
    required = state["rules"]["required"]
    if required:
        answer.append(
            {
                "type": "required_status_checks",
                "parameters": {
                    "do_not_enforce_on_create": False,
                    "required_status_checks": [
                        {"context": name, "integration_id": 15368} for name in required
                    ],
                    "strict_required_status_checks_policy": state["rules"]["strict"],
                },
                "ruleset_id": 1,
            }
        )
    return json.dumps(answer) + "\n"


def compare(state: dict, head: str) -> str:
    bare = state["bare"]
    if re.fullmatch(r"[0-9a-f]{40}", head) is None:
        raise Refused("HTTP 404: No commit found")
    git(bare, "cat-file", "-e", f"{head}^{{commit}}")
    base = git(bare, "rev-parse", "refs/heads/main")
    behind = int(git(bare, "rev-list", "--count", f"{head}..{base}"))
    ahead = int(git(bare, "rev-list", "--count", f"{base}..{head}"))
    fork = git(bare, "merge-base", base, head)
    files = []
    for line in git(bare, "diff", "--name-status", "-M", fork, head).splitlines():
        parts = line.split("\t")
        if parts[0].startswith("R"):
            files.append(
                {
                    "filename": parts[2],
                    "status": "renamed",
                    "previous_filename": parts[1],
                }
            )
        else:
            kind = {"A": "added", "D": "removed"}.get(parts[0][0], "modified")
            files.append({"filename": parts[1], "status": kind})
    status = (
        "identical"
        if not ahead and not behind
        else "ahead"
        if not behind
        else "behind"
        if not ahead
        else "diverged"
    )
    answer = {"status": status, "ahead_by": ahead, "behind_by": behind, "files": files}
    return json.dumps(answer) + "\n"


def handle(state: dict, args: list[str], stdin: str, token: str | None) -> str:
    digest = hashlib.sha256((token or "").encode()).hexdigest()
    if args[:1] == ["api"]:
        if digest != state["token_sha256"]:
            raise Refused("bad credentials")
        return api(state, args[1:])
    command, options, positional = parse(args)
    if digest != state["token_sha256"]:
        raise Refused("bad credentials")
    if command == ("label", "list"):
        return label_list(state, options)
    if command == ("pr", "create"):
        return pr_create(state, options, stdin)
    if command == ("pr", "comment"):
        return pr_comment(state, options, positional[0], stdin)
    if command == ("pr", "edit"):
        return pr_edit(state, options, positional[0], stdin)
    if command == ("pr", "list"):
        return pr_list(state, options)
    return pr_view(state, options, positional[0])


def main() -> int:
    folder = Path(os.environ["ORCH_FAKE_GH_DIR"])
    state_path = folder / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    args = sys.argv[1:]
    # orch passes DEVNULL when it has no input, which reads as empty here.
    stdin = sys.stdin.read()
    refused = None
    failed_after_write = False
    stdout = ""
    try:
        stdout = handle(state, args, stdin, os.environ.get("GH_TOKEN"))
    except Refused as exc:
        refused = str(exc)
    except FailedAfterWrite as exc:
        refused = str(exc)
        failed_after_write = True
    record = {"argv": args, "stdin": stdin, "refused": refused}
    with (folder / "calls.jsonl").open("a", encoding="utf-8") as calls:
        calls.write(json.dumps(record, ensure_ascii=False) + "\n")
    if failed_after_write:
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    if refused is not None:
        print(refused, file=sys.stderr)
        return 1
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    sys.stdout.write(stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
