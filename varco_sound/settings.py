"""VARCO Sound 서버 설정. 공통 API 설정 + 사운드 전용 항목(config.toml 의 [varco_sound] 섹션)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from nc_ai_common.config import REPO_ROOT, ApiSettings, ConfigSource, load_api_settings

SUPPORTED_FORMATS = ("wav", "ogg", "mp3", "flac")

# 크레딧 모드별 세션 예산(크레딧). 세션 = MCP 서버 프로세스 하나 = Claude Code 세션 하나.
# 모드는 '작업 방식'(스킬 규칙)과 '세션 예산 상한'(서버 강제)을 함께 정한다.
CREDIT_MODES = {"saver": 20.0, "normal": 60.0, "flex": 200.0}


@dataclass(frozen=True)
class SoundSettings:
    api: ApiSettings
    output_dir: Path
    default_format: str
    max_candidates: int
    credit_mode: str = "normal"
    session_budget: float | None = CREDIT_MODES["normal"]  # None 이면 상한 없음


def load_settings(src: ConfigSource | None = None) -> SoundSettings:
    src = src or ConfigSource()
    fmt = str(src.get("VARCO_DEFAULT_FORMAT", "varco_sound", "default_format", "wav")).lower()
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(f"default_format={fmt!r} 는 지원하지 않습니다. 허용: {SUPPORTED_FORMATS}")
    out = src.get("VARCO_OUTPUT_DIR", "varco_sound", "output_dir", str(REPO_ROOT / "output" / "varco_sound"))

    mode = str(src.get("VARCO_CREDIT_MODE", "varco_sound", "credit_mode", "normal")).lower()
    if mode not in CREDIT_MODES:
        raise ValueError(f"credit_mode={mode!r} 는 지원하지 않습니다. 허용: {tuple(CREDIT_MODES)}")
    # session_budget 을 직접 주면 모드 기본 예산보다 우선한다. 0 이하는 상한 없음.
    raw_budget = src.get("VARCO_SESSION_BUDGET", "varco_sound", "session_budget", None)
    budget: float | None = CREDIT_MODES[mode] if raw_budget in (None, "") else float(raw_budget)
    if budget is not None and budget <= 0:
        budget = None

    return SoundSettings(
        api=load_api_settings(src),
        output_dir=Path(out).expanduser().resolve(),
        default_format=fmt,
        max_candidates=int(src.get("VARCO_MAX_CANDIDATES", "varco_sound", "max_candidates", 12)),
        credit_mode=mode,
        session_budget=budget,
    )
