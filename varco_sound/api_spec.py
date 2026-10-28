"""VARCO Sound 공개 API 의 문서상 사실(경로·범위·길이 제한·요금)을 한곳에 모은다.

출처: platform.nc-ai.com 레퍼런스/도큐먼트 (2026-10-28 조사).
범위 검증·도구 설명·비용 추정이 모두 이 값을 참조하므로, 문서가 바뀌면 여기만 고친다.
"""

from __future__ import annotations

from dataclasses import dataclass

# 1 크레딧 = $0.01 (가격 페이지: $10 Pack = 1,000 크레딧)
USD_PER_CREDIT = 0.01


@dataclass(frozen=True)
class EndpointSpec:
    path: str
    usd_per_call: float          # 호출당 기본 단가
    usd_per_sample: float = 0.0  # 샘플당 가산(Text to Sound 만)
    min_input_s: float | None = None
    max_input_s: float | None = None


ENDPOINTS: dict[str, EndpointSpec] = {
    # 200호출 / $10.71, 샘플 200개당 가산 $1.53
    "text2sound": EndpointSpec("/sound/varco/v1/api/text2sound", 10.71 / 200, 1.53 / 200),
    # 200호출 / $10.71, 입력 0.5~10초
    "variation": EndpointSpec("/sound/varco/v1/api/variation", 10.71 / 200, 0.0, 0.5, 10.0),
    # 100호출 / $1.53, 입력 0.5~10초
    "mono2stereo": EndpointSpec("/sound/varco/v1/api/mono2stereo", 1.53 / 100, 0.0, 0.5, 10.0),
    # 20호출 / $1.53, 입력 1초~60분
    "looping": EndpointSpec("/sound/varco/v1/api/looping", 1.53 / 20, 0.0, 1.0, 3600.0),
    # 40호출 / $1.53, source·reference 모두 0.5~10초
    "conversion": EndpointSpec("/sound/varco/v1/api/conversion", 1.53 / 40, 0.0, 0.5, 10.0),
    # 40호출 / $1.53, 최대 길이는 문서에 없음
    "enhance": EndpointSpec("/sound/varco/v1/api/enhance", 1.53 / 40),
}

# Text to Sound
T2S_PROMPT_MAX_CHARS = 200
T2S_MAX_PER_CALL = 3
T2S_VERSIONS = ("v1", "v2")

# Variation
VAR_MAX_PER_CALL = 5
VAR_STRENGTH_RANGE = (0.0, 3.0)

# Conversion
CONV_RATIO_RANGE = (0.0, 2.0)

# 출력 포맷: 문서는 44.1kHz 16bit WAV 라고 하지만, 실측(2026-10-28)은 48kHz 16bit 모노 WAV.
# 길이도 문서("10초 고정")와 달리 가변이다. 코드는 실제 파일을 읽어 판단하므로 값을 가정하지 않는다.


def estimate_credits(endpoint: str, calls: int = 1, samples: int = 0) -> float:
    """문서 단가로 계산한 추정 크레딧. 실제 차감은 '호출당 최소 차감량' 때문에 다를 수 있다."""
    spec = ENDPOINTS[endpoint]
    usd = spec.usd_per_call * calls + spec.usd_per_sample * samples
    return round(usd / USD_PER_CREDIT, 2)
