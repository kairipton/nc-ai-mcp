"""오디오 분석: Claude 는 소리를 못 들으므로 소리의 성격을 숫자 + 짧은 해석으로 돌려준다.

해석 문구의 경계값은 일반적인 효과음 기준의 휴리스틱이다(측정값 자체가 근거, 해석은 보조).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
import pyloudnorm
from scipy import signal

from . import audio_io
from nc_ai_common.errors import invalid

FRAME_MS = 5.0             # 엔벨로프 프레임 길이
DEFAULT_SILENCE_DB = -50.0  # 이 dBFS 보다 작은 프레임을 무음으로 본다
DECAY_DROP_DB = 20.0       # 감쇠 시간: 최대값에서 이만큼 떨어질 때까지
BANDS = {"low": (20, 250), "mid": (250, 4000), "high": (4000, None)}


def _db(x: float) -> float:
    return -120.0 if x <= 1e-6 else round(20 * math.log10(x), 2)


def _envelope_db(mono: np.ndarray, sr: int) -> np.ndarray:
    """FRAME_MS 단위 RMS 엔벨로프(dBFS)."""
    hop = max(1, int(sr * FRAME_MS / 1000))
    n = len(mono) // hop
    if n == 0:
        return np.array([_db(float(np.sqrt(np.mean(mono ** 2))) if len(mono) else 0.0)])
    frames = mono[: n * hop].reshape(n, hop)
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    return 20 * np.log10(np.maximum(rms, 1e-6))


def _clip_runs(data: np.ndarray, level: float = 0.9999, min_run: int = 3) -> int:
    """풀스케일 근처 샘플이 min_run 개 이상 연속된 구간 수(디지털 클리핑 흔적)."""
    count = 0
    for ch in range(data.shape[1]):
        hot = np.abs(data[:, ch]) >= level
        if not hot.any():
            continue
        # 연속 구간 길이 계산
        edges = np.diff(np.concatenate(([0], hot.astype(np.int8), [0])))
        starts, ends = np.where(edges == 1)[0], np.where(edges == -1)[0]
        count += int(np.sum((ends - starts) >= min_run))
    return count


def analyze(path: Path, silence_db: float = DEFAULT_SILENCE_DB) -> dict[str, Any]:
    data, sr = audio_io.load(path)
    if data.shape[0] == 0:
        raise invalid(f"빈 오디오 파일입니다: {path}")
    mono = data.mean(axis=1)
    duration = data.shape[0] / sr

    # --- 레벨 ---
    peak = float(np.max(np.abs(data)))
    rms = float(np.sqrt(np.mean(data ** 2)))
    lufs: float | None
    if duration >= 0.4:  # 통합 라우드니스는 400ms 블록이 최소 1개 필요
        value = pyloudnorm.Meter(sr).integrated_loudness(data)
        lufs = None if not math.isfinite(value) else round(float(value), 2)
    else:
        lufs = None

    # --- 시간 구조 ---
    env = _envelope_db(mono, sr)
    frame_s = FRAME_MS / 1000
    loud = np.where(env > silence_db)[0]
    if len(loud):
        lead_s = loud[0] * frame_s
        trail_s = max(0.0, duration - (loud[-1] + 1) * frame_s)
        peak_idx = int(np.argmax(env))
        attack_ms = (peak_idx - loud[0]) * FRAME_MS
        above = np.where(env[peak_idx:] >= env[peak_idx] - DECAY_DROP_DB)[0]
        decay_ms = float(above[-1] * FRAME_MS) if len(above) else 0.0
        silence_ratio = 1 - len(loud) / len(env)
    else:
        lead_s, trail_s, attack_ms, decay_ms, silence_ratio = duration, 0.0, 0.0, 0.0, 1.0

    # --- 주파수 (무음이 아닌 구간만) ---
    active = mono
    if len(loud):
        hop = max(1, int(sr * frame_s))
        active = mono[loud[0] * hop: (loud[-1] + 1) * hop]
    centroid, bands = _spectrum(active, sr)

    # --- 스테레오 ---
    if data.shape[1] >= 2:
        left, right = data[:, 0], data[:, 1]
        mid, side = (left + right) / 2, (left - right) / 2
        mid_rms = float(np.sqrt(np.mean(mid ** 2)))
        width = round(float(np.sqrt(np.mean(side ** 2))) / mid_rms, 3) if mid_rms > 1e-6 else None
        corr = float(np.corrcoef(left, right)[0, 1]) if np.std(left) > 0 and np.std(right) > 0 else None
    else:
        width, corr = 0.0, None

    clip_runs = _clip_runs(data)
    metrics = {
        "duration_s": round(duration, 3),
        "effective_duration_s": round(max(0.0, duration - lead_s - trail_s), 3),
        "sample_rate": sr,
        "channels": int(data.shape[1]),
        "peak_dbfs": _db(peak),
        "rms_dbfs": _db(rms),
        "integrated_lufs": lufs,
        "leading_silence_ms": round(lead_s * 1000, 1),
        "trailing_silence_ms": round(trail_s * 1000, 1),
        "silence_ratio": round(float(silence_ratio), 3),
        "attack_ms": round(float(attack_ms), 1),
        "decay_ms": round(float(decay_ms), 1),
        "spectral_centroid_hz": round(centroid, 1),
        "low_ratio": bands["low"],
        "mid_ratio": bands["mid"],
        "high_ratio": bands["high"],
        "clipping_runs": clip_runs,
        "stereo_width": width,
        "stereo_correlation": None if corr is None else round(corr, 3),
    }
    return {"path": str(path), "metrics": metrics, "interpretation": interpret(metrics),
            "settings": {"silence_threshold_dbfs": silence_db, "decay_drop_db": DECAY_DROP_DB,
                         "bands_hz": {"low": "20-250", "mid": "250-4000", "high": "4000+"}}}


def _spectrum(x: np.ndarray, sr: int) -> tuple[float, dict[str, float]]:
    if len(x) < 64:
        return 0.0, {"low": 0.0, "mid": 0.0, "high": 0.0}
    freqs, psd = signal.welch(x, fs=sr, nperseg=min(4096, len(x)))
    total = float(np.sum(psd))
    if total <= 0:
        return 0.0, {"low": 0.0, "mid": 0.0, "high": 0.0}
    centroid = float(np.sum(freqs * psd) / total)
    bands = {}
    for name, (lo, hi) in BANDS.items():
        mask = (freqs >= lo) & (freqs < (hi or sr))
        bands[name] = round(float(np.sum(psd[mask]) / total), 3)
    return centroid, bands


def interpret(m: dict[str, Any]) -> dict[str, str]:
    """각 수치를 사람 말로 짧게 해석한다(휴리스틱)."""
    out: dict[str, str] = {}
    a = m["attack_ms"]
    out["attack"] = f"어택 {a:.0f}ms: " + (
        "매우 날카로움(타격·클릭)" if a < 10 else "날카로움" if a < 30 else "보통" if a < 100
        else "부드럽게 시작" if a < 300 else "천천히 커짐(스웰)")
    d = m["decay_ms"]
    out["decay"] = f"감쇠 {d:.0f}ms(-20dB까지): " + (
        "짧게 끊김" if d < 150 else "보통 길이 꼬리" if d < 600 else "긴 꼬리·잔향")
    c = m["spectral_centroid_hz"]
    out["brightness"] = f"무게중심 {c:.0f}Hz: " + (
        "어둡고 묵직함" if c < 800 else "따뜻함·중간 밝기" if c < 2000 else "밝음" if c < 4000 else "매우 밝고 쨍함")
    lo, mid, hi = m["low_ratio"], m["mid_ratio"], m["high_ratio"]
    out["balance"] = f"저/중/고 {lo:.2f}/{mid:.2f}/{hi:.2f}: " + (
        "저역 우세(무게감)" if lo >= 0.5 else "고역 우세(밝고 날카로움)" if hi >= 0.3 else "중역 중심")
    p = m["peak_dbfs"]
    out["peak"] = f"피크 {p:.1f}dBFS: " + (
        "풀스케일, 여유 없음" if p > -0.3 else "적당함" if p > -6 else "작음" if p > -18 else "매우 작음")
    lufs = m["integrated_lufs"]
    out["loudness"] = "LUFS 측정 불가(0.4초 미만 또는 무음)" if lufs is None else f"통합 {lufs:.1f} LUFS"
    eff, trail = m["effective_duration_s"], m["trailing_silence_ms"]
    note = f"실제 소리 {eff:.2f}s / 전체 {m['duration_s']:.2f}s"
    if trail > 1000 or m["leading_silence_ms"] > 50:
        note += " — 앞뒤 무음이 김: trim_silence 권장"
    out["duration"] = note
    out["clipping"] = "클리핑 없음" if m["clipping_runs"] == 0 else f"클리핑 흔적 {m['clipping_runs']}곳: 게인 낮추기 필요"
    w = m["stereo_width"]
    if m["channels"] == 1:
        out["stereo"] = "모노"
    elif w is None:
        out["stereo"] = "스테레오 폭 측정 불가"
    else:
        out["stereo"] = f"폭 {w:.2f}: " + ("거의 모노" if w < 0.1 else "보통 폭" if w < 0.5 else "넓음")
    return out


# ---------------- 비교 ----------------

# 지표별 거리 계산 방식: log = 비율 차이(로그), lin = 선형 차이 / scale
RANKABLE: dict[str, tuple[str, float]] = {
    "effective_duration_s": ("log", 1.0),
    "duration_s": ("log", 1.0),
    "attack_ms": ("log", 1.0),
    "decay_ms": ("log", 1.0),
    "spectral_centroid_hz": ("log", 1.0),
    "low_ratio": ("lin", 0.25),
    "mid_ratio": ("lin", 0.25),
    "high_ratio": ("lin", 0.25),
    "peak_dbfs": ("lin", 6.0),
    "rms_dbfs": ("lin", 6.0),
    "integrated_lufs": ("lin", 6.0),
    "stereo_width": ("lin", 0.3),
    "silence_ratio": ("lin", 0.25),
}


def _distance(metric: str, actual: float | None, target: float) -> float | None:
    if actual is None:
        return None
    kind, scale = RANKABLE[metric]
    if kind == "log":
        return abs(math.log((max(actual, 1e-3) + 1) / (max(target, 1e-3) + 1))) / math.log(2)
    return abs(actual - target) / scale


def rank(analyses: list[dict[str, Any]], target: dict[str, float],
         weights: dict[str, float] | None = None) -> list[dict[str, Any]]:
    """target 지표값에 가까운 순서로 정렬. score 가 작을수록 목표에 가깝다."""
    unknown = sorted(set(target) - set(RANKABLE))
    if unknown:
        raise invalid(f"target 에 비교할 수 없는 지표가 있습니다: {unknown}", allowed=", ".join(RANKABLE),
                      fix="허용 지표 이름으로 바꿔 다시 호출하세요.")
    weights = weights or {}
    rows = []
    for a in analyses:
        m = a["metrics"]
        parts, total, wsum = {}, 0.0, 0.0
        for metric, goal in target.items():
            dist = _distance(metric, m.get(metric), float(goal))
            if dist is None:
                parts[metric] = "측정 불가(제외)"
                continue
            w = float(weights.get(metric, 1.0))
            total, wsum = total + w * dist, wsum + w
            parts[metric] = f"{m.get(metric)} (목표 {goal}, 편차 {dist:.2f})"
        rows.append({"path": a["path"], "score": round(total / wsum, 3) if wsum else None, "reasons": parts})
    rows.sort(key=lambda r: (r["score"] is None, r["score"] if r["score"] is not None else 0))
    for i, row in enumerate(rows, 1):
        row["rank"] = i
    return rows
