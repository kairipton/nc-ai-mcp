# nc-ai-mcp

NC AI API Platform(platform.nc-ai.com) 서비스를 감싼 MCP 서버 모음. 사용자는 Claude(Claude Code 세션)이며,
모든 게임 프로젝트(유니티·웹게임)에서 공용으로 쓴다(사용자 범위 등록).

| 서버 | 폴더 | 상태 |
|---|---|---|
| `varco-sound` — 효과음 생성·변형·루프·분석·후처리 | `varco_sound/` | 사용 가능 |
| 3D 에셋 생성 | (예정) | — |

## 구조

```text
nc_ai_common/        모든 서버 공통: 설정(.env·config.toml), HTTP(재시도·오류 변환·키 마스킹), 오류 형식, manifest 기록
varco_sound/         VARCO Sound 서버 (server.py 가 진입점)
tests/common/        공통 코드 테스트
tests/varco_sound/   사운드 서버 테스트
output/              생성 결과 기본 폴더(git 제외)
```

새 서버를 추가할 때: `<서비스>/server.py` + 서비스 전용 모듈을 만들고, HTTP·설정·오류·기록은 `nc_ai_common` 을 쓴다.

## 설치

```bash
cd ~/mcp/nc-ai-mcp
uv sync                      # Python 3.12 가상환경(.venv) + 의존성
cp .env.example .env         # VARCO_API_KEY=<키> 입력 (git 제외)
uv run pytest                # 단위 테스트(API 모킹, 크레딧 0)
```

## 등록 (사용자 범위 — 모든 프로젝트에서 보임)

```bash
claude mcp add --scope user varco-sound -- "C:/Users/52487/mcp/nc-ai-mcp/.venv/Scripts/python.exe" "C:/Users/52487/mcp/nc-ai-mcp/varco_sound/server.py"
```

키는 `-e VARCO_API_KEY=...` 로 넘기지 않고 저장소 루트의 `.env` 에서 읽는다. `-e` 를 쓰면 키가
`~/.claude.json` 에 평문으로 남기 때문이다. 키는 플랫폼 공통 OPENAPI_KEY 라 모든 서버가 같은 값을 쓴다.

## 설정

우선순위: 환경 변수 > `.env` > `config.toml` > 기본값. 예시는 `config.toml.example`.

| 항목 | 환경 변수 | config.toml | 기본값 |
|---|---|---|---|
| API 키 | `VARCO_API_KEY` (env/.env 만) | — | 없음 |
| API 호스트 | `VARCO_BASE_URL` | `[api] base_url` | `https://openapi.ai.nc.com` |
| 요청 제한 시간(초) | `VARCO_TIMEOUT` | `[api] timeout_s` | 180 |
| 재시도 횟수 | `VARCO_MAX_RETRIES` | `[api] max_retries` | 3 |
| 사운드 후보 저장 폴더 | `VARCO_OUTPUT_DIR` | `[varco_sound] output_dir` | `./output/varco_sound` |
| 변환 기본 포맷 | `VARCO_DEFAULT_FORMAT` | `[varco_sound] default_format` | `wav` |
| 후보 수 상한 | `VARCO_MAX_CANDIDATES` | `[varco_sound] max_candidates` | 12 |

---

## varco-sound

### 도구

| 분류 | 도구 | API 엔드포인트 | 크레딧(추정) |
|---|---|---|---|
| 생성 | `generate_sfx` | text2sound | 호출당 ~5.4 + 샘플당 ~0.77 |
| 생성 | `generate_from_sample` | variation | 호출당 ~5.4 (5개까지) |
| 생성 | `make_loop` | looping | ~7.7 |
| 생성 | `mono_to_stereo` | mono2stereo | ~1.5 |
| 생성 | `creature_voice` | conversion | ~3.8 |
| 생성 | `enhance_speech` | enhance | ~3.8 |
| 분석 | `analyze_audio`, `compare_audio` | 로컬 | 0 |
| 후처리 | `trim_silence`, `normalize`, `fade`, `convert` | 로컬 | 0 |
| 관리 | `check_credits`, `estimate_cost`, `list_history` | 로컬 | 0 |
| 확인 | `build_audition_page` | 로컬 HTML | 0 |
| 가이드 | `get_guide`, resource `varco://guide` | — | 0 |

크레딧은 문서 단가 기준 추정치(1크레딧=$0.01). 호출당 최소 차감량 때문에 실제와 다를 수 있다.

### API 에 없어서 만들지 않은 것
- `regenerate`(시드 재현): API 가 시드를 지원하지 않는다.
- `get_job_status` / `get_job_result`: 모든 API 가 동기 응답이다.
- 크레딧 잔액 조회: 공개 API 없음 → `check_credits` 는 세션 추정치만.
- 음악·이미지 입력·프롬프트 개선: 플러그인 전용(공개 API 없음). TTS 는 별도 Voice API(범위 밖).

### 예시 (Claude 가 호출하는 순서)

```text
list_history(query="kick")
generate_sfx(prompt="heavy leather football kicked hard, short punchy thump", n=3, purpose="sfx", description="kick_heavy", note="auto-soccer 슈팅")
trim_silence(path=".../sfx_kick_heavy_20261028_01.wav")          # 후보마다
compare_audio(paths=[...], goal="짧고 묵직한 킥", target={"effective_duration_s":0.4, "spectral_centroid_hz":700})
generate_from_sample(source=".../sfx_kick_heavy_20261028_02_trim.wav", n=3, strength=0.5)
normalize(path=..., mode="lufs", target=-18)
convert(path=..., output_path="C:/Works/auto-soccer/public/audio/kick_01.ogg")
build_audition_page(paths=[...])
```

### 기록
- 모든 생성·후처리는 결과 파일 폴더의 `manifest.json` 에 덧붙인다: 서비스명, 도구, 전체 파라미터, 시드(null),
  부모(원본 manifest id 또는 경로), 파일 경로, 추정 크레딧, 생성 시각, 사용 목적 메모.
- 파일 이름: `{용도}_{짧은설명}_{YYYYMMDD}_{번호}.wav` (예: `sfx_kick_heavy_20261028_01.wav`).
  후처리 결과는 `원본이름_trim.wav`, `_norm`, `_fade`, `_conv`.

### 모듈

| 파일 | 책임 |
|---|---|
| `varco_sound/server.py` | MCP 도구 정의·설명, 오류를 JSON 으로 변환 |
| `varco_sound/service.py` | 도구 로직(검증 → 호출 → 저장 → 기록) |
| `varco_sound/client.py` | 공통 HTTP 위에서 base64 오디오 응답 해석 |
| `varco_sound/api_spec.py` | 문서상 경로·범위·단가(문서 바뀌면 여기만) |
| `varco_sound/settings.py` | 사운드 전용 설정 |
| `varco_sound/analysis.py` | 분석 지표·해석·순위 |
| `varco_sound/processing.py` | 무음 제거·노멀라이즈·페이드·변환 |
| `varco_sound/audio_io.py` | 오디오 읽기·쓰기·API 입력 인코딩 |
| `varco_sound/audition.py` | 청취 비교 HTML |
| `varco_sound/guide.py` | 프롬프트 가이드 |

사용 스킬: `~/.claude/skills/varco-sound/SKILL.md` (저장소 밖, 사용자 범위)
