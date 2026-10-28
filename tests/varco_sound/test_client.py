"""VarcoSoundClient: 엔드포인트 경로와 base64 오디오 응답 해석. (재시도·오류 변환·키 비노출은 tests/common)"""

import asyncio

import httpx
import pytest

from nc_ai_common.errors import NcAiError

from .conftest import wav_b64


def run(coro):
    return asyncio.run(coro)


def test_list_response_parsed_into_audio_bytes(client_factory, api):
    api.push(lambda r: httpx.Response(200, json=[{"audio": wav_b64()}, {"audio": wav_b64()}]))
    result = run(client_factory().call("text2sound", {"prompt": "x", "num_sample": 2}))
    assert len(result.audios) == 2 and result.audios[0][:4] == b"RIFF" and result.attempts == 1
    assert api.requests[0].url.path == "/sound/varco/v1/api/text2sound"


def test_object_response_parsed(client_factory, api):
    api.push(lambda r: httpx.Response(200, json={"audio": wav_b64()}))
    assert len(run(client_factory().call("enhance", {"source": "x"})).audios) == 1


def test_bad_response_shape(client_factory, api):
    api.push(lambda r: httpx.Response(200, json={"result": "?"}))
    with pytest.raises(NcAiError) as e:
        run(client_factory().call("enhance", {"source": "x"}))
    assert e.value.code == "bad_response" and "result" in e.value.message
