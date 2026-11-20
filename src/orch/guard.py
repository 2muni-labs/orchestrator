"""PreToolUse(Bash) guard. Reads only its input and HOME, never orch settings."""

import json
import re
import shlex
import time
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
# Shell builtins (command, exec) and nohup have fixed options; only external tools
# can gain value options in a newer version.
# Work units for one judge call: words read, copied or judged. A command over
# the limit, such as one with 20 000 words, is denied.
_STEP_LIMIT = 20_000
# Wall-clock limit for one judge call, well under the 10 s hook timeout. It bounds
# every cost the step count misses, such as splitting long env -S values.
_TIME_LIMIT_SEC = 3.0
# shlex reads one long word in time quadratic in its length, and the time limit
# cannot stop it midway, so a longer command is denied before it is split. The
# longest command in the local transcripts was 42 KB (2026-11-20).
_MAX_COMMAND_CHARS = 256 * 1024
_VERSIONED_WRAPPERS = {"env", "time", "sudo"}
_WRAPPER_VALUE_OPTIONS = {
    "env": ("uCPSa", {"--unset", "--chdir", "--split-string", "--argv0"}),
    "exec": ("a", set()),
    "time": ("fo", {"--format", "--output"}),
    "sudo": (
        "ugCDhprtTUacR",
        {
            "--auth-type",
            "--login-class",
            "--chroot",
            "--user",
            "--group",
            "--close-from",
            "--chdir",
            "--host",
            "--prompt",
            "--role",
            "--type",
            "--command-timeout",
            "--other-user",
        },
    ),
}
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


class _Budget:
    """Work limit and memo shared by one judge call.

    The PreToolUse hook does not block when it times out, so a command whose
    judgment would run too long is denied instead.
    """

    def __init__(self) -> None:
        self.steps = 0
        self.deadline = time.monotonic() + _TIME_LIMIT_SEC
        self.wrapped: dict[tuple[str, ...], Verdict] = {}

    def spend(self, steps: int = 1) -> None:
        self.steps += steps
        if self.steps > _STEP_LIMIT:
            raise ValueError("Command is too complex to judge")
        if time.monotonic() > self.deadline:
            raise ValueError("Command took too long to judge")


def _wrapped_commands(
    executable: str, args: list[str], budget: _Budget
) -> list[list[str]]:
    """Consume wrapper options and return every command the wrapper may run.

    An option that is not a known value option may still take a value: a newer
    tool version can add one, and getopt_long accepts unique prefixes of long
    options. Both readings are kept for it, as a flag and as an option that takes
    a value, so a missing table entry never turns a deny into an allow. Each
    reading only moves the start of the next word, so readings are kept as a set
    of start positions and each position is read once.
    """
    commands: list[list[str]] = []
    pending = [0]
    seen: set[int] = set()
    while pending:
        commands.extend(
            _read_wrapper(executable, args, pending.pop(), seen, pending, budget)
        )
    return commands


def _read_wrapper(
    executable: str,
    args: list[str],
    index: int,
    seen: set[int],
    pending: list[int],
    budget: _Budget,
) -> list[list[str]]:
    """Read from one start position, reading unknown options as flags.

    The value readings of unknown options go to ``pending``. A position already
    in ``seen`` was read before, so reading stops there. An abbreviation of env
    --split-string also yields the commands of its split value.
    """
    short_options, long_options = _WRAPPER_VALUE_OPTIONS.get(executable, ("", set()))
    versioned = executable in _VERSIONED_WRAPPERS
    found: list[list[str]] = []
    while index < len(args):
        if index in seen:
            return found
        seen.add(index)
        budget.spend()
        arg = args[index]
        if arg == "--":
            budget.spend(len(args) - index)
            return [*found, args[index + 1 :]]
        if not arg.startswith("-"):
            budget.spend(len(args) - index)
            return [*found, args[index:]]
        index += 1
        value = ""
        split_string = False
        if arg.startswith("--"):
            option, equals, value = arg.partition("=")
            if option not in long_options:
                if not versioned:
                    continue
                if not equals and index < len(args):
                    pending.append(index + 1)
                if (
                    executable == "env"
                    and len(option) > 2
                    and "--split-string".startswith(option)
                    and (equals or index < len(args))
                ):
                    split_value = value if equals else args[index]
                    rest = args[index:] if equals else args[index + 1 :]
                    budget.spend(len(rest))
                    found.extend(
                        _wrapped_commands(
                            executable, shlex.split(split_value) + rest, budget
                        )
                    )
                continue
            if not equals:
                if index == len(args):
                    return found
                value = args[index]
                index += 1
            split_string = executable == "env" and option == "--split-string"
        else:
            if executable == "command" and ("v" in arg or "V" in arg):
                return found
            for offset, option in enumerate(arg[1:], start=2):
                budget.spend()
                if option not in short_options:
                    if not versioned:
                        continue
                    if offset < len(arg):
                        pending.append(index)
                    elif index < len(args):
                        pending.append(index + 1)
                    continue
                value = arg[offset:]
                if not value:
                    if index == len(args):
                        return found
                    value = args[index]
                    index += 1
                split_string = executable == "env" and option == "S"
                break
        if split_string:
            budget.spend(len(args) - index)
            return [
                *found,
                *_wrapped_commands(
                    executable, shlex.split(value) + args[index:], budget
                ),
            ]
    return found


def _judge_mise(args: list[str], *, home: str | None, budget: _Budget) -> Verdict:
    separator = args.index("--") if "--" in args else len(args)
    options = args[:separator]
    if not any(arg in {"exec", "x"} for arg in options):
        return Verdict("allow")

    result = _judge_simple(args[separator + 1 :], home=home, budget=budget)
    if result.decision == "deny":
        return result
    index = 0
    while index < len(options):
        arg = options[index]
        index += 1
        if arg in {"-c", "--command"}:
            if index == len(options):
                continue
            command = options[index]
            index += 1
        elif arg.startswith("--command="):
            command = arg[len("--command=") :]
        elif arg.startswith("-c"):
            command = arg[2:]
        else:
            continue
        verdict = _judge(command, home=home, budget=budget)
        if verdict.decision == "deny":
            return verdict
        if verdict.decision == "ask":
            result = verdict
    return result


def _judge_simple(words: list[str], *, home: str | None, budget: _Budget) -> Verdict:
    budget.spend(len(words) + 1)
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
        for wrapped in _wrapped_commands(executable, args, budget):
            key = tuple(wrapped)
            verdict = budget.wrapped.get(key)
            if verdict is None:
                verdict = _judge_simple(wrapped, home=home, budget=budget)
                budget.wrapped[key] = verdict
            if verdict.decision == "deny":
                return verdict
            if verdict.decision == "ask" and result.decision == "allow":
                result = verdict
        return result
    if executable == "mise":
        return _judge_mise(args, home=home, budget=budget)
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
        if subcommand == "selfcheck" and any(
            arg in {"--apply", "--rollback"}
            or arg.startswith(("--apply=", "--rollback="))
            for arg in args
        ):
            return Verdict("ask", "에이전트 버전을 바꾸려면 사용자 확인이 필요합니다.")
    if executable in {"bash", "sh", "zsh"}:
        for index, arg in enumerate(args[:-1]):
            if arg.startswith("-") and not arg.startswith("--") and "c" in arg:
                return _judge(args[index + 1], home=home, budget=budget)
    return Verdict("allow")


def judge(command: str, *, home: str | None) -> Verdict:
    return _judge(command, home=home, budget=_Budget())


def _judge(command: str, *, home: str | None, budget: _Budget) -> Verdict:
    try:
        if len(command) > _MAX_COMMAND_CHARS:
            raise ValueError("Command is too long to judge")
        budget.spend(len(command) // 64 + 1)
        commands, _ = _commands(command)
        result = Verdict("allow")
        for words in commands:
            verdict = _judge_simple(words, home=home, budget=budget)
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
