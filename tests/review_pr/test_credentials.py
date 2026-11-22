import json
import stat
from pathlib import Path

import pytest

from orch import cli
from orch.credentials import issue_token, redact_secrets

TOKEN = "gh" + "s_" + "t0ken" * 8


def token_script(orch_home: Path, body: str) -> Path:
    script = orch_home / "bin" / "ghapp-token.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def test_issue_token_returns_the_stripped_token_and_records_it(tmp_path: Path) -> None:
    token_script(tmp_path, f"printf '  %s\\n\\n' '{TOKEN}'\n")
    secrets = ["earlier"]
    assert issue_token(tmp_path, secrets) == TOKEN
    assert secrets == ["earlier", TOKEN]


@pytest.mark.parametrize(
    "body",
    [
        "echo printed-by-a-failing-script\nexit 1\n",
        "exit 0\n",
        "printf '   \\n'\nexit 0\n",
    ],
)
def test_a_failing_or_empty_script_raises_without_its_output(
    tmp_path: Path, body: str
) -> None:
    token_script(tmp_path, body + "echo stderr-secret >&2\n")
    secrets: list[str] = []
    with pytest.raises(RuntimeError) as caught:
        issue_token(tmp_path, secrets)
    assert str(caught.value) == "Token script failed or returned an empty token"
    assert secrets == []


def test_a_missing_script_raises_without_a_cause(tmp_path: Path) -> None:
    secrets: list[str] = []
    with pytest.raises(RuntimeError) as caught:
        issue_token(tmp_path, secrets)
    assert str(caught.value) == "Token script could not be executed"
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert secrets == []


def test_redact_secrets_replaces_raw_and_json_escaped_forms() -> None:
    secret = 'a"b\\c'
    text = f"raw {secret} json {json.dumps(secret)[1:-1]} end"
    assert redact_secrets(text, [secret]) == "raw [REDACTED] json [REDACTED] end"


def test_redact_secrets_handles_several_values_and_any_iterable() -> None:
    text = "one TOPICVALUE two " + TOKEN
    assert (
        redact_secrets(text, iter([TOKEN, "TOPICVALUE"]))
        == "one [REDACTED] two [REDACTED]"
    )


def test_redact_secrets_without_secrets_returns_the_text() -> None:
    assert redact_secrets("nothing here", []) == "nothing here"


def test_cli_no_longer_defines_its_own_token_helpers() -> None:
    assert not hasattr(cli, "_token")
    assert not hasattr(cli, "_redact")
