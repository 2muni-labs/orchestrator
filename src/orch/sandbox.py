"""The macOS sandbox-exec profile that gate commands run under.

The profile was fixed by measurement on macOS 15.8.1 (24H32) on 2026-11-12: the full
test suite passes under it with no skipped tests, and it blocks the network, writes
outside the worktree and the per-command TMPDIR, reads of credential paths and the
keychain. Measure again after a macOS update (execution design, chapter 6).
"""

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
"""


class SandboxError(Exception):
    """The gate sandbox cannot start."""


def executable(environ: Mapping[str, str] | None = None) -> str:
    """Return sandbox-exec's path; fake agent mode may name a stand-in."""
    raise NotImplementedError


def wrap(
    argv: Sequence[str],
    *,
    executable: str,
    worktree: Path,
    tmpdir: Path,
    home: Path,
) -> list[str]:
    """Return argv run under the profile, with resolved paths as its parameters."""
    raise NotImplementedError


def check(*, executable: str, worktree: Path, tmpdir: Path, home: Path) -> None:
    """Start the profile once on a no-op command; raise SandboxError if it cannot."""
    raise NotImplementedError
