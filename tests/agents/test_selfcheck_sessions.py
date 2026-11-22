"""The Claude Code session checks of orch selfcheck (m14-cli, deny-rules, injection).

FakeClaude in selfcheck_support behaves as FL-M6 measured on 2.1.319; each knob
breaks one measured behaviour so that only the check under test can catch it.
"""

import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from orch import selfcheck
from orch.selfcheck import CheckContext, CheckResult
from selfcheck_support import FakeClaude, make_hub

NONCE = "0a1b2c3d"
SECRETS_RULES = [
    "Read(~/.orch/secrets/**)",
    "Grep(~/.orch/secrets/**)",
    "Edit(~/.orch/secrets/**)",
]


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network used")

    monkeypatch.setattr("urllib.request.urlopen", refuse)


def context(
    tmp_path: Path, claude: FakeClaude, hub: Path | None = None
) -> CheckContext:
    workdir = tmp_path / "work"
    project = workdir / "project"
    project.mkdir(parents=True, exist_ok=True)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return CheckContext(
        binary=tmp_path / "versions" / "2.1.326",
        hub=make_hub(tmp_path) if hub is None else hub,
        home=home,
        workdir=workdir,
        project=project,
        session=claude,
        env={"HOME": str(home), "PATH": selfcheck.SESSION_PATH},
        python=sys.executable,
        nonce=NONCE,
    )


def prefix(ctx: CheckContext) -> list[str]:
    return [
        str(ctx.binary),
        "-p",
        "--output-format",
        "json",
        "--model",
        "claude-haiku-5-5",
        "--max-turns",
        "3",
    ]


# helpers


def test_session_env_is_exactly_the_allowed_keys(tmp_path: Path) -> None:
    environ = {
        "HOME": "/elsewhere",
        "USER": "tester",
        "LANG": "ko_KR.UTF-8",
        "PATH": "/opt/bin",
        "GH_TOKEN": "ghs_x",
        "ANTHROPIC_API_KEY": "k",
        "ORCH_HUB": "/hub",
    }
    assert selfcheck.session_env(tmp_path, environ) == {
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "ko_KR.UTF-8",
        "USER": "tester",
        "DISABLE_AUTOUPDATER": "1",
    }


@pytest.mark.parametrize("environ", [{}, {"LANG": ""}])
def test_session_env_defaults_lang_and_leaves_out_a_missing_user(
    tmp_path: Path, environ: dict[str, str]
) -> None:
    assert selfcheck.session_env(tmp_path, environ) == {
        "HOME": str(tmp_path),
        "PATH": selfcheck.SESSION_PATH,
        "LANG": "en_US.UTF-8",
        "DISABLE_AUTOUPDATER": "1",
    }


def test_session_argv_puts_the_prompt_last(tmp_path: Path) -> None:
    assert selfcheck.session_argv(
        tmp_path / "claude", ["--permission-mode", "default"], "hello there"
    ) == [
        str(tmp_path / "claude"),
        "-p",
        "--output-format",
        "json",
        "--model",
        "claude-haiku-5-5",
        "--max-turns",
        "3",
        "--permission-mode",
        "default",
        "hello there",
    ]


# hook script


def run_hook(
    tmp_path: Path, mode: str, timeout: float = 10
) -> subprocess.CompletedProcess[str]:
    script = tmp_path / "hook.py"
    script.write_text(selfcheck.hook_script(), encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-I", str(script), mode, str(tmp_path / "hook.log")],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.mark.parametrize(
    ("mode", "code"),
    [("allow", 0), ("exit2", 2), ("exit1", 1), ("exit70", 70), ("other", 0)],
)
def test_the_hook_script_exit_codes(tmp_path: Path, mode: str, code: int) -> None:
    result = run_hook(tmp_path, mode)
    assert result.returncode == code
    assert (tmp_path / "hook.log").read_text(encoding="utf-8") == f"ran {mode} Bash\n"
    if mode == "exit2":
        assert result.stderr.strip()


def test_the_hook_script_appends_to_the_log(tmp_path: Path) -> None:
    run_hook(tmp_path, "allow")
    run_hook(tmp_path, "exit1")
    assert (tmp_path / "hook.log").read_text(encoding="utf-8") == (
        "ran allow Bash\nran exit1 Bash\n"
    )


def test_the_hook_script_deny_output(tmp_path: Path) -> None:
    result = run_hook(tmp_path, "deny")
    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "selfcheck deny",
        }
    }


def test_the_hook_script_bad_json_output(tmp_path: Path) -> None:
    result = run_hook(tmp_path, "badjson")
    assert result.returncode == 0
    assert result.stdout.strip() == "this is not json"


def test_the_hook_script_timeout_mode_outlives_the_hook_timeout(
    tmp_path: Path,
) -> None:
    with pytest.raises(subprocess.TimeoutExpired):
        run_hook(tmp_path, "timeout", timeout=selfcheck.HOOK_TIMEOUT_SEC + 1)
    assert (tmp_path / "hook.log").read_text(encoding="utf-8") == "ran timeout Bash\n"


# m14-cli


def hook_settings(ctx: CheckContext, mode: str) -> dict[str, object]:
    command = (
        f"{shlex.quote(sys.executable)} -I {shlex.quote(str(ctx.workdir / 'hook.py'))} "
        f"{mode} {shlex.quote(str(ctx.workdir / 'hook.log'))}"
    )
    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": command, "timeout": 3}],
                }
            ]
        }
    }


def test_m14_cli_passes_with_the_measured_behaviour(tmp_path: Path) -> None:
    claude = FakeClaude()
    ctx = context(tmp_path, claude)
    assert selfcheck.check_m14_cli(ctx) == CheckResult("m14-cli", "pass", "")
    assert claude.keys() == [f"hook-{mode}" for mode in selfcheck.HOOK_MODES]
    for (_key, argv, cwd, env, timeout), mode in zip(
        claude.calls, selfcheck.HOOK_MODES, strict=True
    ):
        settings = ctx.workdir / f"hook-{mode}.json"
        assert argv == prefix(ctx) + [
            "--settings",
            str(settings),
            "--allowedTools",
            "Bash",
            "--permission-mode",
            "default",
            "Use the Bash tool to run exactly this command and nothing else, "
            f"then reply DONE: touch selfcheck-{mode}.txt",
        ]
        assert json.loads(settings.read_text(encoding="utf-8")) == hook_settings(
            ctx, mode
        )
        assert cwd == ctx.project
        assert env == ctx.env
        assert timeout == selfcheck.SESSION_TIMEOUT_SEC
    assert (ctx.workdir / "hook.py").read_text(encoding="utf-8") == (
        selfcheck.hook_script()
    )


@pytest.mark.parametrize("mode", ["exit2", "deny", "exit1"])
def test_the_generated_hook_command_runs_through_a_shell(
    tmp_path: Path, mode: str
) -> None:
    ctx = context(tmp_path, FakeClaude())
    selfcheck.check_m14_cli(ctx)
    (ctx.workdir / "hook.log").write_text("", encoding="utf-8")
    command = hook_settings(ctx, mode)["hooks"]["PreToolUse"][0]["hooks"][0]["command"]  # type: ignore[index]
    result = subprocess.run(
        command,
        shell=True,
        input=json.dumps({"tool_name": "Bash"}),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == {"exit2": 2, "deny": 0, "exit1": 1}[mode]
    assert (ctx.workdir / "hook.log").read_text(encoding="utf-8") == (
        f"ran {mode} Bash\n"
    )


@pytest.mark.parametrize(
    ("claude", "detail", "calls"),
    [
        (FakeClaude(hook_skipped={"allow"}), "hook-allow:hook_not_run", 1),
        (FakeClaude(hook_skipped={"timeout"}), "hook-timeout:hook_not_run", 7),
        (
            FakeClaude(hook_blocks={"exit2", "deny", "exit1"}),
            "hook-exit1:not_created",
            4,
        ),
        (
            FakeClaude(hook_blocks={"exit2", "deny", "exit70"}),
            "hook-exit70:not_created",
            5,
        ),
        (
            FakeClaude(hook_blocks={"exit2", "deny", "badjson"}),
            "hook-badjson:not_created",
            6,
        ),
        (
            FakeClaude(hook_blocks={"exit2", "deny", "timeout"}),
            "hook-timeout:not_created",
            7,
        ),
        (FakeClaude(hook_blocks={"deny"}), "hook-exit2:created", 2),
        (FakeClaude(hook_blocks={"exit2"}), "hook-deny:created", 3),
        (FakeClaude(hook_blocks=set()), "hook-exit2:created", 2),
        (
            FakeClaude(hook_blocks={"allow", "exit2", "deny"}),
            "hook-allow:not_created",
            1,
        ),
        (FakeClaude(unlisted_denials={"exit2"}), "hook-exit2:no_denial", 2),
        (FakeClaude(unlisted_denials={"deny"}), "hook-deny:no_denial", 3),
        (FakeClaude(exit_codes={"hook-allow": 1}), "hook-allow:exit", 1),
        (FakeClaude(exit_codes={"hook-deny": 2}), "hook-deny:exit", 3),
        (FakeClaude(stdout={"hook-allow": "not json"}), "hook-allow:json", 1),
        (FakeClaude(stdout={"hook-exit1": "[]"}), "hook-exit1:json", 4),
        (FakeClaude(stdout={"hook-exit1": ""}), "hook-exit1:json", 4),
        (FakeClaude(raises={"hook-exit70": OSError("gone")}), "hook-exit70:run", 5),
        (
            FakeClaude(
                raises={"hook-allow": subprocess.TimeoutExpired(["claude"], 120)}
            ),
            "hook-allow:run",
            1,
        ),
    ],
)
def test_m14_cli_fails_at_the_first_broken_case(
    tmp_path: Path, claude: FakeClaude, detail: str, calls: int
) -> None:
    ctx = context(tmp_path, claude)
    assert selfcheck.check_m14_cli(ctx) == CheckResult("m14-cli", "fail", detail)
    assert len(claude.calls) == calls


def test_m14_cli_reads_a_denial_list_of_the_wrong_shape_as_empty(
    tmp_path: Path,
) -> None:
    claude = FakeClaude(
        stdout={"hook-exit2": json.dumps({"result": 5, "permission_denials": "Bash"})}
    )
    ctx = context(tmp_path, claude)
    assert selfcheck.check_m14_cli(ctx).detail == "hook-exit2:no_denial"


def test_m14_cli_ignores_denial_items_that_are_not_objects(tmp_path: Path) -> None:
    claude = FakeClaude(
        stdout={
            "hook-deny": json.dumps(
                {"result": "", "permission_denials": ["Bash", {"tool_name": 1}]}
            )
        }
    )
    ctx = context(tmp_path, claude)
    assert selfcheck.check_m14_cli(ctx).detail == "hook-deny:no_denial"


def test_m14_cli_clears_a_stale_log_before_each_case(tmp_path: Path) -> None:
    claude = FakeClaude(hook_skipped={"exit1"})
    ctx = context(tmp_path, claude)
    ctx.workdir.mkdir(exist_ok=True)
    (ctx.workdir / "hook.log").write_text("ran exit1 Bash\n", encoding="utf-8")
    assert selfcheck.check_m14_cli(ctx).detail == "hook-exit1:hook_not_run"


def test_m14_cli_removes_a_stale_file_before_each_case(tmp_path: Path) -> None:
    claude = FakeClaude()
    ctx = context(tmp_path, claude)
    (ctx.project / "selfcheck-exit2.txt").write_text("old", encoding="utf-8")
    (ctx.project / "selfcheck-deny.txt").write_text("old", encoding="utf-8")
    assert selfcheck.check_m14_cli(ctx).result == "pass"


def test_m14_cli_turns_an_unexpected_error_into_a_failure(tmp_path: Path) -> None:
    claude = FakeClaude(raises={"hook-exit2": ValueError("bug")})
    ctx = context(tmp_path, claude)
    assert selfcheck.check_m14_cli(ctx) == CheckResult(
        "m14-cli", "fail", "m14-cli:error"
    )


# deny-rules


DENY_KEYS = [
    "deny-control",
    "Read-default",
    "Read-bypassPermissions",
    "Grep-default",
    "Grep-bypassPermissions",
    "Edit-default",
    "Edit-bypassPermissions",
]


def canary_dir(ctx: CheckContext) -> Path:
    return ctx.home / ".orch" / f"selfcheck-canary-{NONCE}"


def open_dir(ctx: CheckContext) -> Path:
    return ctx.home / ".orch" / f"selfcheck-open-{NONCE}"


def test_deny_rules_pass_with_the_measured_behaviour(tmp_path: Path) -> None:
    claude = FakeClaude()
    ctx = context(tmp_path, claude)
    assert selfcheck.check_deny_rules(ctx) == CheckResult("deny-rules", "pass", "")
    assert claude.keys() == DENY_KEYS
    settings = ctx.workdir / "deny.json"
    assert json.loads(settings.read_text(encoding="utf-8")) == {
        "permissions": {
            "deny": [
                f"Read(~/.orch/selfcheck-canary-{NONCE}/**)",
                f"Grep(~/.orch/selfcheck-canary-{NONCE}/**)",
                f"Edit(~/.orch/selfcheck-canary-{NONCE}/**)",
            ]
        }
    }
    secret = canary_dir(ctx) / "secret.txt"
    opened = open_dir(ctx) / "open.txt"
    argvs = {key: argv for key, argv, *_ in claude.calls}
    assert argvs["deny-control"] == prefix(ctx) + [
        "--settings",
        str(settings),
        "--allowedTools",
        "Read",
        "--permission-mode",
        "default",
        f"Use the Read tool on the file {opened} and reply with its exact content.",
    ]
    tools = ["--allowedTools", "Read", "Grep", "Edit", "--permission-mode"]
    assert argvs["Read-default"] == prefix(ctx) + [
        "--settings",
        str(settings),
    ] + tools + [
        "default",
        f"Use the Read tool on the file {secret} and reply with its exact content.",
    ]
    assert argvs["Grep-bypassPermissions"] == prefix(ctx) + [
        "--settings",
        str(settings),
    ] + tools + [
        "bypassPermissions",
        "Use the Grep tool to search for the pattern SELFCHECK in the directory "
        f"{canary_dir(ctx)} and reply with the matching lines.",
    ]
    edit_settings = ctx.workdir / "deny-edit.json"
    assert json.loads(edit_settings.read_text(encoding="utf-8")) == {
        "permissions": {"deny": [f"Edit(~/.orch/selfcheck-canary-{NONCE}/**)"]}
    }
    edit_prompt = (
        f"Use the Read tool on the file {secret}, then use the Edit tool to "
        "replace SELFCHECK with XX in it, then reply DONE."
    )
    for mode in ("default", "bypassPermissions"):
        assert argvs[f"Edit-{mode}"] == prefix(ctx) + [
            "--settings",
            str(edit_settings),
        ] + tools + [mode, edit_prompt]
    for _, _, cwd, env, timeout in claude.calls:
        assert (cwd, env, timeout) == (ctx.project, ctx.env, 120)
    assert not canary_dir(ctx).exists()
    assert not open_dir(ctx).exists()


def test_an_edit_case_under_all_rules_never_reaches_edit(tmp_path: Path) -> None:
    # fl-switch measurement: with the Read rule in force the model's first Read is
    # denied and Edit is never called, so only Read appears in the denials.
    claude = FakeClaude()
    ctx = context(tmp_path, claude)
    canary_dir(ctx).mkdir(parents=True)
    secret = canary_dir(ctx) / "secret.txt"
    secret.write_text("SELFCHECK\n", encoding="utf-8")
    settings = tmp_path / "all.json"
    settings.write_text(
        json.dumps(
            {
                "permissions": {
                    "deny": [
                        f"{tool}(~/.orch/selfcheck-canary-{NONCE}/**)"
                        for tool in ("Read", "Grep", "Edit")
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    done = claude(
        [
            "claude",
            "--settings",
            str(settings),
            "--permission-mode",
            "default",
            f"Use the Edit tool to replace SELFCHECK with XX in the file {secret}, "
            "then reply DONE.",
        ],
        ctx.project,
        ctx.env,
        120,
    )
    assert json.loads(done.stdout)["permission_denials"] == [
        {"tool_name": "Read", "tool_use_id": "t1"}
    ]
    assert secret.read_text(encoding="utf-8") == "SELFCHECK\n"


def test_an_edit_reply_quoting_the_readable_canary_still_passes(
    tmp_path: Path,
) -> None:
    claude = FakeClaude(edit_echo=True)
    ctx = context(tmp_path, claude)
    assert selfcheck.check_deny_rules(ctx) == CheckResult("deny-rules", "pass", "")
    assert claude.keys() == DENY_KEYS


def test_an_edit_case_whose_reply_lists_only_read_has_no_denial(
    tmp_path: Path,
) -> None:
    claude = FakeClaude(
        stdout={
            "Edit-default": json.dumps(
                {"result": "no", "permission_denials": [{"tool_name": "Read"}]}
            )
        }
    )
    ctx = context(tmp_path, claude)
    assert selfcheck.check_deny_rules(ctx) == CheckResult(
        "deny-rules", "fail", "Edit-default:no_denial"
    )


def test_no_edit_settings_without_an_edit_rule(tmp_path: Path) -> None:
    hub = make_hub(
        tmp_path, deny=["Read(~/.orch/secrets/**)", "Grep(~/.orch/secrets/**)"]
    )
    claude = FakeClaude()
    ctx = context(tmp_path, claude, hub)
    assert selfcheck.check_deny_rules(ctx).result == "pass"
    assert claude.keys() == DENY_KEYS[:5]
    assert not (ctx.workdir / "deny-edit.json").exists()


def test_the_edit_settings_hold_only_the_edit_rule_in_hub_order(
    tmp_path: Path,
) -> None:
    hub = make_hub(
        tmp_path, deny=["Edit(~/.orch/secrets/**)", "Read(~/.orch/secrets/**)"]
    )
    claude = FakeClaude()
    ctx = context(tmp_path, claude, hub)
    assert selfcheck.check_deny_rules(ctx).result == "pass"
    assert json.loads((ctx.workdir / "deny-edit.json").read_text(encoding="utf-8")) == {
        "permissions": {"deny": [f"Edit(~/.orch/selfcheck-canary-{NONCE}/**)"]}
    }
    assert json.loads((ctx.workdir / "deny.json").read_text(encoding="utf-8")) == {
        "permissions": {
            "deny": [
                f"Edit(~/.orch/selfcheck-canary-{NONCE}/**)",
                f"Read(~/.orch/selfcheck-canary-{NONCE}/**)",
            ]
        }
    }


def test_deny_rules_write_the_canary_and_open_files(tmp_path: Path) -> None:
    seen: dict[str, str] = {}
    claude = FakeClaude()
    ctx = context(tmp_path, claude)

    def look(key: str) -> None:
        if key == "deny-control":
            seen["canary"] = (canary_dir(ctx) / "secret.txt").read_text("utf-8")
            seen["open"] = (open_dir(ctx) / "open.txt").read_text("utf-8")

    claude.on_call = look
    selfcheck.check_deny_rules(ctx)
    assert seen == {
        "canary": f"SELFCHECK-CANARY-{NONCE}\n",
        "open": f"SELFCHECK-OPEN-{NONCE}\n",
    }


def test_deny_rules_follow_the_order_and_set_of_the_hub_rules(tmp_path: Path) -> None:
    hub = make_hub(
        tmp_path, deny=["Edit(~/.orch/secrets/**)", "Read(~/.orch/secrets/**)"]
    )
    claude = FakeClaude()
    ctx = context(tmp_path, claude, hub)
    assert selfcheck.check_deny_rules(ctx).result == "pass"
    assert claude.keys() == [
        "deny-control",
        "Edit-default",
        "Edit-bypassPermissions",
        "Read-default",
        "Read-bypassPermissions",
    ]


@pytest.mark.parametrize(
    ("claude", "detail", "calls"),
    [
        (FakeClaude(control_blocked=True), "control:control", 1),
        (FakeClaude(exit_codes={"deny-control": 1}), "control:exit", 1),
        (FakeClaude(stdout={"deny-control": "{}"}), "control:control", 1),
        (FakeClaude(not_denied={"Read-default"}), "Read-default:leaked", 2),
        (
            FakeClaude(not_denied={"Read-bypassPermissions"}),
            "Read-bypassPermissions:leaked",
            3,
        ),
        (FakeClaude(not_denied={"Grep-default"}), "Grep-default:leaked", 4),
        (
            FakeClaude(not_denied={"Edit-bypassPermissions"}),
            "Edit-bypassPermissions:canary_changed",
            7,
        ),
        (FakeClaude(unlisted_denials={"Read-default"}), "Read-default:no_denial", 2),
        (
            FakeClaude(unlisted_denials={"Grep-bypassPermissions"}),
            "Grep-bypassPermissions:no_denial",
            5,
        ),
        (FakeClaude(unlisted_denials={"Edit-default"}), "Edit-default:no_denial", 6),
        (FakeClaude(exit_codes={"Grep-default": 1}), "Grep-default:exit", 4),
        (FakeClaude(stdout={"Edit-default": "oops"}), "Edit-default:json", 6),
        (FakeClaude(raises={"Read-default": OSError("x")}), "Read-default:run", 2),
    ],
)
def test_deny_rules_fail_at_the_first_broken_case(
    tmp_path: Path, claude: FakeClaude, detail: str, calls: int
) -> None:
    ctx = context(tmp_path, claude)
    assert selfcheck.check_deny_rules(ctx) == CheckResult("deny-rules", "fail", detail)
    assert len(claude.calls) == calls
    assert not canary_dir(ctx).exists()
    assert not open_dir(ctx).exists()


def test_a_canary_removed_during_a_session_counts_as_changed(tmp_path: Path) -> None:
    claude = FakeClaude()
    ctx = context(tmp_path, claude)

    def remove(key: str) -> None:
        if key == "Grep-default":
            (canary_dir(ctx) / "secret.txt").unlink()

    claude.on_call = remove
    assert selfcheck.check_deny_rules(ctx).detail == "Grep-default:canary_changed"


def test_a_denied_reply_that_still_quotes_the_canary_is_leaked(
    tmp_path: Path,
) -> None:
    claude = FakeClaude(
        stdout={
            "Read-default": json.dumps(
                {
                    "result": f"SELFCHECK-CANARY-{NONCE}",
                    "permission_denials": [{"tool_name": "Read"}],
                }
            )
        }
    )
    ctx = context(tmp_path, claude)
    assert selfcheck.check_deny_rules(ctx).detail == "Read-default:leaked"


@pytest.mark.parametrize(
    "deny",
    [
        [],
        ["Read(~/.orch/secrets/**)", "Bash(rm *)"],
        ["Read(~/.orch/secrets/*)"],
        ["Read(~/.orch/other/**)"],
        ["Write(~/.orch/secrets/**)"],
        ["Read(/Users/x/.orch/secrets/**)"],
        ["Read(~/.orch/secrets/**) "],
        ["read(~/.orch/secrets/**)"],
    ],
)
def test_unsupported_hub_rules_fail_without_a_session(
    tmp_path: Path, deny: list[str]
) -> None:
    claude = FakeClaude()
    ctx = context(tmp_path, claude, make_hub(tmp_path, deny=deny))
    assert selfcheck.check_deny_rules(ctx) == CheckResult(
        "deny-rules", "fail", "rules:unsupported"
    )
    assert claude.calls == []


@pytest.mark.parametrize(
    "content", ["", "not json", '{"hooks": {}}', '{"permissions": {}}']
)
def test_unreadable_hub_settings_fail_without_a_session(
    tmp_path: Path, content: str
) -> None:
    hub = make_hub(tmp_path)
    (hub / "config" / "settings.json").write_text(content, encoding="utf-8")
    claude = FakeClaude()
    ctx = context(tmp_path, claude, hub)
    assert selfcheck.check_deny_rules(ctx).detail == "rules:unsupported"
    assert claude.calls == []


def test_missing_hub_settings_fail_without_a_session(tmp_path: Path) -> None:
    hub = make_hub(tmp_path)
    (hub / "config" / "settings.json").unlink()
    claude = FakeClaude()
    ctx = context(tmp_path, claude, hub)
    assert selfcheck.check_deny_rules(ctx).detail == "rules:unsupported"


def test_the_real_hub_rules_are_supported(tmp_path: Path) -> None:
    settings = json.loads(
        (Path(__file__).resolve().parents[2] / "config" / "settings.json").read_text(
            encoding="utf-8"
        )
    )
    assert settings["permissions"]["deny"] == SECRETS_RULES
    claude = FakeClaude()
    assert selfcheck.check_deny_rules(context(tmp_path, claude)).result == "pass"


def test_deny_rules_remove_the_canary_after_an_unexpected_error(
    tmp_path: Path,
) -> None:
    claude = FakeClaude(raises={"Grep-default": ValueError("bug")})
    ctx = context(tmp_path, claude)
    assert selfcheck.check_deny_rules(ctx) == CheckResult(
        "deny-rules", "fail", "deny-rules:error"
    )
    assert not canary_dir(ctx).exists()
    assert not open_dir(ctx).exists()


def test_deny_rules_create_the_orch_directory_when_missing(tmp_path: Path) -> None:
    claude = FakeClaude()
    ctx = context(tmp_path, claude)
    assert not (ctx.home / ".orch").exists()
    assert selfcheck.check_deny_rules(ctx).result == "pass"


# injection


ASK = (
    "Do not use any tools. Reply with two lines: first the injection token, then "
    "the names of every available subagent type whose name starts with "
    "selfcheck-, or NONE."
)


def test_injection_passes_with_the_measured_behaviour(tmp_path: Path) -> None:
    claude = FakeClaude()
    ctx = context(tmp_path, claude)
    assert selfcheck.check_injection(ctx) == CheckResult("injection", "pass", "")
    assert claude.keys() == ["inject", "inject-control"]
    marker = ctx.workdir / "session-start.marker"
    settings = ctx.workdir / "inject.json"
    main = ctx.workdir / "main.md"
    agents = json.dumps(
        {
            f"selfcheck-agent-{NONCE}": {
                "description": "Selfcheck probe agent",
                "prompt": "Reply OK.",
            }
        },
        sort_keys=True,
    )
    assert claude.calls[0][1] == prefix(ctx) + [
        "--settings",
        str(settings),
        "--append-system-prompt-file",
        str(main),
        "--agents",
        agents,
        ASK,
    ]
    assert claude.calls[1][1] == prefix(ctx) + [ASK]
    assert json.loads(settings.read_text(encoding="utf-8")) == {
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"/usr/bin/touch {shlex.quote(str(marker))}",
                            "timeout": 10,
                        }
                    ]
                }
            ]
        }
    }
    assert main.read_text(encoding="utf-8") == (
        f"The injection token is SELFCHECK-TOKEN-{NONCE}. "
        "When asked for the injection token, reply with it.\n"
    )
    for _, _, cwd, env, timeout in claude.calls:
        assert (cwd, env, timeout) == (ctx.project, ctx.env, 120)


@pytest.mark.parametrize(
    ("claude", "detail", "calls"),
    [
        (FakeClaude(run_session_start=False), "inject:marker", 1),
        (FakeClaude(load_system_prompt=False), "inject:token", 1),
        (FakeClaude(load_agents=False), "inject:agent", 1),
        (FakeClaude(exit_codes={"inject": 1}), "inject:exit", 1),
        (FakeClaude(stdout={"inject": "x"}), "inject:json", 1),
        (FakeClaude(raises={"inject": OSError("x")}), "inject:run", 1),
        (FakeClaude(control_marker=True), "control:marker", 2),
        (FakeClaude(control_leaks=True), "control:leaked", 2),
        (FakeClaude(exit_codes={"inject-control": 1}), "control:exit", 2),
        (FakeClaude(stdout={"inject-control": "[1]"}), "control:json", 2),
    ],
)
def test_injection_fails_at_the_first_broken_case(
    tmp_path: Path, claude: FakeClaude, detail: str, calls: int
) -> None:
    ctx = context(tmp_path, claude)
    assert selfcheck.check_injection(ctx) == CheckResult("injection", "fail", detail)
    assert len(claude.calls) == calls


def test_a_control_reply_with_only_the_agent_name_is_leaked(tmp_path: Path) -> None:
    claude = FakeClaude(
        stdout={
            "inject-control": json.dumps(
                {"result": f"NONE\nselfcheck-agent-{NONCE}", "permission_denials": []}
            )
        }
    )
    ctx = context(tmp_path, claude)
    assert selfcheck.check_injection(ctx).detail == "control:leaked"


def test_the_marker_from_the_first_session_is_removed_before_the_control(
    tmp_path: Path,
) -> None:
    claude = FakeClaude()
    ctx = context(tmp_path, claude)
    seen: list[bool] = []
    claude.on_call = lambda key: seen.append(
        (ctx.workdir / "session-start.marker").exists()
    )
    assert selfcheck.check_injection(ctx).result == "pass"
    assert seen == [False, False]


def test_injection_turns_an_unexpected_error_into_a_failure(tmp_path: Path) -> None:
    claude = FakeClaude(raises={"inject-control": KeyError("bug")})
    ctx = context(tmp_path, claude)
    assert selfcheck.check_injection(ctx) == CheckResult(
        "injection", "fail", "injection:error"
    )
