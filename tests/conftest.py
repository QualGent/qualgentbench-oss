import pytest


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Strip QGB_* vars so the developer's .env can't leak into assertions.

    Tests that care about a value set it themselves.
    """
    for var in ("QGB_DISALLOWED_TOOLS", "QGB_MCP_SERVER", "QGB_ADB_PATH", "QGB_CACHE_DIR"):
        monkeypatch.delenv(var, raising=False)
