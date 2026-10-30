"""글 판단 AI 공급자(2026-10-30: 예상 밖 말은 GPT 가 분석, Gemini 가 답변 목소리)."""

import json

import pytest
from aicc_agent import llm


def test_provider_choice():
    assert llm.provider({"OPENAI_API_KEY": "sk-x", "GEMINI_API_KEY": "g"}) == "openai"
    assert llm.provider({"GEMINI_API_KEY": "g"}) == "gemini"
    assert llm.provider({"OPENAI_API_KEY": "sk-x", "AICC_TEXT_LLM": "gemini", "GEMINI_API_KEY": "g"}) == "gemini"
    assert llm.provider({}) == "none" and not llm.available({})
    assert llm.name({"OPENAI_API_KEY": "sk-x"}) == "gpt:gpt-5.4-mini"
    assert llm.name({"OPENAI_API_KEY": "sk-x", "AICC_OPENAI_MODEL": "gpt-5.4-nano"}) == "gpt:gpt-5.4-nano"


class _Resp:
    def __init__(self, code, body):
        self.status_code, self._b = code, body
        self.text = json.dumps(body, ensure_ascii=False)

    def json(self):
        return self._b


def test_openai_request_and_fallback_without_reasoning(monkeypatch):
    import httpx
    sent = []

    def post(url, json=None, headers=None, timeout=None):  # noqa: A002
        sent.append(json)
        if "reasoning_effort" in json:
            return _Resp(400, {"error": {"message": "Unsupported parameter: reasoning_effort"}})
        return _Resp(200, {"choices": [{"message": {"content": '{"code": "FAQ_X"}'}}]})
    monkeypatch.setattr(httpx, "post", post)
    out = llm.complete("고르세요", system="sys", json_mode=True, max_tokens=50,
                       env={"OPENAI_API_KEY": "sk-test"})
    assert out == '{"code": "FAQ_X"}'
    assert sent[0]["model"] == "gpt-5.4-mini" and sent[0]["response_format"] == {"type": "json_object"}
    assert sent[0]["messages"][0] == {"role": "system", "content": "sys"} and sent[0]["max_completion_tokens"] == 50
    assert "JSON" in sent[0]["messages"][1]["content"]  # JSON 모드 필수 문구 자동 추가
    assert "reasoning_effort" not in sent[1]  # 추론 설정을 거부하면 빼고 다시


@pytest.mark.parametrize("code,msg", [(401, "키가 올바르지"), (429, "한도"), (500, "오류 500")])
def test_openai_errors_are_clear(monkeypatch, code, msg):
    import httpx
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(code, {"error": {"message": "x"}}))
    with pytest.raises(llm.LLMError, match=msg):
        llm.complete("x", env={"OPENAI_API_KEY": "sk-test"})


def test_router_uses_llm(monkeypatch):
    from aicc_agent.router import LLMRouter
    monkeypatch.setattr(llm, "complete", lambda *a, **k: '{"code": "FAQ_PATIENT_AGE"}')
    r = LLMRouter({"OPENAI_API_KEY": "sk-x"})
    assert r.name == "gpt:gpt-5.4-mini"
    assert r.choose("연세가?", "g", [], [("FAQ_PATIENT_AGE", "나이")]) == "FAQ_PATIENT_AGE"
    with pytest.raises(RuntimeError):
        LLMRouter({})


def test_analyzer_uses_llm(monkeypatch):
    from datetime import date

    from aicc_agent.analysis import LLMAnalyzer
    monkeypatch.setattr(llm, "complete", lambda *a, **k: json.dumps(
        {"work_status": "NOT_WORKING", "available_date": "2026-11-03", "summary": "11월 3일부터 가능"}))
    a = LLMAnalyzer({"OPENAI_API_KEY": "sk-x"}).analyze(["간병사: 3일부터요"], date(2026, 10, 30))
    assert a["available_date"] == "2026-11-03" and a["model"] == "gpt:gpt-5.4-mini"
