"""PC → 상대방(TX): 휴대폰의 자체 송출 모듈에 ADB 로 PCM 을 보내 통화 송신 경로에 넣는다.

기존 프로그램 구조(사용자 설명, 2026-10-26):
  녹음 ARS·AI 음성 → PCM 변환 → ADB 로 휴대폰 자체 송출 모듈에 전달 → 모듈이 통화 TX 에 주입.

이 어댑터는 모듈 쪽 규격을 설정으로 받는다(모듈 소스는 이 저장소에 없음).
- AICC_TX_ADB_TARGET : 모듈이 듣는 소켓. 예) tcp:28200, localabstract:aicc_tx
- AICC_TX_RATE       : 모듈이 받는 샘플레이트(기본 16000)
- AICC_TX_CHANNELS   : 1 또는 2(기본 1), 16bit little-endian PCM
- AICC_TX_START_CMD  : (선택) 모듈을 띄우는 adb 명령. 예) shell am startservice -n com.example.tx/.TxService
- AICC_TX_LOCAL_PORT : (선택) PC 쪽 포트(기본 28210)

PC 스피커는 쓰지 않는다. 소켓이 끊기면 DeviceError 로 통화를 멈춘다(스피커로 자동 전환하지 않음).
"""

from __future__ import annotations

import shlex
import socket
import time
from array import array

from .device import FRAME_MS, SAMPLE_RATE, DeviceError, Frame


def convert_pcm(pcm16k_mono: bytes, rate: int, channels: int) -> bytes:
    if rate == SAMPLE_RATE and channels == 1:
        return pcm16k_mono
    a = array("h")
    a.frombytes(pcm16k_mono)
    if rate != SAMPLE_RATE:
        if rate % SAMPLE_RATE == 0:
            k = rate // SAMPLE_RATE
            up = array("h")
            for i in range(len(a)):  # 선형 보간 업샘플
                x0 = a[i]
                x1 = a[i + 1] if i + 1 < len(a) else x0
                for j in range(k):
                    up.append(x0 + (x1 - x0) * j // k)
            a = up
        else:
            from .audio import resample_linear
            a = array("h")
            a.frombytes(resample_linear(pcm16k_mono, SAMPLE_RATE, rate))
    if channels == 2:
        st = array("h")
        for v in a:
            st.append(v)
            st.append(v)
        a = st
    return a.tobytes()


class AdbSocketTx:
    def __init__(self, adb, target: str, *, rate: int = 16000, channels: int = 1,  # noqa: ANN001
                 start_cmd: str | None = None, local_port: int = 28210, host: str = "127.0.0.1",
                 connect_timeout_s: float = 5.0):
        if not (target.startswith("tcp:") or target.startswith("localabstract:") or target.startswith("localfilesystem:")):
            raise ValueError("AICC_TX_ADB_TARGET 은 tcp:포트 / localabstract:이름 형식입니다")
        if channels not in (1, 2):
            raise ValueError("AICC_TX_CHANNELS 는 1 또는 2")
        self.adb = adb
        self.target = target
        self.rate = rate
        self.channels = channels
        self.start_cmd = start_cmd
        self.local_port = local_port
        self.host = host
        self.connect_timeout_s = connect_timeout_s
        self.sock: socket.socket | None = None
        self._next_t = 0.0
        self.bytes_sent = 0

    def start(self) -> None:
        self.stop()
        if self.start_cmd:
            self.adb.run(*shlex.split(self.start_cmd), timeout=15)
        self.adb.run("forward", f"tcp:{self.local_port}", self.target, timeout=10)
        deadline = time.monotonic() + self.connect_timeout_s
        last: Exception | None = None
        while time.monotonic() < deadline:
            try:
                s = socket.create_connection((self.host, self.local_port), timeout=2)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.sock = s
                self._next_t = time.monotonic()
                return
            except OSError as e:
                last = e
                time.sleep(0.2)
        raise DeviceError(f"휴대폰 송출 모듈에 연결하지 못했습니다({self.target}): {type(last).__name__ if last else ''} "
                          "— 모듈이 실행 중인지, AICC_TX_ADB_TARGET 이 맞는지 확인하세요")

    def write(self, frames: list[Frame]) -> int:
        if self.sock is None:
            raise DeviceError("송출 모듈 연결이 없습니다")
        for f in frames:
            data = convert_pcm(f.pcm, self.rate, self.channels)
            try:
                self.sock.sendall(data)
            except OSError as e:
                self.stop()
                raise DeviceError(f"송출 모듈 연결이 끊겼습니다: {type(e).__name__}") from e
            self.bytes_sent += len(data)
            # 실시간 속도로 보낸다(모듈 버퍼 과다·끼어들기 지연 방지)
            self._next_t = max(self._next_t, time.monotonic() - 0.06) + FRAME_MS / 1000
            delay = self._next_t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        return len(frames)

    def stop(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        try:
            self.adb.run("forward", "--remove", f"tcp:{self.local_port}", timeout=5)
        except Exception:  # noqa: BLE001 - 이미 없을 수 있음
            pass


def tx_from_env(env: dict[str, str], adb) -> AdbSocketTx | None:  # noqa: ANN001
    target = (env.get("AICC_TX_ADB_TARGET") or "").strip()
    if not target:
        return None
    return AdbSocketTx(adb, target, rate=int(env.get("AICC_TX_RATE") or 16000),
                       channels=int(env.get("AICC_TX_CHANNELS") or 1),
                       start_cmd=(env.get("AICC_TX_START_CMD") or "").strip() or None,
                       local_port=int(env.get("AICC_TX_LOCAL_PORT") or 28210))

