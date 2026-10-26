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


class FasterWhisperSTT(STTAdapter):  # pragma: no cover - 선택 의존성, 실기기 평가 필요
    name = "faster-whisper"

    def __init__(self, model: str | None = None, device: str = "auto", compute_type: str = "int8"):
        from faster_whisper import WhisperModel  # 지연 import

        self.model = WhisperModel(model or os.environ.get("AICC_WHISPER_MODEL", "small"), device=device,
                                  compute_type=compute_type)  # 처음 실행 시 모델을 내려받는다(수백 MB)

    def transcribe(self, frames: list[Frame]) -> SttResult:
        import numpy as np

        pcm = b"".join(f.pcm for f in frames)
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        t0 = time.monotonic()
        segs, info = self.model.transcribe(audio, language="ko", beam_size=1, vad_filter=False,
                                           condition_on_previous_text=False)
        segs = list(segs)
        text = " ".join(s.text.strip() for s in segs).strip()
        conf = None
        if segs:
            conf = float(sum(getattr(s, "avg_logprob", 0.0) for s in segs) / len(segs))
        return SttResult(text, True, conf, int((time.monotonic() - t0) * 1000), self.name)


def make_stt(kind: str) -> STTAdapter:
    if kind == "mock":
        return MockSTT()
    if kind == "faster-whisper":
        try:
            return FasterWhisperSTT()
        except ImportError as e:
            raise RuntimeError("음성 인식 모듈(faster-whisper)이 없습니다. setup.ps1 을 다시 실행하세요") from e
    raise ValueError(f"STT 엔진이 설정되지 않았습니다: {kind!r} (AICC_STT_ENGINE)")
