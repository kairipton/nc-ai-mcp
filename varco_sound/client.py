"""VARCO Sound 전용 클라이언트: 공통 HTTP 클라이언트로 호출하고 응답의 base64 오디오를 바이트로 푼다."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

import httpx

from nc_ai_common.config import ApiSettings
from nc_ai_common.errors import NcAiError, redact
from nc_ai_common.http_client import NcAiClient

from .api_spec import ENDPOINTS


@dataclass
class ApiResult:
    """한 번의 API 호출 결과. audios 는 디코딩된 오디오 바이트 목록."""

    endpoint: str
    audios: list[bytes]
    elapsed_s: float
    attempts: int
    request_id: str | None = None
    raw_keys: list[str] = field(default_factory=list)


class VarcoSoundClient:
    def __init__(self, settings: ApiSettings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.http = NcAiClient(settings, transport)

    async def call(self, endpoint: str, payload: dict[str, Any]) -> ApiResult:
        res = await self.http.post_json(ENDPOINTS[endpoint].path, payload)
        # 문서상 text2sound/variation 은 [{"audio": b64}, ...], 나머지는 {"audio": b64}
        items = res.body if isinstance(res.body, list) else [res.body]
        audios: list[bytes] = []
        for item in items:
            value = item.get("audio") if isinstance(item, dict) else None
            for b64 in (value if isinstance(value, list) else [value]):
                if not isinstance(b64, str) or not b64:
                    got = list(item) if isinstance(item, dict) else type(item).__name__
                    raise NcAiError("bad_response", redact(f"응답에 audio 문자열이 없습니다. 받은 키: {got}",
                                                           self.http.api_key), status=200)
                audios.append(base64.b64decode(b64))
        return ApiResult(endpoint=endpoint, audios=audios, elapsed_s=res.elapsed_s, attempts=res.attempts,
                         request_id=res.request_id,
                         raw_keys=sorted({k for it in items if isinstance(it, dict) for k in it}))
