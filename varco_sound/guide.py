"""Claude 용 프롬프트·작업 가이드. MCP resource(varco://guide)와 get_guide 도구가 같은 텍스트를 낸다.

[문서] 표시는 공식 문서 근거, [실측] 표시는 스모크 테스트에서 직접 확인한 내용이다.
근거 없는 추측은 넣지 않는다.
"""

GUIDE = """\
# VARCO Sound 사용 가이드 (Claude 용)

## 1. 모델이 잘하는 것 / 못하는 것
- [문서] 환경음(앰비언스)과 단발 효과음(Foley/SFX)에 특화. 멜로디·구조가 있는 음악 작곡은 부적합.
- [문서] Variation·Looping·Mono to Stereo 는 음악·멜로디·사람 목소리에 쓰면 품질 보장 안 됨.
- [문서] Enhance 는 사람 목소리 전용. 자연음·음악에 쓰면 소리를 깎아 먹을 수 있음.
- [문서] Conversion(creature_voice)은 대사·신음·포효에 최적화. 노래는 부적합.

## 2. 프롬프트 쓰는 법 (generate_sfx)
- [문서] 최대 200자. 미사여구 대신 핵심 소리와 분위기 위주로. 문장이 길면 모든 요소를 담으려다 흐려짐.
- [문서] 한국어·영어·일본어 등 다국어를 그대로 이해(번역 불필요).
- [문서] 단순 키워드("물 떨어지는 소리")도 동작하고, 공간·상황을 묘사하면("축축한 동굴 안에서 물방울이
  떨어지며 울리는 소리") 그에 맞는 디테일이 붙음.
- 권장 구조: [음원/재질] + [동작] + [공간감] + [성격(무게·밝기·길이)]
  예) "heavy leather football kicked hard, short thump, outdoor stadium, no crowd"

## 3. 출력 특성
- [문서] Text to Sound·Variation 결과는 항상 10초(짧은 소리면 뒤가 무음/잔향) → trim_silence 필수.
- [문서] Variation 결과는 모노. Mono to Stereo 결과는 스테레오.
- [문서] Looping 결과는 입력보다 짧아짐(정상).
- [문서] 모든 출력 44.1kHz 16bit WAV.
- [문서] 시드 없음: 같은 프롬프트도 매번 다름 → 후보를 여러 개 뽑아 비교하는 게 기본 전략.

## 4. 파라미터가 소리를 바꾸는 방향
- [문서] Variation strength(0~3, 기본 1): 높을수록 원본과 차이가 커짐. 원본 느낌 유지는 낮게.
- [문서] Variation include(begin,end): 지정한 구간만 새로 만들고 나머지는 원본 유지(튀는 잡음 수정 등).
- [문서] Looping preserve(begin,end): 루프 처리 중 잘리면 안 되는 구간.
- [문서] Conversion ratio(0~2, 기본 1): 0에 가까우면 원본 음색, 클수록 참조 스타일이 강함.
  1.2 이상이면 참조 음색과도 멀어질 수 있음. enhance=true 면 소스 잡음 제거 후 변환.
- [문서] text2sound version: v1(기본)·v2 존재. 차이는 문서에 없음.

## 5. 게임 효과음 종류별 출발점
(아직 실측 전 — 스모크 테스트 후 [실측] 항목으로 채움)

## 6. 흔한 실패와 고치는 법
- 결과가 10초인데 소리는 앞 1초뿐 → trim_silence 후 fade(out 20~50ms)로 끝 클릭 방지.
- 소리가 너무 밝다/가볍다 → 프롬프트에 무게·재질(heavy, deep, low thud)을 넣고 재생성,
  compare_audio target 에 spectral_centroid_hz·low_ratio 를 넣어 고르기.
- 변형이 원본과 너무 다르다 → strength 를 0.3~0.6 으로 낮추기.
- 입력 길이 오류(0.5~10초) → trim_silence 로 무음 제거, 또는 필요한 부분만 잘라서 다시.
"""
