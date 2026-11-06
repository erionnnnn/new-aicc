# 계약(Contracts)

- `openapi.json` — 서버 REST API 전체 명세(자동 생성: `python scripts/export_openapi.py`).
- 아래 표 — PC 에이전트가 `POST /api/v1/agent/calls/{call_id}/events` 로 보내는 통화 이벤트 규약.

## 공통 봉투
| 필드 | 설명 |
|---|---|
| `event_id` | 에이전트가 만든 UUID. 서버는 같은 ID를 한 번만 저장(재전송·재연결 중복 방지) |
| `seq` | 통화 안 순번. 같은 통화의 같은 순번에 다른 이벤트가 오면 409 |
| `turn_id` | 상태기계 턴 번호. 재확인 질문과 동의 발화는 같은 턴이어야 확정 가능 |
| `type` | 아래 표 |
| `occurred_at` | ISO 8601(UTC) |
| `payload` | 유형별 내용. 전화번호를 넣지 않는다 |

## 이벤트 유형
| type | payload | 의미 |
|---|---|---|
| `call.dialing` | `{mock}` | 발신 요청(응답 아님) |
| `call.answered` | `{detail, source}` | 단말 상태로 원격 응답 확인 |
| `call.not_answered` | `{reason, elapsed_ms}` | 부재중·통화중 |
| `call.answer_unknown` | 단말 상태 | 응답 감지 불가 → 대화 시작 안 함 |
| `play` | `{key, clips, params}` | ARS 음원 송출 시작 |
| `tx.written` | `{frames, remote_heard:"UNVERIFIED"}` | 송신 경로 기록(청취 증거 아님) |
| `utterance.final` | `{text, stt_engine, stt_latency_ms, confidence, state, candidate_event_id?}` | 안정된 최종 인식 |
| `schedule.candidate` | `{date, reference_date}` | 재확인 질문 직전 날짜 후보 |
| `schedule.confirmed` / `schedule.confirm_rejected` | `{date, status}` / `{code}` | 서버 확정 결과 |
| `contact.blocked` | `{evidence_event_id}` | 연락거부 저장 완료 |
| `ai.start` / `ai.done` / `ai.failed` / `ai.discarded` | `{return_state}` / `{text, latency_ms}` / `{error}` | 예외 질문 AI(날짜 확정 권한 없음) |
| `silence.timeout` | `{ms}` | 침묵 재질문 |
| `metric.local_latency` | `{vad_end_to_tx_start_ms, boundary:"local_pipeline_not_remote"}` | 로컬 지연(원격 청취 기준 아님) |
| `flags` | `{rx_speech_frames, tx_remote_heard, mock}` | 판정 분리 표시 |
| `call.remote_hangup` / `call.hangup` / `call.ended` | | 종료 |

## 날짜 확정 계약
`POST /api/v1/calls/{call_id}/schedule-confirmations` `{evidence_event_id}` + `Idempotency-Key`.
서버는 근거 이벤트(`utterance.final`)와 그 `candidate_event_id`(`schedule.candidate`)를 읽어
같은 통화·같은 턴인지, 발화가 명시적 동의인지 **서버에서 다시 판정**한 뒤 반영한다. 요청 본문의 날짜 값은 받지 않는다.
