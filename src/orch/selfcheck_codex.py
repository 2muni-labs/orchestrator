"""Bootstrap Codex in the orch mise and report Codex candidates (ADR 0019, FL-M7)."""

import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from orch import notify
from orch.config import Config
from orch.lanes.codex import ISOLATION_ARGS  # noqa: F401
from orch.selfcheck import (
    CheckResult,
    Command,
    CurrentCheck,
    Fetch,
    Notify,
    http_get,
    run_command,
    run_session,
)

RELEASES_URL = (
    "https://api.github.com/repos/openai/codex/releases?per_page=10&page={page}"
)
RELEASE_PAGES = 3
RELEASES_LIMIT = 32 << 20
SUMS_URL = (
    "https://github.com/openai/codex/releases/download/"
    "rust-v{version}/codex-package_SHA256SUMS"
)
SUMS_LIMIT = 64 << 10
MISE_TIMEOUT_SEC = 120
MISE_INSTALL_TIMEOUT_SEC = 900
ISOLATION_MODEL = "gpt-6.1-sol"
ISOLATION_TIMEOUT_SEC = 180
ISOLATION_PROMPT = "Reply with the single word ok."
MACHO_MAGICS = frozenset(
    {
        b"\xcf\xfa\xed\xfe",
        b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
        b"\xfe\xed\xfa\xce",
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
        b"\xca\xfe\xba\xbf",
    }
)
CODEX_AUTOMATED_IDS = frozenset({"FL-M4", "isolation"})

Mise = Callable[
    [Sequence[str], Path, Mapping[str, str], float], subprocess.CompletedProcess[str]
]
Run = Callable[
    [Sequence[str], Path, Mapping[str, str], float, str],
    subprocess.CompletedProcess[str],
]


def run_with_input(
    argv: Sequence[str], cwd: Path, env: Mapping[str, str], timeout: float, text: str
) -> subprocess.CompletedProcess[str]:
    raise NotImplementedError


# Pins and layout


@dataclass(frozen=True)
class CodexPins:
    sha256: str
    version: str
    codesign_team: str
    blocking: tuple[str, ...]
    warn: tuple[str, ...]


def load_codex_pins(hub: Path) -> CodexPins:
    raise NotImplementedError


@dataclass(frozen=True)
class CodexPlatform:
    lock_key: str
    asset: str


def codex_platform(machine: str) -> CodexPlatform:
    raise NotImplementedError


@dataclass(frozen=True)
class CodexLayout:
    root: Path
    candidate: Path
    data: Path
    cache: Path
    state: Path
    installs: Path
    link: Path


def codex_layout(orch_home: Path) -> CodexLayout:
    raise NotImplementedError


def mise_env(
    layout: CodexLayout, config_dir: Path, home: Path, environ: Mapping[str, str]
) -> dict[str, str]:
    raise NotImplementedError


def config_text(version: str) -> str:
    raise NotImplementedError


# Preparing a version and the FL-M4 checks


@dataclass(frozen=True)
class Prepared:
    version: str
    binary: Path
    sha256: str


def prepare_version(
    version: str,
    *,
    layout: CodexLayout,
    pins: CodexPins,
    platform: CodexPlatform,
    mise_path: Path,
    home: Path,
    environ: Mapping[str, str],
    mise: Mise,
    fetch: Fetch,
    command: Command,
) -> Prepared | str:
    raise NotImplementedError


def remove_install(layout: CodexLayout, version: str, protected: set[Path]) -> None:
    raise NotImplementedError


# The isolation check


@dataclass(frozen=True)
class IsolationContext:
    binary: Path
    workdir: Path
    project: Path
    run: Run
    env: Mapping[str, str]


def codex_env(home: Path, environ: Mapping[str, str]) -> dict[str, str]:
    raise NotImplementedError


def isolation_argv(
    binary: Path, project: Path, output: Path, *, isolated: bool
) -> list[str]:
    raise NotImplementedError


def check_isolation(ctx: IsolationContext) -> CheckResult:
    raise NotImplementedError


# Run


@dataclass(frozen=True)
class CodexReport:
    status: str
    reason: str | None
    current: str | None
    latest: str | None
    candidate: str | None
    checks: tuple[CheckResult, ...]
    warnings: tuple[str, ...]
    reused: bool
    notified: str | None

    def to_json(self) -> dict[str, object]:
        raise NotImplementedError


def run_codex(
    *,
    hub: Path,
    orch_home: Path,
    home: Path,
    settings: Config,
    now: datetime,
    fetch: Fetch = http_get,
    command: Command = run_command,
    mise: Mise = run_session,
    send: Notify = notify.send,
    machine: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> CodexReport:
    raise NotImplementedError


# The version in use


def check_current_codex(
    *, orch_home: Path, codex_path: Path, command: Command = run_command
) -> CurrentCheck:
    raise NotImplementedError
