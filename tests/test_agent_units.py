"""에이전트 단위 시험: A24 상태 파싱(가짜 adb 출력), VAD, 녹음 동기화, 음원팩 검사."""

from __future__ import annotations

import io
import wave

import pytest
from aicc_agent.a24 import A24Device, Adb
from aicc_agent.audio import EnergyVAD, silence, split_frames, tone
from aicc_agent.device import DeviceUnverified
from aicc_agent.probe import collect_env
from aicc_agent.recorder import DualRecorder
from aicc_core.voicepack import check_pack, default_manifest


class FakeAdb(Adb):
    def __init__(self, outputs):
        super().__init__(adb="adb")
        self.outputs = outputs
        self.calls = []

    def run(self, *args, timeout=8.0):
        self.calls.append(args)
        key = " ".join(args[:2])
        if args[:1] == ("devices",):
            return "List of devices attached\nR58T1 device product:a24 model:SM_A245N\n"
        return self.outputs.get(key, "")


def test_a24_precise_state_mapping_and_offhook_not_answer():
    adb = FakeAdb({"shell dumpsys": "mCallState=2\nmPreciseCallState=Ringing call state: 0, Foreground call state: 4"})
    d = A24Device(adb)
    assert d.get_call_state().state == "ALERTING"
    adb.outputs["shell dumpsys"] = "mCallState=2\n Foreground call state: 1"
    assert d.get_call_state().state == "ACTIVE"
    adb.outputs["shell dumpsys"] = "mCallState=2\n"  # OFFHOOK 만으로 응답 판정 금지
    assert d.get_call_state().state == "UNKNOWN"


def test_a24_capabilities_without_proof_are_not_supported(tmp_path):
    d = A24Device(FakeAdb({}), route=None, state_file=tmp_path / "none.json")
    caps = d.probe_capabilities()
    assert caps["dial"] == "unknown" and caps["rx_audio"] == "unsupported" and caps["tx_audio"] == "unsupported"
    with pytest.raises(DeviceUnverified):
        d.read_rx_frames(200)
    with pytest.raises(DeviceUnverified):
        d.write_tx_frames([])


def test_a24_dial_command_and_number_validation():
    adb = FakeAdb({})
    d = A24Device(adb)
    d.dial("01012345678")
    assert adb.calls[-1] == ("shell", "am", "start", "-a", "android.intent.action.CALL", "-d", "tel:01012345678")
    with pytest.raises(Exception):
        d.dial("01012345678; rm -rf /")


def test_probe_env_without_adb():
    rep = collect_env(type("NoAdb", (Adb,), {"available": lambda self: False})())
    assert rep["adb"] is None and rep["unknowns"]


@pytest.mark.parametrize("end_ms", [250, 350, 500])
def test_vad_end_of_speech_timing(end_ms):
    vad = EnergyVAD(end_silence_ms=end_ms)
    frames = split_frames(tone(800, 200, 3000) + silence(1000), 0)
    events = [e for f in frames for e in vad.feed(f)]
    kinds = [e.kind for e in events]
    assert kinds == ["SPEECH_START", "SPEECH_END"]
    end = events[1]
    assert end.t_ms == 800  # 발화 끝 시각(무음 대기 시간을 더하지 않음)


def test_vad_rejects_out_of_range_end():
    with pytest.raises(ValueError):
        EnergyVAD(end_silence_ms=50)


def test_recorder_two_channel_alignment():
    r = DualRecorder()
    r.add_rx(split_frames(tone(200, 300, 2000), 0))
    r.add_tx(split_frames(tone(200, 500, 2000), 400))
    res = r.finish()
    with wave.open(io.BytesIO(res.wav)) as w:
        assert w.getnchannels() == 2 and w.getframerate() == 16000
        assert abs(w.getnframes() / 16000 * 1000 - 600) < 25
    assert len(res.sha256) == 64 and res.rx_frames == 10 and res.tx_frames == 10


def test_voicepack_placeholders_block_dialing():
    m = default_manifest("female", "F", "김태희 실장")
    chk = check_pack(m, None)
    assert not chk.dialable
    assert any("자리표시자" in p for p in chk.problems)
    assert any("회사명" in p for p in chk.problems)
