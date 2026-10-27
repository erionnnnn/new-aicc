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

import hashlib
import os
import struct
import subprocess
import time
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
        self._queue: list[bytes] = []
        self._stopped = False
        self._dur = 0.0
        self._deadline = 0.0
        self._cur_stop: str | None = None
        self._preloaded: dict[str, tuple[str, str, int]] = {}

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
        """모아 둔 안내 음성을 송출하고 재생이 끝날 때까지 기다린다(동기 방식)."""
        pcm = bytes(self._buf)
        self._buf.clear()
        if self.call_id is None or not pcm:
            return
        self.play_async(pcm)
        while not self.poll_playback(wait_s=0.05):
            pass

    # ---- 미리 올리기: 통화 연결 전(신호 가는 동안) 고정 안내를 휴대폰에 넣어 두면 재생 때 push 를 생략한다 ----
    def preload(self, key: str, pcm: bytes) -> bool:
        """30초 이하 고정 안내만 미리 올린다. 성공하면 True."""
        if self.call_id is None or not pcm or len(pcm) > MAX_SECONDS * BYTES_PER_SEC:
            return False
        # 송출 모듈은 경로가 정확히 /data/local/tmp/goodcare-uplink-<32hex>.gcar 여야만 실행한다
        # (Uplink.java 'Invalid session file paths') → 미리 올리는 안내마다 별도 32자리 ID 를 쓴다
        gcar = f"{DEVICE_DIR}/goodcare-uplink-{uuid.uuid4().hex}.gcar"
        self._push_gcar(pcm, gcar)
        self._preloaded[key] = (hashlib.sha256(pcm).hexdigest(), gcar, len(pcm))
        return True

    def _push_gcar(self, pcm: bytes, gcar: str) -> None:
        local = self.workdir / (gcar.rsplit("/", 1)[-1])
        local.write_bytes(gcar_bytes(pcm))
        try:
            self.adb.run("push", str(local), gcar, timeout=30)
        finally:
            try:
                local.unlink()
            except OSError:
                pass
        for path in (gcar, gcar + ".stop"):
            if path not in self._remote:
                self._remote.append(path)

    # ---- 비동기 재생: 재생 중에도 러너가 상대 음성을 계속 듣고 끼어들기를 처리할 수 있게 한다 ----
    def play_async(self, pcm: bytes, key: str | None = None) -> None:
        if self.call_id is None:
            raise DeviceError("송출 모듈이 준비되지 않았습니다")
        self._stopped = False
        pre = self._preloaded.get(key) if key else None
        if pre and pre[0] == hashlib.sha256(pcm).hexdigest():
            self._queue = []
            self._run(pre[1], pre[2])  # 이미 휴대폰에 있음: 실행 명령만
            return
        self._queue = split_pcm(pcm)
        self._start_part(self._queue.pop(0))

    def playing(self) -> bool:
        return self._proc is not None or bool(self._queue)

    def poll_playback(self, wait_s: float = 0.0) -> bool:
        """재생이 모두 끝났으면 True. 모듈 오류는 DeviceError."""
        if self._proc is None:
            return True
        try:
            out, err = self._proc.communicate(timeout=wait_s or 0.001)
        except subprocess.TimeoutExpired:
            if time.monotonic() > self._deadline:
                self.stop_playback()
                raise DeviceError("송출 모듈이 제한 시간 안에 끝나지 않았습니다") from None
            return False
        rc = self._proc.returncode
        self._proc = None
        self._finish(out, err, rc)
        if self._queue and not self._stopped:
            self._start_part(self._queue.pop(0))
            return False
        self._queue = []
        return True

    def _start_part(self, pcm: bytes) -> None:
        gcar = self._dev(".gcar")
        self._push_gcar(pcm, gcar)
        self._run(gcar, len(pcm))

    def _run(self, gcar: str, nbytes: int) -> None:
        stop, jar = gcar + ".stop", self._dev(".jar")
        # 정리·권한·실행을 adb 한 번으로 묶어 송출 시작 지연을 줄인다(실행 명령 자체는 검증된 형식 그대로)
        cmd = ["shell", "-T", f"rm -f {stop}; chmod 600 {jar} {gcar}; CLASSPATH={jar} app_process / {MAIN_CLASS} "
               f"--play-file {gcar} {stop}"]
        self._cur_stop = stop
        self._dur = nbytes / BYTES_PER_SEC
        self._deadline = time.monotonic() + self._dur + self.extra_wait_s
        self._proc = self.adb.popen(*cmd)

    def _finish(self, out: bytes | None, err: bytes | None, rc: int | None) -> None:
        events = parse_events((out or b"") + b"\n" + (err or b""))
        self.last_events = events
        bad = next((e for e in events if e.get("kind") == "error"), None)
        if bad or rc not in (0, None):
            msg = bad.get("message") if bad else (err or b"").decode("utf-8", "ignore")[-200:]
            raise DeviceError(f"송출 모듈 실행 실패(코드 {rc}): {msg}")
        if not any(e.get("kind") in ("written", "stopped") for e in events) and events:
            raise DeviceError("송출 모듈이 완료 신호(written)를 보내지 않았습니다")
        self.played_ms += int(self._dur * 1000)

    def stop_playback(self) -> None:
        """재생 중단 신호(.stop 파일)."""
        self._queue = []
        self._stopped = True
        if self.call_id is None:
            return
        try:
            self.adb.run("shell", f"touch {self._cur_stop or self._dev('.gcar.stop')}", timeout=10)
        except DeviceError:
            pass
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None

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
        self._preloaded = {}
        self._cur_stop = None
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

