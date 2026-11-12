"""The macOS sandbox-exec profile that gate commands run under.

The profile was fixed by measurement on macOS 15.8.1 (24H32) on 2026-11-12 and
extended on 2026-11-13. It blocks the network, writes outside the worktree and the
per-command TMPDIR, writes to the worktree's .git file and .venv, reads of
credential paths and the keychain, and starting processes outside the sandbox
through launchd, LaunchServices or AppleEvents. The full test suite passes under it.
Measure again after a macOS update (execution design, chapter 6).
"""

import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
FAKE_ENV = "ORCH_FAKE_SANDBOX_EXEC"
CHECK_COMMAND = "/usr/bin/true"
CHECK_TIMEOUT_SEC = 30

PROFILE = """\
(version 1)
(allow default)

(deny network*)

(deny file-write*)
(allow file-write*
  (subpath (param "WORKTREE"))
  (subpath (param "TMPDIR"))
  (literal "/dev/null")
  (literal "/dev/zero")
  (literal "/dev/tty")
  (literal "/dev/dtracehelper")
  (regex #"^/dev/fd/[0-9]+$")
  (regex #"^/dev/ttys[0-9]+$"))

(deny file-read* file-write*
  (subpath (string-append (param "HOME") "/.orch/secrets"))
  (literal (string-append (param "HOME") "/.orch/config"))
  (subpath (string-append (param "HOME") "/.ssh"))
  (subpath (string-append (param "HOME") "/.config/gh"))
  (subpath (string-append (param "HOME") "/.codex"))
  (subpath (string-append (param "HOME") "/.claude"))
  (literal (string-append (param "HOME") "/.claude.json"))
  (subpath (string-append (param "HOME") "/Library/Keychains"))
  (subpath "/Library/Keychains"))

(deny mach-lookup
  (global-name "com.apple.SecurityServer")
  (global-name "com.apple.securityd")
  (global-name "com.apple.securityd.xpc")
  (global-name "com.apple.security.agent")
  (global-name "com.apple.trustd.agent"))

(allow process-exec (literal "/bin/ps") (with no-sandbox))

(deny file-write*
  (literal (string-append (param "WORKTREE") "/.git"))
  (subpath (string-append (param "WORKTREE") "/.venv")))

(deny file-read* file-write*
  (literal (string-append (param "HOME") "/.git-credentials"))
  (subpath (string-append (param "HOME") "/.config/git/credentials"))
  (literal (string-append (param "HOME") "/.netrc"))
  (subpath (string-append (param "HOME") "/.aws"))
  (subpath (string-append (param "HOME") "/.docker"))
  (literal (string-append (param "HOME") "/.npmrc"))
  (literal (string-append (param "HOME") "/.pypirc"))
  (subpath (string-append (param "HOME") "/.kube"))
  (subpath (string-append (param "HOME") "/.gnupg"))
  (subpath (string-append (param "HOME") "/.config/gcloud"))
  (subpath (string-append (param "HOME") "/.azure"))
  (subpath (string-append (param "HOME") "/.config/hub"))
  (subpath (string-append (param "HOME") "/.config/glab-cli"))
  (subpath (string-append (param "HOME") "/.cargo/credentials"))
  (literal (string-append (param "HOME") "/.cargo/credentials.toml"))
  (literal (string-append (param "HOME") "/.gem/credentials"))
  (subpath (string-append (param "HOME") "/.terraform.d"))
  (literal (string-append (param "HOME") "/.vault-token"))
  (literal (string-append (param "HOME") "/.pgpass"))
  (literal (string-append (param "HOME") "/.boto"))
  (literal (string-append (param "HOME") "/.s3cfg"))
  (subpath (string-append (param "HOME") "/.password-store"))
  (subpath (string-append (param "HOME") "/.config/op"))
  (subpath (string-append (param "HOME") "/.huggingface"))
  (subpath (string-append (param "HOME") "/.cache/huggingface")))

(deny appleevent-send)

(deny mach-lookup
  (global-name "com.apple.coreservices.launchservicesd")
  (global-name "com.apple.coreservices.quarantine-resolver")
  (global-name "com.apple.lsd.mapdb")
  (global-name "com.apple.lsd.modifydb")
  (global-name "com.apple.lsd.open")
  (global-name "com.apple.lsd.xpc"))

(deny process-exec
  (literal "/usr/bin/open")
  (literal "/usr/bin/osascript")
  (literal "/bin/launchctl"))
"""


class SandboxError(Exception):
    """The gate sandbox cannot start."""


def executable(environ: Mapping[str, str] | None = None) -> str:
    """Return sandbox-exec's path; fake agent mode may name a stand-in."""
    env = os.environ if environ is None else environ
    if env.get("ORCH_FAKE_AGENTS") == "1" and env.get(FAKE_ENV):
        return env[FAKE_ENV]
    return SANDBOX_EXEC


def wrap(
    argv: Sequence[str],
    *,
    executable: str,
    worktree: Path,
    tmpdir: Path,
    home: Path,
) -> list[str]:
    """Return argv run under the profile, with resolved paths as its parameters."""
    return [
        executable,
        "-p",
        PROFILE,
        "-D",
        f"WORKTREE={worktree.resolve()}",
        "-D",
        f"TMPDIR={tmpdir.resolve()}",
        "-D",
        f"HOME={home.resolve()}",
        *argv,
    ]


def check(*, executable: str, worktree: Path, tmpdir: Path, home: Path) -> None:
    """Start the profile once on a no-op command; raise SandboxError if it cannot."""
    argv = wrap(
        [CHECK_COMMAND],
        executable=executable,
        worktree=worktree,
        tmpdir=tmpdir,
        home=home,
    )
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=CHECK_TIMEOUT_SEC,
        )
    except OSError as exc:
        detail = str(exc).splitlines()[0]
        raise SandboxError(f"샌드박스 실행 불가: {detail}") from exc
    except subprocess.TimeoutExpired as exc:
        raise SandboxError(f"샌드박스 점검 시간 초과: {CHECK_TIMEOUT_SEC}초") from exc
    if result.returncode != 0:
        lines = result.stderr.splitlines()
        detail = f": {lines[0]}" if lines else ""
        raise SandboxError(f"샌드박스 점검 종료 코드 {result.returncode}{detail}")
