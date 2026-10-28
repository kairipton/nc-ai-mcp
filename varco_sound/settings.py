"""VARCO Sound 서버 설정. 공통 API 설정 + 사운드 전용 항목(config.toml 의 [varco_sound] 섹션)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from nc_ai_common.config import REPO_ROOT, ApiSettings, ConfigSource, load_api_settings

SUPPORTED_FORMATS = ("wav", "ogg", "mp3", "flac")


@dataclass(frozen=True)
class SoundSettings:
    api: ApiSettings
    output_dir: Path
    default_format: str
    max_candidates: int


def load_settings(src: ConfigSource | None = None) -> SoundSettings:
    src = src or ConfigSource()
    fmt = str(src.get("VARCO_DEFAULT_FORMAT", "varco_sound", "default_format", "wav")).lower()
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(f"default_format={fmt!r} 는 지원하지 않습니다. 허용: {SUPPORTED_FORMATS}")
    out = src.get("VARCO_OUTPUT_DIR", "varco_sound", "output_dir", str(REPO_ROOT / "output" / "varco_sound"))
    return SoundSettings(
        api=load_api_settings(src),
        output_dir=Path(out).expanduser().resolve(),
        default_format=fmt,
        max_candidates=int(src.get("VARCO_MAX_CANDIDATES", "varco_sound", "max_candidates", 12)),
    )
