"""orch selfcheck for Claude Code: find, install beside and verify a candidate.

Switching (--apply, --rollback) and the current version check live elsewhere
(ADR 0019, technical design 4.13).
"""

import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from orch import notify, signature
from orch.config import Config

AGENTS_FORMAT = 1
SESSION_MODEL = "claude-haiku-5-5"
SESSION_MAX_TURNS = 3
SESSION_TIMEOUT_SEC = 120
HOOK_TIMEOUT_SEC = 3
MAX_PROBES = 30
LATEST_LIMIT = 64
MANIFEST_LIMIT = 1 << 20
SIGNATURE_LIMIT = 16 << 10
FETCH_TIMEOUT_SEC = 30
DOWNLOAD_TIMEOUT_SEC = 600
CODESIGN = "/usr/bin/codesign"
GIT = "/usr/bin/git"
SESSION_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
HOOK_MODES = ("allow", "exit2", "deny", "exit1", "exit70", "badjson", "timeout")
BLOCKING_HOOK_MODES = frozenset({"exit2", "deny"})
DENY_MODES = ("default", "bypassPermissions")


class SelfcheckError(Exception):
    """Configuration, pins, state file or manifest is malformed or unverified."""


class FetchError(Exception):
    """A network fetch failed, exceeded its size limit or was not https."""


class NotFound(FetchError):
    """HTTP 404."""


Fetch = Callable[[str, int], bytes]
Download = Callable[[str, Path, int], None]
Command = Callable[[Sequence[str], float], subprocess.CompletedProcess[str]]
Session = Callable[
    [Sequence[str], Path, Mapping[str, str], float], subprocess.CompletedProcess[str]
]
Notify = Callable[..., notify.Outcome]


# Default implementations


def http_get(url: str, limit: int) -> bytes:
    raise NotImplementedError


def http_download(url: str, dest: Path, size: int) -> None:
    raise NotImplementedError


def run_command(
    argv: Sequence[str], timeout: float
) -> subprocess.CompletedProcess[str]:
    raise NotImplementedError


def run_session(
    argv: Sequence[str], cwd: Path, env: Mapping[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    raise NotImplementedError


# Pins and versions


@dataclass(frozen=True)
class ClaudePins:
    version: str
    latest_url: str
    signing_key: Path
    signing_fingerprint: str
    codesign_team: str
    blocking: tuple[str, ...]
    warn: tuple[str, ...]


@dataclass(frozen=True)
class Pins:
    sha256: str
    claude: ClaudePins


def load_pins(hub: Path) -> Pins:
    raise NotImplementedError


def parse_version(text: str) -> tuple[int, int, int]:
    raise NotImplementedError


def platform_key(machine: str) -> str:
    raise NotImplementedError


def release_base(latest_url: str) -> str:
    raise NotImplementedError


# Manifest and binary


@dataclass(frozen=True)
class Manifest:
    version: str
    build_date: datetime
    checksum: str
    size: int


def load_key(pins: Pins) -> signature.PublicKey:
    raise NotImplementedError


def read_manifest(
    pins: Pins,
    version: str,
    platform: str,
    key: signature.PublicKey,
    fetch: Fetch,
) -> Manifest:
    raise NotImplementedError


def sha256_file(path: Path) -> str:
    raise NotImplementedError


def verify_binary(
    path: Path, manifest: Manifest, pins: Pins, command: Command
) -> str | None:
    raise NotImplementedError


# Layout and state file


@dataclass(frozen=True)
class ClaudeLayout:
    link: Path
    versions: Path


def claude_layout(home: Path) -> ClaudeLayout:
    raise NotImplementedError


def agents_state_path(orch_home: Path) -> Path:
    raise NotImplementedError


def load_agents_state(path: Path) -> dict[str, object]:
    raise NotImplementedError


def save_agents_state(path: Path, state: Mapping[str, object]) -> None:
    raise NotImplementedError


# Blocking checks


@dataclass(frozen=True)
class CheckResult:
    id: str
    result: str
    detail: str


@dataclass(frozen=True)
class CheckContext:
    binary: Path
    hub: Path
    home: Path
    workdir: Path
    project: Path
    session: Session
    env: Mapping[str, str]
    python: str
    nonce: str


def session_env(home: Path, environ: Mapping[str, str]) -> dict[str, str]:
    raise NotImplementedError


def session_argv(binary: Path, extra: Sequence[str], prompt: str) -> list[str]:
    raise NotImplementedError


def hook_script() -> str:
    raise NotImplementedError


def check_m14_cli(ctx: CheckContext) -> CheckResult:
    raise NotImplementedError


def check_deny_rules(ctx: CheckContext) -> CheckResult:
    raise NotImplementedError


def check_injection(ctx: CheckContext) -> CheckResult:
    raise NotImplementedError


SESSION_CHECKS: Mapping[str, Callable[[CheckContext], CheckResult]] = {
    "m14-cli": check_m14_cli,
    "deny-rules": check_deny_rules,
    "injection": check_injection,
}
AUTOMATED_IDS: frozenset[str] = frozenset(SESSION_CHECKS) | {"FL-M3"}


# Run


@dataclass(frozen=True)
class ClaudeReport:
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


def run_claude(
    *,
    hub: Path,
    orch_home: Path,
    home: Path,
    settings: Config,
    now: datetime,
    fetch: Fetch = http_get,
    download: Download = http_download,
    command: Command = run_command,
    session: Session = run_session,
    send: Notify = notify.send,
    machine: str | None = None,
    environ: Mapping[str, str] | None = None,
    python: str | None = None,
) -> ClaudeReport:
    raise NotImplementedError
