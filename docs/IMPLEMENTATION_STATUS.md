# 구현 추적표 (IMPLEMENTATION_STATUS)

기준 문서: `docs/design/AICC_개발설계_12종_통합본.md` (2026-10-24)
작성일: 2026-10-24 · 시험 환경: Linux 클라우드 작업공간, Python 3.13, Node 22 · **실기기(갤럭시 A24) 미연결**

판정 기준
- **PASS** — 자동 시험으로 확인함(MOCK 단말 포함). 실통화 증거가 아님.
- **PASS(MOCK)** — 소프트웨어 경로는 MOCK 단말로 검증, 실기기 동작은 미확인.
- **UNKNOWN** — 코드는 있으나 이 환경에서 확인할 수단이 없음(실기기·외부 API·사람 청취 필요).
- **FAIL** — 확인했으나 요구를 충족하지 못함.
- **NOT DONE** — 구현하지 않음.

마지막 실행: `pytest` → 91 passed · `ruff check .` 통과 · `tsc -b` + `vite build` 통과.

## 요구사항 R01–R10

| ID | 요구 | 코드 | 시험 | 판정 |
|---|---|---|---|---|
| R01 | 엑셀 업로드·열 매핑·미리보기 검증 | `services/api/aicc_api/excel_io.py`, `services.py:upload_import/validate_import`, `apps/web/src/pages/Upload.tsx` | `test_t01_*`, `test_t02_*`, `test_date_semantics_required` | PASS |
| R02 | 대상 선택·순차 발신·한 단말 한 통화 | `services.py:create_campaign/claim_next`, `models.Device.active_call_id`(unique), `pages/Targets.tsx` | `test_full_mock_campaign_*`, `test_t15_*`(IDLE 전 발신권 미발급) | PASS(MOCK) |
| R03 | USB 통화 입출력(원격이 듣고 PC가 받음) | `apps/agent/aicc_agent/a24.py`(ADB 제어 + 지정 오디오 장치), `probe.py` | `test_a24_*`(가짜 adb), 실기기 시험 없음 | **UNKNOWN** |
| R04 | 실제 응답 후 오프닝 1회 | `flow.py:answered`, `runner.py:_call`(단말 ACTIVE 확인 후에만), `a24.py`(OFFHOOK≠응답) | `test_ai_exception_then_ars_resumes`(opening 1회), `test_a24_precise_state_*` | PASS(MOCK) / 실기기 UNKNOWN |
| R05 | ARS 우선, 예외만 AI | `flow.py`, `intents.py`, `runner.py:_ai` | `test_t09_*`, `test_ai_exception_*`, `test_ai_limit_then_consult` | PASS(MOCK) |
| R06 | 명확한 날짜 + 상대 확인 증거일 때만 갱신 | `flow.py:_on_confirm_date`, `services.py:confirm_schedule`(서버 재판정·턴 일치·멱등) | `test_confirm_requires_*`, `test_t17_*`, `test_range_and_unknown_never_confirm` | PASS |
| R07 | 연락거부 즉시 저장·대기열 제외 | `flow.py`(전역 최우선), `services.py:block_contact`(대기 대상 즉시 BLOCKED), `finish_call`(보고 누락 대비) | `test_refusal_*`, `test_block_between_queue_and_claim`, `test_t14_*` | PASS |
| R08 | 양방향 녹음·종료 후 권한자만 재생 | `recorder.py`(2채널), `services.py:store_recording`(해시·암호화), `campaigns.py:playback`(통화 종료·권한·감사) | `test_recorder_*`, `test_full_mock_*`(재생·403) | PASS(MOCK) — 실통화 음성 포함 여부 UNKNOWN |
| R09 | 원본 행·수정 이력 포함 xlsx | `excel_io.py:build_export`, `exporting.py` | `test_t03_*`, `test_full_mock_*`(대사) | PASS |
| R10 | 중단·장애 후 중복 발신·무단 재개 없음 | `flow.py:stop`, 캠페인 PAUSED 전환(`finish_call`), 발신권·IDLE 확인 | `test_t15_*`, `test_t17_*` | PASS(MOCK) |

## 기능 F01–F15

| ID | 기능 | 판정 | 비고 |
|---|---|---|---|
| F01 | 파일 검사·열 매핑 | PASS | xlsm·vbaProject·암호화(OLE)·손상·10MB·1만 행 |
| F02 | 원본 보존 | PASS | 행 순서·빈 행·추가 열·앞자리 0(문자열·`@` 서식). 수식은 값 기준, 서식 완전 재현은 범위 밖 |
| F03 | 대상 검색·선택 | PASS | 이름·번호·연락처 끝 4자리·발신 가능 필터 |
| F04 | 연락거부 대조 | PASS | 업로드·캠페인 생성·발신 직전 3중, 조직 HMAC |
| F05 | 단말 연결 진단 | PASS(MOCK) / UNKNOWN(A24) | ‘연결됨’과 ‘실통화 준비됨’ 분리, 사람 검증 기록 없이는 잠금 |
| F06 | 남녀 ARS 관리 | PASS | 필수 키 + 날짜 조각, 자리표시자·회사명·승인 검사, 버전 관리 |
| F07 | 캠페인 제어 | PASS(MOCK) | 시작·일시정지·즉시중단·재개, 버전 충돌 409, 202=접수 |
| F08 | 실시간 통화 | PASS(MOCK) / UNKNOWN(실음성) | VAD 250–500ms 설정, STT 어댑터. faster-whisper 정확도·지연 미측정 |
| F09 | 예외 AI | PASS(MOCK) / UNKNOWN(Gemini) | 지난 턴 응답 폐기, 실패 시 상담 요청. Gemini Live 실제 호출 미검증 |
| F10 | 일정 추출·재확인 | PASS | 상대 날짜·연말·범위·모름·상시 가능·정정 |
| F11 | 녹음 저장 | PASS(MOCK) | RX/TX 시간축 2채널, SHA-256, 실패 상태 분리 |
| F12 | 검수·정정 | PASS | 사유 필수·버전 검사·변경이력 추가 전용 |
| F13 | 엑셀 다운로드 | PASS | 명부·통화이력·변경이력·오류행·요약, 진행 중 결과 표시 |
| F14 | 감사·접근권한 | PASS | 역할+별도 권한, 재생·내보내기·수정·차단 감사, CSRF 헤더 |
| F15 | 통계·예약 재시도 | NOT DONE(P1) | 재시도 정책 값만 저장, 자동 재시도 없음(설계 기본값과 일치) |

## 화면 S01–S10 (`apps/web/src`)

| ID | 화면 | 파일 | 판정 |
|---|---|---|---|
| S01 | 로그인(첫 실행 시 조직 생성) | `App.tsx:Login` | 화면 확인(스크린샷) |
| S02 | 명부 업로드 | `pages/Upload.tsx` | 화면 확인 |
| S03 | 대상 목록 | `pages/Targets.tsx` | 화면 확인 |
| S04 | 통화 준비(시작 조건) | `pages/Prepare.tsx` | API 시험 + 화면 |
| S05 | 통화 진행 | `pages/Monitor.tsx` | 화면 확인(데스크톱·모바일) |
| S06 | 결과 검수 | `pages/Review.tsx` | 화면 확인 |
| S07 | 녹음 목록 | `pages/Recordings.tsx` | 빌드 확인 |
| S08 | 결과 다운로드 | `pages/Export.tsx` | 화면 확인 |
| S09 | 연락거부 관리 | `pages/Blocks.tsx` | 화면 확인 |
| S10 | 설정과 진단 | `pages/Settings.tsx` | 화면 확인 |

화면 시험은 자동 UI 시험이 아니라 실제 서버에 MOCK 데이터를 넣고 헤드리스 브라우저로 찍은 스크린샷 검토다.

## 시험 T01–T20

| ID | 시험 | 자동 시험 | 실기기 | 판정 |
|---|---|---|---|---|
| T01 | 정상 xlsx 업로드 | `test_t01_upload_mapping_and_leading_zero` | - | PASS |
| T02 | 빈 번호·중복·손상·매크로 | `test_t02_rejects_bad_files`, `test_t02_row_validation_*` | - | PASS |
| T03 | 기존 날짜·추가 열·행 순서 보존 | `test_t03_export_preserves_*` | - | PASS |
| T04 | A24 실제 발신·응답감지 | `test_a24_*`(가짜 adb 출력 파싱) | 미실행 | **UNKNOWN** |
| T05 | USB RX | - | 미실행 | **UNKNOWN** |
| T06 | USB TX(원격 청취) | - | 미실행 | **UNKNOWN** |
| T07 | 양방향 녹음 | `test_recorder_two_channel_alignment`(MOCK) | 미실행 | **UNKNOWN** |
| T08 | 일반 ARS·침묵 | `test_stale_turn_ignored_and_silence_limits` | - | PASS(MOCK) |
| T09 | 연속 3턴 이상 ARS 유지 | `test_t09_ars_branching_continues_after_first_answer` | - | PASS(MOCK) |
| T10 | 예외 AI 후 복귀 | `test_ai_exception_returns_to_question_once`, `test_ai_exception_then_ars_resumes` | - | PASS(MOCK) |
| T11 | 끼어들기·에코 | 런너 끼어들기 코드 있음, 자동 시험 없음 | 미실행 | **UNKNOWN** |
| T12 | 날짜 확정·부정·정정 | `test_confirm_requires_explicit_yes_and_correction`, `test_denial_*` | - | PASS |
| T13 | 상대 날짜·월말·연말 | `test_dates`, `test_year_end_rollover`, `test_non_dates` | - | PASS |
| T14 | 연락거부·재업로드 | `test_t14_block_survives_reupload`, `test_refusal_blocks_and_never_redials` | - | PASS |
| T15 | USB 분리·브라우저 종료 | `test_t15_device_disconnect_pauses_without_redial`(MOCK 분리) | 미실행 | PASS(MOCK) / 실기기 UNKNOWN |
| T16 | 서버·STT·AI 장애 | `test_t16_ai_failure_does_not_store_date` | - | 부분 PASS(AI 장애만) |
| T17 | 중복 이벤트·재시작 | `test_t17_confirmation_idempotent_and_requires_consent`, 이벤트 ID 멱등 | - | PASS |
| T18 | 권한 없는 녹음·내보내기 | `test_t18_permissions_and_audit`, 재생 403 | - | PASS |
| T19 | 보관기간 만료·백업 복구 | `test_t19_retention_expiry_*` | - | 만료 PASS / 백업 복구 NOT DONE |
| T20 | 전체 완료 후 대사 | `test_full_mock_campaign_and_export_reconciliation` | - | PASS(MOCK) |

## 장치 기능표 (갤럭시 A24)

| 기능 | 상태 | 근거 |
|---|---|---|
| 발신(dial) | unknown | ADB `am start -a android.intent.action.CALL` 코드만 있음. 실기기 시험 없음 |
| 종료(hangup) | unknown | `KEYCODE_ENDCALL` 코드만 있음 |
| 응답감지 | unknown | `dumpsys telephony.registry` precise call state 파싱. 기기·버전별 노출 여부 미확인 |
| RX(상대 음성 수신) | unknown | 공식 문서상 USB 디버깅은 오디오 미제공. 지정 오디오 장치 경로만 구현 |
| TX(ARS 원격 청취) | unknown | 위와 같음. ‘전송됨’은 청취 증거로 쓰지 않음 |
| 녹음 | unknown | RX/TX 실음성 미확보 |

## 지연 측정

| 경계 | 표본 | p50 / p95 |
|---|---|---|
| 상대 발화 종료 → 상대 전화기에서 응답 시작(설계 목표 경계) | 0 | **미측정** |
| 로컬 파이프라인(VAD 발화 종료 → 첫 TX 프레임), MOCK 시뮬레이션 시계 | 참고용 | 시뮬레이션 값이라 의미 없음 — 보고하지 않음 |

## 설계와 다르게 구현한 점(근거)

1. **에이전트 연결**: 설계 10은 outbound WSS. 구현은 같은 원칙(에이전트→서버 단방향, 장치 토큰, 서버가 번호·차단 검증 후 발신권 1회 발급)의 HTTPS 폴링. 단일 PC 운영에서 단순성과 시험 가능성을 우선함.
2. **실시간 이벤트 채널**: SSE/WebSocket 대신 웹이 1초 간격으로 `/campaigns/{id}/live` 조회.
3. **음원 필수 키 추가**: `wrong_person`(잘못된 상대 사과)과 날짜 조각 `month_1..12`, `day_1..31`.
4. **시험 조직(sandbox) 개념 추가**: MOCK 결과가 실제 명부 결과로 오인되지 않도록 조직 단위로 MOCK/실기기를 분리.
5. **전사 저장**: 통화 이벤트의 발화 문장은 DB에 평문으로 저장되며 보관기간 만료 시 삭제된다. 필드 단위 암호화는 미구현(전화번호·원본 행·녹음·업로드 원본·내보내기 파일은 암호화 저장).
