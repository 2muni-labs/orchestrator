"""PreToolUse(Bash) guard. Reads only its input and HOME, never orch settings."""

import json
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from os.path import basename
from typing import Literal


@dataclass(frozen=True)
class Verdict:
    decision: Literal["allow", "deny", "ask"]
    reason: str = ""


_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_HEREDOC = re.compile(
    r"(?<!<)<<(?P<tabs>-?)(?!<)[ \t]*"
    r"(?:'(?P<single>[A-Za-z_][A-Za-z0-9_]*)'"
    r'|"(?P<double>[A-Za-z_][A-Za-z0-9_]*)"'
    r"|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))(?=$|[\s;&|()<>])"
)
_MERGE_ENDPOINT = re.compile(r"(^|/)repos/[^/\s]+/[^/\s]+/pulls/[^/\s]+/merge(/|\?|$)")
_WRAPPERS = {"env", "command", "exec", "nohup", "time", "sudo"}
_FAILURE_OUTPUT = (
    '{"hookSpecificOutput": {"hookEventName": "PreToolUse", '
    '"permissionDecision": "deny", '
    '"permissionDecisionReason": "Bash 훅을 처리할 수 없어 차단합니다."}}'
)


def _heredoc_commands(
    command: str, start: int, pending: list[tuple[str, bool, bool]]
) -> tuple[list[list[str]], int]:
    """Consume queued bodies, inspecting only unquoted bodies' substitutions."""
    commands: list[list[str]] = []
    index = start
    for delimiter, strip_tabs, quoted in pending:
        body_start = index
        body_end = len(command)
        while index < len(command):
            end = command.find("\n", index)
            if end == -1:
                end = len(command)
            line = command[index:end]
            next_line = min(end + 1, len(command))
            if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                body_end = index
                index = next_line
                break
            index = next_line
        if quoted:
            continue
        body = command[body_start:body_end]
        offset = 0
        while offset < len(body):
            char = body[offset]
            if char == "\\":
                offset += 2
            elif body.startswith("$(", offset) or char == "`":
                backtick = char == "`"
                nested, offset = _commands(
                    body, offset + (1 if backtick else 2), "`" if backtick else ")"
                )
                commands.extend(nested)
            else:
                offset += 1
    return commands, index


def _commands(
    command: str, start: int = 0, closing: str | None = None
) -> tuple[list[list[str]], int]:
    """Split controls outside quotes and recursively inspect substitutions.

    Substitutions become a nonempty placeholder in the enclosing word: their
    output is unknown, and must not turn a later argument into an executable.
    shlex handles quote removal and escaping for each resulting simple command.
    """
    commands: list[list[str]] = []
    current: list[str] = []
    quote: str | None = None
    pending: list[tuple[str, bool, bool]] = []
    depth = 0
    index = start

    def flush() -> None:
        words = shlex.split("".join(current), posix=True)
        if words:
            commands.append(words)
        current.clear()

    while index < len(command):
        char = command[index]
        if quote == "'":
            current.append(char)
            if char == "'":
                quote = None
            index += 1
            continue
        if char == "\\":
            current.append(command[index : index + 2])
            index += 2
            continue
        if char == "`" and closing == "`":
            if quote is not None:
                raise ValueError("Unclosed quote in substitution")
            flush()
            return commands, index + 1
        if command.startswith("$(", index) or char == "`":
            backtick = char == "`"
            nested, index = _commands(
                command, index + (1 if backtick else 2), "`" if backtick else ")"
            )
            commands.extend(nested)
            current.append("__orch_substitution__")
            continue
        if quote is not None:
            current.append(char)
            if char == quote:
                quote = None
        elif char in {"'", '"'}:
            current.append(char)
            quote = char
        elif match := _HEREDOC.match(command, index):
            delimiter = match["single"] or match["double"] or match["bare"]
            pending.append((delimiter, bool(match["tabs"]), match["bare"] is None))
            current.append(match[0])
            index = match.end()
            continue
        elif char == ")" and closing == ")" and depth == 0:
            flush()
            return commands, index + 1
        elif char in ";&|\n()":
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            flush()
            if char == "\n" and pending:
                nested, index = _heredoc_commands(command, index + 1, pending)
                commands.extend(nested)
                pending.clear()
                continue
        else:
            current.append(char)
        index += 1
    if closing is not None:
        raise ValueError("Unclosed substitution")
    flush()
    return commands, index


def _executable_index(words: list[str]) -> int:
    index = 0
    while index < len(words) and _ASSIGNMENT.match(words[index]):
        index += 1
    return index


def _force_push(args: list[str]) -> bool:
    if "push" not in args:
        return False
    return any(
        arg in {"--force", "--force-with-lease", "--force-if-includes"}
        or arg.startswith("--force-with-lease=")
        or (arg.startswith("-") and not arg.startswith("--") and "f" in arg)
        or arg.startswith("+")
        for arg in args
    )


def _judge_simple(words: list[str], *, home: str | None) -> Verdict:
    secrets = ["~/.orch/secrets", "$HOME/.orch/secrets", "${HOME}/.orch/secrets"]
    if home is not None:
        secrets.append(home.rstrip("/") + "/.orch/secrets")
    if any(
        re.search(re.escape(path) + r"(?=$|/)", word)
        for word in words
        for path in secrets
    ):
        return Verdict("deny", "비밀 파일 경로에 접근하는 명령은 차단합니다.")

    index = _executable_index(words)
    if index == len(words):
        return Verdict("allow")
    executable = basename(words[index])
    args = words[index + 1 :]
    if executable in _WRAPPERS:
        result = Verdict("allow")
        for index, arg in enumerate(args):
            if arg.startswith("-") or _ASSIGNMENT.match(arg):
                continue
            verdict = _judge_simple(args[index:], home=home)
            if verdict.decision == "deny":
                return verdict
            if verdict.decision == "ask":
                result = verdict
        return result
    if executable == "codex":
        return Verdict("deny", "Codex 직접 실행은 차단합니다. orch를 사용하세요.")
    if executable == "claude" and any(arg in {"-p", "--print"} for arg in args):
        return Verdict("deny", "Claude 비대화형 직접 실행은 차단합니다.")
    if executable == "git" and _force_push(args):
        return Verdict("deny", "Git 강제 푸시는 차단합니다.")
    if executable == "gh":
        if "pr" in args and "merge" in args[args.index("pr") + 1 :]:
            return Verdict("deny", "PR 머지는 사용자가 직접 수행해야 합니다.")
        if any(_MERGE_ENDPOINT.search(arg) for arg in args):
            return Verdict("deny", "API를 통한 PR 머지는 차단합니다.")
    if executable == "orch":
        subcommand = next((arg for arg in args if not arg.startswith("-")), None)
        if subcommand == "resume":
            return Verdict("ask", "작업을 재개하려면 사용자 확인이 필요합니다.")
        if subcommand == "kill":
            return Verdict("ask", "작업을 중단하려면 사용자 확인이 필요합니다.")
    if executable in {"bash", "sh", "zsh"}:
        for index, arg in enumerate(args[:-1]):
            if arg.startswith("-") and not arg.startswith("--") and "c" in arg:
                return judge(args[index + 1], home=home)
    return Verdict("allow")


def judge(command: str, *, home: str | None) -> Verdict:
    try:
        commands, _ = _commands(command)
        result = Verdict("allow")
        for words in commands:
            verdict = _judge_simple(words, home=home)
            if verdict.decision == "deny":
                return verdict
            if verdict.decision == "ask":
                result = verdict
        return result
    except ValueError, RecursionError:
        return Verdict("deny", "명령을 해석할 수 없어 차단합니다.")


def handle(stdin_text: str, environ: Mapping[str, str]) -> str:
    try:
        payload = json.loads(stdin_text)
        if not isinstance(payload, dict):
            return _FAILURE_OUTPUT
        tool_name = payload.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name:
            return _FAILURE_OUTPUT
        if tool_name != "Bash":
            return ""
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            return _FAILURE_OUTPUT
        command = tool_input.get("command")
        if not isinstance(command, str):
            return _FAILURE_OUTPUT
        verdict = judge(command, home=environ.get("HOME"))
        if verdict.decision == "allow":
            return ""
        if verdict.decision not in {"deny", "ask"} or not verdict.reason:
            return _FAILURE_OUTPUT
        return json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": verdict.decision,
                    "permissionDecisionReason": verdict.reason,
                }
            },
            ensure_ascii=False,
        )
    except Exception:
        return _FAILURE_OUTPUT
