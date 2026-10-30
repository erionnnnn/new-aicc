"""글로 판단하는 AI(예상 밖 말 이해·녹음 답 고르기·답 문장 만들기·통화 후 분석·시나리오 제안)의 공통 창구.

2026-10-30 사용자 결정: '예상 밖 답변은 GPT 가 분석하고 Gemini 가 답변(목소리)'.
- 글 판단: OpenAI(GPT) — OPENAI_API_KEY 가 있으면 기본. AICC_TEXT_LLM=gemini 로 되돌릴 수 있음
- 음성 인식·안내 녹음·AI 답 목소리: 지금처럼 Gemini(같은 목소리 유지)
모델: AICC_OPENAI_MODEL(기본 gpt-5.4-mini, 2026-03 출시 빠른 소형 모델, 추론 'none' 으로 지연 최소화).
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("aicc.llm")

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_DEFAULT_MODEL = "gpt-5.4-mini"
GEMINI_MODELS = ("gemini-3.5-flash-lite", "gemini-3.8-flash")


class LLMError(RuntimeError):
    pass


def provider(env: dict | None = None) -> str:
    e = env if env is not None else os.environ
    forced = (e.get("AICC_TEXT_LLM") or "").strip().lower()
    if forced in ("openai", "gpt"):
        return "openai"
    if forced == "gemini":
        return "gemini"
    if e.get("OPENAI_API_KEY"):
        return "openai"
    return "gemini" if e.get("GEMINI_API_KEY") else "none"


def available(env: dict | None = None) -> bool:
    return provider(env) != "none"


def name(env: dict | None = None) -> str:
    e = env if env is not None else os.environ
    p = provider(e)
    if p == "openai":
        return "gpt:" + (e.get("AICC_OPENAI_MODEL") or OPENAI_DEFAULT_MODEL)
    return p


def openai_body(prompt: str, *, system: str | None, json_mode: bool, max_tokens: int, model: str,
                reasoning: bool = True) -> dict:
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    body: dict = {"model": model, "messages": msgs, "max_completion_tokens": max_tokens}
    if reasoning:
        body["reasoning_effort"] = "none"  # 생각 단계 없이 바로 답(전화 응대 지연 최소화)
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    return body


def _openai(prompt: str, *, system: str | None, json_mode: bool, max_tokens: int, timeout: float,
            env: dict) -> str:  # pragma: no cover - 외부 API
    import httpx
    key = env.get("OPENAI_API_KEY")
    if not key:
        raise LLMError("OpenAI 키가 없습니다(설정 > 단말 > 이 PC 에이전트)")
    model = env.get("AICC_OPENAI_MODEL") or OPENAI_DEFAULT_MODEL
    if json_mode and "json" not in (prompt + (system or "")).lower():
        prompt += "\nJSON 으로만 답하세요."  # JSON 모드는 지시문에 'JSON' 이 있어야 함
    for reasoning in (True, False):
        body = openai_body(prompt, system=system, json_mode=json_mode, max_tokens=max_tokens, model=model,
                           reasoning=reasoning)
        try:
            r = httpx.post(OPENAI_URL, json=body, headers={"Authorization": f"Bearer {key}"}, timeout=timeout)
        except httpx.TimeoutException:
            raise LLMError(f"GPT 응답 시간 초과({timeout:.0f}초)") from None
        except httpx.HTTPError as e:
            raise LLMError(f"GPT 연결 실패: {type(e).__name__}") from None
        if r.status_code == 400 and reasoning and "reasoning" in r.text:
            continue  # 이 모델이 추론 설정을 받지 않으면 빼고 다시
        if r.status_code == 401:
            raise LLMError("OpenAI 키가 올바르지 않습니다(설정 > 단말 > 이 PC 에이전트에서 다시 넣으세요)")
        if r.status_code == 429:
            raise LLMError("GPT 사용 한도 초과(429) — 결제·한도를 확인하세요")
        if r.status_code >= 400:
            raise LLMError(f"GPT 오류 {r.status_code}: {r.text[:200]}")
        try:
            return (r.json()["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, ValueError):
            raise LLMError("GPT 응답 형식이 올바르지 않습니다") from None
    raise LLMError("GPT 요청 실패")


def _gemini(prompt: str, *, system: str | None, json_mode: bool, max_tokens: int, timeout: float,
            env: dict) -> str:  # pragma: no cover - 외부 API
    from google import genai
    from google.genai import types
    key = env.get("GEMINI_API_KEY")
    if not key:
        raise LLMError("Gemini 키가 없습니다")
    client = genai.Client(api_key=key)
    cfg = types.GenerateContentConfig(system_instruction=system, max_output_tokens=max_tokens, temperature=0.2,
                                      response_mime_type="application/json" if json_mode else None)
    last = None
    for m in [x for x in dict.fromkeys((env.get("AICC_GEMINI_TEXT_MODEL"), *GEMINI_MODELS)) if x]:
        try:
            return (client.models.generate_content(model=m, contents=prompt, config=cfg).text or "").strip()
        except Exception as e:  # noqa: BLE001
            last = e
    raise LLMError(f"Gemini 글 생성 실패: {last}")


def complete(prompt: str, *, system: str | None = None, json_mode: bool = False, max_tokens: int = 300,
             timeout: float = 10.0, env: dict | None = None) -> str:
    """글 모델 호출(공급자는 설정에 따라). 실패하면 LLMError."""
    e = dict(os.environ) if env is None else env
    p = provider(e)
    if p == "openai":
        return _openai(prompt, system=system, json_mode=json_mode, max_tokens=max_tokens, timeout=timeout, env=e)
    if p == "gemini":
        return _gemini(prompt, system=system, json_mode=json_mode, max_tokens=max_tokens, timeout=timeout, env=e)
    raise LLMError("글 판단용 AI 키가 없습니다(OpenAI 또는 Gemini)")
