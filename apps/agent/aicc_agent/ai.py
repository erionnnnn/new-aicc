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
    "따뜻하고 짧게 대화합니다. 상대를 '선생님'이라고 부릅니다(회사 상담원 말투).\n"
    "목표: 1) 지금 간병 근무 중인지 2) 근무 중이면 언제 끝나는지 3) 다음 근무를 언제부터 할 수 있는지 알아내는 것.\n"
    "규칙:\n"
    "- 딱 한 문장, 40자 안팎으로 짧게 말합니다(말이 길수록 상대가 오래 기다립니다). 매번 '네'로 시작하지 마십시오.\n"
    "- 상대 말에 짧게 반응하고 같은 문장 끝에 '지금 확인할 것'을 부드럽게 묻습니다. 예: '일감은 시기마다 달라서요, 다음 근무는 언제부터 되세요?'\n"
    "- 모르는 사실을 지어내지 마십시오. 일감이 많다·준비돼 있다·바로 드릴 수 있다, 금액·병원·환자 정보처럼 확인되지 않은 내용은 말하지 않습니다.\n"
    "- 날짜를 정하거나 '반영했다/확정했다'고 말하지 마십시오(날짜 확인은 시스템이 따로 합니다).\n"
    "- 일자리 배정·급여·병원·환자 조건은 약속하지 말고 '실제 일감 소개할 때 안내드린다'고 합니다.\n"
    "- 사람이냐고 물으면 자동 안내라고 솔직히 말합니다. 개인정보를 묻지 않습니다.\n"
    "- 간병사가 아니라고 하면 사과하고, 연락을 원치 않으면 정중히 받아들입니다.\n"
    "- 입금·보호자 연락·취소 같은 현장 문제는 '담당자가 바로 확인해서 연락드릴게요'라고 하고 일정 질문으로 돌아옵니다."
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


# Live(실시간 음성) 모델 이름은 자주 바뀐다. 설정값이 없거나 '없는 모델' 오류면 아래 순서로 시도한다.
# 근거: Gemini API 변경 기록(2026-10-27 확인) — gemini-3.8-live(2026-09 GA), gemini-3.1-flash-live-preview(2026-03),
#       gemini-2.5-flash-native-audio-preview-12-2025
LIVE_MODELS = ("gemini-3.8-live", "gemini-3.1-flash-live-preview", "gemini-2.5-flash-native-audio-preview-12-2025")


class GeminiLiveAI(ExceptionAI):  # pragma: no cover - 외부 API
    name = "gemini-live"

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self.model = (model or os.environ.get("AICC_GEMINI_MODEL") or "").strip() or None
        self.voice = os.environ.get("AICC_GEMINI_VOICE", "Aoede")  # 러너가 음원팩(여성/남성)에 맞춰 바꿈
        self._ok_model: str | None = None
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY 가 필요합니다")

    def _candidates(self) -> list[str]:
        return list(dict.fromkeys(m for m in (self._ok_model, self.model, *LIVE_MODELS) if m))

    def respond(self, question: str, *, timeout_s: float = 10.0, context: str | None = None) -> AIReply:
        import asyncio

        from google import genai  # 지연 import
        from google.genai import types

        prompt = context or question

        async def run(model: str) -> tuple[str, bytes]:
            client = genai.Client(api_key=self.api_key)
            # 기존 녹음과 같은 목소리(여성 Aoede / 남성 Charon)
            voice = types.SpeechConfig(voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.voice)))
            cfg = types.LiveConnectConfig(response_modalities=["AUDIO"], system_instruction=SYSTEM_PROMPT,
                                          speech_config=voice, output_audio_transcription={})
            pcm = bytearray()
            text = []
            t_start = time.monotonic()
            t_first = None
            async with client.aio.live.connect(model=model, config=cfg) as session:
                t_conn = time.monotonic()
                try:
                    await session.send_client_content(turns={"role": "user", "parts": [{"text": prompt}]},
                                                      turn_complete=True)
                except Exception:  # noqa: BLE001 - 일부 최신 Live 모델은 실시간 입력으로만 글을 받는다
                    await session.send_realtime_input(text=prompt)
                async for msg in session.receive():
                    sc = getattr(msg, "server_content", None)
                    if sc and sc.model_turn:
                        for part in sc.model_turn.parts:
                            if part.inline_data and part.inline_data.data:
                                if t_first is None:
                                    t_first = time.monotonic()
                                pcm.extend(part.inline_data.data)
                    if sc and getattr(sc, "output_transcription", None) and sc.output_transcription.text:
                        text.append(sc.output_transcription.text)
                    if sc and sc.turn_complete:
                        break
            import logging
            ms = lambda a, b: int(((b or a) - a) * 1000)  # noqa: E731
            logging.getLogger("aicc.agent").info(
                "AI 단계(Live %s): 연결 %dms · 첫 소리 %dms · 전체 %dms · 답 길이 %.1f초",
                model, ms(t_start, t_conn), ms(t_conn, t_first), ms(t_start, time.monotonic()), len(pcm) / 48000)
            return "".join(text), bytes(pcm)

        t0 = time.monotonic()
        errors = []
        for model in self._candidates():
            left = timeout_s - (time.monotonic() - t0)
            if left <= 1:
                break
            try:
                text, pcm = asyncio.run(asyncio.wait_for(run(model), timeout=left))
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                errors.append(f"{model}: {type(e).__name__}: {msg[:160]}")
                if "not found" in msg or "not supported" in msg or "1008" in msg:
                    continue  # 모델 이름 문제 → 다음 모델
                break  # 키·네트워크·시간 초과 등은 다른 모델로도 같으므로 중단
            if not pcm:
                errors.append(f"{model}: 음성 없음")
                continue
            self._ok_model = model
            # Live API 출력은 24kHz PCM → 16kHz 로 변환
            from .audio import resample_linear
            return AIReply(True, limit_sentences(text) or None, resample_linear(pcm, 24000, 16000),
                           int((time.monotonic() - t0) * 1000))
        return AIReply(False, None, None, int((time.monotonic() - t0) * 1000), " | ".join(errors)[:400] or "timeout")


TEXT_MODELS = ("gemini-3.5-flash-lite", "gemini-3.8-flash")  # 빠른 순(2026-10-27 변경 기록 확인)


class GeminiTTSAI(ExceptionAI):  # pragma: no cover - 외부 API
    """목소리 통일용: 답 문장은 글 모델로 만들고, 음성은 안내 녹음과 '같은 TTS 모델·목소리·말투'로 읽는다.

    Live(실시간 음성) 모델은 같은 이름의 목소리라도 TTS 모델과 음색이 달라 안내와 다르게 들린다(2026-10-27 사용자 지적).
    대신 글 생성 + 음성 생성 두 단계라 Live 보다 1~2초 느릴 수 있다.
    """

    name = "gemini-tts"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self.voice = "Aoede"
        self._ok_model: str | None = None
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY 가 필요합니다")

    def _engine(self, pack: str) -> str:
        from aicc_api import tts
        forced = os.environ.get("AICC_AI_TTS_ENGINE", "")
        if forced:
            return forced
        eng = tts.pack_engine(pack)
        if eng == "voicestudio" and not tts.vs_config(pack).get("model"):
            return "gemini"
        if eng == "melo" and pack != "female":
            return "gemini"
        return eng

    def respond(self, question: str, *, timeout_s: float = 10.0, context: str | None = None) -> AIReply:
        """단계별 시간을 기록한다(2026-10-29 실통화: 원인 모를 TimeoutError 로 '잠시만요' 뒤 침묵).

        - gemini-live 음원팩: Live 모델이 답 문장과 목소리를 한 번에 만든다(같은 Live 목소리, 왕복 1회)
        - melo: 글 모델(최대 5초) + 이 PC MeloTTS
        - gemini: 글 모델 + Gemini TTS. 통화 중에는 분당 한도에 걸려도 기다리지 않고 바로 실패 처리
        """
        import concurrent.futures as cf
        import logging
        lg = logging.getLogger("aicc.agent")
        pack = "male" if self.voice == "Charon" else "female"
        engine = self._engine(pack)
        t0 = time.monotonic()
        if engine == "gemini-live":
            if getattr(self, "_live", None) is None:
                self._live = GeminiLiveAI(self.api_key)
            self._live.voice = self.voice
            r = self._live.respond(question, timeout_s=timeout_s, context=context)
            lg.info("AI 단계: Live 한 번에(글+목소리) %dms %s", r.latency_ms, "성공" if r.ok else f"실패 {r.error}")
            return r
        stage = {"name": "글 생성", "t": t0}

        def work() -> tuple[str, bytes]:
            from google import genai
            from google.genai import types
            client = genai.Client(api_key=self.api_key, http_options=types.HttpOptions(timeout=5000))
            cfg = types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT, max_output_tokens=120, temperature=0.6)
            text, err = "", ""
            for m in [x for x in dict.fromkeys((self._ok_model, os.environ.get("AICC_GEMINI_TEXT_MODEL"), *TEXT_MODELS)) if x]:
                try:
                    r = client.models.generate_content(model=m, contents=context or question, config=cfg)
                    text = (r.text or "").strip()
                    if text:
                        self._ok_model = m
                        break
                except Exception as e:  # noqa: BLE001
                    err = f"{m}: {type(e).__name__}: {str(e)[:120]}"
            if not text:
                raise RuntimeError(f"글 생성 실패: {err or '빈 답'}")
            text = limit_sentences(text)
            t_text = time.monotonic()
            lg.info("AI 단계: 글 생성 %dms (%s)", int((t_text - t0) * 1000), self._ok_model)
            stage.update(name=f"음성 생성({engine})", t=t_text)
            from aicc_api import tts
            if engine == "voicestudio":
                wav = tts.synth_voicestudio(text, pack)
            elif engine == "melo":
                wav = tts.synth_melo(text, pack, timeout=max(2.0, timeout_s - (t_text - t0)))
            else:
                wav = tts.synth_gemini(text, pack, self.api_key, wait_on_limit=False)
            lg.info("AI 단계: 음성 생성(%s) %dms", engine, int((time.monotonic() - t_text) * 1000))
            import io
            import wave
            with wave.open(io.BytesIO(wav)) as w:
                return text, w.readframes(w.getnframes())

        ex = cf.ThreadPoolExecutor(max_workers=1)
        try:
            text, pcm = ex.submit(work).result(timeout=timeout_s)
        except cf.TimeoutError:
            msg = f"{stage['name']} 단계에서 시간 초과({int((time.monotonic() - stage['t']) * 1000)}ms)"
            lg.warning("AI 실패: %s", msg)
            return AIReply(False, None, None, int((time.monotonic() - t0) * 1000), msg)
        except Exception as e:  # noqa: BLE001
            lg.warning("AI 실패: %s: %s", type(e).__name__, str(e)[:300])
            return AIReply(False, None, None, int((time.monotonic() - t0) * 1000), f"{type(e).__name__}: {str(e)[:300]}")
        finally:
            ex.shutdown(wait=False)
        return AIReply(True, text, pcm, int((time.monotonic() - t0) * 1000))


def make_ai(kind: str) -> ExceptionAI:
    if kind == "mock":
        return MockAI()
    if kind == "gemini-live":
        return GeminiLiveAI()
    if kind == "gemini-tts":
        return GeminiTTSAI()
    if kind == "failing":
        return FailingAI()
    raise ValueError(f"예외 AI 가 설정되지 않았습니다: {kind!r}")
