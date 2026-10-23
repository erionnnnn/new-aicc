"""PC 에이전트 루프: 상태 보고(heartbeat) → 발신권 요청(claim) → 통화 실행 → 종료 보고.

- 한 단말은 한 번에 한 통화만 처리한다(서버 발신권 + 로컬 직렬 실행).
- 장치 오류·서버 오류 뒤에는 자동으로 다음 발신을 하지 않는다(서버가 캠페인을 일시정지, 운영자 재개 필요).
- 즉시중단 명령은 별도 heartbeat 스레드가 받아 진행 중 통화에 전달한다.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from .ai import ExceptionAI
from .client import AgentClient, ApiError
from .device import DeviceAdapter, DeviceError
from .runner import CallRunner, CallSummary, RunnerConfig
from .stt import STTAdapter

log = logging.getLogger("aicc.agent")
VERSION = "0.1.0"


@dataclass
class StepResult:
    status: str
    summary: CallSummary | None = None
    message: str | None = None


class Agent:
    def __init__(self, client: AgentClient, device: DeviceAdapter, stt: STTAdapter, ai: ExceptionAI,
                 config: RunnerConfig | None = None, probe_report: dict | None = None):
        self.client = client
        self.device = device
        self.runner = CallRunner(client, device, stt, ai, config)
        self.probe_report = probe_report
        self.paused_local = False
        self._lock = threading.Lock()

    def heartbeat(self) -> dict:
        try:
            caps = self.device.probe_capabilities()
            state = self.device.get_call_state().state
            serial = self.device.serial()
        except DeviceError:
            caps = {k: "unsupported" for k in ("dial", "hangup", "answer_detection", "rx_audio", "tx_audio",
                                               "recording")}
            state, serial = "DISCONNECTED", None
        hb = self.client.heartbeat(capabilities=caps, phone_state=state, serial=serial,
                                   probe_report=self.probe_report, agent_version=VERSION)
        for cmd in hb.get("commands", []):
            if cmd["type"] == "STOP_NOW":
                self.runner.stop_requested = True
        return hb

    def step(self) -> StepResult:
        """한 번의 순환. 통화가 있으면 끝까지 실행한다."""
        with self._lock:
            try:
                self.client.replay_journal()
            except Exception:  # noqa: BLE001
                return StepResult("SERVER_UNREACHABLE")
            hb = self.heartbeat()
            if hb.get("active_call_id"):
                return StepResult("WAIT_DEVICE_IDLE")
            try:
                claim = self.client.claim()
            except ApiError as e:
                return StepResult("CLAIM_ERROR", message=e.code)
            if claim.get("status") != "CALL":
                return StepResult(claim.get("status", "IDLE"), message=claim.get("message"))
            self.runner.stop_requested = False
            summary = self.runner.run(claim)
            return StepResult("CALLED", summary)

    def run_forever(self, stop: threading.Event, idle_wait_s: float = 2.0) -> None:  # pragma: no cover
        def hb_loop() -> None:
            while not stop.is_set():
                try:
                    if not self._lock.locked():
                        pass
                    else:
                        self.heartbeat()
                except Exception:  # noqa: BLE001
                    pass
                stop.wait(3.0)

        threading.Thread(target=hb_loop, daemon=True, name="aicc-heartbeat").start()
        while not stop.is_set():
            try:
                r = self.step()
            except Exception as e:  # noqa: BLE001
                log.warning("에이전트 순환 오류: %s", type(e).__name__)
                r = StepResult("ERROR")
            if r.status != "CALLED":
                stop.wait(idle_wait_s)
