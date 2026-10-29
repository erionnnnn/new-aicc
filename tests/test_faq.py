"""예상 질문 200개: 예시 말이 모두 해당 항목으로 연결되는지, 문구 원칙을 지키는지."""

from datetime import date

import pytest
from aicc_core.faq import FAQS
from aicc_core.flow import CallFlow, Play, StartAI
from aicc_core.scenarios import SCENARIO_KEYS, match

REF = date(2026, 10, 29)
ALL = set(SCENARIO_KEYS)


def test_two_hundred_unique():
    assert len(FAQS) >= 200
    assert len({f.code for f in FAQS}) == len(FAQS)
    assert all(f.examples for f in FAQS)


@pytest.mark.parametrize("faq", FAQS, ids=lambda f: f.code)
def test_examples_route_to_recorded_answer(faq):
    """예시 말 → 기존 시나리오 또는 이 항목(어느 쪽이든 녹음 답, AI 아님)."""
    for ex in faq.examples:
        m = match(ex)
        assert m is not None, f"{faq.code}: '{ex}' 에 맞는 녹음이 없음"


def test_most_examples_hit_their_own_faq():
    own = sum(1 for f in FAQS for ex in f.examples if (m := match(ex)) and m.code == "FAQ_" + f.code)
    total = sum(len(f.examples) for f in FAQS)
    assert own / total >= 0.85, (own, total)


def test_texts_follow_rules():
    banned = ("만원", "%", "퍼센트", "무조건", "보장해")
    for f in FAQS:
        assert "간병사님" not in f.text  # 호칭은 '선생님'
        assert not any(b in f.text for b in banned), f.code  # 확정 안 된 금액·약속 금지
        assert len(f.text) <= 120, f.code
        assert f.kind in ("INFO", "REVIEW", "PREF", "END")
        if f.kind == "END":
            assert f.outcome, f.code


def test_live_call_examples_2026_10_29():
    """실통화에서 AI 로 넘어가 침묵했던 말 → 녹음으로 즉답."""
    for text, key in (("환자분이 몇 살이에요?", "sc_faq_patient_age"),
                      ("네, 다음에 연락드릴게요.", "sc_faq_will_call_back"),
                      ("좋은간병 일 많아요?", "sc_faq_job_volume"),  # 2026-10-29 11:32 실통화
                      ("여보세요? 일 언제부터 줄 수 있어요?", "sc_faq_when_job_comes")):
        f = CallFlow(REF, clips=ALL, ai_conversation=True)
        f.answered()
        f.utterance("네", turn=f.turn, event_id="y")
        acts = f.utterance(text, turn=f.turn, event_id="q")
        assert not any(isinstance(a, StartAI) for a in acts), text
        assert key in [a.key for a in acts if isinstance(a, Play)], (text, acts)


def test_dates_still_win_over_faq():
    f = CallFlow(REF, clips=ALL, ai_conversation=True)
    f.answered()
    acts = f.utterance("아마 다음 주 월요일부터 될 것 같아요", turn=f.turn, event_id="d")
    assert "confirm_date" in [a.key for a in acts if isinstance(a, Play)]


def test_without_recording_falls_back_to_ai():
    base = {k for k in ALL if not k.startswith("sc_faq_")}
    f = CallFlow(REF, clips=base, ai_conversation=True)
    f.answered()
    acts = f.utterance("환자분이 몇 살이에요?", turn=f.turn, event_id="q")
    assert any(isinstance(a, StartAI) for a in acts)


def test_recording_delete_is_not_contact_block():
    from aicc_core.flow import BlockContact
    f = CallFlow(REF, clips=ALL, ai_conversation=True)
    f.answered()
    acts = f.utterance("통화 내용 삭제해 주세요", turn=f.turn, event_id="r")
    assert not any(isinstance(a, BlockContact) for a in acts)
    f2 = CallFlow(REF, clips=ALL, ai_conversation=True)
    f2.answered()
    acts = f2.utterance("제 번호 삭제해 주세요", turn=f2.turn, event_id="r")
    assert any(isinstance(a, BlockContact) for a in acts)


def test_cannot_work_ends_call():
    """2026-10-29 실통화: '나 일 못해요' 에도 끊지 않고 대화를 이어감 → 정중히 마무리."""
    from aicc_core.flow import HangUp
    f = CallFlow(REF, clips=ALL, ai_conversation=True)
    f.answered()
    acts = f.utterance("나 일 못해요", turn=f.turn, event_id="c")
    assert any(isinstance(a, HangUp) for a in acts) and f.finished and f.outcome == "UNCONFIRMED"
    f2 = CallFlow(REF, clips=ALL, ai_conversation=True)
    f2.answered()
    acts = f2.utterance("지금은 일 못 해요, 다음 주 월요일부터 돼요", turn=f2.turn, event_id="d")
    assert "confirm_date" in [a.key for a in acts if isinstance(a, Play)]  # 날짜가 있으면 날짜 우선
