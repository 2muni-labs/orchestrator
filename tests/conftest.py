"""Shared test setup.

Tests live in one directory per test group (tests/<group>/, tests/integration/).
pyproject.toml puts this directory and every group directory on sys.path, so
helper modules here, such as transitions.py, and test modules of other groups can
be imported by bare name.
"""

import pytest

from orch import sandbox
from sandbox_env import FAKE_SANDBOX_EXEC


@pytest.fixture(autouse=True)
def fake_gate_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run gate commands through a pass-through stand-in for sandbox-exec.

    Tests that need the real one take the real_sandbox fixture in test_sandbox.py.
    """
    monkeypatch.setattr(sandbox, "SANDBOX_EXEC", str(FAKE_SANDBOX_EXEC))
