"""통화로 시나리오를 만들고 고치는 기능(2026-10-30): 기록 → 제안 → 자동 검사 → 승인 → 통화에 반영."""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone

from aicc_api import learning
from aicc_core.flow import CallFlow, Play, StartAI
from aicc_core.scenario_set import ScenarioSet
from aicc_core.scenarios import SCENARIO_KEYS
from test_e2e_mock_calls import make_agent, run_all, setup_mock, start

REF = date(2026, 10, 30)


def _seed_real_call(admin):
    """MOCK 캠페인을 한 번 돌린 뒤, 그 통화 하나를 실통화처럼 바꾸고 'AI 로 넘어간 말' 이벤트를 붙인다."""
    from aicc_api.db import SessionLocal
    from aicc_api.models import CallEvent, CallSession
    _, dev, camp, _ = setup_mock(admin)
    agent = make_agent(admin, dev)
    agent.heartbeat()
    start(admin, camp["campaign_id"], dev["id"])
    run_all(agent, limit=1)
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        call = db.query(CallSession).first()
        call.is_mock = False
        seq = 1000

        def ev(t, payload):
            nonlocal seq
            seq += 1
            db.add(CallEvent(id=str(uuid.uuid4()), call_id=call.id, seq=seq, turn_id=1, type=t, payload=payload,
                             occurred_at=now))
        for text in ("엘리베이터는 있나요", "엘리베이터 있어요?"):
            ev("utterance.final", {"text": text, "state": "ASK_AVAILABLE_DATE"})
            ev("ai.start", {"return_state": "ASK_AVAILABLE_DATE"})
            ev("ai.done", {"text": "…"})
        ev("utterance.final", {"text": "거기 동네가 어디예요", "state": "ASK_AVAILABLE_DATE"})
        ev("ai.start", {"return_state": "ASK_AVAILABLE_DATE"})
        ev("ai.routed", {"code": "FAQ_HOSPITAL_WHERE"})
        ev("play", {"key": "sc_faq_hospital_where"})
        ev("call.remote_hangup", {})
        org_id = call.organization_id
        db.commit()
    return org_id


def fake_llm(prompt: str) -> str:
    assert "엘리베이터는 있나요" in prompt and "FAQ_PATIENT_AGE" in prompt  # 못 알아들은 말 + 기존 답 목록
    return json.dumps({"proposals": [
        {"action": "NEW", "code_hint": "ELEVATOR", "kind": "INFO", "examples": [0, 1],
         "text": "병원 시설은 실제 일감 소개드릴 때 정확하게 안내드릴게요. 언제부터 근무 가능하세요?", "why": "시설 질문"},
        {"action": "NEW", "code_hint": "BAD", "kind": "INFO", "examples": [0],
         "text": "하루 20만원 보장해 드려요. 언제부터 가능하세요?"},             # 금지 표현 → 검사 불합격
        {"action": "ADD_EXAMPLES", "target_code": "FAQ_NOT_EXIST", "examples": [0]},  # 없는 코드 → 버림
    ]}, ensure_ascii=False)


def test_collect_finds_unmatched_and_hangups(admin):
    org_id = _seed_real_call(admin)
    from aicc_api.db import SessionLocal
    with SessionLocal() as db:
        c = learning.collect(db, org_id, days=7)
    texts = [x["text"] for x in c["unmatched"]]
    assert "엘리베이터는 있나요" in texts and "거기 동네가 어디예요" in texts
    assert next(x for x in c["unmatched"] if x["text"] == "거기 동네가 어디예요")["routed"] == "FAQ_HOSPITAL_WHERE"
    assert c["plays"]["sc_faq_hospital_where"] == 1 and c["hangup_after"]["sc_faq_hospital_where"] == 1


def test_full_learning_loop(admin, monkeypatch):
    _seed_real_call(admin)
    monkeypatch.setattr(learning, "gemini_llm", fake_llm)
    r = admin.post("/api/v1/scenario-proposals/generate", json={"days": 7})
    assert r.status_code == 200, r.text
    props = admin.get("/api/v1/scenario-proposals").json()
    by = {(p["action"], p["target_code"]): p for p in props}
    add = by[("ADD_EXAMPLES", "FAQ_HOSPITAL_WHERE")]  # AI 가 고른 답 → 표현 추가(글 모델 없이)
    new = next(p for p in props if p["action"] == "NEW" and p["draft"]["code"] == "LRN_ELEVATOR")
    bad = next(p for p in props if p["action"] == "NEW" and p["draft"]["code"] == "LRN_BAD")
    assert len(props) == 3
    assert new["checks"]["ok"] and new["checks"]["corpus"]["after"] == new["checks"]["corpus"]["before"]
    assert not bad["checks"]["ok"] and any("보장" in e for e in bad["checks"]["errors"])
    assert admin.post(f"/api/v1/scenario-proposals/{bad['id']}/approve", json={}).status_code == 409
    # 문구를 고치면 다시 검사해서 승인 가능
    fixed = {"text": "금액은 실제 일감 소개드릴 때 정확하게 안내드릴게요. 언제부터 가능하세요?"}
    rc = admin.post(f"/api/v1/scenario-proposals/{bad['id']}/recheck", json={"edits": fixed}).json()
    assert rc["checks"]["ok"]
    assert admin.post(f"/api/v1/scenario-proposals/{add['id']}/approve", json={}).status_code == 200
    ra = admin.post(f"/api/v1/scenario-proposals/{new['id']}/approve", json={})
    assert ra.status_code == 200 and ra.json()["code"] == "LRN_ELEVATOR"
    assert "female" in ra.json()["voice"]  # 음원팩에 칸이 생김(녹음 엔진이 없으면 '녹음 필요')
    pack = next(p for p in admin.get("/api/v1/voice-packs").json() if p["key"] == "female")
    assert "sc_lrn_elevator" in pack["clips"]
    # 같은 제안을 다시 만들지 않음
    assert admin.post("/api/v1/scenario-proposals/generate", json={"days": 7}).json()["made"] == 0
    # 답 목록·끄기
    cat = {s["code"]: s for s in admin.get("/api/v1/scenarios").json()}
    assert cat["LRN_ELEVATOR"]["source"] == "learned" and "거기 동네가 어디예요" in cat["FAQ_HOSPITAL_WHERE"]["examples"]
    assert admin.put("/api/v1/scenarios/FAQ_PATIENT_AGE", json={"enabled": False}).status_code == 200
    from aicc_api.db import SessionLocal
    org_id = admin.get("/api/v1/auth/me").json()["organization"]["id"]
    with SessionLocal() as db:
        ext = learning.scenario_ext(db, org_id)
    assert "FAQ_PATIENT_AGE" in ext["disabled"] and ext["learned"][0]["code"] == "LRN_ELEVATOR"
    # 통화에 반영: 배운 답은 녹음이 있으면 바로 재생, 끈 답은 쓰지 않음
    clips = set(SCENARIO_KEYS) | {"sc_lrn_elevator"}
    f = CallFlow(REF, clips=clips, ai_conversation=True, scenario_ext=ext)
    f.answered()
    f.utterance("네", turn=f.turn, event_id="y")
    acts = f.utterance("엘리베이터는 있나요", turn=f.turn, event_id="q")
    assert [a.key for a in acts if isinstance(a, Play)] == ["sc_lrn_elevator"]
    acts = f.utterance("거기 동네가 어디예요", turn=f.turn, event_id="h")
    assert [a.key for a in acts if isinstance(a, Play)] == ["sc_faq_hospital_where"]
    g = CallFlow(REF, clips=clips, ai_conversation=True, scenario_ext=ext)
    g.answered()
    acts = g.utterance("환자분이 몇 살이에요?", turn=g.turn, event_id="a")
    assert "sc_faq_patient_age" not in [a.key for a in acts if isinstance(a, Play)]
    # 효과 통계
    st = admin.get("/api/v1/scenarios/stats?days=7").json()
    assert st["ai"]["total"] == 3 and st["ai"]["routed"] == 1
    assert next(a for a in st["answers"] if a["key"] == "sc_faq_hospital_where")["hangup_rate"] == 100.0


def test_claim_carries_scenarios(admin):
    _, dev, camp, _ = setup_mock(admin)
    agent = make_agent(admin, dev)
    agent.heartbeat()
    start(admin, camp["campaign_id"], dev["id"])
    claim = agent.client.claim()
    assert set(claim["scenarios"]) == {"learned", "disabled", "extra_keywords", "examples"}


def test_auto_apply_only_examples(admin, monkeypatch):
    _seed_real_call(admin)
    admin.put("/api/v1/settings", json={"policies": {"auto_apply_examples": True}})
    monkeypatch.setattr(learning, "gemini_llm", fake_llm)
    admin.post("/api/v1/scenario-proposals/generate", json={"days": 7})
    done = admin.get("/api/v1/scenario-proposals?status=APPROVED").json()
    pend = admin.get("/api/v1/scenario-proposals?status=PENDING").json()
    assert [p["action"] for p in done] == ["ADD_EXAMPLES"]  # 표현 추가만 자동
    assert all(p["action"] == "NEW" for p in pend)


def test_scenario_set_ignores_bad_ext():
    s = ScenarioSet({"learned": [{"code": "NOT_PREFIXED", "kind": "INFO", "text": "x"},
                                 {"code": "LRN_END_NO_OUTCOME", "kind": "END", "text": "x"},
                                 {"code": "LRN_OK", "kind": "INFO", "text": "좋아요", "keywords": ["[", "엘리베이터"]}],
                     "disabled": ["SILENCE_CHECK"], "extra_keywords": {"FAQ_PATIENT_AGE": ["("]}})
    assert [x.code for x in s.learned] == ["LRN_OK"] and s.by_code["LRN_OK"].keywords == ("엘리베이터",)
    assert "SILENCE_CHECK" in s.by_code  # 흐름용 안내는 끌 수 없음
    flow = CallFlow(REF, clips=set(SCENARIO_KEYS), ai_conversation=True)
    flow.answered()
    assert any(isinstance(a, StartAI) for a in flow.utterance("엘리베이터는 있나요", turn=flow.turn, event_id="z"))


def test_review_fixes_2026_10_30(admin, monkeypatch):
    """코드 리뷰 반영: 차단 답에 표현 추가 금지, 네/아니요·날짜 같은 말은 표현으로 못 씀, 빈 칸만 생기면 음원 승인 유지."""
    _seed_real_call(admin)

    def llm(prompt):
        return json.dumps({"proposals": [
            {"action": "ADD_EXAMPLES", "target_code": "CUSTOM_DELETE_REVIEW", "examples": [0]},   # 연락처 삭제(차단) → 버림
            {"action": "NEW", "code_hint": "OK_SURE", "kind": "INFO", "examples": [0, 1],
             "text": "네, 시설은 실제 일감 소개 때 안내드릴게요. 언제부터 가능하세요?"}]})
    monkeypatch.setattr(learning, "gemini_llm", llm)
    admin.post("/api/v1/scenario-proposals/generate", json={"days": 7})
    props = admin.get("/api/v1/scenario-proposals").json()
    assert all(p["target_code"] != "CUSTOM_DELETE_REVIEW" for p in props)
    new = next(p for p in props if p["action"] == "NEW")
    bad = admin.post(f"/api/v1/scenario-proposals/{new['id']}/recheck",
                     json={"edits": {"examples": ["네 알겠어요", "엘리베이터는 있나요"]}}).json()
    assert not bad["checks"]["ok"] and any("단순" in e for e in bad["checks"]["errors"])
    # 음원팩이 승인된 상태에서, 녹음 엔진이 없어 빈 칸만 생기면 승인 유지
    admin.put("/api/v1/settings", json={"company_name": "좋은간병"})
    admin.put("/api/v1/voice-packs/female", json={"speaker_name": "김태희"})
    admin.post("/api/v1/voice-packs/female/tts", json={"engine": "test"})
    assert admin.post("/api/v1/voice-packs/female/approve").status_code == 200
    r = admin.post(f"/api/v1/scenario-proposals/{new['id']}/approve", json={})
    assert r.status_code == 200 and "녹음 필요" in r.json()["voice"]["female"]
    pack = next(p for p in admin.get("/api/v1/voice-packs").json() if p["key"] == "female")
    assert pack["approved_by"] and "sc_lrn_ok_sure" in pack["clips"]


def test_generate_skips_answered_greeting_plain_2026_11_06(admin, monkeypatch):
    """실사용 제안 3건이 모두 불합격: 이미 답이 있는 말·'여보세요.'·'네, 다음에 연락드릴게요.'가 재료로 들어감."""
    from aicc_api.db import SessionLocal
    from aicc_api.models import CallEvent, CallSession, ScenarioProposal
    org_id = _seed_real_call(admin)
    with SessionLocal() as db:
        call = db.query(CallSession).filter(CallSession.is_mock.is_(False)).first()
        for i, text in enumerate(("여보세요? 예. 아, 제가 지금 다리 수술해서 못 해요.", "여보세요.", "네, 다음에 연락드릴게요.",
                                  "환자분이 몇 살이에요?")):
            db.add(CallEvent(id=str(uuid.uuid4()), call_id=call.id, seq=2000 + i * 2, turn_id=1, type="utterance.final",
                             payload={"text": text, "state": "ASK_AVAILABLE_DATE"}, occurred_at=datetime.now(timezone.utc)))
            db.add(CallEvent(id=str(uuid.uuid4()), call_id=call.id, seq=2001 + i * 2, turn_id=1, type="ai.start",
                             payload={"return_state": "ASK_AVAILABLE_DATE"}, occurred_at=datetime.now(timezone.utc)))
        # 예전에 만들어진 불량 제안(근거가 인사뿐)
        db.add(ScenarioProposal(organization_id=org_id, status="PENDING", action="NEW", target_code="LRN_SCREEN_INTRO",
                                draft={"code": "LRN_SCREEN_INTRO", "kind": "INFO", "text": "x", "examples": ["여보세요."]},
                                evidence={}, checks={}))
        db.commit()
    seen = {}

    def llm(prompt: str) -> str:
        seen["prompt"] = prompt
        return json.dumps({"proposals": [
            {"action": "NEW", "code_hint": "INTRO", "kind": "INFO", "examples": [0],
             "text": "안녕하세요, 좋은간병에서 근무 일정 확인드리는 안내입니다. 언제부터 가능하세요?"}]}, ensure_ascii=False)
    monkeypatch.setattr(learning, "gemini_llm", llm)
    assert admin.post("/api/v1/scenario-proposals/generate", json={"days": 7}).status_code == 200
    p = seen["prompt"]
    assert "엘리베이터는 있나요" in p  # 진짜 모르는 말은 그대로
    for t in ("다리 수술", "여보세요.", "다음에 연락드릴게요", "몇 살이에요"):
        assert t not in p.split("할 일:")[0].split("이미 있는 녹음 답")[0], t
    props = admin.get("/api/v1/scenario-proposals?status=ALL").json()
    old = next(x for x in props if x["target_code"] == "LRN_SCREEN_INTRO")
    assert old["status"] == "REJECTED" and "자동 정리" in old["decision_note"]
    intro = next(x for x in props if x["action"] == "NEW" and x["draft"]["code"] == "LRN_INTRO")
    assert not intro["checks"]["ok"] and any("첫 인사" in e for e in intro["checks"]["errors"])


def test_strip_filler():
    assert learning.strip_filler("여보세요? 예. 아, 제가 지금 다리 수술해서 못 해요.") == "제가 지금 다리 수술해서 못 해요."
    assert learning.strip_filler("네") == "네"
