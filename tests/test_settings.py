import pytest
from fastapi.testclient import TestClient

from app import llm, main
from app.config import load_config


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    checked = []

    async def fake_check(key):
        checked.append(key)
        return None if key == "sk-good" else "invalid API key"

    monkeypatch.setattr(llm, "check_key", fake_check)
    with TestClient(main.app) as c:
        c.checked = checked
        yield c


def test_settings_page_renders_and_hides_secrets(client):
    client.post("/settings", data={"anthropic_api_key": "sk-good", "strava_client_id": "123",
                                   "strava_client_secret": "topsecret"})
    html = client.get("/settings").text
    assert "sk-good" not in html and "topsecret" not in html
    assert "stored — leave blank to keep" in html
    assert "client_id=123" in html  # Connect link is live once the app credentials exist


def test_blank_secret_keeps_stored_value_and_key_is_verified(client):
    r = client.post("/settings", data={"anthropic_api_key": "sk-good"})
    assert "Claude API key verified" in r.text
    client.post("/settings", data={"anthropic_api_key": ""})
    client.post("/profile", data={"weight_kg": "72.5"})
    cfg = load_config(main.app.state.st.db)
    assert cfg.anthropic_api_key == "sk-good"
    assert cfg.weight_kg == 72.5
    assert client.checked == ["sk-good"]  # unchanged key isn't re-checked


def test_bad_key_and_bad_number_are_reported(client):
    assert "the Claude API key didn&#39;t work: invalid API key" in client.post("/settings", data={"anthropic_api_key": "sk-bad"}).text
    r = client.post("/profile", data={"lthr": "abc"})
    assert "lthr: &#39;abc&#39; is not a number" in r.text or "lthr: 'abc' is not a number" in r.text


def test_availability_and_clear_key(client):
    client.post("/settings", data={"anthropic_api_key": "sk-good"})
    client.post("/profile", data={"availability": "1", "available_days": ["tue", "sat"], "max_weekend_minutes": "240"})
    client.post("/settings", data={"clear_anthropic_api_key": "1"})
    cfg = load_config(main.app.state.st.db)
    assert cfg.available_days == ["tue", "sat"]
    assert cfg.max_weekend_minutes == 240
    assert cfg.max_minutes(0) == 0 and cfg.max_minutes(5) == 240
    # A form without the availability section must not wipe the days
    client.post("/profile", data={"weight_kg": "70"})
    cfg = load_config(main.app.state.st.db)
    assert cfg.available_days == ["tue", "sat"]
    assert cfg.anthropic_api_key == ""


def test_pages_split_profile_and_settings(client):
    client.post("/settings", data={"anthropic_api_key": "sk-good", "strava_client_secret": "topsecret",
                                   "notify_service": "notify.mobile_app_x"})
    settings = client.get("/settings").text
    profile = client.get("/profile").text
    assert 'name="weight_kg"' not in settings and 'name="available_days"' not in settings
    assert 'name="notify_service"' in settings and "notify.mobile_app_x" in settings
    assert 'name="weight_kg"' in profile and 'name="available_days"' in profile
    assert "sk-good" not in profile and "topsecret" not in profile and 'name="anthropic_api_key"' not in profile


def test_profile_save_keeps_secrets_and_recomputes(client):
    client.post("/settings", data={"anthropic_api_key": "sk-good"})
    r = client.post("/profile", data={"weight_kg": "68", "ftp_override": "260"})
    assert "Profile saved." in r.text
    cfg = load_config(main.app.state.st.db)
    assert cfg.anthropic_api_key == "sk-good"
    assert cfg.weight_kg == 68 and cfg.ftp_override == 260
