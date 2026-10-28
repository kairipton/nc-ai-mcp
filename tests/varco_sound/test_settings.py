"""사운드 설정: 크레딧 모드 기본 예산과 직접 지정 우선순위."""

import pytest

from nc_ai_common.config import ConfigSource
from varco_sound.settings import load_settings


def test_credit_mode_presets_and_override(tmp_path):
    assert load_settings(ConfigSource(tmp_path, {})).session_budget == 60
    s = load_settings(ConfigSource(tmp_path, {"VARCO_CREDIT_MODE": "saver"}))
    assert (s.credit_mode, s.session_budget) == ("saver", 20)
    s = load_settings(ConfigSource(tmp_path, {"VARCO_CREDIT_MODE": "flex", "VARCO_SESSION_BUDGET": "500"}))
    assert (s.credit_mode, s.session_budget) == ("flex", 500)
    assert load_settings(ConfigSource(tmp_path, {"VARCO_SESSION_BUDGET": "0"})).session_budget is None
    (tmp_path / "config.toml").write_text('[varco_sound]\ncredit_mode = "flex"\n', encoding="utf-8")
    assert load_settings(ConfigSource(tmp_path, {})).session_budget == 200
    with pytest.raises(ValueError):
        load_settings(ConfigSource(tmp_path, {"VARCO_CREDIT_MODE": "yolo"}))
