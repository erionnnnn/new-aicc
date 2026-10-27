"""MOCK 단말과 가상 상대(페르소나). 실제 전화를 걸지 않는다.

모든 결과는 is_mock 으로 표시되며, 시험 조직(sandbox)에서만 사용할 수 있다.
페르소나는 합성 번호의 마지막 숫자로 정한다(실제 사람 응답 아님).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .audio import SAMPLES_PER_FRAME, silence, split_frames, tone
from .device import FRAME_MS, CallStatus, DeviceAdapter, DeviceError, Frame

QUESTION_LABELS = {"opening", "ask_repeat", "ask_working", "ask_end_date", "ask_available_date", "confirm_date"}


@dataclass
class Persona:
    name: str
    answer: str = "ANSWER"  # ANSWER | NO_ANSWER | BUSY | VOICEMAIL
    replies: list[str | None] = field(default_factory=list)  # None = 침묵, "HANGUP" = 상대 종료
    note: str = ""


PERSONAS: dict[str, Persona] = {
    "0": Persona("상시 가능", replies=["네 괜찮아요", "아니요 쉬고 있어요", "언제든 가능해요"]),
    "1": Persona("근무 중·종료일·가능일", replies=["네", "네 지금 병원에서 일하고 있어요", "25일까지요",
                                             "26일부터 가능해요", "네 맞아요"]),
    "2": Persona("날짜 정정", replies=["네", "아니요 쉬고 있어요", "26일부터요", "아니 27일이요", "네 맞습니다"]),
    "3": Persona("통화 중 연락거부", replies=["네", "앞으로 연락하지 마세요"]),
    "4": Persona("부재중", answer="NO_ANSWER"),
    "5": Persona("예외 질문 후 응답", replies=["네", "근데 누구세요?", "쉬고 있어요", "다음 주 월요일부터 가능해요", "네"]),
    "6": Persona("지금 통화 곤란", replies=["지금 운전 중이라 나중에 전화주세요"]),
    "7": Persona("날짜 모름", replies=["네", "쉬어요", "잘 모르겠어요"]),
    "8": Persona("음성사서함", answer="VOICEMAIL", replies=["고객님의 전화를 받을 수 없어 음성사서함으로 연결됩니다"]),
    "9": Persona("무응답 후 끊음", replies=["네", None, None, "HANGUP"]),
}


def persona_for(number: str) -> Persona:
    return PERSONAS.get(number[-1:] if number else "", PERSONAS["0"])


class MockDevice(DeviceAdapter):
    kind = "mock"
    RING_MS = 2000

    def __init__(self, persona: Persona | None = None):
        self._clock = 0
        self._state = "IDLE"
        self._fixed = persona
        self._persona = persona
        self._dial_at = 0
        self._replies: list[str | None] = []
        self._pending: list[Frame] = []
        self._seq = 0
        self.tx_frames_written = 0
        self.connected = True
        self.hangups = 0
        self.dialed: list[str] = []

    # ---- 시뮬레이션 제어 ----
    def now_ms(self) -> int:
        return self._clock

    def _advance(self, ms: int) -> None:
        self._clock += ms
        if self._state in ("DIALING", "ALERTING") and self._persona:
            elapsed = self._clock - self._dial_at
            if elapsed >= self.RING_MS:
                if self._persona.answer in ("ANSWER", "VOICEMAIL"):
                    self._state = "ACTIVE"
                    if self._persona.answer == "VOICEMAIL":
                        self._queue_reply()
                elif self._persona.answer == "BUSY":
                    self._state = "IDLE"
                elif elapsed >= 30000:
                    self._state = "IDLE"
                else:
                    self._state = "ALERTING"

    def _queue_reply(self) -> None:
        if not self._replies:
            return
        r = self._replies.pop(0)
        start = self._clock + 300
        if r is None:
            return
        if r == "HANGUP":
            self._pending.append(Frame(b"", start, -1, mock_text="__HANGUP__", speech=False))
            return
        speech_ms = max(400, 90 * len(r))
        frames = split_frames(tone(speech_ms, 220, 2500), start, self._seq)
        frames[-1].mock_text = r
        self._seq += len(frames)
        tail = split_frames(silence(600), start + speech_ms, self._seq)
        for f in tail:
            f.speech = False
        self._seq += len(tail)
        self._pending.extend(frames + tail)

    # ---- DeviceAdapter ----
    def probe_capabilities(self) -> dict[str, str]:
        return {k: "mock" for k in ("dial", "hangup", "answer_detection", "rx_audio", "tx_audio", "recording")}

    def serial(self) -> str | None:
        return "MOCK"

    def dial(self, number: str) -> str:
        if not self.connected:
            raise DeviceError("MOCK 단말 연결 끊김")
        if self._state != "IDLE":
            raise DeviceError("이미 통화 중")
        self._persona = self._fixed or persona_for(number)
        self._replies = list(self._persona.replies)
        self._state = "DIALING"
        self._dial_at = self._clock
        self._pending = []
        self.dialed.append(number)
        return f"mock-dial-{len(self.dialed)}"

    def hangup(self) -> None:
        if not self.connected:
            raise DeviceError("MOCK 단말 연결 끊김")
        self.hangups += 1
        self._state = "IDLE"
        self._pending = []

    def get_call_state(self) -> CallStatus:
        if not self.connected:
            raise DeviceError("MOCK 단말 연결 끊김")
        return CallStatus(self._state, {"persona": self._persona.name if self._persona else None})

    def read_rx_frames(self, max_ms: int) -> list[Frame]:
        if not self.connected:
            raise DeviceError("MOCK 단말 연결 끊김")
        end = self._clock + max_ms
        out: list[Frame] = []
        while self._pending and self._pending[0].t_ms < end:
            f = self._pending.pop(0)
            if f.mock_text == "__HANGUP__":
                self._clock = max(self._clock, f.t_ms)
                self._state = "IDLE"
                self._pending = []
                return out
            out.append(f)
        # 남는 시간은 무음 프레임
        t = out[-1].t_ms + FRAME_MS if out else self._clock
        while t < end:
            out.append(Frame(b"\x00\x00" * SAMPLES_PER_FRAME, t, -1, speech=False))
            t += FRAME_MS
        self._advance(max_ms)
        return out

    def write_tx_frames(self, frames: list[Frame]) -> int:
        if not self.connected:
            raise DeviceError("MOCK 단말 연결 끊김")
        n = len(frames)
        self.tx_frames_written += n
        self._advance(n * FRAME_MS)
        return n

    def tx_segment_done(self, label: str) -> None:
        if (label in QUESTION_LABELS or label.startswith("sc_")) and self._state == "ACTIVE":
            self._queue_reply()
