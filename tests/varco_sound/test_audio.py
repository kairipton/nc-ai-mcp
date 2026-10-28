"""분석·후처리·파일명 규칙 (로컬 기능)."""

import numpy as np
import pytest
import soundfile as sf

from varco_sound import analysis, processing
from nc_ai_common.errors import NcAiError

from .conftest import SR, tone, write_wav


# ---------------- analysis

def test_silence_and_effective_duration(tmp_path):
    p = write_wav(tmp_path / "a.wav", tone(0.5, lead=0.2, tail=2.0))
    m = analysis.analyze(p)["metrics"]
    assert m["duration_s"] == pytest.approx(2.7, abs=0.01)
    assert m["leading_silence_ms"] == pytest.approx(200, abs=10)
    assert m["trailing_silence_ms"] > 1800
    assert m["effective_duration_s"] < 0.7


def test_brightness_and_band_balance(tmp_path):
    low = analysis.analyze(write_wav(tmp_path / "l.wav", tone(1.0, freq=100)))
    high = analysis.analyze(write_wav(tmp_path / "h.wav", tone(1.0, freq=8000)))
    assert low["metrics"]["low_ratio"] > 0.9 and high["metrics"]["high_ratio"] > 0.9
    assert low["metrics"]["spectral_centroid_hz"] < 300 < 5000 < high["metrics"]["spectral_centroid_hz"]
    assert "묵직" in low["interpretation"]["brightness"] and "쨍" in high["interpretation"]["brightness"]


def test_attack_sharp_vs_slow(tmp_path):
    sharp = analysis.analyze(write_wav(tmp_path / "s.wav", tone(1.0)))
    t = np.arange(SR) / SR
    swell = (0.5 * np.sin(2 * np.pi * 440 * t) * t).astype(np.float32)[:, None]
    slow = analysis.analyze(write_wav(tmp_path / "w.wav", swell))
    assert sharp["metrics"]["attack_ms"] < 15 and slow["metrics"]["attack_ms"] > 500


def test_clipping_detected(tmp_path):
    x = np.clip(tone(1.0, amp=3.0), -1, 1)
    m = analysis.analyze(write_wav(tmp_path / "c.wav", x))["metrics"]
    assert m["clipping_runs"] > 0 and m["peak_dbfs"] > -0.1


def test_stereo_width(tmp_path):
    mono_as_stereo = analysis.analyze(write_wav(tmp_path / "m.wav", tone(1.0, channels=2)))["metrics"]
    rng = np.random.default_rng(0)
    wide = analysis.analyze(write_wav(tmp_path / "w.wav", (rng.standard_normal((SR, 2)) * 0.2).astype(np.float32)))["metrics"]
    assert mono_as_stereo["stereo_width"] < 0.05 and wide["stereo_width"] > 0.8


def test_short_clip_lufs_none(tmp_path):
    m = analysis.analyze(write_wav(tmp_path / "s.wav", tone(0.2)))["metrics"]
    assert m["integrated_lufs"] is None


def test_rank_rejects_unknown_metric(tmp_path):
    a = analysis.analyze(write_wav(tmp_path / "a.wav", tone(1.0)))
    with pytest.raises(NcAiError) as e:
        analysis.rank([a], {"punchiness": 1})
    assert "punchiness" in e.value.message and e.value.allowed


# ---------------- processing

def test_trim_removes_silence(tmp_path):
    src = write_wav(tmp_path / "a.wav", tone(0.5, lead=0.3, tail=2.0))
    res = processing.trim_silence(src)
    assert res["duration_after_s"] < 0.8 and res["removed_start_ms"] > 250
    assert res["output"].endswith("a_trim.wav")


def test_trim_all_silent_errors(tmp_path):
    src = write_wav(tmp_path / "z.wav", np.zeros((SR, 1), dtype=np.float32))
    with pytest.raises(NcAiError):
        processing.trim_silence(src)


def test_normalize_lufs_hits_target(tmp_path):
    src = write_wav(tmp_path / "a.wav", tone(2.0, amp=0.05))
    res = processing.normalize(src, "lufs", -20, peak_ceiling_db=0)
    assert res["lufs_after"] == pytest.approx(-20, abs=0.5) and not res["limited_by_peak_ceiling"]


def test_normalize_respects_peak_ceiling(tmp_path):
    src = write_wav(tmp_path / "a.wav", tone(2.0, amp=0.05))
    res = processing.normalize(src, "lufs", -3, peak_ceiling_db=-1)
    assert res["limited_by_peak_ceiling"] and res["peak_dbfs_after"] == pytest.approx(-1, abs=0.1) and "note" in res


def test_normalize_peak_mode(tmp_path):
    src = write_wav(tmp_path / "a.wav", tone(0.2, amp=0.1))
    res = processing.normalize(src, "peak", -3)
    data, _ = sf.read(res["output"])
    assert 20 * np.log10(np.max(np.abs(data))) == pytest.approx(-3, abs=0.1)


def test_fade_ends_at_zero(tmp_path):
    src = write_wav(tmp_path / "a.wav", np.full((SR, 1), 0.5, dtype=np.float32))
    res = processing.fade(src, fade_in_ms=50, fade_out_ms=100, curve="cosine")
    data, _ = sf.read(res["output"])
    assert abs(data[0]) < 1e-3 and abs(data[-1]) < 0.01 and data[SR // 2] == pytest.approx(0.5, abs=1e-3)


def test_fade_too_long_errors(tmp_path):
    src = write_wav(tmp_path / "a.wav", tone(0.1))
    with pytest.raises(NcAiError):
        processing.fade(src, fade_in_ms=80, fade_out_ms=80)


@pytest.mark.parametrize("fmt", ["ogg", "mp3", "flac", "wav"])
def test_convert_formats_resample_channels(tmp_path, fmt):
    src = write_wav(tmp_path / "a.wav", tone(1.0, channels=2))
    res = processing.convert(src, fmt, sample_rate=22050, channels=1)
    info = sf.info(res["output"])
    assert info.samplerate == 22050 and info.channels == 1 and res["output"].endswith(f".{fmt}")


def test_never_overwrites_original_or_existing(tmp_path):
    src = write_wav(tmp_path / "a.wav", tone(1.0))
    with pytest.raises(NcAiError):
        processing.fade(src, fade_out_ms=10, output_path=str(src))
    target = tmp_path / "out.wav"
    processing.fade(src, fade_out_ms=10, output_path=str(target))
    with pytest.raises(NcAiError) as e:
        processing.fade(src, fade_out_ms=20, output_path=str(target))
    assert "overwrite" in e.value.fix
    processing.fade(src, fade_out_ms=20, output_path=str(target), overwrite=True)
    # 자동 경로는 번호를 붙인다
    a = processing.trim_silence(src)["output"]
    b = processing.trim_silence(src)["output"]
    assert a != b and b.endswith("a_trim2.wav")


# ---------------- 여러 소리가 든 파일

def three_hits():
    hit = tone(0.3, freq=120)
    gap = np.zeros((int(0.6 * SR), 1), dtype=np.float32)
    return np.concatenate([gap[:4410], hit, gap, hit * 0.7, gap, hit, gap])


def test_event_count_and_first_event_attack(tmp_path):
    m = analysis.analyze(write_wav(tmp_path / "k.wav", three_hits()))["metrics"]
    assert m["event_count"] == 3
    assert m["attack_ms"] < 15 and m["first_event_duration_s"] < 0.5  # 파일 전체(2.5s)가 아니라 첫 소리 기준
    assert m["effective_duration_s"] > 1.5


def test_split_events_writes_each_hit(tmp_path):
    src = write_wav(tmp_path / "k.wav", three_hits())
    res = processing.split_events(src)
    assert res["event_count"] == 3
    names = [o["output"].rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for o in res["outputs"]]
    assert names == ["k_ev01.wav", "k_ev02.wav", "k_ev03.wav"]
    for o in res["outputs"]:
        assert 0.25 < o["duration_s"] < 0.6
        assert analysis.analyze(__import__("pathlib").Path(o["output"]))["metrics"]["event_count"] == 1


def test_tremolo_sound_counts_as_one_event(tmp_path):
    """으르렁거림처럼 30ms 짧게 푹 꺼지는 떨림은 끊김이 아니다(실측 오판 회귀 방지)."""
    t = np.arange(int(1.5 * SR)) / SR
    gate = np.where((t % 0.25) < 0.22, 1.0, 0.001)  # 250ms 마다 30ms 동안 -60dB 로 꺼짐
    x = (0.5 * np.sin(2 * np.pi * 150 * t) * gate).astype(np.float32)[:, None]
    assert analysis.analyze(write_wav(tmp_path / "g.wav", x))["metrics"]["event_count"] == 1


def test_stereo_low_correlation_warning(tmp_path):
    rng = np.random.default_rng(0)
    x = (rng.standard_normal((SR, 2)) * 0.2).astype(np.float32)
    assert "모노로 합치면" in analysis.analyze(write_wav(tmp_path / "w.wav", x))["interpretation"]["stereo"]


def test_split_events_silent_file_errors(tmp_path):
    with pytest.raises(NcAiError):
        processing.split_events(write_wav(tmp_path / "z.wav", np.zeros((SR, 1), dtype=np.float32)))
