"""orch selfcheck for Codex: the orch mise, the bootstrap, candidates and isolation.

Nothing here uses the network or runs the real mise, codex or codesign. The fake mise
follows FL-M7 (mise 2026.10.2): `mise lock --global` writes no entry for 0.160.0 and
`mise install` then writes the entry of the current platform.
"""

import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orch import notify, proxy, sandbox, selfcheck, selfcheck_codex
from orch.config import Config
from orch.lanes.codex import ISOLATION_ARGS
from orch.selfcheck import CheckResult, CurrentCheck, NotFound, SelfcheckError
from orch.selfcheck_codex import (
    CodexLayout,
    CodexPlatform,
    IsolationContext,
    Prepared,
    SandboxContext,
    check_current_codex,
    check_isolation,
    check_sandbox,
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
    sandbox_probe_argv,
    sandbox_probe_prompt,
)

REPO = Path(__file__).resolve().parents[2]
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


def model_entry(
    slug: str,
    priority: int,
    *,
    visibility: str = "list",
    api: object = True,
    efforts: Sequence[str] = ("low", "medium", "high", "xhigh"),
) -> dict[str, object]:
    """One item of codex debug models as measured on 0.171.0 (fields trimmed)."""
    return {
        "slug": slug,
        "display_name": slug,
        "visibility": visibility,
        "supported_in_api": api,
        "priority": priority,
        "supported_reasoning_levels": [
            {"effort": effort, "description": effort} for effort in efforts
        ],
        "default_reasoning_level": "medium",
    }


def models_json(*entries: dict[str, object]) -> str:
    return json.dumps({"models": list(entries)}, indent=2) + "\n"


# The default answer picks the default model, so nothing but the candidate changes.
DEFAULT_MODEL = "gpt-6.1-sol"
DEFAULT_MODELS = models_json(
    model_entry("gpt-6.1-sol", 1), model_entry("gpt-6.1-terra", 2)
)


@dataclass
class FakeCommands:
    """codesign, --version and debug models."""

    codesign_fail: set[str] = field(default_factory=set)
    codesign_raises: bool = False
    version_output: dict[str, str] = field(default_factory=dict)
    models_output: dict[str, str] = field(default_factory=dict)
    models_default: str = DEFAULT_MODELS
    models_exit: int = 0
    models_raises: BaseException | None = None
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
        if list(argv[1:]) == ["debug", "models"]:
            if self.models_raises is not None:
                raise self.models_raises
            body = self.models_output.get(argv[0], self.models_default)
            return completed(argv, self.models_exit, body)
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
    probe: FakeProbe = field(default_factory=lambda: FakeProbe())
    isolation: FakeCodexRun = field(default_factory=lambda: FakeCodexRun())
    proxy: FakeProxy = field(default_factory=lambda: FakeProxy())

    def session(
        self,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
        text: str,
    ) -> subprocess.CompletedProcess[str]:
        """The probe runs under sandbox-exec; isolation runs codex directly."""
        self.probe.proxy = self.proxy
        if "danger-full-access" in argv:
            return self.probe(argv, cwd, env, timeout, text)
        return self.isolation(argv, cwd, env, timeout, text)

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
            "session": self.session,
            "proxy_factory": self.proxy,
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
    (home / ".orch" / "secrets").mkdir(parents=True)
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
    assert len(pins.warn) == 7
    assert all(isinstance(item, str) for item in pins.warn)


def test_every_blocking_id_has_a_check() -> None:
    pins = load_codex_pins(REPO)
    assert selfcheck_codex.CODEX_AUTOMATED_IDS == frozenset(
        {"FL-M4", "S-M2", "S-M2b", "isolation"}
    )
    assert set(pins.blocking) <= selfcheck_codex.CODEX_AUTOMATED_IDS


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
        "gpt-6.2-sol",
    )


def test_isolation_argv() -> None:
    binary, project, out = Path("/b/codex"), Path("/p"), Path("/w/x.last")
    tail = [
        "-C",
        "/p",
        "-m",
        "gpt-6.2-sol",
        "-c",
        "model_reasoning_effort=low",
        "--sandbox",
        "read-only",
        "-o",
        "/w/x.last",
        "-",
    ]
    assert isolation_argv(
        binary, project, out, isolated=False, model="gpt-6.2-sol"
    ) == [
        "/b/codex",
        "exec",
        *tail,
    ]
    assert isolation_argv(binary, project, out, isolated=True, model="gpt-6.2-sol") == [
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
        isolation_argv(
            ctx.binary,
            ctx.project,
            work / "control.last",
            isolated=False,
            model=ctx.model,
        ),
        isolation_argv(
            ctx.binary,
            ctx.project,
            work / "isolated.last",
            isolated=True,
            model=ctx.model,
        ),
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


# The S-M2 and S-M2b checks: one codex run under the profile behind the proxy

PORT = 41000
PROBE_HOSTS = frozenset({"chatgpt.com", "ab.chatgpt.com"})
CANARY_NAME = "a1b2c3d4e5f60718"
CANARY_TOKEN = "c0ffee" * 4
WRITE_TOKEN = "beefcafe12345678"
PROMPT_HEAD = (
    "Run each of these four shell commands exactly once, one at a time, in this "
    "order, even if one fails. Then reply with the single word done.\n"
)


def event(
    allowed: bool, host: str, reason: str = "allowed", port: int | None = 443
) -> proxy.ProxyEvent:
    return proxy.ProxyEvent(
        at="2026-11-24T00:00:00+00:00",
        allowed=allowed,
        host=host,
        port=port,
        reason=reason,
    )


@dataclass
class FakeProxy:
    """A factory and the proxy it makes. alive() turns false after stop()."""

    port: int = PORT
    start_error: BaseException | None = None
    alive_at_end: bool = True
    recorded: list[proxy.ProxyEvent] = field(default_factory=list)
    created: list[tuple[frozenset[str], Path]] = field(default_factory=list)
    started: int = 0
    stopped: int = 0

    def __call__(self, hosts: frozenset[str], log_path: Path) -> FakeProxy:
        self.created.append((hosts, log_path))
        return self

    def start(self) -> int:
        if self.start_error is not None:
            raise self.start_error
        self.started += 1
        self.stopped = 0
        return self.port

    def alive(self) -> bool:
        return self.alive_at_end and self.stopped == 0

    def stop(self) -> None:
        self.stopped += 1

    def events(self) -> tuple[proxy.ProxyEvent, ...]:
        return tuple(self.recorded)


def command_item(command: str, exit_code: object, output: str = "") -> str:
    return json.dumps(
        {
            "type": "item.completed",
            "item": {
                "id": "item_1",
                "type": "command_execution",
                "command": command,
                "aggregated_output": output,
                "exit_code": exit_code,
                "status": "completed" if exit_code == 0 else "failed",
            },
        }
    )


@dataclass
class FakeProbe:
    """codex exec --json under the orch profile, as measured on 0.160.0.

    It reads the canary path and the write token from the prompt, runs the four
    requested commands the way the profile and the proxy answer them, and records
    what the proxy saw.
    """

    proxy: FakeProxy | None = None
    skip: set[str] = field(default_factory=set)
    cat_exit: object = 1
    read_canary: bool = False
    stderr_token: bool = False
    write_file: bool = True
    write_content: str | None = None
    proxied_exit: object = 56
    direct_exit: object = 6
    allowed_hosts: tuple[str, ...] = ("chatgpt.com", "ab.chatgpt.com")
    deny_probe_host: bool = True
    returncode: int = 0
    raises: BaseException | None = None
    noise: bool = True
    calls: list[tuple[list[str], Path, dict[str, str], float, str]] = field(
        default_factory=list
    )
    canary_seen: list[tuple[bool, int, str]] = field(default_factory=list)
    git_seen: list[bool] = field(default_factory=list)

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
        self.git_seen.append((cwd / ".git").is_dir())
        canary = Path(text.splitlines()[1].removeprefix("1. cat "))
        token = text.splitlines()[2].removeprefix("2. echo ").split()[0]
        if canary.is_file():
            self.canary_seen.append(
                (
                    True,
                    stat.S_IMODE(canary.stat().st_mode),
                    canary.read_text(encoding="utf-8"),
                )
            )
        else:
            self.canary_seen.append((False, 0, ""))
        if self.raises is not None:
            raise self.raises
        lines = []
        if self.noise:
            lines += [
                json.dumps({"type": "thread.started", "thread_id": "t"}),
                json.dumps(
                    {
                        "type": "item.started",
                        "item": {
                            "type": "command_execution",
                            "command": f"cat {canary}",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": f"cat {canary}"},
                    }
                ),
                "not json",
            ]
        if "cat" not in self.skip:
            output = (
                canary.read_text(encoding="utf-8")
                if self.read_canary
                else f"cat: {canary}: Operation not permitted\n"
            )
            lines.append(
                command_item(f"/bin/zsh -lc 'cat {canary}'", self.cat_exit, output)
            )
        if "write" not in self.skip:
            if self.write_file:
                (cwd / "probe.txt").write_text(
                    (self.write_content if self.write_content is not None else token)
                    + "\n",
                    encoding="utf-8",
                )
            lines.append(command_item(f"/bin/zsh -lc 'echo {token} > probe.txt'", 0))
        if self.proxy is not None:
            for host in self.allowed_hosts:
                self.proxy.recorded.append(event(True, host))
        if "proxied" not in self.skip:
            if self.proxy is not None and self.deny_probe_host:
                self.proxy.recorded.append(event(False, "example.com", "host"))
            lines.append(
                command_item(
                    "/bin/zsh -lc 'curl -sS -m 10 -o /dev/null https://example.com'",
                    self.proxied_exit,
                    "curl: (56) CONNECT tunnel failed, response 403\n",
                )
            )
        if "direct" not in self.skip:
            lines.append(
                command_item(
                    "/bin/zsh -lc \"curl --noproxy '*' -sS -m 10 -o /dev/null "
                    'https://example.com"',
                    self.direct_exit,
                    "curl: (6) Could not resolve host: example.com\n",
                )
            )
        lines.append(json.dumps({"type": "turn.completed"}))
        stderr = "OpenAI Codex v0.160.0\n" + (CANARY_TOKEN if self.stderr_token else "")
        return completed(argv, self.returncode, "\n".join(lines) + "\n", stderr)


def sandbox_context(
    tmp_path: Path,
    probe: FakeProbe,
    px: FakeProxy,
    *,
    secrets: bool = True,
) -> SandboxContext:
    home = tmp_path / "home"
    if secrets:
        (home / ".orch" / "secrets").mkdir(parents=True)
    else:
        home.mkdir()
    workdir = tmp_path / "work"
    project = workdir / "project"
    project.mkdir(parents=True)
    probe.proxy = px
    return SandboxContext(
        binary=Path("/opt/codex/bin/codex"),
        workdir=workdir,
        project=project,
        home=home,
        hosts=PROBE_HOSTS,
        run=probe,
        env={"HOME": str(home), "PATH": selfcheck.SESSION_PATH},
        canary_name=CANARY_NAME,
        canary_token=CANARY_TOKEN,
        write_token=WRITE_TOKEN,
        proxy_factory=px,
        model="gpt-6.2-sol",
    )


def canary_of(ctx: SandboxContext) -> Path:
    return ctx.home / ".orch" / "secrets" / f"selfcheck-{CANARY_NAME}.canary"


def test_sandbox_probe_argv() -> None:
    assert sandbox_probe_argv(Path("/b/codex"), Path("/p"), "gpt-6.2-sol") == [
        "/b/codex",
        "exec",
        "--json",
        *ISOLATION_ARGS,
        "-C",
        "/p",
        "-m",
        "gpt-6.2-sol",
        "-c",
        "model_reasoning_effort=low",
        "--sandbox",
        "danger-full-access",
        "-",
    ]


def test_sandbox_probe_prompt() -> None:
    assert sandbox_probe_prompt(Path("/h/.orch/secrets/x.canary"), "tok") == (
        PROMPT_HEAD
        + "1. cat /h/.orch/secrets/x.canary\n"
        + "2. echo tok > probe.txt\n"
        + "3. curl -sS -m 10 -o /dev/null https://example.com\n"
        + "4. curl --noproxy '*' -sS -m 10 -o /dev/null https://example.com\n"
    )


def test_the_sandbox_checks_pass_as_measured(tmp_path: Path) -> None:
    probe, px = FakeProbe(), FakeProxy()
    ctx = sandbox_context(tmp_path, probe, px)
    assert check_sandbox(ctx) == (
        CheckResult("S-M2", "pass", ""),
        CheckResult("S-M2b", "pass", ""),
    )
    assert px.created == [(PROBE_HOSTS, ctx.workdir / "proxy.log")]
    assert (px.started, px.stopped >= 1) == (1, True)
    [(argv, cwd, env, timeout, text)] = probe.calls
    tmpdir = ctx.workdir / "tmp"
    assert tmpdir.is_dir()
    assert argv == sandbox.wrap_codex(
        sandbox_probe_argv(ctx.binary, ctx.project, ctx.model),
        executable=sandbox.SANDBOX_EXEC,
        worktree=ctx.project,
        tmpdir=tmpdir,
        home=ctx.home,
        codex_home=ctx.home / ".codex",
        port=PORT,
    )
    assert cwd == ctx.project
    assert env == {
        **ctx.env,
        "TMPDIR": str(tmpdir),
        **proxy.proxy_env(PORT),
    }
    assert timeout == 300
    assert text == sandbox_probe_prompt(canary_of(ctx), WRITE_TOKEN)
    # The canary existed during the run, private, and is gone afterwards.
    assert probe.canary_seen == [(True, 0o600, CANARY_TOKEN + "\n")]
    assert not canary_of(ctx).exists()
    assert list((ctx.home / ".orch" / "secrets").iterdir()) == []


def _no_secrets(ctx: SandboxContext) -> None:
    (ctx.home / ".orch" / "secrets").rmdir()


def _secrets_link(ctx: SandboxContext) -> None:
    real = ctx.home / "real-secrets"
    real.mkdir()
    (ctx.home / ".orch" / "secrets").rmdir()
    (ctx.home / ".orch" / "secrets").symlink_to(real)


@pytest.mark.parametrize("setup", [_no_secrets, _secrets_link])
def test_without_a_secrets_directory_nothing_runs(
    tmp_path: Path, setup: Callable[[SandboxContext], None]
) -> None:
    probe, px = FakeProbe(), FakeProxy()
    ctx = sandbox_context(tmp_path, probe, px)
    setup(ctx)
    assert check_sandbox(ctx) == (
        CheckResult("S-M2", "fail", "probe:canary"),
        CheckResult("S-M2b", "fail", "probe:canary"),
    )
    assert probe.calls == []
    assert px.created == []


def test_an_existing_canary_name_is_refused_and_kept(tmp_path: Path) -> None:
    probe, px = FakeProbe(), FakeProxy()
    ctx = sandbox_context(tmp_path, probe, px)
    canary_of(ctx).write_text("someone else's\n", encoding="utf-8")
    assert check_sandbox(ctx)[0] == CheckResult("S-M2", "fail", "probe:canary")
    assert probe.calls == []
    assert canary_of(ctx).read_text(encoding="utf-8") == "someone else's\n"


def test_a_proxy_that_cannot_start_runs_nothing_and_removes_the_canary(
    tmp_path: Path,
) -> None:
    probe, px = FakeProbe(), FakeProxy(start_error=proxy.ProxyError("bind"))
    ctx = sandbox_context(tmp_path, probe, px)
    assert check_sandbox(ctx) == (
        CheckResult("S-M2", "fail", "probe:proxy"),
        CheckResult("S-M2b", "fail", "probe:proxy"),
    )
    assert probe.calls == []
    assert not canary_of(ctx).exists()


@pytest.mark.parametrize(
    ("probe", "detail"),
    [
        (FakeProbe(raises=OSError("no codex")), "probe:run"),
        (FakeProbe(raises=subprocess.TimeoutExpired("codex", 300)), "probe:run"),
        (FakeProbe(raises=ValueError("bad")), "probe:run"),
        (FakeProbe(returncode=1), "probe:exit"),
        (FakeProbe(raises=RuntimeError("surprise")), "probe:error"),
    ],
)
def test_a_run_that_does_not_finish_fails_both_and_cleans_up(
    tmp_path: Path, probe: FakeProbe, detail: str
) -> None:
    px = FakeProxy()
    ctx = sandbox_context(tmp_path, probe, px)
    assert check_sandbox(ctx) == (
        CheckResult("S-M2", "fail", detail),
        CheckResult("S-M2b", "fail", detail),
    )
    assert px.stopped >= 1
    assert not canary_of(ctx).exists()


@pytest.mark.parametrize(
    ("probe", "s_m2", "s_m2b"),
    [
        (FakeProbe(skip={"cat"}), "s_m2:not_run", "pass"),
        (FakeProbe(cat_exit=0, read_canary=True), "s_m2:read", "pass"),
        (FakeProbe(read_canary=True), "s_m2:read", "pass"),
        (FakeProbe(stderr_token=True), "s_m2:read", "pass"),
        (FakeProbe(cat_exit=0), "s_m2:read", "pass"),
        (FakeProbe(cat_exit=None), "s_m2:read", "pass"),
        (FakeProbe(cat_exit=True), "s_m2:read", "pass"),
        (FakeProbe(cat_exit="1"), "s_m2:read", "pass"),
        (FakeProbe(skip={"write"}), "s_m2:write", "pass"),
        (FakeProbe(write_file=False), "s_m2:write", "pass"),
        (FakeProbe(write_content="other"), "s_m2:write", "pass"),
        (
            FakeProbe(allowed_hosts=("chatgpt.com", "evil.example")),
            "pass",
            "s_m2b:host",
        ),
        (FakeProbe(allowed_hosts=()), "pass", "s_m2b:proxy_unused"),
        (FakeProbe(skip={"proxied"}), "pass", "s_m2b:not_run"),
        (FakeProbe(skip={"direct"}), "pass", "s_m2b:not_run"),
        (FakeProbe(proxied_exit=0), "pass", "s_m2b:proxied"),
        (FakeProbe(proxied_exit=None), "pass", "s_m2b:proxied"),
        (FakeProbe(deny_probe_host=False), "pass", "s_m2b:proxied"),
        (FakeProbe(direct_exit=0), "pass", "s_m2b:direct"),
        (FakeProbe(direct_exit=False), "pass", "s_m2b:direct"),
        (FakeProbe(skip={"cat", "direct"}), "s_m2:not_run", "s_m2b:not_run"),
    ],
)
def test_each_check_names_its_first_failure(
    tmp_path: Path, probe: FakeProbe, s_m2: str, s_m2b: str
) -> None:
    ctx = sandbox_context(tmp_path, probe, FakeProxy())

    def result(check_id: str, detail: str) -> CheckResult:
        return CheckResult(
            check_id,
            "pass" if detail == "pass" else "fail",
            "" if detail == "pass" else detail,
        )

    assert check_sandbox(ctx) == (result("S-M2", s_m2), result("S-M2b", s_m2b))
    assert not canary_of(ctx).exists()


def test_a_proxy_that_stopped_during_the_run_fails_s_m2b(tmp_path: Path) -> None:
    ctx = sandbox_context(tmp_path, FakeProbe(), FakeProxy(alive_at_end=False))
    assert check_sandbox(ctx) == (
        CheckResult("S-M2", "pass", ""),
        CheckResult("S-M2b", "fail", "s_m2b:proxy_stopped"),
    )


def test_the_probe_host_counts_only_as_a_denial(tmp_path: Path) -> None:
    probe = FakeProbe(deny_probe_host=False)
    px = FakeProxy(recorded=[event(True, "example.com")])
    ctx = sandbox_context(tmp_path, probe, px)
    assert check_sandbox(ctx)[1] == CheckResult("S-M2b", "fail", "s_m2b:host")


def test_a_probe_file_that_is_a_directory_fails_s_m2(tmp_path: Path) -> None:
    probe = FakeProbe(write_file=False)
    ctx = sandbox_context(tmp_path, probe, FakeProxy())
    (ctx.project / "probe.txt").mkdir()
    assert check_sandbox(ctx)[0] == CheckResult("S-M2", "fail", "s_m2:write")


# The run: bootstrap


PASSED_CHECKS = [
    {"id": "FL-M4", "result": "pass", "detail": ""},
    {"id": "S-M2", "result": "pass", "detail": ""},
    {"id": "S-M2b", "result": "pass", "detail": ""},
    {"id": "isolation", "result": "pass", "detail": ""},
]
# A blocking id without a check function keeps the candidate not_automated.
NOT_AUTOMATED_CHECKS = [
    {"id": "FL-M4", "result": "skipped", "detail": ""},
    {"id": "S-M2", "result": "skipped", "detail": ""},
    {"id": "S-M2b", "result": "skipped", "detail": ""},
    {"id": "X-NEW", "result": "missing", "detail": ""},
    {"id": "isolation", "result": "skipped", "detail": ""},
]


def add_unknown_blocking_id(world: World) -> None:
    pins = world.hub / "config" / "pins.toml"
    text = pins.read_text(encoding="utf-8")
    head, codex = text.split("[codex]", 1)
    old = '  { id = "isolation", what'
    assert codex.count(old) == 1
    codex = codex.replace(old, '  { id = "X-NEW", what = "new" },\n' + old, 1)
    pins.write_text(head + "[codex]" + codex, encoding="utf-8")
    blocking = load_codex_pins(world.hub).blocking
    assert blocking == ("FL-M4", "S-M2", "S-M2b", "X-NEW", "isolation")


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
    # The candidate copy now holds the next candidate, prepared after the bootstrap.
    assert (layout.candidate / "config.toml").read_text() == config_text("0.170.0")
    assert stat.S_IMODE(world.state_path.stat().st_mode) == 0o600


def installed(world: World) -> list[str]:
    return sorted(p.name for p in world.layout.installs.iterdir() if not p.is_symlink())


def test_after_the_bootstrap_a_candidate_that_passes_is_ready(world: World) -> None:
    report = world.run()
    assert report.status == "ready"
    assert report.reason is None
    assert report.latest == "0.171.0"
    assert report.candidate == "0.170.0"
    assert [check.__dict__ for check in report.checks] == PASSED_CHECKS
    assert report.reused is False
    assert report.notified == "sent"
    pins = load_codex_pins(world.hub)
    binary = world.layout.installs / "0.170.0" / "bin" / "codex"
    assert world.codex()["candidate"] == {
        "version": "0.170.0",
        "model": DEFAULT_MODEL,
        "status": "ready",
        "sha256": selfcheck.sha256_file(binary),
        "path": str(binary),
        "pins_sha256": pins.sha256,
        "checks": PASSED_CHECKS,
        "warnings": list(pins.warn),
        "at": NOW.isoformat(),
    }
    assert [(event, kw["target"]) for event, kw in world.send.calls] == [
        ("selfcheck_ready", "codex@0.170.0")
    ]
    event, kwargs = world.send.calls[0]
    assert kwargs["settings"] is world.settings
    assert kwargs["orch_home"] == world.orch_home
    assert kwargs["home"] == world.home
    assert kwargs["now"] == NOW
    # Installed beside the baseline; the link still points at the baseline.
    assert installed(world) == ["0.160.0", "0.170.0"]
    assert world.layout.link.resolve() == (
        world.layout.installs / "0.160.0" / "bin" / "codex"
    )
    assert world.codex()["current"]["version"] == "0.160.0"  # type: ignore[index]


def test_the_report_json(world: World) -> None:
    data = world.run().to_json()
    assert data == {
        "status": "ready",
        "reason": None,
        "current": "0.160.0",
        "latest": "0.171.0",
        "candidate": "0.170.0",
        "checks": PASSED_CHECKS,
        "warnings": list(load_codex_pins(world.hub).warn),
        "reused": False,
        "notified": "sent",
        "model": DEFAULT_MODEL,
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


def fail_state_write(monkeypatch: pytest.MonkeyPatch) -> None:
    # A replaced save instead of a read-only directory, which root could still
    # write (m-selfcheck-restore, #656).
    def fail(path: Path, state: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(selfcheck_codex, "save_agents_state", fail)


def test_a_bootstrap_that_cannot_save_restores_the_link(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    fail_state_write(monkeypatch)
    report = world.run()
    assert (report.status, report.reason) == ("error", "state_write")
    assert not world.layout.link.is_symlink()
    assert not world.layout.link.exists()
    assert not world.state_path.exists()
    assert world.send.calls == []


def test_a_bootstrap_that_cannot_save_restores_an_existing_link(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.layout.link.parent.mkdir(parents=True)
    world.layout.link.symlink_to("/somewhere/else/codex")
    fail_state_write(monkeypatch)
    report = world.run()
    assert (report.status, report.reason) == ("error", "state_write")
    assert world.layout.link.is_symlink()
    assert os.readlink(world.layout.link) == "/somewhere/else/codex"
    assert not [p for p in world.layout.link.parent.iterdir() if p != world.layout.link]
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
    assert report.status == "ready"
    assert report.current == "0.160.0"
    # Only the candidate is prepared, in the candidate copy.
    assert world.mise.calls
    assert all(cwd == world.layout.candidate for _, cwd, _, _ in world.mise.calls)
    assert not world.layout.link.exists()
    assert [url for url, _ in world.fetch.calls] == [
        RELEASES.format(1),
        RELEASES.format(2),
        RELEASES.format(3),
        SUMS.format("0.170.0"),
    ]
    assert all(limit == 32 << 20 for _, limit in world.fetch.calls[:3])


def test_only_three_pages_are_read(world: World) -> None:
    world.record_current()
    page = [release("rust-v0.150.0", "2026-10-01T00:00:00Z")]
    world.publish(PAGE_ONE, PAGE_TWO, page, page)
    world.run()
    assert [url for url, _ in world.fetch.calls if "/releases?" in url] == [
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


def test_a_not_automated_candidate_is_reused_without_saving_or_notifying(
    world: World,
) -> None:
    add_unknown_blocking_id(world)
    world.record_current()
    first = world.run()
    assert first.status == "not_automated"
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


def test_a_blocking_id_without_a_check_is_not_automated(world: World) -> None:
    add_unknown_blocking_id(world)
    world.record_current()
    report = world.run()
    assert (report.status, report.reason) == ("not_automated", None)
    assert [check.__dict__ for check in report.checks] == NOT_AUTOMATED_CHECKS
    pins = load_codex_pins(world.hub)
    assert world.codex()["candidate"] == {
        "version": "0.170.0",
        "model": None,
        "status": "not_automated",
        "sha256": None,
        "path": None,
        "pins_sha256": pins.sha256,
        "checks": NOT_AUTOMATED_CHECKS,
        "warnings": list(pins.warn),
        "at": NOW.isoformat(),
    }
    assert [(event, kw["target"]) for event, kw in world.send.calls] == [
        ("selfcheck_failed", "codex@0.170.0")
    ]
    # Nothing is installed or run for a candidate that cannot be checked.
    assert world.mise.calls == []
    assert world.probe.calls == []
    assert world.isolation.calls == []
    assert installed(world) == ["0.160.0"]


# The run: checking a candidate


def checks_of(report: selfcheck_codex.CodexReport) -> list[tuple[str, str, str]]:
    return [(c.id, c.result, c.detail) for c in report.checks]


def test_the_checks_run_in_order_with_the_candidate_binary(world: World) -> None:
    world.record_current()
    world.run()
    binary = world.layout.installs / "0.170.0" / "bin" / "codex"
    # One probe run under the profile, then the two isolation runs.
    [(argv, cwd, env, timeout, text)] = world.probe.calls
    assert str(binary) in argv
    assert argv[0] == sandbox.SANDBOX_EXEC
    assert world.probe.git_seen == [True]
    assert cwd.name == "project"
    assert cwd.parent.name.startswith("orch-selfcheck-")
    assert env["HOME"] == str(world.home)
    assert "GH_TOKEN" not in env
    assert timeout == 300
    assert [call[0][0] for call in world.isolation.calls] == [str(binary)] * 2
    assert world.isolation.calls[0][1] == cwd
    assert world.proxy.created == [
        (proxy.load_proxy_hosts(world.hub), cwd.parent / "proxy.log")
    ]
    # The temporary directory and the canary are gone.
    assert not cwd.parent.exists()
    assert list((world.home / ".orch" / "secrets").iterdir()) == []
    assert world.probe.canary_seen[0][0] is True


def test_a_fl_m4_failure_stops_before_any_codex_run(world: World) -> None:
    world.record_current()
    world.commands.codesign_fail = {"rg"}
    report = world.run()
    assert report.status == "failed"
    assert checks_of(report) == [
        ("FL-M4", "fail", "codesign"),
        ("S-M2", "skipped", ""),
        ("S-M2b", "skipped", ""),
        ("isolation", "skipped", ""),
    ]
    assert world.probe.calls == []
    assert world.isolation.calls == []
    assert installed(world) == ["0.160.0"]
    candidate = world.codex()["candidate"]
    assert (candidate["status"], candidate["sha256"], candidate["path"]) == (  # type: ignore[index]
        "failed",
        None,
        None,
    )
    assert [event for event, _ in world.send.calls] == ["selfcheck_failed"]
    assert report.notified == "sent"


def test_a_sandbox_failure_stops_before_isolation(world: World) -> None:
    world.record_current()
    world.probe.cat_exit = 0
    report = world.run()
    assert report.status == "failed"
    assert checks_of(report) == [
        ("FL-M4", "pass", ""),
        ("S-M2", "fail", "s_m2:read"),
        ("S-M2b", "pass", ""),
        ("isolation", "skipped", ""),
    ]
    assert world.isolation.calls == []
    assert installed(world) == ["0.160.0"]
    assert [event for event, _ in world.send.calls] == ["selfcheck_failed"]


def test_an_isolation_failure_fails_the_candidate(world: World) -> None:
    world.record_current()
    world.isolation.control = _result(0, "no hooks here\n")
    report = world.run()
    assert report.status == "failed"
    assert checks_of(report) == [
        ("FL-M4", "pass", ""),
        ("S-M2", "pass", ""),
        ("S-M2b", "pass", ""),
        ("isolation", "fail", "control:no_hooks"),
    ]
    assert installed(world) == ["0.160.0"]


def test_a_git_setup_failure_fails_the_first_codex_check(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.record_current()
    monkeypatch.setattr(selfcheck_codex, "GIT", str(world.root / "missing" / "git"))
    report = world.run()
    assert report.status == "failed"
    assert checks_of(report) == [
        ("FL-M4", "pass", ""),
        ("S-M2", "fail", "setup:git"),
        ("S-M2b", "skipped", ""),
        ("isolation", "skipped", ""),
    ]
    assert world.probe.calls == []


def test_invalid_proxy_hosts_fail_both_sandbox_checks(world: World) -> None:
    pins = world.hub / "config" / "pins.toml"
    text = pins.read_text(encoding="utf-8")
    old = 'proxy_hosts = ["chatgpt.com", "ab.chatgpt.com"]'
    assert text.count(old) == 1
    pins.write_text(text.replace(old, 'proxy_hosts = ["1.2.3.4"]'), encoding="utf-8")
    world.record_current()
    report = world.run()
    assert report.status == "failed"
    assert checks_of(report) == [
        ("FL-M4", "pass", ""),
        ("S-M2", "fail", "probe:hosts"),
        ("S-M2b", "fail", "probe:hosts"),
        ("isolation", "skipped", ""),
    ]
    assert world.probe.calls == []


def test_a_failed_candidate_is_reused_without_installing_or_running(
    world: World,
) -> None:
    world.record_current()
    world.probe.direct_exit = 0
    first = world.run()
    assert first.status == "failed"
    saved = world.state_path.read_bytes()
    world.send.calls.clear()
    world.mise.calls.clear()
    world.probe.calls.clear()
    second = world.run()
    assert (second.status, second.reused, second.notified) == ("failed", True, None)
    assert second.checks == first.checks
    assert second.warnings == first.warnings
    assert world.mise.calls == []
    assert world.probe.calls == []
    assert world.send.calls == []
    assert world.state_path.read_bytes() == saved


def test_a_ready_candidate_is_reused_while_its_binary_is_unchanged(
    world: World,
) -> None:
    world.record_current()
    first = world.run()
    assert first.status == "ready"
    saved = world.state_path.read_bytes()
    world.send.calls.clear()
    world.mise.calls.clear()
    world.probe.calls.clear()
    second = world.run()
    assert (second.status, second.reused, second.notified) == ("ready", True, None)
    assert second.checks == first.checks
    assert world.mise.calls == []
    assert world.probe.calls == []
    assert world.send.calls == []
    assert world.state_path.read_bytes() == saved


def test_a_ready_candidate_whose_binary_changed_is_checked_again(
    world: World,
) -> None:
    world.record_current()
    world.run()
    binary = world.layout.installs / "0.170.0" / "bin" / "codex"
    binary.write_bytes(binary.read_bytes() + b"tampered")
    world.probe.calls.clear()
    world.send.calls.clear()
    report = world.run()
    assert (report.status, report.reused) == ("ready", False)
    assert len(world.probe.calls) == 1
    assert world.codex()["candidate"]["sha256"] == selfcheck.sha256_file(binary)  # type: ignore[index]
    assert [event for event, _ in world.send.calls] == ["selfcheck_ready"]


def test_a_ready_candidate_whose_binary_is_gone_is_checked_again(
    world: World,
) -> None:
    world.record_current()
    world.run()
    shutil.rmtree(world.layout.installs / "0.170.0")
    world.probe.calls.clear()
    report = world.run()
    assert (report.status, report.reused) == ("ready", False)
    assert len(world.probe.calls) == 1


def test_a_failed_candidate_is_checked_again_after_a_pins_change(
    world: World,
) -> None:
    world.record_current()
    world.probe.direct_exit = 0
    world.run()
    pins = world.hub / "config" / "pins.toml"
    pins.write_text(pins.read_text() + "\n# changed\n")
    world.probe.direct_exit = 6
    world.probe.calls.clear()
    report = world.run()
    assert (report.status, report.reused) == ("ready", False)
    assert len(world.probe.calls) == 1


def _record_ready_candidate(world: World, version: str) -> Path:
    binary = world.layout.installs / version / "bin" / "codex"
    build_package(binary.parents[1], version)
    state = world.state()
    state["agents"]["codex"]["candidate"] = {  # type: ignore[index]
        "version": version,
        "model": DEFAULT_MODEL,
        "status": "ready",
        "sha256": selfcheck.sha256_file(binary),
        "path": str(binary),
        "pins_sha256": load_codex_pins(world.hub).sha256,
        "checks": PASSED_CHECKS,
        "warnings": [],
        "at": NOW.isoformat(),
    }
    selfcheck.save_agents_state(world.state_path, state)
    return binary


def test_a_new_ready_candidate_removes_the_older_ready_one(world: World) -> None:
    world.record_current()
    _record_ready_candidate(world, "0.165.0")
    report = world.run()
    assert (report.status, report.candidate) == ("ready", "0.170.0")
    assert installed(world) == ["0.160.0", "0.170.0"]


def test_a_failed_candidate_keeps_the_older_ready_one(world: World) -> None:
    world.record_current()
    _record_ready_candidate(world, "0.165.0")
    world.probe.direct_exit = 0
    report = world.run()
    assert (report.status, report.candidate) == ("failed", "0.170.0")
    assert installed(world) == ["0.160.0", "0.165.0"]


def test_a_failed_candidate_never_removes_the_current_install(world: World) -> None:
    current = world.record_current("0.170.0")
    world.publish(
        [release("rust-v0.171.0", "2026-11-10T00:00:00Z")] + PAGE_ONE[2:], PAGE_TWO
    )
    world.probe.direct_exit = 0
    report = world.run()
    assert (report.status, report.candidate) == ("failed", "0.171.0")
    assert current.is_file()
    assert installed(world) == ["0.170.0"]


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
                    "checks": NOT_AUTOMATED_CHECKS,
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
