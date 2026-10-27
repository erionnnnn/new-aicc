"""통화 대화 상태기계(설계 05 통화 상태 전이).

순수 로직이며 I/O가 없다. 런너(에이전트)가 이벤트를 넣고 Action 목록을 받아 실행한다.

핵심 보장
- 연락거부는 어느 대화 상태에서든 최우선으로 처리하고 BlockContact 를 즉시 내보낸다.
- 일정 반영(ConfirmSchedule)은 CONFIRM_DATE 상태에서 같은 턴의 명시적 동의 발화가 있을 때만 나온다.
- 턴 번호가 다른(지난) 인식 결과는 버린다.
- 예외 질문만 AI_EXCEPTION 으로 가고, 처리 후 저장된 질문 상태로 복귀한다.
  첫 답변 이후에도 일반 답은 ARS 분기를 유지한다(회귀 시험 T09).
- AI 는 날짜를 확정하지 못한다. AI 응답 이벤트에는 날짜 필드가 없다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .dates import DateMention
from .intents import Interpretation, interpret
from .scenarios import BY_CODE, Scenario
from .scenarios import match as match_scenario

# WAIT_AVAILABILITY: 녹음 상황 응답(급여·병원 질문 등)을 들려준 뒤 일정 답을 기다리는 상태(질문을 다시 하지 않음)
DIALOGUE_STATES = ("OPENING", "ASK_WORKING", "ASK_END_DATE", "ASK_AVAILABLE_DATE", "CONFIRM_DATE", "WAIT_AVAILABILITY")
QUESTION_KEY = {
    "ASK_WORKING": "ask_working",
    "ASK_END_DATE": "ask_end_date",
    "ASK_AVAILABLE_DATE": "ask_available_date",
}
OUTCOMES = (
    "CONFIRMED", "ANYTIME", "UNCONFIRMED", "CALLBACK_REQUESTED", "REFUSED", "WRONG_PERSON", "VOICEMAIL",
    "NO_ANSWER", "LINE_BUSY", "DIAL_FAILED", "NO_RESPONSE", "CONSULT_REQUESTED", "REMOTE_HANGUP",
    "DEVICE_ERROR", "STOPPED",
)


# ---------------- Actions ----------------
@dataclass(frozen=True)
class Play:
    key: str
    turn: int
    params: dict = field(default_factory=dict)


@dataclass(frozen=True)
class StopTx:
    reason: str


@dataclass(frozen=True)
class StartAI:
    question: str
    return_state: str
    turn: int


@dataclass(frozen=True)
class ConfirmSchedule:
    available_date: date
    evidence_event_id: str
    candidate_event_id: str
    turn: int


@dataclass(frozen=True)
class BlockContact:
    reason: str
    evidence_event_id: str


@dataclass(frozen=True)
class MarkReview:
    reason: str


@dataclass(frozen=True)
class HangUp:
    reason: str


@dataclass(frozen=True)
class End:
    outcome: str


Action = Play | StopTx | StartAI | ConfirmSchedule | BlockContact | MarkReview | HangUp | End


@dataclass
class Observation:
    work_status: str | None = None  # WORKING | NOT_WORKING | None
    end_date: date | None = None
    end_date_text: str | None = None
    available_date: date | None = None  # 후보(확정 여부는 confirmation_state)
    available_text: str | None = None
    range_start: date | None = None
    range_end: date | None = None
    confirmation_state: str = "UNCONFIRMED"  # CONFIRMED | ANYTIME | RANGE | UNKNOWN | UNCONFIRMED
    candidate_event_id: str | None = None
    evidence_event_id: str | None = None
    history: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # 선호 조건 등 상황 메모(예: 평일선호, 야간불가)

    def as_dict(self) -> dict:
        return {
            "work_status": self.work_status,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "end_date_text": self.end_date_text,
            "available_date": self.available_date.isoformat() if self.available_date else None,
            "available_text": self.available_text,
            "range_start": self.range_start.isoformat() if self.range_start else None,
            "range_end": self.range_end.isoformat() if self.range_end else None,
            "confirmation_state": self.confirmation_state,
            "candidate_event_id": self.candidate_event_id,
            "evidence_event_id": self.evidence_event_id,
            "history": self.history,
            "notes": self.notes,
        }


class CallFlow:
    def __init__(self, reference: date, *, max_reprompt: int = 2, max_ai: int = 2, max_corrections: int = 3,
                 clips: set[str] | frozenset[str] | None = None, max_scenarios: int = 4,
                 ai_conversation: bool = False, max_ai_conversation: int = 6):
        self.reference = reference
        self.state = "QUEUED"
        self.turn = 0
        self.max_reprompt = max_reprompt
        self.max_ai = max_ai
        self.max_corrections = max_corrections
        self.reprompts: dict[str, int] = {}
        self.ai_count = 0
        self.corrections = 0
        self.return_state: str | None = None
        self.obs = Observation()
        self.outcome: str | None = None
        self.review_reasons: list[str] = []
        self.transcript: list[dict] = []
        # 녹음이 있는 안내 키. 상황 응답(sc_*)은 녹음이 있을 때만 쓴다(없으면 기존 흐름·AI 로 처리)
        self.clips = frozenset(clips or ())
        self.max_scenarios = max_scenarios
        self.scenario_count = 0
        # 대화형 AI: 규칙으로 못 알아들은 말을 '다시 말씀해 주세요' 대신 AI(같은 목소리)가 자연스럽게 받아 주고
        # 원래 확인하려던 질문으로 이끈다. 날짜 확정은 여전히 이 상태기계만 한다(AI 는 말만 함).
        self.ai_conversation = ai_conversation
        if ai_conversation:
            self.max_ai = max(self.max_ai, max_ai_conversation)

    # ---------- 공개 이벤트 ----------
    @property
    def finished(self) -> bool:
        return self.outcome is not None

    def dialing(self) -> list[Action]:
        self.state = "DIALING"
        return []

    def answered(self) -> list[Action]:
        """원격 응답이 실제로 확인된 뒤에만 호출한다(발신음·통화중은 응답이 아님)."""
        if self.finished or self.state not in ("QUEUED", "DIALING"):
            return []
        self.state = "OPENING"
        return [self._play("opening")]

    def not_answered(self, reason: str) -> list[Action]:
        mapping = {"NO_ANSWER": "NO_ANSWER", "BUSY": "LINE_BUSY", "FAILED": "DIAL_FAILED", "VOICEMAIL": "VOICEMAIL"}
        return self._end(mapping.get(reason, "DIAL_FAILED"))

    def utterance(self, text: str, *, turn: int, event_id: str, final: bool = True) -> list[Action]:
        if self.finished or not final:
            return []  # 부분 인식은 런너의 사전 준비에만 사용
        if turn != self.turn:
            return []  # 지난 턴 결과 폐기
        self.transcript.append({"turn": turn, "event_id": event_id, "text": text, "state": self.state})
        state_for_parse = self.return_state if self.state == "AI_EXCEPTION" else self.state
        it = interpret(text, state_for_parse or "OPENING", self.reference)

        # 1) 전역 최우선
        if it.intent == "REFUSAL":
            self.obs.history.append({"event": "REFUSAL", "event_id": event_id})
            return [StopTx("refusal"), BlockContact("CALLEE_REQUEST", event_id), self._play("refusal_ack"),
                    HangUp("refusal"), *self._end("REFUSED")]
        if it.intent == "VOICEMAIL":
            return [StopTx("voicemail"), HangUp("voicemail"), *self._end("VOICEMAIL")]
        if it.intent == "WRONG_PERSON":
            self.review_reasons.append("WRONG_PERSON")
            return [StopTx("wrong_person"), self._play("wrong_person"), HangUp("wrong_person"),
                    MarkReview("WRONG_PERSON"), *self._end("WRONG_PERSON")]
        if it.intent == "BUSY":
            drive = BY_CODE["DRIVING"]
            key = drive.key if self._has(drive.key) and match_scenario(text, ("END",)) is drive else "callback"
            return [self._play(key), HangUp("busy"), *self._end("CALLBACK_REQUESTED")]

        if self.state == "AI_EXCEPTION":
            # AI 응답 중 일반 발화는 AI 세션이 처리한다. 상태기계는 날짜를 받지 않는다.
            return []
        if self.state not in DIALOGUE_STATES:
            return []

        sc_actions = self._scenario(text, it, event_id)
        if sc_actions is not None:
            return sc_actions
        if it.intent == "REPEAT":
            didnt = BY_CODE["DIDNT_HEAR"].key
            n = self.reprompts.get(self.state, 0)
            if self.state == "OPENING" and self._has(didnt) and n < self.max_reprompt:
                self.reprompts[self.state] = n + 1
                self.state = "WAIT_AVAILABILITY"
                return [self._play(didnt)]
            return self._reprompt(it, same_question=True)
        if it.intent == "QUESTION":
            return self._exception(text)

        handler = getattr(self, f"_on_{self.state.lower()}")
        return handler(it, event_id)

    def silence(self, *, turn: int) -> list[Action]:
        if self.finished or turn != self.turn or self.state not in DIALOGUE_STATES:
            return []
        n = self.reprompts.get(self.state, 0)
        if n >= self.max_reprompt:
            self.review_reasons.append("NO_RESPONSE")
            end_key = BY_CODE["SILENCE_END"].key
            return [self._play(end_key if self._has(end_key) else "callback"), HangUp("silence"),
                    *self._end("NO_RESPONSE")]
        self.reprompts[self.state] = n + 1
        check = BY_CODE["SILENCE_CHECK"].key
        if n == 0 and self._has(check) and self.state != "CONFIRM_DATE":
            return [self._play(check)]  # "제 말 들리실까요? 근무 가능하신 일정만 말씀해주시면 됩니다."
        return [self._question()]

    def ai_done(self, *, turn: int) -> list[Action]:
        if self.finished or self.state != "AI_EXCEPTION" or turn != self.turn:
            return []
        self.state = self.return_state or "ASK_WORKING"
        self.return_state = None
        if self.ai_conversation:
            return []  # AI 가 답 끝에 확인할 내용을 이미 물었다 → 같은 질문 녹음을 반복하지 않고 답을 기다림
        return [self._question()]

    def ai_failed(self, *, turn: int) -> list[Action]:
        if self.finished or self.state != "AI_EXCEPTION" or turn != self.turn:
            return []
        self.review_reasons.append("AI_FAILED")
        if self.ai_conversation and self.return_state in DIALOGUE_STATES:
            # 대화형 보조가 실패하면 통화를 끝내지 않고 원래 질문(녹음)으로 돌아간다
            self.state = self.return_state
            self.return_state = None
            return [self._question()]
        return [StopTx("ai_failed"), self._play("ai_failed"), HangUp("ai_failed"), *self._end("CONSULT_REQUESTED")]

    def remote_hangup(self) -> list[Action]:
        if self.finished:
            return []
        return self._end("CONFIRMED" if self.obs.confirmation_state == "CONFIRMED" else "REMOTE_HANGUP")

    def device_error(self, detail: str) -> list[Action]:
        if self.finished:
            return []
        self.review_reasons.append(f"DEVICE_ERROR:{detail}")
        return [StopTx("device_error"), *self._end("DEVICE_ERROR")]

    def timeout(self) -> list[Action]:
        """최대 통화 길이 초과: 날짜를 추정하지 않고 미확인 종료."""
        if self.finished:
            return []
        self.review_reasons.append("MAX_CALL_DURATION")
        return [StopTx("timeout"), HangUp("timeout"), *self._end("UNCONFIRMED")]

    def stop(self) -> list[Action]:
        if self.finished:
            return []
        return [StopTx("operator_stop"), HangUp("operator_stop"), *self._end("STOPPED")]

    # ---------- 상태별 처리 ----------
    def _on_opening(self, it: Interpretation, eid: str) -> list[Action]:
        if it.intent == "WORKING":
            self.obs.work_status = "WORKING"
            end = self._pick(it, "END")
            if end:
                self._set_end(end)
                return self._go("ASK_AVAILABLE_DATE")
            return self._go("ASK_END_DATE")
        if it.intent == "NOT_WORKING":
            self.obs.work_status = "NOT_WORKING"
            return self._from_available(it, eid) if self._pick(it, "START") else self._go("ASK_AVAILABLE_DATE")
        if it.intent == "DATE":
            return self._from_available(it, eid)
        if it.intent == "YES":
            return self._go("ASK_WORKING")
        if it.intent == "NO":
            if self.ai_conversation:
                return self._reprompt(it)  # '아닌데요'만으로는 뜻이 모호 → AI 가 되물음
            return [self._play("callback"), HangUp("declined"), *self._end("CALLBACK_REQUESTED")]
        return self._reprompt(it)

    def _on_ask_working(self, it: Interpretation, eid: str) -> list[Action]:
        if it.intent == "WORKING":
            self.obs.work_status = "WORKING"
            end = self._pick(it, "END")
            if end:
                self._set_end(end)
                if self._pick(it, "START"):
                    return self._from_available(it, eid)
                return self._go("ASK_AVAILABLE_DATE")
            return self._go("ASK_END_DATE")
        if it.intent == "NOT_WORKING":
            self.obs.work_status = "NOT_WORKING"
            if self._pick(it, "START"):
                return self._from_available(it, eid)
            return self._go("ASK_AVAILABLE_DATE")
        if it.intent == "DATE":
            return self._from_available(it, eid)
        if it.intent == "ANYTIME":
            self.obs.work_status = "NOT_WORKING"
            return self._anytime(it)
        return self._reprompt(it)

    def _on_ask_end_date(self, it: Interpretation, eid: str) -> list[Action]:
        if it.intent == "DATE":
            end = self._pick(it, "END") or self._pick(it, None)
            if end:
                self._set_end(end)
            if self._pick(it, "START"):
                return self._from_available(it, eid)
            return self._go("ASK_AVAILABLE_DATE")
        if it.intent in ("UNKNOWN_DATE", "RANGE"):
            rng = next((m for m in it.dates if m.kind == "RANGE"), None)
            self.obs.end_date_text = rng.source if rng else it.text
            self.obs.history.append({"event": "END_DATE_UNCLEAR", "text": it.text})
            return self._go("ASK_AVAILABLE_DATE")
        if it.intent == "ANYTIME":
            return self._anytime(it)
        if it.intent in ("YES", "NO", "UNCLEAR"):
            return self._reprompt(it)
        return self._reprompt(it)

    def _on_ask_available_date(self, it: Interpretation, eid: str) -> list[Action]:
        if it.intent == "DATE":
            start = self._pick(it, "START") or self._pick(it, None)
            if start is None:
                end = self._pick(it, "END")
                if end:
                    # '25일까지 해요' — 종료일만 저장하고 26일 가능을 추정하지 않는다.
                    self._set_end(end)
                    if self.obs.work_status is None:
                        self.obs.work_status = "WORKING"
                return self._reprompt(it, same_question=True)
            return self._candidate(start, it, eid)
        if it.intent == "ANYTIME":
            return self._anytime(it)
        if it.intent == "RANGE":
            rng = next(m for m in it.dates if m.kind == "RANGE")
            self.obs.range_start, self.obs.range_end = rng.range_start, rng.range_end
            self.obs.available_text = rng.source
            self.obs.history.append({"event": "RANGE", "text": it.text})
            n = self.reprompts.get(self.state, 0)
            if n >= self.max_reprompt:
                self.obs.confirmation_state = "RANGE"
                self.review_reasons.append("DATE_RANGE_ONLY")
                return [self._play("closing"), HangUp("range"), *self._end("UNCONFIRMED")]
            return self._reprompt(it, same_question=True)
        if it.intent == "UNKNOWN_DATE":
            self.obs.confirmation_state = "UNKNOWN"
            self.obs.available_text = it.text
            self.review_reasons.append("DATE_UNKNOWN")
            return [self._play("closing"), HangUp("unknown"), *self._end("UNCONFIRMED")]
        return self._reprompt(it)

    def _on_confirm_date(self, it: Interpretation, eid: str) -> list[Action]:
        cand = self.obs.available_date
        if it.intent == "YES" and cand is not None:
            return self._confirm(eid)
        if it.intent == "DATE":
            new = self._pick(it, "START") or self._pick(it, None)
            if new and new.value == cand and it.yes and not it.no:
                return self._confirm(eid)  # "네, 26일이요"
            if new and new.value != cand:
                self.corrections += 1
                self.obs.history.append({"event": "CORRECTION", "from": cand.isoformat() if cand else None,
                                         "to": new.value.isoformat() if new.value else None, "event_id": eid})
                if self.corrections > self.max_corrections:
                    self.review_reasons.append("TOO_MANY_CORRECTIONS")
                    return [self._play("closing"), HangUp("corrections"), *self._end("UNCONFIRMED")]
                return self._candidate(new, it, eid)
            if new and new.value == cand:
                # 같은 날짜를 다시 말했지만 명시적 동의어가 없음 → 다시 확인
                return self._reprompt(it, same_question=True)
        if it.intent == "NO":
            self.obs.history.append({"event": "DENIED", "candidate": cand.isoformat() if cand else None,
                                     "event_id": eid})
            self.obs.available_date = None
            self.obs.candidate_event_id = None
            return self._go("ASK_AVAILABLE_DATE")
        return self._reprompt(it, same_question=True)

    def _on_wait_availability(self, it: Interpretation, eid: str) -> list[Action]:
        """상황 응답 녹음 뒤: 일정 관련 답이면 처리하고, 단순 대답·불명확이면 원래 질문을 이어서 한다."""
        if it.intent in ("WORKING", "NOT_WORKING"):
            self.state = "ASK_WORKING"
            return self._on_ask_working(it, eid)
        if it.intent == "DATE":
            if self.obs.work_status is None and self._pick(it, "END") and not self._pick(it, "START"):
                self.obs.work_status = "WORKING"
            return self._from_available(it, eid)
        if it.intent == "ANYTIME":
            return self._anytime(it)
        if it.intent in ("RANGE", "UNKNOWN_DATE"):
            self.state = "ASK_AVAILABLE_DATE"
            return self._on_ask_available_date(it, eid)
        if it.intent in ("YES", "NO"):
            return [self._question()]  # "네" → 이어서 근무 여부(또는 가능일) 질문
        return self._reprompt(it)

    def _has(self, key: str) -> bool:
        return key in self.clips

    def _scenario(self, text: str, it: Interpretation, eid: str) -> list[Action] | None:
        """녹음된 상황 응답(기존 ars_scenarios). 해당 없거나 일정 답이 함께 있으면 None(기존 흐름으로)."""
        s: Scenario | None = match_scenario(text)
        if s is None:
            return None
        dated = any(m.kind in ("DATE", "ANYTIME", "RANGE") for m in it.dates)
        if s.note and s.note not in self.obs.notes and s.kind == "PREF":
            self.obs.notes.append(s.note)  # 선호 조건은 녹음 재생 여부와 관계없이 기록
        if not self._has(s.key) or self.scenario_count >= self.max_scenarios:
            return None
        if s.kind == "PREF":
            if dated or it.intent in ("WORKING", "NOT_WORKING"):
                return None  # "평일만, 다음 주 월요일부터요" → 날짜 처리 우선(메모는 남김)
            return self._scenario_play(s, eid)
        if dated:
            return None
        if s.kind == "END":
            self.obs.history.append({"event": "SCENARIO", "code": s.code, "event_id": eid})
            if s.note and s.note not in self.obs.notes:
                self.obs.notes.append(s.note)
            out: list[Action] = [StopTx(s.code.lower()), self._play(s.key)]
            if s.review:
                self.review_reasons.append(s.review)
                out.append(MarkReview(s.review))
            if s.code == "UNKNOWN_SCHEDULE":
                self.obs.confirmation_state = "UNKNOWN"
                self.obs.available_text = text
            return [*out, HangUp(s.code.lower()), *self._end(s.outcome or "UNCONFIRMED")]
        if s.kind == "INFO":
            return self._scenario_play(s, eid)
        return None

    def _scenario_play(self, s: Scenario, eid: str) -> list[Action]:
        self.scenario_count += 1
        self.obs.history.append({"event": "SCENARIO", "code": s.code, "event_id": eid})
        play = self._play(s.key)
        if self.state == "CONFIRM_DATE":
            return [play, self._question()]  # 답한 뒤 날짜 재확인을 다시
        self.state = "WAIT_AVAILABILITY"
        return [play]

    # ---------- 보조 ----------
    def _pick(self, it: Interpretation, role: str | None) -> DateMention | None:
        for m in reversed(it.dates):  # 같은 발화의 마지막 언급을 우선
            if m.kind == "DATE" and m.role == role and m.value is not None:
                return m
        return None

    def _set_end(self, m: DateMention) -> None:
        self.obs.end_date = m.value
        self.obs.end_date_text = m.source
        self.obs.history.append({"event": "END_DATE_CANDIDATE", "date": m.value.isoformat() if m.value else None})

    def _from_available(self, it: Interpretation, eid: str) -> list[Action]:
        end = self._pick(it, "END")
        if end:
            self._set_end(end)
        start = self._pick(it, "START") or self._pick(it, None)
        if start:
            return self._candidate(start, it, eid)
        return self._go("ASK_AVAILABLE_DATE")

    def _candidate(self, m: DateMention, it: Interpretation, eid: str) -> list[Action]:
        if m.value is None:
            return self._reprompt(it)
        if m.value < self.reference:
            self.obs.history.append({"event": "PAST_DATE_REJECTED", "date": m.value.isoformat()})
            return self._reprompt(it, same_question=self.state == "ASK_AVAILABLE_DATE")
        self.obs.available_date = m.value
        self.obs.available_text = m.source
        self.obs.candidate_event_id = eid
        self.obs.range_start = self.obs.range_end = None
        self.obs.history.append({"event": "CANDIDATE", "date": m.value.isoformat(), "event_id": eid,
                                 "notes": list(m.notes)})
        self.state = "CONFIRM_DATE"
        return [self._question()]

    def _confirm(self, eid: str) -> list[Action]:
        cand = self.obs.available_date
        assert cand is not None
        self.obs.confirmation_state = "CONFIRMED"
        self.obs.evidence_event_id = eid
        self.obs.history.append({"event": "CONFIRMED", "date": cand.isoformat(), "event_id": eid})
        action = ConfirmSchedule(cand, eid, self.obs.candidate_event_id or eid, self.turn)
        self.state = "CLOSING"
        return [action, self._play("closing"), HangUp("done"), *self._end("CONFIRMED")]

    def _anytime(self, it: Interpretation) -> list[Action]:
        self.obs.confirmation_state = "ANYTIME"
        self.obs.available_text = it.text
        self.obs.history.append({"event": "ANYTIME", "text": it.text})
        self.state = "CLOSING"
        return [self._play("closing"), HangUp("anytime"), *self._end("ANYTIME")]

    def _exception(self, text: str) -> list[Action]:
        if self.ai_count >= self.max_ai:
            self.review_reasons.append("AI_LIMIT")
            return [self._play("callback"), HangUp("ai_limit"), *self._end("CONSULT_REQUESTED")]
        self.ai_count += 1
        self.return_state = self.state
        self.state = "AI_EXCEPTION"
        self.turn += 1
        return [StartAI(text, self.return_state, self.turn)]

    def _go(self, state: str) -> list[Action]:
        self.state = state
        return [self._question()]

    def _reprompt(self, it: Interpretation, same_question: bool = False) -> list[Action]:
        if self.ai_conversation and it.text.strip() and self.ai_count < self.max_ai:
            return self._exception(it.text)  # 못 알아들은 말 → AI 가 자연스럽게 응대
        n = self.reprompts.get(self.state, 0)
        if n >= self.max_reprompt:
            self.review_reasons.append(f"UNCLEAR_{self.state}")
            return [self._play("callback"), HangUp("unclear"), *self._end("UNCONFIRMED")]
        self.reprompts[self.state] = n + 1
        if self.state == "OPENING" or not same_question:
            # 오프닝은 한 번만 재생한다(R04). 이해하지 못한 경우 짧은 재요청 후 같은 질문을 기다린다.
            if self.state == "OPENING":
                return [self._play("ask_repeat")]
        return [self._question()]

    def _question(self) -> Play:
        if self.state == "CONFIRM_DATE":
            d = self.obs.available_date
            assert d is not None
            return self._play("confirm_date", {"date": d.isoformat(), "month": d.month, "day": d.day})
        if self.state == "OPENING":
            return self._play("ask_repeat")
        if self.state == "WAIT_AVAILABILITY":
            self.state = "ASK_WORKING" if self.obs.work_status is None else "ASK_AVAILABLE_DATE"
        return self._play(QUESTION_KEY[self.state])

    def _play(self, key: str, params: dict | None = None) -> Play:
        self.turn += 1
        return Play(key, self.turn, params or {})

    def _end(self, outcome: str) -> list[Action]:
        if self.outcome is None:
            self.outcome = outcome
            self.state = "ENDED"
        return [End(outcome)]
