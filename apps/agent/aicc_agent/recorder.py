"""양방향 녹음: RX(상대)와 실제 TX 송출 프레임을 시간축에 맞춰 2채널 WAV 로 만든다.

왼쪽=상대(RX), 오른쪽=우리 송출(TX). 원격이 실제로 들은 음성과 TX 버퍼가 같다고 가정하지 않는다
(실기기 검증 항목). 종료 시 길이·누락·해시를 계산한다.
"""

from __future__ import annotations

import hashlib
from array import array
from dataclasses import dataclass

from .audio import SAMPLES_PER_FRAME, wav_bytes
from .device import FRAME_MS, Frame


@dataclass
class RecordingResult:
    wav: bytes
    sha256: str
    duration_ms: int
    rx_frames: int
    tx_frames: int
    gaps_filled: int


class DualRecorder:
    def __init__(self) -> None:
        self.rx: dict[int, bytes] = {}
        self.tx: dict[int, bytes] = {}

    def add_rx(self, frames: list[Frame]) -> None:
        for f in frames:
            if f.pcm:
                self.rx[f.t_ms // FRAME_MS] = f.pcm

    def add_tx(self, frames: list[Frame]) -> None:
        for f in frames:
            if f.pcm:
                self.tx[f.t_ms // FRAME_MS] = f.pcm

    def finish(self) -> RecordingResult:
        last = max([*self.rx.keys(), *self.tx.keys(), 0])
        zero = b"\x00\x00" * SAMPLES_PER_FRAME
        out = array("h")
        gaps = 0
        for i in range(last + 1):
            left = self.rx.get(i)
            right = self.tx.get(i)
            if left is None and right is None:
                gaps += 1
            la, ra = array("h"), array("h")
            la.frombytes((left or zero)[: SAMPLES_PER_FRAME * 2].ljust(SAMPLES_PER_FRAME * 2, b"\x00"))
            ra.frombytes((right or zero)[: SAMPLES_PER_FRAME * 2].ljust(SAMPLES_PER_FRAME * 2, b"\x00"))
            for a, b in zip(la, ra, strict=True):
                out.append(a)
                out.append(b)
        wav = wav_bytes(out.tobytes(), channels=2)
        return RecordingResult(wav, hashlib.sha256(wav).hexdigest(), (last + 1) * FRAME_MS, len(self.rx),
                               len(self.tx), gaps)
