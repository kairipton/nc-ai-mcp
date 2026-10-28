"""Claude 가 바로 고칠 수 있는 형태의 오류.

모든 오류는 {code, message, allowed, fix, ...} 구조로 직렬화된다.
API 키는 어떤 경로로도 메시지에 섞이지 않도록 redact() 를 거친다.
"""

from __future__ import annotations

import json
from typing import Any


class NcAiError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        allowed: str | None = None,
        fix: str | None = None,
        status: int | None = None,
        retryable: bool = False,
        request_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.allowed = allowed
        self.fix = fix
        self.status = status
        self.retryable = retryable
        self.request_id = request_id
        self.extra = extra or {}

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"error": self.code, "message": self.message}
        for key in ("allowed", "fix", "status", "request_id"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        data["retryable"] = self.retryable
        data.update(self.extra)
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


def invalid(message: str, *, allowed: str | None = None, fix: str | None = None) -> NcAiError:
    """입력 검증 실패(크레딧을 쓰기 전에 로컬에서 거른다)."""
    return NcAiError("invalid_argument", message, allowed=allowed, fix=fix)


def redact(text: str, secret: str | None) -> str:
    """문자열에서 비밀 값을 지운다. 키가 짧거나 없으면 그대로 둔다."""
    if secret and len(secret) >= 4 and secret in text:
        return text.replace(secret, "[REDACTED]")
    return text
