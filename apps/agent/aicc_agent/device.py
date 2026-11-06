"""DeviceAdapter 계약(설계 08 장치 어댑터 계약).

- probe_capabilities: dial, hangup, answer_detection, rx_audio, tx_audio, recording 각각
  supported | unsupported | unknown 을 반환한다. 'supported' 는 실기기 증거가 있을 때만 쓴다.
- dial 은 발신 요청 식별자만 만든다. 실제 응답(answered)은 get_call_state 로 따로 판정한다.
- write_tx_frames 가 받아들였다(accepted)는 사실은 원격 청취 성공을 뜻하지 않는다.
- 연결이 끊기면 기본 스피커로 자동 전환하지 않고 DeviceError 를 던진다.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field

CAPABILITY_KEYS = ("dial", "hangup", "answer_detection", "rx_audio", "tx_audio", "recording")
SAMPLE_RATE = 16000
FRAME_MS = 20


class DeviceError(RuntimeError):
    pass


class DeviceUnverified(DeviceError):
    """검증되지 않은 기능을 쓰려고 할 때."""


@dataclass
class Frame:
    """RX/TX 오디오 프레임. pcm: 16kHz mono 16-bit little-endian."""
    pcm: bytes
    t_ms: int  # 통화 시작 기준 단조 시각
    seq: int = 0
    # MOCK 전용: 이 프레임에서 끝나는 발화 문장(실기기에는 없음)
    mock_text: str | None = None
    speech: bool = True


@dataclass
class CallStatus:
    state: str  # IDLE | DIALING | ALERTING | ACTIVE | ENDED | UNKNOWN
    detail: dict = field(default_factory=dict)


class DeviceAdapter(abc.ABC):
    kind: str = "abstract"

    @abc.abstractmethod
    def probe_capabilities(self) -> dict[str, str]: ...

    @abc.abstractmethod
    def dial(self, number: str) -> str: ...

    @abc.abstractmethod
    def hangup(self) -> None: ...

    @abc.abstractmethod
    def get_call_state(self) -> CallStatus: ...

    @abc.abstractmethod
    def read_rx_frames(self, max_ms: int) -> list[Frame]: ...

    @abc.abstractmethod
    def write_tx_frames(self, frames: list[Frame]) -> int:
        """송신 경로에 넣은 프레임 수. 원격 청취 여부와 무관하다."""

    def stop_tx(self) -> None:  # noqa: B027 - 선택 구현
        pass

    def self_test(self) -> dict:
        return {"capabilities": self.probe_capabilities()}

    def serial(self) -> str | None:
        return None

    def now_ms(self) -> int:
        raise NotImplementedError
