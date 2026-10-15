import pytest

from app import ha


@pytest.fixture(autouse=True)
def no_real_home_assistant(monkeypatch):
    """HA_URL/HA_TOKEN may be exported in the shell (the MCP server uses them); tests must never reach HA."""
    monkeypatch.setattr(ha, "HA_URL", "")
    monkeypatch.setattr(ha, "HA_TOKEN", "")
