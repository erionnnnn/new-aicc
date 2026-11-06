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


class GeminiLiveSTT(STTAdapter):  # pragma: no cover - 외부 API(핵심 흐름은 가짜 세션으로 시험)
    """Google 실시간 음성 인식(gemini-3.5-transcribe-live, Live API).

    2026-10-29 실통화 로그: 말이 끝난 뒤 소리를 한꺼번에 보내면 2.5초 안에 글이 오지 않아(TimeoutError) 매번
    PC 인식으로 넘어가 응답까지 4.5~5.6초 걸렸다. 그래서
    1) 상대가 말하는 동안 소리를 실시간으로 흘려보낸다(stream_start/stream_audio) → 말이 끝날 때 글이 거의 다 와 있음
    2) 말이 끝나면 '끝' 신호(audio_stream_end)만 보내고 짧게(기본 1.2초) 기다린다
    3) PC 인식(faster-whisper)은 처음부터 동시에 돌려 두어, Google 이 늦으면 기다림 없이 바로 그 결과를 쓴다
    4) 시간 초과로 연결을 끊지 않는다(끊으면 다음 말마다 재연결 1초 이상)
    근거: ai.google.dev/gemini-api/docs/live-api/live-transcribe (16kHz 16bit 모노 PCM, audio_stream_end, input_transcription)
    """

    name = "gemini-live"
    MODEL = "gemini-3.5-transcribe-live"

    def __init__(self, api_key: str, fallback: STTAdapter | None = None, timeout_s: float = 2.5,
                 end_wait_s: float | None = None):
        import asyncio
        import threading
        self.api_key = api_key
        self.fallback = fallback
        self.timeout_s = timeout_s  # 실시간으로 못 보낸 말(한꺼번에 보냄)의 최대 기다림
        self.end_wait_s = end_wait_s if end_wait_s is not None else float(os.environ.get("AICC_STT_END_WAIT", "1.2"))
        self.model = os.environ.get("AICC_STT_MODEL") or self.MODEL
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True, name="gemini-stt").start()
        self._session = None
        self._cm = None
        self._queue = None
        self._out = None
        self._recv_task = None
        self._send_task = None
        self._streamed = 0       # 이번 말에서 실시간으로 보낸 바이트
        self._stream_on = False
        self.last_error: str | None = None
        from concurrent.futures import ThreadPoolExecutor
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt-fallback")

    # ---- 연결 ----
    def _connect(self):  # noqa: ANN202 - 시험에서 가짜 세션으로 바꿔 끼운다
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=self.api_key)
        try:
            tcfg = types.AudioTranscriptionConfig(language_codes=["ko-KR"], custom_vocabulary=VOCAB)
        except Exception:  # noqa: BLE001 - 구버전 SDK 는 일부 항목이 없을 수 있음
            tcfg = types.AudioTranscriptionConfig()
        cfg = types.LiveConnectConfig(response_modalities=["TEXT"], input_audio_transcription=tcfg)
        return client.aio.live.connect(model=self.model, config=cfg)

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
        await self._close()
        try:
            self._queue = asyncio.Queue()
            self._out = asyncio.Queue()
            self._cm = self._connect()
            self._session = await self._cm.__aenter__()
            self._recv_task = asyncio.ensure_future(self._recv())
            self._send_task = asyncio.ensure_future(self._send())
            self.last_error = None
        except Exception as e:  # noqa: BLE001
            self._session = None
            self.last_error = f"{type(e).__name__}: {str(e)[:200]}"

    async def _recv(self) -> None:
        try:
            async for msg in self._session.receive():
                sc = getattr(msg, "server_content", None)
                it = getattr(sc, "input_transcription", None) if sc else None
                if it is not None and (getattr(it, "text", None) or getattr(it, "finished", False)):
                    await self._queue.put((it.text or "", bool(getattr(it, "finished", False))))
        except Exception as e:  # noqa: BLE001
            self.last_error = f"{type(e).__name__}: {str(e)[:200]}"
            self._session = None

    async def _send(self) -> None:
        """보낼 소리를 순서대로 하나씩 보낸다(여러 곳에서 동시에 보내 순서가 섞이지 않게)."""
        try:
            from google.genai import types
            blob = lambda d: types.Blob(data=d, mime_type="audio/pcm;rate=16000")  # noqa: E731
        except ImportError:  # 시험 환경(가짜 세션)
            from types import SimpleNamespace
            blob = lambda d: SimpleNamespace(data=d, mime_type="audio/pcm;rate=16000")  # noqa: E731
        while True:
            item = await self._out.get()
            sess = self._session
            if sess is None:
                continue
            try:
                if item is None:
                    await sess.send_realtime_input(audio_stream_end=True)
                else:
                    await sess.send_realtime_input(audio=blob(item))
            except Exception as e:  # noqa: BLE001
                self.last_error = f"{type(e).__name__}: {str(e)[:200]}"
                self._session = None

    async def _close(self) -> None:
        for t in (self._recv_task, self._send_task):
            if t:
                t.cancel()
        self._recv_task = self._send_task = None
        if self._cm is not None:
            try:
                await self._cm.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        self._cm = None
        self._session = None

    # ---- 실시간 흘려보내기(러너가 말하는 동안 호출) ----
    def _drain(self) -> None:
        if self._queue is not None:
            while not self._queue.empty():
                self._queue.get_nowait()  # 지난 결과 버림

    def stream_start(self) -> None:
        self._streamed = 0
        self._stream_on = self._session is not None
        if self._stream_on:
            self.loop.call_soon_threadsafe(self._drain)

    def stream_audio(self, pcm: bytes) -> None:
        if not self._stream_on or not pcm or self._out is None:
            return
        self._streamed += len(pcm)
        self.loop.call_soon_threadsafe(self._out.put_nowait, pcm)

    # ---- 결과 받기 ----
    async def _collect(self, first_wait: float, already: int = 0) -> str:
        """already: 끝 신호 전에 이미 와 있던 조각 수. 그 뒤 새 조각(끝 신호 이후)이 하나라도 오거나 '완료'가 와야
        조각 기다림을 줄인다 — 말 끝부분('…가능해요')이 잘리지 않게."""
        import asyncio
        import re
        parts: list[str] = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + first_wait
        while True:
            left = deadline - loop.time()
            if left <= 0:
                break
            try:
                text, finished = await asyncio.wait_for(self._queue.get(), timeout=left)
            except asyncio.TimeoutError:
                break
            parts.append(text)
            if finished:
                break
            if len(parts) > already and "".join(parts).strip():
                deadline = min(deadline, loop.time() + 0.3)  # 글이 오기 시작하면 조각이 더 오는지 잠깐만 본다
        text = re.sub(r"\s+", " ", "".join(parts)).strip()
        if not text:
            raise TimeoutError("인식 결과 없음")
        return text

    async def _finish_stream(self) -> str:
        already = self._queue.qsize()
        self._out.put_nowait(None)  # 끝 신호
        return await self._collect(self.end_wait_s, already)

    async def _burst(self, pcm: bytes) -> str:
        if self._session is None:
            await self._open()
            if self._session is None:
                raise RuntimeError(self.last_error or "연결 실패")
        self._drain()
        step = 3200  # 100ms
        for i in range(0, len(pcm), step):
            self._out.put_nowait(pcm[i:i + step])
        self._out.put_nowait(None)
        return await self._collect(self.timeout_s)

    def transcribe(self, frames: list[Frame]) -> SttResult:
        import asyncio
        pcm = b"".join(f.pcm for f in frames)
        t0 = time.monotonic()
        # PC 인식을 동시에 시작(Google 이 늦으면 기다리지 않고 바로 쓰기 위해)
        fb_future = self._pool.submit(self.fallback.transcribe, frames) if self.fallback else None
        streamed = self._stream_on and self._session is not None and self._streamed >= len(pcm) * 0.8
        self._stream_on = False
        try:
            coro = self._finish_stream() if streamed else self._burst(pcm)
            wait = (self.end_wait_s if streamed else self.timeout_s) + 1.0
            text = asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=wait)
            if fb_future:
                fb_future.cancel()
            return SttResult(text, True, None, int((time.monotonic() - t0) * 1000),
                             self.name + ("" if streamed else "(한꺼번에)"))
        except Exception as e:  # noqa: BLE001
            self.last_error = f"{type(e).__name__}: {str(e)[:200]}"
            import logging
            logging.getLogger("aicc.agent").warning("Google 인식 실패 → PC 인식으로 대신(%s): %s",
                                                    "실시간" if streamed else "한꺼번에", self.last_error)
            if self._session is None:  # 연결이 실제로 끊긴 경우에만 다시 연결(시간 초과로는 끊지 않음)
                asyncio.run_coroutine_threadsafe(self._open(), self.loop)
            if fb_future is None:
                return SttResult("", True, None, int((time.monotonic() - t0) * 1000), self.name)
            r = fb_future.result()
            r.latency_ms = int((time.monotonic() - t0) * 1000)
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
