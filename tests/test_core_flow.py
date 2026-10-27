"""T08–T13 상태기계 회귀 시험 (실기기 아님, 순수 로직)."""

from datetime import date

from aicc_core.flow import BlockContact, CallFlow, ConfirmSchedule, End, Play, StartAI

REF = date(2026, 10, 24)


class Driver:
    def __init__(self):
        self.f = CallFlow(REF)
        self.n = 0
        self.actions = list(self.f.answered())

    def say(self, text):
        self.n += 1
        acts = self.f.utterance(text, turn=self.f.turn, event_id=f"e{self.n}")
        self.actions += acts
        return acts

    def plays(self):
        return [a.key for a in self.actions if isinstance(a, Play)]

    def confirms(self):
        return [a for a in self.actions if isinstance(a, ConfirmSchedule)]


def test_happy_path_not_working():
    d = Driver()
    d.say("네 괜찮아요")
    d.say("아니요 지금 쉬고 있어요")
    d.say("다음 주 월요일부터 가능해요")
    assert d.f.state == "CONFIRM_DATE"
    d.say("네 맞아요")
    c = d.confirms()
    assert len(c) == 1 and c[0].available_date == date(2026, 10, 26) and c[0].evidence_event_id == "e4"
    assert d.f.outcome == "CONFIRMED"
    # 사용자 시나리오(2026-10-27): 첫 '네' 뒤 바로 가능일 질문, '쉬고 있어요'에는 같은 질문을 한 번 더
    assert d.plays() == ["opening", "ask_available_date", "ask_available_date", "confirm_date", "closing"]


def test_t09_ars_branching_continues_after_first_answer():
    """첫 답변 이후에도 일반 응답이 AI 예외로 빠지지 않아야 한다."""
    d = Driver()
    for text in ["네", "네 일하고 있어요", "25일까지요", "26일부터 돼요", "네"]:
        d.say(text)
    assert not any(isinstance(a, StartAI) for a in d.actions)
    assert d.f.obs.end_date == date(2026, 10, 25)
    assert d.confirms()[0].available_date == date(2026, 10, 26)


def test_end_date_does_not_imply_start():
    d = Driver()
    d.say("네")
    d.say("25일까지 일해요")
    assert d.f.state == "ASK_AVAILABLE_DATE"
    d.say("25일까지요")  # 다시 종료일만 → 재질문, 26일 추정 금지
    assert d.f.obs.available_date is None
    assert d.f.state == "ASK_AVAILABLE_DATE"


def test_confirm_requires_explicit_yes_and_correction():
    d = Driver()
    d.say("네")
    d.say("아니요")
    d.say("26일부터요")
    d.say("아니 27일이요")
    assert d.f.obs.available_date == date(2026, 10, 27) and d.f.state == "CONFIRM_DATE"
    assert not d.confirms()
    d.say("네 맞습니다")
    assert d.confirms()[0].available_date == date(2026, 10, 27)
    hist = [h["event"] for h in d.f.obs.history]
    assert "CORRECTION" in hist


def test_denial_returns_to_question_and_hangup_keeps_unconfirmed():
    d = Driver()
    d.say("네")
    d.say("쉬고 있어요")
    d.say("26일")
    d.say("아니요")
    assert d.f.state == "ASK_AVAILABLE_DATE" and d.f.obs.available_date is None
    d.actions += d.f.remote_hangup()
    assert d.f.outcome == "REMOTE_HANGUP" and not d.confirms()


def test_refusal_any_state_blocks_immediately():
    for prefix in ([], ["네"], ["네", "쉬어요"], ["네", "쉬어요", "26일부터"]):
        d = Driver()
        for p in prefix:
            d.say(p)
        d.say("앞으로 연락하지 마세요")
        blocks = [a for a in d.actions if isinstance(a, BlockContact)]
        assert len(blocks) == 1, prefix
        assert d.f.outcome == "REFUSED" and not d.confirms()


def test_ai_exception_returns_to_question_once():
    d = Driver()
    d.say("네")
    d.say("근데 누구세요?")
    assert d.f.state == "AI_EXCEPTION"
    ai = [a for a in d.actions if isinstance(a, StartAI)][0]
    assert ai.return_state == "ASK_AVAILABLE_DATE"
    # AI 진행 중 일반 발화는 상태기계가 날짜로 쓰지 않음
    d.say("26일부터 가능해요")
    assert d.f.obs.available_date is None
    assert d.f.ai_done(turn=ai.turn)[0] == Play("ask_available_date", d.f.turn, {})
    assert d.f.state == "ASK_AVAILABLE_DATE"
    # 오래된 턴의 AI 완료는 무시
    assert d.f.ai_done(turn=ai.turn) == []


def test_ai_limit_then_consult():
    d = Driver()
    d.say("네")
    for _ in range(2):
        d.say("급여 얼마예요?")
        d.f.ai_done(turn=d.f.turn)
    d.say("병원은 어디예요?")
    assert d.f.outcome == "CONSULT_REQUESTED"


def test_stale_turn_ignored_and_silence_limits():
    d = Driver()
    assert d.f.utterance("네", turn=d.f.turn - 1, event_id="old") == []
    d.say("네")
    t = d.f.turn
    d.f.silence(turn=t)
    d.f.silence(turn=d.f.turn)
    acts = d.f.silence(turn=d.f.turn)
    assert any(isinstance(a, End) and a.outcome == "NO_RESPONSE" for a in acts)


def test_range_and_unknown_never_confirm():
    d = Driver()
    for t in ["네", "쉬어요", "월말쯤이요", "월말쯤", "월말"]:
        d.say(t)
    assert d.f.outcome == "UNCONFIRMED" and d.f.obs.confirmation_state == "RANGE" and not d.confirms()
    d = Driver()
    for t in ["네", "쉬어요", "잘 모르겠어요"]:
        d.say(t)
    assert d.f.obs.confirmation_state == "UNKNOWN" and not d.confirms()


def test_anytime_not_a_date():
    d = Driver()
    for t in ["네", "쉬어요", "언제든 가능해요"]:
        d.say(t)
    assert d.f.outcome == "ANYTIME" and d.f.obs.available_date is None and not d.confirms()


def test_busy_today_is_callback_not_refusal():
    d = Driver()
    d.say("오늘 안 돼요")
    assert d.f.outcome == "CALLBACK_REQUESTED"
    assert not any(isinstance(a, BlockContact) for a in d.actions)


def test_past_date_not_accepted():
    d = Driver()
    for t in ["네", "쉬어요", "이번 주 수요일부터요"]:
        d.say(t)
    assert d.f.obs.available_date is None
