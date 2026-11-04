"""Replace personal paths and block secrets in text that leaves this machine."""

import re
from collections.abc import Iterable
from pathlib import Path

_START = r"(?<![A-Za-z0-9_.\-/~$])"
_END = r"(?![A-Za-z0-9_\-]|\.[A-Za-z0-9_\-])"
_SECRET_PATTERNS = (
    ("GitHub token", re.compile(r"(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9_.\-]{36,}")),
    (
        "GitHub personal access token",
        re.compile(r"(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_.\-]{22,}"),
    ),
    (
        "JWT",
        re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]*"),
    ),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)
_RESIDUAL_PATHS = (
    "/Users/",
    "/home/",
    "C:\\Users\\",
    "/private/tmp/",
    "/tmp/claude-",
    "/var/folders/",
)


class RedactionError(Exception):
    """Base class for text that must not leave this machine."""


class SecretFoundError(RedactionError):
    """The text contains a secret. Secrets are never replaced."""


class ResidualPathError(RedactionError):
    """A personal path pattern remains after replacement."""


def _replace_worktree(match: re.Match[str]) -> str:
    return "" if match.group("file_separator") else "."


def redact(
    text: str, *, home: Path, orch_home: Path, secrets: Iterable[str] = ()
) -> str:
    """Return text with known paths replaced, or raise if it must not be sent."""
    if isinstance(secrets, (str, bytes)) or secrets is None:
        raise TypeError("secrets must be an iterable of strings")
    secret_values = list(secrets)
    for secret in secret_values:
        if not isinstance(secret, str):
            raise TypeError("secrets must contain only strings")
        if not secret:
            raise ValueError("secrets must not contain empty strings")
    for name, path in (("home", home), ("orch_home", orch_home)):
        if not path.is_absolute() or path == Path("/"):
            raise ValueError(f"{name} must be absolute and not the root")

    for kind, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            raise SecretFoundError(kind)
    for secret in secret_values:
        if secret in text:
            raise SecretFoundError("configured secret")

    worktree_pattern = (
        _START
        + re.escape(str(orch_home))
        + r"/[A-Za-z0-9._-]+/worktrees/[A-Za-z0-9._-]*[A-Za-z0-9_-]"
        + _END
        + r"(?:(?P<file_separator>/(?=[A-Za-z0-9._-]))|/)?"
    )
    text = re.sub(worktree_pattern, _replace_worktree, text)
    if not orch_home.is_relative_to(home):
        text = re.sub(_START + re.escape(str(orch_home)) + _END, "$ORCH_HOME", text)
    text = re.sub(_START + re.escape(str(home)) + _END, "~", text)

    for blocked_path in _RESIDUAL_PATHS:
        if blocked_path in text:
            raise ResidualPathError(blocked_path)
    if re.search(r"/root" + _END, text):
        raise ResidualPathError("/root")
    if str(home).replace("/", "-") in text:
        raise ResidualPathError("encoded home path")
    return text
