"""기존 녹음 상황 응답(ars_scenarios) 연결 시험."""

from datetime import date

from aicc_core.flow import CallFlow, ConfirmSchedule, MarkReview, Play
from aicc_core.scenarios import SCENARIO_KEYS, match

REF = date(2026, 10, 24)
ALL = set(SCENARIO_KEYS)


class D:
    def __init__(self, clips=ALL):
        self.f = CallFlow(REF, clips=clips)
        self.n = 0
        self.actions = list(self.f.answered())

    def say(self, text):
        self.n += 1
        acts = self.f.utterance(text, turn=self.f.turn, event_id=f"e{self.n}")
        self.actions += acts
        return [a.key for a in acts if isinstance(a, Play)]


def test_pay_question_plays_recording_then_waits_for_schedule():
    d = D()
    d.say("네")
    assert d.say("급여는 얼마예요?") == ["sc_ask_pay"]
    assert d.f.state == "WAIT_AVAILABILITY"
    assert d.say("다음 주 월요일부터 가능해요") == ["confirm_date"]
    d.say("네 맞아요")
    assert len([a for a in d.actions if isinstance(a, ConfirmSchedule)]) == 1


def test_after_info_simple_yes_continues_with_working_question():
    d = D()
    assert d.say("누구세요?") == ["sc_custom_intro"]
    assert d.say("네") == ["ask_available_date"]
    assert d.f.state == "ASK_AVAILABLE_DATE"


def test_after_info_working_answer_goes_to_end_date():
    d = D()
    d.say("왜 전화했어요")
    assert d.say("지금 근무 중이에요") == ["ask_end_date"]


def test_preference_with_date_keeps_date_flow_and_note():
    d = D()
    d.say("네")
    assert d.say("평일만 되는데 다음 주 월요일부터 가능해요") == ["confirm_date"]
    assert "평일선호" in d.f.obs.notes


def test_preference_without_date_plays_recording():
    d = D()
    d.say("네")
    assert d.say("야간은 싫어요") == ["sc_no_night"]
    assert d.f.obs.notes == ["야간불가"]


def test_end_scenarios_outcomes():
    d = D()
    d.say("네")
    assert d.say("문자로 보내 주세요") == ["sc_text_request"]
    assert d.f.outcome == "UNCONFIRMED" and "TEXT_REQUESTED" in d.f.review_reasons
    assert any(isinstance(a, MarkReview) for a in d.actions)
    d = D()
    d.say("네")
    assert d.say("당분간 쉴게요") == ["sc_rest"]
    assert d.f.outcome == "UNCONFIRMED"


def test_driving_uses_recording_busy_otherwise_callback():
    d = D()
    assert d.say("지금 운전 중이에요") == ["sc_driving"]
    assert d.f.outcome == "CALLBACK_REQUESTED"
    d = D()
    assert d.say("지금 바빠요") == ["callback"]


def test_no_recordings_falls_back_to_previous_behavior():
    d = D(clips=set())
    d.say("네")
    acts = d.f.utterance("급여는 얼마예요?", turn=d.f.turn, event_id="x")
    assert type(acts[0]).__name__ == "StartAI"  # 녹음이 없으면 기존처럼 AI 예외 응답


def test_silence_uses_check_then_end_recordings():
    d = D()
    d.say("네")
    assert [a.key for a in d.f.silence(turn=d.f.turn) if isinstance(a, Play)] == ["sc_silence_check"]
    d.f.silence(turn=d.f.turn)
    last = [a.key for a in d.f.silence(turn=d.f.turn) if isinstance(a, Play)]
    assert last == ["sc_silence_end"] and d.f.outcome == "NO_RESPONSE"


def test_didnt_hear_after_opening():
    d = D()
    assert d.say("뭐라고요?") == ["sc_didnt_hear"]
    assert d.f.state == "WAIT_AVAILABILITY"


def test_info_during_confirm_reasks_date():
    d = D()
    d.say("네")
    d.say("아니요")
    d.say("내일부터요")
    assert d.say("근데 급여는 얼마예요") == ["sc_ask_pay", "confirm_date"]


def test_refusal_still_wins_and_scenario_limit():
    d = D()
    assert d.say("급여 얼마예요 연락하지 마세요") == ["refusal_ack"]
    d = D()
    for q in ("급여 얼마예요", "어느 병원이에요", "몇 시간 근무예요", "어떤 환자예요"):
        d.say(q)
    assert d.f.scenario_count == 4
    acts = d.f.utterance("누구세요", turn=d.f.turn, event_id="z")
    assert type(acts[0]).__name__ == "StartAI"  # 한도 초과 후에는 AI 로


def test_keywords_do_not_steal_not_working_answer():
    assert match("지금은 쉬어요") is None
    assert match("중증 환자는 싫어요").code == "NO_SEVERE"
    assert match("무슨 일감이에요").code == "ASK_JOB"


def test_real_call_phrases_2026_10_27():
    """실통화에서 나온 말: 잘못된 상대·전화 이유 질문을 같은 질문 반복 대신 녹음으로 처리."""
    d = D()
    assert d.say("나 간병사 아니야.") == ["wrong_person"]
    assert d.f.outcome == "WRONG_PERSON"
    d = D()
    d.say("네. 네.")
    assert d.say("네가 그걸 왜 궁금해.") == ["sc_why_call"]
    assert match("안 알려줄래?").code == "WHY_CALL"


def test_conversation_mode_uses_ai_instead_of_repeat_and_keeps_state():
    from aicc_core.flow import StartAI
    f = CallFlow(REF, clips=ALL, ai_conversation=True)
    f.answered()
    acts = f.utterance("음 글쎄 그게 좀", turn=f.turn, event_id="e1")
    assert isinstance(acts[0], StartAI) and acts[0].return_state == "OPENING"
    assert f.ai_done(turn=f.turn) == []  # AI 가 이미 되물음 → 녹음 질문 반복 안 함
    assert f.state == "OPENING"
    acts = f.utterance("지금은 쉬고 있어요", turn=f.turn, event_id="e2")
    assert [a.key for a in acts if isinstance(a, Play)] == ["ask_available_date"]


def test_conversation_mode_ai_failure_returns_to_question_not_hangup():
    f = CallFlow(REF, clips=ALL, ai_conversation=True)
    f.answered()
    f.utterance("네", turn=f.turn, event_id="e1")
    f.utterance("음 그게", turn=f.turn, event_id="e2")
    acts = f.ai_failed(turn=f.turn)
    assert [a.key for a in acts if isinstance(a, Play)] == ["ask_available_date"] and not f.finished


def test_conversation_mode_opening_no_is_not_hangup():
    from aicc_core.flow import StartAI
    f = CallFlow(REF, clips=ALL, ai_conversation=True)
    f.answered()
    acts = f.utterance("아닌데요", turn=f.turn, event_id="e1")
    assert isinstance(acts[0], StartAI) and not f.finished


def test_wrong_person_variants():
    d = D()
    assert d.say("나 간병이 아닌데?") == ["wrong_person"]


def test_work_issue_promises_followup_marks_review_and_continues():
    d = D()
    d.say("네")
    assert d.say("근데 저번 입금이 아직 안 됐어요") == ["sc_custom_pay_review"]
    assert "PAY_REVIEW" in d.f.review_reasons and d.f.state == "WAIT_AVAILABILITY" and not d.f.finished
    assert d.say("다음 주 월요일부터요") == ["confirm_date"]


def test_working_but_end_unknown_asks_to_call_back():
    d = D()
    d.say("네")
    d.say("지금 일하고 있어요")
    assert d.say("언제 끝날지 모르겠어요") == ["sc_end_unknown"]
    assert d.f.outcome == "CALLBACK_REQUESTED" and "END_DATE_UNKNOWN" in d.f.review_reasons


def test_honorific_is_seonsaengnim():
    from aicc_core.scenarios import CURRENT_TEXTS
    assert all("간병사님" not in t for t in CURRENT_TEXTS.values())
    assert CURRENT_TEXTS["opening"].startswith("안녕하세요, 선생님")


def test_anytime_and_review_closings_when_recorded():
    d = D(clips=ALL | {"closing_anytime", "closing_review"})
    d.say("네")
    assert d.say("언제든 가능해요") == ["closing_anytime"]
    d = D(clips=ALL | {"closing_anytime", "closing_review"})
    d.say("네")
    d.say("지금 근무 중이에요")
    assert d.say("11월 8일에 끝나요") == ["sc_work_end_date"]
    play = [a for a in d.actions if isinstance(a, Play)][-1]
    assert play.params["month"] == 11 and play.params["day"] == 8
