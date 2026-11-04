import ast
import inspect
import re
import sys
from pathlib import Path

import pytest

from orch import redact as redact_module
from orch.redact import RedactionError, ResidualPathError, SecretFoundError, redact

HOME = Path("/Users/alice")
ORCH = HOME / ".orch"
WORKTREE = f"{ORCH}/orchestrator/worktrees/m1-store"
OUTSIDE = Path("/opt/orch")

REPO_ROOT = Path(__file__).resolve().parents[1]


def run(text: str, **kwargs: object) -> str:
    return redact(text, **{"home": HOME, "orch_home": ORCH, **kwargs})


# Token-shaped values are joined at run time so that none appears in the repository.
def github_token(kind: str = "p", length: int = 36, char: str = "a") -> str:
    return "gh" + kind + "_" + char * length


def github_pat(length: int = 22) -> str:
    return "github" + "_pat_" + "a" * length


def jwt(first: int = 10, second: int = 10, third: int = 5) -> str:
    return "ey" + "J" + "a" * first + "." + "b" * second + "." + "c" * third


def key_header(algorithm: str = "") -> str:
    return "-----BEGIN " + algorithm + "PRIVATE" + " KEY-----"


def installation_shaped_token() -> str:
    """Shaped like a real token: 383 chars, 7 alphanumerics after the prefix."""
    head = "gh" + "s_" + "a" * 7 + "_"
    body = ("b.c_d-" * 64)[: 383 - len(head)]
    return head + body


# Replacement


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"{WORKTREE}/src/orch/store.py", "src/orch/store.py"),
        (f"{WORKTREE}/src/orch/store.py:12", "src/orch/store.py:12"),
        (f"see {WORKTREE}/a.py and {WORKTREE}/b.py.", "see a.py and b.py."),
        (WORKTREE, "."),
        (f"{WORKTREE}/", "."),
        (f"cd {WORKTREE} && ls", "cd . && ls"),
        (f"({WORKTREE})", "(.)"),
        (f"see {WORKTREE}.", "see .."),
        (f"see {WORKTREE}/a.py.", "see a.py."),
        (f"{ORCH}/hub.v2/worktrees/fix_a-1.b/x.py", "x.py"),
        (f"{WORKTREE}\n{WORKTREE}/x.py\n", ".\nx.py\n"),
    ],
)
def test_worktree_path_becomes_repository_relative(text: str, expected: str) -> None:
    assert run(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/Users/alice", "~"),
        ("/Users/alice/", "~/"),
        ("see /Users/alice.", "see ~."),
        ("/Users/alice/work/x.py", "~/work/x.py"),
        ('"/Users/alice/x"', '"~/x"'),
        ("'/Users/alice/x'", "'~/x'"),
        ("(/Users/alice/x)", "(~/x)"),
        ("path=/Users/alice/x", "path=~/x"),
        ("a: /Users/alice/x, /Users/alice/y", "a: ~/x, ~/y"),
        ("/Users/alice/x\n/Users/alice/y\n", "~/x\n~/y\n"),
    ],
)
def test_home_becomes_tilde(text: str, expected: str) -> None:
    assert run(text) == expected


def test_runtime_paths_outside_worktrees_use_tilde_when_under_home() -> None:
    assert run(f"{ORCH}/orchestrator/logs/a.log") == "~/.orch/orchestrator/logs/a.log"
    assert run(str(ORCH)) == "~/.orch"


@pytest.mark.parametrize(
    "text",
    [
        f"{ORCH}/orchestrator/worktrees",
        f"{ORCH}/orchestrator/worktrees/",
        f"{ORCH}/orchestrator/old/worktrees/m1/a.py",
    ],
)
def test_only_the_exact_worktree_shape_is_made_relative(text: str) -> None:
    assert run(text) == text.replace(str(HOME), "~")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/opt/orch", "$ORCH_HOME"),
        ("/opt/orch/hub/logs/a.log", "$ORCH_HOME/hub/logs/a.log"),
        ("/opt/orch/hub/worktrees/m1/a.py", "a.py"),
        ("/opt/orch/hub/worktrees/m1", "."),
        ("/opt/orchard/x", "/opt/orchard/x"),
        ("/Users/alice/x", "~/x"),
    ],
)
def test_orch_home_outside_home_becomes_a_variable(text: str, expected: str) -> None:
    assert run(text, orch_home=OUTSIDE) == expected


def test_orch_home_in_a_sibling_of_home_is_not_treated_as_under_home() -> None:
    assert (
        run("/Users/alice2/.orch/x", orch_home=Path("/Users/alice2/.orch"))
        == "$ORCH_HOME/x"
    )


@pytest.mark.parametrize("root", ["/private/tmp/o", "/var/folders/ab/o"])
def test_known_orch_home_under_a_blocked_prefix_is_replaced_before_the_check(
    root: str,
) -> None:
    assert run(f"{root}/hub/logs/a", orch_home=Path(root)) == "$ORCH_HOME/hub/logs/a"
    assert run(f"{root}/hub/worktrees/m1/a.py", orch_home=Path(root)) == "a.py"


def test_home_may_be_root_user_home() -> None:
    assert (
        run("/root/x and /root", home=Path("/root"), orch_home=Path("/root/.orch"))
        == "~/x and ~"
    )


@pytest.mark.parametrize(
    "text", ["", "plain text", "한글 문장\n둘째 줄", "src/orch/store.py", "~/x"]
)
def test_text_without_known_paths_is_unchanged(text: str) -> None:
    assert run(text) == text


@pytest.mark.parametrize(
    "text",
    [WORKTREE + "/a.py", f"{ORCH}/x/logs/a", "/Users/alice/x", "~/x", "plain"],
)
def test_redact_is_idempotent(text: str) -> None:
    once = run(text)
    assert run(once) == once


# Residual paths


@pytest.mark.parametrize(
    "text",
    [
        "/Users/bob/x",
        "/home/bob",
        "/home/bob/x",
        "C:\\Users\\bob",
        "/private/tmp/x",
        "/var/folders/ab/x",
        "/private/var/folders/ab/x",
        "/root",
        "/root/",
        "/root/x",
        "see /root.",
        "(/root)",
        "a/root/b",
        "ok\nthen /home/bob\n",
    ],
)
def test_residual_personal_path_raises(text: str) -> None:
    with pytest.raises(ResidualPathError):
        run(text)


@pytest.mark.parametrize(
    "text",
    [
        "/Users/alice2/x",
        "/Users/alice.bak/x",
        "/Users/alice-old/x",
        "/Users/alice_old/x",
        "/System/Volumes/Data/Users/alice/x",
        "file:///Users/alice/x",
        "/mnt/Users/alice/x",
    ],
)
def test_home_is_only_replaced_as_a_whole_absolute_path(text: str) -> None:
    with pytest.raises(ResidualPathError):
        run(text)


def test_home_is_matched_literally_not_as_a_regex() -> None:
    home = Path("/Users/a.b")
    assert run("/Users/a.b/x", home=home, orch_home=home / ".orch") == "~/x"
    with pytest.raises(ResidualPathError):
        run("/Users/axb/x", home=home, orch_home=home / ".orch")


def test_one_residual_path_among_replaced_ones_raises() -> None:
    with pytest.raises(ResidualPathError):
        run(f"/Users/alice/a {WORKTREE}/b /home/bob/c")


@pytest.mark.parametrize(
    "text",
    [
        "src/root.py",
        "/rootfs/x",
        "/root_cause",
        "/root-dir",
        "/root.txt",
        "/Users",
        "/home",
    ],
)
def test_lookalikes_of_blocked_patterns_pass(text: str) -> None:
    assert run(text) == text


# Deliberate over-blocking: a false alarm is visible, a public leak is not.
@pytest.mark.parametrize(
    "text",
    [
        "src/home/page.py",
        "src/Users/x.py",
        "/Users/alice/home/x",
        "/Users/alice/root/x",
    ],
)
def test_patterns_also_block_relative_paths_that_contain_them(text: str) -> None:
    with pytest.raises(ResidualPathError):
        run(text)


# Secrets


@pytest.mark.parametrize("kind", ["p", "o", "u", "s", "r"])
def test_github_token_prefixes_raise(kind: str) -> None:
    with pytest.raises(SecretFoundError):
        run("token " + github_token(kind))


@pytest.mark.parametrize("char", [".", "-", "_"])
def test_github_token_body_may_contain_punctuation(char: str) -> None:
    with pytest.raises(SecretFoundError):
        run(github_token("s", 36, char))


def test_installation_shaped_token_raises() -> None:
    token = installation_shaped_token()
    assert len(token) == 383
    with pytest.raises(SecretFoundError):
        run(f"Authorization: {token}")


def test_github_token_below_minimum_length_passes() -> None:
    text = github_token("p", 35)
    assert run(text) == text


def test_github_pat_length_boundary() -> None:
    with pytest.raises(SecretFoundError):
        run(github_pat(22))
    assert run(github_pat(21)) == github_pat(21)


@pytest.mark.parametrize("before", ["-", " ", "=", '"', "\n", ":", "/"])
def test_token_after_a_separator_raises(before: str) -> None:
    with pytest.raises(SecretFoundError):
        run(before + github_token())
    with pytest.raises(SecretFoundError):
        run(before + github_pat())


@pytest.mark.parametrize("before", ["x", "7", "_"])
def test_token_inside_a_longer_identifier_passes(before: str) -> None:
    assert run(before + github_token()) == before + github_token()
    assert run(before + github_pat()) == before + github_pat()


@pytest.mark.parametrize("third", [5, 0])
def test_jwt_raises_with_or_without_signature(third: int) -> None:
    with pytest.raises(SecretFoundError):
        run("Bearer " + jwt(third=third))


@pytest.mark.parametrize(
    "value", [jwt(first=9), jwt(second=9)], ids=["first", "second"]
)
def test_jwt_with_short_segments_passes(value: str) -> None:
    assert run(value) == value


@pytest.mark.parametrize("algorithm", ["", "RSA ", "EC ", "OPENSSH ", "ENCRYPTED "])
def test_private_key_header_raises(algorithm: str) -> None:
    with pytest.raises(SecretFoundError):
        run("line\n" + key_header(algorithm) + "\nbody\n")


def test_public_key_header_passes() -> None:
    text = "-----BEGIN " + "PUBLIC" + " KEY-----"
    assert run(text) == text


@pytest.mark.parametrize("text", ["hunter2", "xxhunter2yy", "a\nhunter2\nb"])
def test_exact_secret_anywhere_in_text_raises(text: str) -> None:
    with pytest.raises(SecretFoundError):
        run(text, secrets=("other", "hunter2"))


def test_secret_is_matched_literally_not_as_a_regex() -> None:
    assert run("aXbbc", secrets=("a.b*c",)) == "aXbbc"
    with pytest.raises(SecretFoundError):
        run("a.b*c", secrets=("a.b*c",))


def test_secrets_accepts_any_iterable_including_a_generator() -> None:
    with pytest.raises(SecretFoundError):
        run("hunter2", secrets=(value for value in ["x-long-value", "hunter2"]))
    assert run("plain", secrets=[]) == "plain"
    assert run("plain", secrets=iter(())) == "plain"


@pytest.mark.parametrize("secret", [f"{HOME}/key", f"{WORKTREE}/key", f"{ORCH}/key"])
def test_secrets_are_checked_on_the_original_text(secret: str) -> None:
    with pytest.raises(SecretFoundError):
        run(f"value {secret}", secrets=(secret,))


def test_secret_wins_over_residual_path() -> None:
    with pytest.raises(SecretFoundError):
        run("/home/bob " + github_token())


def test_token_inside_a_known_path_raises() -> None:
    with pytest.raises(SecretFoundError):
        run(f"{WORKTREE}/" + github_token())


# Arguments


@pytest.mark.parametrize("secrets", [("",), ("ok", "")])
@pytest.mark.parametrize("text", ["", "plain"])
def test_empty_secret_is_a_caller_bug_and_raises(
    text: str, secrets: tuple[str, ...]
) -> None:
    with pytest.raises(ValueError):
        run(text, secrets=secrets)


@pytest.mark.parametrize(
    "secrets", ["hunter2", b"hunter2", (None,), (b"hunter2",), None]
)
def test_malformed_secrets_raise_type_error(secrets: object) -> None:
    with pytest.raises(TypeError):
        run("plain", secrets=secrets)


@pytest.mark.parametrize("bad", [Path("relative"), Path(""), Path("/")])
def test_home_and_orch_home_must_be_absolute_and_not_the_root(bad: Path) -> None:
    with pytest.raises(ValueError):
        run("plain", home=bad)
    with pytest.raises(ValueError):
        run("plain", orch_home=bad)


def test_no_bypass_parameter_exists() -> None:
    parameters = inspect.signature(redact).parameters
    assert list(parameters) == ["text", "home", "orch_home", "secrets"]
    for name in ("home", "orch_home", "secrets"):
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY


# Exceptions


def test_exception_hierarchy() -> None:
    assert issubclass(SecretFoundError, RedactionError)
    assert issubclass(ResidualPathError, RedactionError)
    assert not issubclass(RedactionError, ValueError)


@pytest.mark.parametrize(
    "value",
    [
        github_token(),
        github_pat(),
        jwt(),
        installation_shaped_token(),
        "hunter2",
    ],
    ids=["github_token", "github_pat", "jwt", "installation_shaped", "exact_value"],
)
def test_secret_error_never_carries_the_value(value: str) -> None:
    with pytest.raises(SecretFoundError) as caught:
        run(f"leadingword {value} after", secrets=("hunter2",))
    exc = caught.value
    assert str(exc)
    assert value not in str(exc)
    assert value not in repr(exc)
    assert "leadingword" not in str(exc)
    assert exc.__cause__ is None
    assert exc.__context__ is None


def test_residual_error_never_carries_the_path() -> None:
    with pytest.raises(ResidualPathError) as caught:
        run("leadingword /home/bob/private-name after")
    exc = caught.value
    assert str(exc)
    for fragment in ("bob", "private-name", "leadingword"):
        assert fragment not in str(exc)
        assert fragment not in repr(exc)
    assert exc.__cause__ is None
    assert exc.__context__ is None


# Module rules


def test_module_imports_only_the_standard_library() -> None:
    tree = ast.parse(Path(redact_module.__file__).read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            modules.add((node.module or "").split(".")[0])
    assert modules <= set(sys.stdlib_module_names)


@pytest.mark.parametrize("relative", ["tests/test_redact.py", "src/orch/redact.py"])
def test_sources_hold_no_token_shaped_string(relative: str) -> None:
    source = (REPO_ROOT / relative).read_text(encoding="utf-8")
    shapes = [
        r"(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9_.\-]{36,}",
        r"(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_.\-]{22,}",
        r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]*",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    ]
    for shape in shapes:
        assert re.search(shape, source) is None
