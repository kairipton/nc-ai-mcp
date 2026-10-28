"""설정 우선순위와 manifest 기록·파일명 규칙(공통)."""

from datetime import datetime

from nc_ai_common import manifest
from nc_ai_common.config import ConfigSource, load_api_settings


def test_priority_env_over_dotenv_over_toml(tmp_path):
    (tmp_path / ".env").write_text("VARCO_API_KEY=from-dotenv\nVARCO_TIMEOUT=50\n", encoding="utf-8")
    (tmp_path / "config.toml").write_text('[api]\ntimeout_s = 70\nmax_retries = 5\nbase_url = "https://toml/"\n',
                                          encoding="utf-8")
    s = load_api_settings(ConfigSource(tmp_path, {"VARCO_TIMEOUT": "30"}))
    assert s.timeout_s == 30 and s.max_retries == 5 and s.base_url == "https://toml"
    assert s.api_key == "from-dotenv"


def test_key_never_read_from_toml(tmp_path):
    (tmp_path / "config.toml").write_text('[api]\napi_key = "nope"\n', encoding="utf-8")
    s = load_api_settings(ConfigSource(tmp_path, {}))
    assert s.api_key is None


def test_env_key_wins(tmp_path):
    (tmp_path / ".env").write_text("VARCO_API_KEY=dotenv\n", encoding="utf-8")
    assert load_api_settings(ConfigSource(tmp_path, {"VARCO_API_KEY": "env"})).api_key == "env"


def test_naming_rule(tmp_path):
    now = datetime(2026, 10, 28)
    p1 = manifest.next_audio_path(tmp_path, "SFX", "Kick Heavy!", now=now)
    assert p1.name == "sfx_kick_heavy_20261028_01.wav"
    p1.write_bytes(b"x")
    assert manifest.next_audio_path(tmp_path, "sfx", "kick heavy", now=now).name.endswith("_02.wav")
    assert manifest.next_audio_path(tmp_path, "sfx", "축구공", now=now).name == "sfx_sound_20261028_01.wav"


def test_append_and_search(tmp_path):
    a = manifest.append(tmp_path, {"tool": "t1", "params": {"prompt": "leather kick"}, "files": ["a.wav"]}, "Svc")
    manifest.append(tmp_path, {"tool": "t2", "params": {"prompt": "click"}, "files": ["b.wav"]}, "Svc")
    assert a["service"] == "Svc" and a["id"].startswith("m_")
    assert [r["tool"] for r in manifest.search(tmp_path, query="KICK")] == ["t1"]
    assert manifest.find_by_file(tmp_path, "b.wav")["tool"] == "t2"
    assert not (tmp_path / "manifest.json.lock").exists()
