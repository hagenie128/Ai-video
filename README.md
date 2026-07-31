# AI Shorts Maker

상품 사진과 간단한 정보만 입력하면 **한국어 쇼츠 대본 → 장면 구성 → AI 영상 → TTS → 자동 편집 →
자막·BGM·효과음 → 1080x1920 MP4 렌더링**까지 자동으로 처리하는 Windows 로컬 웹앱입니다.

- 사용자가 최종 감독자입니다. 자동 생성 후에도 **대본, 장면, 프롬프트, 미디어 순서, 자막**을 모두 수정할 수 있습니다.
- 외부 서비스(LLM / TTS / Higgsfield)가 없거나 실패해도 **수동 방식으로 자동 대체(Plan B)** 됩니다.
- Docker / DB / 로그인 / 결제 없음. 데이터는 전부 로컬 JSON + 파일입니다.
- API 키는 `project.json`에 저장하지 않습니다. `.env` 또는 Windows 환경변수에만 저장하고 화면에는 마스킹해서 보여줍니다.

출력 규격: **1080x1920 / 30fps / H.264 / yuv420p / AAC / faststart**

---

## 1. 설치

### 1) Python 3.11 이상

- https://www.python.org/downloads/windows/ (설치 화면에서 **"Add python.exe to PATH"** 체크 필수)
- 또는 PowerShell에서:

```
winget install --id Python.Python.3.12 -e
```

### 2) FFmpeg (필수)

렌더링 엔진입니다. 아래 셋 중 하나로 설치합니다.

```
winget install --id Gyan.FFmpeg -e
```

- 또는 https://www.gyan.dev/ffmpeg/builds/ 에서 **full build** zip을 받아 `C:\ffmpeg`에 풀고
  `C:\ffmpeg\bin`을 PATH 환경변수에 추가
- 또는 `ffmpeg.exe`, `ffprobe.exe`를 이 프로젝트의 `bin\` 폴더에 복사

> 설치 직후에는 **새 터미널 창**을 열어야 PATH가 반영됩니다.
> 앱 사이드바에 `FFmpeg 사용 가능 ✅`이 뜨면 정상입니다.

### 3) Node.js (선택 — Higgsfield AI 영상 생성용)

- https://nodejs.org 에서 LTS 설치. AI 영상을 직접 만들지 않고 수동 업로드만 쓸 거면 필요 없습니다.

---

## 2. 실행

프로젝트 폴더의 `run.bat`을 더블클릭합니다.

`run.bat`이 하는 일:

1. Python 3.11+ 확인
2. FFmpeg 확인 (없으면 설치 안내)
3. `.venv` 가상환경 생성 + `requirements.txt` 설치 (첫 실행만, 몇 분 소요)
4. Higgsfield CLI 설치 여부 안내 (선택)
5. `http://localhost:8501` 로 Streamlit 실행 + 브라우저 자동 열기

수동으로 실행하려면:

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m streamlit run app.py
```

종료는 실행된 검은 창에서 `Ctrl + C`입니다.

---

## 3. 화면 구성

| 화면 | 하는 일 |
|---|---|
| **0. AI 자동 제작** | 상품 정보 입력 → 전체 자동 생성 → 대본/장면/AI영상/초안/자막 검수 |
| 1. 프로젝트 설정 | 프리셋, 목표 길이, 영상 톤 |
| 2. 대본과 음성 | 평문 대본, 음성 업로드, 자막 소스 방식 |
| 3. 미디어 업로드 | 사진·영상·BGM·효과음 업로드 |
| 4. 타임라인 | 컷 순서·길이·효과·관심영역·자막 세부 조정 |
| 5. 자막 설정 | 폰트·크기·색·위치, SRT/ASS 내보내기 |
| 6. 오디오 설정 | 음성/BGM/효과음 볼륨, 덕킹, 페이드, 마지막 블랙 |
| 7. 출력 | 미리보기 / 최종 렌더링, 프로젝트 JSON |
| **8. AI 연동 설정** | LLM · 한국어 TTS · Higgsfield 연동과 크레딧 예산 |
| **9. 결과 검수** | 렌더 전 자동 점검, 렌더 후 ffprobe 검사 + 주요 프레임 |
| **10. 사용량 통계** | 월별 생성 횟수·크레딧·실패·재생성 집계 |

---

## 4. LLM API 설정 (대본 생성)

`8. AI 연동 설정` → `LLM (대본 생성)` 탭

1. 공급자 선택: **Anthropic / OpenAI / Gemini / 로컬(사용 안 함)**
2. 모델 선택 또는 직접 입력
3. API 키 입력 → `저장` (앱 폴더의 `.env`에 저장됩니다. `.env`는 git에 올라가지 않습니다)
4. `연결 테스트`로 확인

환경변수 이름:

| 공급자 | 환경변수 |
|---|---|
| Anthropic | `ANTHROPIC_API_KEY` |
| OpenAI | `OPENAI_API_KEY` |
| Gemini | `GEMINI_API_KEY` |
| ElevenLabs (TTS) | `ELEVENLABS_API_KEY` |

Windows 환경변수로 등록해도 됩니다(환경변수가 `.env`보다 우선). `.env.example`을 복사해 `.env`로 쓰면 됩니다.

### LLM 키가 없으면 (Plan B)

키가 없어도 앱은 정상 동작합니다. 이때 대본은 **입력한 자료만 재구성하는 규칙 기반 생성기**가 만듭니다.

- 콘텐츠 유형 9종별 후킹/구성/마무리 템플릿 사용
- 없는 수치나 사실을 만들지 않음 (입력한 '강조할 사실' / '상품 설명' 안에서만)
- 첫 3초 3컷 보장, 자막 12~16자 분할, 금지 표현 자동 제거

생성 후 `장면별 편집`에서 문장을 직접 다듬으면 됩니다.

### 비전 분석

LLM이 Anthropic / OpenAI / Gemini 중 하나로 설정되어 있으면 `자료 분석 실행` 시 사진 내용까지 분석합니다
(설명, 태그, 사람/손/로고 포함 여부, 관심 영역, 유사 이미지 그룹, 품질 점수).
없으면 해상도·비율·밝기·파일명·dHash 유사도·업로드 순서로 분석합니다.

---

## 5. 한국어 TTS

`8. AI 연동 설정` → `한국어 TTS` 탭

### 기본값: Edge TTS (무료, 키 불필요)

```
pip install edge-tts
```

`run.bat`이 자동으로 설치합니다. 음성 목록은 `사용 가능한 한국어 음성 조회`로 실제 조회합니다.

음성 성격 프리셋: **차분한 여성 / 차분한 남성 / 빠른 정보형 / 낮고 건조한 경고형**
조절 항목: 속도, 피치, 볼륨, 감정 강도(ElevenLabs 전용)

### 다른 공급자

- **ElevenLabs**: `ELEVENLABS_API_KEY` 입력 후 `음성 목록 조회`
- **OpenAI TTS**: `OPENAI_API_KEY` 입력, 음성 6종
- **직접 음성 업로드**: TTS를 쓰지 않고 직접 녹음한 파일 사용

### 동작 방식

장면별로 따로 합성해서 이어 붙입니다. 그래서 **장면별 실제 발화 길이**를 알 수 있고,
그 길이가 그대로 컷 길이와 자막 타이밍이 됩니다(싱크가 어긋나지 않습니다).

합성 후 `ffprobe`로 실제 길이를 재고, 목표 길이와 차이가 크면 대본 줄이기/속도 조절을 안내합니다.

`합성 테스트`가 실패하면 공급자를 `직접 음성 업로드`로 바꾸고 `0. AI 자동 제작` → 자료 섹션에서
음성 파일을 올리면 나머지 단계는 그대로 진행됩니다.

---

## 6. Higgsfield (AI 영상)

이 앱은 **공식 CLI만** 사용합니다. 공개 REST API를 추정해 호출하지 않습니다.
CLI 문법도 추측하지 않고, 설치된 CLI의 `--help` 출력에서 확인된 명령/옵션만 사용합니다.

### 설치

```
npm i -g @higgsfield/cli
```

### 로그인 (앱 안에서 가능)

`8. AI 연동 설정` → `Higgsfield` 탭 → **`브라우저로 로그인`** 버튼.
앱이 사용자 PC에서 실행되므로 버튼을 누르면 브라우저 OAuth 창이 열립니다.

터미널에서 직접 해도 됩니다:

```
higgsfield auth login
```

### 워크스페이스

`No workspace selected` 오류가 나면 같은 화면의 **`워크스페이스 조회`** → 선택 →
**`이 워크스페이스로 설정`** 을 누르세요. 터미널 방식은:

```
higgsfield workspace list
higgsfield workspace set <workspace_id>
```

### 선택: 공식 스킬

```
npx skills add higgsfield-ai/skills
```

### 앱이 사용하는 실제 명령

앱은 아래 명령을 `--help`로 확인한 뒤에만 실행합니다.

```
higgsfield model list --video --json        # 사용 가능한 모델(job_type) 조회
higgsfield model get <job_type> --json      # 이 모델이 받는 파라미터 확인
higgsfield generate cost <job_type> ...     # 실제 예상 크레딧
higgsfield generate create <job_type> --prompt "..." --wait
higgsfield generate wait <job_id>           # (--wait 를 못 쓸 때)
higgsfield account status                   # 잔여 크레딧
```

모델은 옵션이 아니라 **위치 인자(job_type)** 입니다. `duration`, `aspect_ratio` 같은 값은 모델마다
달라서 `model get`으로 확인된 파라미터만 전달합니다. 필요한 명령이 확인되지 않으면
**수동 업로드 모드로 자동 전환**되고, 브라우저 자동화 같은 우회는 하지 않습니다.

### Claude Higgsfield MCP 연결

이 앱은 MCP를 직접 호출하지 않습니다. MCP는 Claude 쪽에서 쓰는 **별도 워크플로**입니다.

1. Claude 설정 → **Connectors** → **Custom connector** 추가
2. URL 입력:

```
https://mcp.higgsfield.ai/mcp
```

3. 또는 Claude Code에서 Higgsfield CLI를 직접 사용

MCP나 웹에서 만든 영상은 앱의 각 장면 **수동 업로드**에 올리면 그 장면에 자동 연결됩니다.

> ⚠️ 웹에서만 제공되는 무료/무제한 모델은 CLI/MCP에서 쓸 수 없는 경우가 있습니다.
> 그럴 때는 웹에서 만든 영상을 수동 업로드로 등록하세요.

### 장면 선별 원칙

전체 영상을 AI로 만들지 않습니다. **실제 사진으로 표현하기 어려운 장면만** 생성합니다.

- AI 영상: 첫 후킹 움직임, 제조/가공 과정, 추상적 설명, 강한 엔딩
- 실제 사진 우선: 제품 비교, 디테일 증거, 로고/각인 비교, 제품 전체 모습, 사용자 제공 근거
- 기본 2~4개, 사용자가 최대 개수 지정, 장면별 최대 시도 2회(기본)
- 생성 기본값: 9:16, 3~6초, 오디오 Off, **자막·로고·읽을 수 있는 글자 생성 금지**

모델 선택 규칙과 크레딧 추정치는 `higgsfield_models.json`에서 바꿉니다(코드 수정 불필요).
실제 사용 가능한 모델은 `model list` 조회 결과와 이 파일의 별칭을 대조해 정합니다.

### 크레딧 관리

`8. AI 연동 설정` → `Higgsfield` 탭

| 항목 | 설명 |
|---|---|
| 월 크레딧 예산 | 이 달 총 상한. 넘으면 생성이 차단됩니다 |
| 영상 1편당 최대 크레딧 | 이 값을 넘으면 **자동 승인 설정이어도 반드시 사용자 승인** |
| 사용 승인 방식 | `매번 승인` 또는 `설정 예산 이하면 자동 승인` |
| 장면별 최대 시도 횟수 | 같은 장면 무제한 재생성 방지 (기본 2회) |
| 생성 타임아웃 | 기본 900초 |

장면별 최대 시도 횟수에 도달하면 장면 카드에 **마지막 실패 사유**가 표시되고, 세 가지 중
하나를 고르게 됩니다: **수동 업로드** / **사진으로 대체** / **시도 횟수 초기화**
(로그인·설치·예산 등 원인을 고친 뒤에만 쓰세요. 이미 쓴 크레딧은 환불되지 않습니다).

- 생성 전에 장면명·프롬프트·모델·길이·예상 크레딧·참고 이미지를 보여주고 승인을 받습니다.
- `실제 견적 조회`를 누르면 `generate cost`로 실제 크레딧을 조회합니다.
- 모든 생성 기록(날짜, 장면 ID, 모델, 길이, 프롬프트, 예상/실제 크레딧, 작업 ID, 결과 파일,
  시도 횟수, 상태)은 `project.json`에 저장되고 `10. 사용량 통계`에서 월별로 집계됩니다.
- 액세스 토큰은 로그·화면·`project.json`에 남기지 않습니다(JWT 패턴 자동 마스킹).

---

## 7. 자동 제작 사용법

### 기본 흐름

1. 사이드바에서 **새 프로젝트** 생성
2. `0. AI 자동 제작` 화면으로 이동
3. **기본 정보** 입력
   - 업종/카테고리, 상품명, 상품 설명, 핵심 장점, **강조할 사실**, 타깃, 분위기, 금지 표현
   - 브랜드명 노출 여부, 목표 길이(15/20/30/45/60초), 플랫폼, 콘텐츠 유형
   - 구체적 수치는 '강조할 사실'과 '상품 설명'에 적은 값만 대본에 쓰입니다
4. **자료** 업로드: 상품 사진 여러 장, 기존 영상, (선택) 참고 대본/링크/로고/금지 이미지
5. **생성 옵션** 확인 후 **🚀 AI 쇼츠 전체 생성** 클릭
6. 9단계 진행 상황을 보고, 결과를 검수·수정
7. `9. 결과 검수`에서 점검 → 최종 렌더 → 다운로드

### 9단계

```
1/9 자료 분석          사진 분석, 역할·태그·관심영역 지정
2/9 대본 생성          구조화 JSON (장면별 나레이션/자막/화면 종류)
3/9 장면 구성          장면 ↔ 실제 자료 매칭, AI 영상 필요 장면 선별
4/9 Higgsfield 프롬프트 장면별 영어 프롬프트 (로고·문자 금지 포함)
5/9 AI 영상 생성        승인 규칙과 예산 확인 후 생성 (기본 꺼짐)
6/9 TTS 생성           장면별 합성 → 이어 붙이기 → 장면 타이밍 확정
7/9 자동 편집          첫 컷 hook, 첫 3초 3컷, 사진/영상 교차, 하드컷
8/9 자막·오디오 합성    TTS 기준 자동 자막, BGM·효과음 자동 선택, 덕킹
9/9 최종 렌더링        1080x1920 / 30fps / H.264 / AAC
```

**한 단계가 실패해도 전체가 멈추지 않습니다.** 가능한 단계는 계속 진행하고, 실패한 단계는
`✋ 수동 작업 필요`로 표시되며 **`이 단계부터 재시도`** 버튼이 나옵니다.

### 자동 배치 규칙

- 첫 컷은 hook, 첫 3초 안에 최소 3컷(최대 5컷)
- 첫 프레임부터 화면 표시 (시작/종료 페이드 기본 0)
- 하드컷 중심, 사진과 영상 교차, 같은 타입 3개 이상 연속 회피
- 유사 이미지 연속 사용 방지, **모든 자료를 무조건 쓰지 않음**
- 쓰지 않은 자료는 삭제하지 않고 `사용 안 함(unused)`으로 남습니다
- ending은 마지막, 마지막 0.8초 블랙
- 음성이 있으면 장면별 실제 발화 길이가 컷 길이

역할별 기본 길이: hook 0.7~1.5 / evidence 0.7~1.2 / comparison 0.6~1.0 / detail 0.6~1.0 /
process 1.3~2.5 / explanation 1.0~1.8 / ending 1.2~2.2초

### 수정 후 재렌더

- 대본: `전체 대본` 편집 → `장면에 재배분`, 또는 `장면별 편집`
- 후킹만 다시: `후킹만 재생성`
- 길이: `길이 줄이기` / `길이 늘리기`
- 말투: `말투 변경`
- 점검: `사실성 점검`(제공 자료에 없는 수치·단정 표현 확인), `금지 표현 점검`
- 순서/길이/효과/관심영역: `4. 타임라인`
- 자막: `자막 미리보기`에서 9:16 프레임 위에서 위치 확인 후 조정
- 다시 렌더: `9. 결과 검수` 또는 `7. 출력`

### 관심 영역 (크롭 기준점)

각 컷마다 `center / top / bottom / left / right / custom`을 고를 수 있고, `custom`은 X/Y 비율
슬라이더로 지정합니다. zoompan과 crop이 그 위치를 기준으로 동작하며, 이미지 경계 밖으로 나가지 않습니다.

사진 효과: `none / slow_zoom_in(최대 1.06) / fast_zoom_in(최대 1.12) / slow_zoom_out /
pan_left_to_right / pan_right_to_left / subtle_shake`

---

## 8. BGM과 효과음

**저작권이 불명확한 음원을 자동으로 내려받지 않습니다.** 사용 권한이 있는 파일만 쓰세요.

폴더에 넣어 두면 자동 추천됩니다.

```
assets/
  music/    ← BGM
  sfx/      ← 효과음
```

파일명에 태그 단어를 넣으면 자동 인식됩니다.

- BGM 태그: `dark, warning, fast, neutral, emotional, luxury, playful`
  (예: `dark_tension_loop.mp3`, `luxury_cinematic.mp3`)
- 효과음 태그: `impact, whoosh, click, metal, cut, glitch, bass_drop`
  (예: `impact_hit.wav`, `whoosh_transition.wav`)

콘텐츠 유형과 장면 역할로 자동 선택하고, 음성 구간에는 BGM을 자동으로 줄입니다(sidechaincompress 덕킹).
첫 후킹 효과음은 음성보다 크지 않게 볼륨을 제한합니다. **BGM이 없어도 렌더는 정상 동작합니다.**

---

## 9. 프리셋

| 프리셋 | 특징 |
|---|---|
| `dark_warning` | 경고형 다크톤. 채도 0.78 / 대비 1.12 / 그레인 4 / BGM 0.12 / 자막 74px |
| `clean_info` | 밝고 깨끗한 정보형. 빠른 하드컷 |
| `fast_comparison` | 아주 짧은 컷, 좌우 팬으로 비교 강조, 첫 3초 4컷 |
| `product_demo` | 제품 소개/사용법. 느린 확대, 고급 톤 |
| `emotional_story` | 스토리텔링/후기. 긴 호흡, 따뜻한 톤 |

모두 `presets/*.json` 파일이라 직접 수정할 수 있습니다.
공통: 1080x1920 / 30fps / 시작·종료 페이드 0 / 마지막 블랙 0.8초 / 하드컷 전용.

---

## 10. 결과 검수

`9. 결과 검수` 화면.

### 렌더 전 자동 점검

첫 프레임 검정 여부, 첫 3초 컷 수, 첫 컷 역할, 음성 유무와 길이 차이, 자막 누락/길이/위치,
영상 길이, 마지막 블랙, 해상도, FPS, 중복·유사 이미지 연속 사용, 타입 교차, 과도하게 긴 정지컷,
글자 많은 사진 노출 시간, BGM 음량과 덕킹, 효과음 위치·음량, 파일 누락/손상,
Higgsfield 생성 실패 장면 → **통과 / 경고 / 실패**

실패 항목이 있으면 최종 렌더 버튼이 잠깁니다. 고친 뒤 다시 점검하세요.

### 렌더 후 검사

`ffprobe`로 해상도·코덱·픽셀 포맷·FPS·오디오 스트림·길이·컨테이너를 확인하고,
`volumedetect`로 클리핑을 검사합니다.
주요 시점 프레임(0.0초 / 1.0초 / 3.0초 / 중간 / 종료 직전)을 뽑아 썸네일로 보여주므로
첫 프레임이 검정인지, 중간에 빈 화면이 있는지 눈으로 바로 확인할 수 있습니다.

---

## 11. 권리와 안전

- 업로드한 이미지·영상·음악·상표 자료의 **사용 권한은 사용자 책임**입니다.
- AI 생성 결과는 세부 형상과 글자가 원본과 달라질 수 있습니다.
- 사실성 콘텐츠는 제공한 자료에 근거해 **직접 검수**해야 합니다.
- 이 앱은 **근거 없는 수치나 허위 단정을 자동 생성하지 않습니다.** 수치는 입력한 자료 안에서만 사용하고,
  `사실성 점검`이 자료에 없는 숫자와 단정 표현을 찾아 줍니다.
- 브랜드 로고와 읽을 수 있는 텍스트를 AI 영상에 생성하지 않는 것이 **기본값**입니다
  (프롬프트에 `no logo`, `no readable letters` 자동 포함). 설정에서만 바꿀 수 있습니다.
- 레퍼런스 이미지는 **구조와 소재만** 참고하도록 프롬프트에 명시됩니다.
- 원본 비교 자료의 텍스트는 사용자가 검수해야 합니다.

---

## 12. 오류 해결

### FFmpeg 없음
사이드바에 `FFmpeg 없음`이 뜨면 2번 항목대로 설치하고 **새 창**에서 `run.bat`을 다시 실행하세요.
`ffmpeg.exe`/`ffprobe.exe`를 `bin\` 폴더에 복사하는 방법이 가장 확실합니다.

### 대본이 이상하거나 딱딱하다
- LLM 키가 없으면 규칙 기반 생성입니다. 키를 넣으면 품질이 크게 올라갑니다.
- `말투 변경`, `후킹만 재생성`, `장면별 편집`으로 직접 다듬으세요.

### 대본에 없는 숫자가 들어갔다
`사실성 점검`을 누르면 제공 자료에 없는 수치를 찾아 줍니다. 해당 장면을 직접 수정하세요.

### TTS 합성 실패
- `pip install edge-tts` 확인
- 사내망/방화벽 환경에서 막힐 수 있습니다. 이때는 공급자를 `직접 음성 업로드`로 바꾸고
  음성 파일을 올리면 나머지 단계는 그대로 진행됩니다.

### Higgsfield CLI 관련
| 증상 | 해결 |
|---|---|
| `CLI 없음` | `npm i -g @higgsfield/cli` (Node.js 필요) |
| `Not authenticated` / `로그인 안 됨` | `higgsfield auth login` |
| `No workspace selected` | `higgsfield workspace set <workspace_id>` |
| `모델 조회 실패` | 로그인 확인. 실패하면 `higgsfield_models.json` 후보 모델을 사용합니다 |
| `영상 생성 명령을 확인하지 못했습니다` | CLI 버전이 다릅니다. 수동 업로드를 쓰세요 |
| 생성 타임아웃 | 설정에서 타임아웃을 늘리거나, 작업 ID로 `higgsfield generate get <id>` 확인 후 수동 업로드 |
| 예산 초과 | 월 예산/영상당 한도를 조정하거나 AI 영상 수를 줄이세요 |

로그: `logs/higgsfield.log` (민감값 마스킹됨)

### 렌더링 실패
- 오류 메시지와 실행된 FFmpeg 명령이 화면에 그대로 표시됩니다.
- 로그: `projects/<프로젝트명>/logs/render_*.log`
- 자주 있는 원인: 손상된 미디어 파일(`자료 분석`이 찾아 줍니다), 다른 프로그램이 파일을 열고 있음,
  자막 폰트 이름 오류(`맑은 고딕` / `Malgun Gothic` 으로 변경)

### 첫 프레임이 검정으로 나온다
`시작 페이드`를 0으로 두고(자동 구성은 0으로 설정합니다), 첫 컷을 밝은 컷으로 바꾸세요.
`9. 결과 검수`가 이 문제를 자동으로 잡아 줍니다.

### 자막이 쇼츠 UI에 가린다
`자막 미리보기`에서 붉은 안전 영역을 보고 `하단 여백`을 250~350 사이로 조정하세요.

### 한글/공백 경로
프로젝트 이름과 파일명에 한글·공백을 써도 됩니다(테스트로 검증됨). 파일명의 `<>:"/\|?*` 문자는
자동으로 `_`로 바뀝니다.

### 프로젝트가 안 열린다
`projects/<이름>/project.json`이 있어야 목록에 나옵니다. 파일이 깨졌으면 백업에서 복원하거나
사이드바의 `프로젝트 JSON 불러오기`로 다시 올리세요. 오래된 JSON은 자동으로 현재 구조에 맞게 보정됩니다.

---

## 13. 파일 구조

```
shorts-maker/
├─ app.py                        Streamlit 앱, 화면 라우팅
├─ renderer.py                   FFmpeg 렌더러 (정규화→concat→자막·오디오)
├─ timeline.py                   컷 순서·길이 계산, 자동 타임라인
├─ subtitles.py                  자막 분할, SRT/ASS 생성 (cut/script/auto 모드)
├─ audio.py                      오디오 믹싱 필터 그래프 (덕킹 포함)
├─ project_manager.py            프로젝트 JSON 저장/불러오기/마이그레이션
├─ utils.py                      경로, FFmpeg 실행, 미디어 정보
├─ config.py                     .env 비밀값 관리, AI 설정
├─ ai_provider.py                Anthropic / OpenAI / Gemini 공통 인터페이스
├─ script_generator.py           한국어 대본 생성 (LLM + 규칙 기반 Plan B)
├─ media_analyzer.py             사진/영상 분석, 역할·태그·관심영역
├─ scene_planner.py              장면 ↔ 자료 매칭, AI 영상 장면 선별
├─ higgsfield_service.py         Higgsfield CLI 연동 (help 기반 문법 확인)
├─ higgsfield_prompt_builder.py  장면별 영상 프롬프트 생성
├─ higgsfield_models.json        모델 선택 규칙·크레딧 추정치 (설정 파일)
├─ tts_service.py                Edge TTS / ElevenLabs / OpenAI / 업로드
├─ audio_library.py              BGM·효과음 라이브러리와 자동 추천
├─ automation_pipeline.py        9단계 전체 자동화
├─ quality_checker.py            렌더 전 점검 / 렌더 후 검사
├─ ui_auto.py                    0. AI 자동 제작 화면
├─ ui_ai_settings.py             8. AI 연동 설정 화면
├─ ui_higgsfield.py              AI 영상 승인·생성·수동 업로드 섹션
├─ ui_review.py                  9. 결과 검수 / 10. 사용량 통계
├─ presets/                      dark_warning, clean_info, fast_comparison,
│                                product_demo, emotional_story
├─ assets/music/, assets/sfx/    사용자 소유 음원 라이브러리
├─ projects/                     프로젝트별 데이터와 미디어
├─ outputs/                      렌더링 결과 MP4
├─ temp/, logs/                  작업 폴더, 로그
├─ tests/                        회귀 테스트 (아래 참고)
├─ .env.example                  API 키 템플릿
├─ requirements.txt
├─ run.bat
└─ README.md
```

`project.json`에 저장되는 것: 컷, 자막/오디오/영상 설정, `ai_brief`, `script_data`, `scene_plan`,
`media_analysis`, `tts`, `auto_cues`, `higgsfield_generations`, `automation_log`, `credit_usage`.
**API 키와 토큰은 저장되지 않습니다.**

---

## 14. 테스트

```
python tests/test_baseline_render.py    기존 렌더링 회귀 (프로젝트 생성~최종 MP4)
python tests/test_step1.py              설정/비밀값, LLM JSON 파싱, 대본 생성, TTS
python tests/test_step2.py              미디어 분석, 장면 계획, 자동 타임라인, 관심영역 렌더
python tests/test_step3.py              Higgsfield CLI 문법 확인, 수동 업로드, 크레딧 승인
python tests/test_step4.py              9단계 파이프라인 전체 실행, BGM/SFX, 자동 자막
python tests/test_step5.py              품질 검사(렌더 전/후), 월간 통계
python tests/test_conditions.py         최종 회귀 (사진만/사진+영상/음성 업로드/BGM 없음/
                                        CLI 없음/예산 초과/손상 파일/한글 경로/재불러오기)
python tests/test_ui.py                 모든 화면 렌더 + 주요 버튼 실제 클릭
python tests/smoke_app.py               Streamlit 서버 기동 확인
```

테스트용 더미 사진/영상/음성은 `tests/make_fixtures.py`가 FFmpeg와 Pillow로 만듭니다.
`temp/`, `projects/`, `outputs/` 안에만 파일을 만들며 실제 작업 데이터는 건드리지 않습니다.

---

## 15. 코드 구조 주의사항

**화면 모듈(`ui_*.py`)은 `app.py`를 import 하지 않습니다.**

Streamlit은 `app.py`를 `__main__`으로 실행합니다. 여기서 `from app import ...` 를 하면
파이썬이 `app`이라는 **별개 모듈을 새로 만들어 다시 실행**하고, 그 과정에서 `main()`이 한 번 더
호출되어 사이드바 위젯이 두 번 생성됩니다 → `StreamlitDuplicateElementKey: key='pick_project'`.

그래서 공용 함수는 화면과 무관한 모듈에 둡니다.

- `add_media_files()` → `media_manager.py` (app.py와 ui_auto.py가 각각 import)
- `app.py` 최하단은 `if __name__ == "__main__": main()` 으로 감쌉니다.

`tests/test_no_reentry.py`가 이 규칙을 검사합니다. 새 화면 모듈을 추가할 때 이 테스트의
`UI_MODULES` 목록에도 파일명을 넣어 주세요.
