"""Stand-in for the gh CLI: strict about the calls it accepts, with no network.

State lives in $ORCH_FAKE_GH_DIR/state.json and every call is appended to calls.jsonl,
including refused ones. Unknown commands and flags, a wrong token, an unknown label and
a branch that is not on the bare repository all fail, as they would on GitHub. The
flags come from the help text of gh 2.102.0.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

VIEW_FIELDS = {"state", "mergedAt", "mergeable"}
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
}


class Refused(Exception):
    """GitHub would refuse this call."""


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
            "labels": labels,
            "comments": [],
        }
    )
    return url + "\n"


def pr_comment(state: dict, options: dict[str, list[str]], url: str, stdin: str) -> str:
    find_pull_request(state, url)["comments"].append(read_body(options, stdin))
    return url + "\n"


def pr_view(state: dict, options: dict[str, list[str]], url: str) -> str:
    fields = single(options, "--json").split(",")
    if set(fields) - VIEW_FIELDS:
        raise Refused(f"unknown fields: {sorted(set(fields) - VIEW_FIELDS)}")
    item = find_pull_request(state, url)
    values = {
        "state": item["state"],
        "mergedAt": item["mergedAt"],
        "mergeable": "MERGEABLE",
    }
    return json.dumps({field: values[field] for field in fields}) + "\n"


def handle(state: dict, args: list[str], stdin: str, token: str | None) -> str:
    command, options, positional = parse(args)
    digest = hashlib.sha256((token or "").encode()).hexdigest()
    if digest != state["token_sha256"]:
        raise Refused("bad credentials")
    if command == ("label", "list"):
        return label_list(state, options)
    if command == ("pr", "create"):
        return pr_create(state, options, stdin)
    if command == ("pr", "comment"):
        return pr_comment(state, options, positional[0], stdin)
    return pr_view(state, options, positional[0])


def main() -> int:
    folder = Path(os.environ["ORCH_FAKE_GH_DIR"])
    state_path = folder / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    args = sys.argv[1:]
    # orch passes DEVNULL when it has no input, which reads as empty here.
    stdin = sys.stdin.read()
    refused = None
    stdout = ""
    try:
        stdout = handle(state, args, stdin, os.environ.get("GH_TOKEN"))
    except Refused as exc:
        refused = str(exc)
    record = {"argv": args, "stdin": stdin, "refused": refused}
    with (folder / "calls.jsonl").open("a", encoding="utf-8") as calls:
        calls.write(json.dumps(record, ensure_ascii=False) + "\n")
    if refused is not None:
        print(refused, file=sys.stderr)
        return 1
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    sys.stdout.write(stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
