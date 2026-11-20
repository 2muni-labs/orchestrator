"""orch selfcheck for Claude Code: pins, manifests, binaries, agents.json and the run.

The session checks themselves are in test_selfcheck_sessions.py.
"""

import fcntl
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orch import notify, selfcheck
from orch.config import Config
from orch.selfcheck import CheckResult, ClaudeReport, SelfcheckError
from selfcheck_support import (
    BASE,
    BUILD_DATES,
    LATEST,
    MACHINE,
    NOW,
    PLATFORM,
    REPO,
    Commands,
    FakeClaude,
    Release,
    binary_bytes,
    make_hub,
    manifest_bytes,
    sign_document,
)

REAL_KEY_FINGERPRINT = "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE"
CLAUDE_IDS = ("FL-M3", "m14-cli", "deny-rules", "injection")


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network used")

    monkeypatch.setattr("urllib.request.urlopen", refuse)


# Pins and versions


def test_the_real_pins_load() -> None:
    pins = selfcheck.load_pins(REPO)
    data = (REPO / "config" / "pins.toml").read_bytes()
    assert pins.sha256 == hashlib.sha256(data).hexdigest()
    claude = pins.claude
    assert claude.version == "2.1.319"
    assert claude.latest_url == LATEST
    assert claude.signing_key == REPO / "config" / "keys" / "claude-code.asc"
    assert claude.signing_key.is_absolute()
    assert claude.signing_fingerprint == REAL_KEY_FINGERPRINT
    assert claude.codesign_team == "Q6L2SF6YDW"
    assert claude.blocking == CLAUDE_IDS
    assert claude.warn == tuple(tomllib.loads(data.decode())["claude"]["warn"])


def test_every_claude_blocking_id_in_the_real_pins_has_a_check() -> None:
    ids = {
        item["id"]
        for item in tomllib.loads(
            (REPO / "config" / "pins.toml").read_text(encoding="utf-8")
        )["claude"]["blocking"]
    }
    assert ids <= selfcheck.AUTOMATED_IDS


def test_the_automated_ids_are_the_session_checks_and_provenance() -> None:
    assert set(selfcheck.SESSION_CHECKS) == {"m14-cli", "deny-rules", "injection"}
    assert selfcheck.SESSION_CHECKS["m14-cli"] is selfcheck.check_m14_cli
    assert selfcheck.SESSION_CHECKS["deny-rules"] is selfcheck.check_deny_rules
    assert selfcheck.SESSION_CHECKS["injection"] is selfcheck.check_injection
    assert selfcheck.AUTOMATED_IDS == frozenset(CLAUDE_IDS)


PINS_OK = """
[claude]
version = "2.1.319"
latest_url = "https://downloads.claude.ai/claude-code-releases/latest"
signing_key = "config/keys/claude-code.asc"
signing_fingerprint = "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE"
codesign_team = "Q6L2SF6YDW"
blocking = [{ id = "FL-M3", what = "x" }, { id = "m14-cli", what = "y" }]
warn = ["w1", "w2"]

[codex]
version = "anything"
"""


def write_pins(tmp_path: Path, text: str) -> Path:
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "pins.toml").write_text(text, encoding="utf-8")
    return tmp_path


def test_a_minimal_pins_file_loads_and_other_tables_are_ignored(
    tmp_path: Path,
) -> None:
    pins = selfcheck.load_pins(write_pins(tmp_path, PINS_OK))
    assert pins.claude.blocking == ("FL-M3", "m14-cli")
    assert pins.claude.warn == ("w1", "w2")
    assert pins.claude.signing_key == tmp_path / "config/keys/claude-code.asc"
    assert pins.sha256 == hashlib.sha256(PINS_OK.encode()).hexdigest()


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('version = "2.1.319"', ""),
        ('version = "2.1.319"', 'version = "2.1"'),
        ('version = "2.1.319"', "version = 2"),
        ("latest_url = ", "other_url = "),
        ('"https://downloads', '"http://downloads'),
        ("releases/latest", "releases/stable"),
        ('signing_key = "config/keys/claude-code.asc"', ""),
        ('"config/keys/claude-code.asc"', '"/etc/claude-code.asc"'),
        ('"config/keys/claude-code.asc"', '"config/../../claude-code.asc"'),
        ('"31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE"', '"31ddde24"'),
        (
            '"31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE"',
            '"31ddde24ddfab679f42d7bd2baa929ff1a7ecace"',
        ),
        ('codesign_team = "Q6L2SF6YDW"', ""),
        ('"Q6L2SF6YDW"', '"Q6L2SF6YD"'),
        ('"Q6L2SF6YDW"', '"q6l2sf6ydw"'),
        ('{ id = "m14-cli", what = "y" }', '{ id = "FL-M3", what = "y" }'),
        ('{ id = "m14-cli", what = "y" }', '{ what = "y" }'),
        ('{ id = "m14-cli", what = "y" }', '{ id = 1, what = "y" }'),
        ('{ id = "m14-cli", what = "y" }', '"m14-cli"'),
        (
            'blocking = [{ id = "FL-M3", what = "x" }, { id = "m14-cli", what = "y" }]',
            "",
        ),
        ('warn = ["w1", "w2"]', 'warn = ["w1", 2]'),
        ('warn = ["w1", "w2"]', ""),
        ("[claude]", "[claudecode]"),
        ("[claude]", "[claude"),
    ],
)
def test_an_invalid_pins_file_is_refused(tmp_path: Path, old: str, new: str) -> None:
    assert old in PINS_OK
    write_pins(tmp_path, PINS_OK.replace(old, new, 1))
    with pytest.raises(SelfcheckError):
        selfcheck.load_pins(tmp_path)


def test_a_missing_pins_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SelfcheckError):
        selfcheck.load_pins(tmp_path)


@pytest.mark.parametrize(
    ("text", "value"),
    [("2.1.319", (2, 1, 319)), ("0.0.0", (0, 0, 0)), ("10.20.30", (10, 20, 30))],
)
def test_parse_version(text: str, value: tuple[int, int, int]) -> None:
    assert selfcheck.parse_version(text) == value


@pytest.mark.parametrize(
    "text", ["", "2.1", "2.1.319.1", "v2.1.319", "2.1.319 ", "2.1.x", "2.1.319\n"]
)
def test_parse_version_refuses_other_forms(text: str) -> None:
    with pytest.raises(SelfcheckError):
        selfcheck.parse_version(text)


def test_versions_compare_numerically() -> None:
    assert selfcheck.parse_version("2.1.329") > selfcheck.parse_version("2.1.99")


@pytest.mark.parametrize(
    ("machine", "key"), [("arm64", "darwin-arm64"), ("x86_64", "darwin-x64")]
)
def test_platform_key(machine: str, key: str) -> None:
    assert selfcheck.platform_key(machine) == key


@pytest.mark.parametrize("machine", ["", "aarch64", "i386", "x64", "ARM64"])
def test_platform_key_refuses_other_machines(machine: str) -> None:
    with pytest.raises(SelfcheckError):
        selfcheck.platform_key(machine)


def test_release_base_strips_latest() -> None:
    assert selfcheck.release_base(LATEST) == BASE


@pytest.mark.parametrize("url", [BASE + "/stable", BASE, BASE + "/latest/"])
def test_release_base_needs_a_latest_url(url: str) -> None:
    with pytest.raises(SelfcheckError):
        selfcheck.release_base(url)


# Key and manifest


def test_the_real_key_loads_with_the_pinned_fingerprint() -> None:
    key = selfcheck.load_key(selfcheck.load_pins(REPO))
    assert key.fingerprint == REAL_KEY_FINGERPRINT


def test_a_key_whose_fingerprint_differs_from_the_pins_is_refused(
    tmp_path: Path,
) -> None:
    hub = make_hub(tmp_path, fingerprint="0" * 40)
    with pytest.raises(SelfcheckError):
        selfcheck.load_key(selfcheck.load_pins(hub))


def test_a_missing_or_invalid_key_file_is_refused(tmp_path: Path) -> None:
    hub = make_hub(tmp_path)
    pins = selfcheck.load_pins(hub)
    pins.claude.signing_key.write_text("not a key\n", encoding="utf-8")
    with pytest.raises(SelfcheckError):
        selfcheck.load_key(pins)
    pins.claude.signing_key.unlink()
    with pytest.raises(SelfcheckError):
        selfcheck.load_key(pins)


def real_release() -> Release:
    data = REPO / "tests" / "data" / "claude-manifests"
    release = Release()
    for version in ("2.1.300", "2.1.319"):
        release.files[f"{BASE}/{version}/manifest.json"] = (
            data / f"manifest-{version}.json"
        ).read_bytes()
        release.files[f"{BASE}/{version}/manifest.json.sig"] = (
            data / f"manifest-{version}.json.sig"
        ).read_bytes()
    return release


def test_the_real_2_1_319_manifest_reads_with_the_real_key() -> None:
    pins = selfcheck.load_pins(REPO)
    release = real_release()
    manifest = selfcheck.read_manifest(
        pins, "2.1.319", PLATFORM, selfcheck.load_key(pins), release.fetch
    )
    assert manifest.version == "2.1.319"
    assert manifest.build_date == datetime(2026, 11, 5, 19, 38, 9, tzinfo=UTC)
    assert manifest.build_date.utcoffset() is not None
    assert manifest.size == 336699808
    assert len(manifest.checksum) == 64
    assert manifest.checksum == manifest.checksum.lower()
    assert manifest.checksum.startswith("af6f1f55")
    assert release.fetches == [
        (f"{BASE}/2.1.319/manifest.json", selfcheck.MANIFEST_LIMIT),
        (f"{BASE}/2.1.319/manifest.json.sig", selfcheck.SIGNATURE_LIMIT),
    ]


def test_a_manifest_under_another_version_url_is_refused() -> None:
    pins = selfcheck.load_pins(REPO)
    release = real_release()
    release.files[f"{BASE}/2.1.320/manifest.json"] = release.files[
        f"{BASE}/2.1.319/manifest.json"
    ]
    release.files[f"{BASE}/2.1.320/manifest.json.sig"] = release.files[
        f"{BASE}/2.1.319/manifest.json.sig"
    ]
    with pytest.raises(SelfcheckError):
        selfcheck.read_manifest(
            pins, "2.1.320", PLATFORM, selfcheck.load_key(pins), release.fetch
        )


def test_a_manifest_changed_by_one_byte_is_refused() -> None:
    pins = selfcheck.load_pins(REPO)
    release = real_release()
    url = f"{BASE}/2.1.319/manifest.json"
    release.files[url] = release.files[url].replace(b"2026-11-05", b"2026-11-04", 1)
    with pytest.raises(SelfcheckError):
        selfcheck.read_manifest(
            pins, "2.1.319", PLATFORM, selfcheck.load_key(pins), release.fetch
        )


def test_a_signature_from_another_version_is_refused() -> None:
    pins = selfcheck.load_pins(REPO)
    release = real_release()
    release.files[f"{BASE}/2.1.319/manifest.json.sig"] = release.files[
        f"{BASE}/2.1.300/manifest.json.sig"
    ]
    with pytest.raises(SelfcheckError):
        selfcheck.read_manifest(
            pins, "2.1.319", PLATFORM, selfcheck.load_key(pins), release.fetch
        )


@pytest.fixture
def hub_pins(tmp_path: Path) -> selfcheck.Pins:
    return selfcheck.load_pins(make_hub(tmp_path))


def read(release: Release, pins: selfcheck.Pins, version: str) -> selfcheck.Manifest:
    return selfcheck.read_manifest(
        pins, version, PLATFORM, selfcheck.load_key(pins), release.fetch
    )


def test_a_test_signed_manifest_reads(hub_pins: selfcheck.Pins) -> None:
    release = Release()
    content = release.publish("2.1.326")
    manifest = read(release, hub_pins, "2.1.326")
    assert manifest == selfcheck.Manifest(
        "2.1.326",
        datetime(2026, 11, 16, 17, 26, 18, tzinfo=UTC),
        hashlib.sha256(content).hexdigest(),
        len(content),
    )


def test_the_platform_entry_is_chosen_by_key(hub_pins: selfcheck.Pins) -> None:
    release = Release()
    release.publish("2.1.326")
    manifest = selfcheck.read_manifest(
        hub_pins, "2.1.326", "darwin-arm64", selfcheck.load_key(hub_pins), release.fetch
    )
    assert manifest.checksum == "0" * 64
    assert manifest.size == 1


@pytest.mark.parametrize("missing", ["manifest.json", "manifest.json.sig"])
def test_a_missing_manifest_or_signature_is_not_found(
    hub_pins: selfcheck.Pins, missing: str
) -> None:
    release = Release()
    release.publish("2.1.326")
    del release.files[f"{BASE}/2.1.326/{missing}"]
    with pytest.raises(selfcheck.NotFound):
        read(release, hub_pins, "2.1.326")


def test_a_fetch_error_passes_through(hub_pins: selfcheck.Pins) -> None:
    release = Release()
    release.publish("2.1.326")
    release.errors[f"{BASE}/2.1.326/manifest.json.sig"] = selfcheck.FetchError("x")
    with pytest.raises(selfcheck.FetchError) as caught:
        read(release, hub_pins, "2.1.326")
    assert not isinstance(caught.value, selfcheck.NotFound)


def test_a_tampered_test_manifest_is_refused(hub_pins: selfcheck.Pins) -> None:
    release = Release()
    release.publish("2.1.326", tamper=True)
    with pytest.raises(SelfcheckError):
        read(release, hub_pins, "2.1.326")


def test_a_signature_that_is_not_utf8_is_refused(hub_pins: selfcheck.Pins) -> None:
    release = Release()
    release.publish("2.1.326")
    release.files[f"{BASE}/2.1.326/manifest.json.sig"] = b"\xff\xfe"
    with pytest.raises(SelfcheckError):
        read(release, hub_pins, "2.1.326")


CONTENT = binary_bytes("2.1.326")
GOOD_ENTRY = {
    "binary": "claude",
    "checksum": hashlib.sha256(CONTENT).hexdigest(),
    "size": len(CONTENT),
}


@pytest.mark.parametrize(
    "manifest",
    [
        b"[]",
        b"not json",
        manifest_bytes("2.1.325", BUILD_DATES["2.1.326"], CONTENT),
        manifest_bytes("2.1.326", "2026-11-16T17:26:18", CONTENT),
        manifest_bytes("2.1.326", "2026-11-16T17:26:18+00:00", CONTENT),
        manifest_bytes("2.1.326", "yesterday", CONTENT),
        manifest_bytes(
            "2.1.326", BUILD_DATES["2.1.326"], CONTENT, extra={"buildDate": 1}
        ),
        manifest_bytes(
            "2.1.326", BUILD_DATES["2.1.326"], CONTENT, extra={"platforms": {}}
        ),
        manifest_bytes(
            "2.1.326",
            BUILD_DATES["2.1.326"],
            CONTENT,
            extra={"platforms": {PLATFORM: "claude"}},
        ),
        manifest_bytes(
            "2.1.326",
            BUILD_DATES["2.1.326"],
            CONTENT,
            platform_entry={**GOOD_ENTRY, "binary": "claude.exe"},
        ),
        manifest_bytes(
            "2.1.326",
            BUILD_DATES["2.1.326"],
            CONTENT,
            platform_entry={**GOOD_ENTRY, "checksum": GOOD_ENTRY["checksum"].upper()},
        ),
        manifest_bytes(
            "2.1.326",
            BUILD_DATES["2.1.326"],
            CONTENT,
            platform_entry={**GOOD_ENTRY, "checksum": "ab" * 31},
        ),
        manifest_bytes(
            "2.1.326",
            BUILD_DATES["2.1.326"],
            CONTENT,
            platform_entry={**GOOD_ENTRY, "size": 0},
        ),
        manifest_bytes(
            "2.1.326",
            BUILD_DATES["2.1.326"],
            CONTENT,
            platform_entry={**GOOD_ENTRY, "size": -5},
        ),
        manifest_bytes(
            "2.1.326",
            BUILD_DATES["2.1.326"],
            CONTENT,
            platform_entry={**GOOD_ENTRY, "size": True},
        ),
        manifest_bytes(
            "2.1.326",
            BUILD_DATES["2.1.326"],
            CONTENT,
            platform_entry={**GOOD_ENTRY, "size": "100"},
        ),
        manifest_bytes(
            "2.1.326",
            BUILD_DATES["2.1.326"],
            CONTENT,
            platform_entry={"binary": "claude", "checksum": GOOD_ENTRY["checksum"]},
        ),
    ],
)
def test_a_signed_manifest_with_bad_content_is_refused(
    hub_pins: selfcheck.Pins, manifest: bytes
) -> None:
    release = Release()
    release.publish("2.1.326", manifest=manifest)
    with pytest.raises(SelfcheckError):
        read(release, hub_pins, "2.1.326")


# Default network implementations


@pytest.mark.parametrize(
    "url", ["http://downloads.claude.ai/x", "ftp://x/y", "file:///etc/passwd", ""]
)
def test_the_default_fetchers_refuse_other_schemes(tmp_path: Path, url: str) -> None:
    with pytest.raises(selfcheck.FetchError):
        selfcheck.http_get(url, 100)
    with pytest.raises(selfcheck.FetchError):
        selfcheck.http_download(url, tmp_path / "out", 100)
    assert not (tmp_path / "out").exists()


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.offset = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self.body)
        chunk = self.body[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        pass


def test_http_get_reads_up_to_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[object, object]] = []

    def urlopen(url: object, timeout: object = None) -> _Response:
        seen.append((url, timeout))
        return _Response(b"2.1.329\n")

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    assert selfcheck.http_get(LATEST, 8) == b"2.1.329\n"
    with pytest.raises(selfcheck.FetchError):
        selfcheck.http_get(LATEST, 7)
    assert seen[0][1] == selfcheck.FETCH_TIMEOUT_SEC


def test_http_get_turns_404_into_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    def urlopen(url: object, timeout: object = None) -> _Response:
        raise urllib.error.HTTPError(str(url), 404, "Not Found", None, None)  # type: ignore[arg-type]

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    with pytest.raises(selfcheck.NotFound):
        selfcheck.http_get(LATEST, 8)


@pytest.mark.parametrize("code", [403, 500])
def test_http_get_turns_other_errors_into_fetch_errors(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    import urllib.error

    def urlopen(url: object, timeout: object = None) -> _Response:
        raise urllib.error.HTTPError(str(url), code, "x", None, None)  # type: ignore[arg-type]

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    with pytest.raises(selfcheck.FetchError) as caught:
        selfcheck.http_get(LATEST, 8)
    assert not isinstance(caught.value, selfcheck.NotFound)


def test_http_get_turns_connection_errors_into_fetch_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import urllib.error

    def urlopen(url: object, timeout: object = None) -> _Response:
        raise urllib.error.URLError("no route")

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    with pytest.raises(selfcheck.FetchError):
        selfcheck.http_get(LATEST, 8)


def test_http_download_writes_the_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    body = os.urandom(3 * (1 << 20) + 5)
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda url, timeout=None: _Response(body)
    )
    selfcheck.http_download(f"{BASE}/x", tmp_path / "out", len(body))
    assert (tmp_path / "out").read_bytes() == body


def test_http_download_refuses_a_longer_body_and_removes_the_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    body = os.urandom((1 << 20) + 10)
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda url, timeout=None: _Response(body)
    )
    with pytest.raises(selfcheck.FetchError):
        selfcheck.http_download(f"{BASE}/x", tmp_path / "out", len(body) - 1)
    assert not (tmp_path / "out").exists()


def test_http_download_never_overwrites_an_existing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "out").write_bytes(b"keep")
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda url, timeout=None: _Response(b"new")
    )
    with pytest.raises((selfcheck.FetchError, FileExistsError)):
        selfcheck.http_download(f"{BASE}/x", tmp_path / "out", 3)
    assert (tmp_path / "out").read_bytes() == b"keep"


def test_run_command_and_run_session_use_the_given_arguments(tmp_path: Path) -> None:
    script = (
        "import os, sys; print(os.getcwd()); print(os.environ.get('X')); "
        "print(sys.stdin.read() == '')"
    )
    result = selfcheck.run_command([sys.executable, "-c", "print('hi')"], 10)
    assert (result.returncode, result.stdout) == (0, "hi\n")
    result = selfcheck.run_session(
        [sys.executable, "-c", script], tmp_path, {"X": "1"}, 10
    )
    assert result.stdout.splitlines() == [str(tmp_path.resolve()), "1", "True"]
    failed = selfcheck.run_command([sys.executable, "-c", "raise SystemExit(3)"], 10)
    assert failed.returncode == 3
    with pytest.raises(subprocess.TimeoutExpired):
        selfcheck.run_command([sys.executable, "-c", "import time; time.sleep(5)"], 0.5)


# Binary


def test_sha256_file(tmp_path: Path) -> None:
    data = os.urandom(3 * (1 << 20) + 1)
    (tmp_path / "f").write_bytes(data)
    assert selfcheck.sha256_file(tmp_path / "f") == hashlib.sha256(data).hexdigest()


def binary_setup(tmp_path: Path) -> tuple[selfcheck.Pins, selfcheck.Manifest, Path]:
    pins = selfcheck.load_pins(make_hub(tmp_path))
    content = binary_bytes("2.1.326")
    path = tmp_path / "versions" / "2.1.326"
    path.parent.mkdir()
    path.write_bytes(content)
    manifest = selfcheck.Manifest(
        "2.1.326", NOW, hashlib.sha256(content).hexdigest(), len(content)
    )
    return pins, manifest, path


def test_verify_binary_passes_and_runs_codesign_and_version(tmp_path: Path) -> None:
    pins, manifest, path = binary_setup(tmp_path)
    commands = Commands()
    assert selfcheck.verify_binary(path, manifest, pins, commands) is None
    assert commands.calls == [
        (
            [
                "/usr/bin/codesign",
                "--verify",
                "--strict",
                "-R=anchor apple generic and certificate leaf[subject.OU] = "
                '"Q6L2SF6YDW"',
                str(path),
            ],
            60,
        ),
        ([str(path), "--version"], 30),
    ]


def test_verify_binary_reports_size_first(tmp_path: Path) -> None:
    pins, manifest, path = binary_setup(tmp_path)
    path.write_bytes(b"x")
    commands = Commands()
    assert selfcheck.verify_binary(path, manifest, pins, commands) == "size"
    assert commands.calls == []
    path.unlink()
    assert selfcheck.verify_binary(path, manifest, pins, commands) == "size"


def test_verify_binary_reports_a_checksum_mismatch(tmp_path: Path) -> None:
    pins, manifest, path = binary_setup(tmp_path)
    data = bytearray(path.read_bytes())
    data[0] ^= 1
    path.write_bytes(bytes(data))
    commands = Commands()
    assert selfcheck.verify_binary(path, manifest, pins, commands) == "checksum"
    assert commands.calls == []


@pytest.mark.parametrize("code", [1, 3])
def test_verify_binary_reports_a_codesign_failure(tmp_path: Path, code: int) -> None:
    pins, manifest, path = binary_setup(tmp_path)
    commands = Commands(codesign_codes={"2.1.326": code})
    assert selfcheck.verify_binary(path, manifest, pins, commands) == "codesign"
    assert len(commands.calls) == 1


@pytest.mark.parametrize(
    "error", [OSError("x"), subprocess.TimeoutExpired(["codesign"], 60)]
)
def test_verify_binary_reports_codesign_that_cannot_run(
    tmp_path: Path, error: Exception
) -> None:
    pins, manifest, path = binary_setup(tmp_path)
    commands = Commands(raises={"codesign:2.1.326": error})
    assert selfcheck.verify_binary(path, manifest, pins, commands) == "codesign"


@pytest.mark.parametrize(
    "commands",
    [
        Commands(versions={"2.1.326": "2.1.325"}),
        Commands(versions={"2.1.326": ""}),
        Commands(version_codes={"2.1.326": 1}),
        Commands(raises={"version:2.1.326": OSError("x")}),
        Commands(raises={"version:2.1.326": subprocess.TimeoutExpired(["c"], 30)}),
    ],
)
def test_verify_binary_reports_a_version_failure(
    tmp_path: Path, commands: Commands
) -> None:
    pins, manifest, path = binary_setup(tmp_path)
    assert selfcheck.verify_binary(path, manifest, pins, commands) == "version"


# Layout and state file


def test_layout_and_state_paths(tmp_path: Path) -> None:
    layout = selfcheck.claude_layout(tmp_path)
    assert layout.link == tmp_path / ".local" / "bin" / "claude"
    assert layout.versions == tmp_path / ".local" / "share" / "claude" / "versions"
    assert selfcheck.agents_state_path(tmp_path) == tmp_path / "agents.json"


def test_a_missing_state_file_is_empty(tmp_path: Path) -> None:
    assert selfcheck.load_agents_state(tmp_path / "agents.json") == {
        "format": 1,
        "agents": {},
    }


CURRENT = {
    "version": "2.1.319",
    "sha256": "a" * 64,
    "path": "/x/versions/2.1.319",
    "at": "2026-11-20T09:00:00+00:00",
    "source": "bootstrap",
}


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        "[]",
        '{"agents": {}}',
        '{"format": 2, "agents": {}}',
        '{"format": "1", "agents": {}}',
        '{"format": 1}',
        '{"format": 1, "agents": []}',
        '{"format": 1, "agents": {"claude": []}}',
        '{"format": 1, "agents": {"claude": {"current": "2.1.319"}}}',
        '{"format": 1, "agents": {"claude": {"current": {"version": "2.1.319"}}}}',
        json.dumps(
            {"format": 1, "agents": {"claude": {"current": {**CURRENT, "sha256": 1}}}}
        ),
        json.dumps(
            {"format": 1, "agents": {"claude": {"current": CURRENT, "candidate": 5}}}
        ),
    ],
)
def test_a_malformed_state_file_is_refused(tmp_path: Path, text: str) -> None:
    (tmp_path / "agents.json").write_text(text, encoding="utf-8")
    with pytest.raises(SelfcheckError):
        selfcheck.load_agents_state(tmp_path / "agents.json")


def test_an_unreadable_state_file_is_refused(tmp_path: Path) -> None:
    (tmp_path / "agents.json").mkdir()
    with pytest.raises(SelfcheckError):
        selfcheck.load_agents_state(tmp_path / "agents.json")


def test_a_valid_state_file_loads_with_other_agents_kept(tmp_path: Path) -> None:
    state = {
        "format": 1,
        "agents": {
            "claude": {"current": CURRENT, "previous": None, "candidate": None},
            "codex": {"anything": [1, 2]},
        },
    }
    (tmp_path / "agents.json").write_text(json.dumps(state), encoding="utf-8")
    assert selfcheck.load_agents_state(tmp_path / "agents.json") == state


def test_save_writes_sorted_indented_json_with_mode_0600(tmp_path: Path) -> None:
    path = tmp_path / "agents.json"
    state = {"format": 1, "agents": {"claude": {"current": CURRENT}}}
    selfcheck.save_agents_state(path, state)
    text = path.read_text(encoding="utf-8")
    assert text == json.dumps(state, indent=2, sort_keys=True) + "\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == ["agents.json"]
    assert selfcheck.load_agents_state(path) == state


def test_save_replaces_atomically_and_cleans_up_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "agents.json"
    selfcheck.save_agents_state(path, {"format": 1, "agents": {}})
    before = path.read_bytes()

    def fail(*args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        selfcheck.save_agents_state(
            path, {"format": 1, "agents": {"claude": {"current": CURRENT}}}
        )
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["agents.json"]


# Report


def test_the_report_json_names_every_field() -> None:
    report = ClaudeReport(
        status="ready",
        reason=None,
        current="2.1.319",
        latest="2.1.329",
        candidate="2.1.326",
        checks=(CheckResult("FL-M3", "pass", ""), CheckResult("x", "fail", "a:b")),
        warnings=("w",),
        reused=False,
        notified="sent",
    )
    assert report.to_json() == {
        "status": "ready",
        "reason": None,
        "current": "2.1.319",
        "latest": "2.1.329",
        "candidate": "2.1.326",
        "checks": [
            {"id": "FL-M3", "result": "pass", "detail": ""},
            {"id": "x", "result": "fail", "detail": "a:b"},
        ],
        "warnings": ["w"],
        "reused": False,
        "notified": "sent",
    }
    json.dumps(report.to_json())


# run_claude


@dataclass
class Sent:
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    outcome: notify.Outcome = notify.Outcome.SENT

    def __call__(self, event: str, **kwargs: object) -> notify.Outcome:
        self.calls.append((event, kwargs))
        return self.outcome


@dataclass
class World:
    root: Path
    hub: Path
    home: Path
    orch_home: Path
    release: Release = field(default_factory=Release)
    commands: Commands = field(default_factory=Commands)
    claude: FakeClaude = field(default_factory=FakeClaude)
    sent: Sent = field(default_factory=Sent)
    settings: Config = field(default_factory=Config)
    now: datetime = NOW

    @property
    def link(self) -> Path:
        return self.home / ".local" / "bin" / "claude"

    @property
    def versions(self) -> Path:
        return self.home / ".local" / "share" / "claude" / "versions"

    @property
    def state_path(self) -> Path:
        return self.orch_home / "agents.json"

    def environ(self) -> dict[str, str]:
        return {
            "HOME": str(self.home),
            "USER": "tester",
            "LANG": "C.UTF-8",
            "GH_TOKEN": "ghs_secret",
            "PATH": "/opt/bin:/usr/bin",
        }

    def run(self) -> ClaudeReport:
        return selfcheck.run_claude(
            hub=self.hub,
            orch_home=self.orch_home,
            home=self.home,
            settings=self.settings,
            now=self.now,
            fetch=self.release.fetch,
            download=self.release.download,
            command=self.commands,
            session=self.claude,
            send=self.sent,
            machine=MACHINE,
            environ=self.environ(),
            python=sys.executable,
        )

    def install(self, version: str, content: bytes | None = None) -> Path:
        self.versions.mkdir(parents=True, exist_ok=True)
        path = self.versions / version
        path.write_bytes(binary_bytes(version) if content is None else content)
        path.chmod(0o755)
        return path

    def point_link(self, version: str) -> None:
        self.link.parent.mkdir(parents=True, exist_ok=True)
        if self.link.is_symlink() or self.link.exists():
            self.link.unlink()
        self.link.symlink_to(self.versions / version)

    def record_current(self, version: str = "2.1.319", **claude: object) -> None:
        path = self.install(version)
        self.point_link(version)
        entry: dict[str, object] = {
            "current": {
                "version": version,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "path": str(path),
                "at": "2026-11-19T00:00:00+00:00",
                "source": "bootstrap",
            },
            "previous": None,
            "candidate": None,
        }
        entry.update(claude)
        self.write_state({"format": 1, "agents": {"claude": entry}})

    def write_state(self, state: dict[str, object]) -> None:
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def state(self) -> dict[str, object]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def claude_state(self) -> dict[str, object]:
        return self.state()["agents"]["claude"]  # type: ignore[index,return-value]


@pytest.fixture
def world(tmp_path: Path) -> World:
    home = tmp_path / "home"
    orch_home = home / ".orch"
    orch_home.mkdir(parents=True)
    return World(tmp_path, make_hub(tmp_path), home, orch_home)


def results(report: ClaudeReport) -> list[tuple[str, str, str]]:
    return [(c.id, c.result, c.detail) for c in report.checks]


ALL_PASS = [(item, "pass", "") for item in CLAUDE_IDS]


def pins_sha(world: World) -> str:
    return hashlib.sha256((world.hub / "config" / "pins.toml").read_bytes()).hexdigest()


def warn_list(world: World) -> list[str]:
    return tomllib.loads(
        (world.hub / "config" / "pins.toml").read_text(encoding="utf-8")
    )["claude"]["warn"]


# bootstrap


def test_bootstrap_records_the_verified_current_binary(world: World) -> None:
    world.release.latest("2.1.319\n")
    world.release.publish("2.1.319")
    path = world.install("2.1.319")
    world.point_link("2.1.319")
    report = world.run()
    assert (report.status, report.reason, report.current) == (
        "up_to_date",
        "latest",
        "2.1.319",
    )
    assert report.latest == "2.1.319"
    assert world.state() == {
        "format": 1,
        "agents": {
            "claude": {
                "current": {
                    "version": "2.1.319",
                    "sha256": hashlib.sha256(binary_bytes("2.1.319")).hexdigest(),
                    "path": str(path.resolve()),
                    "at": NOW.isoformat(),
                    "source": "bootstrap",
                },
                "previous": None,
                "candidate": None,
            }
        },
    }
    assert world.sent.calls == []
    assert [argv[0] for argv, _ in world.commands.calls] == [
        "/usr/bin/codesign",
        str(path.resolve()),
    ]


def test_bootstrap_follows_a_relative_link(world: World) -> None:
    world.release.latest("2.1.319\n")
    world.release.publish("2.1.319")
    world.install("2.1.319")
    world.link.parent.mkdir(parents=True)
    world.link.symlink_to(Path("../share/claude/versions/2.1.319"))
    assert world.run().status == "up_to_date"
    assert world.claude_state()["current"]["version"] == "2.1.319"  # type: ignore[index]


def bootstrap_failure(world: World, reason: str) -> None:
    report = world.run()
    assert (report.status, report.reason) == ("unverified", reason)
    assert report.current is None
    assert not world.state_path.exists()
    assert world.sent.calls == []
    assert world.claude.calls == []
    assert world.release.downloads == []


def test_bootstrap_without_a_link_is_unverified(world: World) -> None:
    world.release.publish_measured()
    world.install("2.1.319")
    bootstrap_failure(world, "link")


def test_bootstrap_with_a_plain_file_is_unverified(world: World) -> None:
    world.release.publish_measured()
    world.link.parent.mkdir(parents=True)
    world.link.write_bytes(binary_bytes("2.1.319"))
    bootstrap_failure(world, "link")


def test_bootstrap_with_a_link_outside_the_versions_directory_is_unverified(
    world: World,
) -> None:
    world.release.publish_measured()
    other = world.root / "elsewhere" / "2.1.319"
    other.parent.mkdir()
    other.write_bytes(binary_bytes("2.1.319"))
    world.link.parent.mkdir(parents=True)
    world.link.symlink_to(other)
    bootstrap_failure(world, "link")


def test_bootstrap_with_a_link_to_a_non_version_name_is_unverified(
    world: World,
) -> None:
    world.release.publish_measured()
    world.install("latest")
    world.point_link("latest")
    bootstrap_failure(world, "link")


def test_bootstrap_of_a_version_other_than_the_baseline_is_unverified(
    world: World,
) -> None:
    world.release.publish_measured()
    world.install("2.1.326")
    world.point_link("2.1.326")
    bootstrap_failure(world, "baseline")


def test_bootstrap_without_a_manifest_is_unverified(world: World) -> None:
    world.release.latest("2.1.329\n")
    world.install("2.1.319")
    world.point_link("2.1.319")
    bootstrap_failure(world, "fetch")


def test_bootstrap_with_a_network_error_is_unverified(world: World) -> None:
    world.release.publish_measured()
    world.release.errors[f"{BASE}/2.1.319/manifest.json"] = selfcheck.FetchError("x")
    world.install("2.1.319")
    world.point_link("2.1.319")
    bootstrap_failure(world, "fetch")


def test_bootstrap_with_a_bad_signature_is_unverified(world: World) -> None:
    world.release.publish_measured()
    world.release.publish("2.1.319", tamper=True)
    world.install("2.1.319")
    world.point_link("2.1.319")
    bootstrap_failure(world, "signature")


def test_bootstrap_of_a_changed_binary_is_unverified(world: World) -> None:
    world.release.publish_measured()
    content = bytearray(binary_bytes("2.1.319"))
    content[3] ^= 1
    world.install("2.1.319", bytes(content))
    world.point_link("2.1.319")
    bootstrap_failure(world, "checksum")


def test_bootstrap_of_a_binary_with_a_wrong_code_signature_is_unverified(
    world: World,
) -> None:
    world.release.publish_measured()
    world.commands.codesign_codes["2.1.319"] = 3
    world.install("2.1.319")
    world.point_link("2.1.319")
    bootstrap_failure(world, "codesign")


# candidates


def test_the_measured_releases_give_2_1_326_as_a_ready_candidate(
    world: World,
) -> None:
    world.release.publish_measured()
    world.record_current()
    report = world.run()
    assert (report.status, report.reason) == ("ready", None)
    assert (report.current, report.latest, report.candidate) == (
        "2.1.319",
        "2.1.329",
        "2.1.326",
    )
    assert results(report) == ALL_PASS
    assert report.warnings == tuple(warn_list(world))
    assert report.reused is False
    assert report.notified == "sent"

    assert world.release.manifest_fetches() == [
        "2.1.329",
        "2.1.328",
        "2.1.327",
        "2.1.326",
    ]
    assert [(url, size) for url, _, size in world.release.downloads] == [
        (f"{BASE}/2.1.326/{PLATFORM}/claude", len(binary_bytes("2.1.326")))
    ]
    dest = world.versions / "2.1.326"
    assert dest.read_bytes() == binary_bytes("2.1.326")
    assert stat.S_IMODE(dest.stat().st_mode) == 0o755
    assert sorted(p.name for p in world.versions.iterdir()) == ["2.1.319", "2.1.326"]
    assert os.readlink(world.link) == str(world.versions / "2.1.319")

    candidate = world.claude_state()["candidate"]
    assert candidate == {
        "version": "2.1.326",
        "status": "ready",
        "sha256": hashlib.sha256(binary_bytes("2.1.326")).hexdigest(),
        "path": str(dest),
        "pins_sha256": pins_sha(world),
        "checks": [{"id": i, "result": r, "detail": d} for i, r, d in ALL_PASS],
        "warnings": warn_list(world),
        "at": NOW.isoformat(),
    }
    assert world.claude_state()["current"]["version"] == "2.1.319"  # type: ignore[index]
    assert world.claude_state()["previous"] is None

    assert world.sent.calls == [
        (
            "selfcheck_ready",
            {
                "settings": world.settings,
                "orch_home": world.orch_home,
                "home": world.home,
                "now": NOW,
                "target": "claude@2.1.326",
            },
        )
    ]


def test_the_sessions_run_the_candidate_with_a_clean_environment(
    world: World,
) -> None:
    world.release.publish_measured()
    world.record_current()
    world.run()
    assert world.claude.calls
    for _, argv, cwd, env, timeout in world.claude.calls:
        assert argv[0] == str(world.versions / "2.1.326")
        assert env == {
            "HOME": str(world.home),
            "PATH": selfcheck.SESSION_PATH,
            "LANG": "C.UTF-8",
            "USER": "tester",
            "DISABLE_AUTOUPDATER": "1",
        }
        assert timeout == selfcheck.SESSION_TIMEOUT_SEC
        assert cwd.name == "project"
    # the session check order follows the pins
    keys = world.claude.keys()
    assert keys.index("hook-allow") < keys.index("deny-control") < keys.index("inject")


def test_the_work_and_canary_directories_are_removed(world: World) -> None:
    world.release.publish_measured()
    world.record_current()
    seen: list[Path] = []
    world.claude.on_call = lambda key: seen.append(world.claude.calls[-1][2])
    world.run()
    project = seen[0]
    assert not project.parent.exists()
    assert project.parent.name.startswith("orch-selfcheck-")
    leftovers = [p.name for p in world.orch_home.iterdir()]
    assert not [name for name in leftovers if name.startswith("selfcheck-")]


def test_the_project_is_a_git_repository_during_the_sessions(world: World) -> None:
    world.release.publish_measured()
    world.record_current()
    seen: list[bool] = []
    world.claude.on_call = lambda key: seen.append(
        (world.claude.calls[-1][2] / ".git").is_dir()
    )
    world.run()
    assert seen and all(seen)


def test_a_held_version_is_skipped_without_a_fetch(world: World) -> None:
    world.release.publish_measured()
    world.record_current()
    world.settings = Config(selfcheck_hold=("claude@2.1.326", "codex@2.1.325"))
    report = world.run()
    assert (report.status, report.candidate) == ("ready", "2.1.325")
    assert world.release.manifest_fetches() == [
        "2.1.329",
        "2.1.328",
        "2.1.327",
        "2.1.325",
    ]


def test_a_shorter_minimum_age_picks_a_newer_candidate(world: World) -> None:
    world.release.publish_measured()
    world.record_current()
    world.settings = Config(selfcheck_min_age_days=1)
    assert world.run().candidate == "2.1.328"


def test_the_minimum_age_boundary_is_inclusive(world: World) -> None:
    world.release.publish_measured()
    world.record_current()
    world.now = datetime(2026, 11, 22, 16, 44, 15, tzinfo=UTC)
    assert world.run().candidate == "2.1.329"


def test_only_young_versions_leave_the_state_unchanged(world: World) -> None:
    world.release.publish_measured()
    world.record_current()
    before = world.state_path.read_bytes()
    world.now = datetime(2026, 11, 19, 0, 0, tzinfo=UTC)
    world.settings = Config(selfcheck_min_age_days=90)
    report = world.run()
    assert (report.status, report.reason, report.candidate) == (
        "up_to_date",
        "no_candidate",
        None,
    )
    assert world.state_path.read_bytes() == before
    assert world.release.manifest_fetches() == [
        "2.1.329",
        "2.1.328",
        "2.1.327",
        "2.1.326",
        "2.1.325",
        "2.1.324",
        "2.1.323",
        "2.1.322",
        "2.1.321",
        "2.1.320",
    ]
    assert world.sent.calls == []
    assert world.release.downloads == []


def test_a_latest_equal_to_or_older_than_current_is_up_to_date(world: World) -> None:
    world.release.publish_measured()
    world.record_current("2.1.329")
    report = world.run()
    assert (report.status, report.reason, report.latest) == (
        "up_to_date",
        "latest",
        "2.1.329",
    )
    assert world.release.manifest_fetches() == []
    world.release.latest("2.1.300")
    assert world.run().status == "up_to_date"


@pytest.mark.parametrize("body", [b"", b"latest", b"2.1", b"\xff\xfe", b"2.1.329 x"])
def test_an_unreadable_latest_is_unknown(world: World, body: bytes) -> None:
    world.release.publish_measured()
    world.release.files[LATEST] = body
    world.record_current()
    before = world.state_path.read_bytes()
    report = world.run()
    assert (report.status, report.reason) == ("unknown", "latest")
    assert world.state_path.read_bytes() == before
    assert world.sent.calls == []


def test_a_latest_fetch_error_is_unknown(world: World) -> None:
    world.release.publish_measured()
    world.release.errors[LATEST] = selfcheck.FetchError("offline")
    world.record_current()
    assert (world.run().status, world.run().reason) == ("unknown", "latest")
    assert world.release.fetches[0] == (LATEST, selfcheck.LATEST_LIMIT)


def test_a_latest_with_surrounding_whitespace_is_read(world: World) -> None:
    world.release.publish_measured()
    world.release.latest("  2.1.329\r\n")
    world.record_current()
    assert world.run().latest == "2.1.329"


def test_a_network_error_while_probing_is_unknown(world: World) -> None:
    world.release.publish_measured()
    world.release.errors[f"{BASE}/2.1.327/manifest.json"] = selfcheck.FetchError("x")
    world.record_current()
    before = world.state_path.read_bytes()
    report = world.run()
    assert (report.status, report.reason) == ("unknown", "fetch")
    assert world.state_path.read_bytes() == before
    assert world.release.downloads == []


def test_probing_stops_after_max_probes(world: World) -> None:
    world.release.latest("2.1.400\n")
    world.record_current()
    report = world.run()
    assert (report.status, report.reason) == ("up_to_date", "no_candidate")
    assert world.release.manifest_fetches() == [
        f"2.1.{n}" for n in range(400, 400 - selfcheck.MAX_PROBES, -1)
    ]


def test_held_versions_count_as_probes(world: World) -> None:
    world.release.latest("2.1.400\n")
    world.record_current()
    world.settings = Config(selfcheck_hold=("claude@2.1.400", "claude@2.1.399"))
    world.run()
    assert len(world.release.manifest_fetches()) == selfcheck.MAX_PROBES - 2


def test_probing_stops_at_patch_zero(world: World) -> None:
    world.release.latest("2.2.1\n")
    world.record_current()
    report = world.run()
    assert report.status == "up_to_date"
    assert world.release.manifest_fetches() == ["2.2.1", "2.2.0"]


def test_a_bad_signature_while_probing_fails_that_version(world: World) -> None:
    world.release.publish_measured()
    world.release.publish("2.1.328", tamper=True)
    world.record_current()
    report = world.run()
    assert (report.status, report.candidate) == ("failed", "2.1.328")
    assert results(report) == [
        ("FL-M3", "fail", "manifest:signature"),
        ("m14-cli", "skipped", ""),
        ("deny-rules", "skipped", ""),
        ("injection", "skipped", ""),
    ]
    assert world.release.manifest_fetches() == ["2.1.329", "2.1.328"]
    assert world.release.downloads == []
    assert world.claude.calls == []
    candidate = world.claude_state()["candidate"]
    assert candidate["status"] == "failed"  # type: ignore[index]
    assert candidate["sha256"] is None  # type: ignore[index]
    assert candidate["path"] is None  # type: ignore[index]
    assert [event for event, _ in world.sent.calls] == ["selfcheck_failed"]
    assert world.sent.calls[0][1]["target"] == "claude@2.1.328"


def test_a_manifest_with_bad_content_while_probing_fails_that_version(
    world: World,
) -> None:
    world.release.publish_measured()
    world.release.publish(
        "2.1.329",
        manifest=manifest_bytes("2.1.329", "not a date", binary_bytes("2.1.329")),
    )
    world.record_current()
    report = world.run()
    assert (report.status, report.candidate) == ("failed", "2.1.329")
    assert results(report)[0] == ("FL-M3", "fail", "manifest:signature")


# reuse


def test_a_ready_candidate_is_reused_without_checks_or_notification(
    world: World,
) -> None:
    world.release.publish_measured()
    world.record_current()
    first = world.run()
    state = world.state_path.read_bytes()
    sessions = len(world.claude.calls)
    downloads = len(world.release.downloads)
    world.now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    second = world.run()
    assert (second.status, second.candidate, second.reused) == (
        "ready",
        "2.1.326",
        True,
    )
    assert second.checks == first.checks
    assert second.warnings == first.warnings
    assert second.notified is None
    assert len(world.claude.calls) == sessions
    assert len(world.release.downloads) == downloads
    assert len(world.sent.calls) == 1
    assert world.state_path.read_bytes() == state


def test_a_ready_candidate_is_checked_again_after_the_pins_change(
    world: World,
) -> None:
    world.release.publish_measured()
    world.record_current()
    world.run()
    sessions = len(world.claude.calls)
    pins = world.hub / "config" / "pins.toml"
    pins.write_text(pins.read_text(encoding="utf-8") + "\n# changed\n", "utf-8")
    report = world.run()
    assert report.reused is False
    assert len(world.claude.calls) == 2 * sessions
    assert world.claude_state()["candidate"]["pins_sha256"] == pins_sha(world)  # type: ignore[index]
    assert len(world.sent.calls) == 2


def test_a_ready_candidate_whose_binary_changed_is_checked_again(
    world: World,
) -> None:
    world.release.publish_measured()
    world.record_current()
    world.run()
    dest = world.versions / "2.1.326"
    dest.write_bytes(b"changed")
    report = world.run()
    assert (report.status, report.reused) == ("ready", False)
    assert dest.read_bytes() == binary_bytes("2.1.326")
    assert len(world.release.downloads) == 2


def test_a_failed_candidate_is_checked_again(world: World) -> None:
    world.release.publish_measured()
    world.record_current()
    world.claude.hook_blocks = {"exit2", "deny", "exit1"}
    assert world.run().status == "failed"
    world.claude.hook_blocks = {"exit2", "deny"}
    report = world.run()
    assert (report.status, report.reused) == ("ready", False)


# not automated


def test_a_blocking_id_without_a_check_is_not_automated(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".orch").mkdir(parents=True)
    world = World(
        tmp_path,
        make_hub(tmp_path, blocking_extra=("new-check",)),
        home,
        home / ".orch",
    )
    world.release.publish_measured()
    world.record_current()
    report = world.run()
    assert (report.status, report.candidate) == ("not_automated", "2.1.326")
    assert results(report) == [
        ("FL-M3", "skipped", ""),
        ("m14-cli", "skipped", ""),
        ("deny-rules", "skipped", ""),
        ("new-check", "missing", ""),
        ("injection", "skipped", ""),
    ]
    assert world.release.downloads == []
    assert world.claude.calls == []
    assert world.commands.calls == []
    candidate = world.claude_state()["candidate"]
    assert candidate["status"] == "not_automated"  # type: ignore[index]
    assert candidate["sha256"] is None  # type: ignore[index]
    assert candidate["path"] is None  # type: ignore[index]
    assert [event for event, _ in world.sent.calls] == ["selfcheck_failed"]


# provenance and install failures


def failed_candidate(world: World, detail: str) -> ClaudeReport:
    report = world.run()
    assert (report.status, report.candidate) == ("failed", "2.1.326")
    assert results(report) == [
        ("FL-M3", "fail", detail),
        ("m14-cli", "skipped", ""),
        ("deny-rules", "skipped", ""),
        ("injection", "skipped", ""),
    ]
    assert world.claude.calls == []
    assert [event for event, _ in world.sent.calls] == ["selfcheck_failed"]
    assert report.notified == "sent"
    candidate = world.claude_state()["candidate"]
    assert candidate["sha256"] is None  # type: ignore[index]
    assert candidate["path"] is None  # type: ignore[index]
    return report


def no_temporary_files(world: World) -> None:
    assert not [p for p in world.versions.iterdir() if p.name.startswith(".")]


def test_a_served_binary_that_differs_from_the_manifest_fails(world: World) -> None:
    world.release.publish_measured()
    content = binary_bytes("2.1.326")
    world.release.publish("2.1.326", served=content[:-1] + b"X")
    world.record_current()
    failed_candidate(world, "binary:checksum")
    assert not (world.versions / "2.1.326").exists()
    no_temporary_files(world)


def test_a_short_binary_fails_on_size(world: World) -> None:
    world.release.publish_measured()
    world.release.publish("2.1.326", served=binary_bytes("2.1.326")[:-10])
    world.record_current()
    failed_candidate(world, "binary:size")
    assert not (world.versions / "2.1.326").exists()


def test_a_wrong_code_signature_fails(world: World) -> None:
    world.release.publish_measured()
    world.commands.codesign_codes["2.1.326"] = 3
    world.record_current()
    failed_candidate(world, "binary:codesign")
    assert not (world.versions / "2.1.326").exists()


def test_a_binary_reporting_another_version_fails(world: World) -> None:
    world.release.publish_measured()
    world.commands.versions["2.1.326"] = "2.1.319"
    world.record_current()
    failed_candidate(world, "binary:version")


def test_a_download_error_fails_and_leaves_no_temporary_file(world: World) -> None:
    world.release.publish_measured()
    world.release.errors[f"{BASE}/2.1.326/{PLATFORM}/claude"] = selfcheck.FetchError(
        "reset"
    )
    world.record_current()
    failed_candidate(world, "install:download")
    no_temporary_files(world)
    assert not (world.versions / "2.1.326").exists()


def test_the_download_goes_to_a_hidden_temporary_name_first(world: World) -> None:
    world.release.publish_measured()
    world.record_current()
    world.run()
    ((_, dest, _),) = world.release.downloads
    assert dest.parent == world.versions
    assert dest.name.startswith(".2.1.326.selfcheck-")
    assert not dest.exists()


def test_an_existing_correct_binary_is_used_without_a_download(world: World) -> None:
    world.release.publish_measured()
    world.record_current()
    world.install("2.1.326")
    report = world.run()
    assert report.status == "ready"
    assert world.release.downloads == []


def test_an_existing_wrong_binary_is_replaced_by_a_download(world: World) -> None:
    world.release.publish_measured()
    world.record_current()
    world.install("2.1.326", b"stale")
    report = world.run()
    assert report.status == "ready"
    assert len(world.release.downloads) == 1
    assert (world.versions / "2.1.326").read_bytes() == binary_bytes("2.1.326")


def test_the_versions_directory_is_created(world: World) -> None:
    world.release.publish_measured()
    world.record_current()
    target = world.versions / "2.1.319"
    elsewhere = world.root / "keep" / "2.1.319"
    elsewhere.parent.mkdir()
    target.rename(elsewhere)
    world.versions.rmdir()
    state = world.state()
    state["agents"]["claude"]["current"]["path"] = str(elsewhere)  # type: ignore[index]
    world.write_state(state)
    world.link.unlink()
    world.link.symlink_to(elsewhere)
    assert world.run().status == "ready"
    assert (world.versions / "2.1.326").is_file()


def test_a_candidate_that_the_link_points_to_is_in_use(world: World) -> None:
    world.release.publish_measured()
    world.record_current()
    world.install("2.1.326", b"someone else's")
    world.point_link("2.1.326")
    failed_candidate(world, "install:in_use")
    assert (world.versions / "2.1.326").read_bytes() == b"someone else's"
    assert world.release.downloads == []


def test_a_candidate_recorded_as_previous_is_in_use(world: World) -> None:
    world.release.publish_measured()
    previous = world.install("2.1.326", b"previous")
    world.record_current(
        previous={
            "version": "2.1.326",
            "sha256": hashlib.sha256(b"previous").hexdigest(),
            "path": str(previous),
            "at": "2026-11-18T00:00:00+00:00",
            "source": "apply",
        }
    )
    failed_candidate(world, "install:in_use")
    assert previous.read_bytes() == b"previous"


# session check failures


def test_a_failed_session_check_stops_the_later_checks(world: World) -> None:
    world.release.publish_measured()
    world.record_current()
    world.claude.hook_blocks = {"exit2", "deny", "exit1"}
    report = world.run()
    assert (report.status, report.reason) == ("failed", None)
    assert results(report) == [
        ("FL-M3", "pass", ""),
        ("m14-cli", "fail", "hook-exit1:not_created"),
        ("deny-rules", "skipped", ""),
        ("injection", "skipped", ""),
    ]
    assert world.claude.keys() == [
        "hook-allow",
        "hook-exit2",
        "hook-deny",
        "hook-exit1",
    ]
    assert not (world.versions / "2.1.326").exists()
    candidate = world.claude_state()["candidate"]
    assert candidate["status"] == "failed"  # type: ignore[index]
    assert candidate["sha256"] is None  # type: ignore[index]
    assert candidate["path"] is None  # type: ignore[index]
    assert [event for event, _ in world.sent.calls] == ["selfcheck_failed"]


def test_a_failed_injection_check_reports_the_case(world: World) -> None:
    world.release.publish_measured()
    world.record_current()
    world.claude.load_agents = False
    report = world.run()
    assert results(report)[-1] == ("injection", "fail", "inject:agent")
    assert report.status == "failed"


def test_a_link_changed_by_a_session_is_restored_and_fails(world: World) -> None:
    world.release.publish_measured()
    world.record_current()
    original = os.readlink(world.link)

    def relink(key: str) -> None:
        if key == "inject":
            world.link.unlink()
            world.link.symlink_to(world.versions / "2.1.326")

    world.claude.on_call = relink
    report = world.run()
    assert (report.status, report.reason) == ("failed", "link_changed")
    assert results(report) == ALL_PASS
    assert os.readlink(world.link) == original
    assert (world.versions / "2.1.319").exists()
    assert world.claude_state()["candidate"]["status"] == "failed"  # type: ignore[index]
    assert [event for event, _ in world.sent.calls] == ["selfcheck_failed"]


def test_a_link_created_by_a_session_is_removed(world: World) -> None:
    world.release.publish_measured()
    world.record_current()
    world.link.unlink()

    def relink(key: str) -> None:
        if key == "inject" and not world.link.is_symlink():
            world.link.symlink_to(world.versions / "2.1.326")

    world.claude.on_call = relink
    report = world.run()
    assert (report.status, report.reason) == ("failed", "link_changed")
    assert not world.link.is_symlink()
    assert not world.link.exists()


# replacing an older ready candidate


def ready_entry(world: World, version: str, path: Path) -> dict[str, object]:
    return {
        "version": version,
        "status": "ready",
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "path": str(path),
        "pins_sha256": pins_sha(world),
        "checks": [],
        "warnings": [],
        "at": "2026-11-19T00:00:00+00:00",
    }


def test_a_new_ready_candidate_removes_the_older_ready_one(world: World) -> None:
    world.release.publish_measured()
    old = world.install("2.1.325")
    world.record_current()
    state = world.state()
    state["agents"]["claude"]["candidate"] = ready_entry(world, "2.1.325", old)  # type: ignore[index]
    world.write_state(state)
    assert world.run().candidate == "2.1.326"
    assert not old.exists()
    assert (world.versions / "2.1.326").exists()


def test_a_failed_new_candidate_keeps_the_older_ready_one(world: World) -> None:
    world.release.publish_measured()
    old = world.install("2.1.325")
    world.record_current()
    state = world.state()
    state["agents"]["claude"]["candidate"] = ready_entry(world, "2.1.325", old)  # type: ignore[index]
    world.write_state(state)
    world.commands.codesign_codes["2.1.326"] = 1
    assert world.run().status == "failed"
    assert old.exists()


def test_an_older_ready_candidate_at_a_protected_path_is_kept(world: World) -> None:
    world.release.publish_measured()
    world.record_current()
    current = world.versions / "2.1.319"
    state = world.state()
    state["agents"]["claude"]["candidate"] = ready_entry(world, "2.1.319", current)  # type: ignore[index]
    world.write_state(state)
    assert world.run().status == "ready"
    assert current.exists()


# state handling


def test_other_agents_in_the_state_file_are_kept(world: World) -> None:
    world.release.publish_measured()
    world.record_current()
    state = world.state()
    state["agents"]["codex"] = {"current": {"version": "0.160.0"}, "x": [1]}  # type: ignore[index]
    world.write_state(state)
    world.run()
    assert world.state()["agents"]["codex"] == {  # type: ignore[index]
        "current": {"version": "0.160.0"},
        "x": [1],
    }
    assert stat.S_IMODE(world.state_path.stat().st_mode) == 0o600


def test_a_malformed_state_file_is_an_error_and_is_left_alone(world: World) -> None:
    world.release.publish_measured()
    world.state_path.write_text("{broken", encoding="utf-8")
    report = world.run()
    assert (report.status, report.reason) == ("error", "state")
    assert world.state_path.read_text(encoding="utf-8") == "{broken"
    assert world.release.fetches == []
    assert world.sent.calls == []


def test_invalid_pins_are_an_error(world: World) -> None:
    (world.hub / "config" / "pins.toml").write_text("[claude]\n", encoding="utf-8")
    report = world.run()
    assert (report.status, report.reason) == ("error", "pins")
    assert world.release.fetches == []


def test_a_key_that_does_not_match_the_pins_is_an_error(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".orch").mkdir(parents=True)
    world = World(
        tmp_path, make_hub(tmp_path, fingerprint="1" * 40), home, home / ".orch"
    )
    world.release.publish_measured()
    world.record_current()
    report = world.run()
    assert (report.status, report.reason) == ("error", "key")
    assert world.release.fetches == []


def test_an_unknown_machine_is_an_error(world: World) -> None:
    world.release.publish_measured()
    world.record_current()
    report = selfcheck.run_claude(
        hub=world.hub,
        orch_home=world.orch_home,
        home=world.home,
        settings=world.settings,
        now=NOW,
        fetch=world.release.fetch,
        download=world.release.download,
        command=world.commands,
        session=world.claude,
        send=world.sent,
        machine="sparc",
        environ=world.environ(),
        python=sys.executable,
    )
    assert (report.status, report.reason) == ("error", "platform")


def test_a_failed_state_write_is_an_error_without_a_notification(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.release.publish_measured()
    world.record_current()

    def fail(path: Path, state: object) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(selfcheck, "save_agents_state", fail)
    report = world.run()
    assert (report.status, report.reason) == ("error", "state_write")
    assert world.sent.calls == []


def test_the_notification_outcome_is_reported(world: World) -> None:
    world.release.publish_measured()
    world.record_current()
    world.sent.outcome = notify.Outcome.DISABLED
    assert world.run().notified == "disabled"


def test_a_held_lock_makes_the_run_busy(world: World) -> None:
    world.release.publish_measured()
    world.record_current()
    before = world.state_path.read_bytes()
    with (world.orch_home / "agents.json.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = world.run()
    assert (report.status, report.reason) == ("busy", None)
    assert world.release.fetches == []
    assert world.state_path.read_bytes() == before
    assert world.run().status == "ready"


def test_the_lock_is_released_after_an_error(world: World) -> None:
    (world.hub / "config" / "pins.toml").write_text("", encoding="utf-8")
    assert world.run().status == "error"
    with (world.orch_home / "agents.json.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_a_signed_manifest_helper_round_trips() -> None:
    document = manifest_bytes("2.1.326", BUILD_DATES["2.1.326"], CONTENT)
    assert sign_document(document).startswith("-----BEGIN PGP SIGNATURE-----")


# m-selfcheck-claude-3: unexpected errors, deletion limits and link restore


@pytest.fixture
def temp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "tmp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


def lock_is_free(world: World) -> None:
    with (world.orch_home / "agents.json.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)


def nothing_left(world: World, temp_root: Path) -> None:
    assert not [p for p in temp_root.iterdir() if p.name.startswith("orch-selfcheck-")]
    assert not [p for p in world.orch_home.iterdir() if p.name.startswith("selfcheck-")]


@pytest.mark.parametrize("git", ["/usr/bin/false", "missing"])
def test_a_failing_git_init_fails_the_candidate(
    world: World, temp_root: Path, monkeypatch: pytest.MonkeyPatch, git: str
) -> None:
    world.release.publish_measured()
    world.record_current()
    monkeypatch.setattr(
        selfcheck, "GIT", git if git.startswith("/") else str(world.root / git)
    )
    report = world.run()
    assert (report.status, report.candidate) == ("failed", "2.1.326")
    assert results(report) == [
        ("FL-M3", "pass", ""),
        ("m14-cli", "fail", "setup:git"),
        ("deny-rules", "skipped", ""),
        ("injection", "skipped", ""),
    ]
    assert world.claude.calls == []
    assert not (world.versions / "2.1.326").exists()
    candidate = world.claude_state()["candidate"]
    assert candidate["status"] == "failed"  # type: ignore[index]
    assert [event for event, _ in world.sent.calls] == ["selfcheck_failed"]
    nothing_left(world, temp_root)
    lock_is_free(world)


def test_a_raising_send_keeps_the_result(world: World) -> None:
    world.release.publish_measured()
    world.record_current()

    def boom(event: str, **kwargs: object) -> notify.Outcome:
        raise RuntimeError("ntfy exploded")

    report = selfcheck.run_claude(
        hub=world.hub,
        orch_home=world.orch_home,
        home=world.home,
        settings=world.settings,
        now=NOW,
        fetch=world.release.fetch,
        download=world.release.download,
        command=world.commands,
        session=world.claude,
        send=boom,
        machine=MACHINE,
        environ=world.environ(),
        python=sys.executable,
    )
    assert (report.status, report.candidate, report.notified) == (
        "ready",
        "2.1.326",
        "failed",
    )
    assert world.claude_state()["candidate"]["status"] == "ready"  # type: ignore[index]
    lock_is_free(world)


@pytest.mark.parametrize("where", ["fetch", "download", "codesign"])
def test_an_unexpected_error_is_an_internal_error(
    world: World, temp_root: Path, where: str
) -> None:
    world.release.publish_measured()
    world.record_current()
    before = world.state_path.read_bytes()
    if where == "fetch":
        world.release.errors[f"{BASE}/2.1.328/manifest.json"] = RuntimeError("x")
    elif where == "download":
        world.release.errors[f"{BASE}/2.1.326/{PLATFORM}/claude"] = RuntimeError("x")
    else:
        world.commands.raises["codesign:2.1.326"] = RuntimeError("x")
    report = world.run()
    assert (report.status, report.reason) == ("error", "internal")
    assert report.notified is None
    assert world.sent.calls == []
    assert world.state_path.read_bytes() == before
    assert world.claude.calls == []
    nothing_left(world, temp_root)
    lock_is_free(world)


def test_an_unexpected_error_during_bootstrap_is_internal(world: World) -> None:
    world.release.publish_measured()
    world.install("2.1.319")
    world.point_link("2.1.319")
    world.commands.raises["version:2.1.319"] = ValueError("x")
    report = world.run()
    assert (report.status, report.reason) == ("error", "internal")
    assert not world.state_path.exists()
    lock_is_free(world)


@pytest.mark.parametrize("kind", ["outside", "not_a_version", "directory"])
def test_an_older_ready_candidate_is_deleted_only_inside_versions(
    world: World, kind: str
) -> None:
    world.release.publish_measured()
    world.record_current()
    if kind == "outside":
        old = world.root / "elsewhere" / "2.1.325"
        old.parent.mkdir()
        old.write_bytes(b"keep me")
    elif kind == "not_a_version":
        old = world.versions / "notes.txt"
        old.write_bytes(b"keep me")
    else:
        old = world.versions / "2.1.325"
        old.mkdir()
        (old / "inside").write_bytes(b"keep me")
    entry = {
        "version": "2.1.325",
        "status": "ready",
        "sha256": "b" * 64,
        "path": str(old),
        "pins_sha256": pins_sha(world),
        "checks": [],
        "warnings": [],
        "at": "2026-11-19T00:00:00+00:00",
    }
    state = world.state()
    state["agents"]["claude"]["candidate"] = entry  # type: ignore[index]
    world.write_state(state)
    report = world.run()
    assert (report.status, report.candidate) == ("ready", "2.1.326")
    assert old.exists()


def test_a_link_that_cannot_be_restored_still_fails(world: World) -> None:
    world.release.publish_measured()
    world.record_current()
    bin_dir = world.link.parent

    def relink(key: str) -> None:
        if key == "inject":
            world.link.unlink()
            world.link.symlink_to(world.versions / "2.1.326")
            bin_dir.chmod(0o555)

    world.claude.on_call = relink
    try:
        report = world.run()
    finally:
        bin_dir.chmod(0o755)
    assert (report.status, report.reason) == ("failed", "link_changed")
    assert sorted(p.name for p in bin_dir.iterdir()) == ["claude"]
    lock_is_free(world)


def test_a_regular_file_at_the_link_is_never_touched(world: World) -> None:
    world.release.publish_measured()
    world.record_current()
    world.link.unlink()
    world.link.write_bytes(binary_bytes("2.1.319"))

    def replace(key: str) -> None:
        if key == "inject" and not world.link.is_symlink():
            world.link.unlink()
            world.link.symlink_to(world.versions / "2.1.326")

    world.claude.on_call = replace
    report = world.run()
    assert (report.status, report.reason) == ("failed", "link_changed")
    assert world.link.is_symlink()


def test_a_rewritten_regular_file_at_the_link_counts_as_changed(
    world: World,
) -> None:
    world.release.publish_measured()
    world.record_current()
    world.link.unlink()
    world.link.write_bytes(b"one")

    def rewrite(key: str) -> None:
        if key == "inject":
            world.link.unlink()
            world.link.write_bytes(b"other content")

    world.claude.on_call = rewrite
    report = world.run()
    assert (report.status, report.reason) == ("failed", "link_changed")
    assert world.link.read_bytes() == b"other content"


def test_a_regular_file_created_where_no_link_was_is_kept(world: World) -> None:
    world.release.publish_measured()
    world.record_current()
    world.link.unlink()

    def create(key: str) -> None:
        if key == "inject":
            world.link.write_bytes(b"created")

    world.claude.on_call = create
    report = world.run()
    assert (report.status, report.reason) == ("failed", "link_changed")
    assert world.link.read_bytes() == b"created"
