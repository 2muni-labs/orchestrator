"""Shared test setup.

pytest inserts this directory into sys.path for this conftest, so helper modules
here, such as transitions.py, can be imported from tests/integration as well.
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
