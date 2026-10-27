"""Shared fixtures. pytest loads this file automatically before the tests in this folder."""

import pytest

from dmgate.rules.opa import OpaServer, find_opa


@pytest.fixture(scope="session")
def opa_server():
    """One OPA sidecar for the whole test session. A server holds one soft table at a time, so every
    test pushes the table it needs before querying."""
    if find_opa() is None:
        pytest.skip("OPA binary missing (see CLAUDE.md)")
    with OpaServer() as server:
        yield server
