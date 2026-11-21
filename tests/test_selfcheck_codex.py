"""orch selfcheck for Codex: the orch mise, the bootstrap, candidates and isolation.

Nothing here uses the network or runs the real mise, codex or codesign. The fake mise
follows FL-M7 (mise 2026.10.2): `mise lock --global` writes no entry for 0.160.0 and
`mise install` then writes the entry of the current platform.
"""

import fcntl
import hashlib
import json
import os
import stat
import subprocess
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orch import notify, selfcheck, selfcheck_codex
from orch.config import Config
from orch.lanes.codex import ISOLATION_ARGS
from orch.selfcheck import CheckResult, CurrentCheck, NotFound, SelfcheckError
from orch.selfcheck_codex import (
    CodexLayout,
    CodexPlatform,
    IsolationContext,
    Prepared,
    check_current_codex,
    check_isolation,
    codex_env,
    codex_layout,
    codex_platform,
    config_text,
    isolation_argv,
    load_codex_pins,
    mise_env,
    prepare_version,
    remove_install,
    run_codex,
)

REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
MACHINE = "x86_64"
TEAM = "2DC432GLL2"
MAGIC = b"\xcf\xfa\xed\xfe"
ASSET = "codex-package-x86_64-apple-darwin.tar.gz"
ARM_ASSET = "codex-package-aarch64-apple-darwin.tar.gz"
MISE = Path("/opt/mise/bin/mise")
ENVIRON = {
    "HOME": "/elsewhere",
    "PATH": "/usr/local/bin:/usr/bin",
    "LANG": "ko_KR.UTF-8",
    "USER": "tester",
    "GH_TOKEN": "token-must-not-leak",
    "GITHUB_TOKEN": "token-must-not-leak",
    "MISE_SHELL": "zsh",
}
RELEASES = "https://api.github.com/repos/openai/codex/releases?per_page=10&page={}"
SUMS = (
    "https://github.com/openai/codex/releases/download/"
    "rust-v{}/codex-package_SHA256SUMS"
)
CODESIGN_REQ = f'-R=anchor apple generic and certificate leaf[subject.OU] = "{TEAM}"'


def checksum(version: str, asset: str = ASSET) -> str:
    return hashlib.sha256(f"{asset} {version}".encode()).hexdigest()


def asset_url(version: str, asset: str = ASSET) -> str:
    return f"https://github.com/openai/codex/releases/download/rust-v{version}/{asset}"


def sums_text(version: str) -> str:
    other = hashlib.sha256(f"other {version}".encode()).hexdigest()
    lines = [
        f"{other}  codex-app-server-package-x86_64-apple-darwin.tar.gz",
        f"{checksum(version, ARM_ASSET)}  {ARM_ASSET}",
        f"{other}  codex-provisioned-package-x86_64-apple-darwin.tar.gz",
        f"{checksum(version)}  {ASSET}",
        f"{other}  codex-package-x86_64-unknown-linux-musl.tar.gz",
    ]
    return "\n".join(lines) + "\n"


def lock_entry(version: str, *, arm: bool = True) -> str:
    text = (
        f'[[tools.codex]]\nversion = "{version}"\nbackend = "aqua:openai/codex"\n'
        f'specifiers = ["{version}"]\n\n'
    )
    platforms = [("macos-x64", ASSET)] + ([("macos-arm64", ARM_ASSET)] if arm else [])
    for key, asset in platforms:
        text += (
            f'[tools.codex."platforms.{key}"]\n'
            f'checksum = "sha256:{checksum(version, asset)}"\n'
            f'url = "{asset_url(version, asset)}"\n'
            'url_api = "https://api.github.com/repos/openai/codex/releases/assets/1"\n\n'
        )
    return text


LOCK_HEADER = "# @generated\n\nlockfile_version = 3\n\n"


def completed(
    argv: Sequence[str], code: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), code, stdout, stderr)


# Fakes


@dataclass
class FakeMise:
    """mise 2026.10.2 as measured: config ls, lock --global and install."""

    skip_lock: set[str] = field(default_factory=lambda: {"0.160.0"})
    exits: dict[str, int] = field(default_factory=dict)
    raises: dict[str, BaseException] = field(default_factory=dict)
    ls_output: Callable[[Path, list[str]], str] | None = None
    install_writes_lock: bool = True
    package: Callable[[Path, str], None] | None = None
    calls: list[tuple[list[str], Path, dict[str, str], float]] = field(
        default_factory=list
    )

    def __call__(
        self, argv: Sequence[str], cwd: Path, env: Mapping[str, str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), cwd, dict(env), timeout))
        action = " ".join(argv[1:])
        if action in self.raises:
            raise self.raises[action]
        if action in self.exits:
            return completed(argv, self.exits[action])
        config = Path(env["MISE_GLOBAL_CONFIG_FILE"])
        tools = tomllib.loads(config.read_text(encoding="utf-8"))["tools"]
        version = tools["codex"]
        lock = config.parent / "mise.lock"
        if action == "config ls --json":
            if self.ls_output is not None:
                return completed(argv, 0, self.ls_output(config, list(tools)))
            body = [{"path": str(config), "tools": list(tools)}]
            return completed(argv, 0, json.dumps(body, indent=2))
        if action == "lock --global":
            if version in self.skip_lock:
                if not lock.exists():
                    lock.write_text(LOCK_HEADER, encoding="utf-8")
            else:
                lock.write_text(LOCK_HEADER + lock_entry(version), encoding="utf-8")
            return completed(argv, 0, "", "Lockfile written")
        if action == "install":
            pkg = Path(env["MISE_DATA_DIR"]) / "installs" / "codex" / version
            (self.package or build_package)(pkg, version)
            alias = pkg.parent / "latest"
            if alias.is_symlink():
                alias.unlink()
            alias.symlink_to(f"./{version}")
            text = lock.read_text(encoding="utf-8") if lock.exists() else LOCK_HEADER
            if self.install_writes_lock and f'version = "{version}"' not in text:
                lock.write_text(text + lock_entry(version, arm=False), encoding="utf-8")
            return completed(argv, 0)
        raise AssertionError(f"unexpected mise call {argv}")


def build_package(pkg: Path, version: str) -> None:
    """The measured layout: Mach-O executables and dylibs plus text files."""
    files = {
        "bin/codex": MAGIC + f"codex-cli {version}".encode(),
        "bin/codex-code-mode-host": MAGIC + b"host",
        "codex-path/rg": MAGIC + b"rg",
        "codex-resources/voice/lib/libz.1.dylib": MAGIC + b"libz",
        "codex-package.json": b'{\n  "layoutVersion": 1\n}\n',
        "codex-resources/voice/NOTICE.md": b"# Notice\n",
    }
    for name, body in files.items():
        path = pkg / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        path.chmod(0o755)


@dataclass
class FakeCommands:
    """codesign and --version."""

    codesign_fail: set[str] = field(default_factory=set)
    codesign_raises: bool = False
    version_output: dict[str, str] = field(default_factory=dict)
    calls: list[tuple[list[str], float]] = field(default_factory=list)

    def __call__(
        self, argv: Sequence[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), timeout))
        if argv[0] == selfcheck.CODESIGN:
            if self.codesign_raises:
                raise OSError("codesign failed to start")
            name = Path(argv[-1]).name
            return completed(argv, 3 if name in self.codesign_fail else 0)
        if list(argv[1:]) == ["--version"]:
            if argv[0] in self.version_output:
                return completed(argv, 0, self.version_output[argv[0]])
            body = Path(argv[0]).read_bytes()
            assert body[:4] == MAGIC
            return completed(argv, 0, body[4:].decode() + "\n")
        raise AssertionError(f"unexpected command {argv}")

    def codesigned(self) -> list[str]:
        return [argv[-1] for argv, _ in self.calls if argv[0] == selfcheck.CODESIGN]


@dataclass
class FakeFetch:
    bodies: dict[str, bytes] = field(default_factory=dict)
    errors: dict[str, BaseException] = field(default_factory=dict)
    calls: list[tuple[str, int]] = field(default_factory=list)

    def __call__(self, url: str, limit: int) -> bytes:
        self.calls.append((url, limit))
        if url in self.errors:
            raise self.errors[url]
        if url not in self.bodies:
            raise NotFound(url)
        return self.bodies[url]


@dataclass
class FakeSend:
    raises: bool = False
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    def __call__(self, event: str, **kwargs: object) -> notify.Outcome:
        self.calls.append((event, kwargs))
        if self.raises:
            raise RuntimeError("send failed")
        return notify.Outcome.SENT


def release(
    tag: str, published: str, *, prerelease: bool = False, draft: bool = False
) -> dict[str, object]:
    return {
        "tag_name": tag,
        "prerelease": prerelease,
        "draft": draft,
        "published_at": published,
        "assets": [],
    }


# The measured release list, newest first by creation, not by version.
PAGE_ONE = [
    release("rust-v0.173.0-alpha.4", "2026-11-21T00:35:19Z", prerelease=True),
    release("rust-v0.171.0", "2026-11-20T00:44:47Z"),
    release("rust-v0.172.0-alpha.2", "2026-11-19T15:40:25Z", prerelease=True),
    release("rust-v0.170.0", "2026-11-18T03:40:48Z"),
    release("rust-v0.169.1", "2026-11-17T22:21:10Z"),
]
PAGE_TWO = [
    release("rust-v0.169.0", "2026-11-13T08:37:05Z"),
    release("rust-v0.168.0", "2026-11-13T03:54:29Z"),
]


def make_hub(root: Path, pins_text: str | None = None) -> Path:
    hub = root / "hub"
    (hub / "config").mkdir(parents=True)
    text = (
        (REPO / "config" / "pins.toml").read_text(encoding="utf-8")
        if pins_text is None
        else pins_text
    )
    (hub / "config" / "pins.toml").write_text(text, encoding="utf-8")
    return hub


@dataclass
class World:
    root: Path
    hub: Path
    orch_home: Path
    home: Path
    mise: FakeMise = field(default_factory=FakeMise)
    commands: FakeCommands = field(default_factory=FakeCommands)
    fetch: FakeFetch = field(default_factory=FakeFetch)
    send: FakeSend = field(default_factory=FakeSend)
    settings: Config = field(default_factory=lambda: Config(mise_path=MISE))
    machine: str = MACHINE

    @property
    def layout(self) -> CodexLayout:
        return codex_layout(self.orch_home)

    @property
    def state_path(self) -> Path:
        return self.orch_home / "agents.json"

    def publish(self, *pages: list[dict[str, object]]) -> None:
        for number, page in enumerate(pages, start=1):
            self.fetch.bodies[RELEASES.format(number)] = json.dumps(page).encode()
        self.fetch.bodies[RELEASES.format(len(pages) + 1)] = b"[]"

    def publish_sums(self, *versions: str) -> None:
        for version in versions:
            self.fetch.bodies[SUMS.format(version)] = sums_text(version).encode()

    def run(self, **overrides: object) -> selfcheck_codex.CodexReport:
        kwargs: dict[str, object] = {
            "hub": self.hub,
            "orch_home": self.orch_home,
            "home": self.home,
            "settings": self.settings,
            "now": NOW,
            "fetch": self.fetch,
            "command": self.commands,
            "mise": self.mise,
            "send": self.send,
            "machine": self.machine,
            "environ": ENVIRON,
        }
        kwargs.update(overrides)
        return run_codex(**kwargs)  # type: ignore[arg-type]

    def state(self) -> dict[str, object]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def codex(self) -> dict[str, object]:
        return self.state()["agents"]["codex"]  # type: ignore[index]

    def record_current(self, version: str = "0.160.0") -> Path:
        """An existing record without running the bootstrap."""
        binary = self.layout.installs / version / "bin" / "codex"
        build_package(binary.parents[1], version)
        state = {
            "format": 1,
            "agents": {
                "codex": {
                    "current": {
                        "version": version,
                        "sha256": selfcheck.sha256_file(binary),
                        "path": str(binary),
                        "at": NOW.isoformat(),
                        "source": "bootstrap",
                    },
                    "previous": None,
                    "candidate": None,
                }
            },
        }
        selfcheck.save_agents_state(self.state_path, state)
        return binary


@pytest.fixture
def world(tmp_path: Path) -> World:
    home = tmp_path / "home"
    home.mkdir()
    w = World(tmp_path, make_hub(tmp_path), tmp_path / "orch", home)
    w.publish(PAGE_ONE, PAGE_TWO)
    w.publish_sums("0.160.0", "0.170.0", "0.171.0")
    return w


# Pins, platform, layout and environments


def test_the_real_pins_load() -> None:
    pins = load_codex_pins(REPO)
    body = (REPO / "config" / "pins.toml").read_bytes()
    assert pins.sha256 == hashlib.sha256(body).hexdigest()
    assert pins.version == "0.160.0"
    assert pins.codesign_team == TEAM
    assert pins.blocking == ("FL-M4", "S-M2", "S-M2b", "isolation")
    assert len(pins.warn) == 6
    assert all(isinstance(item, str) for item in pins.warn)


def test_only_s_m2_and_s_m2b_lack_a_check() -> None:
    pins = load_codex_pins(REPO)
    assert selfcheck_codex.CODEX_AUTOMATED_IDS == frozenset({"FL-M4", "isolation"})
    missing = [i for i in pins.blocking if i not in selfcheck_codex.CODEX_AUTOMATED_IDS]
    assert missing == ["S-M2", "S-M2b"]


def _pins_with(replace: tuple[str, str]) -> str:
    text = (REPO / "config" / "pins.toml").read_text(encoding="utf-8")
    old, new = replace
    head, codex = text.split("[codex]", 1)
    assert old in codex
    return head + "[codex]" + codex.replace(old, new, 1)


@pytest.mark.parametrize(
    "replace",
    [
        ('version = "0.160.0"', 'version = "0.160"'),
        ('version = "0.160.0"', "version = 160"),
        ('codesign_team = "2DC432GLL2"', 'codesign_team = "2dc432gll2"'),
        ('codesign_team = "2DC432GLL2"', 'codesign_team = "2DC432GLL"'),
        ('{ id = "S-M2b"', '{ id = "S-M2"'),
        ('{ id = "S-M2b"', "{ id = 7"),
        ('warn = [\n  "codex-review', 'warn = [\n  7,\n  "codex-review'),
    ],
)
def test_malformed_codex_pins_are_refused(
    tmp_path: Path, replace: tuple[str, str]
) -> None:
    hub = make_hub(tmp_path, _pins_with(replace))
    with pytest.raises(SelfcheckError):
        load_codex_pins(hub)


def test_missing_pins_are_refused(tmp_path: Path) -> None:
    with pytest.raises(SelfcheckError):
        load_codex_pins(tmp_path)


def test_platforms() -> None:
    assert codex_platform("x86_64") == CodexPlatform("macos-x64", ASSET)
    assert codex_platform("arm64") == CodexPlatform("macos-arm64", ARM_ASSET)
    with pytest.raises(SelfcheckError):
        codex_platform("aarch64")


def test_layout(tmp_path: Path) -> None:
    root = tmp_path / "agent-mise"
    assert codex_layout(tmp_path) == CodexLayout(
        root=root,
        candidate=root / "candidate",
        data=root / "data",
        cache=root / "cache",
        state=root / "state",
        installs=root / "data" / "installs" / "codex",
        link=tmp_path / "bin" / "codex",
    )
    # FL-M7: mise reads an ancestor's mise/ or .mise/ config.toml as a project config.
    assert all(
        part not in ("mise", ".mise") for part in root.relative_to(tmp_path).parts
    )


def test_mise_env_has_exactly_the_measured_keys(tmp_path: Path) -> None:
    layout = codex_layout(tmp_path)
    env = mise_env(layout, layout.candidate, Path("/home/u"), ENVIRON)
    assert env == {
        "HOME": "/home/u",
        "PATH": selfcheck.SESSION_PATH,
        "LANG": "ko_KR.UTF-8",
        "USER": "tester",
        "MISE_GLOBAL_CONFIG_FILE": str(layout.candidate / "config.toml"),
        "MISE_CONFIG_DIR": str(layout.candidate),
        "MISE_DATA_DIR": str(layout.data),
        "MISE_CACHE_DIR": str(layout.cache),
        "MISE_STATE_DIR": str(layout.state),
    }
    bare = mise_env(layout, layout.root, Path("/home/u"), {})
    assert bare["LANG"] == "en_US.UTF-8"
    assert "USER" not in bare
    assert bare["MISE_CONFIG_DIR"] == str(layout.root)


def test_codex_env_has_no_tokens() -> None:
    assert codex_env(Path("/home/u"), ENVIRON) == {
        "HOME": "/home/u",
        "PATH": selfcheck.SESSION_PATH,
        "LANG": "ko_KR.UTF-8",
        "USER": "tester",
    }
    assert codex_env(Path("/home/u"), {}) == {
        "HOME": "/home/u",
        "PATH": selfcheck.SESSION_PATH,
        "LANG": "en_US.UTF-8",
    }


def test_config_text() -> None:
    text = config_text("0.171.0")
    assert text == '[tools]\ncodex = "0.171.0"\n\n[settings]\nlockfile = true\n'
    assert tomllib.loads(text) == {
        "tools": {"codex": "0.171.0"},
        "settings": {"lockfile": True},
    }


# Preparing a version


def prepare(world: World, version: str) -> Prepared | str:
    return prepare_version(
        version,
        layout=world.layout,
        pins=load_codex_pins(world.hub),
        platform=codex_platform(world.machine),
        mise_path=MISE,
        home=world.home,
        environ=ENVIRON,
        mise=world.mise,
        fetch=world.fetch,
        command=world.commands,
    )


@pytest.mark.parametrize("version", ["0.171.0", "0.160.0"])
def test_a_version_is_prepared_in_the_candidate_directory(
    world: World, version: str
) -> None:
    result = prepare(world, version)
    layout = world.layout
    binary = layout.installs / version / "bin" / "codex"
    assert result == Prepared(version, binary, selfcheck.sha256_file(binary))
    assert (layout.candidate / "config.toml").read_text() == config_text(version)
    env = mise_env(layout, layout.candidate, world.home, ENVIRON)
    assert [(argv, cwd, e, t) for argv, cwd, e, t in world.mise.calls] == [
        ([str(MISE), "config", "ls", "--json"], layout.candidate, env, 120),
        ([str(MISE), "lock", "--global"], layout.candidate, env, 120),
        ([str(MISE), "install"], layout.candidate, env, 900),
    ]
    assert world.fetch.calls == [(SUMS.format(version), 64 << 10)]


def test_every_mach_o_file_is_codesigned_in_path_order(world: World) -> None:
    assert isinstance(prepare(world, "0.171.0"), Prepared)
    pkg = world.layout.installs / "0.171.0"
    expected = sorted(
        str(pkg / name)
        for name in (
            "bin/codex",
            "bin/codex-code-mode-host",
            "codex-path/rg",
            "codex-resources/voice/lib/libz.1.dylib",
        )
    )
    assert world.commands.codesigned() == expected
    for argv, timeout in world.commands.calls:
        if argv[0] == selfcheck.CODESIGN:
            assert argv[:-1] == [
                selfcheck.CODESIGN,
                "--verify",
                "--strict",
                CODESIGN_REQ,
            ]
            assert timeout == 60
    assert (
        [str(pkg / "bin" / "codex"), "--version"],
        30,
    ) in world.commands.calls


def test_a_stale_candidate_lock_is_removed_before_locking(world: World) -> None:
    candidate = world.layout.candidate
    candidate.mkdir(parents=True)
    stale = lock_entry("0.160.0").replace(checksum("0.160.0"), "0" * 64)
    (candidate / "mise.lock").write_text(LOCK_HEADER + stale, encoding="utf-8")
    assert isinstance(prepare(world, "0.160.0"), Prepared)


def _two_tools(config: Path, tools: list[str]) -> str:
    return json.dumps([{"path": str(config), "tools": ["codex", "node"]}])


def _two_configs(config: Path, tools: list[str]) -> str:
    return json.dumps(
        [
            {"path": str(config), "tools": ["codex"]},
            {"path": "/Users/u/.config/mise/config.toml", "tools": ["codex"]},
        ]
    )


def _other_path(config: Path, tools: list[str]) -> str:
    return json.dumps([{"path": "/Users/u/.orch/mise/config.toml", "tools": ["codex"]}])


def _not_json(config: Path, tools: list[str]) -> str:
    return f"{config}  codex"


@pytest.mark.parametrize(
    ("setup", "detail"),
    [
        (lambda w: w.mise.exits.update({"config ls --json": 1}), "mise:config"),
        (lambda w: setattr(w.mise, "ls_output", _two_tools), "mise:config"),
        (lambda w: setattr(w.mise, "ls_output", _two_configs), "mise:config"),
        (lambda w: setattr(w.mise, "ls_output", _other_path), "mise:config"),
        (lambda w: setattr(w.mise, "ls_output", _not_json), "mise:config"),
        (lambda w: w.mise.exits.update({"lock --global": 1}), "mise:lock"),
        (lambda w: w.mise.exits.update({"install": 1}), "mise:install"),
        (lambda w: w.mise.raises.update({"install": OSError("gone")}), "mise:run"),
        (
            lambda w: w.mise.raises.update(
                {"lock --global": subprocess.TimeoutExpired("mise", 120)}
            ),
            "mise:run",
        ),
        (lambda w: setattr(w.mise, "install_writes_lock", False), "lock:entry"),
    ],
)
def test_mise_failures(
    world: World, setup: Callable[[World], object], detail: str
) -> None:
    setup(world)
    assert prepare(world, "0.160.0") == detail


def _write_lock_on_install(text: str) -> Callable[[Path, str], None]:
    def package(pkg: Path, version: str) -> None:
        build_package(pkg, version)
        lock = pkg.parents[3] / "candidate" / "mise.lock"
        lock.write_text(text, encoding="utf-8")

    return package


@pytest.mark.parametrize(
    "lock_text",
    [
        "lockfile_version = 2\n\n" + lock_entry("0.171.0"),
        LOCK_HEADER + lock_entry("0.171.0").replace("rust-v0.171.0", "rust-v0.170.0"),
        LOCK_HEADER + lock_entry("0.171.0").replace(ASSET, ARM_ASSET, 2),
        LOCK_HEADER + lock_entry("0.171.0").replace("sha256:", "sha512:"),
        LOCK_HEADER + lock_entry("0.171.0") + lock_entry("0.171.0"),
        LOCK_HEADER + lock_entry("0.170.0"),
        LOCK_HEADER
        + lock_entry("0.171.0").replace(
            '"platforms.macos-x64"', '"platforms.linux-x64"'
        ),
        "this is = = not toml",
    ],
)
def test_a_wrong_lock_entry_is_refused(world: World, lock_text: str) -> None:
    world.mise.skip_lock = {"0.171.0"}
    world.mise.install_writes_lock = False
    world.mise.package = _write_lock_on_install(lock_text)
    assert prepare(world, "0.171.0") == "lock:entry"


def test_a_tampered_lock_checksum_does_not_match_the_sums(world: World) -> None:
    text = LOCK_HEADER + lock_entry("0.171.0").replace(checksum("0.171.0"), "e" * 64)
    world.mise.skip_lock = {"0.171.0"}
    world.mise.install_writes_lock = False
    world.mise.package = _write_lock_on_install(text)
    assert prepare(world, "0.171.0") == "sums:mismatch"


def _sums_without_package_line(version: str) -> bytes:
    lines = sums_text(version).splitlines()
    return (
        "\n".join(line for line in lines if not line.endswith(f"  {ASSET}")) + "\n"
    ).encode()


@pytest.mark.parametrize(
    ("body", "error", "detail"),
    [
        (None, NotFound("sums"), "sums:fetch"),
        (None, selfcheck.FetchError("down"), "sums:fetch"),
        (b"\xff\xfe\x00", None, "sums:format"),
        (_sums_without_package_line("0.171.0"), None, "sums:missing"),
        (
            (sums_text("0.171.0") + f"{'a' * 64}  {ASSET}\n").encode(),
            None,
            "sums:ambiguous",
        ),
        (
            sums_text("0.171.0").replace(checksum("0.171.0"), "b" * 64).encode(),
            None,
            "sums:mismatch",
        ),
        (
            # Only an exact two-space line counts; a one-space line is not read.
            sums_text("0.171.0").replace(f"  {ASSET}", f" {ASSET}").encode(),
            None,
            "sums:missing",
        ),
    ],
)
def test_sums_failures(
    world: World, body: bytes | None, error: BaseException | None, detail: str
) -> None:
    url = SUMS.format("0.171.0")
    if error is not None:
        world.fetch.errors[url] = error
    if body is not None:
        world.fetch.bodies[url] = body
    assert prepare(world, "0.171.0") == detail


def test_the_provisioned_package_line_is_not_taken_for_the_package(
    world: World,
) -> None:
    # The provisioned line ends with the same platform name; it must not match.
    body = _sums_without_package_line("0.171.0")
    assert b"codex-provisioned-package-x86_64-apple-darwin.tar.gz" in body
    world.fetch.bodies[SUMS.format("0.171.0")] = body
    assert prepare(world, "0.171.0") == "sums:missing"


def _package_without_binary(pkg: Path, version: str) -> None:
    build_package(pkg, version)
    (pkg / "bin" / "codex").write_bytes(b"#!/bin/sh\necho codex-cli\n")


def _package_with_file_link(pkg: Path, version: str) -> None:
    build_package(pkg, version)
    (pkg / "bin" / "extra").symlink_to("codex")


def _package_with_dir_link(pkg: Path, version: str) -> None:
    build_package(pkg, version)
    (pkg / "codex-resources" / "linked").symlink_to("/usr")


def _package_as_link(pkg: Path, version: str) -> None:
    real = pkg.parent / f".real-{version}"
    build_package(real, version)
    pkg.symlink_to(real)


def _package_missing(pkg: Path, version: str) -> None:
    pkg.parent.mkdir(parents=True, exist_ok=True)


@pytest.mark.parametrize(
    ("package", "detail"),
    [
        (_package_missing, "package:missing"),
        (_package_as_link, "package:missing"),
        (_package_with_file_link, "package:symlink"),
        (_package_with_dir_link, "package:symlink"),
        (_package_without_binary, "package:binary"),
    ],
)
def test_package_failures(
    world: World, package: Callable[[Path, str], None], detail: str
) -> None:
    world.mise.package = package
    assert prepare(world, "0.171.0") == detail


def test_one_unsigned_dylib_fails_the_codesign_check(world: World) -> None:
    world.commands.codesign_fail = {"libz.1.dylib"}
    assert prepare(world, "0.171.0") == "codesign"


def test_codesign_that_cannot_start_fails(world: World) -> None:
    world.commands.codesign_raises = True
    assert prepare(world, "0.171.0") == "codesign"


@pytest.mark.parametrize(
    "output",
    ["codex-cli 0.170.0\n", "codex 0.171.0\n", "codex-cli 0.171.0 extra\n", ""],
)
def test_a_wrong_version_output_fails(world: World, output: str) -> None:
    binary = world.layout.installs / "0.171.0" / "bin" / "codex"
    world.commands.version_output[str(binary)] = output
    assert prepare(world, "0.171.0") == "version"


# Removing an install


def test_remove_install_removes_only_a_version_directory(tmp_path: Path) -> None:
    layout = codex_layout(tmp_path)
    build_package(layout.installs / "0.171.0", "0.171.0")
    remove_install(layout, "0.171.0", set())
    assert not (layout.installs / "0.171.0").exists()


def test_remove_install_leaves_protected_links_and_odd_names(tmp_path: Path) -> None:
    layout = codex_layout(tmp_path)
    build_package(layout.installs / "0.160.0", "0.160.0")
    protected = {(layout.installs / "0.160.0" / "bin" / "codex").resolve()}
    remove_install(layout, "0.160.0", protected)
    assert (layout.installs / "0.160.0" / "bin" / "codex").exists()

    outside = tmp_path / "outside"
    build_package(outside, "0.171.0")
    (layout.installs / "0.171.0").symlink_to(outside)
    remove_install(layout, "0.171.0", set())
    assert (outside / "bin" / "codex").exists()

    build_package(layout.installs / "latest-real", "0.172.0")
    remove_install(layout, "latest-real", set())
    assert (layout.installs / "latest-real").exists()

    remove_install(layout, "0.199.0", set())  # missing: no error


# The isolation check


@dataclass
class FakeCodexRun:
    """codex exec as measured on 0.171.0: user hooks print hook: lines on stderr."""

    control: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None
    isolated: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None
    write_output: bool = True
    calls: list[tuple[list[str], Path, dict[str, str], float, str]] = field(
        default_factory=list
    )

    def __call__(
        self,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
        text: str,
    ) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        self.calls.append((argv, cwd, dict(env), timeout, text))
        flagged = "--disable" in argv
        override = self.isolated if flagged else self.control
        if override is not None:
            return override(argv)
        if self.write_output:
            Path(argv[argv.index("-o") + 1]).write_text("ok", encoding="utf-8")
        hooks = (
            ""
            if flagged
            else "".join(
                f"hook: {name}\nhook: {name} Completed\n"
                for name in ("SessionStart", "UserPromptSubmit", "Stop")
            )
        )
        return completed(
            argv, 0, "ok\n", "OpenAI Codex v0.171.0\n" + hooks + "tokens used\n"
        )


def isolation_context(tmp_path: Path, run: FakeCodexRun) -> IsolationContext:
    project = tmp_path / "work" / "project"
    project.mkdir(parents=True)
    return IsolationContext(
        Path("/opt/codex/bin/codex"),
        tmp_path / "work",
        project,
        run,
        {"HOME": "/home/u", "PATH": selfcheck.SESSION_PATH},
    )


def test_isolation_argv() -> None:
    binary, project, out = Path("/b/codex"), Path("/p"), Path("/w/x.last")
    tail = [
        "-C",
        "/p",
        "-m",
        "gpt-6.1-sol",
        "-c",
        "model_reasoning_effort=low",
        "--sandbox",
        "read-only",
        "-o",
        "/w/x.last",
        "-",
    ]
    assert isolation_argv(binary, project, out, isolated=False) == [
        "/b/codex",
        "exec",
        *tail,
    ]
    assert isolation_argv(binary, project, out, isolated=True) == [
        "/b/codex",
        "exec",
        *ISOLATION_ARGS,
        *tail,
    ]


def test_isolation_passes_as_measured(tmp_path: Path) -> None:
    run = FakeCodexRun()
    ctx = isolation_context(tmp_path, run)
    assert check_isolation(ctx) == CheckResult("isolation", "pass", "")
    work = tmp_path / "work"
    assert [call[0] for call in run.calls] == [
        isolation_argv(ctx.binary, ctx.project, work / "control.last", isolated=False),
        isolation_argv(ctx.binary, ctx.project, work / "isolated.last", isolated=True),
    ]
    for argv, cwd, env, timeout, text in run.calls:
        assert "--json" not in argv
        assert cwd == ctx.project
        assert env == dict(ctx.env)
        assert timeout == 180
        assert text == "Reply with the single word ok."


def _raise(
    exc: BaseException,
) -> Callable[[list[str]], subprocess.CompletedProcess[str]]:
    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        raise exc

    return run


def _result(
    code: int, stderr: str, write: bool = True
) -> Callable[[list[str]], subprocess.CompletedProcess[str]]:
    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        if write:
            Path(argv[argv.index("-o") + 1]).write_text("ok", encoding="utf-8")
        return completed(argv, code, "ok\n", stderr)

    return run


@pytest.mark.parametrize(
    ("control", "isolated", "detail"),
    [
        (_raise(OSError("no codex")), None, "control:run"),
        (_raise(subprocess.TimeoutExpired("codex", 180)), None, "control:run"),
        (_result(1, "hook: SessionStart\n"), None, "control:exit"),
        (_result(0, "OpenAI Codex\ntokens used\n"), None, "control:no_hooks"),
        # Only a line that starts with "hook:" counts.
        (_result(0, "  hook: SessionStart\nnohook: x\n"), None, "control:no_hooks"),
        (None, _raise(OSError("no codex")), "isolated:run"),
        (None, _result(1, ""), "isolated:exit"),
        (None, _result(0, "hook: SessionStart\n"), "isolated:hooks"),
        (None, _result(0, "", write=False), "isolated:output"),
        (_raise(ValueError("surprise")), None, "isolation:error"),
    ],
)
def test_isolation_failures(
    tmp_path: Path,
    control: Callable[[list[str]], subprocess.CompletedProcess[str]] | None,
    isolated: Callable[[list[str]], subprocess.CompletedProcess[str]] | None,
    detail: str,
) -> None:
    run = FakeCodexRun(control=control, isolated=isolated)
    assert check_isolation(isolation_context(tmp_path, run)) == CheckResult(
        "isolation", "fail", detail
    )


def test_a_control_failure_stops_before_the_isolated_run(tmp_path: Path) -> None:
    run = FakeCodexRun(control=_result(0, "no hooks here\n"))
    check_isolation(isolation_context(tmp_path, run))
    assert len(run.calls) == 1


# The run: bootstrap


EXPECTED_CHECKS = [
    {"id": "FL-M4", "result": "skipped", "detail": ""},
    {"id": "S-M2", "result": "missing", "detail": ""},
    {"id": "S-M2b", "result": "missing", "detail": ""},
    {"id": "isolation", "result": "skipped", "detail": ""},
]


def test_the_bootstrap_installs_the_baseline_and_records_it(world: World) -> None:
    report = world.run()
    layout = world.layout
    binary = layout.installs / "0.160.0" / "bin" / "codex"
    assert report.current == "0.160.0"
    codex = world.codex()
    assert codex["current"] == {
        "version": "0.160.0",
        "sha256": selfcheck.sha256_file(binary),
        "path": str(binary),
        "at": NOW.isoformat(),
        "source": "bootstrap",
    }
    assert codex["previous"] is None
    assert layout.link.is_symlink()
    assert os.readlink(layout.link) == str(binary)
    assert (layout.root / "config.toml").read_text() == config_text("0.160.0")
    lock = tomllib.loads((layout.root / "mise.lock").read_text())
    assert lock["tools"]["codex"][0]["version"] == "0.160.0"
    assert not (layout.candidate / "config.toml").exists()
    assert not (layout.candidate / "mise.lock").exists()
    assert stat.S_IMODE(world.state_path.stat().st_mode) == 0o600


def test_after_the_bootstrap_the_candidate_is_reported_not_automated(
    world: World,
) -> None:
    report = world.run()
    assert report.status == "not_automated"
    assert report.reason is None
    assert report.latest == "0.171.0"
    assert report.candidate == "0.170.0"
    assert [check.__dict__ for check in report.checks] == EXPECTED_CHECKS
    assert report.reused is False
    assert report.notified == "sent"
    pins = load_codex_pins(world.hub)
    assert world.codex()["candidate"] == {
        "version": "0.170.0",
        "status": "not_automated",
        "sha256": None,
        "path": None,
        "pins_sha256": pins.sha256,
        "checks": EXPECTED_CHECKS,
        "warnings": list(pins.warn),
        "at": NOW.isoformat(),
    }
    assert [(event, kw["target"]) for event, kw in world.send.calls] == [
        ("selfcheck_failed", "codex@0.170.0")
    ]
    event, kwargs = world.send.calls[0]
    assert kwargs["settings"] is world.settings
    assert kwargs["orch_home"] == world.orch_home
    assert kwargs["home"] == world.home
    assert kwargs["now"] == NOW
    # Not installed: only the baseline is in the orch mise.
    assert sorted(
        p.name for p in world.layout.installs.iterdir() if not p.is_symlink()
    ) == ["0.160.0"]


def test_the_report_json(world: World) -> None:
    data = world.run().to_json()
    assert data == {
        "status": "not_automated",
        "reason": None,
        "current": "0.160.0",
        "latest": "0.171.0",
        "candidate": "0.170.0",
        "checks": EXPECTED_CHECKS,
        "warnings": list(load_codex_pins(world.hub).warn),
        "reused": False,
        "notified": "sent",
    }


def test_without_a_mise_path_the_bootstrap_is_unverified(world: World) -> None:
    world.settings = Config()
    report = world.run()
    assert (report.status, report.reason) == ("unverified", "mise_path")
    assert world.mise.calls == []
    assert not world.state_path.exists()
    assert world.send.calls == []


def test_a_regular_file_at_the_link_is_unverified(world: World) -> None:
    world.layout.link.parent.mkdir(parents=True)
    world.layout.link.write_text("not a link")
    report = world.run()
    assert (report.status, report.reason) == ("unverified", "link")
    assert world.layout.link.read_text() == "not a link"
    assert not world.state_path.exists()


def test_an_existing_link_is_replaced_by_the_bootstrap(world: World) -> None:
    world.layout.link.parent.mkdir(parents=True)
    world.layout.link.symlink_to("/somewhere/else/codex")
    world.run()
    assert os.readlink(world.layout.link) == str(
        world.layout.installs / "0.160.0" / "bin" / "codex"
    )


def test_a_failed_bootstrap_removes_the_install_and_records_nothing(
    world: World,
) -> None:
    world.commands.codesign_fail = {"rg"}
    report = world.run()
    assert (report.status, report.reason) == ("unverified", "codesign")
    assert not (world.layout.installs / "0.160.0").exists()
    assert not world.state_path.exists()
    assert not world.layout.link.exists()
    assert not world.layout.link.is_symlink()
    assert world.send.calls == []
    assert not (world.layout.root / "config.toml").exists()


def test_a_bootstrap_that_cannot_save_restores_the_link(world: World) -> None:
    world.orch_home.mkdir()
    (world.orch_home / "agents.json.lock").touch()
    world.layout.root.mkdir()
    world.layout.link.parent.mkdir()
    world.orch_home.chmod(0o555)
    try:
        report = world.run()
    finally:
        world.orch_home.chmod(0o755)
    assert (report.status, report.reason) == ("error", "state_write")
    assert not world.layout.link.is_symlink()
    assert not world.state_path.exists()
    assert world.send.calls == []


def test_the_claude_entry_is_kept(world: World) -> None:
    claude = {
        "current": {
            "version": "2.1.327",
            "sha256": "a" * 64,
            "path": "/x/2.1.327",
            "at": NOW.isoformat(),
            "source": "apply",
        },
        "previous": None,
        "candidate": None,
    }
    selfcheck.save_agents_state(
        world.state_path, {"format": 1, "agents": {"claude": claude}}
    )
    world.run()
    assert world.state()["agents"]["claude"] == claude  # type: ignore[index]


# The run: candidates


def test_with_a_record_there_is_no_bootstrap(world: World) -> None:
    world.record_current()
    report = world.run()
    assert report.status == "not_automated"
    assert report.current == "0.160.0"
    assert world.mise.calls == []
    assert not world.layout.link.exists()
    assert [url for url, _ in world.fetch.calls] == [
        RELEASES.format(1),
        RELEASES.format(2),
        RELEASES.format(3),
    ]
    assert all(limit == 32 << 20 for _, limit in world.fetch.calls)


def test_only_three_pages_are_read(world: World) -> None:
    world.record_current()
    page = [release("rust-v0.150.0", "2026-10-01T00:00:00Z")]
    world.publish(PAGE_ONE, PAGE_TWO, page, page)
    world.run()
    assert [url for url, _ in world.fetch.calls] == [
        RELEASES.format(n) for n in (1, 2, 3)
    ]


def test_young_held_and_prerelease_versions_are_skipped(world: World) -> None:
    world.record_current()
    world.settings = Config(mise_path=MISE, selfcheck_hold=("codex@0.170.0",))
    report = world.run()
    assert (report.latest, report.candidate) == ("0.171.0", "0.169.1")


def test_min_age_follows_the_setting(world: World) -> None:
    world.record_current()
    world.settings = Config(mise_path=MISE, selfcheck_min_age_days=1)
    assert world.run().candidate == "0.171.0"


def test_malformed_and_draft_items_are_ignored(world: World) -> None:
    world.record_current()
    page = [
        "not a dict",
        release("rust-v0.180.0", "2026-11-01T00:00:00Z", draft=True),
        release("rust-v0.181.0", "2026-11-01T00:00:00+00:00"),
        release("rust-v0.182.0", "yesterday"),
        release("v0.183.0", "2026-11-01T00:00:00Z"),
        release("rust-v0.184.0-alpha.1", "2026-11-01T00:00:00Z"),
        {
            "tag_name": "rust-v0.185.0",
            "prerelease": 0,
            "draft": False,
            "published_at": "2026-11-01T00:00:00Z",
        },
        {
            "tag_name": "rust-v0.186.0",
            "draft": False,
            "published_at": "2026-11-01T00:00:00Z",
        },
        release("rust-v0.165.0", "2026-11-01T00:00:00Z"),
    ]
    world.publish(page)
    report = world.run()
    assert (report.latest, report.candidate) == ("0.165.0", "0.165.0")


def test_the_earliest_date_of_a_repeated_version_counts(world: World) -> None:
    world.record_current()
    world.publish(
        [release("rust-v0.171.0", "2026-11-20T00:44:47Z")],
        [release("rust-v0.171.0", "2026-11-01T00:00:00Z")],
    )
    assert world.run().candidate == "0.171.0"


def test_up_to_date_and_unknown(world: World) -> None:
    world.record_current("0.171.0")
    report = world.run()
    assert (report.status, report.reason, report.latest) == (
        "up_to_date",
        "latest",
        "0.171.0",
    )

    world.record_current("0.169.1")
    world.publish([release("rust-v0.171.0", "2026-11-20T00:44:47Z")])
    report = world.run()
    assert (report.status, report.reason) == ("up_to_date", "no_candidate")

    world.publish(
        [release("rust-v0.173.0-alpha.4", "2026-11-01T00:00:00Z", prerelease=True)]
    )
    report = world.run()
    assert (report.status, report.reason) == ("unknown", "latest")

    world.fetch.bodies[RELEASES.format(1)] = b'{"message": "Server Error"}'
    report = world.run()
    assert (report.status, report.reason) == ("unknown", "releases")

    world.fetch.bodies[RELEASES.format(1)] = b"<html>"
    report = world.run()
    assert (report.status, report.reason) == ("unknown", "releases")

    world.fetch.errors[RELEASES.format(1)] = selfcheck.FetchError("504")
    report = world.run()
    assert (report.status, report.reason) == ("unknown", "fetch")
    assert world.codex()["candidate"] is None
    assert world.send.calls == []


def test_a_fetch_error_on_a_later_page_is_unknown(world: World) -> None:
    world.record_current()
    world.fetch.errors[RELEASES.format(2)] = selfcheck.FetchError("504")
    report = world.run()
    assert (report.status, report.reason) == ("unknown", "fetch")


def test_the_same_candidate_is_reused_without_saving_or_notifying(world: World) -> None:
    world.record_current()
    first = world.run()
    saved = world.state_path.read_bytes()
    world.send.calls.clear()
    second = world.run()
    assert second.status == "not_automated"
    assert second.reused is True
    assert second.candidate == "0.170.0"
    assert second.checks == first.checks
    assert second.warnings == first.warnings
    assert second.notified is None
    assert world.state_path.read_bytes() == saved
    assert world.send.calls == []


def test_a_pins_change_records_the_candidate_again(world: World) -> None:
    world.record_current()
    world.run()
    pins = world.hub / "config" / "pins.toml"
    pins.write_text(pins.read_text() + "\n# changed\n")
    world.send.calls.clear()
    report = world.run()
    assert report.reused is False
    assert len(world.send.calls) == 1
    assert (
        world.codex()["candidate"]["pins_sha256"]
        == hashlib.sha256(  # type: ignore[index]
            pins.read_bytes()
        ).hexdigest()
    )


def test_all_automated_pins_are_not_supported_yet(world: World) -> None:
    text = (REPO / "config" / "pins.toml").read_text(encoding="utf-8")
    head, codex = text.split("[codex]", 1)
    codex = codex.replace(
        '  { id = "S-M2", what', '  # { id = "S-M2", what', 1
    ).replace('  { id = "S-M2b", what', '  # { id = "S-M2b", what', 1)
    (world.hub / "config" / "pins.toml").write_text(head + "[codex]" + codex)
    assert load_codex_pins(world.hub).blocking == ("FL-M4", "isolation")
    world.record_current()
    before = world.state_path.read_bytes()
    report = world.run()
    assert (report.status, report.reason) == ("error", "unsupported")
    assert world.state_path.read_bytes() == before
    assert world.send.calls == []


def test_a_failing_notification_is_reported(world: World) -> None:
    world.record_current()
    world.send.raises = True
    report = world.run()
    assert report.notified == "failed"
    assert world.codex()["candidate"]["version"] == "0.170.0"  # type: ignore[index]


# The run: errors and the lock


def test_another_selfcheck_holds_the_lock(world: World) -> None:
    world.orch_home.mkdir()
    with (world.orch_home / "agents.json.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = world.run()
    assert report.status == "busy"
    assert world.mise.calls == []
    assert world.fetch.calls == []


BAD_CODEX_STATE = json.dumps(
    {"format": 1, "agents": {"codex": {"current": {"version": "0.160.0"}}}}
)


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        (lambda w: (w.hub / "config" / "pins.toml").unlink(), "pins"),
        (lambda w: setattr(w, "machine", "ppc"), "platform"),
        (
            lambda w: (
                w.orch_home.mkdir(),
                w.state_path.write_text(BAD_CODEX_STATE),
            ),
            "state",
        ),
    ],
)
def test_unreadable_inputs_are_errors(
    world: World, setup: Callable[[World], object], reason: str
) -> None:
    setup(world)
    report = world.run()
    assert (report.status, report.reason) == ("error", reason)
    assert world.mise.calls == []


def test_an_unexpected_exception_is_internal_and_releases_the_lock(
    world: World,
) -> None:
    world.record_current()
    world.fetch.errors[RELEASES.format(1)] = ValueError("surprise")
    report = world.run()
    assert (report.status, report.reason) == ("error", "internal")
    with (world.orch_home / "agents.json.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_an_unexpected_os_error_is_io(world: World) -> None:
    world.record_current()
    world.fetch.errors[RELEASES.format(1)] = PermissionError("denied")
    report = world.run()
    assert (report.status, report.reason) == ("error", "io")


def test_a_candidate_that_cannot_be_saved_is_a_state_write_error(world: World) -> None:
    world.record_current()
    (world.orch_home / "agents.json.lock").touch()
    world.orch_home.chmod(0o555)
    try:
        report = world.run()
    finally:
        world.orch_home.chmod(0o755)
    assert (report.status, report.reason) == ("error", "state_write")
    assert world.send.calls == []


# agents.json codex validation


def _valid_entry() -> dict[str, object]:
    return {
        "version": "0.160.0",
        "sha256": "a" * 64,
        "path": "/x/codex",
        "at": NOW.isoformat(),
        "source": "bootstrap",
    }


@pytest.mark.parametrize(
    "codex",
    [
        {"current": {"version": "0.160.0"}},
        {"current": {**_valid_entry(), "source": "manual"}},
        {"previous": {**_valid_entry(), "sha256": None}},
        {"current": "0.160.0"},
        {"candidate": {"version": "0.170.0", "status": "maybe"}},
        "not a table",
    ],
)
def test_a_malformed_codex_entry_is_refused(tmp_path: Path, codex: object) -> None:
    path = tmp_path / "agents.json"
    path.write_text(json.dumps({"format": 1, "agents": {"codex": codex}}))
    with pytest.raises(SelfcheckError):
        selfcheck.load_agents_state(path)


def test_a_valid_codex_entry_loads(tmp_path: Path) -> None:
    path = tmp_path / "agents.json"
    data = {
        "format": 1,
        "agents": {
            "codex": {
                "current": _valid_entry(),
                "previous": None,
                "candidate": {
                    "version": "0.170.0",
                    "status": "not_automated",
                    "sha256": None,
                    "path": None,
                    "pins_sha256": "b" * 64,
                    "checks": EXPECTED_CHECKS,
                    "warnings": [],
                    "at": NOW.isoformat(),
                },
            }
        },
    }
    path.write_text(json.dumps(data))
    assert selfcheck.load_agents_state(path) == data


# The version in use


def test_check_current_codex(world: World) -> None:
    binary = world.record_current()
    check = lambda path: check_current_codex(  # noqa: E731
        orch_home=world.orch_home, codex_path=path, command=world.commands
    )
    world.layout.link.parent.mkdir(parents=True)
    world.layout.link.symlink_to(binary)
    assert check(world.layout.link) == CurrentCheck("ok", None, "0.160.0")
    assert ([str(binary), "--version"], 30) in world.commands.calls

    world.commands.version_output[str(binary)] = "codex-cli 0.171.0\n"
    assert check(binary) == CurrentCheck("mismatch", "version", "0.160.0")
    world.commands.version_output.clear()

    other = world.root / "other-codex"
    other.write_bytes(binary.read_bytes())
    assert check(other) == CurrentCheck("mismatch", "path", "0.160.0")

    binary.write_bytes(MAGIC + b"codex-cli 0.160.0 tampered")
    assert check(binary) == CurrentCheck("mismatch", "sha256", "0.160.0")

    binary.unlink()
    assert check(binary) == CurrentCheck("mismatch", "missing", "0.160.0")


def test_check_current_codex_without_a_record(tmp_path: Path) -> None:
    assert check_current_codex(
        orch_home=tmp_path, codex_path=Path("/x"), command=FakeCommands()
    ) == CurrentCheck("no_record", None, None)
    selfcheck.save_agents_state(
        tmp_path / "agents.json",
        {"format": 1, "agents": {"claude": {"current": None}}},
    )
    assert check_current_codex(
        orch_home=tmp_path, codex_path=Path("/x"), command=FakeCommands()
    ) == CurrentCheck("no_record", None, None)
    (tmp_path / "agents.json").write_text("{")
    assert check_current_codex(
        orch_home=tmp_path, codex_path=Path("/x"), command=FakeCommands()
    ) == CurrentCheck("error", "state", None)


def test_the_default_runner_passes_input(tmp_path: Path) -> None:
    result = selfcheck_codex.run_with_input(
        ["/bin/cat"], tmp_path, {"PATH": "/usr/bin:/bin"}, 10, "hello"
    )
    assert (result.returncode, result.stdout) == (0, "hello")
