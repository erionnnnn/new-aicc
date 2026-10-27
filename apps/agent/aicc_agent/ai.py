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
    "당신은 '좋은간병'의 간병사 근무 일정 확인 전화 상담원입니다(자동 안내). 전화 받은 간병사님과 자연스럽게, "
    "따뜻하고 짧게 대화합니다.\n"
    "목표: 1) 지금 간병 근무 중인지 2) 근무 중이면 언제 끝나는지 3) 다음 근무를 언제부터 할 수 있는지 알아내는 것.\n"
    "규칙:\n"
    "- 한두 문장, 말하듯이. 매번 '네'로 시작하지 마십시오. 같은 말을 되풀이하지 마십시오.\n"
    "- 상대 말에 먼저 자연스럽게 반응하고, 끝에 '지금 확인할 것'을 부드럽게 한 번 묻습니다.\n"
    "- 날짜를 정하거나 '반영했다/확정했다'고 말하지 마십시오(날짜 확인은 시스템이 따로 합니다).\n"
    "- 일자리 배정·급여·병원·환자 조건은 약속하지 말고 '실제 일감 소개할 때 안내드린다'고 합니다.\n"
    "- 사람이냐고 물으면 자동 안내라고 솔직히 말합니다. 개인정보를 묻지 않습니다.\n"
    "- 간병사가 아니라고 하면 사과하고, 연락을 원치 않으면 정중히 받아들입니다."
)

STATE_GOAL = {
    "OPENING": "통화 괜찮은지 확인하고, 지금 간병 근무 중이신지",
    "ASK_WORKING": "지금 간병 근무 중이신지",
    "ASK_END_DATE": "지금 하시는 근무가 언제 끝나는지",
    "ASK_AVAILABLE_DATE": "다음 근무를 언제부터 하실 수 있는지",
    "WAIT_AVAILABILITY": "다음 근무를 언제부터 하실 수 있는지",
    "CONFIRM_DATE": "말씀하신 날짜부터 근무 가능하신 게 맞는지",
}


def build_prompt(question: str, state: str | None, transcript: list[str] | None) -> str:
    """AI 에 넘길 현재 상황(확인할 것 + 최근 대화)."""
    goal = STATE_GOAL.get(state or "", "다음 근무 가능 일정")
    lines = "\n".join((transcript or [])[-8:])
    return (f"[지금 확인할 것] {goal}\n[최근 대화]\n{lines}\n[간병사님 방금 한 말] {question}\n"
            "위 규칙대로 상담원으로서 바로 할 말만 답하세요.")


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
    def respond(self, question: str, *, timeout_s: float = 10.0, context: str | None = None) -> AIReply: ...


def limit_sentences(text: str, n: int = 2) -> str:
    parts = re.split(r"(?<=[.!?。])\s+", text.strip())
    return " ".join(parts[:n]).strip()


class MockAI(ExceptionAI):
    """MOCK: 정해진 범위 안내 문장. 생성형 AI 가 아니다."""

    name = "mock"

    def respond(self, question: str, *, timeout_s: float = 10.0, context: str | None = None) -> AIReply:
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

    def respond(self, question: str, *, timeout_s: float = 10.0, context: str | None = None) -> AIReply:
        return AIReply(False, None, None, int(timeout_s * 1000), "timeout")


class GeminiLiveAI(ExceptionAI):  # pragma: no cover - 외부 API, 키·모델 확인 필요
    name = "gemini-live"

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self.model = model or os.environ.get("AICC_GEMINI_MODEL")
        self.voice = os.environ.get("AICC_GEMINI_VOICE", "Aoede")  # 러너가 음원팩(여성/남성)에 맞춰 바꿈
        if not self.api_key or not self.model:
            raise RuntimeError("GEMINI_API_KEY 와 AICC_GEMINI_MODEL(공식 문서에서 확인한 Live 모델명)이 필요합니다")

    def respond(self, question: str, *, timeout_s: float = 10.0, context: str | None = None) -> AIReply:
        import asyncio

        from google import genai  # 지연 import
        from google.genai import types

        async def run() -> tuple[str, bytes]:
            client = genai.Client(api_key=self.api_key)
            # 기존 녹음과 같은 목소리(여성 Aoede / 남성 Charon)
            voice = types.SpeechConfig(voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.voice)))
            cfg = types.LiveConnectConfig(response_modalities=["AUDIO"],
                                          system_instruction=SYSTEM_PROMPT,
                                          speech_config=voice,
                                          output_audio_transcription={})
            pcm = bytearray()
            text = []
            async with client.aio.live.connect(model=self.model, config=cfg) as session:
                await session.send_client_content(turns={"role": "user", "parts": [{"text": context or question}]},
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
            # 원인을 로그에서 바로 알 수 있게 오류 내용을 함께 남긴다(키 오류·모델 이름 오류 등)
            return AIReply(False, None, None, int((time.monotonic() - t0) * 1000),
                           f"{type(e).__name__}: {str(e)[:200]}")
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
