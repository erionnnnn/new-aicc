"""scrcpy 로 A24 통화 수신 음성(RX)을 USB 로 받는다.

근거(scrcpy 공식 문서 doc/audio.md, doc/recording.md, 2026-10-26 확인)
- `--audio-source=voice-call-downlink` : 통화의 상대(수신) 쪽 음성만 캡처. Android 11 이상.
- `--audio-codec=raw` + `--record=file.wav` : 무압축 16bit PCM WAV 로 기록.
- `--no-window`(영상·제어 없음) + `--no-playback` : PC 스피커로 재생하지 않음(통화 중 PC 무출력 요구).
- scrcpy 에는 PC 소리를 통화로 보내는(TX) 기능이 문서에 없다 → TX 는 별도 출력 장치(TxOutput).

동작: 발신 직후 scrcpy 를 띄워 임시 WAV 에 기록시키고, 그 파일을 끝에서부터 읽어(tail) 20ms 프레임으로 넘긴다.
scrcpy 오디오는 48kHz 스테레오로 가정하되 WAV 머리글의 실제 값을 읽어 16kHz 모노로 변환한다.
파일 쓰기 버퍼 때문에 지연이 생길 수 있으며 실제 지연은 실기기에서 측정해야 한다(UNVERIFIED).
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import tempfile
import time
from array import array
from pathlib import Path

from .device import FRAME_MS, SAMPLE_RATE, DeviceError, Frame

SCRCPY_SOURCES = ("voice-call-downlink", "voice-call", "voice-call-uplink")


REPO_ROOT = Path(__file__).resolve().parents[3]
BUNDLED_DIR = REPO_ROOT / "tools" / "scrcpy"  # scripts/setup.ps1 이 scrcpy 5.0.1 을 여기에 설치


def _bundled(name: str) -> str | None:
    for cand in (BUNDLED_DIR / f"{name}.exe", BUNDLED_DIR / name):
        if cand.is_file():
            return str(cand)
    return None


def scrcpy_path() -> str | None:
    return os.environ.get("AICC_SCRCPY") or _bundled("scrcpy") or shutil.which("scrcpy")


def bundled_adb() -> str | None:
    return _bundled("adb")


def build_command(scrcpy: str, record_path: str, *, serial: str | None = None,
                  source: str = "voice-call-downlink", audio_buffer_ms: int = 30) -> list[str]:
    if source not in SCRCPY_SOURCES:
        raise ValueError(f"지원하지 않는 audio-source: {source}")
    cmd = [scrcpy]
    if serial:
        cmd += ["-s", serial]
    cmd += [
        "--no-window",            # 영상·제어 없이 오디오만
        "--no-playback",          # PC 스피커로 재생하지 않음
        f"--audio-source={source}",
        "--audio-codec=raw",
        f"--audio-buffer={audio_buffer_ms}",
        "--require-audio",        # 오디오 캡처 실패 시 조용히 넘어가지 않고 종료
        f"--record={record_path}",
        "--record-format=wav",
    ]
    return cmd


class WavTail:
    """기록 중인 WAV 파일을 이어서 읽는다. 머리글의 데이터 크기 값은 기록 중 0일 수 있어 무시한다."""

    def __init__(self, path: Path):
        self.path = path
        self.fh = None
        self.channels = 0
        self.rate = 0
        self.bits = 0
        self.data_start: int | None = None
        self._rem = b""

    def _open(self) -> bool:
        if self.fh is None:
            if not self.path.exists():
                return False
            self.fh = self.path.open("rb")
        if self.data_start is not None:
            return True
        self.fh.seek(0)
        head = self.fh.read(4096)
        if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            return False
        pos = 12
        fmt_ok = False
        while pos + 8 <= len(head):
            cid, size = head[pos:pos + 4], struct.unpack("<I", head[pos + 4:pos + 8])[0]
            body = pos + 8
            if cid == b"fmt ":
                if body + 16 > len(head):
                    return False
                tag, ch, rate, _br, _ba, bits = struct.unpack("<HHIIHH", head[body:body + 16])
                if tag not in (1, 0xFFFE) or bits != 16:
                    raise DeviceError(f"scrcpy WAV 형식이 16bit PCM 이 아닙니다(tag={tag}, bits={bits})")
                self.channels, self.rate, self.bits = ch, rate, bits
                fmt_ok = True
            elif cid == b"data":
                if not fmt_ok:
                    return False
                self.data_start = body
                self.fh.seek(body)
                return True
            if size == 0 or size == 0xFFFFFFFF:
                if cid != b"data":
                    return False
            pos = body + size + (size & 1)
        return False

    def read_pcm16k(self) -> bytes:
        """새로 기록된 부분을 16kHz mono 16bit 로 변환해 돌려준다(없으면 b"")."""
        if not self._open():
            return b""
        assert self.fh is not None
        raw = self._rem + self.fh.read()
        frame_bytes = 2 * self.channels
        if self.rate % SAMPLE_RATE == 0:
            frame_bytes *= self.rate // SAMPLE_RATE  # 간축 묶음 단위로 잘라 샘플 손실 방지
        usable = len(raw) - len(raw) % frame_bytes
        self._rem = raw[usable:]
        if usable == 0:
            return b""
        return to_mono16k(raw[:usable], self.channels, self.rate)

    def close(self) -> None:
        if self.fh:
            self.fh.close()
            self.fh = None


def to_mono16k(pcm: bytes, channels: int, rate: int) -> bytes:
    a = array("h")
    a.frombytes(pcm)
    if channels > 1:
        mono = array("h", (sum(a[i:i + channels]) // channels for i in range(0, len(a), channels)))
    else:
        mono = a
    if rate == SAMPLE_RATE:
        return mono.tobytes()
    if rate % SAMPLE_RATE == 0:
        k = rate // SAMPLE_RATE  # 48k→16k: 3개 평균(간이 저역 통과 + 간축)
        n = len(mono) // k
        return array("h", (sum(mono[i * k:(i + 1) * k]) // k for i in range(n))).tobytes()
    from .audio import resample_linear
    return resample_linear(mono.tobytes(), rate, SAMPLE_RATE)


class ScrcpyRx:
    """통화 1건 동안 scrcpy 프로세스를 띄워 RX 프레임을 공급한다."""

    def __init__(self, *, serial: str | None = None, source: str | None = None, scrcpy: str | None = None,
                 workdir: Path | None = None):
        self.scrcpy = scrcpy or scrcpy_path()
        self.serial = serial
        self.source = source or os.environ.get("AICC_SCRCPY_SOURCE", "voice-call-downlink")
        self.workdir = workdir
        self.proc: subprocess.Popen | None = None
        self.tail: WavTail | None = None
        self.path: Path | None = None
        self._buf = b""
        self._t0 = time.monotonic()
        self._emitted_ms = 0

    def available(self) -> bool:
        return bool(self.scrcpy)

    def now_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)

    def start(self) -> None:
        if not self.scrcpy:
            raise DeviceError("scrcpy 실행 파일을 찾지 못했습니다(AICC_SCRCPY 또는 PATH 설정)")
        self.stop()
        d = self.workdir or Path(tempfile.gettempdir())
        d.mkdir(parents=True, exist_ok=True)
        self.path = d / f"aicc_rx_{os.getpid()}_{int(time.time() * 1000)}.wav"
        cmd = build_command(self.scrcpy, str(self.path), serial=self.serial, source=self.source)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=flags)
        self.tail = WavTail(self.path)
        self._buf = b""
        self._t0 = time.monotonic()
        self._emitted_ms = 0

    def _check_alive(self) -> None:
        if self.proc and self.proc.poll() is not None:
            err = (self.proc.stderr.read() if self.proc.stderr else b"").decode("utf-8", "ignore")[-300:]
            self.proc = None
            raise DeviceError(f"scrcpy 오디오 캡처가 종료되었습니다: {err.strip() or '원인 불명'}")

    def read(self, max_ms: int) -> list[Frame]:
        """max_ms 동안 기다리며 모인 RX 를 20ms 프레임으로 돌려준다."""
        if self.tail is None:
            raise DeviceError("RX 캡처가 시작되지 않았습니다")
        deadline = time.monotonic() + max_ms / 1000
        step = SAMPLE_RATE * FRAME_MS // 1000 * 2
        frames: list[Frame] = []
        while True:
            self._check_alive()
            self._buf += self.tail.read_pcm16k()
            while len(self._buf) >= step:
                frames.append(Frame(self._buf[:step], self._emitted_ms))
                self._buf = self._buf[step:]
                self._emitted_ms += FRAME_MS
            if time.monotonic() >= deadline:
                return frames
            time.sleep(0.01)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        if self.tail:
            self.tail.close()
            self.tail = None
        if self.path and self.path.exists():
            try:
                self.path.unlink()  # 원본 통화 음성 임시 파일은 남기지 않는다(녹음은 서버에 암호화 업로드)
            except OSError:
                pass
        self.path = None


class TxOutput:  # pragma: no cover - 사운드카드 필요
    """ARS·AI 음성을 내보낼 PC 출력 장치. PC 기본 출력·스피커로 보이는 장치는 거부한다."""

    SPEAKER_HINTS = ("speaker", "스피커", "realtek", "headphone", "헤드폰", "hdmi", "display")

    def __init__(self, device: str | int):
        import sounddevice as sd

        info = sd.query_devices(device, "output")
        default = sd.query_devices(kind="output")
        name = str(info["name"]).lower()
        if info["name"] == default["name"] or any(h in name for h in self.SPEAKER_HINTS):
            raise DeviceError(f"TX 출력 장치가 PC 기본/스피커 장치로 보입니다: {info['name']} — 통화 음성 누출 방지를 위해 거부")
        self._out = sd.RawOutputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", device=device,
                                       blocksize=SAMPLE_RATE * FRAME_MS // 1000)
        self._out.start()

    def write(self, frames: list[Frame]) -> int:
        for f in frames:
            self._out.write(f.pcm)
        return len(frames)

    def close(self) -> None:
        try:
            self._out.stop()
            self._out.close()
        except Exception:  # noqa: BLE001
            pass
