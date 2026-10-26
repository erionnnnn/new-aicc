"""갤럭시 A24 실기기 어댑터.

확인된 사실과 가설을 구분한다.
- 발신/종료: ADB(`am start -a android.intent.action.CALL`, `input keyevent KEYCODE_ENDCALL`)로 요청할 수 있다.
  실제 동작은 기기·One UI 버전에 따라 다르므로 aicc-probe 의 승인된 시험 통화로 확인 전까지 'unknown'.
- 응답 감지: 일반 앱은 발신 통화의 원격 응답 시점을 공식 API로 받지 못한다. dumpsys telephony.registry 의
  precise call state(ForegroundCallState ACTIVE=1)가 노출되는 기기에서만 시도하며, 확인 전까지 'unknown'.
  mCallState=2(OFFHOOK)는 발신 직후에도 나오므로 응답으로 쓰지 않는다.
- 통화 음성(RX/TX): Android 공식 문서상 USB 디버깅 연결 자체는 오디오를 제공하지 않는다(설계 08 [S1][S2]).
  따라서 이 어댑터는 운영자가 명시적으로 지정한 오디오 입력/출력 장치(AudioRoute)가 있을 때만 RX/TX 를 시도하며,
  그 장치가 실제 '통화 음성'을 주고받는지는 사람이 원격 단말로 검증해야 한다(서버 proof).
- 지정 장치가 없거나 연결이 끊기면 기본 스피커로 전환하지 않고 DeviceError 를 던진다.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .device import FRAME_MS, SAMPLE_RATE, CallStatus, DeviceAdapter, DeviceError, DeviceUnverified, Frame

_PRECISE = re.compile(r"[Ff]oreground\s*call\s*state[:=]\s*(-?\d+)")
_CALLSTATE = re.compile(r"mCallState=(\d)")
# PreciseCallState: 0 IDLE, 1 ACTIVE, 2 HOLDING, 3 DIALING, 4 ALERTING, 5 INCOMING, 6 WAITING, 7 DISCONNECTED, 8 DISCONNECTING
_PRECISE_MAP = {0: "IDLE", 1: "ACTIVE", 2: "ACTIVE", 3: "DIALING", 4: "ALERTING", 7: "ENDED", 8: "ENDED"}
_SPEAKER_HINTS = ("speaker", "스피커", "realtek", "headphone", "헤드폰", "hdmi", "display", "default", "기본")


class Adb:
    def __init__(self, serial: str | None = None, adb: str | None = None):
        self.adb = adb or os.environ.get("AICC_ADB") or shutil.which("adb")
        self.serial = serial or os.environ.get("AICC_ADB_SERIAL")

    def available(self) -> bool:
        return bool(self.adb)

    def run(self, *args: str, timeout: float = 8.0) -> str:
        if not self.adb:
            raise DeviceError("adb 실행 파일을 찾지 못했습니다(Android platform-tools 설치 필요)")
        cmd = [self.adb] + (["-s", self.serial] if self.serial else []) + list(args)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as e:
            raise DeviceError(f"adb 응답 없음: {' '.join(args[:2])}") from e
        if r.returncode != 0:
            raise DeviceError(f"adb 오류({r.returncode}): {r.stderr.strip()[:200]}")
        return r.stdout

    def devices(self) -> list[dict]:
        out = self.run("devices", "-l") if self.adb else ""
        res = []
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                info = {"serial": parts[0], "state": parts[1]}
                for p in parts[2:]:
                    if ":" in p:
                        k, v = p.split(":", 1)
                        info[k] = v
                res.append(info)
        return res

    def getprop(self, key: str) -> str:
        return self.run("shell", "getprop", key).strip()


class AudioRoute:  # pragma: no cover - 실기기·사운드카드 필요
    """운영자가 명시적으로 지정한 입력(RX)·출력(TX) 오디오 장치. sounddevice(선택 설치) 사용."""

    def __init__(self, rx_device: str | int, tx_device: str | int):
        import sounddevice as sd  # 지연 import

        self.sd = sd
        self.rx_device = rx_device
        self.tx_device = tx_device
        out_info = sd.query_devices(tx_device, "output")
        default_out = sd.query_devices(kind="output")
        name = str(out_info["name"]).lower()
        if out_info["name"] == default_out["name"] or any(h in name for h in _SPEAKER_HINTS):
            raise DeviceError(f"TX 출력 장치가 PC 기본/스피커 장치로 보입니다: {out_info['name']} — 통화 음성 누출 방지를 위해 거부")
        self._in = sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", device=rx_device,
                                     blocksize=SAMPLE_RATE * FRAME_MS // 1000)
        self._out = sd.RawOutputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", device=tx_device,
                                       blocksize=SAMPLE_RATE * FRAME_MS // 1000)
        self._in.start()
        self._out.start()
        self.t0 = time.monotonic()

    def now_ms(self) -> int:
        return int((time.monotonic() - self.t0) * 1000)

    def read(self, max_ms: int) -> list[Frame]:
        frames = []
        n = max(1, max_ms // FRAME_MS)
        for _ in range(n):
            data, _overflow = self._in.read(SAMPLE_RATE * FRAME_MS // 1000)
            frames.append(Frame(bytes(data), self.now_ms()))
        return frames

    def write(self, frames: list[Frame]) -> int:
        for f in frames:
            self._out.write(f.pcm)
        return len(frames)

    def close(self) -> None:
        for s in (self._in, self._out):
            try:
                s.stop()
                s.close()
            except Exception:  # noqa: BLE001
                pass


class A24Device(DeviceAdapter):
    """rx: ScrcpyRx(권장, USB) 또는 AudioRoute / tx: TxOutput 또는 AudioRoute.
    route 하나만 주면 RX·TX 모두 그 장치를 쓴다(이전 방식 호환)."""

    kind = "a24"

    def __init__(self, adb: Adb | None = None, route: AudioRoute | None = None, state_file: Path | None = None,
                 *, rx=None, tx=None):  # noqa: ANN001
        self.adb = adb or Adb()
        self.route = route
        self.rx = rx if rx is not None else route
        self.tx = tx if tx is not None else route
        self.state_file = state_file
        self._t0 = time.monotonic()
        self._precise_seen = False
        self._rx_running = False

    def now_ms(self) -> int:
        if self.rx is not None and hasattr(self.rx, "now_ms"):
            return self.rx.now_ms()
        return int((time.monotonic() - self._t0) * 1000)

    def _probe_state(self) -> dict:
        if self.state_file and self.state_file.is_file():
            try:
                return json.loads(self.state_file.read_text(encoding="utf-8"))
            except ValueError:
                return {}
        return {}

    def serial(self) -> str | None:
        try:
            return self.adb.getprop("ro.serialno") or None
        except DeviceError:
            return None

    def probe_capabilities(self) -> dict[str, str]:
        ps = self._probe_state()
        caps = {k: "unknown" for k in ("dial", "hangup", "answer_detection", "rx_audio", "tx_audio", "recording")}
        try:
            if not any(d["state"] == "device" for d in self.adb.devices()):
                return {k: "unsupported" for k in caps}
        except DeviceError:
            return {k: "unsupported" for k in caps}
        # aicc-probe 의 승인된 시험 통화에서 확인된 항목만 supported 로 올린다(사람 검증은 서버 proof 로 별도).
        for k in ("dial", "hangup", "answer_detection"):
            if ps.get(f"{k}_ok") is True:
                caps[k] = "supported"
        caps["rx_audio"] = "unsupported" if self.rx is None else ("supported" if ps.get("rx_audio_ok") else "unknown")
        caps["tx_audio"] = "unsupported" if self.tx is None else ("supported" if ps.get("tx_audio_ok") else "unknown")
        if self.rx is None or self.tx is None:
            caps["recording"] = "unsupported"
        elif ps.get("recording_ok"):
            caps["recording"] = "supported"
        return caps

    def _start_rx(self) -> None:
        if self.rx is not None and hasattr(self.rx, "start") and not self._rx_running:
            self.rx.start()  # 발신 직후 시작해 상대 첫마디를 놓치지 않게 한다
            self._rx_running = True

    def _stop_rx(self) -> None:
        if self.rx is not None and hasattr(self.rx, "stop") and self._rx_running:
            self.rx.stop()
        self._rx_running = False

    def dial(self, number: str) -> str:
        if not re.fullmatch(r"0\d{8,10}", number):
            raise DeviceError("발신 번호 형식 오류")
        self.adb.run("shell", "am", "start", "-a", "android.intent.action.CALL", "-d", f"tel:{number}")
        try:
            self._start_rx()
        except DeviceError:
            self.adb.run("shell", "input", "keyevent", "KEYCODE_ENDCALL")
            raise
        return f"adb-dial-{int(time.time())}"

    def hangup(self) -> None:
        try:
            self.adb.run("shell", "input", "keyevent", "KEYCODE_ENDCALL")
        finally:
            self._stop_rx()

    def get_call_state(self) -> CallStatus:
        out = self.adb.run("shell", "dumpsys", "telephony.registry", timeout=5)
        m = _PRECISE.search(out)
        legacy = _CALLSTATE.search(out)
        detail = {"legacy": int(legacy.group(1)) if legacy else None}
        if m:
            self._precise_seen = True
            code = int(m.group(1))
            detail["precise"] = code
            return CallStatus(_PRECISE_MAP.get(code, "UNKNOWN"), detail)
        if legacy and legacy.group(1) == "0":
            return CallStatus("IDLE", detail)
        # OFFHOOK 만으로는 응답 여부를 알 수 없다
        return CallStatus("UNKNOWN", detail)

    def read_rx_frames(self, max_ms: int) -> list[Frame]:
        if self.rx is None:
            raise DeviceUnverified("RX 오디오 경로가 지정·검증되지 않았습니다")
        if hasattr(self.rx, "start") and not self._rx_running:
            time.sleep(max_ms / 1000)  # 통화 밖(종료 확인 대기 등): 캡처 없이 대기만
            return []
        return self.rx.read(max_ms)

    def write_tx_frames(self, frames: list[Frame]) -> int:
        if self.tx is None:
            raise DeviceUnverified("TX 오디오 경로가 지정·검증되지 않았습니다(scrcpy 는 송신을 지원하지 않음)")
        return self.tx.write(frames)

    def tx_segment_done(self, label: str) -> None:
        pass
