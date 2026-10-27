"""PCM 유틸리티와 에너지 기반 VAD(외부 라이브러리 없이 동작).

VAD 는 발화 종료 후 무음 지속시간(end_silence_ms, 초기 후보 250~500ms)을 기준으로 끝을 판정한다.
발화 길이를 고정 1초로 자르지 않고, 매 턴 고정 대기를 추가하지 않는다.
"""

from __future__ import annotations

import io
import math
import struct
import wave
from array import array
from dataclasses import dataclass

from .device import FRAME_MS, SAMPLE_RATE, Frame

SAMPLES_PER_FRAME = SAMPLE_RATE * FRAME_MS // 1000


def rms(pcm: bytes) -> float:
    if not pcm:
        return 0.0
    a = array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    if not a:
        return 0.0
    return math.sqrt(sum(x * x for x in a) / len(a))


def silence(ms: int) -> bytes:
    return b"\x00\x00" * (SAMPLE_RATE * ms // 1000)


def tone(ms: int, freq: float = 440.0, amp: int = 1200) -> bytes:
    n = SAMPLE_RATE * ms // 1000
    return struct.pack(f"<{n}h", *(int(amp * math.sin(2 * math.pi * freq * i / SAMPLE_RATE)) for i in range(n)))


def split_frames(pcm: bytes, start_ms: int, seq0: int = 0) -> list[Frame]:
    step = SAMPLES_PER_FRAME * 2
    out = []
    for i in range(0, len(pcm), step):
        out.append(Frame(pcm[i:i + step], start_ms + (i // step) * FRAME_MS, seq0 + i // step))
    return out


def wav_bytes(pcm: bytes, channels: int = 1, rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def read_wav_mono16(data: bytes) -> bytes:
    with wave.open(io.BytesIO(data)) as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError("16-bit mono WAV 필요")
        pcm = w.readframes(w.getnframes())
        rate = w.getframerate()
    if rate != SAMPLE_RATE:
        pcm = resample_linear(pcm, rate, SAMPLE_RATE)
    return pcm


def resample_linear(pcm: bytes, src: int, dst: int) -> bytes:
    a = array("h")
    a.frombytes(pcm)
    if not a or src == dst:
        return pcm
    n = int(len(a) * dst / src)
    out = array("h", [0]) * n
    for i in range(n):
        x = i * src / dst
        j = int(x)
        f = x - j
        y0 = a[j]
        y1 = a[j + 1] if j + 1 < len(a) else y0
        out[i] = int(y0 + (y1 - y0) * f)
    return out.tobytes()


@dataclass
class VadEvent:
    kind: str  # SPEECH_START | SPEECH_END
    t_ms: int
    frames: list[Frame]


class EnergyVAD:
    def __init__(self, threshold: float = 300.0, end_silence_ms: int = 350, min_speech_ms: int = 120):
        if not 150 <= end_silence_ms <= 1500:
            raise ValueError("end_silence_ms 범위 오류")
        self.threshold = threshold
        self.end_silence_ms = end_silence_ms
        self.min_speech_ms = min_speech_ms
        self._in_speech = False
        self._speech_ms = 0
        self._silence_ms = 0
        self._buf: list[Frame] = []

    def feed(self, f: Frame) -> list[VadEvent]:
        voiced = f.speech if f.mock_text is not None or not f.pcm else rms(f.pcm) >= self.threshold
        events: list[VadEvent] = []
        if voiced:
            if not self._in_speech:
                self._in_speech = True
                self._speech_ms = 0
                self._buf = []
                events.append(VadEvent("SPEECH_START", f.t_ms, []))
            self._speech_ms += FRAME_MS
            self._silence_ms = 0
            self._buf.append(f)
        elif self._in_speech:
            self._silence_ms += FRAME_MS
            self._buf.append(f)
            if self._silence_ms >= self.end_silence_ms:
                self._in_speech = False
                if self._speech_ms >= self.min_speech_ms or any(x.mock_text for x in self._buf):
                    events.append(VadEvent("SPEECH_END", f.t_ms - self._silence_ms + FRAME_MS, self._buf))
                self._buf = []
        return events

    def reset(self) -> None:
        self._in_speech = False
        self._speech_ms = self._silence_ms = 0
        self._buf = []

    @property
    def in_speech(self) -> bool:
        return self._in_speech
