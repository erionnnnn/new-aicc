"""통화 후 AI 분석(제안 전용)과 시간대별 연결률 통계."""

from __future__ import annotations

import json
from datetime import date

from aicc_agent.analysis import MockAnalyzer, build_analysis_prompt, parse_analysis
from aicc_agent.runner import RunnerConfig
from test_e2e_mock_calls import make_agent, run_all, setup_mock, start

TODAY = date(2026, 10, 28)
GOOD = json.dumps({
    "work_status": "WORKING", "end_date": "2026-11-05", "available_date": "2026-11-07",
    "preferences": ["강남 쪽", "주간만"], "issues": ["급여 문의 010-1234-5678"],
    "summary": "11월 5일 종료, 7일부터 가능", "confidence": 0.82, "evidence": "5일 끝나고 7일부터 돼요",
}, ensure_ascii=False)


def test_parse_analysis_validates_and_masks():
    a = parse_analysis("```json\n" + GOOD + "\n```", TODAY)
    assert a["work_status"] == "WORKING" and a["end_date"] == "2026-11-05" and a["available_date"] == "2026-11-07"
    assert a["preferences"] == ["강남 쪽", "주간만"]
    assert "010" not in a["issues"][0] and "[전화번호]" in a["issues"][0]
    assert a["confidence"] == 0.82


def test_parse_analysis_rejects_bad_values():
    bad = parse_analysis(json.dumps({"work_status": "MAYBE", "end_date": "11월 5일", "available_date": "2031-01-01",
                                     "confidence": "높음", "preferences": "주간"}), TODAY)
    assert bad["work_status"] == "UNKNOWN" and bad["end_date"] is None and bad["available_date"] is None
    assert bad["confidence"] == 0.0 and bad["preferences"] == []
    assert parse_analysis("모르겠어요", TODAY) is None
    assert parse_analysis("", TODAY) is None


def test_prompt_contains_reference_date_and_dialog():
    p = build_analysis_prompt(["상담원: 근무 중이세요?", "간병사: 네 5일에 끝나요"], TODAY)
    assert "2026-10-28" in p and "간병사: 네 5일에 끝나요" in p


def test_analysis_is_suggestion_only_and_shown_in_call_detail(admin):
    res, dev, camp, items = setup_mock(admin)
    agent = make_agent(admin, dev)
    an = MockAnalyzer(GOOD)
    agent.runner.analyzer = an
    agent.runner.cfg = RunnerConfig(**{**agent.runner.cfg.__dict__, "analysis_async": False})
    agent.heartbeat()
    assert start(admin, camp["campaign_id"], dev["id"]).status_code == 202
    before = {i["external_no"]: i for i in admin.get(f"/api/v1/caregivers?batch_id={res['batch_id']}").json()["items"]}
    summaries = run_all(agent)
    assert summaries and an.prompts, "간병사 말이 있는 통화는 분석해야 함"
    calls = admin.get("/api/v1/reviews?state=ALL").json()
    details = [admin.get(f"/api/v1/calls/{c['id']}").json() for c in calls]
    analysed = [d for d in details if (d.get("flags") or {}).get("ai_analysis")]
    assert analysed
    a = analysed[0]["flags"]["ai_analysis"]
    assert a["available_date"] == "2026-11-07" and a["engine"] == "mock" and a["at"]
    # 제안 전용: AI 가 말한 11-07 은 명부에 반영되지 않는다(통화 중 명시 동의로 확정된 날짜만 반영)
    after = admin.get(f"/api/v1/caregivers?batch_id={res['batch_id']}").json()["items"]
    assert all(i["confirmed_available_date"] != "2026-11-07" for i in after)
    assert before["00132"]["confirmed_available_date"] == next(
        i for i in after if i["external_no"] == "00132")["confirmed_available_date"]


def test_analysis_endpoint_rejects_bad_payload(admin):
    _, dev, camp, _ = setup_mock(admin)
    agent = make_agent(admin, dev)
    agent.heartbeat()
    start(admin, camp["campaign_id"], dev["id"])
    run_all(agent, limit=1)
    cid = admin.get("/api/v1/reviews?state=ALL").json()[0]["id"]
    import pytest
    from aicc_agent.client import ApiError
    with pytest.raises(ApiError) as e:
        agent.client.post_analysis(cid, {"work_status": "WORKING", "end_date": "내일"})
    assert e.value.status == 400


def test_hourly_stats(admin):
    _, dev, camp, _ = setup_mock(admin)
    agent = make_agent(admin, dev)
    agent.heartbeat()
    start(admin, camp["campaign_id"], dev["id"])
    n = len(run_all(agent))
    real = admin.get("/api/v1/stats/hourly").json()
    assert real["total"]["calls"] == 0 and real["hours"] == []  # 모의 통화는 기본 제외
    s = admin.get("/api/v1/stats/hourly?include_mock=true&days=7").json()
    assert s["total"]["calls"] == n
    assert s["total"]["connected"] <= n and s["total"]["confirmed"] >= 1
    h = s["hours"][0]
    assert 0 <= h["hour"] <= 23 and h["connect_rate"] == round(100 * h["connected"] / h["calls"], 1)
    camp_only = admin.get(f"/api/v1/stats/hourly?include_mock=true&campaign_id={camp['campaign_id']}").json()
    assert camp_only["total"]["calls"] == n
