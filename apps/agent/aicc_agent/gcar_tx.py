"""PC → 상대방(TX): 기존 검증 방식 그대로 — .gcar 파일을 adb push 하고 app_process 로 송출 모듈(JAR)을 실행.

사용자 확인 규격(2026-10-26, 기존 a24_usb 구현):
- 모듈 파일 : goodcare-uplink.jar, 실행 클래스 com.goodcare.a24.Uplink
- 실행 명령 : adb -s <SERIAL> shell -T CLASSPATH=/data/local/tmp/goodcare-uplink-<ID>.jar app_process /
              com.goodcare.a24.Uplink --play-file <dev>/goodcare-uplink-<ID>.gcar <dev>/goodcare-uplink-<ID>.gcar.stop
  (<ID> = 32자리 소문자 UUID hex)
- .gcar     : b"GCAR" + 샘플레이트 + 채널 수 + PCM 바이트 길이(각 32bit 빅엔디언) + PCM16 리틀엔디언
- 형식      : 16000Hz, 모노, 파일당 최대 30초
- 실제 이동통신 통화가 연결된 상태에서만 실행한다(러너가 응답 확인 후 재생).

동작: 러너가 안내 1건을 프레임으로 넘기면 모아 두었다가(write), 안내가 끝나면(flush) 파일로 만들어 송출하고
재생이 끝날 때까지 기다린다. 중단(stop_tx)은 .stop 파일을 만들어 모듈에 알린다.
"""

from __future__ import annotations

import os
import struct
import subprocess
import uuid
from pathlib import Path

from .device import SAMPLE_RATE, DeviceError, Frame

DEVICE_DIR = "/data/local/tmp"
MAIN_CLASS = "com.goodcare.a24.Uplink"
MAX_SECONDS = 30
BYTES_PER_SEC = SAMPLE_RATE * 2


def gcar_bytes(pcm16k_mono: bytes, rate: int = SAMPLE_RATE, channels: int = 1) -> bytes:
    if len(pcm16k_mono) % 2:
        pcm16k_mono = pcm16k_mono[:-1]
    return b"GCAR" + struct.pack(">III", rate, channels, len(pcm16k_mono)) + pcm16k_mono


def split_pcm(pcm: bytes, max_seconds: int = MAX_SECONDS) -> list[bytes]:
    step = max_seconds * BYTES_PER_SEC
    return [pcm[i:i + step] for i in range(0, len(pcm), step)] or [b""]


class GcarFileTx:
    def __init__(self, adb, jar_path: str | os.PathLike, *, workdir: Path | None = None,  # noqa: ANN001
                 extra_wait_s: float = 10.0):
        self.adb = adb
        self.jar = Path(jar_path)
        self.workdir = workdir or Path(os.environ.get("AICC_DATA_DIR", ".aicc")) / "tmp"
        self.extra_wait_s = extra_wait_s
        self.call_id: str | None = None
        self._buf = bytearray()
        self._proc: subprocess.Popen | None = None
        self._remote: list[str] = []
        self.played_ms = 0
        self.last_events: list[dict] = []

    # ---- 경로 ----
    def _dev(self, suffix: str) -> str:
        return f"{DEVICE_DIR}/goodcare-uplink-{self.call_id}{suffix}"

    def start(self) -> None:
        """통화 1건 시작: JAR 을 휴대폰에 넣는다(통화마다 새 ID)."""
        if not self.jar.is_file():
            raise DeviceError(f"송출 모듈 파일이 없습니다: {self.jar} (설정 > 단말 > 이 PC 에이전트에서 JAR 경로 지정)")
        self.stop()
        self.call_id = uuid.uuid4().hex  # 32자리 소문자
        self.workdir.mkdir(parents=True, exist_ok=True)
        verify_bundled(self.jar)
        self.adb.run("push", str(self.jar), self._dev(".jar"), timeout=30)
        self._remote = [self._dev(".jar")]
        self._buf.clear()

    def write(self, frames: list[Frame]) -> int:
        if self.call_id is None:
            raise DeviceError("송출 모듈이 준비되지 않았습니다")
        for f in frames:
            self._buf.extend(f.pcm)
        return len(frames)

    def flush(self) -> None:
        """모아 둔 안내 음성을 송출하고 재생이 끝날 때까지 기다린다."""
        if self.call_id is None or not self._buf:
            self._buf.clear()
            return
        pcm = bytes(self._buf)
        self._buf.clear()
        for part in split_pcm(pcm):
            self._play(part)

    def _play(self, pcm: bytes) -> None:
        local = self.workdir / f"goodcare-uplink-{self.call_id}.gcar"
        local.write_bytes(gcar_bytes(pcm))
        gcar, stop = self._dev(".gcar"), self._dev(".gcar.stop")
        try:
            self.adb.run("shell", f"rm -f {stop}", timeout=10)
            self.adb.run("push", str(local), gcar, timeout=30)
            self.adb.run("shell", f"chmod 600 {self._dev('.jar')} {gcar}", timeout=10)
        finally:
            try:
                local.unlink()
            except OSError:
                pass
        if gcar not in self._remote:
            self._remote += [gcar, stop]
        cmd = ["shell", "-T", f"CLASSPATH={self._dev('.jar')}", "app_process", "/", MAIN_CLASS,
               "--play-file", gcar, stop]
        dur = len(pcm) / BYTES_PER_SEC
        self._proc = self.adb.popen(*cmd)
        try:
            out, err = self._proc.communicate(timeout=dur + self.extra_wait_s)
            rc = self._proc.returncode
        except subprocess.TimeoutExpired:
            self.stop_playback()
            raise DeviceError("송출 모듈이 제한 시간 안에 끝나지 않았습니다") from None
        finally:
            self._proc = None
        events = parse_events((out or b"") + b"\n" + (err or b""))
        self.last_events = events
        bad = next((e for e in events if e.get("kind") == "error"), None)
        if bad or rc not in (0, None):
            msg = bad.get("message") if bad else (err or b"").decode("utf-8", "ignore")[-200:]
            raise DeviceError(f"송출 모듈 실행 실패(코드 {rc}): {msg}")
        if not any(e.get("kind") in ("written", "stopped") for e in events) and events:
            raise DeviceError("송출 모듈이 완료 신호(written)를 보내지 않았습니다")
        self.played_ms += int(dur * 1000)

    def stop_playback(self) -> None:
        """재생 중단 신호(.stop 파일)."""
        if self.call_id is None:
            return
        try:
            self.adb.run("shell", f"touch {self._dev('.gcar.stop')}", timeout=10)
        except DeviceError:
            pass
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    def stop(self) -> None:
        """통화 종료: 재생 중단, 휴대폰 임시 파일 삭제."""
        self._buf.clear()
        if self.call_id is not None:
            self.stop_playback()
            if self._remote:
                try:
                    self.adb.run("shell", "rm -f " + " ".join(self._remote), timeout=10)
                except DeviceError:
                    pass
        self._remote = []
        self.call_id = None


BUNDLED_JAR = Path(__file__).resolve().parent / "bin" / "goodcare-uplink.jar"  # GitHub Actions 빌드 결과


def verify_bundled(jar: Path) -> None:
    """저장소 빌드 JAR 은 매니페스트의 해시와 일치해야 쓴다(기존 checked_helper 와 같은 원칙)."""
    import hashlib
    import json
    man = jar.with_suffix(".json")
    if not man.is_file():
        return
    meta = json.loads(man.read_text(encoding="utf-8"))
    if meta.get("protocol") != "GCAR-1" or hashlib.sha256(jar.read_bytes()).hexdigest() != meta.get("sha256"):
        raise DeviceError("송출 모듈 무결성 확인 실패(goodcare-uplink.jar 해시 불일치)")


def parse_events(raw: bytes) -> list[dict]:
    """Uplink.java 는 한 줄에 하나씩 {"kind":..., "message":...} JSON 을 출력한다."""
    import json
    out = []
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def default_jar() -> Path | None:
    from .scrcpy_rx import REPO_ROOT
    for cand in (REPO_ROOT / "tools" / "uplink" / "goodcare-uplink.jar", BUNDLED_JAR):
        if cand.is_file():
            return cand
    return None

