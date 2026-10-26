"""MOCK 단말 종단 시험: 서버 API + 에이전트 런너 + 상태기계 + 녹음 + 내보내기.

실제 전화·실제 음성 인식이 아니다. T04~T07 실기기 항목은 여기서 판정하지 않는다.
대상: T08~T12, T14, T15(MOCK 분리), T16(AI 실패), T17(중복), T20(대사).
"""

from __future__ import annotations

import io

import pytest
from aicc_agent.agent import Agent
from aicc_agent.ai import FailingAI, MockAI
from aicc_agent.client import AgentClient, ApiError
from aicc_agent.device import DeviceError
from aicc_agent.mock_device import MockDevice
from aicc_agent.stt import MockSTT
from conftest import make_user, upload_commit
from make_fixtures import clean_roster_bytes, roster_bytes
from openpyxl import load_workbook


def setup_mock(admin, roster=None, semantics="NEXT_AVAILABLE"):
    res = upload_commit(admin, roster or clean_roster_bytes(), semantics)
    packs = admin.post("/api/v1/voice-packs/init").json()
    female = next(p for p in packs if p["key"] == "female")
    dev = admin.post("/api/v1/devices", json={"name": "MOCK-1", "kind": "mock"}).json()
    items = admin.get(f"/api/v1/caregivers?batch_id={res['batch_id']}&status=dialable").json()["items"]
    camp = admin.post("/api/v1/campaigns", json={"batch_id": res["batch_id"], "voice_pack_id": female["id"],
                                                 "target_ids": [i["caregiver_id"] for i in items]}, idem=True).json()
    return res, dev, camp, items


def make_agent(admin, dev, device=None, ai=None):
    client = AgentClient(admin.c, dev["token"])
    return Agent(client, device or MockDevice(), MockSTT(), ai or MockAI())


def start(admin, camp_id, dev_id):
    c = admin.get(f"/api/v1/campaigns/{camp_id}").json()
    return admin.post(f"/api/v1/campaigns/{camp_id}/start", json={"device_id": dev_id, "expected_version": c["version"]},
                      idem=True)


def run_all(agent, limit=30):
    out = []
    for _ in range(limit):
        r = agent.step()
        if r.status != "CALLED":
            break
        out.append(r.summary)
    return out


def test_full_mock_campaign_and_export_reconciliation(admin):
    res, dev, camp, items = setup_mock(admin)
    agent = make_agent(admin, dev)
    agent.heartbeat()  # 연결 신호
    r = start(admin, camp["campaign_id"], dev["id"])
    assert r.status_code == 202, r.text
    summaries = run_all(agent)
    outcomes = {s.outcome for s in summaries}
    # 합성 번호 끝자리 페르소나: 1(근무·종료일·가능일), 2(정정), 6(통화곤란), 9(무응답 후 끊음), 0(상시 가능)
    assert len(summaries) == 5
    assert {"CONFIRMED", "CALLBACK_REQUESTED", "ANYTIME"} <= outcomes
    camp_now = admin.get(f"/api/v1/campaigns/{camp['campaign_id']}").json()
    assert camp_now["state"] == "COMPLETED"
    # 일정 반영: 00123(…0001) → 2026 기준 '26일부터' 확정, 00124(…0002) → 정정 후 27일
    cgs = {i["external_no"]: i for i in admin.get(f"/api/v1/caregivers?batch_id={res['batch_id']}").json()["items"]}
    assert cgs["00123"]["confirmed_available_date"].endswith("-26")
    assert cgs["00124"]["confirmed_available_date"].endswith("-27")
    # 통화곤란·상시가능은 기존 날짜 유지
    assert cgs["00132"]["confirmed_available_date"] == "2026-11-15"
    # 녹음: 통화별 2채널 WAV 저장, 재생 권한
    recs = admin.get("/api/v1/recordings").json()
    assert recs and all(r["state"] == "READY" and r["is_mock"] for r in recs)
    pb = admin.post(f"/api/v1/recordings/{recs[0]['id']}/playback").json()
    audio = admin.get(pb["url"])
    assert audio.status_code == 200 and audio.content[:4] == b"RIFF"
    op = make_user(admin, "opx", "operator")
    assert op.post(f"/api/v1/recordings/{recs[0]['id']}/playback").status_code == 403
    # 내보내기 대사(T20)
    e = admin.post("/api/v1/exports", json={"campaign_id": camp["campaign_id"]}, idem=True).json()
    wb = load_workbook(io.BytesIO(admin.get(f"/api/v1/exports/{e['export_id']}/download").content))
    summ = {wb["요약"].cell(i, 1).value: wb["요약"].cell(i, 2).value for i in range(1, wb["요약"].max_row + 1)}
    assert summ["통화 시도 수"] == 5 and summ["캠페인 대상 수"] == 5
    assert summ["일정 확인완료 통화 수"] == 2 and summ["날짜 변경 건수"] == 2
    assert summ["데이터구분"].startswith("MOCK")
    assert wb["통화이력"].max_row == 6 and wb["변경이력"].max_row == 3
    ws = wb["명부"]
    hdr = [c.value for c in ws[3]]
    col_date = hdr.index("근무확정일") + 1
    row_123 = next(r for r in range(4, ws.max_row + 1) if ws.cell(r, 1).value == "00123")
    assert ws.cell(row_123, col_date).value.day == 26
    assert ws.cell(row_123, hdr.index("확인상태") + 1).value == "통화 확인완료"
    assert ws.cell(row_123, hdr.index("데이터구분") + 1).value.startswith("MOCK")


def test_refusal_blocks_and_never_redials(admin):
    rows = [["00201", "거부자", "010-2000-0003", None, "", ""]]
    res, dev, camp, items = setup_mock(admin, roster_bytes(rows))
    agent = make_agent(admin, dev)
    agent.heartbeat()
    start(admin, camp["campaign_id"], dev["id"])
    s = run_all(agent)
    assert s[0].outcome == "REFUSED"
    blocks = admin.get("/api/v1/contact-blocks").json()
    assert len(blocks) == 1 and blocks[0]["source_call_id"]
    # 같은 번호 재업로드 → 새 캠페인 생성 불가(발신 0건)
    res2 = upload_commit(admin, roster_bytes([["00999", "다른이름", "010-2000-0003", None, "", ""]]))
    items2 = admin.get(f"/api/v1/caregivers?batch_id={res2['batch_id']}").json()["items"]
    packs = admin.get("/api/v1/voice-packs").json()
    r = admin.post("/api/v1/campaigns", json={"batch_id": res2["batch_id"], "voice_pack_id": packs[0]["id"],
                                              "target_ids": [items2[0]["caregiver_id"]]}, idem=True)
    assert r.status_code == 422


def test_block_between_queue_and_claim(admin):
    """큐 등록 후 발신 직전 차단(설계 09: 발신권 획득 때 재확인)."""
    res, dev, camp, items = setup_mock(admin)
    agent = make_agent(admin, dev)
    agent.heartbeat()
    start(admin, camp["campaign_id"], dev["id"])
    first = items[0]
    admin.post("/api/v1/contact-blocks", json={"caregiver_id": first["caregiver_id"], "reason": "전화로 거부 접수"})
    s = run_all(agent)
    device: MockDevice = agent.device
    assert "01010000001" not in device.dialed
    assert len(s) == 4


def test_t15_device_disconnect_pauses_without_redial(admin):
    res, dev, camp, items = setup_mock(admin)
    device = MockDevice()
    agent = make_agent(admin, dev, device=device)
    agent.heartbeat()
    start(admin, camp["campaign_id"], dev["id"])
    orig = device.read_rx_frames
    calls = {"n": 0}

    def flaky(ms):
        calls["n"] += 1
        if calls["n"] == 25:
            device.connected = False
        return orig(ms)
    device.read_rx_frames = flaky
    r = agent.step()
    assert r.summary.outcome == "DEVICE_ERROR"
    assert admin.get(f"/api/v1/campaigns/{camp['campaign_id']}").json()["state"] == "PAUSED"
    # 연결이 끊긴 단말은 발신권을 풀지 않는다(IDLE 확인 전)
    assert agent.step().status == "WAIT_DEVICE_IDLE"
    assert len(device.dialed) == 1
    # 연결 복구 + IDLE 확인 후에도 운영자 재개 전에는 발신하지 않음
    device.connected = True
    device.hangup()
    assert agent.step().status in ("IDLE", "WAIT_DEVICE_IDLE")
    assert len(device.dialed) == 1


def test_t16_ai_failure_does_not_store_date(admin):
    rows = [["00301", "질문자", "010-3000-0005", None, "", ""]]
    res, dev, camp, items = setup_mock(admin, roster_bytes(rows))
    agent = make_agent(admin, dev, ai=FailingAI())
    agent.runner.cfg.ai_wait_after_ms = 1
    agent.heartbeat()
    start(admin, camp["campaign_id"], dev["id"])
    s = run_all(agent)
    assert s[0].outcome == "CONSULT_REQUESTED"
    cg = admin.get(f"/api/v1/caregivers?batch_id={res['batch_id']}").json()["items"][0]
    assert cg["confirmed_available_date"] is None
    call = admin.get("/api/v1/reviews").json()[0]
    assert call["review_state"] == "PENDING" and "AI_FAILED" in call["review_reasons"]


def test_ai_exception_then_ars_resumes(admin):
    rows = [["00302", "질문자2", "010-3000-0015", None, "", ""]]
    res, dev, camp, items = setup_mock(admin, roster_bytes(rows))
    agent = make_agent(admin, dev)
    agent.heartbeat()
    start(admin, camp["campaign_id"], dev["id"])
    s = run_all(agent)
    assert s[0].outcome == "CONFIRMED"
    detail = admin.get(f"/api/v1/calls/{s[0].call_id}").json()
    types = [e["type"] for e in detail["events"]]
    assert types.count("ai.start") == 1 and "ai.done" in types
    plays = [e["payload"]["key"] for e in detail["events"] if e["type"] == "play"]
    assert plays.count("opening") == 1  # 오프닝 1회(R04)
    assert plays[:3] == ["opening", "ask_working", "ask_working"]  # AI 응답 후 원래 질문 복귀


def test_t17_confirmation_idempotent_and_requires_consent(admin):
    rows = [["00401", "확인자", "010-4000-0001", None, "", ""]]
    res, dev, camp, items = setup_mock(admin, roster_bytes(rows))
    agent = make_agent(admin, dev)
    agent.heartbeat()
    start(admin, camp["campaign_id"], dev["id"])
    s = run_all(agent)[0]
    detail = admin.get(f"/api/v1/calls/{s.call_id}").json()
    yes_ev = [e for e in detail["events"] if e["type"] == "utterance.final"][-1]
    client = agent.client
    again = client.confirm(s.call_id, yes_ev["id"])  # 같은 키 재전송
    assert again["status"] in ("APPLIED", "ALREADY_APPLIED")
    changes = admin.get(f"/api/v1/caregivers/{items[0]['caregiver_id']}/changes").json()
    assert len(changes) == 1
    # 동의가 아닌 발화를 근거로 확정 시도 → 거부
    not_yes = [e for e in detail["events"] if e["type"] == "utterance.final"][-2]
    with pytest.raises(ApiError) as ei:
        client._req("POST", f"/api/v1/calls/{s.call_id}/schedule-confirmations",
                    json_body={"evidence_event_id": not_yes["id"]}, idem="manual-1")
    assert ei.value.code in ("CANDIDATE_INVALID", "NOT_EXPLICIT_CONSENT", "TURN_MISMATCH")


def test_production_org_requires_verified_device(app_client):
    app_client.post("/api/v1/auth/bootstrap", json={"org_name": "운영", "environment": "production",
                                                    "username": "boss", "password": "boss-pass-123"})
    from conftest import login
    admin = login(app_client, "boss", "boss-pass-123")
    assert admin.post("/api/v1/devices", json={"name": "m", "kind": "mock"}).status_code == 409
    res = upload_commit(admin, clean_roster_bytes())
    packs = admin.post("/api/v1/voice-packs/init").json()
    dev = admin.post("/api/v1/devices", json={"name": "A24", "kind": "a24"}).json()
    items = admin.get(f"/api/v1/caregivers?batch_id={res['batch_id']}&status=dialable").json()["items"]
    camp = admin.post("/api/v1/campaigns", json={"batch_id": res["batch_id"], "voice_pack_id": packs[0]["id"],
                                                 "target_ids": [items[0]["caregiver_id"]]}, idem=True).json()
    assert camp["is_mock"] is False
    pf = admin.get(f"/api/v1/campaigns/{camp['campaign_id']}/preflight?device_id={dev['id']}").json()
    assert not pf["ok"]
    failing = {c["key"] for c in pf["checks"] if not c["ok"]}
    assert {"voice", "policy", "device"} <= failing
    r = start(admin, camp["campaign_id"], dev["id"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "DEVICE_AUDIO_UNVERIFIED"
    # 사람 검증 없이 에이전트가 'supported'를 보고해도 실통화 준비가 되지 않음
    client = AgentClient(app_client, dev["token"])
    client.heartbeat(capabilities={k: "supported" for k in ("dial", "hangup", "answer_detection", "rx_audio",
                                                              "tx_audio", "recording")}, phone_state="IDLE")
    d = admin.get("/api/v1/devices").json()[0]
    assert d["real_call_ready"] is False
    # 프로브 없이 검증 기록 불가, 10회 미만 불가
    proof = {"rx_verified": True, "tx_verified": True, "pc_silent_verified": True, "recording_verified": True,
             "test_calls": 3, "remote_listener": "시험자"}
    assert admin.post(f"/api/v1/devices/{dev['id']}/proof", json=proof).status_code == 409
    assert client.claim()["status"] == "IDLE"


def test_mock_device_raises_on_disconnect():
    d = MockDevice()
    d.connected = False
    with pytest.raises(DeviceError):
        d.get_call_state()


def test_t19_retention_expiry_purges_recordings_and_transcripts(admin):
    from datetime import timedelta

    from aicc_api import services
    from aicc_api.db import SessionLocal
    from aicc_api.models import CallEvent, CallSession, Recording

    admin.put("/api/v1/settings", json={"policies": {"retention_days": 30}})
    rows = [["00501", "보관", "010-5000-0001", None, "", ""]]
    res, dev, camp, items = setup_mock(admin, roster_bytes(rows))
    agent = make_agent(admin, dev)
    agent.heartbeat()
    start(admin, camp["campaign_id"], dev["id"])
    run_all(agent)
    with SessionLocal() as db:
        rec = db.query(Recording).one()
        assert rec.expires_at is not None
        rec.expires_at = services.utcnow() - timedelta(seconds=1)
        call = db.query(CallSession).one()
        call.ended_at = services.utcnow() - timedelta(days=31)
        db.commit()
    assert admin.post("/api/v1/admin/purge-expired").json()["expired"] >= 2
    recs = admin.get("/api/v1/recordings").json()
    assert recs[0]["state"] == "EXPIRED"
    assert admin.post(f"/api/v1/recordings/{recs[0]['id']}/playback").status_code == 409
    with SessionLocal() as db:
        texts = [e.payload.get("text") for e in db.query(CallEvent).filter(CallEvent.type == "utterance.final")]
        assert texts and all(t == "[보관기간 만료로 삭제]" for t in texts)
    # 연락거부·변경이력 자체는 유지(날짜 값)
    assert admin.get(f"/api/v1/caregivers/{items[0]['caregiver_id']}/changes").json()


def test_switch_to_production_blocks_mock_dialing(admin):
    res, dev, camp, items = setup_mock(admin)
    assert admin.put("/api/v1/settings", json={"environment": "production"}).status_code == 200
    agent = make_agent(admin, dev)
    agent.heartbeat()
    pf = admin.get(f"/api/v1/campaigns/{camp['campaign_id']}/preflight?device_id={dev['id']}").json()
    assert not pf["ok"]  # 기존 MOCK 캠페인·단말은 운영 조직에서 시작 불가
    assert agent.step().status != "CALLED"


def test_pilot_mode_calls_only_registered_test_numbers(admin):
    """운영 조직 + 실통화 시험 모드: 실기기 검증 기록 없이도 등록한 시험 번호로만 발신."""
    rows = [["00801", "시험직원", "010-8000-0001", None, "", ""], ["00802", "실제간병사", "010-8000-0002", None, "", ""]]
    admin.put("/api/v1/settings", json={"company_name": "좋은케어"})
    admin.get("/api/v1/voice-packs")
    admin.post("/api/v1/voice-packs/female/tts", json={"engine": "test"})
    pack = admin.post("/api/v1/voice-packs/female/approve").json()
    assert admin.put("/api/v1/settings", json={"environment": "production"}).status_code == 200
    res = upload_commit(admin, roster_bytes(rows))
    items = admin.get(f"/api/v1/caregivers?batch_id={res['batch_id']}").json()["items"]
    ids = [i["caregiver_id"] for i in items]
    # 시험 모드 꺼짐: 일반 실통화 → 검증 기록 없어 시작 불가
    admin.put("/api/v1/settings", json={"policies": {"pilot_enabled": True, "test_numbers": "010-8000-0001"}})
    camp = admin.post("/api/v1/campaigns", json={"batch_id": res["batch_id"], "voice_pack_id": pack["id"],
                                                 "target_ids": ids}, idem=True).json()
    assert camp["pilot"] is True and camp["accepted_count"] == 1
    assert camp["excluded_targets"][0]["reason"] == "NOT_TEST_NUMBER"
    dev = admin.post("/api/v1/devices", json={"name": "A24", "kind": "a24"}).json()
    device = MockDevice()
    agent = make_agent(admin, dev, device=device)
    agent.heartbeat()
    pf = admin.get(f"/api/v1/campaigns/{camp['campaign_id']}/preflight?device_id={dev['id']}").json()
    assert pf["ok"], pf
    assert start(admin, camp["campaign_id"], dev["id"]).status_code == 202
    s = run_all(agent)
    assert len(s) == 1 and device.dialed == ["01080000001"]
    assert "01080000002" not in device.dialed
    # 시험 모드를 끄면 남은 시험 캠페인도 발신 거부
    admin.put("/api/v1/settings", json={"policies": {"pilot_enabled": False}})
    assert agent.step().status != "CALLED"


def test_pilot_rejects_invalid_test_number(admin):
    r = admin.put("/api/v1/settings", json={"policies": {"test_numbers": "1234"}})
    assert r.status_code == 422
