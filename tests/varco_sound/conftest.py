"""공용 픽스처: 임시 설정, 합성 오디오, 가짜 API(httpx.MockTransport)."""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from typing import Callable

import httpx
import numpy as np
import pytest
import soundfile as sf

from nc_ai_common.config import ApiSettings
from varco_sound.client import VarcoSoundClient
from varco_sound.settings import SoundSettings
from varco_sound.service import VarcoService

SECRET = "SECRET-TEST-KEY-1234567890"
SR = 44100


def make_settings(tmp_path: Path, key: str | None = SECRET, retries: int = 2) -> SoundSettings:
    api = ApiSettings(api_key=key, base_url="https://api.test", timeout_s=5, max_retries=retries)
    return SoundSettings(api=api, output_dir=tmp_path / "out", default_format="wav", max_candidates=12)


def tone(duration: float = 1.0, freq: float = 440.0, sr: int = SR, amp: float = 0.5,
         lead: float = 0.0, tail: float = 0.0, channels: int = 1) -> np.ndarray:
    """앞뒤 무음(lead/tail 초)이 붙은 지수 감쇠 사인파."""
    t = np.arange(int(duration * sr)) / sr
    body = amp * np.sin(2 * np.pi * freq * t) * np.exp(-3 * t)
    x = np.concatenate([np.zeros(int(lead * sr)), body, np.zeros(int(tail * sr))]).astype(np.float32)
    return np.repeat(x[:, None], channels, axis=1)


def write_wav(path: Path, data: np.ndarray, sr: int = SR) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), data, sr, subtype="PCM_16")
    return path


def wav_b64(data: np.ndarray | None = None, sr: int = SR) -> str:
    buf = io.BytesIO()
    sf.write(buf, tone(0.5, lead=0.1, tail=9.4) if data is None else data, sr, format="WAV", subtype="PCM_16")
    return base64.b64encode(buf.getvalue()).decode()


class FakeApi:
    """요청을 기록하고, 큐에 넣은 응답을 순서대로 돌려준다. 큐가 비면 기본 성공 응답."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.queue: list[Callable[[httpx.Request], httpx.Response]] = []

    def push(self, fn: Callable[[httpx.Request], httpx.Response]) -> None:
        self.queue.append(fn)

    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.queue:
            return self.queue.pop(0)(request)
        body = json.loads(request.content)
        if request.url.path.endswith(("text2sound", "variation")):
            return httpx.Response(200, json=[{"audio": wav_b64()} for _ in range(body.get("num_sample", 1))])
        return httpx.Response(200, json={"audio": wav_b64()})


@pytest.fixture
def api() -> FakeApi:
    return FakeApi()


@pytest.fixture
def client_factory(tmp_path, api):
    def make(key: str | None = SECRET, retries: int = 2) -> VarcoSoundClient:
        c = VarcoSoundClient(make_settings(tmp_path, key, retries).api, transport=httpx.MockTransport(api.handler))

        async def no_sleep(_s: float) -> None:
            return None

        c.http._sleep = no_sleep
        return c
    return make


@pytest.fixture
def svc(tmp_path, client_factory) -> VarcoService:
    s = make_settings(tmp_path)
    return VarcoService(s, client_factory())
