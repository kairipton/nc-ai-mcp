"""VarcoService: 검증(크레딧 전), 배치 분할, 저장·기록, 부분 실패, 후처리 기록, 비용 추정."""

import asyncio
import json

import httpx
import pytest

from nc_ai_common import manifest
from nc_ai_common.errors import NcAiError

from .conftest import tone, write_wav


def run(coro):
    return asyncio.run(coro)


# ---------------- generate_sfx

def test_generate_sfx_splits_into_batches_of_three(svc, api):
    res = run(svc.generate_sfx("heavy kick", n=5, description="kick heavy", note="테스트"))
    assert [b["num_sample"] for b in api.bodies()] == [3, 2]
    assert all(b["version"] == "v1" and b["prompt"] == "heavy kick" for b in api.bodies())
    assert len(res["files"]) == 5 and res["api_calls"] == 2
    first = res["files"][0]
    assert first["path"].endswith("_01.wav") and "sfx_kick_heavy_" in first["path"]
    assert first["sample_rate"] == 44100 and "quick_metrics" in first
    assert res["seed"] is None and res["credits_estimated"] > 0 and res["credits_remaining"] is None


def test_generate_sfx_writes_manifest(svc, api):
    res = run(svc.generate_sfx("click", n=1, purpose="ui", description="click", note="메뉴 버튼"))
    records = manifest.load(svc.settings.output_dir)
    assert len(records) == 1
    rec = records[0]
    assert rec["id"] == res["manifest_id"] and rec["service"] == "VARCO Sound"
    assert rec["tool"] == "generate_sfx" and rec["params"]["prompt"] == "click" and rec["note"] == "메뉴 버튼"
    assert rec["files"] == [f["path"] for f in res["files"]]


def test_generate_sfx_numbering_continues(svc, api):
    run(svc.generate_sfx("click", n=2, description="click"))
    res = run(svc.generate_sfx("click", n=1, description="click"))
    assert res["files"][0]["path"].endswith("_03.wav")


@pytest.mark.parametrize("kwargs,msg", [
    ({"prompt": "x" * 201}, "200"),
    ({"prompt": "  "}, "비었"),
    ({"prompt": "ok", "n": 13}, "n="),
    ({"prompt": "ok", "version": "v9"}, "version"),
])
def test_generate_sfx_rejects_bad_input_without_calling_api(svc, api, kwargs, msg):
    with pytest.raises(NcAiError) as e:
        run(svc.generate_sfx(**kwargs))
    assert e.value.code == "invalid_argument" and msg in e.value.message and e.value.fix is not None or e.value.allowed
    assert not api.requests


def test_partial_failure_keeps_saved_files(svc, api):
    api.push(lambda r: httpx.Response(200, json=[{"audio": __import__("tests.varco_sound.conftest", fromlist=["wav_b64"]).wav_b64()}] * 3))
    api.push(lambda r: httpx.Response(402, json={"error": "No available credits"}))
    with pytest.raises(NcAiError) as e:
        run(svc.generate_sfx("kick", n=6))
    d = e.value.to_dict()
    assert d["error"] == "insufficient_credits" and d["partial"] is True and len(d["saved_files"]) == 3
    assert manifest.load(svc.settings.output_dir)[0]["id"] == d["manifest_id"]


# ---------------- generate_from_sample / loop / stereo / conversion / enhance

def test_variation_payload_and_region(svc, api, tmp_path):
    src = write_wav(tmp_path / "src.wav", tone(2.0))
    res = run(svc.generate_from_sample(str(src), n=7, strength=0.5, include_begin=0.5))
    bodies = api.bodies()
    assert [b["num_sample"] for b in bodies] == [5, 2]
    assert bodies[0]["strength"] == 0.5 and bodies[0]["include"] == {"begin": 0.5, "end": 2.0}
    assert isinstance(bodies[0]["source"], str) and len(res["files"]) == 7
    assert res["files"][0]["path"].startswith(str(svc.settings.output_dir))


def test_variation_without_region_sends_no_include(svc, api, tmp_path):
    src = write_wav(tmp_path / "src.wav", tone(1.0))
    run(svc.generate_from_sample(str(src), n=1))
    assert "include" not in api.bodies()[0]


def test_variation_rejects_too_long(svc, api, tmp_path):
    src = write_wav(tmp_path / "src.wav", tone(10.5))
    with pytest.raises(NcAiError) as e:
        run(svc.generate_from_sample(str(src)))
    assert "길이" in e.value.message and e.value.allowed == "0.5~10.0s" and not api.requests


def test_variation_pads_short_source(svc, api, tmp_path):
    import base64, io, soundfile as sf
    src = write_wav(tmp_path / "hit.wav", tone(0.27))
    res = run(svc.generate_from_sample(str(src), n=1))
    sent = sf.info(io.BytesIO(base64.b64decode(api.bodies()[0]["source"])))
    assert sent.duration >= 0.5 and res["params"]["padded_to_s"] == 0.5


@pytest.mark.parametrize("b,e_", [(1.5, 1.0), (0.0, 5.0)])
def test_variation_rejects_bad_region(svc, api, tmp_path, b, e_):
    src = write_wav(tmp_path / "src.wav", tone(2.0))
    with pytest.raises(NcAiError):
        run(svc.generate_from_sample(str(src), include_begin=b, include_end=e_))
    assert not api.requests


def test_variation_strength_range(svc, api, tmp_path):
    src = write_wav(tmp_path / "src.wav", tone(1.0))
    with pytest.raises(NcAiError) as e:
        run(svc.generate_from_sample(str(src), strength=3.5))
    assert e.value.allowed == "0.0~3.0"


def test_missing_source_file(svc):
    with pytest.raises(NcAiError) as e:
        run(svc.generate_from_sample("C:/nope/missing.wav"))
    assert "파일이 없습니다" in e.value.message


def test_variation_records_parent_manifest_id(svc, api):
    gen = run(svc.generate_sfx("kick", n=1))
    var = run(svc.generate_from_sample(gen["files"][0]["path"], n=1))
    rec = [r for r in manifest.load(svc.settings.output_dir) if r["id"] == var["manifest_id"]][0]
    assert rec["parents"] == [gen["manifest_id"]]


def test_make_loop_preserve(svc, api, tmp_path):
    src = write_wav(tmp_path / "amb.wav", tone(3.0))
    run(svc.make_loop(str(src), preserve_begin=1.0, preserve_end=2.0))
    assert api.bodies()[0]["preserve"] == {"begin": 1.0, "end": 2.0}
    assert api.requests[0].url.path.endswith("/looping")


def test_make_loop_rejects_too_short(svc, api, tmp_path):
    src = write_wav(tmp_path / "amb.wav", tone(0.8))
    with pytest.raises(NcAiError):
        run(svc.make_loop(str(src)))
    assert not api.requests


def test_mono_to_stereo_downmixes_stereo_input(svc, api, tmp_path):
    import base64, io, soundfile as sf
    src = write_wav(tmp_path / "st.wav", tone(1.0, channels=2))
    run(svc.mono_to_stereo(str(src)))
    sent = sf.info(io.BytesIO(base64.b64decode(api.bodies()[0]["source"])))
    assert sent.channels == 1


def test_creature_voice_payload(svc, api, tmp_path):
    s = write_wav(tmp_path / "s.wav", tone(1.0))
    r = write_wav(tmp_path / "r.wav", tone(1.0, freq=120))
    run(svc.creature_voice(str(s), str(r), ratio=1.4, enhance=True))
    body = api.bodies()[0]
    assert body["ratio"] == 1.4 and body["enhance"] is True and body["source"] != body["reference"]


def test_creature_voice_ratio_range(svc, api, tmp_path):
    s = write_wav(tmp_path / "s.wav", tone(1.0))
    with pytest.raises(NcAiError) as e:
        run(svc.creature_voice(str(s), str(s), ratio=2.5))
    assert e.value.allowed == "0.0~2.0" and not api.requests


def test_enhance_speech_calls_endpoint(svc, api, tmp_path):
    s = write_wav(tmp_path / "v.wav", tone(1.0))
    res = run(svc.enhance_speech(str(s)))
    assert api.requests[0].url.path.endswith("/enhance") and len(res["files"]) == 1


# ---------------- 후처리 기록 / 관리

def test_postprocess_records_parent_and_keeps_original(svc, tmp_path):
    src = write_wav(tmp_path / "a.wav", tone(0.5, lead=0.3, tail=1.0))
    before = src.read_bytes()
    res = svc.trim_silence(str(src), note="trim test")
    assert src.read_bytes() == before and res["source"] == str(src)
    rec = manifest.load(tmp_path)[0]
    assert rec["tool"] == "trim_silence" and rec["parents"] == [str(src)] and rec["credits_estimated"] == 0


def test_convert_format_from_output_path(svc, tmp_path):
    src = write_wav(tmp_path / "a.wav", tone(1.0))
    res = svc.convert(str(src), output_path=str(tmp_path / "game" / "a.ogg"))
    assert res["format"] == "ogg" and res["output"].endswith("a.ogg")


def test_session_usage_and_estimate(svc, api):
    run(svc.generate_sfx("kick", n=4))
    usage = svc.check_credits()
    assert usage["session_calls"] == {"text2sound": 2} and usage["session_samples"] == {"text2sound": 4}
    assert usage["credits_remaining"] is None and usage["session_credits_estimated"] > 0
    est = svc.estimate_cost("generate_sfx", 4)
    assert est["api_calls"] == 2 and est["credits_estimated"] == usage["session_credits_estimated"]
    with pytest.raises(NcAiError):
        svc.estimate_cost("trim_silence")


def test_list_history_query(svc, api):
    run(svc.generate_sfx("leather kick", n=1, note="auto-soccer"))
    run(svc.generate_sfx("ui click", n=1))
    hits = svc.list_history(query="kick")
    assert hits["count"] == 1 and hits["records"][0]["params"]["prompt"] == "leather kick"
    assert svc.list_history(tool="generate_sfx")["count"] == 2


def test_audition_page(svc, api):
    res = run(svc.generate_sfx("kick", n=2))
    page = svc.build_audition_page([f["path"] for f in res["files"]], note="귀로 골라 주세요")
    html = open(page["page"], encoding="utf-8").read()
    assert html.count("<audio") == 2 and "kick" in html and "귀로 골라 주세요" in html
    # 같은 폴더라 상대 경로
    assert 'src="sfx_kick_' in html


def test_compare_ranking(svc, tmp_path):
    low = write_wav(tmp_path / "low.wav", tone(0.4, freq=80))
    high = write_wav(tmp_path / "high.wav", tone(0.4, freq=6000))
    res = svc.compare_audio([str(high), str(low)], target={"spectral_centroid_hz": 100, "low_ratio": 0.9},
                            goal="묵직한 저음")
    assert res["ranking"][0]["path"] == str(low) and res["ranking"][0]["rank"] == 1
    assert len(res["table"]) == 2 and json.dumps(res, ensure_ascii=False)


def test_split_events_records_manifest(svc, tmp_path):
    from .test_audio import three_hits
    src = write_wav(tmp_path / "k.wav", three_hits())
    res = svc.split_events(str(src), note="split")
    rec = manifest.load(tmp_path)[0]
    assert rec["tool"] == "split_events" and len(rec["files"]) == 3 and rec["parents"] == [str(src)]
    assert res["outputs"][0]["quick_metrics"]["event_count"] == 1
