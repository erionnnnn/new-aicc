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


def test_live_read_match_and_tag():
    from aicc_api import tts
    assert tts.read_match("네, 11월 3일부터 가능하신 게 맞으실까요?", "네 11월 3일부터 가능하신 게 맞으실까요") > 0.95
    assert tts.read_match("근무 중이신가요?", "안녕하세요 좋은간병입니다 무엇을 도와드릴까요") < tts.LIVE_MATCH_MIN
    assert tts.read_match("", "아무거나") == 0.0
    assert tts.voice_tag("female", "gemini-live").startswith("tts:gemini-live:")
    assert tts.voice_tag("male", "gemini-live").split(":")[3] == "Charon"


def _fake_melo_server(wav_rate=24000):
    import io
    import math
    import struct
    import threading
    import wave
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers["Content-Length"])
            req = json.loads(self.rfile.read(n))
            assert req["text"]
            frames = wav_rate // 2
            pcm = struct.pack(f"<{frames}h", *(int(8000 * math.sin(2 * math.pi * 220 * i / wav_rate))
                                                for i in range(frames)))
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(wav_rate)
                w.writeframes(pcm)
            body = buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            body = b'{"ok": true, "speakers": ["KR"]}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_melo_engine_stores_24k_master_and_16k_clip(admin, monkeypatch):
    import io
    import wave

    from aicc_api import tts
    srv = _fake_melo_server()
    monkeypatch.setenv("AICC_MELO_URL", f"http://127.0.0.1:{srv.server_address[1]}")
    try:
        st = admin.get("/api/v1/melo/status").json()
        assert st["ready"] is True
        admin.put("/api/v1/settings", json={"company_name": "좋은간병"})
        packs = admin.post("/api/v1/voice-packs/init").json()
        assert any(p["key"] == "female" for p in packs)
        admin.put("/api/v1/voice-packs/female", json={"speaker_name": "김태희"})
        r = admin.post("/api/v1/voice-packs/female/tts", json={"engine": "melo", "max_items": 3, "unify": True})
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["generated"] == 3 and not out["failed"]
        import os
        import pathlib
        data = pathlib.Path(os.environ["AICC_DATA_DIR"])
        masters = list(data.rglob("female/master/*-24k.wav"))
        assert len(masters) == 3
        with wave.open(str(masters[0])) as w:
            assert w.getframerate() == 24000 and w.getnchannels() == 1 and w.getsampwidth() == 2
        calls16 = [f for f in data.rglob("female/*.wav") if "master" not in f.parts]
        assert calls16
        with wave.open(str(calls16[0])) as w:
            assert w.getframerate() == 16000
        # 남성 음원팩은 거절(목소리 1개)
        admin.put("/api/v1/voice-packs/male", json={"speaker_name": "이실장"})
        r2 = admin.post("/api/v1/voice-packs/male/tts", json={"engine": "melo", "max_items": 1})
        assert r2.status_code == 502 and "여성" in r2.text
        # 변환: 24k → 16k
        w24 = tts.synth_melo24("안녕하세요", "female")
        with wave.open(io.BytesIO(w24)) as w:
            assert w.getframerate() == 24000
        with wave.open(io.BytesIO(tts.to16k(w24))) as w:
            assert w.getframerate() == 16000 and 7800 <= w.getnframes() <= 8200
    finally:
        srv.shutdown()


def test_melo_server_patches_import_without_melo():
    import importlib.util
    import pathlib
    spec = importlib.util.spec_from_file_location("melo_server", pathlib.Path("scripts/melo_server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._patch_g2pkk()  # 패키지가 없어도 오류 없이 넘어가야 함
    mod._patch_mecab_ja()
    assert mod.OUT_RATE == 24000
