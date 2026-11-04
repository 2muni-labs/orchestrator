"""Replace personal paths and block secrets in text that leaves this machine."""

from collections.abc import Iterable
from pathlib import Path


class RedactionError(Exception):
    """Base class for text that must not leave this machine."""


class SecretFoundError(RedactionError):
    """The text contains a secret. Secrets are never replaced."""


class ResidualPathError(RedactionError):
    """A personal path pattern remains after replacement."""


def redact(
    text: str, *, home: Path, orch_home: Path, secrets: Iterable[str] = ()
) -> str:
    """Return text with known paths replaced, or raise if it must not be sent."""
    raise NotImplementedError
