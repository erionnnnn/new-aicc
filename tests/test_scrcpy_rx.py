"""scrcpy RX 어댑터 시험. 실제 scrcpy·A24 대신 WAV 를 조금씩 써 나가는 가짜 scrcpy 스크립트를 쓴다."""

from __future__ import annotations

import math
import struct
import sys
import time
from array import array

import pytest
from aicc_agent.a24 import A24Device, Adb
from aicc_agent.device import DeviceError
from aicc_agent.scrcpy_rx import ScrcpyRx, WavTail, build_command, to_mono16k


def wav_header(channels=2, rate=48000, data_size=0):
    br = rate * channels * 2
    return (b"RIFF" + struct.pack("<I", 36 + data_size) + b"WAVE" + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels,
            rate, br, channels * 2, 16) + b"data" + struct.pack("<I", data_size))


def stereo48k(ms, freq=300, amp=4000):
    n = 48 * ms
    vals = []
    for i in range(n):
        v = int(amp * math.sin(2 * math.pi * freq * i / 48000))
        vals += [v, v]
    return struct.pack(f"<{len(vals)}h", *vals)


def test_command_never_plays_on_pc_and_uses_downlink():
    cmd = build_command("scrcpy", "x.wav", serial="R58T")
    assert "--no-playback" in cmd and "--no-window" in cmd and "--no-video" in cmd
    assert "--audio-source=voice-call-downlink" in cmd and "--audio-codec=raw" in cmd
    assert cmd[1:3] == ["-s", "R58T"] and "--require-audio" in cmd
    with pytest.raises(ValueError):
        build_command("scrcpy", "x.wav", source="output")  # 휴대폰 전체 출력 캡처 금지


def test_to_mono16k_from_48k_stereo():
    out = to_mono16k(stereo48k(100), 2, 48000)
    a = array("h")
    a.frombytes(out)
    assert len(a) == 1600  # 100ms @16k


def test_wavtail_reads_incrementally_with_zero_size_header(tmp_path):
    p = tmp_path / "rx.wav"
    p.write_bytes(wav_header())  # 기록 중: 데이터 크기 0
    t = WavTail(p)
    assert t.read_pcm16k() == b""
    body = stereo48k(60)
    with p.open("ab") as f:
        f.write(body[:1001])  # 프레임 경계가 아닌 곳에서 끊김
    first = t.read_pcm16k()
    with p.open("ab") as f:
        f.write(body[1001:])
    second = t.read_pcm16k()
    assert (len(first) + len(second)) // 2 == 960  # 60ms @16k
    t.close()


FAKE_SCRCPY = r'''
import math, struct, sys, time
path = [a.split("=",1)[1] for a in sys.argv if a.startswith("--record=")][0]
def hdr():
    return b"RIFF"+struct.pack("<I",36)+b"WAVE"+b"fmt "+struct.pack("<IHHIIHH",16,1,2,48000,192000,4,16)+b"data"+struct.pack("<I",0)
with open(path,"wb") as f:
    f.write(hdr()); f.flush()
    for k in range(40):  # 40 x 20ms = 0.8s
        vals=[]
        for i in range(960):
            v=int(5000*math.sin(2*math.pi*300*(k*960+i)/48000)) if 10<=k<30 else 0
            vals += [v, v]
        f.write(struct.pack("<%dh"%len(vals),*vals)); f.flush(); time.sleep(0.02)
time.sleep(30)
'''


@pytest.fixture()
def fake_scrcpy(tmp_path):
    script = tmp_path / "fake_scrcpy.py"
    script.write_text(FAKE_SCRCPY)
    if sys.platform.startswith("win"):
        launcher = tmp_path / "scrcpy.bat"
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\n')
    else:
        launcher = tmp_path / "scrcpy"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        launcher.chmod(0o755)
    return str(launcher)


def test_scrcpy_rx_streams_frames_and_cleans_up(fake_scrcpy, tmp_path):
    rx = ScrcpyRx(scrcpy=fake_scrcpy, workdir=tmp_path / "w")
    rx.start()
    frames = []
    t0 = time.monotonic()
    while len(frames) < 40 and time.monotonic() - t0 < 10:
        frames += rx.read(200)
    path = rx.path
    rx.stop()
    assert len(frames) >= 40
    assert all(len(f.pcm) == 640 for f in frames)  # 20ms @16k mono 16bit
    assert [f.t_ms for f in frames[:3]] == [0, 20, 40]
    from aicc_agent.audio import rms
    loud = [i for i, f in enumerate(frames[:40]) if rms(f.pcm) > 300]
    assert loud and loud[0] >= 9 and loud[-1] <= 30
    assert path is not None and not path.exists()  # 통화 음성 임시 파일 삭제


def test_scrcpy_exit_raises_device_error(tmp_path):
    bad = tmp_path / ("bad.bat" if sys.platform.startswith("win") else "bad")
    if sys.platform.startswith("win"):
        bad.write_text("@echo ERROR: Could not capture audio 1>&2\n@exit /b 1\n")
    else:
        bad.write_text("#!/bin/sh\necho 'ERROR: Could not capture audio' >&2\nexit 1\n")
        bad.chmod(0o755)
    rx = ScrcpyRx(scrcpy=str(bad), workdir=tmp_path)
    rx.start()
    time.sleep(0.5)
    with pytest.raises(DeviceError, match="capture"):
        rx.read(100)
    rx.stop()


class FakeAdb(Adb):
    def __init__(self):
        super().__init__(adb="adb")
        self.calls = []

    def run(self, *args, timeout=8.0):
        self.calls.append(args)
        if args[:1] == ("devices",):
            return "List of devices attached\nR58T device\n"
        return ""


class FakeRx:
    def __init__(self):
        self.started = self.stopped = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def read(self, ms):
        return []

    def now_ms(self):
        return 0


def test_a24_starts_rx_on_dial_stops_on_hangup_and_tx_locked(tmp_path):
    rx = FakeRx()
    d = A24Device(FakeAdb(), rx=rx, state_file=tmp_path / "s.json")
    caps = d.probe_capabilities()
    assert caps["rx_audio"] == "unknown" and caps["tx_audio"] == "unsupported" and caps["recording"] == "unsupported"
    d.dial("01012345678")
    assert rx.started == 1
    d.hangup()
    assert rx.stopped == 1
    assert d.read_rx_frames(10) == []  # 통화 밖에서는 캡처하지 않음
    from aicc_agent.device import DeviceUnverified
    with pytest.raises(DeviceUnverified):
        d.write_tx_frames([])


def test_adb_socket_tx_sends_converted_pcm_and_cleans_forward():
    import socket as _s
    import threading

    from aicc_agent.adb_tx import AdbSocketTx, convert_pcm
    from aicc_agent.audio import split_frames, tone

    srv = _s.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    got = bytearray()

    def serve():
        c, _ = srv.accept()
        while True:
            b = c.recv(65536)
            if not b:
                break
            got.extend(b)
        c.close()
    th = threading.Thread(target=serve, daemon=True)
    th.start()
    adb = FakeAdb()
    tx = AdbSocketTx(adb, "localabstract:aicc_tx", rate=48000, channels=2, local_port=port,
                     start_cmd="shell am startservice -n com.x/.Tx")
    tx.start()
    frames = split_frames(tone(200), 0)  # 10 frames
    t0 = time.monotonic()
    assert tx.write(frames) == 10
    assert time.monotonic() - t0 >= 0.12  # 실시간 속도 송출
    tx.stop()
    th.join(2)
    assert len(got) == 10 * 960 * 2 * 2  # 20ms @48k stereo 16bit
    assert ("forward", f"tcp:{port}", "localabstract:aicc_tx") in adb.calls
    assert ("shell", "am", "startservice", "-n", "com.x/.Tx") in adb.calls
    assert ("forward", "--remove", f"tcp:{port}") in adb.calls
    assert len(convert_pcm(b"\x01\x00" * 320, 16000, 1)) == 640


def test_adb_socket_tx_unreachable_raises():
    from aicc_agent.adb_tx import AdbSocketTx
    tx = AdbSocketTx(FakeAdb(), "tcp:28200", local_port=1, connect_timeout_s=0.5)
    with pytest.raises(DeviceError, match="송출 모듈"):
        tx.start()
    with pytest.raises(ValueError):
        AdbSocketTx(FakeAdb(), "28200")


def test_gcar_header_format():
    from aicc_agent.gcar_tx import gcar_bytes, split_pcm
    pcm = b"\x01\x02" * 8000  # 0.5초
    g = gcar_bytes(pcm)
    assert g[:4] == b"GCAR"
    assert struct.unpack(">III", g[4:16]) == (16000, 1, 16000)
    assert g[16:] == pcm
    parts = split_pcm(b"\x00" * (16000 * 2 * 65))  # 65초 → 30+30+5
    assert [len(p) // 32000 for p in parts] == [30, 30, 5]


class FakeProc:
    def __init__(self, rc=0):
        self.rc = rc
        self.returncode = rc
        self.stderr = None

    def communicate(self, timeout=None):
        if self.rc:
            return b'{"kind":"error","message":"IllegalStateException: Connect a cellular call first"}\n', b""
        return b'{"kind":"route","message":"Telephony TX selected"}\n{"kind":"written","message":"ok"}\n', b""

    def wait(self, timeout=None):
        return self.rc

    def poll(self):
        return self.rc


class GcarAdb(FakeAdb):
    def __init__(self, rc=0):
        super().__init__()
        self.popens = []
        self.rc = rc

    def popen(self, *args):
        self.popens.append(args)
        return FakeProc(self.rc)


def test_gcar_tx_push_and_app_process_command(tmp_path):
    import re as _re

    from aicc_agent.audio import split_frames, tone
    from aicc_agent.gcar_tx import GcarFileTx
    jar = tmp_path / "goodcare-uplink.jar"
    jar.write_bytes(b"PK-fake-jar")
    adb = GcarAdb()
    tx = GcarFileTx(adb, jar, workdir=tmp_path / "w", extra_wait_s=1)
    tx.start()
    cid = tx.call_id
    assert _re.fullmatch(r"[0-9a-f]{32}", cid)
    assert ("push", str(jar), f"/data/local/tmp/goodcare-uplink-{cid}.jar") in adb.calls
    tx.write(split_frames(tone(500), 0))
    tx.flush()
    gcar = f"/data/local/tmp/goodcare-uplink-{cid}.gcar"
    jar_dev = f"/data/local/tmp/goodcare-uplink-{cid}.jar"
    assert adb.popens[-1] == ("shell", "-T", f"rm -f {gcar}.stop; chmod 600 {jar_dev} {gcar}; CLASSPATH={jar_dev} "
                              f"app_process / com.goodcare.a24.Uplink --play-file {gcar} {gcar}.stop")
    assert any(c[0] == "push" and c[2] == gcar for c in adb.calls)
    assert not list((tmp_path / "w").glob("*.gcar"))  # PC 임시 파일 삭제
    tx.stop()
    assert any(c[0] == "shell" and c[1].startswith("rm -f") and gcar in c[1] for c in adb.calls)
    assert tx.call_id is None


def test_gcar_tx_failure_and_a24_flush(tmp_path):
    from aicc_agent.audio import split_frames, tone
    from aicc_agent.gcar_tx import GcarFileTx
    jar = tmp_path / "u.jar"
    jar.write_bytes(b"x")
    adb = GcarAdb(rc=1)
    tx = GcarFileTx(adb, jar, workdir=tmp_path, extra_wait_s=1)
    d = A24Device(adb, rx=FakeRx(), tx=tx, state_file=tmp_path / "s.json")
    d.dial("01012345678")
    assert tx.call_id is not None  # 발신과 함께 JAR 준비
    d.write_tx_frames(split_frames(tone(100), 0))
    with pytest.raises(DeviceError, match="Connect a cellular call first"):
        d.tx_segment_done("opening")
    d.hangup()
    assert tx.call_id is None
    with pytest.raises(DeviceError, match="송출 모듈 파일이 없습니다"):
        GcarFileTx(adb, tmp_path / "none.jar").start()


DUAL_SIM_DIALING = """  mCallState=0
  mPreciseCallState=Ringing call state: 0, Foreground call state: 0, Background call state: 0
  mCallState=2
  mPreciseCallState=Ringing call state: 0, Foreground call state: 4, Background call state: 0
"""


def test_dual_sim_uses_active_slot_not_idle_one():
    from aicc_agent.a24 import parse_call_state
    assert parse_call_state(DUAL_SIM_DIALING).state == "ALERTING"
    assert parse_call_state(DUAL_SIM_DIALING.replace("state: 4", "state: 1")).state == "ACTIVE"
    assert parse_call_state("mCallState=0\nmCallState=0\n").state == "IDLE"
    assert parse_call_state("mCallState=0\nmCallState=2\n").state == "UNKNOWN"  # OFFHOOK 만으론 응답 판정 불가
    both_idle_precise = DUAL_SIM_DIALING.replace("state: 4", "state: 0")
    assert parse_call_state(both_idle_precise).state == "DIALING"  # 한 슬롯이 OFFHOOK


def test_gcar_preload_skips_push_at_play_time(tmp_path):
    from aicc_agent.gcar_tx import GcarFileTx
    jar = tmp_path / "u.jar"
    jar.write_bytes(b"x")
    adb = GcarAdb()
    tx = GcarFileTx(adb, jar, workdir=tmp_path, extra_wait_s=1)
    tx.start()
    pcm = b"\x01\x00" * 16000
    assert tx.preload("opening", pcm)
    pushes = sum(1 for c in adb.calls if c[0] == "push")
    tx.play_async(pcm, "opening")
    assert sum(1 for c in adb.calls if c[0] == "push") == pushes  # 재생 때는 실행 명령만
    import re as _re
    for cmd in adb.popens:  # 모듈의 경로 검사 규칙(Uplink.java)과 같아야 실행된다
        g = _re.search(r"--play-file (\S+) (\S+)$", cmd[2])
        assert _re.fullmatch(r"/data/local/tmp/goodcare-uplink-[a-f0-9]{32}[.]gcar", g.group(1))
        assert g.group(2) == g.group(1) + ".stop"
    while not tx.poll_playback(0.01):
        pass
    tx.play_async(pcm + b"\x00\x00", "opening")  # 내용이 다르면 새로 올림
    assert sum(1 for c in adb.calls if c[0] == "push") == pushes + 1
    tx.stop()


def test_real_call_evidence_upgrades_capabilities(tmp_path):
    """2026-10-29: 시험 통화를 여러 번 했는데 기능이 'unknown' → 실통화 결과로 기능 확인 표시를 올린다."""
    from aicc_agent.a24 import A24Device

    class FakeAdb:
        def devices(self):
            return [{"serial": "x", "state": "device"}]

    st = tmp_path / "probe_state.json"
    dev = A24Device(FakeAdb(), state_file=st, rx=object(), tx=object())
    assert dev.probe_capabilities()["rx_audio"] == "unknown" and dev.evidence_report() is None
    # 받지 않은 통화는 근거가 아님
    dev.record_call_evidence({"call_id": "a", "answered": False, "utterances": 0, "plays": 1, "outcome": "NO_ANSWER",
                              "phone_idle": True, "recording_both": False, "remote_answered_prompt": False})
    assert dev.probe_capabilities()["rx_audio"] == "unknown"
    # 받고 말했지만 확정 전 → RX·응답감지만
    dev.record_call_evidence({"call_id": "b", "answered": True, "utterances": 2, "plays": 3, "outcome": "REMOTE_HANGUP",
                              "phone_idle": True, "recording_both": True, "remote_answered_prompt": False})
    caps = dev.probe_capabilities()
    assert caps["answer_detection"] == caps["rx_audio"] == "supported" and caps["tx_audio"] == "unknown"
    # 확인 질문에 '네' → 송출도 확인, 양쪽 녹음
    dev.record_call_evidence({"call_id": "c", "answered": True, "utterances": 3, "plays": 4, "outcome": "CONFIRMED",
                              "phone_idle": True, "recording_both": True, "remote_answered_prompt": True})
    caps = dev.probe_capabilities()
    assert all(caps[k] == "supported" for k in ("dial", "hangup", "answer_detection", "rx_audio", "tx_audio", "recording"))
    rep = dev.evidence_report()
    assert rep["source"] == "auto_from_real_calls" and rep["probe_state"]["auto_evidence"]["calls"] == 2


def test_a24_restarts_rx_each_call_and_releases():
    """2026-10-29: 상대가 먼저 끊으면 수신 캡처가 계속 돌아 다음 통화가 앞 통화 소리를 이어받음."""
    from aicc_agent.a24 import A24Device

    class FakeAdb:
        def __init__(self):
            self.cmds = []

        def run(self, *a, **k):
            self.cmds.append(a)
            return ""

        def devices(self):
            return [{"serial": "x", "state": "device"}]

    class Rx:
        def __init__(self):
            self.starts = self.stops = 0

        def start(self):
            self.starts += 1

        def stop(self):
            self.stops += 1

    rx = Rx()
    dev = A24Device(FakeAdb(), rx=rx, tx=rx)
    dev.dial("01012345678")
    assert rx.starts == 1
    dev.dial("01012345679")  # 앞 통화가 정리되지 않았어도 새로 시작
    assert rx.starts == 2 and rx.stops >= 1
    dev.release()
    assert dev._rx_running is False
