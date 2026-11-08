"""Tests for scripts/guard_regress.py, using fake transcripts under tmp_path only."""

import importlib.util
import json
import pathlib
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "guard_regress.py"
REAL_HOME = Path.home()
REAL_CLAUDE = REAL_HOME / ".claude"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("guard_regress", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gr = _load()


def _forbid_real_claude(method_name: str) -> Callable[..., object]:
    original = getattr(pathlib.Path, method_name)

    def guarded(self: Path, *args: object, **kwargs: object) -> object:
        resolved = Path(str(self))
        if resolved == REAL_CLAUDE or resolved.is_relative_to(REAL_CLAUDE):
            raise AssertionError(f"test touched the real transcripts: {method_name}")
        return original(self, *args, **kwargs)

    return guarded


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fix HOME, ORCH_HOME and ORCH_HUB under tmp_path; fail on real ~/.claude."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ORCH_HOME", str(home / ".orch"))
    monkeypatch.delenv("ORCH_HUB", raising=False)
    for name in ("open", "glob", "is_dir", "exists", "iterdir", "read_text"):
        monkeypatch.setattr(pathlib.Path, name, _forbid_real_claude(name))
    return home


def _bash(command: str, *, sidechain: bool = False) -> dict:
    return {
        "isSidechain": sidechain,
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": command}}
            ],
        },
    }


def _write(directory: Path, name: str, lines: list[object]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    with path.open("w", encoding="utf-8") as handle:
        for line in lines:
            handle.write((line if isinstance(line, str) else json.dumps(line)) + "\n")
    return path


def _run(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, str, str]:
    code = gr.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_isolation_guard_rejects_real_transcripts(isolated: Path) -> None:
    assert Path.home() == isolated
    with pytest.raises(AssertionError, match="real transcripts"):
        (REAL_CLAUDE / "projects").is_dir()


def test_collect_skips_sidechains_broken_lines_and_other_tools(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "t"
    _write(
        directory,
        "a.jsonl",
        [
            _bash("git status"),
            _bash("codex exec x", sidechain=True),
            "{not json",
            "",
            "[1, 2]",
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "name": "Read", "input": {"command": "x"}},
                        {"type": "text", "text": "codex exec y"},
                    ],
                }
            },
            {"message": {"role": "user", "content": _bash("ls")["message"]["content"]}},
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "name": "Bash", "input": {"command": 3}}
                    ],
                }
            },
            _bash("ls -la"),
        ],
    )
    _write(directory, "notes.txt", ['{"x": 1}'])
    assert gr.collect([directory]) == ["git status", "ls -la"]


def test_collect_reads_every_directory_in_order(tmp_path: Path) -> None:
    _write(tmp_path / "one", "b.jsonl", [_bash("echo 2")])
    _write(tmp_path / "one", "a.jsonl", [_bash("echo 1")])
    _write(tmp_path / "two", "a.jsonl", [_bash("echo 3")])
    assert gr.collect([tmp_path / "one", tmp_path / "two"]) == [
        "echo 1",
        "echo 2",
        "echo 3",
    ]


def test_summary_counts_unique_commands_per_decision_and_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / "t"
    _write(
        directory,
        "a.jsonl",
        [
            _bash("git status"),
            _bash("git status"),
            _bash("codex exec a"),
            _bash("codex exec a"),
            _bash("codex exec b"),
            _bash("git push --force origin x"),
            _bash("orch resume 3"),
            _bash("ls"),
        ],
    )
    code, out, err = _run(capsys, ["--transcripts", str(directory)])
    assert code == 0
    assert err == ""
    lines = out.splitlines()
    assert lines[0] == "commands: 8, unique: 6"
    assert lines[1] == "decisions: allow 2, deny 3, ask 1"
    assert sorted(lines[2:]) == sorted(
        [
            "  ask x1: 작업을 재개하려면 사용자 확인이 필요합니다.",
            "  deny x2: Codex 직접 실행은 차단합니다. orch를 사용하세요.",
            "  deny x1: Git 강제 푸시는 차단합니다.",
        ]
    )


def test_summary_without_show_never_prints_commands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / "t"
    _write(directory, "a.jsonl", [_bash("codex exec marker-xyz"), _bash("echo hi")])
    code, out, _ = _run(capsys, ["--transcripts", str(directory)])
    assert code == 0
    assert "marker-xyz" not in out
    assert "echo hi" not in out
    assert "---" not in out


def test_show_prints_flagged_commands_with_home_replaced(
    tmp_path: Path, isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / "t"
    _write(
        directory,
        "a.jsonl",
        [
            _bash(f"{isolated}/bin/codex exec marker-abc"),
            _bash(f"ls {isolated}/allowed"),
        ],
    )
    code, out, _ = _run(capsys, ["--transcripts", str(directory), "--show"])
    assert code == 0
    assert "--- deny: Codex 직접 실행은 차단합니다. orch를 사용하세요." in out
    assert "~/bin/codex exec marker-abc" in out
    assert str(isolated) not in out
    assert "allowed" not in out


def test_show_truncates_long_commands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / "t"
    command = "codex exec " + "a" * (gr.SHOW_LIMIT + 50)
    _write(directory, "a.jsonl", [_bash(command)])
    code, out, _ = _run(capsys, ["--transcripts", str(directory), "--show"])
    assert code == 0
    shown = out.splitlines()[-1]
    assert shown == command[: gr.SHOW_LIMIT] + "…"


def test_show_withholds_lines_with_secrets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / "t"
    token = "ghp_" + "A1b2C3d4E5" * 4
    _write(directory, "a.jsonl", [_bash(f"GH_TOKEN={token} codex exec x")])
    code, out, _ = _run(capsys, ["--transcripts", str(directory), "--show"])
    assert code == 0
    assert token not in out
    assert out.splitlines()[-1] == "[withheld: secret] executables: codex"
    assert "--- deny: Codex 직접 실행은 차단합니다. orch를 사용하세요." in out


def test_safe_withholds_residual_paths(isolated: Path) -> None:
    assert (
        gr.safe("cat /Users/someone/x", home=isolated, orch_home=isolated / ".orch")
        == "[withheld by redact]"
    )
    assert (
        gr.safe(f"cat {isolated}/x", home=isolated, orch_home=isolated / ".orch")
        == "cat ~/x"
    )


def test_missing_directory_exits_64(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _run(capsys, ["--transcripts", str(tmp_path / "missing")])
    assert code == 64
    assert out == ""
    assert "not found" in err


def test_one_missing_directory_among_several_exits_64(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path / "t", "a.jsonl", [_bash("ls")])
    code, out, _ = _run(
        capsys,
        ["--transcripts", str(tmp_path / "t"), "--transcripts", str(tmp_path / "no")],
    )
    assert code == 64
    assert out == ""


def test_no_commands_exits_64(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / "t"
    _write(directory, "a.jsonl", [_bash("ls", sidechain=True), "broken"])
    code, out, err = _run(capsys, ["--transcripts", str(directory)])
    assert code == 64
    assert out == ""
    assert "no Bash commands" in err


def test_bad_option_exits_64(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, _ = _run(capsys, ["--nope"])
    assert code == 64


def test_transcript_dir_encodes_every_non_alphanumeric_character() -> None:
    assert gr.transcript_dir(
        Path("/Users/a.b/Repositories/my_repo"), Path("/h")
    ) == Path("/h/.claude/projects/-Users-a-b-Repositories-my-repo")
    assert gr.transcript_dir(Path("/x/orch-1"), Path("/h")) == Path(
        "/h/.claude/projects/-x-orch-1"
    )


def test_default_directory_follows_orch_hub(
    tmp_path: Path,
    isolated: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hub = tmp_path / "hub"
    monkeypatch.setenv("ORCH_HUB", str(hub))
    _write(gr.transcript_dir(hub, isolated), "a.jsonl", [_bash("ls")])
    code, out, _ = _run(capsys, [])
    assert code == 0
    assert out.splitlines()[0] == "commands: 1, unique: 1"


def test_default_directory_without_orch_hub_is_the_script_repository(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = _run(capsys, [])
    assert code == 64
    assert "not found" in err
    _write(gr.transcript_dir(gr.REPO, isolated), "a.jsonl", [_bash("ls")])
    code, out, _ = _run(capsys, [])
    assert code == 0
    assert out.splitlines()[0] == "commands: 1, unique: 1"


SESSION = "0123abcd-4567-89ab-cdef-0123456789ab"


@pytest.mark.parametrize("prefix", ["/private/tmp", "/tmp"])
def test_show_replaces_the_session_temporary_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], prefix: str
) -> None:
    directory = tmp_path / "t"
    scratch = f"{prefix}/claude-501/-Users-someone-Repositories-x/{SESSION}"
    _write(directory, "a.jsonl", [_bash(f"codex exec < {scratch}/scratchpad/p.md")])
    code, out, _ = _run(capsys, ["--transcripts", str(directory), "--show"])
    assert code == 0
    assert out.splitlines()[-1] == "codex exec < <session-tmp>/scratchpad/p.md"
    assert "someone" not in out


def test_show_replaces_a_prefix_without_a_session_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / "t"
    command = "codex exec < /private/tmp/claude-501/-Users-someone-x/tasks/b.output"
    _write(directory, "a.jsonl", [_bash(command)])
    code, out, _ = _run(capsys, ["--transcripts", str(directory), "--show"])
    assert code == 0
    assert out.splitlines()[-1] == "codex exec < <session-tmp>/tasks/b.output"


def test_show_does_not_replace_other_temporary_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / "t"
    command = "codex exec < /private/tmp/claude-x/notes.md && /usr/bin/env cat q"
    _write(directory, "a.jsonl", [_bash(command)])
    code, out, _ = _run(capsys, ["--transcripts", str(directory), "--show"])
    assert code == 0
    assert out.splitlines()[-1] == ("[withheld: residual path] executables: codex, env")
    assert "notes" not in out


def test_show_names_executables_of_a_withheld_command_once_by_base_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / "t"
    command = (
        "cat /var/folders/ab/x | /Users/someone/.local/bin/codex exec - ; "
        "A=1 cat y; ./we$ird z"
    )
    _write(directory, "a.jsonl", [_bash(command)])
    code, out, _ = _run(capsys, ["--transcripts", str(directory), "--show"])
    assert code == 0
    assert out.splitlines()[-1] == (
        "[withheld: residual path] executables: cat, codex, ?"
    )
    assert "someone" not in out and "folders" not in out


def test_show_marks_an_unparsable_withheld_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / "t"
    _write(directory, "a.jsonl", [_bash("cat '/var/folders/x")])
    code, out, _ = _run(capsys, ["--transcripts", str(directory), "--show"])
    assert code == 0
    assert out.splitlines()[-1] == (
        "[withheld: residual path] executables: (unparsable)"
    )


def test_show_redacts_before_truncating(
    tmp_path: Path, isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / "t"
    # Cut before redact, the line would end inside the home path and be withheld.
    padding = "a" * (gr.SHOW_LIMIT - 40)
    command = f"codex exec {padding} {isolated}/tail"
    assert len(command) > gr.SHOW_LIMIT
    _write(directory, "a.jsonl", [_bash(command)])
    code, out, _ = _run(capsys, ["--transcripts", str(directory), "--show"])
    assert code == 0
    assert out.splitlines()[-1] == f"codex exec {padding} ~/tail"
