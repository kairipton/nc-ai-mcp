"""VARCO Sound MCP 서버 진입점 (stdio).

실행: python <저장소>/varco_sound/server.py  (Claude Code 가 사용자 범위로 등록해 실행한다)

도구 설명(description)은 Claude 가 읽는 사용 설명서다. 로직은 varco_sound.service 에 있고,
여기서는 인자 스키마·설명을 정의하고 NcAiError 를 Claude 가 고칠 수 있는 JSON 오류로 바꾼다.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Annotated, Any, Awaitable, Callable, Literal

# 스크립트로 직접 실행되면 sys.path[0] 이 이 폴더가 되므로, 저장소 루트로 바꿔 패키지 import 가 되게 한다.
if __package__ in (None, ""):
    sys.path[0] = str(Path(__file__).resolve().parent.parent)

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from nc_ai_common.errors import NcAiError, redact
from varco_sound.api_spec import estimate_credits
from varco_sound.settings import load_settings
from varco_sound.guide import GUIDE
from varco_sound.service import MODE_RULES, VarcoService

log = logging.getLogger("varco_sound.server")
settings = load_settings()
service = VarcoService(settings)

INSTRUCTIONS = (
    "NC AI VARCO Sound API 로 게임 효과음을 생성·변형·루프화하고, 로컬에서 분석·후처리하는 서버. "
    "너는 소리를 들을 수 없으므로 생성 후 반드시 analyze_audio/compare_audio 수치로 판단하라. "
    "API 는 시드·비동기·잔액 조회를 지원하지 않는다. 처음 쓰면 get_guide 를 먼저 읽어라. "
    f"모든 생성 결과의 기본 저장 폴더: {settings.output_dir}. "
    f"크레딧 모드: {settings.credit_mode} (세션 예산 {settings.session_budget or '상한 없음'} 크레딧, 서버가 초과 호출을 거부). "
    f"모드 규칙: {MODE_RULES[settings.credit_mode]} "
    "사용자가 대화에서 더 아끼라고 하면 더 엄격하게 따르되, 예산·모드를 올리는 것은 사용자만 결정한다."
)

mcp = MCPServer("varco-sound", instructions=INSTRUCTIONS)

C_T2S = estimate_credits("text2sound", 1, 1)
C_T2S3 = estimate_credits("text2sound", 1, 3)
C_VAR = estimate_credits("variation")
C_LOOP = estimate_credits("looping")
C_M2S = estimate_credits("mono2stereo")
C_CONV = estimate_credits("conversion")
C_ENH = estimate_credits("enhance")


async def _guard(fn: Callable[[], Awaitable[dict[str, Any]] | dict[str, Any]]) -> dict[str, Any]:
    """NcAiError 를 JSON 오류 메시지로 바꿔 Claude 가 code/allowed/fix 를 읽고 바로 고치게 한다."""
    try:
        result = fn()
        if hasattr(result, "__await__"):
            result = await result  # type: ignore[assignment]
        return result  # type: ignore[return-value]
    except NcAiError as err:
        raise ToolError(err.to_json()) from None
    except Exception as exc:  # 예상 못 한 오류도 원인을 보이게 한다(키는 지움). 전체 추적은 stderr 로그에.
        log.exception("도구 실행 중 예외")
        raise ToolError(NcAiError("internal_error", redact(f"{type(exc).__name__}: {exc}", settings.api.api_key),
                                   fix="입력 파일이 손상되지 않았는지 확인하세요. 반복되면 사용자에게 서버 로그를 보여 달라고 요청하세요."
                                   ).to_json()) from None


# 공통 인자 타입
OutputDir = Annotated[str | None, Field(description="저장 폴더 절대 경로. 생략하면 서버 기본 출력 폴더(후보 보관용). "
                                                    "최종본은 후처리 도구의 output_path 로 게임 폴더에 내보내는 것을 권장.")]
Purpose = Annotated[str, Field(description="파일 이름 맨 앞 용도 태그(영문). 예: sfx, ui, amb, hit, goal")]
Description = Annotated[str, Field(description="파일 이름에 들어갈 짧은 영어 설명(예: kick_heavy). 비우면 프롬프트/원본 이름에서 만든다. 한글은 빠진다.")]
Note = Annotated[str | None, Field(description="manifest 에 남길 사용 목적 메모(예: '축구 슈팅 임팩트, auto-soccer'). 나중에 list_history 로 찾을 때 쓰인다.")]
AudioPath = Annotated[str, Field(description="오디오 파일 절대 경로(wav/ogg/mp3/flac)")]


# ================================================================ 생성 (API, 크레딧 소모)

@mcp.tool(description=f"""텍스트로 효과음·환경음 후보 여러 개를 생성해 WAV 로 저장한다.

언제: 새 효과음(타격, UI, 득점, 실패, 발소리, 환경음 등)이 필요할 때. 같은 프롬프트도 매번 다르게 나오므로 n=3~4 로 뽑아 비교한다.
언제 쓰지 말 것: 멜로디가 있는 음악(모델이 부적합), 이미 비슷한 소리가 있는지 확인 안 했을 때(list_history 먼저).
결과 특성(실측): 48kHz 16bit 모노 WAV, 피크 -2dBFS 로 맞춰져 옴. 길이는 가변(1.4~5.8초).
  한 파일에 같은 소리가 여러 번(실측 2~7번) 들어 있는 경우가 많다 → analyze 의 event_count 확인 후
  단발이 필요하면 split_events 로 나눈다(나뉜 조각들은 그 자체로 변형 후보가 된다).
인자:
- prompt: 최대 200자. 한국어·영어 모두 이해. 핵심 음원/재질 + 동작 + 공간 + 성격 위주로 짧게.
- n: 후보 수 1~{settings.max_candidates}. API 가 한 번에 3개까지라 3개 단위로 나눠 호출한다.
  비용 대부분이 호출 기본료라 n 은 3의 배수가 효율적(n=3 약 {C_T2S3}, n=4 는 2회 호출로 약 13.8).
- version: v1(기본) 또는 v2. 차이는 문서에 없음.
비용: 호출 1회(3개) 약 {C_T2S3} 크레딧, 1개면 약 {C_T2S} 크레딧(문서 단가 추정). 시간: 실측 호출(3개)당 약 10~11초.
반환: files[{{path, duration_s, sample_rate, channels, quick_metrics}}], params, request_ids, manifest_id, credits_estimated.
예: generate_sfx(prompt="heavy leather ball kicked hard, short punchy thump", n=3, purpose="sfx", description="kick_heavy")""")
async def generate_sfx(
    prompt: Annotated[str, Field(description="만들 소리 설명. 최대 200자.", max_length=200)],
    n: Annotated[int, Field(description="후보 개수", ge=1, le=settings.max_candidates)] = 3,
    version: Annotated[Literal["v1", "v2"], Field(description="생성 모델 버전")] = "v1",
    purpose: Purpose = "sfx",
    description: Description = "",
    output_dir: OutputDir = None,
    note: Note = None,
) -> dict[str, Any]:
    return await _guard(lambda: service.generate_sfx(prompt, n, version, purpose, description, output_dir, note))


@mcp.tool(description=f"""기존 오디오 하나를 바탕으로 톤은 유지하고 세부가 다른 변형(variation)을 만든다.

언제: 마음에 드는 효과음을 찾은 뒤 반복 재생용 테이크(발소리·타격·총소리 2~5개)를 늘릴 때, 특정 구간만 고칠 때.
언제 쓰지 말 것: 음악·멜로디·사람 목소리(품질 보장 안 됨).
입력: 0.5~10초(길면 먼저 trim_silence). 0.5초보다 짧은 단발(split_events 조각 등)은 뒤에 무음을 붙여 0.5초로 맞춰 보낸다
  (실측: 0.27초 킥을 0.6초로 패딩해 정상 동작). 출력(실측): 모노, 길이 가변(대개 입력 길이 근처, 가끔 10초) → trim_silence.
인자:
- strength 0.0~3.0(기본 1.0): 높을수록 원본과 차이가 커짐. 원본 느낌 유지는 0.3~0.6, 실험적이면 1.5 이상.
- include_begin/include_end(초): 이 구간만 새로 만들고 나머지는 원본 유지. 둘 다 생략하면 전체 변형.
- n: 1~{settings.max_candidates}(API 는 한 번에 5개까지, 넘으면 나눠 호출).
실측 strength 효과(단일 킥 기준): 0.3 → 원본과 거의 같은 밝기·저역(무게중심 ~230Hz), 2.0 → 더 밝고(~600Hz) 가볍고 레벨이 5dB 정도 낮아짐.
비용: 호출당 약 {C_VAR} 크레딧(5개까지 한 호출). 시간: 실측 3~6초.
예: generate_from_sample(source="C:/.../sfx_kick_heavy_20261028_01_trim.wav", n=3, strength=0.5)""")
async def generate_from_sample(
    source: AudioPath,
    n: Annotated[int, Field(description="변형 개수", ge=1, le=settings.max_candidates)] = 3,
    strength: Annotated[float, Field(description="변형 강도(클수록 원본과 다름)", ge=0.0, le=3.0)] = 1.0,
    include_begin: Annotated[float | None, Field(description="변형할 구간 시작(초)", ge=0)] = None,
    include_end: Annotated[float | None, Field(description="변형할 구간 끝(초)", ge=0)] = None,
    purpose: Purpose = "var",
    description: Description = "",
    output_dir: OutputDir = None,
    note: Note = None,
) -> dict[str, Any]:
    return await _guard(lambda: service.generate_from_sample(source, n, strength, include_begin, include_end,
                                                             purpose, description, output_dir, note))


@mcp.tool(description=f"""오디오의 시작과 끝을 AI 로 이어 끊김 없이 반복 재생되는 루프로 만든다.

언제: 바람·비·관중 웅성임·기계음 같은 지속형 환경음을 배경 루프로 쓸 때.
언제 쓰지 말 것: 박자가 있는 음악·보컬(루프 지점에서 박자가 어긋남), 단발 효과음.
입력: 1초~60분. 출력: 입력보다 다소 짧아짐(정상). 너무 짧거나 급변하는 소리는 루프 품질이 떨어짐.
인자: preserve_begin/preserve_end(초) — 루프 처리 중 잘리면 안 되는 구간(예: 중간의 특정 새소리).
비용: 호출당 약 {C_LOOP} 크레딧.
예: make_loop(source="C:/.../amb_stadium_crowd_20261028_01.wav")""")
async def make_loop(
    source: AudioPath,
    preserve_begin: Annotated[float | None, Field(description="보존 구간 시작(초)", ge=0)] = None,
    preserve_end: Annotated[float | None, Field(description="보존 구간 끝(초)", ge=0)] = None,
    purpose: Purpose = "loop",
    description: Description = "",
    output_dir: OutputDir = None,
    note: Note = None,
) -> dict[str, Any]:
    return await _guard(lambda: service.make_loop(source, preserve_begin, preserve_end, purpose, description,
                                                  output_dir, note))


@mcp.tool(description=f"""모노 오디오를 AI 로 공간감 있는 스테레오로 확장한다(단순 채널 복제가 아님).

언제: 환경음·Foley 가 밋밋하게 중앙에만 들릴 때(analyze_audio 의 stereo_width≈0). 스테레오 입력은 모노로 합쳐 보낸다.
언제 쓰지 말 것: 음악·멜로디(위상 문제), 게임에서 3D 위치로 재생할 소리(3D 공간화는 모노가 유리).
입력: 0.5~10초. 출력: 2채널. 생성형이라 원본과 질감이 미세하게 달라질 수 있음.
비용: 호출당 약 {C_M2S} 크레딧.
예: mono_to_stereo(source="C:/.../amb_wind_20261028_01_trim.wav")""")
async def mono_to_stereo(
    source: AudioPath,
    purpose: Purpose = "st",
    description: Description = "",
    output_dir: OutputDir = None,
    note: Note = None,
) -> dict[str, Any]:
    return await _guard(lambda: service.mono_to_stereo(source, purpose, description, output_dir, note))


@mcp.tool(description=f"""소스 목소리(source)를 참조 소리(reference)의 몬스터·크리처 음색으로 바꾼다. 말의 억양·길이는 유지.

언제: 보스·몬스터·NPC 의 대사/포효/신음. reference 는 generate_sfx 로 만든 'low guttural monster growl' 같은 소리도 가능.
언제 쓰지 말 것: 노래·멜로디, 사람→사람 음성 변환.
입력: source·reference 모두 0.5~10초. 출력 길이 = source 길이.
인자:
- ratio 0.0~2.0(기본 1.0): 0 에 가까우면 원본 음색, 클수록 참조 스타일이 강함. 1.2 이상이면 참조 음색과도 멀어질 수 있음.
- enhance: source 에 잡음이 많으면 true(내부에서 잡음 제거 후 변환).
비용: 호출당 약 {C_CONV} 크레딧.
예: creature_voice(source="C:/.../line_attack.wav", reference="C:/.../creature_growl_01.wav", ratio=1.0)""")
async def creature_voice(
    source: AudioPath,
    reference: Annotated[str, Field(description="입힐 음색의 참조 오디오 절대 경로(배경음 없는 깨끗한 샘플일수록 좋음)")],
    ratio: Annotated[float, Field(description="변환 강도", ge=0.0, le=2.0)] = 1.0,
    enhance: Annotated[bool, Field(description="소스 잡음 제거 후 변환")] = False,
    purpose: Purpose = "creature",
    description: Description = "",
    output_dir: OutputDir = None,
    note: Note = None,
) -> dict[str, Any]:
    return await _guard(lambda: service.creature_voice(source, reference, ratio, enhance, purpose, description,
                                                       output_dir, note))


@mcp.tool(description=f"""사람 목소리 녹음에서 배경 잡음·잔향을 제거해 선명하게 만든다.

언제: 직접 녹음한 대사·가이드 음성의 잡음 제거.
언제 쓰지 말 것: 효과음·자연음·음악(잡음으로 보고 깎아 먹음), 클리핑으로 찢어진 녹음 복구.
출력: 문서상 44.1kHz 16bit WAV(실측 전). 최대 입력 길이는 문서에 없음.
비용: 호출당 약 {C_ENH} 크레딧.
예: enhance_speech(source="C:/.../line_attack_raw.wav")""")
async def enhance_speech(
    source: AudioPath,
    purpose: Purpose = "enh",
    description: Description = "",
    output_dir: OutputDir = None,
    note: Note = None,
) -> dict[str, Any]:
    return await _guard(lambda: service.enhance_speech(source, purpose, description, output_dir, note))


# ================================================================ 분석 (로컬, 무료)

@mcp.tool(description="""오디오 파일 하나를 수치로 분석하고 각 수치를 짧게 해석한다. 너는 소리를 못 들으므로 생성·후처리 후 항상 쓴다.

지표: duration_s, effective_duration_s(앞뒤 무음 제외), peak_dbfs, rms_dbfs, integrated_lufs(0.4초 미만은 null),
leading/trailing_silence_ms, silence_ratio, event_count(파일 안 소리 개수), first_event_duration_s,
attack_ms(첫 소리 시작→최대), decay_ms(첫 소리 최대→-20dB),
spectral_centroid_hz(밝기), low/mid/high_ratio(20-250 / 250-4k / 4k+ Hz 에너지 비율), clipping_runs, stereo_width(side/mid), stereo_correlation.
해석 문구는 일반적인 효과음 기준의 휴리스틱이다(예: 어택<10ms 매우 날카로움, 무게중심<800Hz 어둡고 묵직함).
인자: silence_db — 무음으로 볼 레벨(기본 -50dBFS). 잔향 꼬리를 소리로 치려면 -60.
비용: 0(로컬). 시간: 1초 미만.
예: analyze_audio(path="C:/.../sfx_kick_heavy_20261028_01.wav")""")
async def analyze_audio(
    path: AudioPath,
    silence_db: Annotated[float, Field(description="무음 임계값 dBFS", le=0, ge=-100)] = -50.0,
) -> dict[str, Any]:
    return await _guard(lambda: service.analyze_audio(path, silence_db))


@mcp.tool(description="""후보 여러 개를 같은 지표 표로 비교하고, target 에 가장 가까운 순서로 정렬해 근거와 함께 돌려준다.

언제: generate_sfx/generate_from_sample 후보 중 고를 때. 말로 된 목표를 지표 목표값으로 옮겨 target 에 넣는다.
  예) "짧고 묵직한 타격음" → {"effective_duration_s":0.4, "attack_ms":5, "spectral_centroid_hz":700, "low_ratio":0.5}
  예) "밝고 가벼운 UI 클릭" → {"effective_duration_s":0.08, "attack_ms":2, "spectral_centroid_hz":4000, "high_ratio":0.35}
target 에 쓸 수 있는 지표: effective_duration_s, duration_s, attack_ms, decay_ms, spectral_centroid_hz, low_ratio, mid_ratio,
high_ratio, peak_dbfs, rms_dbfs, integrated_lufs, stereo_width, silence_ratio. 시간·주파수는 비율(로그) 차이, 나머지는 선형 차이로 잰다.
weights 로 지표별 중요도(기본 1)를 줄 수 있다. score 가 작을수록 목표에 가깝다.
주의: 수치가 가깝다고 듣기에 좋다는 보장은 없다. 최종 선택은 build_audition_page 로 사람에게 확인받는 것을 권장.
비용: 0(로컬).
예: compare_audio(paths=[...], goal="짧고 묵직한 킥", target={"effective_duration_s":0.4,"spectral_centroid_hz":700})""")
async def compare_audio(
    paths: Annotated[list[str], Field(description="비교할 오디오 절대 경로 목록", min_length=1)],
    target: Annotated[dict[str, float] | None, Field(description="지표 이름 → 목표값")] = None,
    weights: Annotated[dict[str, float] | None, Field(description="지표 이름 → 가중치(기본 1)")] = None,
    goal: Annotated[str | None, Field(description="사람 말로 된 목표(결과에 그대로 기록)")] = None,
    silence_db: Annotated[float, Field(description="무음 임계값 dBFS", le=0, ge=-100)] = -50.0,
) -> dict[str, Any]:
    return await _guard(lambda: service.compare_audio(paths, target, weights, goal, silence_db))


# ================================================================ 후처리 (로컬, 무료)

OutputPath = Annotated[str | None, Field(description="결과 파일 절대 경로(확장자로 포맷 결정). 생략하면 원본 옆에 '_trim' 같은 접미사로 저장. "
                                                     "게임 폴더로 바로 내보낼 때 사용.")]
Overwrite = Annotated[bool, Field(description="output_path 가 이미 있을 때 덮어쓸지. 원본 자체는 절대 덮어쓰지 않음.")]


@mcp.tool(description="""앞뒤 무음을 잘라 새 파일로 저장한다(원본 유지).

언제: generate_sfx/generate_from_sample 결과 직후 거의 항상(앞뒤 무음·잔향 꼬리 정리). 끝이 뚝 끊기면 이어서 fade(fade_out_ms=20~50).
인자: threshold_db(기본 -50dBFS, 잔향을 더 남기려면 -60), pad_ms(자른 경계에 남길 여유, 기본 10ms), trim_start/trim_end.
비용: 0. 예: trim_silence(path="C:/.../sfx_kick_heavy_20261028_01.wav")""")
async def trim_silence(
    path: AudioPath,
    threshold_db: Annotated[float, Field(description="무음 임계값 dBFS", le=0, ge=-100)] = -50.0,
    pad_ms: Annotated[float, Field(description="경계 여유(ms)", ge=0, le=1000)] = 10.0,
    trim_start: Annotated[bool, Field(description="앞 무음 자르기")] = True,
    trim_end: Annotated[bool, Field(description="뒤 무음 자르기")] = True,
    output_path: OutputPath = None,
    overwrite: Overwrite = False,
    note: Note = None,
) -> dict[str, Any]:
    return await _guard(lambda: service.trim_silence(path, threshold_db, pad_ms, trim_start, trim_end,
                                                     output_path, overwrite, note))


@mcp.tool(description="""한 파일에 여러 번 들어 있는 소리(예: 킥 4~6번)를 하나씩 잘라 '원본_ev01.wav, _ev02 …' 로 저장한다(원본 유지).

언제: analyze_audio 의 event_count 가 2 이상인데 단발 효과음 하나가 필요할 때. generate_sfx 결과는 한 파일에
  같은 소리를 여러 번 담아 오는 경우가 많다(실측). 잘라 낸 조각들은 그대로 '변형 후보'로도 쓸 수 있다.
언제 쓰지 말 것: 이어지는 환경음(한 덩어리로 잡힘), 일부러 연속된 소리(연타·발소리 시퀀스)를 통째로 쓸 때.
판정 기준: 파일 최대 레벨 -15dB 위로 올라온 곳을 소리 시작, -30dB 아래로 내려간 곳을 끝으로 보고, 앞뒤를 무음 임계값까지 넓힌다.
인자: silence_db(기본 -50, 꼬리를 더 남기려면 -60), pad_ms(기본 10), min_event_ms(이보다 짧은 조각은 버림, 기본 30), output_dir(생략 시 원본 폴더).
비용: 0. 예: split_events(path="C:/.../sfx_kick_heavy_20261028_01.wav")""")
async def split_events(
    path: AudioPath,
    silence_db: Annotated[float, Field(description="무음 임계값 dBFS", le=0, ge=-100)] = -50.0,
    pad_ms: Annotated[float, Field(description="조각 앞뒤 여유(ms)", ge=0, le=1000)] = 10.0,
    min_event_ms: Annotated[float, Field(description="최소 조각 길이(ms)", ge=0, le=5000)] = 30.0,
    output_dir: Annotated[str | None, Field(description="저장 폴더(생략 시 원본 폴더)")] = None,
    note: Note = None,
) -> dict[str, Any]:
    return await _guard(lambda: service.split_events(path, silence_db, pad_ms, min_event_ms, output_dir, note))


@mcp.tool(description="""목표 라우드니스(LUFS) 또는 피크(dBFS)로 레벨을 맞춰 새 파일로 저장한다.

언제: 효과음끼리 크기를 맞출 때, 최종 내보내기 직전. 프로젝트에 라우드니스 기준이 있으면 그것을 쓰고, 없으면 사용자에게 묻는다.
인자:
- mode: 'lufs'(기본, 체감 크기 기준) 또는 'peak'(최대 샘플 기준, 0.4초 미만 짧은 소리는 peak 만 가능).
- target: lufs 기본 -18(ASWG-R001 의 모바일/웹 게임 전체 믹스 권장치를 출발점으로 쓴 값), peak 기본 -1.
- peak_ceiling_db: 피크 상한(기본 -1dBFS). 넘으면 게인을 줄이고 limited_by_peak_ceiling=true(리미터 없음).
비용: 0. 예: normalize(path="C:/.../kick_trim.wav", mode="lufs", target=-18)""")
async def normalize(
    path: AudioPath,
    mode: Annotated[Literal["lufs", "peak"], Field(description="맞출 기준")] = "lufs",
    target: Annotated[float | None, Field(description="목표값(LUFS 또는 dBFS). 생략 시 lufs=-18, peak=-1", le=0, ge=-60)] = None,
    peak_ceiling_db: Annotated[float, Field(description="피크 상한 dBFS", le=0, ge=-30)] = -1.0,
    output_path: OutputPath = None,
    overwrite: Overwrite = False,
    note: Note = None,
) -> dict[str, Any]:
    return await _guard(lambda: service.normalize(path, mode, target, peak_ceiling_db, output_path, overwrite, note))


@mcp.tool(description="""페이드 인/아웃을 넣어 새 파일로 저장한다.

언제: trim 후 끝이 뚝 끊겨 클릭음이 날 때(fade_out 20~50ms), 루프가 아닌 환경음의 시작·끝을 부드럽게 할 때.
인자: fade_in_ms, fade_out_ms(합이 파일 길이보다 짧아야 함), curve 'linear' 또는 'cosine'(더 부드러운 S자).
비용: 0. 예: fade(path="C:/.../kick_trim.wav", fade_out_ms=30)""")
async def fade(
    path: AudioPath,
    fade_in_ms: Annotated[float, Field(description="페이드 인(ms)", ge=0)] = 0.0,
    fade_out_ms: Annotated[float, Field(description="페이드 아웃(ms)", ge=0)] = 0.0,
    curve: Annotated[Literal["linear", "cosine"], Field(description="곡선")] = "linear",
    output_path: OutputPath = None,
    overwrite: Overwrite = False,
    note: Note = None,
) -> dict[str, Any]:
    return await _guard(lambda: service.fade(path, fade_in_ms, fade_out_ms, curve, output_path, overwrite, note))


@mcp.tool(description=f"""포맷(wav/ogg/mp3/flac)·샘플레이트·채널을 바꿔 새 파일로 저장한다.

언제: 최종 내보내기. 유니티는 wav 를 넣고 임포트 설정에서 압축하는 게 일반적, 웹게임은 ogg 또는 mp3 로 용량을 줄인다.
인자:
- format: 생략하면 output_path 확장자, 그것도 없으면 서버 기본({settings.default_format}).
- sample_rate: 예 44100, 48000, 22050(생략 시 유지). channels: 1(모노로 합침) 또는 2.
- bit_depth: wav/flac 16 또는 24(wav 는 32 float 도 가능). quality: ogg/mp3 음질 0~1(기본 0.7, 높을수록 고음질·큰 용량).
비용: 0. 예: convert(path="C:/.../kick_norm.wav", output_path="C:/Works/auto-soccer/public/audio/kick.ogg")""")
async def convert(
    path: AudioPath,
    format: Annotated[Literal["wav", "ogg", "mp3", "flac"] | None, Field(description="출력 포맷")] = None,
    sample_rate: Annotated[int | None, Field(description="출력 샘플레이트(Hz)", ge=8000, le=192000)] = None,
    channels: Annotated[Literal[1, 2] | None, Field(description="출력 채널 수")] = None,
    bit_depth: Annotated[Literal[16, 24, 32], Field(description="wav/flac 비트 깊이")] = 16,
    quality: Annotated[float, Field(description="ogg/mp3 음질 0~1", ge=0, le=1)] = 0.7,
    output_path: OutputPath = None,
    overwrite: Overwrite = False,
    note: Note = None,
) -> dict[str, Any]:
    return await _guard(lambda: service.convert(path, format, sample_rate, channels, bit_depth, quality,
                                                output_path, overwrite, note))


# ================================================================ 관리

@mcp.tool(description="""크레딧 모드·세션 예산·남은 예산과 이번 세션의 API 호출 수, 추정 소모 크레딧을 돌려준다.

언제: 소리 작업을 시작할 때 먼저 호출해 credit_mode 와 mode_rules(이번 세션에서 지킬 작업 규칙)를 확인한다.
세션 예산을 넘는 생성 호출은 서버가 session_budget_exceeded 로 거부한다. 예산·모드 변경은 사용자만 결정한다.
주의: 공개 API 에 잔액 조회가 없어 credits_remaining 은 항상 null. 잔액은 사용자가 대시보드에서 확인해야 한다.
비용: 0.""")
async def check_credits() -> dict[str, Any]:
    return await _guard(service.check_credits)


@mcp.tool(description="""크레딧을 쓰는 도구를 호출하기 전에 예상 크레딧과 세션 예산 안인지(within_budget)를 계산한다(문서 단가 기준 추정).
같은 호출 수로 더 많이 받을 수 있으면 efficiency_tip 으로 알려 준다.

인자: tool(generate_sfx, generate_from_sample, make_loop, mono_to_stereo, creature_voice, enhance_speech), n(후보/호출 수).
비용: 0(계산만). 예: estimate_cost(tool="generate_sfx", n=6) → 2회 호출, 약 15.3 크레딧""")
async def estimate_cost(
    tool: Annotated[str, Field(description="도구 이름")],
    n: Annotated[int, Field(description="후보 수(생성 도구) 또는 호출 수", ge=1, le=100)] = 1,
) -> dict[str, Any]:
    return await _guard(lambda: service.estimate_cost(tool, n))


@mcp.tool(description="""manifest 기록(지금까지 만든 소리·후처리)을 최근 순으로 찾는다.

언제: 새로 생성하기 전에 비슷한 소리를 이미 만들었는지 확인(크레딧 절약), 어떤 프롬프트·파라미터로 만들었는지 확인할 때.
인자: folder(생략 시 기본 출력 폴더), query(프롬프트·메모·파일명 부분 일치), tool(도구 이름으로 거르기), limit.
비용: 0. 예: list_history(query="kick")""")
async def list_history(
    folder: Annotated[str | None, Field(description="manifest.json 이 있는 폴더")] = None,
    query: Annotated[str | None, Field(description="검색어")] = None,
    tool: Annotated[str | None, Field(description="도구 이름 필터")] = None,
    limit: Annotated[int, Field(description="최대 개수", ge=1, le=200)] = 20,
) -> dict[str, Any]:
    return await _guard(lambda: service.list_history(folder, query, tool, limit))


@mcp.tool(description="""후보 파일들을 한 페이지에서 재생·비교할 수 있는 로컬 HTML 을 만든다. 각 소리 옆에 프롬프트·파라미터·분석 수치 표시.

언제: 수치로 2~4개까지 좁힌 뒤 사람이 귀로 최종 선택해야 할 때. 만든 뒤 사용자에게 page 경로를 알려 브라우저로 열어 달라고 요청.
인자: paths, title, note(사람에게 보여줄 안내), output_path(생략 시 첫 파일 폴더에 audition_<시각>.html).
비용: 0.""")
async def build_audition_page(
    paths: Annotated[list[str], Field(description="들어볼 오디오 절대 경로 목록", min_length=1)],
    title: Annotated[str, Field(description="페이지 제목")] = "VARCO Sound 후보 비교",
    note: Annotated[str | None, Field(description="페이지 상단 안내 문구")] = None,
    output_path: Annotated[str | None, Field(description="HTML 저장 경로")] = None,
) -> dict[str, Any]:
    return await _guard(lambda: service.build_audition_page(paths, title, note, output_path))


@mcp.tool(description="""VARCO Sound 프롬프트 작성법·파라미터 효과·출력 특성·흔한 실패 대처를 담은 가이드를 돌려준다.
처음 쓸 때, 또는 결과가 의도와 다를 때 읽는다. 같은 내용이 resource varco://guide 에도 있다. 비용: 0.""")
async def get_guide() -> dict[str, Any]:
    return {"guide": GUIDE}


@mcp.resource("varco://guide", name="varco_sound_guide", description="VARCO Sound 프롬프트·파라미터 가이드",
              mime_type="text/markdown")
def guide_resource() -> str:
    return GUIDE


if __name__ == "__main__":
    mcp.run("stdio")
