import pytest

from app import checkin, garmin, ha


@pytest.fixture(autouse=True)
def no_real_home_assistant(monkeypatch):
    """HA_URL/HA_TOKEN may be exported in the shell (the MCP server uses them); tests must never reach HA."""
    monkeypatch.setattr(ha, "HA_URL", "")
    monkeypatch.setattr(ha, "HA_TOKEN", "")
    # load_config falls back to ANTHROPIC_API_KEY: with it set, page views would start real check-ins
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # A Garmin call a test forgot to mock fails here instead of reaching Garmin (.invalid never resolves)
    monkeypatch.setattr(garmin, "BASE", "http://garmin.invalid")


@pytest.fixture(autouse=True)
def no_real_checkin(request, monkeypatch):
    """Every page render may start the daily check-in: with a key and a ride, that's a real Claude call. Only tests
    marked real_checkin get the real one."""
    if request.node.get_closest_marker("real_checkin"):
        return

    async def generate(db, cfg, today):
        return None

    monkeypatch.setattr(checkin, "generate", generate)
