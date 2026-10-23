# 간병사 일정 확인 AICC

간병사에게 전화를 걸어 현재 근무 여부·근무 종료일·다음 근무 가능일을 확인하고 엑셀 명부를 갱신하는 내부 업무용 AICC입니다.
병원 배정이나 고용계약을 대신하지 않습니다. 설계 기준은 [`docs/design/AICC_개발설계_12종_통합본.md`](docs/design/AICC_개발설계_12종_통합본.md),
구현 현황은 [`docs/IMPLEMENTATION_STATUS.md`](docs/IMPLEMENTATION_STATUS.md)에 있습니다.

> **현재 상태**: 명부 업로드부터 결과 다운로드까지 웹·서버·에이전트가 동작하며, 통화 흐름은 **MOCK 단말**(실제 전화를 걸지 않는 가상 상대)로 검증했습니다.
> 갤럭시 A24 USB 실통화(RX/TX·응답감지·PC 무음·녹음)는 **미검증**이며, 검증 기록이 없으면 실제 발신은 서버에서 잠겨 있습니다.
> 절차: [`docs/A24_실기기_검증절차.md`](docs/A24_실기기_검증절차.md)

## 구조
```
apps/web           React + TypeScript 관리화면(S01–S10)
apps/agent         PC 에이전트·통합 실행기(aicc)·진단(aicc-probe)·MOCK 시뮬레이터(aicc-simulate)
services/api       FastAPI 서버, SQLAlchemy 모델, Alembic 마이그레이션
packages/core      공통 로직: 전화번호, 한국어 날짜·의도 해석, 통화 상태기계, 음원팩 규격
packages/contracts OpenAPI 명세, 통화 이벤트 규약
tests              단위·API·MOCK 종단 시험, 합성 엑셀 픽스처
docs               설계 원문, 구현 추적표, 실기기 검증 절차
infra              선택: PostgreSQL
```

## 처음 설치부터 실행까지

필요: Python 3.11 이상, Node.js 20 이상.

**Windows (PowerShell)**
```powershell
git clone https://github.com/erionnnnn/new-aicc.git
cd new-aicc
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1   # 가상환경·의존성·웹 빌드·바탕화면 AICC 아이콘
```
이후에는 바탕화면 **AICC** 아이콘 하나만 실행합니다(서버·웹·에이전트를 함께 띄우고 브라우저를 엽니다).

**macOS / Linux**
```bash
./scripts/setup.sh
.venv/bin/aicc            # http://127.0.0.1:8765
```

### 첫 실행(시험 조직으로 흐름 확인)
1. 브라우저에서 조직을 만듭니다 — **시험 조직(MOCK 통화만)** 선택
2. 설정 > 운영 정책에서 회사명 입력, 설정 > 안내 음성 > 기본 음원팩 만들기
3. 설정 > 단말 > MOCK 단말 등록(‘이 PC 에이전트에 바로 연결’ 체크)
4. 명부 업로드 → 근무확정일 의미 선택 → 검사 → 저장 → 대상 선택 → 통화 준비 → 시작
5. 통화 진행 화면에서 MOCK 통화를 보고, 결과 검수·결과 다운로드

MOCK 상대는 합성 번호의 끝자리로 정해집니다(`aicc-simulate --list-personas`). 예시 명부: `tests/fixtures/clean_roster.xlsx`.

## 시험
```bash
.venv/bin/python tests/fixtures/make_fixtures.py
.venv/bin/pytest            # 91 passed (2026-10-24)
.venv/bin/ruff check .
(cd apps/web && npm run build)
```

## 핵심 규칙(코드로 강제)
- 날짜는 재확인 질문에 대한 **명시적 동의 발화**가 같은 턴에 있을 때만 반영합니다. 서버가 근거 발화를 다시 판정하며, AI나 에이전트가 보낸 날짜 값만으로는 저장하지 않습니다.
- 연락거부는 어느 단계에서든 즉시 저장되고, 업로드·캠페인 생성·발신 직전에 모두 대조합니다(조직 HMAC, 재업로드에도 유지).
- 한 단말은 한 통화만. DB가 종료를 기록해도 단말 IDLE 확인 전에는 다음 발신권을 주지 않습니다. 장치 장애 뒤에는 캠페인이 일시정지되고 자동 재발신하지 않습니다.
- ‘USB 연결됨’과 ‘실통화 준비됨’을 구분합니다. 실통화는 사람이 원격 단말로 10회 이상 확인한 기록이 있어야 열립니다.
- 시험 조직(MOCK)과 운영 조직(실기기)을 분리해 시험 결과가 실제 명부로 오인되지 않게 합니다.

## 보안·개인정보
- 전화번호·원본 행·업로드 원본·녹음·내보내기 파일은 암호화 저장(Fernet), 연락거부 대조는 조직 범위 HMAC.
- 비밀값은 `.aicc/secrets.env`(개발 자동 생성) 또는 환경변수. 운영에서는 직접 지정해야 기동합니다. `.aicc/`는 커밋되지 않습니다.
- STT·Gemini 키는 PC 에이전트 환경변수로만 사용하며 브라우저·저장소에 두지 않습니다.
- 녹음 안내·고지, 보관 기간, 통화 허용 시간, AI 외부 전송 범위는 **운영 담당자가 확정**해야 하며, 미설정이면 실제 발신이 잠깁니다. 이 저장소는 법률 적합성을 판단하지 않습니다.

## 설정
환경변수 목록은 [`.env.example`](.env.example). 실기기용 선택 의존성: `pip install -e ".[voice]"`(faster-whisper, sounddevice, google-genai), PostgreSQL: `pip install -e ".[postgres]"`.
