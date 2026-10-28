"""설정 로드(모든 서버 공통).

우선순위: 환경 변수 > 저장소 루트의 .env > 저장소 루트의 config.toml > 기본값.
API 키는 환경 변수 또는 .env 에서만 읽는다(config.toml 에 키를 두지 않는다).
키는 NC AI 플랫폼 공통 OPENAPI_KEY 이므로 모든 서버가 같은 VARCO_API_KEY 를 쓴다.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from .errors import NcAiError

# 저장소 루트(= nc_ai_common 의 상위 폴더). .env / config.toml 은 여기서 찾는다.
REPO_ROOT = Path(__file__).resolve().parent.parent


class ConfigSource:
    """env → .env → config.toml 순으로 값을 찾는다. toml 은 [section] 별로 나뉜다."""

    def __init__(self, root: Path = REPO_ROOT, environ: dict[str, str] | None = None) -> None:
        self.root = root
        self.env = dict(os.environ if environ is None else environ)
        self.dotenv = {k: v for k, v in dotenv_values(root / ".env").items() if v is not None}
        path = root / "config.toml"
        self.toml: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def get(self, env_name: str, section: str, key: str, default: Any) -> Any:
        for source in (self.env, self.dotenv):
            if source.get(env_name):
                return source[env_name]
        return self.toml.get(section, {}).get(key, default)

    def secret(self, env_name: str) -> str | None:
        """비밀 값은 env / .env 에서만."""
        return self.env.get(env_name) or self.dotenv.get(env_name) or None


@dataclass(frozen=True)
class ApiSettings:
    api_key: str | None
    base_url: str
    timeout_s: float
    max_retries: int

    def require_key(self) -> str:
        """키가 없으면 고치는 방법을 담아 실패시킨다(키 값 자체는 절대 출력하지 않음)."""
        if not self.api_key:
            raise NcAiError(
                "missing_api_key",
                "VARCO_API_KEY 가 설정되지 않았습니다.",
                fix=f"환경 변수 VARCO_API_KEY 를 설정하거나 {REPO_ROOT / '.env'} 에 "
                "VARCO_API_KEY=<키> 한 줄을 넣은 뒤 MCP 서버를 다시 시작하세요.",
            )
        return self.api_key


def load_api_settings(src: ConfigSource) -> ApiSettings:
    return ApiSettings(
        api_key=src.secret("VARCO_API_KEY"),
        base_url=str(src.get("VARCO_BASE_URL", "api", "base_url", "https://openapi.ai.nc.com")).rstrip("/"),
        timeout_s=float(src.get("VARCO_TIMEOUT", "api", "timeout_s", 180)),
        max_retries=int(src.get("VARCO_MAX_RETRIES", "api", "max_retries", 3)),
    )
