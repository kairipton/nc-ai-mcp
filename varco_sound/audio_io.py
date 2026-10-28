"""오디오 파일 읽기·쓰기·API 입력용 인코딩.

모든 내부 처리는 float32 배열 (frames, channels) 형태로 한다.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

import numpy as np
import soundfile as sf

from .settings import SUPPORTED_FORMATS
from nc_ai_common.errors import invalid

# soundfile 포맷 이름 / 기본 subtype
_SF_FORMAT = {"wav": ("WAV", "PCM_16"), "flac": ("FLAC", "PCM_16"),
              "ogg": ("OGG", "VORBIS"), "mp3": ("MP3", "MPEG_LAYER_III")}


def resolve_path(path: str) -> Path:
    p = Path(path).expanduser()
    return p.resolve()


def require_file(path: str) -> Path:
    p = resolve_path(path)
    if not p.is_file():
        raise invalid(f"파일이 없습니다: {p}", fix="analyze/list_history 결과의 절대 경로를 그대로 넘기세요.")
    return p


def load(path: Path) -> tuple[np.ndarray, int]:
    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return data, sr


def info(path: Path) -> dict:
    i = sf.info(str(path))
    return {
        "path": str(path),
        "duration_s": round(i.frames / i.samplerate, 3),
        "format": path.suffix.lstrip(".").lower(),
        "sample_rate": i.samplerate,
        "channels": i.channels,
        "subtype": i.subtype,
    }


def write(path: Path, data: np.ndarray, sr: int, fmt: str, *, bit_depth: int = 16,
          quality: float | None = None) -> Path:
    """fmt 에 맞춰 저장한다. quality(0~1, 높을수록 고음질)는 ogg/mp3 에만 적용."""
    if fmt not in SUPPORTED_FORMATS:
        raise invalid(f"format={fmt!r} 는 지원하지 않습니다.", allowed=", ".join(SUPPORTED_FORMATS))
    container, subtype = _SF_FORMAT[fmt]
    if fmt in ("wav", "flac"):
        subtype = {16: "PCM_16", 24: "PCM_24"}.get(bit_depth) or ("FLOAT" if fmt == "wav" and bit_depth == 32 else None)
        if subtype is None:
            raise invalid(f"bit_depth={bit_depth} 는 지원하지 않습니다.", allowed="16, 24 (wav 는 32 float 도 가능)")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.clip(data, -1.0, 1.0)
    kwargs = {}
    if quality is not None and fmt in ("ogg", "mp3"):
        # soundfile 의 compression_level 은 0(고음질)~1(저음질) 이므로 뒤집는다.
        kwargs["compression_level"] = float(1.0 - min(1.0, max(0.0, quality)))
    sf.write(str(path), data, sr, format=container, subtype=subtype, **kwargs)
    return path


def sniff_extension(blob: bytes) -> str:
    """API 가 돌려준 바이트의 실제 포맷을 앞부분 서명으로 판별한다."""
    if blob[:4] == b"RIFF":
        return "wav"
    if blob[:4] == b"OggS":
        return "ogg"
    if blob[:4] == b"fLaC":
        return "flac"
    if blob[:3] == b"ID3" or (len(blob) > 1 and blob[0] == 0xFF and blob[1] & 0xE0 == 0xE0):
        return "mp3"
    return "bin"


def encode_for_api(path: Path, *, min_s: float | None, max_s: float | None, label: str = "source",
                   mono: bool = False) -> tuple[str, float]:
    """API 입력용 base64 문자열을 만든다.

    어떤 포맷이 들어와도 16bit WAV 로 바꿔 보낸다(API 는 WAV 출력만 문서화되어 있음).
    문서상 길이 범위를 벗어나면 크레딧을 쓰기 전에 실패시킨다.
    """
    data, sr = load(path)
    duration = data.shape[0] / sr
    if min_s is not None and duration < min_s:
        raise invalid(f"{label} 길이 {duration:.2f}s 가 최소 {min_s}s 보다 짧습니다.",
                      allowed=f"{min_s}~{max_s}s",
                      fix="더 긴 소리를 쓰거나, 꼬리에 무음을 덧붙이지 말고 원본 소리를 길게 생성하세요.")
    if max_s is not None and duration > max_s:
        raise invalid(f"{label} 길이 {duration:.2f}s 가 최대 {max_s}s 를 넘습니다.",
                      allowed=f"{min_s}~{max_s}s",
                      fix=f"trim_silence 로 무음을 자르거나, 필요한 구간만 {max_s}s 이하로 잘라서 다시 호출하세요.")
    if mono and data.shape[1] > 1:
        data = data.mean(axis=1, keepdims=True)
    buf = io.BytesIO()
    sf.write(buf, np.clip(data, -1.0, 1.0), sr, format="WAV", subtype="PCM_16")
    return base64.b64encode(buf.getvalue()).decode("ascii"), round(duration, 3)
