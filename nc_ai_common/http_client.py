"""NC AI API Platform HTTP 클라이언트(모든 서버 공통).

책임: JSON POST 전송, 일시적 오류(429·5xx·네트워크)만 지수 백오프로 재시도, HTTP 오류 → NcAiError 변환.
응답 본문 해석(오디오·3D 모델 등)은 각 서버의 클라이언트가 맡는다.
API 키는 헤더에만 싣고, 오류 메시지·로그에는 redact() 를 거친 문자열만 남긴다.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import ApiSettings
from .errors import NcAiError, redact

log = logging.getLogger("nc_ai_common.http")

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
STATUS_PAGE = "https://platform.nc-ai.com/ko/api-status"


@dataclass
class HttpResult:
    body: Any
    elapsed_s: float
    attempts: int
    request_id: str | None


class NcAiClient:
    def __init__(self, settings: ApiSettings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = settings
        self._transport = transport  # 테스트에서 MockTransport 주입
        self._sleep = asyncio.sleep   # 테스트에서 대기 없이 돌리려고 교체 가능

    @property
    def api_key(self) -> str | None:
        return self._settings.api_key

    async def post_json(self, path: str, payload: dict[str, Any]) -> HttpResult:
        key = self._settings.require_key()
        url = self._settings.base_url + path
        headers = {"OPENAPI_KEY": key, "Content-Type": "application/json"}
        started = time.monotonic()
        attempt = 0

        async with httpx.AsyncClient(timeout=self._settings.timeout_s, transport=self._transport) as http:
            while True:
                attempt += 1
                try:
                    response = await http.post(url, json=payload, headers=headers)
                except httpx.TransportError as exc:
                    # 네트워크/타임아웃: 일시적 오류로 보고 재시도
                    error = NcAiError(
                        "network_error",
                        redact(f"네트워크 오류: {type(exc).__name__}: {exc}", key),
                        fix=f"잠시 후 다시 호출하세요. 계속되면 VARCO_TIMEOUT 을 늘리거나 API 상태 페이지({STATUS_PAGE})를 확인하세요.",
                        retryable=True,
                    )
                else:
                    if response.status_code == 200:
                        try:
                            body = response.json()
                        except ValueError as exc:
                            raise NcAiError("bad_response", "API 응답이 JSON 이 아닙니다.", status=200,
                                            fix="일시적 문제일 수 있으니 한 번 더 호출해 보세요.") from exc
                        return HttpResult(body, round(time.monotonic() - started, 2), attempt,
                                          _request_id(response, body))
                    error = _http_error(response, key)

                if not error.retryable or attempt > self._settings.max_retries:
                    error.extra.pop("_retry_after", None)
                    error.extra["attempts"] = attempt
                    raise error
                delay = _backoff_delay(attempt, error)
                log.warning("NC AI %s 재시도 %d/%d (%s), %.1fs 대기", path, attempt,
                            self._settings.max_retries, error.code, delay)
                await self._sleep(delay)


def _backoff_delay(attempt: int, error: NcAiError) -> float:
    retry_after = error.extra.pop("_retry_after", None)
    if retry_after is not None:
        return float(retry_after)
    return min(30.0, 2 ** (attempt - 1)) + random.uniform(0, 0.5)


def _request_id(response: httpx.Response, body: Any) -> str | None:
    if isinstance(body, dict) and body.get("request_id"):
        return str(body["request_id"])
    for name in ("x-request-id", "request-id", "x-amzn-requestid"):
        if name in response.headers:
            return response.headers[name]
    return None


def _http_error(response: httpx.Response, key: str) -> NcAiError:
    status = response.status_code
    try:
        body: Any = response.json()
    except ValueError:
        body = response.text[:500]
    request_id = _request_id(response, body)
    detail = redact(_summarize_body(body), key)

    if status == 401:
        return NcAiError("unauthorized", f"인증 실패(401): {detail}", status=status, request_id=request_id,
                         fix="VARCO_API_KEY 값이 맞는지, 삭제된 키가 아닌지 확인 후 서버를 재시작하세요.")
    if status == 402:
        return NcAiError("insufficient_credits", f"크레딧 부족(402): {detail}", status=status,
                         request_id=request_id,
                         fix="사용자에게 크레딧 구매 또는 워크스페이스 결제 담당자 확인을 요청하세요. 재시도해도 소용없습니다.")
    if status in (400, 422):
        return NcAiError("api_rejected_input", f"API 가 입력을 거부했습니다({status}): {detail}",
                         status=status, request_id=request_id,
                         fix="detail 에 나온 필드를 문서 범위에 맞게 고쳐 다시 호출하세요. 같은 입력으로 재시도하지 마세요.")
    retryable = status in RETRYABLE_STATUS
    error = NcAiError("server_error" if status >= 500 else "http_error",
                      f"API 오류({status}): {detail}", status=status, request_id=request_id,
                      retryable=retryable,
                      fix=f"재시도 횟수를 다 썼습니다. 잠시 후 다시 호출하거나 {STATUS_PAGE} 를 확인하세요."
                      if retryable else "요청 경로·내용을 확인하세요.")
    if status == 429 and "retry-after" in response.headers:
        try:
            error.extra["_retry_after"] = min(60.0, float(response.headers["retry-after"]))
        except ValueError:
            pass
    return error


def _summarize_body(body: Any) -> str:
    """FastAPI 422 형식({"detail": [{loc, msg}]})이면 필드별로 짧게 요약한다."""
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, list):
            parts = []
            for item in detail[:5]:
                if isinstance(item, dict):
                    loc = ".".join(str(x) for x in item.get("loc", []) if x != "body")
                    parts.append(f"{loc}: {item.get('msg', '')}")
            return "; ".join(parts) or str(body)[:300]
        for k in ("message", "error", "detail"):
            if body.get(k):
                return str(body[k])[:300]
    return str(body)[:300]
