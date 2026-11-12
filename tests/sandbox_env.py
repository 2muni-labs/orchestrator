"""The gate sandbox in tests: a pass-through stand-in, and when the real one can run.

sandbox-exec cannot start inside another sandbox (sandbox_apply fails, exit 71,
measured on macOS 15.8.1). Once G2 runs under the gate sandbox, tests that need the
real sandbox-exec skip there and run when the suite runs outside a sandbox.
"""

import ctypes
import os
import sys
from pathlib import Path

FAKE_SANDBOX_EXEC = Path(__file__).resolve().parent / "fake_sandbox" / "sandbox-exec"
REAL_SANDBOX_EXEC = "/usr/bin/sandbox-exec"


def inside_sandbox() -> bool:
    """Return whether this process already runs under a macOS sandbox."""
    if sys.platform != "darwin":
        return False
    lib = ctypes.CDLL("/usr/lib/libSystem.dylib")
    lib.sandbox_check.restype = ctypes.c_int
    return lib.sandbox_check(os.getpid(), None, 0) != 0


def real_sandbox_reason() -> str | None:
    """Return why the real sandbox-exec cannot run here, or None if it can."""
    if sys.platform != "darwin":
        return "sandbox-exec exists only on macOS"
    if inside_sandbox():
        return "already inside a sandbox, where sandbox-exec cannot start"
    return None
