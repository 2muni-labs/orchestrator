"""GitHub App token issuing and removal of secret values from CLI output."""

import json
import subprocess
from collections.abc import Iterable
from pathlib import Path


def issue_token(orch_home: Path, secrets: list[str]) -> str:
    """Run the token script, add the token to secrets and return it."""
    try:
        result = subprocess.run(
            [str(orch_home / "bin" / "ghapp-token.sh")],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception:
        raise RuntimeError("Token script could not be executed") from None
    token = result.stdout.strip()
    if result.returncode != 0 or not token:
        raise RuntimeError("Token script failed or returned an empty token")
    secrets.append(token)
    return token


def redact_secrets(text: str, secrets: Iterable[str]) -> str:
    """Replace each secret, raw or JSON-escaped, with [REDACTED]."""
    for secret in secrets:
        escaped = json.dumps(secret, ensure_ascii=False)[1:-1]
        text = text.replace(escaped, "[REDACTED]")
        text = text.replace(secret, "[REDACTED]")
    return text
