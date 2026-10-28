"""로컬 후처리(무음 제거·노멀라이즈·페이드·포맷 변환). API 크레딧을 쓰지 않는다.

모든 함수는 원본을 건드리지 않고 새 파일을 쓴 뒤 그 경로와 변경 내용을 돌려준다.
"""

from __future__ import annotations

import math
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np
import pyloudnorm
from scipy import signal

from . import audio_io
from .analysis import DEFAULT_SILENCE_DB, FRAME_MS, _envelope_db
from nc_ai_common.errors import invalid


def derived_path(src: Path, op: str, fmt: str | None = None, output_path: str | None = None,
                 overwrite: bool = False) -> Path:
    """후처리 결과 경로. output_path 가 있으면 그대로, 없으면 원본 옆에 '_op' 접미사.

    자동 경로는 이미 있으면 _2, _3 … 을 붙인다. output_path 가 이미 있으면 overwrite=True 일 때만 덮어쓴다.
    원본 자체는 어떤 경우에도 덮어쓰지 않는다.
    """
    if output_path:
        target = audio_io.resolve_path(output_path)
        if target == src.resolve():
            raise invalid("output_path 가 원본과 같습니다.", fix="원본은 덮어쓰지 않습니다. 다른 경로를 주세요.")
        if target.exists() and not overwrite:
            raise invalid(f"output_path 가 이미 있습니다: {target}",
                          fix="다른 경로를 주거나, 교체가 의도라면 overwrite=true 로 다시 호출하세요.")
        return target
    ext = fmt or src.suffix.lstrip(".")
    base = src.with_name(f"{src.stem}_{op}.{ext}")
    n = 2
    while base.exists():
        base = src.with_name(f"{src.stem}_{op}{n}.{ext}")
        n += 1
    return base


def _out_format(path: Path) -> str:
    fmt = path.suffix.lstrip(".").lower()
    if fmt not in ("wav", "ogg", "mp3", "flac"):
        raise invalid(f"출력 확장자 .{fmt} 는 지원하지 않습니다.", allowed="wav, ogg, mp3, flac")
    return fmt


def trim_silence(src: Path, threshold_db: float = DEFAULT_SILENCE_DB, pad_ms: float = 10.0,
                 trim_start: bool = True, trim_end: bool = True, output_path: str | None = None, overwrite: bool = False) -> dict[str, Any]:
    data, sr = audio_io.load(src)
    env = _envelope_db(data.mean(axis=1), sr)
    loud = np.where(env > threshold_db)[0]
    if not len(loud):
        raise invalid(f"전체가 {threshold_db}dBFS 이하라 남길 소리가 없습니다.",
                      fix="threshold_db 를 더 낮게(예: -60) 주거나 파일이 무음인지 analyze_audio 로 확인하세요.")
    hop = max(1, int(sr * FRAME_MS / 1000))
    pad = int(sr * pad_ms / 1000)
    start = max(0, loud[0] * hop - pad) if trim_start else 0
    end = min(len(data), (loud[-1] + 1) * hop + pad) if trim_end else len(data)
    out = derived_path(src, "trim", output_path=output_path, overwrite=overwrite)
    audio_io.write(out, data[start:end], sr, _out_format(out))
    return {"output": str(out), "removed_start_ms": round(start / sr * 1000, 1),
            "removed_end_ms": round((len(data) - end) / sr * 1000, 1),
            "duration_before_s": round(len(data) / sr, 3), "duration_after_s": round((end - start) / sr, 3)}


def normalize(src: Path, mode: str = "lufs", target: float | None = None, peak_ceiling_db: float = -1.0,
              output_path: str | None = None, overwrite: bool = False) -> dict[str, Any]:
    data, sr = audio_io.load(src)
    peak = float(np.max(np.abs(data)))
    if peak <= 1e-6:
        raise invalid("무음 파일은 노멀라이즈할 수 없습니다.")
    peak_db = 20 * math.log10(peak)

    if mode == "peak":
        goal = -1.0 if target is None else target
        gain_db = goal - peak_db
        measured = {"peak_dbfs_before": round(peak_db, 2)}
    elif mode == "lufs":
        goal = -18.0 if target is None else target
        if len(data) / sr < 0.4:
            raise invalid("0.4초 미만은 LUFS 를 잴 수 없습니다.", fix="mode='peak' 로 다시 호출하세요.")
        meter = pyloudnorm.Meter(sr)
        lufs = float(meter.integrated_loudness(data))
        if not math.isfinite(lufs):
            raise invalid("LUFS 측정 불가(거의 무음).", fix="mode='peak' 로 다시 호출하세요.")
        gain_db = goal - lufs
        # 게이팅(-70 LUFS 절대 게이트) 때문에 게인을 올리면 조용한 꼬리가 측정에 새로 들어와
        # 결과가 목표보다 낮게 나올 수 있다 → 재측정하며 몇 번 보정한다.
        for _ in range(4):
            got = float(meter.integrated_loudness(data * (10 ** (gain_db / 20))))
            if not math.isfinite(got) or abs(goal - got) < 0.05:
                break
            gain_db += goal - got
        measured = {"lufs_before": round(lufs, 2), "peak_dbfs_before": round(peak_db, 2)}
    else:
        raise invalid(f"mode={mode!r} 는 지원하지 않습니다.", allowed="lufs, peak")

    # 피크 상한을 넘지 않도록 게인을 줄인다(리미터는 쓰지 않음 → 목표 LUFS 미달 가능).
    limited = False
    if peak_db + gain_db > peak_ceiling_db:
        gain_db = peak_ceiling_db - peak_db
        limited = True
    out_data = data * (10 ** (gain_db / 20))
    out = derived_path(src, "norm", output_path=output_path, overwrite=overwrite)
    audio_io.write(out, out_data, sr, _out_format(out))
    result = {"output": str(out), "mode": mode, "target": goal, "gain_db": round(gain_db, 2),
              "peak_dbfs_after": round(peak_db + gain_db, 2), "limited_by_peak_ceiling": limited, **measured}
    if mode == "lufs" and len(data) / sr >= 0.4:
        result["lufs_after"] = round(float(pyloudnorm.Meter(sr).integrated_loudness(out_data)), 2)
    if limited:
        result["note"] = (f"피크 상한 {peak_ceiling_db}dBFS 때문에 목표보다 작게 맞췄습니다. "
                          "더 크게 하려면 peak_ceiling_db 를 올리세요(0 초과는 클리핑).")
    return result


def fade(src: Path, fade_in_ms: float = 0.0, fade_out_ms: float = 0.0, curve: str = "linear",
         output_path: str | None = None, overwrite: bool = False) -> dict[str, Any]:
    if fade_in_ms <= 0 and fade_out_ms <= 0:
        raise invalid("fade_in_ms 와 fade_out_ms 가 모두 0 입니다.", fix="둘 중 하나 이상을 0 보다 크게 주세요.")
    if curve not in ("linear", "cosine"):
        raise invalid(f"curve={curve!r} 는 지원하지 않습니다.", allowed="linear, cosine")
    data, sr = audio_io.load(src)
    n = len(data)
    n_in, n_out = int(sr * fade_in_ms / 1000), int(sr * fade_out_ms / 1000)
    if n_in + n_out > n:
        raise invalid(f"페이드 합계 {fade_in_ms + fade_out_ms}ms 가 파일 길이 {n / sr * 1000:.0f}ms 보다 깁니다.",
                      fix="페이드 길이를 줄이세요.")

    def ramp(k: int) -> np.ndarray:
        t = np.linspace(0, 1, k, endpoint=False, dtype=np.float32)
        return t if curve == "linear" else (0.5 - 0.5 * np.cos(np.pi * t)).astype(np.float32)

    out_data = data.copy()
    if n_in:
        out_data[:n_in] *= ramp(n_in)[:, None]
    if n_out:
        out_data[n - n_out:] *= ramp(n_out)[::-1][:, None]
    out = derived_path(src, "fade", output_path=output_path, overwrite=overwrite)
    audio_io.write(out, out_data, sr, _out_format(out))
    return {"output": str(out), "fade_in_ms": fade_in_ms, "fade_out_ms": fade_out_ms, "curve": curve}


def convert(src: Path, fmt: str, sample_rate: int | None = None, channels: int | None = None,
            bit_depth: int = 16, quality: float = 0.7, output_path: str | None = None, overwrite: bool = False) -> dict[str, Any]:
    data, sr = audio_io.load(src)
    if channels is not None:
        if channels == 1 and data.shape[1] > 1:
            data = data.mean(axis=1, keepdims=True)
        elif channels == 2 and data.shape[1] == 1:
            data = np.repeat(data, 2, axis=1)
        elif channels not in (1, 2):
            raise invalid(f"channels={channels} 는 지원하지 않습니다.", allowed="1, 2")
    new_sr = sr
    if sample_rate and sample_rate != sr:
        # 정수비 다상 리샘플링(고품질, 앨리어싱 필터 포함)
        ratio = Fraction(sample_rate, sr).limit_denominator(1000)
        data = signal.resample_poly(data, ratio.numerator, ratio.denominator, axis=0).astype(np.float32)
        new_sr = sample_rate
    out = derived_path(src, "conv", fmt=fmt, output_path=output_path, overwrite=overwrite)
    if output_path and _out_format(out) != fmt:
        raise invalid(f"output_path 확장자(.{out.suffix.lstrip('.')})와 format={fmt} 가 다릅니다.",
                      fix="둘을 맞춰 다시 호출하세요.")
    audio_io.write(out, data, new_sr, fmt, bit_depth=bit_depth, quality=quality)
    return {"output": str(out), "format": fmt, "sample_rate": new_sr, "channels": int(data.shape[1]),
            "bit_depth": bit_depth if fmt in ("wav", "flac") else None,
            "quality": quality if fmt in ("ogg", "mp3") else None}
