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
        key = " ".join(a.split()[0] for a in args[:2])
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


class _CP:
    def __init__(self, out):
        self.stdout, self.stderr, self.returncode = out, "", 0


@pytest.mark.parametrize("out,msg", [
    ("List of devices attached\n", "연결된 휴대폰이 없습니다"),
    ("List of devices attached\nR58T\tunauthorized\n", "허용"),
    ("List of devices attached\nA\tdevice\nB\tdevice\n", "여러 대"),
])
def test_adb_ensure_ready_messages(monkeypatch, out, msg):
    from aicc_agent.device import DeviceError
    a = Adb(adb="adb")
    monkeypatch.setattr(a, "_exec", lambda args, timeout: _CP(out if args[0] == "devices" else ""))
    with pytest.raises(DeviceError, match=msg):
        a.run("shell", "getprop", "x")


def test_adb_ready_then_runs(monkeypatch):
    a = Adb(adb="adb")
    calls = []

    def ex(args, timeout):
        calls.append(args)
        return _CP("List of devices attached\nR58T\tdevice\n" if args[0] == "devices" else "SM-A245N")
    monkeypatch.setattr(a, "_exec", ex)
    assert a.getprop("ro.product.model") == "SM-A245N"
    assert calls[0] == ["start-server"] and calls[-1] == ["shell", "getprop", "ro.product.model"]


class AsyncTxDevice:
    """파일 송출(비동기) 단말 흉내: 재생은 play_ms 동안 이어지고, 정해진 시각에 상대가 말한다."""

    def __init__(self, play_ms=3000, speech=(500, 1100), amp=4000):
        self.t = 0
        self.amp = amp
        self.play_ms = play_ms
        self.speech = speech
        self.play_end = 0
        self.stopped_at = None

    def now_ms(self):
        return self.t

    def tx_async(self):
        return True

    def tx_play_async(self, pcm, key=None):
        self.play_end = self.t + self.play_ms

    def tx_poll(self):
        return self.stopped_at is not None or self.t >= self.play_end

    def stop_tx(self):
        self.stopped_at = self.t

    def read_rx_frames(self, ms):
        from aicc_agent.audio import tone
        from aicc_agent.device import Frame
        out = []
        for _ in range(ms // 20):
            loud = self.speech[0] <= self.t < self.speech[1]
            out.append(Frame(tone(20, 300.0, self.amp)[:640] if loud else b"\x00" * 640, self.t))
            self.t += 20
        return out


class VB:
    def render(self, a):
        return [a.key]


def _runner(dev):
    from aicc_agent.audio import EnergyVAD
    from aicc_agent.recorder import DualRecorder
    from aicc_agent.runner import CallRunner, RunnerConfig
    r = CallRunner(None, dev, None, None, RunnerConfig())
    r.is_mock = False
    r.vad = EnergyVAD(300, 600)
    r._pending_utts, r.recorder, r.rx_speech_frames = [], DualRecorder(), 0
    r.voice = VB()
    return r


def test_soft_or_short_sound_during_playback_is_not_barge_in():
    from aicc_core.flow import Play
    seen = []
    for dev in (AsyncTxDevice(speech=(500, 1100), amp=500),   # 작은 소리(메아리 수준)
                AsyncTxDevice(speech=(500, 760))):             # 큰 소리지만 0.26초
        r = _runner(dev)
        r._emit = lambda t, turn, p=None, event_id=None: seen.append(t) or "e"
        r._on_utterance = lambda frames, t: seen.append("utt")

        class F:
            finished = False
        r.flow = F()
        r._play_async(Play("opening", 0), b"\x00" * 64000, [], 0)
        assert dev.stopped_at is None and dev.t >= 3000  # 오프닝 끝까지 재생
    assert "tx.barge_in" not in seen and "utt" not in seen  # 메아리를 답으로 처리하지 않음


class _Flow:
    finished = False


def test_barge_in_during_file_playback_answers_current_question():
    from aicc_core.flow import Play
    dev = AsyncTxDevice()
    r = _runner(dev)
    r.flow = _Flow()
    seen = []
    r._emit = lambda t, turn, p=None, event_id=None: seen.append(t) or "e"
    r._play_async(Play("ask_working", 1), b"\x00" * 64000, [], 0)
    assert dev.stopped_at is not None and dev.stopped_at < 1000  # 0.4초 이상 이어지자 곧바로 재생 중단
    assert "tx.barge_in" in seen
    assert r.vad.in_speech  # 끝나지 않은 발화는 대화 루프가 이어서 처리(다음 질문에 붙지 않음)


def test_no_barge_in_for_closing_plays_to_end():
    from aicc_core.flow import Play
    dev = AsyncTxDevice(play_ms=2000)
    r = _runner(dev)
    r.flow = _Flow()
    r._emit = lambda *a, **k: "e"
    r._play_async(Play("closing", 1), b"\x00" * 64000, [], 0)
    assert dev.stopped_at is None and dev.t >= 2000
