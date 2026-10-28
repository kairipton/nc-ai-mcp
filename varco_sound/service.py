"""도구 로직(오케스트레이션). server.py 는 이 클래스의 메서드를 MCP 도구로 노출만 한다.

흐름: 입력 검증(크레딧 쓰기 전) → API 호출(필요하면 여러 번 나눠서) → 파일 저장 → manifest 기록 → 구조화된 결과 반환.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from nc_ai_common import manifest

from . import analysis, audio_io, audition, processing
from .api_spec import (CONV_RATIO_RANGE, ENDPOINTS, T2S_MAX_PER_CALL, T2S_PROMPT_MAX_CHARS, T2S_VERSIONS,
                       VAR_MAX_PER_CALL, VAR_STRENGTH_RANGE, estimate_credits)
from .client import ApiResult, VarcoSoundClient
from .settings import SoundSettings
from nc_ai_common.errors import NcAiError, invalid

SERVICE = "VARCO Sound"
SEED_NOTE = "VARCO Sound API 는 시드를 지원하지 않습니다. 같은 파라미터로도 매번 다른 결과가 나옵니다."
REMAINING_NOTE = "공개 API 에 크레딧 잔액 조회가 없습니다. 대시보드(platform.nc-ai.com › 대시보드 › 현황)에서 확인하세요."
QUICK_METRICS = ("effective_duration_s", "peak_dbfs", "integrated_lufs", "attack_ms", "decay_ms",
                 "spectral_centroid_hz", "low_ratio", "high_ratio")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _check_range(name: str, value: float, lo: float, hi: float) -> None:
    if not lo <= value <= hi:
        raise invalid(f"{name}={value} 가 허용 범위를 벗어났습니다.", allowed=f"{lo}~{hi}",
                      fix=f"{name} 를 {lo}~{hi} 사이로 다시 호출하세요.")


class VarcoService:
    def __init__(self, settings: SoundSettings, client: VarcoSoundClient | None = None) -> None:
        self.settings = settings
        self.client = client or VarcoSoundClient(settings.api)
        # 이 서버 프로세스(= 한 Claude 세션)의 누적 사용량
        self.calls: Counter[str] = Counter()
        self.samples: Counter[str] = Counter()
        self.credits_spent = 0.0

    # ------------------------------------------------------------ 공통

    def _folder(self, output_dir: str | None) -> Path:
        return audio_io.resolve_path(output_dir) if output_dir else self.settings.output_dir

    async def _call(self, endpoint: str, payload: dict[str, Any], samples: int = 0) -> tuple[ApiResult, float]:
        result = await self.client.call(endpoint, payload)
        credits = estimate_credits(endpoint, 1, samples)
        self.calls[endpoint] += 1
        self.samples[endpoint] += samples
        self.credits_spent = round(self.credits_spent + credits, 2)
        return result, credits

    def _save(self, audios: list[bytes], folder: Path, purpose: str, description: str) -> list[dict[str, Any]]:
        files = []
        for blob in audios:
            ext = audio_io.sniff_extension(blob)
            path = manifest.next_audio_path(folder, purpose, description, ext)
            path.write_bytes(blob)
            entry = {"path": str(path)}
            if ext != "bin":
                entry.update(audio_io.info(path))
                metrics = analysis.analyze(path)["metrics"]
                entry["quick_metrics"] = {k: metrics[k] for k in QUICK_METRICS}
            else:
                entry["warning"] = "알 수 없는 오디오 포맷으로 저장했습니다(서명 판별 실패)."
            files.append(entry)
        return files

    async def _generate(self, *, tool: str, endpoint: str, payloads: list[tuple[dict[str, Any], int]],
                        params: dict[str, Any], folder: Path, purpose: str, description: str,
                        note: str | None, parents: list[str]) -> dict[str, Any]:
        """payloads 를 순서대로 호출. 중간에 실패하면 이미 저장한 파일을 오류에 담아 알린다."""
        files: list[dict[str, Any]] = []
        request_ids: list[str] = []
        credits = 0.0
        elapsed = 0.0
        try:
            for payload, samples in payloads:
                result, c = await self._call(endpoint, payload, samples)
                credits += c
                elapsed += result.elapsed_s
                if result.request_id:
                    request_ids.append(result.request_id)
                files.extend(self._save(result.audios, folder, purpose, description))
        except NcAiError as err:
            if files:
                rec = self._record(folder, tool, params, files, parents, credits, request_ids, note, description)
                err.extra.update({"partial": True, "saved_files": [f["path"] for f in files],
                                  "manifest_id": rec["id"], "credits_estimated_spent": round(credits, 2)})
            raise
        rec = self._record(folder, tool, params, files, parents, credits, request_ids, note, description)
        return {
            "tool": tool,
            "files": files,
            "params": params,
            "seed": None,
            "seed_note": SEED_NOTE,
            "job_id": None,
            "request_ids": request_ids,
            "manifest_id": rec["id"],
            "manifest_path": str(folder / manifest.MANIFEST_NAME),
            "api_calls": len(payloads),
            "elapsed_s": round(elapsed, 2),
            "credits_estimated": round(credits, 2),
            "credits_remaining": None,
            "credits_note": REMAINING_NOTE,
            "session_credits_estimated": self.credits_spent,
            "created_at": rec["created_at"],
        }

    def _record(self, folder: Path, tool: str, params: dict[str, Any], files: list[dict[str, Any]],
                parents: list[str], credits: float, request_ids: list[str], note: str | None,
                description: str) -> dict[str, Any]:
        return manifest.append(folder, {
            "tool": tool, "params": params, "seed": None, "parents": parents,
            "files": [f["path"] for f in files], "credits_estimated": round(credits, 2),
            "request_ids": request_ids, "note": note, "description": description,
        }, SERVICE)

    def _parents(self, *paths: Path) -> list[str]:
        """원본 파일의 manifest id(있으면) 또는 경로."""
        out = []
        for p in paths:
            rec = manifest.find_by_file(p.parent, str(p))
            out.append(rec["id"] if rec else str(p))
        return out

    @staticmethod
    def _region(begin: float | None, end: float | None, duration: float, label: str) -> dict[str, float] | None:
        if begin is None and end is None:
            return None
        b = 0.0 if begin is None else begin
        e = duration if end is None else end
        if not 0 <= b < e <= duration + 1e-3:
            raise invalid(f"{label} 구간 begin={b}, end={e} 가 올바르지 않습니다.",
                          allowed=f"0 <= begin < end <= {duration:.2f}(파일 길이)",
                          fix="파일 길이 안에서 begin < end 로 다시 지정하세요.")
        return {"begin": round(b, 3), "end": round(e, 3)}

    # ------------------------------------------------------------ 생성 (API)

    async def generate_sfx(self, prompt: str, n: int = 3, version: str = "v1", purpose: str = "sfx",
                           description: str = "", output_dir: str | None = None,
                           note: str | None = None) -> dict[str, Any]:
        prompt = prompt.strip()
        if not prompt:
            raise invalid("prompt 가 비었습니다.", fix="만들 소리를 한 문장으로 적으세요.")
        if len(prompt) > T2S_PROMPT_MAX_CHARS:
            raise invalid(f"prompt 가 {len(prompt)}자로 최대 {T2S_PROMPT_MAX_CHARS}자를 넘습니다.",
                          allowed=f"1~{T2S_PROMPT_MAX_CHARS}자",
                          fix="핵심 소리(재질·동작)와 분위기만 남기고 줄여 다시 호출하세요.")
        if version not in T2S_VERSIONS:
            raise invalid(f"version={version!r} 는 지원하지 않습니다.", allowed=", ".join(T2S_VERSIONS))
        _check_range("n", n, 1, self.settings.max_candidates)
        # API 는 한 번에 최대 3개 → 나눠서 호출
        batches = [min(T2S_MAX_PER_CALL, n - i) for i in range(0, n, T2S_MAX_PER_CALL)]
        payloads = [({"version": version, "prompt": prompt, "num_sample": k}, k) for k in batches]
        params = {"prompt": prompt, "n": n, "version": version}
        return await self._generate(tool="generate_sfx", endpoint="text2sound", payloads=payloads, params=params,
                                    folder=self._folder(output_dir), purpose=purpose,
                                    description=description or prompt, note=note, parents=[])

    async def generate_from_sample(self, source: str, n: int = 3, strength: float = 1.0,
                                   include_begin: float | None = None, include_end: float | None = None,
                                   purpose: str = "var", description: str = "", output_dir: str | None = None,
                                   note: str | None = None) -> dict[str, Any]:
        src = audio_io.require_file(source)
        _check_range("strength", strength, *VAR_STRENGTH_RANGE)
        _check_range("n", n, 1, self.settings.max_candidates)
        spec = ENDPOINTS["variation"]
        b64, duration = audio_io.encode_for_api(src, min_s=spec.min_input_s, max_s=spec.max_input_s)
        region = self._region(include_begin, include_end, duration, "include")
        batches = [min(VAR_MAX_PER_CALL, n - i) for i in range(0, n, VAR_MAX_PER_CALL)]
        payloads = []
        for k in batches:
            body: dict[str, Any] = {"source": b64, "num_sample": k, "strength": strength}
            if region:
                body["include"] = region
            payloads.append((body, 0))
        params = {"source": str(src), "n": n, "strength": strength, "include": region}
        return await self._generate(tool="generate_from_sample", endpoint="variation", payloads=payloads,
                                    params=params, folder=self._folder(output_dir),
                                    purpose=purpose, description=description or src.stem, note=note,
                                    parents=self._parents(src))

    async def make_loop(self, source: str, preserve_begin: float | None = None, preserve_end: float | None = None,
                        purpose: str = "loop", description: str = "", output_dir: str | None = None,
                        note: str | None = None) -> dict[str, Any]:
        src = audio_io.require_file(source)
        spec = ENDPOINTS["looping"]
        b64, duration = audio_io.encode_for_api(src, min_s=spec.min_input_s, max_s=spec.max_input_s)
        region = self._region(preserve_begin, preserve_end, duration, "preserve")
        body: dict[str, Any] = {"source": b64}
        if region:
            body["preserve"] = region
        params = {"source": str(src), "preserve": region}
        return await self._generate(tool="make_loop", endpoint="looping", payloads=[(body, 0)], params=params,
                                    folder=self._folder(output_dir),
                                    purpose=purpose, description=description or src.stem, note=note,
                                    parents=self._parents(src))

    async def mono_to_stereo(self, source: str, purpose: str = "st", description: str = "",
                             output_dir: str | None = None, note: str | None = None) -> dict[str, Any]:
        src = audio_io.require_file(source)
        spec = ENDPOINTS["mono2stereo"]
        b64, _ = audio_io.encode_for_api(src, min_s=spec.min_input_s, max_s=spec.max_input_s, mono=True)
        return await self._generate(tool="mono_to_stereo", endpoint="mono2stereo", payloads=[({"source": b64}, 0)],
                                    params={"source": str(src)},
                                    folder=self._folder(output_dir),
                                    purpose=purpose, description=description or src.stem, note=note,
                                    parents=self._parents(src))

    async def creature_voice(self, source: str, reference: str, ratio: float = 1.0, enhance: bool = False,
                             purpose: str = "creature", description: str = "", output_dir: str | None = None,
                             note: str | None = None) -> dict[str, Any]:
        src, ref = audio_io.require_file(source), audio_io.require_file(reference)
        _check_range("ratio", ratio, *CONV_RATIO_RANGE)
        spec = ENDPOINTS["conversion"]
        src_b64, _ = audio_io.encode_for_api(src, min_s=spec.min_input_s, max_s=spec.max_input_s, label="source")
        ref_b64, _ = audio_io.encode_for_api(ref, min_s=spec.min_input_s, max_s=spec.max_input_s, label="reference")
        body = {"source": src_b64, "reference": ref_b64, "ratio": ratio, "enhance": enhance}
        params = {"source": str(src), "reference": str(ref), "ratio": ratio, "enhance": enhance}
        return await self._generate(tool="creature_voice", endpoint="conversion", payloads=[(body, 0)], params=params,
                                    folder=self._folder(output_dir),
                                    purpose=purpose, description=description or src.stem, note=note,
                                    parents=self._parents(src, ref))

    async def enhance_speech(self, source: str, purpose: str = "enh", description: str = "",
                             output_dir: str | None = None, note: str | None = None) -> dict[str, Any]:
        src = audio_io.require_file(source)
        b64, _ = audio_io.encode_for_api(src, min_s=None, max_s=None)
        return await self._generate(tool="enhance_speech", endpoint="enhance", payloads=[({"source": b64}, 0)],
                                    params={"source": str(src)},
                                    folder=self._folder(output_dir),
                                    purpose=purpose, description=description or src.stem, note=note,
                                    parents=self._parents(src))

    # ------------------------------------------------------------ 분석 (로컬)

    def analyze_audio(self, path: str, silence_db: float = analysis.DEFAULT_SILENCE_DB) -> dict[str, Any]:
        return analysis.analyze(audio_io.require_file(path), silence_db)

    def compare_audio(self, paths: list[str], target: dict[str, float] | None = None,
                      weights: dict[str, float] | None = None, goal: str | None = None,
                      silence_db: float = analysis.DEFAULT_SILENCE_DB) -> dict[str, Any]:
        if len(paths) < 1:
            raise invalid("paths 가 비었습니다.", fix="비교할 파일 경로를 1개 이상 주세요.")
        analyses = [analysis.analyze(audio_io.require_file(p), silence_db) for p in paths]
        table = [{"path": a["path"], **{k: a["metrics"][k] for k in analysis.RANKABLE if k in a["metrics"]}}
                 for a in analyses]
        result: dict[str, Any] = {"goal": goal, "table": table,
                                  "interpretations": {a["path"]: a["interpretation"] for a in analyses}}
        if target:
            result["ranking"] = analysis.rank(analyses, target, weights)
            result["target"] = target
        else:
            result["ranking"] = None
            result["ranking_note"] = "target 을 주면 목표에 가까운 순으로 정렬합니다."
        return result

    # ------------------------------------------------------------ 후처리 (로컬)

    def _post(self, tool: str, src: Path, result: dict[str, Any], params: dict[str, Any],
              note: str | None) -> dict[str, Any]:
        out = Path(result["output"])
        rec = manifest.append(out.parent, {"tool": tool, "params": params, "seed": None,
                                           "parents": self._parents(src), "files": [str(out)],
                                           "credits_estimated": 0, "note": note, "description": out.stem}, SERVICE)
        metrics = analysis.analyze(out)["metrics"]
        return {"tool": tool, "source": str(src), **result, **audio_io.info(out),
                "quick_metrics": {k: metrics[k] for k in QUICK_METRICS},
                "manifest_id": rec["id"], "created_at": rec["created_at"], "credits_estimated": 0}

    def trim_silence(self, path: str, threshold_db: float = analysis.DEFAULT_SILENCE_DB, pad_ms: float = 10.0,
                     trim_start: bool = True, trim_end: bool = True, output_path: str | None = None,
                     overwrite: bool = False, note: str | None = None) -> dict[str, Any]:
        src = audio_io.require_file(path)
        params = {"threshold_db": threshold_db, "pad_ms": pad_ms, "trim_start": trim_start, "trim_end": trim_end}
        res = processing.trim_silence(src, output_path=output_path, overwrite=overwrite, **params)
        return self._post("trim_silence", src, res, params, note)

    def normalize(self, path: str, mode: str = "lufs", target: float | None = None, peak_ceiling_db: float = -1.0,
                  output_path: str | None = None, overwrite: bool = False, note: str | None = None) -> dict[str, Any]:
        src = audio_io.require_file(path)
        res = processing.normalize(src, mode, target, peak_ceiling_db, output_path=output_path, overwrite=overwrite)
        return self._post("normalize", src, res, {"mode": mode, "target": res["target"],
                                                  "peak_ceiling_db": peak_ceiling_db}, note)

    def fade(self, path: str, fade_in_ms: float = 0.0, fade_out_ms: float = 0.0, curve: str = "linear",
             output_path: str | None = None, overwrite: bool = False, note: str | None = None) -> dict[str, Any]:
        src = audio_io.require_file(path)
        res = processing.fade(src, fade_in_ms, fade_out_ms, curve, output_path=output_path, overwrite=overwrite)
        return self._post("fade", src, res, {"fade_in_ms": fade_in_ms, "fade_out_ms": fade_out_ms,
                                             "curve": curve}, note)

    def convert(self, path: str, format: str | None = None, sample_rate: int | None = None,
                channels: int | None = None, bit_depth: int = 16, quality: float = 0.7,
                output_path: str | None = None, overwrite: bool = False, note: str | None = None) -> dict[str, Any]:
        src = audio_io.require_file(path)
        fmt = (format or (Path(output_path).suffix.lstrip(".") if output_path else self.settings.default_format)).lower()
        res = processing.convert(src, fmt, sample_rate, channels, bit_depth, quality,
                                 output_path=output_path, overwrite=overwrite)
        return self._post("convert", src, res, {"format": fmt, "sample_rate": sample_rate, "channels": channels,
                                                "bit_depth": bit_depth, "quality": quality}, note)

    # ------------------------------------------------------------ 관리

    def check_credits(self) -> dict[str, Any]:
        return {
            "credits_remaining": None,
            "note": REMAINING_NOTE,
            "session_credits_estimated": self.credits_spent,
            "session_calls": dict(self.calls),
            "session_samples": dict(self.samples),
            "estimate_basis": "문서 단가 기준 추정(1크레딧=$0.01). 호출당 최소 차감량 때문에 실제와 다를 수 있음.",
            "api_key_configured": bool(self.settings.api.api_key),
        }

    def estimate_cost(self, tool: str, n: int = 1) -> dict[str, Any]:
        mapping = {"generate_sfx": "text2sound", "generate_from_sample": "variation", "make_loop": "looping",
                   "mono_to_stereo": "mono2stereo", "creature_voice": "conversion", "enhance_speech": "enhance"}
        if tool not in mapping:
            raise invalid(f"tool={tool!r} 는 크레딧을 쓰는 도구가 아닙니다.", allowed=", ".join(mapping),
                          fix="후처리·분석 도구는 로컬에서 돌아 크레딧이 0 입니다.")
        endpoint = mapping[tool]
        per_call = {"text2sound": T2S_MAX_PER_CALL, "variation": VAR_MAX_PER_CALL}.get(endpoint, 1)
        calls = -(-n // per_call) if per_call > 1 else n
        samples = n if endpoint == "text2sound" else 0
        return {"tool": tool, "n": n, "api_calls": calls,
                "credits_estimated": estimate_credits(endpoint, calls, samples),
                "usd_estimated": round(estimate_credits(endpoint, calls, samples) * 0.01, 4),
                "note": "문서 단가 기준 추정. 호출당 최소 차감량이 적용될 수 있음."}

    def list_history(self, folder: str | None = None, query: str | None = None, tool: str | None = None,
                     limit: int = 20) -> dict[str, Any]:
        f = self._folder(folder)
        rows = manifest.search(f, query, tool, limit)
        return {"folder": str(f), "count": len(rows), "records": rows}

    def build_audition_page(self, paths: list[str], title: str = "VARCO Sound 후보 비교",
                            note: str | None = None, output_path: str | None = None) -> dict[str, Any]:
        if not paths:
            raise invalid("paths 가 비었습니다.", fix="들어볼 파일 경로를 1개 이상 주세요.")
        files = [audio_io.require_file(p) for p in paths]
        items = [{"path": str(p), "analysis": analysis.analyze(p), "record": manifest.find_by_file(p.parent, str(p))}
                 for p in files]
        page = audio_io.resolve_path(output_path) if output_path else \
            files[0].parent / f"audition_{datetime.now():%Y%m%d_%H%M%S}.html"
        audition.build(page, title, items, note)
        return {"page": str(page), "url": page.as_uri(), "count": len(items),
                "next_step": "사용자에게 이 페이지를 브라우저로 열어 귀로 골라 달라고 요청하세요."}
