"""GitHub App token issuing and removal of secret values from CLI output."""

from collections.abc import Iterable
from pathlib import Path


def issue_token(orch_home: Path, secrets: list[str]) -> str:
    """Run the token script, add the token to secrets and return it."""
    raise NotImplementedError


def redact_secrets(text: str, secrets: Iterable[str]) -> str:
    """Replace each secret, raw or JSON-escaped, with [REDACTED]."""
    raise NotImplementedError
