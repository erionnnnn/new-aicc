"""예외 질문 응대 AI 어댑터.

- 예외로 확정된 턴에만 호출한다. 일반 ARS 는 AI 응답을 기다리지 않는다.
- AI 결과는 '말할 오디오/문장'뿐이다. 날짜 확정·DB 저장 권한이 없다.
- 정책(설계 08): 예외 질문당 최대 2문장, 10초 내 실패 처리.
- Gemini Live 실제 모델·SDK·입출력 형식은 배포 시 공식 문서로 확인해 고정한다.
  이 저장소에서는 실제 API 호출을 검증하지 않았다(키 없음 → UNVERIFIED).
"""

from __future__ import annotations

import abc
import os
import re
import time
from dataclasses import dataclass

SYSTEM_PROMPT = (
    "당신은 간병사 근무 일정 확인 전화의 자동 안내입니다. 일정 확인만 담당하며 일자리 배정·확정·급여·계약을 "
    "약속하지 않습니다. 병원·환자 정보와 일자리 조건은 '전담 실장님이 따로 연락드린다'고만 안내합니다. "
    "실제 사람이라고 주장하지 않습니다. 한국어 존댓말로 최대 두 문장으로 짧게 답하고, 날짜를 정하거나 "
    "확정하지 마십시오. 개인정보를 묻지 마십시오."
)


@dataclass
class AIReply:
    ok: bool
    text: str | None
    pcm: bytes | None
    latency_ms: int
    error: str | None = None


class ExceptionAI(abc.ABC):
    name = "abstract"

    @abc.abstractmethod
    def respond(self, question: str, *, timeout_s: float = 10.0) -> AIReply: ...


def limit_sentences(text: str, n: int = 2) -> str:
    parts = re.split(r"(?<=[.!?。])\s+", text.strip())
    return " ".join(parts[:n]).strip()


class MockAI(ExceptionAI):
    """MOCK: 정해진 범위 안내 문장. 생성형 AI 가 아니다."""

    name = "mock"

    def respond(self, question: str, *, timeout_s: float = 10.0) -> AIReply:
        q = question
        if re.search(r"급여|일당|시급|돈|병원|환자|조건|배정|일자리", q):
            text = "그 부분은 제가 안내드리기 어렵고, 전담 실장님이 따로 연락드릴 거예요."
        elif re.search(r"누구|어디|회사|무슨\s*일", q):
            text = "근무 일정만 확인드리는 자동 안내예요. 일자리 배정은 전담 실장님이 따로 연락드려요."
        elif re.search(r"녹음|ai|로봇|기계|사람", q, re.I):
            text = "네, 저는 자동 안내이고 통화 내용은 일정 확인 목적으로 기록돼요."
        else:
            text = "죄송하지만 그 질문은 담당자가 다시 연락드려 안내할게요."
        return AIReply(True, text, None, 5)


class FailingAI(ExceptionAI):
    name = "failing"

    def respond(self, question: str, *, timeout_s: float = 10.0) -> AIReply:
        return AIReply(False, None, None, int(timeout_s * 1000), "timeout")


class GeminiLiveAI(ExceptionAI):  # pragma: no cover - 외부 API, 키·모델 확인 필요
    name = "gemini-live"

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self.model = model or os.environ.get("AICC_GEMINI_MODEL")
        if not self.api_key or not self.model:
            raise RuntimeError("GEMINI_API_KEY 와 AICC_GEMINI_MODEL(공식 문서에서 확인한 Live 모델명)이 필요합니다")

    def respond(self, question: str, *, timeout_s: float = 10.0) -> AIReply:
        import asyncio

        from google import genai  # 지연 import
        from google.genai import types

        async def run() -> tuple[str, bytes]:
            client = genai.Client(api_key=self.api_key)
            cfg = types.LiveConnectConfig(response_modalities=["AUDIO"],
                                          system_instruction=SYSTEM_PROMPT,
                                          output_audio_transcription={})
            pcm = bytearray()
            text = []
            async with client.aio.live.connect(model=self.model, config=cfg) as session:
                await session.send_client_content(turns={"role": "user", "parts": [{"text": question}]},
                                                  turn_complete=True)
                async for msg in session.receive():
                    sc = getattr(msg, "server_content", None)
                    if sc and sc.model_turn:
                        for part in sc.model_turn.parts:
                            if part.inline_data and part.inline_data.data:
                                pcm.extend(part.inline_data.data)
                    if sc and getattr(sc, "output_transcription", None) and sc.output_transcription.text:
                        text.append(sc.output_transcription.text)
                    if sc and sc.turn_complete:
                        break
            return "".join(text), bytes(pcm)

        t0 = time.monotonic()
        try:
            text, pcm = asyncio.run(asyncio.wait_for(run(), timeout=timeout_s))
        except Exception as e:  # noqa: BLE001
            return AIReply(False, None, None, int((time.monotonic() - t0) * 1000), type(e).__name__)
        # Live API 출력은 24kHz PCM(공식 문서 기준, 배포 시 재확인) → 16kHz 로 변환
        from .audio import resample_linear
        return AIReply(True, limit_sentences(text) or None, resample_linear(pcm, 24000, 16000) if pcm else None,
                       int((time.monotonic() - t0) * 1000))


def make_ai(kind: str) -> ExceptionAI:
    if kind == "mock":
        return MockAI()
    if kind == "gemini-live":
        return GeminiLiveAI()
    if kind == "failing":
        return FailingAI()
    raise ValueError(f"예외 AI 가 설정되지 않았습니다: {kind!r}")
