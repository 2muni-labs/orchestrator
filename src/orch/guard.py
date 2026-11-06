"""PreToolUse(Bash) guard. Reads only its input and HOME, never orch settings."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Verdict:
    decision: Literal["allow", "deny", "ask"]
    reason: str = ""


def judge(command: str, *, home: str | None) -> Verdict:
    raise NotImplementedError


def handle(stdin_text: str, environ: Mapping[str, str]) -> str:
    raise NotImplementedError
