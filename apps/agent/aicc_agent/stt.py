"""STT 어댑터. 엔진별 지연·정확도를 같은 인터페이스로 비교할 수 있게 한다.

- MockSTT: MOCK 단말 프레임의 텍스트를 그대로 돌려준다(실제 인식 아님).
- FasterWhisperSTT: 평가 후보. VAD 로 끊긴 구간을 한국어로 인식한다(선택 설치 `.[voice]`).
  이 저장소에서 한국어 전화음질 정확도·지연은 아직 측정하지 않았다(UNVERIFIED).
"""

from __future__ import annotations

import abc
import os
import time
from dataclasses import dataclass

from .device import Frame


@dataclass
class SttResult:
    text: str
    final: bool
    confidence: float | None
    latency_ms: int  # 발화 종료 → 인식 결과까지(로컬)
    engine: str


class STTAdapter(abc.ABC):
    name = "abstract"

    @abc.abstractmethod
    def transcribe(self, frames: list[Frame]) -> SttResult: ...


class MockSTT(STTAdapter):
    name = "mock"

    def transcribe(self, frames: list[Frame]) -> SttResult:
        text = " ".join(f.mock_text for f in frames if f.mock_text)
        return SttResult(text, True, 1.0, 0, self.name)


DEFAULT_PROMPT = ("좋은간병입니다. 선생님. 간병 근무 일정 확인 전화입니다. 네. 아니요. 아니에요. 네, 통화 가능합니다. 지금은 근무 중이에요. "
                  "근무 안 해요. 다음 주 월요일부터 가능해요. 11월 3일. 연락하지 마세요.")


class FasterWhisperSTT(STTAdapter):  # pragma: no cover - 선택 의존성, 실기기 평가 필요
    name = "faster-whisper"

    def __init__(self, model: str | None = None, device: str | None = None, compute_type: str = "int8"):
        from faster_whisper import WhisperModel  # 지연 import

        # 기본 CPU: GPU 가 있어도 CUDA/cuDNN DLL 이 없으면 실패하므로 명시적으로 지정할 때만 GPU 사용
        device = device or os.environ.get("AICC_WHISPER_DEVICE", "cpu")
        self.model = WhisperModel(model or os.environ.get("AICC_WHISPER_MODEL", "small"), device=device,
                                  compute_type=compute_type,
                                  # PC 프로세서 코어를 모두 사용(기본값은 4개). 인식 시간 단축
                                  cpu_threads=int(os.environ.get("AICC_WHISPER_THREADS") or (os.cpu_count() or 4)))  # 처음 실행 시 모델을 내려받는다(수백 MB)
        # 짧은 '아니요'를 '안녕'으로 적는 오인식(2026-10-27 실통화)을 줄이려고 통화에서 나올 말을 미리 알려 준다
        self.prompt = os.environ.get("AICC_STT_PROMPT", DEFAULT_PROMPT)

    def transcribe(self, frames: list[Frame]) -> SttResult:
        import numpy as np

        pcm = b"".join(f.pcm for f in frames)
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        t0 = time.monotonic()
        segs, info = self.model.transcribe(audio, language="ko", beam_size=1, vad_filter=False,
                                           condition_on_previous_text=False, without_timestamps=True,
                                           initial_prompt=self.prompt)
        segs = list(segs)
        text = " ".join(s.text.strip() for s in segs).strip()
        conf = None
        if segs:
            conf = float(sum(getattr(s, "avg_logprob", 0.0) for s in segs) / len(segs))
        return SttResult(text, True, conf, int((time.monotonic() - t0) * 1000), self.name)


VOCAB = ["좋은간병", "간병사", "선생님", "실장님", "김태희", "근무", "입실", "퇴실", "보호자", "간병비", "일당", "주급", "정산",
         "요양병원", "평일", "주말", "야간", "주간", "중증", "와상"]


class GeminiLiveSTT(STTAdapter):  # pragma: no cover - 외부 API
    """Google 실시간 음성 인식(gemini-3.5-transcribe-live, Live API, 2026-08 GA).

    통화마다 연결을 미리 열어 두고(begin_call), 말이 끝나면 그 구간 소리를 보내 '끝' 신호 후 최종 글을 받는다.
    실패·시간 초과(기본 2.5초)면 PC 인식(faster-whisper)으로 즉시 대신한다 — 통화는 끊기지 않는다.
    근거: ai.google.dev/gemini-api/docs/live-api/live-transcribe (16kHz 16bit 모노 PCM, audio_stream_end, input_transcription)
    """

    name = "gemini-live"
    MODEL = "gemini-3.5-transcribe-live"

    def __init__(self, api_key: str, fallback: STTAdapter | None = None, timeout_s: float = 2.5):
        import asyncio
        import threading
        self.api_key = api_key
        self.fallback = fallback
        self.timeout_s = timeout_s
        self.model = os.environ.get("AICC_STT_MODEL") or self.MODEL
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True, name="gemini-stt").start()
        self._session = None
        self._cm = None
        self._queue = None
        self._recv_task = None
        self.last_error: str | None = None

    # ---- 통화 단위 연결 ----
    def begin_call(self) -> None:
        import asyncio
        asyncio.run_coroutine_threadsafe(self._open(), self.loop)  # 기다리지 않음(신호 가는 동안 연결)

    def end_call(self) -> None:
        import asyncio
        try:
            asyncio.run_coroutine_threadsafe(self._close(), self.loop).result(timeout=3)
        except Exception:  # noqa: BLE001
            pass

    async def _open(self) -> None:
        import asyncio

        from google import genai
        from google.genai import types
        await self._close()
        client = genai.Client(api_key=self.api_key)
        try:
            tcfg = types.AudioTranscriptionConfig(language_codes=["ko-KR"], custom_vocabulary=VOCAB)
        except Exception:  # noqa: BLE001 - 구버전 SDK 는 일부 항목이 없을 수 있음
            tcfg = types.AudioTranscriptionConfig()
        cfg = types.LiveConnectConfig(response_modalities=["TEXT"], input_audio_transcription=tcfg)
        try:
            self._cm = client.aio.live.connect(model=self.model, config=cfg)
            self._session = await self._cm.__aenter__()
            self._queue = asyncio.Queue()
            self._recv_task = asyncio.ensure_future(self._recv())
            self.last_error = None
        except Exception as e:  # noqa: BLE001
            self._session = None
            self.last_error = f"{type(e).__name__}: {str(e)[:200]}"

    async def _recv(self) -> None:
        try:
            async for msg in self._session.receive():
                sc = getattr(msg, "server_content", None)
                it = getattr(sc, "input_transcription", None) if sc else None
                if it is not None and getattr(it, "text", None):
                    await self._queue.put(it.text)
        except Exception as e:  # noqa: BLE001
            self.last_error = f"{type(e).__name__}: {str(e)[:200]}"
            self._session = None

    async def _close(self) -> None:
        if self._recv_task:
            self._recv_task.cancel()
            self._recv_task = None
        if self._cm is not None:
            try:
                await self._cm.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        self._cm = None
        self._session = None

    async def _run(self, pcm: bytes) -> str:
        import asyncio

        from google.genai import types
        if self._session is None:
            await self._open()
            if self._session is None:
                raise RuntimeError(self.last_error or "연결 실패")
        while not self._queue.empty():
            self._queue.get_nowait()  # 지난 결과 버림
        step = 3200  # 100ms
        for i in range(0, len(pcm), step):
            await self._session.send_realtime_input(
                audio=types.Blob(data=pcm[i:i + step], mime_type="audio/pcm;rate=16000"))
        await self._session.send_realtime_input(audio_stream_end=True)
        parts = [await asyncio.wait_for(self._queue.get(), timeout=self.timeout_s)]
        while True:  # 한 발화가 여러 조각으로 올 수 있음
            try:
                parts.append(await asyncio.wait_for(self._queue.get(), timeout=0.12))
            except asyncio.TimeoutError:
                break
        return " ".join(p.strip() for p in parts if p.strip())

    def transcribe(self, frames: list[Frame]) -> SttResult:
        import asyncio
        pcm = b"".join(f.pcm for f in frames)
        t0 = time.monotonic()
        try:
            text = asyncio.run_coroutine_threadsafe(self._run(pcm), self.loop).result(timeout=self.timeout_s + 1.5)
            return SttResult(text, True, None, int((time.monotonic() - t0) * 1000), self.name)
        except Exception as e:  # noqa: BLE001
            self.last_error = f"{type(e).__name__}: {str(e)[:200]}"
            import logging
            logging.getLogger("aicc.agent").warning("Google 인식 실패 → PC 인식으로 대신: %s", self.last_error)
            asyncio.run_coroutine_threadsafe(self._close(), self.loop)
            if self.fallback is None:
                return SttResult("", True, None, int((time.monotonic() - t0) * 1000), self.name)
            r = self.fallback.transcribe(frames)
            r.latency_ms += int((time.monotonic() - t0) * 1000) - r.latency_ms
            r.engine = f"{self.fallback.name}(대신)"
            return r


def make_stt(kind: str) -> STTAdapter:
    if kind == "mock":
        return MockSTT()
    if kind == "gemini-live":
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("Google 실시간 인식에는 Gemini 키가 필요합니다")
        try:
            fb: STTAdapter | None = FasterWhisperSTT()  # 실패 시 대신할 PC 인식
        except Exception:  # noqa: BLE001
            fb = None
        return GeminiLiveSTT(key, fb)
    if kind == "faster-whisper":
        try:
            return FasterWhisperSTT()
        except ImportError as e:
            raise RuntimeError("음성 인식 모듈(faster-whisper)이 없습니다. setup.ps1 을 다시 실행하세요") from e
    raise ValueError(f"STT 엔진이 설정되지 않았습니다: {kind!r} (AICC_STT_ENGINE)")
