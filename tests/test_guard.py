import ast
import json
from pathlib import Path

import pytest

from orch import guard
from orch.guard import Verdict, handle, judge

HOME = "/Users/someone"

DENY = [
    # codex, any form
    "codex exec x",
    "/opt/bin/codex --version",
    "FOO=1 codex exec",
    "env -i FOO=1 codex exec",
    "command codex",
    "exec codex",
    "nohup codex exec &",
    "time codex exec",
    "sudo codex",
    "cd /tmp && codex exec",
    "true; codex exec",
    "false || codex exec",
    "echo hi | codex exec -",
    "(codex exec)",
    "echo ok\ncodex exec",
    'echo "$(codex exec)"',
    "echo `codex exec`",
    "bash -c 'codex exec'",
    'sh -lc "codex exec"',
    "zsh -ec 'cd x && codex'",
    # claude print mode
    "claude -p hello",
    "claude --print hello",
    "/Users/someone/.local/bin/claude --model m -p x",
    # force pushes
    "git push --force origin task/x",
    "git push -f origin task/x",
    "git push -uf origin task/x",
    "git push -fu origin task/x",
    "git push --force-with-lease origin task/x",
    "git push --force-with-lease=task/x:abc origin task/x",
    "git push --force-if-includes origin task/x",
    "git push origin +task/x",
    "git push origin +HEAD:main",
    "git -C /repo push --force origin x",
    "git -c core.x=y push -f origin x",
    "git --no-pager push --force origin x",
    # merges
    "gh pr merge 5",
    "GH_TOKEN=$(~/.orch/bin/ghapp-token.sh) gh pr merge 5 --merge",
    "gh pr merge --squash 5",
    "gh api repos/o/r/pulls/5/merge -X PUT",
    "gh api /repos/o/r/pulls/5/merge -X PUT",
    "gh api -X PUT repos/o/r/pulls/12/merge",
    "gh api --method PUT 'repos/{owner}/{repo}/pulls/5/merge'",
    "gh api https://api.github.com/repos/o/r/pulls/5/merge",
    "gh api repos/o/r/pulls/5/merge?x=1",
    # secrets
    "cat ~/.orch/secrets/key.pem",
    "cat ~/.orch/secrets",
    "ls $HOME/.orch/secrets/",
    'cat "${HOME}/.orch/secrets/app.pem"',
    "cat /Users/someone/.orch/secrets/app.pem",
    "cp x ~/.orch/secrets/y",
    "openssl x -in ~/.orch/secrets/a.pem",
    # options with values before the rule (position independent)
    "gh -R o/r pr merge 3",
    "gh --repo o/r pr merge 3",
    "gh pr -R o/r merge 3",
    "gh -R o/r api repos/o/r/pulls/3/merge -X PUT",
    "gh --hostname h api repos/o/r/pulls/3/merge",
    "git --git-dir x push -f origin y",
    "git --work-tree w --git-dir x push --force origin y",
    "git push origin y --force",
    "git push origin y -f",
    "git -C /repo push origin +y",
    "env -u X codex exec",
    "sudo -u bob codex",
    "time -p codex exec",
    # substitutions inside unquoted heredoc bodies
    "cat <<EOF\n$(codex exec)\nEOF",
    "cat <<EOF\nx `codex exec` y\nEOF",
    "cat <<-EOF\n\t$(git push -f origin x)\n\tEOF",
    "cat <<EOF\nit's $(gh pr merge 3)\nEOF",
    "cat <<EOF\n$(codex exec\nEOF",
    # unquoted delimiters inside a quoted substitution: the body runs $(...)
    'git commit -m "$(cat <<EOF\nmsg $(codex exec)\nEOF\n)"',
    'git commit -m "$(cat <<EOF\nmsg `codex exec`\nEOF\n)"',
    'gh pr create --body "$(cat <<EOF\n$(gh pr merge 3)\nEOF\n)"',
    # commands after a heredoc inside a substitution are still judged
    "git commit -m \"$(cat <<'EOF'\nmsg\nEOF\n)\"; codex exec",
    # a heredoc operator inside quotes is not a heredoc
    'echo "a <<EOF b"\ncodex exec',
    "echo 'a <<EOF b'\ncodex exec",
    # substitutions outside single quotes still run
    'echo "$(codex exec y)"',
    'echo "`codex exec`"',
    "echo 'a' \"$(codex exec)\"",
    "echo 'x'$(codex exec)'y'",
    'echo "it\'s $(codex exec)"',
    "cat <<EOF\n'$(codex exec)'\nEOF",
    # wrappers with their own options
    "nohup -- codex exec",
    "command -p codex",
    "exec -a name codex",
    # unparsable
    "echo 'unterminated",
    'echo "unterminated',
]

ALLOW = [
    "ls -la",
    "grep -rn codex src",
    'echo "gh pr merge"',
    "echo codex",
    "git push -u origin task/x",
    "git push origin task/x",
    "git -C /repo status",
    "git log --format=%H -1",
    "orch run x",
    "orch status",
    "orch dispatch m13-x",
    "claude --version",
    "claude",
    "gh pr create --title x --body-file -",
    "gh pr view 5",
    "gh api repos/o/r/pulls/5",
    "gh api repos/o/r/pulls/5/reviews",
    "gh api repos/o/r/pulls/5/merged",
    "cat ~/.orch/secretsx",
    "ls ~/.orch/orchestrator/tasks",
    "cat ~/.orch/config",
    "git commit -m 'codex; gh pr merge'",
    "python -c 'print(1)'",
    "",
    "GH_TOKEN=$(~/.orch/bin/ghapp-token.sh) gh pr create --title x --body-file -",
    "GH_TOKEN=$(~/.orch/bin/ghapp-token.sh) gh api repos/o/r/pulls/5",
    (
        "git commit -q -F - <<'EOF'\nDon't run codex exec here\n"
        "codex exec\ngh pr merge 5\nEOF"
    ),
    (
        "gh pr create --body-file - <<EOF\n"
        "main's rule: git push --force is banned\nEOF\necho done"
    ),
    'cat <<-"END"\n\tit\'s fine\n\tEND\nls',
    "cat <<< 'codex'",
    "cat <<'EOF'\n$(codex exec)\nEOF",
    'cat <<"EOF"\n`codex exec`\nEOF',
    "cat <<EOF\nrun codex exec, then gh pr merge 3\nEOF",
    "cat <<EOF\nit's $(date) now\nEOF",
    "gh pr view 3 -R o/r",
    "gh -R o/r pr list",
    "gh -R o/r issue view 3",
    "git -C /repo log --format=%H",
    "git --git-dir x push origin y",
    "env -u X ls",
    "sudo -u bob ls",
    # the commit and PR body forms the main agent uses every day
    "git commit -m \"$(cat <<'EOF'\nIt's a message (#1)\nEOF\n)\"",
    (
        "git commit -m \"$(cat <<'EOF'\nFix x\n\ncodex is mentioned\n"
        'gh pr merge 3\ngit push --force\nEOF\n)"'
    ),
    (
        "gh pr create --title t --body \"$(cat <<'EOF'\n## Summary\n"
        "- main's rule (#75)\nEOF\n)\""
    ),
    'git commit -m "$(cat <<EOF\nIt\'s $(date) (#1)\nEOF\n)"',
    # a quoted delimiter makes the body plain text, even $(...) and backticks
    "cat <<'EOF'\n`codex exec`\nit's (x\nEOF",
    'cat <<"EOF"\n$(gh pr merge 3)\nEOF',
    # inside single quotes, $( and backticks are plain text
    "echo 'it`s'",
    "grep 'x$(codex exec y)' f",
    "grep -v 'a\\|(`/Users' f",
    "grep -nE 'git diff|`orch prepare <|results/<id>' docs/x.md",
    "echo '$('",
    "echo 'a`codex exec`b'",
    "echo \"$(echo 'it`s')\"",
    "cat <<EOF\nit's `date`\nEOF",
]


@pytest.mark.parametrize("command", DENY)
def test_deny(command: str) -> None:
    verdict = judge(command, home=HOME)
    assert verdict.decision == "deny", command
    assert verdict.reason


@pytest.mark.parametrize("command", ALLOW)
def test_allow(command: str) -> None:
    assert judge(command, home=HOME) == Verdict("allow"), command


@pytest.mark.parametrize(
    "command",
    [
        "orch resume m13-x",
        "orch resume m13-x --keep",
        "cd /x && orch resume a",
        # ask passes through wrappers
        "env orch resume 3",
        "env FOO=1 orch resume 3",
        "exec orch resume 3",
        "nohup orch resume 3",
        "command orch resume 3",
        "env orch resume 3; ls",
    ],
)
def test_ask_for_resume(command: str) -> None:
    verdict = judge(command, home=HOME)
    assert verdict.decision == "ask"
    assert verdict.reason


@pytest.mark.parametrize(
    "command",
    [
        "cat <<'EOF'\nit's text\nEOF\ncodex exec",
        "cat <<EOF\nx\nEOF\ngit push -f origin x",
        "cat <<EOF; codex exec\nbody\nEOF",
    ],
)
def test_commands_after_a_heredoc_are_judged(command: str) -> None:
    assert judge(command, home=HOME).decision == "deny"


@pytest.mark.parametrize(
    "command",
    [
        "orch resume a; codex exec",
        "env orch resume 3 && nohup codex exec",
        "command orch resume 3 | exec codex",
    ],
)
def test_deny_wins_over_ask(command: str) -> None:
    assert judge(command, home=HOME).decision == "deny"


def test_home_form_needs_home() -> None:
    assert judge("cat /Users/someone/.orch/secrets/a", home=None).decision == "allow"
    assert judge("cat ~/.orch/secrets/a", home=None).decision == "deny"


# handle


def payload(command: object, tool: str = "Bash") -> str:
    return json.dumps({"tool_name": tool, "tool_input": {"command": command}})


def output(text: str) -> dict[str, object]:
    value = json.loads(text)
    inner = value["hookSpecificOutput"]
    assert inner["hookEventName"] == "PreToolUse"
    assert inner["permissionDecisionReason"]
    return inner


def test_handle_allows_with_no_output() -> None:
    assert handle(payload("ls"), {"HOME": HOME}) == ""


def test_handle_denies() -> None:
    text = handle(payload("codex exec"), {"HOME": HOME})
    assert "\n" not in text.strip()
    assert output(text)["permissionDecision"] == "deny"


def test_handle_asks() -> None:
    assert (
        output(handle(payload("orch resume a"), {"HOME": HOME}))["permissionDecision"]
        == "ask"
    )


@pytest.mark.parametrize("command", ALLOW)
def test_handle_passes_allowed_commands_without_output(command: str) -> None:
    assert handle(payload(command), {"HOME": HOME}) == ""


@pytest.mark.parametrize("command", DENY)
def test_handle_denies_every_deny_case_and_never_allows(command: str) -> None:
    text = handle(payload(command), {"HOME": HOME})
    assert output(text)["permissionDecision"] == "deny"
    assert '"allow"' not in text


def test_handle_passes_home_from_environ() -> None:
    text = handle(payload("cat /Users/someone/.orch/secrets/a"), {"HOME": HOME})
    assert output(text)["permissionDecision"] == "deny"


def test_handle_ignores_other_tools() -> None:
    assert handle(payload("codex", tool="Read"), {"HOME": HOME}) == ""


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        "[]",
        '"Bash"',
        json.dumps({"tool_name": "Bash"}),
        json.dumps({"tool_name": "Bash", "tool_input": "ls"}),
        json.dumps({"tool_name": "Bash", "tool_input": {"command": 5}}),
        json.dumps({"tool_name": "Bash", "tool_input": {}}),
    ],
)
def test_handle_fails_closed_on_bad_input(text: str) -> None:
    assert output(handle(text, {"HOME": HOME}))["permissionDecision"] == "deny"


def test_handle_fails_closed_on_internal_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(command: str, *, home: str | None) -> Verdict:
        raise RuntimeError("boom")

    monkeypatch.setattr(guard, "judge", broken)
    assert output(handle(payload("ls"), {"HOME": HOME}))["permissionDecision"] == "deny"


def test_handle_works_without_home() -> None:
    assert handle(payload("ls"), {}) == ""


def test_handle_ignores_a_broken_orch_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / ".orch").mkdir(parents=True)
    (home / ".orch" / "config").write_text("not = [valid")
    (home / ".orch" / "state.db").write_text("garbage")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ORCH_HOME", str(home / ".orch"))
    monkeypatch.setenv("ORCH_HUB", str(tmp_path / "missing-hub"))
    environ = {"HOME": str(home), "ORCH_HOME": str(home / ".orch")}
    assert handle(payload("ls"), environ) == ""
    assert output(handle(payload("codex"), environ))["permissionDecision"] == "deny"


def test_guard_imports_no_orch_settings_or_state() -> None:
    source = Path(guard.__file__).read_text()
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    forbidden = {"sqlite3", "tomllib", "orch"}
    assert not {name for name in names if name.split(".")[0] in forbidden}
