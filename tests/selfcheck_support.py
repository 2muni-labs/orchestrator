"""Fakes for the selfcheck tests: a test signing key, a release server, codesign and
--version, and a Claude Code session that behaves as FL-M6 measured (2.1.319).

Nothing here uses the network or runs the real claude or codesign.
"""

import base64
import hashlib
import json
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from orch import selfcheck
from orch.signature import PublicKey
from test_signature_rules import CREATED, make_key, sign, subpacket

REPO = Path(__file__).resolve().parents[1]
BASE = "https://downloads.claude.ai/claude-code-releases"
LATEST = f"{BASE}/latest"
PLATFORM = "darwin-x64"
MACHINE = "x86_64"
NOW = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)

# buildDate values measured on 2026-11-20 (2.1.320 does not exist).
BUILD_DATES = {
    "2.1.319": "2026-11-05T19:38:09Z",
    "2.1.321": "2026-11-09T17:01:59Z",
    "2.1.322": "2026-11-10T00:53:25Z",
    "2.1.323": "2026-11-12T19:29:04Z",
    "2.1.324": "2026-11-13T15:54:24Z",
    "2.1.325": "2026-11-13T19:15:06Z",
    "2.1.326": "2026-11-16T17:26:18Z",
    "2.1.327": "2026-11-17T18:29:12Z",
    "2.1.328": "2026-11-18T21:56:43Z",
    "2.1.329": "2026-11-19T16:44:15Z",
}


def _mpi(value: int) -> bytes:
    return value.bit_length().to_bytes(2, "big") + value.to_bytes(
        (value.bit_length() + 7) // 8, "big"
    )


def _armored_key(n: int, e: int) -> tuple[str, str]:
    body = b"\x04" + (1_700_000_000).to_bytes(4, "big") + b"\x01" + _mpi(n) + _mpi(e)
    header = b"\x99" + len(body).to_bytes(2, "big")
    fingerprint = hashlib.sha1(header + body).hexdigest().upper()
    text = base64.b64encode(header + body).decode()
    armored = (
        "-----BEGIN PGP PUBLIC KEY BLOCK-----\n\n"
        + text
        + "\n-----END PGP PUBLIC KEY BLOCK-----\n"
    )
    return armored, fingerprint


_RAW_KEY, PRIVATE = make_key(1024, 7)
KEY_ARMOR, FINGERPRINT = _armored_key(_RAW_KEY.n, _RAW_KEY.e)
KEY = PublicKey(FINGERPRINT, _RAW_KEY.n, _RAW_KEY.e)


def sign_document(document: bytes) -> str:
    issuer = subpacket(33, b"\x04" + bytes.fromhex(FINGERPRINT))
    return sign(
        hashed=CREATED + issuer,
        unhashed=b"",
        document=document,
        key=KEY,
        private=PRIVATE,
    )


def binary_bytes(version: str) -> bytes:
    return f"claude binary {version}\n".encode() * 64


def manifest_bytes(
    version: str,
    build_date: str,
    content: bytes,
    *,
    extra: Mapping[str, object] | None = None,
    platform_entry: Mapping[str, object] | None = None,
) -> bytes:
    entry = {
        "binary": "claude",
        "checksum": hashlib.sha256(content).hexdigest(),
        "size": len(content),
    }
    if platform_entry is not None:
        entry = dict(platform_entry)
    document: dict[str, object] = {
        "version": version,
        "manifestSignatureEnforcement": "flag",
        "buildDate": build_date,
        "platforms": {
            "darwin-arm64": {"binary": "claude", "checksum": "0" * 64, "size": 1},
            PLATFORM: entry,
        },
    }
    if extra:
        document.update(extra)
    return json.dumps(document, indent=2).encode()


# Hub


def make_hub(
    root: Path,
    *,
    blocking_extra: Sequence[str] = (),
    deny: Sequence[str] | None = None,
    pins_text: str | None = None,
    fingerprint: str | None = None,
) -> Path:
    """A hub with the real pins.toml and settings.json, signed by the test key."""
    hub = root / "hub"
    (hub / "config" / "keys").mkdir(parents=True)
    (hub / "config" / "keys" / "test.asc").write_text(KEY_ARMOR, encoding="utf-8")
    text = (REPO / "config" / "pins.toml").read_text(encoding="utf-8")
    text = text.replace(
        'signing_key = "config/keys/claude-code.asc"',
        'signing_key = "config/keys/test.asc"',
    )
    text = re.sub(
        r'signing_fingerprint = "[0-9A-F]{40}"',
        f'signing_fingerprint = "{fingerprint or FINGERPRINT}"',
        text,
    )
    for item in blocking_extra:
        text = text.replace(
            '  { id = "injection",',
            f'  {{ id = "{item}", what = "test" }},\n  {{ id = "injection",',
            1,
        )
    (hub / "config" / "pins.toml").write_text(
        text if pins_text is None else pins_text, encoding="utf-8"
    )
    settings = json.loads(
        (REPO / "config" / "settings.json").read_text(encoding="utf-8")
    )
    if deny is not None:
        settings["permissions"]["deny"] = list(deny)
    (hub / "config" / "settings.json").write_text(
        json.dumps(settings, indent=2), encoding="utf-8"
    )
    return hub


# Release server


@dataclass
class Release:
    files: dict[str, bytes] = field(default_factory=dict)
    binaries: dict[str, bytes] = field(default_factory=dict)
    errors: dict[str, Exception] = field(default_factory=dict)
    fetches: list[tuple[str, int]] = field(default_factory=list)
    downloads: list[tuple[str, Path, int]] = field(default_factory=list)

    def latest(self, text: str) -> None:
        self.files[LATEST] = text.encode()

    def publish(
        self,
        version: str,
        build_date: str | None = None,
        *,
        content: bytes | None = None,
        served: bytes | None = None,
        manifest: bytes | None = None,
        signature: str | None = None,
        tamper: bool = False,
    ) -> bytes:
        """Publish a signed manifest and its binary; returns the binary bytes."""
        content = binary_bytes(version) if content is None else content
        if manifest is None:
            manifest = manifest_bytes(
                version, build_date or BUILD_DATES[version], content
            )
        sig = sign_document(manifest) if signature is None else signature
        if tamper:
            manifest = manifest.replace(b"claude", b"claudf", 1)
        self.files[f"{BASE}/{version}/manifest.json"] = manifest
        self.files[f"{BASE}/{version}/manifest.json.sig"] = sig.encode()
        self.binaries[f"{BASE}/{version}/{PLATFORM}/claude"] = (
            content if served is None else served
        )
        return content

    def publish_measured(self) -> None:
        self.latest("2.1.329\n")
        for version in BUILD_DATES:
            self.publish(version)

    def fetch(self, url: str, limit: int) -> bytes:
        self.fetches.append((url, limit))
        if url in self.errors:
            raise self.errors[url]
        if url not in self.files:
            raise selfcheck.NotFound(url)
        body = self.files[url]
        if len(body) > limit:
            raise selfcheck.FetchError("too large")
        return body

    def download(self, url: str, dest: Path, size: int) -> None:
        self.downloads.append((url, dest, size))
        if url in self.errors:
            raise self.errors[url]
        if url not in self.binaries:
            raise selfcheck.NotFound(url)
        body = self.binaries[url]
        if len(body) > size:
            raise selfcheck.FetchError("too large")
        with dest.open("xb") as stream:
            stream.write(body)

    def manifest_fetches(self) -> list[str]:
        return [
            url.removeprefix(BASE + "/").removesuffix("/manifest.json")
            for url, _ in self.fetches
            if url.endswith("/manifest.json")
        ]


# codesign and --version


@dataclass
class Commands:
    codesign_codes: dict[str, int] = field(default_factory=dict)
    versions: dict[str, str] = field(default_factory=dict)
    version_codes: dict[str, int] = field(default_factory=dict)
    raises: dict[str, Exception] = field(default_factory=dict)
    calls: list[tuple[list[str], float]] = field(default_factory=list)

    def __call__(
        self, argv: Sequence[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        self.calls.append((argv, timeout))
        path = Path(argv[-1] if argv[0] == selfcheck.CODESIGN else argv[0])
        kind = "codesign" if argv[0] == selfcheck.CODESIGN else "version"
        if f"{kind}:{path.name}" in self.raises:
            raise self.raises[f"{kind}:{path.name}"]
        if kind == "codesign":
            return subprocess.CompletedProcess(
                argv, self.codesign_codes.get(path.name, 0), "", ""
            )
        version = self.versions.get(path.name, path.name)
        return subprocess.CompletedProcess(
            argv,
            self.version_codes.get(path.name, 0),
            f"{version} (Claude Code)\n",
            "",
        )


# Claude Code session


def _option(argv: Sequence[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


def _expand(rule_path: str, home: str) -> str:
    return home + rule_path[1:] if rule_path.startswith("~/") else rule_path


def _prompt_tool(prompt: str) -> str:
    return "Edit" if "Edit tool" in prompt else prompt.split()[2]


@dataclass
class FakeClaude:
    """Behaves like Claude Code 2.1.319 as FL-M6 measured, unless a knob says so.

    Case keys: hook-<mode>, deny-control, <Tool>-<mode>, inject, inject-control.

    Edit cases follow the fl-switch measurement (2.1.319 and 2.1.327): the model
    Reads the file before it Edits. A Read denied by the settings ends the case
    with only Read in the denials and no Edit call.
    """

    hook_blocks: set[str] = field(default_factory=lambda: {"exit2", "deny"})
    hook_skipped: set[str] = field(default_factory=set)
    unlisted_denials: set[str] = field(default_factory=set)
    not_denied: set[str] = field(default_factory=set)
    edit_echo: bool = False
    control_blocked: bool = False
    run_session_start: bool = True
    load_system_prompt: bool = True
    load_agents: bool = True
    control_leaks: bool = False
    control_marker: bool = False
    exit_codes: dict[str, int] = field(default_factory=dict)
    stdout: dict[str, str] = field(default_factory=dict)
    raises: dict[str, BaseException] = field(default_factory=dict)
    on_call: Callable[[str], None] | None = None
    calls: list[tuple[str, list[str], Path, dict[str, str], float]] = field(
        default_factory=list
    )
    _last: dict[str, str] = field(default_factory=dict)

    def keys(self) -> list[str]:
        return [key for key, *_ in self.calls]

    def __call__(
        self,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        settings_path = _option(argv, "--settings")
        settings = (
            json.loads(Path(settings_path).read_text(encoding="utf-8"))
            if settings_path
            else {}
        )
        hooks = settings.get("hooks", {})
        prompt = argv[-1]
        if "PreToolUse" in hooks:
            command = hooks["PreToolUse"][0]["hooks"][0]["command"]
            words = shlex.split(command)
            key = f"hook-{words[-2]}"
        elif "permissions" in settings:
            tool = _prompt_tool(prompt)
            path = re.search(r"(/[^\s,]+)", prompt)
            assert path is not None
            if "selfcheck-open-" in path.group(1):
                key = "deny-control"
            else:
                key = f"{tool}-{_option(argv, '--permission-mode')}"
        elif "SessionStart" in hooks:
            key = "inject"
        else:
            key = "inject-control"
        self.calls.append((key, argv, cwd, dict(env), timeout))
        if self.on_call is not None:
            self.on_call(key)
        if key in self.raises:
            raise self.raises[key]

        denials: list[dict[str, object]] = []
        if key.startswith("hook-"):
            result = self._hook(words, prompt, cwd, denials)
        elif key == "deny-control" or "-" in key and key[0].isupper():
            result = self._deny(key, settings, prompt, env, denials)
        else:
            result = self._inject(key, argv, hooks)

        if key in self.stdout:
            out = self.stdout[key]
        else:
            out = json.dumps(
                {
                    "type": "result",
                    "is_error": False,
                    "num_turns": 2,
                    "result": result,
                    "permission_denials": denials,
                }
            )
        return subprocess.CompletedProcess(argv, self.exit_codes.get(key, 0), out, "")

    def _hook(
        self,
        words: list[str],
        prompt: str,
        cwd: Path,
        denials: list[dict[str, object]],
    ) -> str:
        mode, log = words[-2], words[-1]
        if mode in self.hook_skipped:
            return "I will not run commands."
        with open(log, "a", encoding="utf-8") as stream:
            stream.write(f"ran {mode} Bash\n")
        if mode in self.hook_blocks:
            if mode not in self.unlisted_denials:
                denials.append({"tool_name": "Bash", "tool_use_id": "t1"})
            return "The hook blocked the command."
        (cwd / prompt.split()[-1]).write_text("", encoding="utf-8")
        return "DONE"

    def _deny(
        self,
        key: str,
        settings: Mapping[str, object],
        prompt: str,
        env: Mapping[str, str],
        denials: list[dict[str, object]],
    ) -> str:
        tool = _prompt_tool(prompt)
        match = re.search(r"(/[^\s,]+)", prompt)
        assert match is not None
        target = match.group(1)

        def ruled(name: str) -> bool:
            for rule in settings["permissions"]["deny"]:  # type: ignore[index]
                found = re.fullmatch(r"(\w+)\((.*)/\*\*\)", rule)
                assert found is not None
                prefix = _expand(found.group(2), env["HOME"]) + "/"
                if found.group(1) == name and (target + "/").startswith(prefix):
                    return True
            return False

        if tool == "Edit" and ruled("Read"):
            denials.append({"tool_name": "Read", "tool_use_id": "t1"})
            return "I could not read the file, so I did not edit it."
        denied = ruled(tool)
        if key == "deny-control" and self.control_blocked:
            denied = True
        if key in self.not_denied:
            denied = False
        path = Path(target)
        if denied:
            if key not in self.unlisted_denials:
                denials.append({"tool_name": tool, "tool_use_id": "t1"})
            if tool == "Edit" and self.edit_echo:
                return "I read the file but cannot edit it:\n" + path.read_text(
                    encoding="utf-8"
                )
            return "I cannot access that file."
        if tool == "Read":
            return path.read_text(encoding="utf-8")
        if tool == "Grep":
            return "".join(
                line
                for file in sorted(path.iterdir())
                for line in file.read_text(encoding="utf-8").splitlines(True)
                if "SELFCHECK" in line
            )
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("SELFCHECK", "XX"), encoding="utf-8")
        return "DONE"

    def _inject(self, key: str, argv: list[str], hooks: Mapping[str, object]) -> str:
        if key == "inject":
            if self.run_session_start:
                command = hooks["SessionStart"][0]["hooks"][0]["command"]  # type: ignore[index]
                subprocess.run(command, shell=True, check=True)
            words = shlex.split(
                hooks["SessionStart"][0]["hooks"][0]["command"]  # type: ignore[index]
            )
            self._last["marker"] = words[-1]
            main = _option(argv, "--append-system-prompt-file")
            assert main is not None
            found = re.search(
                r"SELFCHECK-TOKEN-[0-9a-f]+", Path(main).read_text(encoding="utf-8")
            )
            assert found is not None
            self._last["token"] = found.group(0)
            token = found.group(0) if self.load_system_prompt else "NONE"
            names = []
            agents = _option(argv, "--agents")
            if agents:
                self._last["agent"] = next(iter(json.loads(agents)))
                if self.load_agents:
                    names = [
                        n for n in json.loads(agents) if n.startswith("selfcheck-")
                    ]
            return f"{token}\n{', '.join(names) or 'NONE'}"
        if self.control_marker and "marker" in self._last:
            Path(self._last["marker"]).touch()
        if self.control_leaks:
            return f"{self._last.get('token', '')}\n{self._last.get('agent', '')}"
        return "NONE\nNONE"


def remove(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
