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
    assert "--no-playback" in cmd and "--no-window" in cmd
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
