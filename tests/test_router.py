"""AI 녹음 선택(2026-10-30): 못 알아들은 말 → 녹음된 답 중 고르기."""

from datetime import date

from aicc_agent.router import MockRouter, build_candidates, build_router_prompt, parse_choice
from aicc_core.flow import CallFlow, HangUp, Play, StartAI
from aicc_core.scenarios import SCENARIO_KEYS, SCENARIOS

REF = date(2026, 10, 30)
ALL = set(SCENARIO_KEYS)


def test_parse_choice_only_accepts_listed_codes():
    codes = {"FAQ_PATIENT_AGE", "ASK_PAY"}
    assert parse_choice('{"code": "FAQ_PATIENT_AGE", "why": "나이"}', codes) == "FAQ_PATIENT_AGE"
    assert parse_choice('```json\n{"code":"ASK_PAY"}\n```', codes) == "ASK_PAY"
    assert parse_choice('{"code": "NONE"}', codes) is None
    assert parse_choice('{"code": "FAQ_MADE_UP"}', codes) is None  # 목록 밖 코드는 버림
    assert parse_choice("", codes) is None


def test_candidates_only_recorded_answer_kinds():
    c = build_candidates(SCENARIOS, {"sc_ask_pay", "sc_faq_patient_age", "sc_silence_check", "opening"})
    assert {x for x, _ in c} == {"ASK_PAY", "FAQ_PATIENT_AGE"}
    p = build_router_prompt("우리 엄마 나이가 많은데 괜찮아요?", "다음 근무를 언제부터", ["상담원: 언제부터 가능하세요?"], c)
    assert "FAQ_PATIENT_AGE:" in p and "우리 엄마" in p


def _flow_to_ai(text):
    f = CallFlow(REF, clips=ALL, ai_conversation=True)
    f.answered()
    f.utterance("네", turn=f.turn, event_id="y")
    acts = f.utterance(text, turn=f.turn, event_id="q")
    assert any(isinstance(a, StartAI) for a in acts), "이 말은 규칙으로 못 알아들어 AI 로 가야 함"
    return f


def test_routed_info_answer_returns_to_question_stage():
    f = _flow_to_ai("엘리베이터는 있나요")
    acts = f.ai_routed("FAQ_HOSPITAL_WHERE", turn=f.turn, event_id="r")
    assert [a.key for a in acts if isinstance(a, Play)] == ["sc_faq_hospital_where"]
    assert f.state == "WAIT_AVAILABILITY" and {"event": "AI_ROUTED", "code": "FAQ_HOSPITAL_WHERE"} in f.obs.history


def test_routed_end_answer_ends_call():
    f = _flow_to_ai("음 그게 좀 애매한데 이것저것 하느라")
    acts = f.ai_routed("FAQ_NOT_DECIDED_FAMILY", turn=f.turn, event_id="r")
    assert any(isinstance(a, HangUp) for a in acts) and f.finished


def test_unknown_or_unrecorded_code_falls_back_to_ai():
    f = _flow_to_ai("엘리베이터는 있나요")
    assert f.ai_routed("FAQ_NOPE", turn=f.turn, event_id="r") is None
    assert f.state == "AI_EXCEPTION"  # 기존 AI 답으로 계속
    g = CallFlow(REF, clips={k for k in ALL if k != "sc_faq_hospital_where"}, ai_conversation=True)
    g.answered()
    g.utterance("네", turn=g.turn, event_id="y")
    g.utterance("엘리베이터는 있나요", turn=g.turn, event_id="q")
    assert g.ai_routed("FAQ_HOSPITAL_WHERE", turn=g.turn, event_id="r") is None and g.state == "AI_EXCEPTION"


def test_mock_router():
    r = MockRouter("FAQ_PATIENT_AGE")
    assert r.choose("어르신 연세가?", "g", [], [("FAQ_PATIENT_AGE", "x")]) == "FAQ_PATIENT_AGE"
    assert MockRouter(None).choose("x", "g", [], [("A", "x")]) is None
