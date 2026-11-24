"""The Codex sandbox profile text and argv (m-codex-profile, ADR 0028).

The real sandbox-exec behaviour of this profile is in test_sandbox_real.py, which
skips inside a sandbox.
"""

import re
from pathlib import Path

import pytest

from orch import sandbox
from orch.sandbox import (
    CODEX_DENY_FILES,
    CODEX_SQLITE_PATTERN,
    CODEX_TLS_SERVICES,
    CODEX_WRITE_DIRS,
    CODEX_WRITE_FILES,
    PROFILE,
    codex_profile,
    wrap_codex,
)

PORT = 41000
HOME_CODEX_DENY = '  (subpath (string-append (param "HOME") "/.codex"))\n'
WORKTREE_ALLOW = '  (subpath (param "WORKTREE"))\n'
TMPDIR_ALLOW = '  (subpath (param "TMPDIR"))\n'
NETWORK_DENY = "(deny network*)"
GATE_PROFILE = PROFILE


def expected(port: int, codex_home: Path, output: Path | None) -> str:
    """The ADR 0028 transformation, restated independently of the implementation."""
    ch = str(codex_home.resolve())
    text = PROFILE.replace(HOME_CODEX_DENY, "", 1)
    text = text.replace(
        NETWORK_DENY,
        f'{NETWORK_DENY}\n(allow network-outbound (remote ip "localhost:{port}"))',
        1,
    )
    if output is not None:
        text = text.replace(WORKTREE_ALLOW, "", 1)
    lines = [f'  (regex #"^{re.escape(ch)}/{CODEX_SQLITE_PATTERN}$")\n']
    lines += [f'  (subpath "{ch}/{name}")\n' for name in CODEX_WRITE_DIRS]
    lines += [f'  (literal "{ch}/{name}")\n' for name in CODEX_WRITE_FILES]
    if output is not None:
        lines.append(f'  (literal "{output.resolve()}")\n')
    text = text.replace(TMPDIR_ALLOW, TMPDIR_ALLOW + "".join(lines), 1)
    denies = " ".join(f'(literal "{ch}/{name}")' for name in CODEX_DENY_FILES)
    names = " ".join(f'(global-name "{name}")' for name in CODEX_TLS_SERVICES)
    return (
        text
        + f"\n(deny file-write* {denies})\n"
        + f"(allow mach-lookup {names})\n"
        + '(allow file-read* (subpath "/Library/Keychains"))\n'
    )


@pytest.fixture
def codex_home(tmp_path: Path) -> Path:
    path = tmp_path / "home" / ".codex"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def output(tmp_path: Path) -> Path:
    path = tmp_path / "out" / "last.txt"
    path.parent.mkdir()
    return path


# The gate profile the transformation relies on


@pytest.mark.parametrize(
    "piece", [HOME_CODEX_DENY, WORKTREE_ALLOW, TMPDIR_ALLOW, NETWORK_DENY]
)
def test_each_replaced_piece_occurs_once_in_the_gate_profile(piece: str) -> None:
    assert PROFILE.count(piece) == 1


def test_the_gate_profile_ends_with_a_newline() -> None:
    assert PROFILE.endswith("\n")


def test_the_measured_allowances() -> None:
    assert CODEX_TLS_SERVICES == (
        "com.apple.trustd.agent",
        "com.apple.trustd",
        "com.apple.SecurityServer",
        "com.apple.securityd",
        "com.apple.securityd.xpc",
    )
    assert CODEX_WRITE_DIRS == (
        "sessions",
        "tmp",
        "cache",
        "thread-writer-locks",
        "shell_snapshots",
    )
    assert CODEX_WRITE_FILES == (
        "models_cache.json",
        ".sqlite-maintenance.lock",
        "installation_id",
    )
    assert CODEX_DENY_FILES == ("config.toml", "auth.json", "hooks.json")
    assert CODEX_SQLITE_PATTERN == r"[a-z_]+_[0-9]+\.sqlite(-wal|-shm)?"


@pytest.mark.parametrize(
    "name",
    ["logs_2.sqlite", "state_5.sqlite-wal", "thread_history_1.sqlite-shm"],
)
def test_the_sqlite_pattern_matches_the_measured_files(name: str) -> None:
    assert re.fullmatch(CODEX_SQLITE_PATTERN, name)


@pytest.mark.parametrize(
    "name",
    [
        "logs.sqlite",
        "Logs_2.sqlite",
        "logs_2.sqlite-journal",
        "config.toml",
        "a/b_1.sqlite",
    ],
)
def test_the_sqlite_pattern_refuses_other_names(name: str) -> None:
    assert re.fullmatch(CODEX_SQLITE_PATTERN, name) is None


# The profile text


def test_the_implementation_profile_is_exact(codex_home: Path) -> None:
    assert codex_profile(port=PORT, codex_home=codex_home) == expected(
        PORT, codex_home, None
    )


def test_the_review_profile_is_exact(codex_home: Path, output: Path) -> None:
    assert codex_profile(port=PORT, codex_home=codex_home, output=output) == expected(
        PORT, codex_home, output
    )


def test_the_implementation_profile_keeps_the_worktree_and_tmpdir(
    codex_home: Path,
) -> None:
    text = codex_profile(port=PORT, codex_home=codex_home)
    assert WORKTREE_ALLOW in text
    assert TMPDIR_ALLOW in text


def test_the_review_profile_drops_only_the_worktree_write(
    codex_home: Path, output: Path
) -> None:
    text = codex_profile(port=PORT, codex_home=codex_home, output=output)
    assert WORKTREE_ALLOW not in text
    assert TMPDIR_ALLOW in text
    assert f'  (literal "{output.resolve()}")\n' in text
    # .git and .venv write denials name WORKTREE and stay.
    assert '(literal (string-append (param "WORKTREE") "/.git"))' in text


def test_the_profile_opens_only_the_proxy_port(codex_home: Path) -> None:
    text = codex_profile(port=PORT, codex_home=codex_home)
    assert (
        '(deny network*)\n(allow network-outbound (remote ip "localhost:41000"))\n'
        in text
    )
    assert text.count("allow network") == 1
    assert "network-inbound" not in text
    assert "network-bind" not in text


def test_the_profile_lets_codex_read_its_home(codex_home: Path) -> None:
    text = codex_profile(port=PORT, codex_home=codex_home)
    assert HOME_CODEX_DENY not in text
    assert '"/.codex"' not in text


@pytest.mark.parametrize(
    "line",
    [
        '  (subpath (string-append (param "HOME") "/.orch/secrets"))\n',
        '  (literal (string-append (param "HOME") "/.orch/config"))\n',
        '  (subpath (string-append (param "HOME") "/.ssh"))\n',
        '  (subpath (string-append (param "HOME") "/.claude"))\n',
        '  (subpath (string-append (param "HOME") "/Library/Keychains"))\n',
        '  (global-name "com.apple.security.agent")',
        "(deny appleevent-send)\n",
        '  (literal "/bin/launchctl"))\n',
    ],
)
def test_the_profile_keeps_the_gate_denials(codex_home: Path, line: str) -> None:
    for output in (None, codex_home.parent / "last.txt"):
        assert line in codex_profile(port=PORT, codex_home=codex_home, output=output)


def test_the_allowances_come_after_the_gate_denials(codex_home: Path) -> None:
    text = codex_profile(port=PORT, codex_home=codex_home)
    assert text.startswith(PROFILE[: PROFILE.index(NETWORK_DENY)])
    tail = text.split('(literal "/bin/launchctl"))\n', 1)[1]
    ch = str(codex_home.resolve())
    assert tail == (
        f'\n(deny file-write* (literal "{ch}/config.toml") (literal "{ch}/auth.json")'
        f' (literal "{ch}/hooks.json"))\n'
        '(allow mach-lookup (global-name "com.apple.trustd.agent")'
        ' (global-name "com.apple.trustd") (global-name "com.apple.SecurityServer")'
        ' (global-name "com.apple.securityd")'
        ' (global-name "com.apple.securityd.xpc"))\n'
        '(allow file-read* (subpath "/Library/Keychains"))\n'
    )
    assert "com.apple.security.agent" not in tail


def test_the_codex_writes_follow_the_tmpdir_line(codex_home: Path) -> None:
    ch = str(codex_home.resolve())
    text = codex_profile(port=PORT, codex_home=codex_home)
    block = text.split(TMPDIR_ALLOW, 1)[1].split("\n")
    assert block[:9] == [
        f'  (regex #"^{re.escape(ch)}/[a-z_]+_[0-9]+\\.sqlite(-wal|-shm)?$")',
        f'  (subpath "{ch}/sessions")',
        f'  (subpath "{ch}/tmp")',
        f'  (subpath "{ch}/cache")',
        f'  (subpath "{ch}/thread-writer-locks")',
        f'  (subpath "{ch}/shell_snapshots")',
        f'  (literal "{ch}/models_cache.json")',
        f'  (literal "{ch}/.sqlite-maintenance.lock")',
        f'  (literal "{ch}/installation_id")',
    ]
    assert block[9] == '  (literal "/dev/null")'


def test_the_codex_home_is_resolved(tmp_path: Path, codex_home: Path) -> None:
    link = tmp_path / "link-codex"
    link.symlink_to(codex_home)
    text = codex_profile(port=PORT, codex_home=link)
    assert text == expected(PORT, codex_home, None)
    assert str(link) not in text


def test_a_relative_codex_home_is_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, codex_home: Path
) -> None:
    monkeypatch.chdir(codex_home.parent)
    text = codex_profile(port=PORT, codex_home=Path(".codex"))
    assert text == expected(PORT, codex_home, None)


def test_the_output_is_resolved(tmp_path: Path, codex_home: Path, output: Path) -> None:
    link = tmp_path / "link-out"
    link.symlink_to(output.parent)
    text = codex_profile(port=PORT, codex_home=codex_home, output=link / output.name)
    assert text == expected(PORT, codex_home, output)


def test_the_gate_profile_is_not_changed(codex_home: Path, output: Path) -> None:
    codex_profile(port=PORT, codex_home=codex_home, output=output)
    assert sandbox.PROFILE == GATE_PROFILE
    assert "localhost" not in sandbox.PROFILE


# Input checks


@pytest.mark.parametrize("port", [0, 65536, -1, True, "41000", 41000.0])
def test_a_bad_port_is_refused(codex_home: Path, port: object) -> None:
    with pytest.raises(ValueError):
        codex_profile(port=port, codex_home=codex_home)  # type: ignore[arg-type]


@pytest.mark.parametrize("port", [1, 65535])
def test_the_port_bounds_are_accepted(codex_home: Path, port: int) -> None:
    assert f'"localhost:{port}"' in codex_profile(port=port, codex_home=codex_home)


@pytest.mark.parametrize("name", ['a"b', "a\\b", "a\nb", "a\tb", "a\x7fb", "a\x01b"])
def test_a_codex_home_that_breaks_the_profile_is_refused(
    tmp_path: Path, name: str
) -> None:
    with pytest.raises(ValueError):
        codex_profile(port=PORT, codex_home=tmp_path / name)


@pytest.mark.parametrize("name", ['a"b', "a\\b", "a\nb", "a\x7fb"])
def test_an_output_that_breaks_the_profile_is_refused(
    tmp_path: Path, codex_home: Path, name: str
) -> None:
    with pytest.raises(ValueError):
        codex_profile(port=PORT, codex_home=codex_home, output=tmp_path / name)


def test_an_output_that_is_a_directory_is_refused(
    codex_home: Path, output: Path
) -> None:
    with pytest.raises(ValueError):
        codex_profile(port=PORT, codex_home=codex_home, output=output.parent)


def test_a_path_with_spaces_and_dashes_is_accepted(tmp_path: Path) -> None:
    home = tmp_path / "my home-1" / ".codex"
    home.mkdir(parents=True)
    assert codex_profile(port=PORT, codex_home=home) == expected(PORT, home, None)


# wrap_codex


@pytest.fixture
def dirs(tmp_path: Path) -> dict[str, Path]:
    found = {}
    for name in ("worktree", "tmpdir", "home"):
        path = tmp_path / "wrap" / name
        path.mkdir(parents=True)
        found[name] = path
    return found


def test_wrap_codex_passes_the_profile_and_resolved_paths(
    tmp_path: Path, dirs: dict[str, Path], codex_home: Path, output: Path
) -> None:
    link = tmp_path / "wt-link"
    link.symlink_to(dirs["worktree"])
    argv = wrap_codex(
        ["codex", "exec", "--json", "-"],
        executable="/x/sandbox-exec",
        worktree=link,
        tmpdir=dirs["tmpdir"],
        home=dirs["home"],
        codex_home=codex_home,
        port=PORT,
        output=output,
    )
    assert argv == [
        "/x/sandbox-exec",
        "-p",
        expected(PORT, codex_home, output),
        "-D",
        f"WORKTREE={dirs['worktree'].resolve()}",
        "-D",
        f"TMPDIR={dirs['tmpdir'].resolve()}",
        "-D",
        f"HOME={dirs['home'].resolve()}",
        "codex",
        "exec",
        "--json",
        "-",
    ]


def test_wrap_codex_without_output_uses_the_implementation_profile(
    dirs: dict[str, Path], codex_home: Path
) -> None:
    argv = wrap_codex(
        ["true"],
        executable="/usr/bin/sandbox-exec",
        codex_home=codex_home,
        port=PORT,
        **dirs,
    )
    assert argv[2] == expected(PORT, codex_home, None)
    assert argv[-1] == "true"


def test_wrap_codex_keeps_the_arguments(
    dirs: dict[str, Path], codex_home: Path
) -> None:
    args = ["a b", "$HOME", "'x'", ""]
    argv = wrap_codex(
        args,
        executable="/usr/bin/sandbox-exec",
        codex_home=codex_home,
        port=PORT,
        **dirs,
    )
    assert argv[-4:] == args


def test_wrap_codex_refuses_a_bad_port(dirs: dict[str, Path], codex_home: Path) -> None:
    with pytest.raises(ValueError):
        wrap_codex(
            ["true"],
            executable="/usr/bin/sandbox-exec",
            codex_home=codex_home,
            port=0,
            **dirs,
        )


def test_wrap_is_unchanged(dirs: dict[str, Path]) -> None:
    argv = sandbox.wrap(["true"], executable="/usr/bin/sandbox-exec", **dirs)
    assert argv[2] == GATE_PROFILE
