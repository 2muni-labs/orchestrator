"""Bootstrap Codex in the orch mise and report Codex candidates (ADR 0019, FL-M7)."""

import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

from orch import notify
from orch.config import Config
from orch.lanes.codex import ISOLATION_ARGS
from orch.selfcheck import (
    CODESIGN,
    SESSION_PATH,
    CheckResult,
    Command,
    CurrentCheck,
    Fetch,
    FetchError,
    Notify,
    SelfcheckError,
    _link_state,
    _restore_link,
    _switch_link,
    agents_state_path,
    http_get,
    load_agents_state,
    parse_version,
    run_command,
    run_session,
    save_agents_state,
    sha256_file,
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
    return subprocess.run(
        argv,
        cwd=cwd,
        env=dict(env),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        input=text,
    )


# Pins and layout


@dataclass(frozen=True)
class CodexPins:
    sha256: str
    version: str
    codesign_team: str
    blocking: tuple[str, ...]
    warn: tuple[str, ...]


def load_codex_pins(hub: Path) -> CodexPins:
    try:
        body = (hub / "config" / "pins.toml").read_bytes()
        data = tomllib.loads(body.decode("utf-8"))["codex"]
        if not isinstance(data, dict):
            raise ValueError("Expected codex table")
        version = data["version"]
        parse_version(version)
        team = data["codesign_team"]
        if not isinstance(team, str) or re.fullmatch(r"[0-9A-Z]{10}", team) is None:
            raise ValueError("Invalid codesign team")
        blocking = data["blocking"]
        if not isinstance(blocking, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("id"), str)
            for item in blocking
        ):
            raise ValueError("Invalid blocking list")
        ids = tuple(item["id"] for item in blocking)
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate blocking id")
        warn = data["warn"]
        if not isinstance(warn, list) or any(not isinstance(i, str) for i in warn):
            raise ValueError("Invalid warnings")
        return CodexPins(
            hashlib.sha256(body).hexdigest(), version, team, ids, tuple(warn)
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SelfcheckError("Cannot load Codex pins") from exc


@dataclass(frozen=True)
class CodexPlatform:
    lock_key: str
    asset: str


def codex_platform(machine: str) -> CodexPlatform:
    if machine == "x86_64":
        return CodexPlatform("macos-x64", "codex-package-x86_64-apple-darwin.tar.gz")
    if machine == "arm64":
        return CodexPlatform("macos-arm64", "codex-package-aarch64-apple-darwin.tar.gz")
    raise SelfcheckError("Unsupported platform")


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
    root = orch_home / "agent-mise"
    return CodexLayout(
        root,
        root / "candidate",
        root / "data",
        root / "cache",
        root / "state",
        root / "data" / "installs" / "codex",
        orch_home / "bin" / "codex",
    )


def mise_env(
    layout: CodexLayout, config_dir: Path, home: Path, environ: Mapping[str, str]
) -> dict[str, str]:
    return {
        **codex_env(home, environ),
        "MISE_GLOBAL_CONFIG_FILE": str(config_dir / "config.toml"),
        "MISE_CONFIG_DIR": str(config_dir),
        "MISE_DATA_DIR": str(layout.data),
        "MISE_CACHE_DIR": str(layout.cache),
        "MISE_STATE_DIR": str(layout.state),
    }


def config_text(version: str) -> str:
    return f'[tools]\ncodex = "{version}"\n\n[settings]\nlockfile = true\n'


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
    layout.candidate.mkdir(parents=True, exist_ok=True)
    config = layout.candidate / "config.toml"
    config.write_text(config_text(version), encoding="utf-8")
    lock = layout.candidate / "mise.lock"
    lock.unlink(missing_ok=True)
    env = mise_env(layout, layout.candidate, home, environ)
    try:
        result = mise(
            [str(mise_path), "config", "ls", "--json"],
            layout.candidate,
            env,
            MISE_TIMEOUT_SEC,
        )
        if result.returncode != 0:
            return "mise:config"
        try:
            configs = json.loads(result.stdout)
            if (
                not isinstance(configs, list)
                or len(configs) != 1
                or not isinstance(configs[0], dict)
                or not isinstance(configs[0].get("path"), str)
                or Path(configs[0]["path"]).resolve() != config.resolve()
                or configs[0].get("tools") != ["codex"]
            ):
                return "mise:config"
        except ValueError, TypeError:
            return "mise:config"
        result = mise(
            [str(mise_path), "lock", "--global"],
            layout.candidate,
            env,
            MISE_TIMEOUT_SEC,
        )
        if result.returncode != 0:
            return "mise:lock"
        result = mise(
            [str(mise_path), "install"], layout.candidate, env, MISE_INSTALL_TIMEOUT_SEC
        )
        if result.returncode != 0:
            return "mise:install"
    except OSError, subprocess.TimeoutExpired:
        return "mise:run"

    checksum = _lock_checksum(lock, version, platform)
    if checksum is None:
        return "lock:entry"
    try:
        body = fetch(SUMS_URL.format(version=version), SUMS_LIMIT)
    except FetchError:
        return "sums:fetch"
    try:
        text = body.decode("utf-8")
    except UnicodeError:
        return "sums:format"
    hashes = []
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (\S+)", line)
        if match is not None and match[2] == platform.asset:
            hashes.append(match[1])
    if not hashes:
        return "sums:missing"
    if len(hashes) > 1:
        return "sums:ambiguous"
    if hashes[0] != checksum:
        return "sums:mismatch"

    pkg = layout.installs / version
    if pkg.is_symlink() or not pkg.is_dir():
        return "package:missing"
    machos: list[Path] = []
    for directory, dirs, files in os.walk(pkg, followlinks=False, onerror=_walk_error):
        for name in dirs + files:
            path = Path(directory) / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                return "package:symlink"
            if stat.S_ISREG(info.st_mode):
                with path.open("rb") as stream:
                    if stream.read(4) in MACHO_MAGICS:
                        machos.append(path)
    binary = pkg / "bin" / "codex"
    if binary not in machos:
        return "package:binary"
    requirement = (
        "-R=anchor apple generic and certificate leaf[subject.OU] = "
        f'"{pins.codesign_team}"'
    )
    for path in sorted(machos):
        try:
            result = command(
                [CODESIGN, "--verify", "--strict", requirement, str(path)], 60
            )
        except OSError, subprocess.TimeoutExpired:
            return "codesign"
        if result.returncode != 0:
            return "codesign"
    try:
        result = command([str(binary), "--version"], 30)
    except OSError, subprocess.TimeoutExpired:
        return "version"
    if result.returncode != 0 or result.stdout.split() != ["codex-cli", version]:
        return "version"
    return Prepared(version, binary, sha256_file(binary))


def _lock_checksum(lock: Path, version: str, platform: CodexPlatform) -> str | None:
    try:
        data = tomllib.loads(lock.read_text(encoding="utf-8"))
        if data.get("lockfile_version") != 3:
            return None
        entries = data["tools"]["codex"]
        if not isinstance(entries, list):
            return None
        matching = [
            item
            for item in entries
            if isinstance(item, dict) and item.get("version") == version
        ]
        if len(matching) != 1:
            return None
        entry = matching[0][f"platforms.{platform.lock_key}"]
        if not isinstance(entry, dict):
            return None
        checksum = entry.get("checksum")
        url = (
            "https://github.com/openai/codex/releases/download/"
            f"rust-v{version}/{platform.asset}"
        )
        if (
            not isinstance(checksum, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", checksum) is None
            or entry.get("url") != url
        ):
            return None
        return checksum.removeprefix("sha256:")
    except OSError, ValueError, KeyError, TypeError:
        return None


def _walk_error(error: OSError) -> None:
    raise error


def remove_install(layout: CodexLayout, version: str, protected: set[Path]) -> None:
    try:
        path = layout.installs / version
        resolved = path.resolve()
        if (
            re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None
            or resolved.parent != layout.installs.resolve()
            or path.is_symlink()
            or not path.is_dir()
            or any(p.resolve().is_relative_to(resolved) for p in protected)
        ):
            return
        shutil.rmtree(path)
    except OSError:
        pass


# The isolation check


@dataclass(frozen=True)
class IsolationContext:
    binary: Path
    workdir: Path
    project: Path
    run: Run
    env: Mapping[str, str]


def codex_env(home: Path, environ: Mapping[str, str]) -> dict[str, str]:
    env = {
        "HOME": str(home),
        "PATH": SESSION_PATH,
        "LANG": environ.get("LANG") or "en_US.UTF-8",
    }
    if "USER" in environ:
        env["USER"] = environ["USER"]
    return env


def isolation_argv(
    binary: Path, project: Path, output: Path, *, isolated: bool
) -> list[str]:
    return [
        str(binary),
        "exec",
        *(ISOLATION_ARGS if isolated else ()),
        "-C",
        str(project),
        "-m",
        ISOLATION_MODEL,
        "-c",
        "model_reasoning_effort=low",
        "--sandbox",
        "read-only",
        "-o",
        str(output),
        "-",
    ]


def check_isolation(ctx: IsolationContext) -> CheckResult:
    try:
        for case, isolated in (("control", False), ("isolated", True)):
            output = ctx.workdir / f"{case}.last"
            argv = isolation_argv(ctx.binary, ctx.project, output, isolated=isolated)
            try:
                result = ctx.run(
                    argv, ctx.project, ctx.env, ISOLATION_TIMEOUT_SEC, ISOLATION_PROMPT
                )
            except OSError, subprocess.TimeoutExpired:
                return CheckResult("isolation", "fail", f"{case}:run")
            if result.returncode != 0:
                return CheckResult("isolation", "fail", f"{case}:exit")
            hooks = sum(line.startswith("hook:") for line in result.stderr.splitlines())
            if not isolated and hooks == 0:
                return CheckResult("isolation", "fail", "control:no_hooks")
            if isolated:
                if hooks > 0:
                    return CheckResult("isolation", "fail", "isolated:hooks")
                if not output.is_file():
                    return CheckResult("isolation", "fail", "isolated:output")
        return CheckResult("isolation", "pass", "")
    except Exception:
        return CheckResult("isolation", "fail", "isolation:error")


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
        return {
            "status": self.status,
            "reason": self.reason,
            "current": self.current,
            "latest": self.latest,
            "candidate": self.candidate,
            "checks": [asdict(check) for check in self.checks],
            "warnings": list(self.warnings),
            "reused": self.reused,
            "notified": self.notified,
        }


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
    run: _CodexRun | None = None
    try:
        run = _CodexRun(
            hub,
            orch_home,
            home,
            settings,
            now,
            fetch,
            command,
            mise,
            send,
            platform.machine() if machine is None else machine,
            os.environ if environ is None else environ,
        )
        orch_home.mkdir(parents=True, exist_ok=True)
        with (orch_home / "agents.json.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return run.report("busy")
            try:
                return run.execute()
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
    except _StateWriteError:
        reason = "state_write"
    except OSError:
        reason = "io"
    except Exception:
        reason = "internal"
    if run is not None:
        return run.report("error", reason)
    return CodexReport("error", reason, None, None, None, (), (), False, None)


class _StateWriteError(Exception):
    """A state replacement failed, distinct from other filesystem operations."""


@dataclass
class _CodexRun:
    hub: Path
    orch_home: Path
    home: Path
    settings: Config
    now: datetime
    fetch: Fetch
    command: Command
    mise: Mise
    send: Notify
    machine: str
    environ: Mapping[str, str]
    current: str | None = None
    latest: str | None = None
    candidate: str | None = None
    checks: tuple[CheckResult, ...] = ()
    warnings: tuple[str, ...] = ()

    def report(
        self,
        status: str,
        reason: str | None = None,
        *,
        reused: bool = False,
        notified: str | None = None,
    ) -> CodexReport:
        return CodexReport(
            status,
            reason,
            self.current,
            self.latest,
            self.candidate,
            self.checks,
            self.warnings,
            reused,
            notified,
        )

    def save(self, state: Mapping[str, object]) -> None:
        try:
            save_agents_state(agents_state_path(self.orch_home), state)
        except OSError as exc:
            raise _StateWriteError from exc

    def execute(self) -> CodexReport:
        try:
            pins = load_codex_pins(self.hub)
        except SelfcheckError:
            return self.report("error", "pins")
        try:
            platform = codex_platform(self.machine)
        except SelfcheckError:
            return self.report("error", "platform")
        try:
            state = load_agents_state(agents_state_path(self.orch_home))
        except SelfcheckError:
            return self.report("error", "state")
        codex = state["agents"].setdefault("codex", {})
        if codex.get("current") is None:
            reason = self.bootstrap(pins, platform, state, codex)
            if reason:
                return self.report("unverified", reason)
        self.current = codex["current"]["version"]

        releases: dict[str, datetime] = {}
        for page in range(1, RELEASE_PAGES + 1):
            try:
                body = self.fetch(RELEASES_URL.format(page=page), RELEASES_LIMIT)
            except FetchError:
                return self.report("unknown", "fetch")
            try:
                items = json.loads(body)
            except ValueError, UnicodeError:
                return self.report("unknown", "releases")
            if not isinstance(items, list):
                return self.report("unknown", "releases")
            if not items:
                break
            for item in items:
                parsed = _parse_release(item)
                if parsed is not None:
                    version, published = parsed
                    releases[version] = min(releases.get(version, published), published)
        if not releases:
            return self.report("unknown", "latest")
        self.latest = max(releases, key=parse_version)
        current_version = parse_version(self.current)
        if parse_version(self.latest) <= current_version:
            return self.report("up_to_date", "latest")
        candidates = [
            version
            for version, published in releases.items()
            if parse_version(version) > current_version
            and f"codex@{version}" not in self.settings.selfcheck_hold
            and self.now - published
            >= timedelta(days=self.settings.selfcheck_min_age_days)
        ]
        if not candidates:
            return self.report("up_to_date", "no_candidate")
        self.candidate = max(candidates, key=parse_version)
        if all(item in CODEX_AUTOMATED_IDS for item in pins.blocking):
            return self.report("error", "unsupported")
        self.checks = tuple(
            CheckResult(
                item, "skipped" if item in CODEX_AUTOMATED_IDS else "missing", ""
            )
            for item in pins.blocking
        )
        self.warnings = pins.warn
        previous_candidate = codex.get("candidate")
        if (
            isinstance(previous_candidate, dict)
            and previous_candidate["version"] == self.candidate
            and previous_candidate["status"] == "not_automated"
            and previous_candidate["pins_sha256"] == pins.sha256
        ):
            self.checks = tuple(
                CheckResult(item["id"], item["result"], item["detail"])
                for item in previous_candidate["checks"]
            )
            self.warnings = tuple(previous_candidate["warnings"])
            return self.report("not_automated", reused=True)
        codex["candidate"] = {
            "version": self.candidate,
            "status": "not_automated",
            "sha256": None,
            "path": None,
            "pins_sha256": pins.sha256,
            "checks": [asdict(check) for check in self.checks],
            "warnings": list(self.warnings),
            "at": self.now.isoformat(),
        }
        self.save(state)
        try:
            notified = self.send(
                "selfcheck_failed",
                settings=self.settings,
                orch_home=self.orch_home,
                home=self.home,
                now=self.now,
                target=f"codex@{self.candidate}",
            ).value
        except Exception:
            notified = "failed"
        return self.report("not_automated", notified=notified)

    def bootstrap(
        self,
        pins: CodexPins,
        platform: CodexPlatform,
        state: dict[str, object],
        codex: dict[str, object],
    ) -> str | None:
        if self.settings.mise_path is None:
            return "mise_path"
        layout = codex_layout(self.orch_home)
        if _link_state(layout.link)[0] == "other":
            return "link"
        prepared = prepare_version(
            pins.version,
            layout=layout,
            pins=pins,
            platform=platform,
            mise_path=self.settings.mise_path,
            home=self.home,
            environ=self.environ,
            mise=self.mise,
            fetch=self.fetch,
            command=self.command,
        )
        if isinstance(prepared, str):
            remove_install(layout, pins.version, set())
            return prepared
        layout.root.mkdir(parents=True, exist_ok=True)
        os.replace(layout.candidate / "config.toml", layout.root / "config.toml")
        os.replace(layout.candidate / "mise.lock", layout.root / "mise.lock")
        before = _link_state(layout.link)
        layout.link.parent.mkdir(parents=True, exist_ok=True)
        _switch_link(layout.link, str(prepared.binary))
        codex.update(
            current={
                "version": prepared.version,
                "sha256": prepared.sha256,
                "path": str(prepared.binary),
                "at": self.now.isoformat(),
                "source": "bootstrap",
            },
            previous=None,
            candidate=None,
        )
        try:
            self.save(state)
        except _StateWriteError:
            _restore_link(layout.link, before)
            raise
        return None


def _parse_release(item: object) -> tuple[str, datetime] | None:
    if (
        not isinstance(item, dict)
        or item.get("draft") is not False
        or item.get("prerelease") is not False
    ):
        return None
    tag = item.get("tag_name")
    published = item.get("published_at")
    if not isinstance(tag, str) or not isinstance(published, str):
        return None
    match = re.fullmatch(r"rust-v([0-9]+\.[0-9]+\.[0-9]+)", tag)
    if match is None or not published.endswith("Z"):
        return None
    try:
        date = datetime.fromisoformat(published)
    except ValueError:
        return None
    if date.tzinfo is None or date.utcoffset() is None:
        return None
    return match[1], date


# The version in use


def check_current_codex(
    *, orch_home: Path, codex_path: Path, command: Command = run_command
) -> CurrentCheck:
    try:
        try:
            state = load_agents_state(agents_state_path(orch_home))
        except SelfcheckError:
            return CurrentCheck("error", "state", None)
        current = state["agents"].get("codex", {}).get("current")
        if current is None:
            return CurrentCheck("no_record", None, None)
        version = current["version"]
        resolved = codex_path.resolve()
        if resolved != Path(current["path"]).resolve():
            return CurrentCheck("mismatch", "path", version)
        if not resolved.is_file():
            return CurrentCheck("mismatch", "missing", version)
        if sha256_file(resolved) != current["sha256"]:
            return CurrentCheck("mismatch", "sha256", version)
        try:
            result = command([str(resolved), "--version"], 30)
        except OSError, subprocess.TimeoutExpired:
            return CurrentCheck("mismatch", "version", version)
        if result.returncode != 0 or result.stdout.split() != ["codex-cli", version]:
            return CurrentCheck("mismatch", "version", version)
        return CurrentCheck("ok", None, version)
    except OSError:
        return CurrentCheck("error", "io", None)
    except Exception:
        return CurrentCheck("error", "internal", None)
