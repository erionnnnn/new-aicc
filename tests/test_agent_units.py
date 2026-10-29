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


def test_google_stt_falls_back_to_pc_on_failure():
    from aicc_agent.device import Frame
    from aicc_agent.stt import GeminiLiveSTT, MockSTT
    stt = GeminiLiveSTT("invalid-key", fallback=MockSTT(), timeout_s=0.5)
    stt.begin_call()
    r = stt.transcribe([Frame(b"\x00" * 640, 0, mock_text="네 맞아요")])
    assert r.text == "네 맞아요" and "대신" in r.engine and stt.last_error
    stt.end_call()


def test_voicebank_prefers_full_date_sentence_and_variants():
    from aicc_agent.runner import VoiceBank
    from aicc_core.flow import Play
    clips = {"confirm_date": {"file": "x"}, "month_10": {"file": "x"}, "day_28": {"file": "x"},
             "confirm_10_28": {"file": "x"}, "sc_work_end_date": {"file": "x"}, "endack_11_08": {"file": "x"},
             "ask_available_date": {"file": "x"}, "ask_available_date__v2": {"file": "x"}}
    vb = VoiceBank({"clips": clips}, lambda k: None, False)
    assert vb.render(Play("confirm_date", 1, {"month": 10, "day": 28})) == ["confirm_10_28"]
    assert vb.render(Play("confirm_date", 1, {"month": 10, "day": 29})) == ["month_10", "day_29", "confirm_date"]
    assert vb.render(Play("sc_work_end_date", 1, {"month": 11, "day": 8})) == ["endack_11_08"]
    seen = {vb.render(Play("ask_available_date", 1))[0] for _ in range(60)}
    assert seen == {"ask_available_date", "ask_available_date__v2"}  # 녹음된 다른 표현만 섞어 씀


def test_script_counts():
    from aicc_core.voicepack import END_ACK_KEYS, FULL_CONFIRM_KEYS
    assert len(FULL_CONFIRM_KEYS) == 366 and len(END_ACK_KEYS) == 366 and "confirm_02_29" in FULL_CONFIRM_KEYS


def test_voice_script_in_repo_matches_code():
    import json
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    before = (root / "docs/voice/aicc_voice_script_female.json").read_text(encoding="utf-8")
    subprocess.run([sys.executable, str(root / "scripts/make_voice_script.py")], check=True, capture_output=True)
    after = (root / "docs/voice/aicc_voice_script_female.json").read_text(encoding="utf-8")
    assert before == after, "문구가 바뀌었으면 scripts/make_voice_script.py 를 실행해 대본을 갱신하세요"
    assert len(json.loads(after)) == 855 + len(__import__("aicc_core.faq").faq.FAQS)  # 기존 855 + 예상 질문


class _FakeLive:
    """Live API 가짜 세션: 소리를 받다가 '끝' 신호가 오면 받아쓴 글을 돌려준다(respond=False 면 침묵)."""

    def __init__(self, text="네 맞아요", respond=True, chunks=None):
        import asyncio
        from types import SimpleNamespace as NS
        self.NS = NS
        self.text, self.respond, self.chunks = text, respond, chunks
        self.audio = bytearray()
        self.ends = 0
        self.q = asyncio.Queue()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send_realtime_input(self, audio=None, audio_stream_end=None):
        if audio is not None:
            self.audio.extend(audio.data)
        if audio_stream_end:
            self.ends += 1
            if self.respond:
                for i, c in enumerate(self.chunks or [self.text]):
                    last = i == len(self.chunks or [self.text]) - 1
                    await self.q.put(self.NS(server_content=self.NS(
                        input_transcription=self.NS(text=c, finished=last))))

    async def receive(self):
        while True:
            yield await self.q.get()


def _live_stt(fake, fallback=None, end_wait=0.5):
    import time

    from aicc_agent.stt import GeminiLiveSTT

    class T(GeminiLiveSTT):
        def _connect(self):
            return fake

    s = T("k", fallback=fallback, timeout_s=0.8, end_wait_s=end_wait)
    s.begin_call()
    for _ in range(50):
        if s._session is not None:
            break
        time.sleep(0.01)
    return s


def test_google_stt_streams_during_speech_and_finishes_fast():
    import time

    from aicc_agent.device import Frame
    fake = _FakeLive(chunks=["네, ", "맞습니다."])
    stt = _live_stt(fake)
    frames = [Frame(b"\x01\x00" * 320, i * 20) for i in range(30)]
    stt.stream_start()
    for f in frames:
        stt.stream_audio(f.pcm)
    t0 = time.monotonic()
    r = stt.transcribe(frames)
    assert r.text == "네, 맞습니다." and r.engine == "gemini-live"
    assert time.monotonic() - t0 < 0.3
    assert len(fake.audio) == sum(len(f.pcm) for f in frames) and fake.ends == 1
    stt.end_call()


def test_google_stt_burst_when_not_streamed():
    from aicc_agent.device import Frame
    fake = _FakeLive(text="아니요")
    stt = _live_stt(fake)
    r = stt.transcribe([Frame(b"\x01\x00" * 320, 0)])
    assert r.text == "아니요" and "한꺼번에" in r.engine
    stt.end_call()


def test_google_stt_slow_uses_parallel_fallback_without_extra_wait():
    import time

    from aicc_agent.device import Frame
    from aicc_agent.stt import STTAdapter, SttResult

    class SlowPC(STTAdapter):
        name = "pc"

        def transcribe(self, frames):
            time.sleep(0.3)
            return SttResult("네", True, None, 300, self.name)

    fake = _FakeLive(respond=False)
    stt = _live_stt(fake, fallback=SlowPC(), end_wait=0.5)
    frames = [Frame(b"\x01\x00" * 320, 0)]
    stt.stream_start()
    stt.stream_audio(frames[0].pcm)
    t0 = time.monotonic()
    r = stt.transcribe(frames)
    took = time.monotonic() - t0
    assert r.text == "네" and "대신" in r.engine
    assert took < 0.7, took  # 기다림(0.5) + PC 인식(0.3)이 아니라 동시에 돌아 0.5초 남짓
    assert stt._session is not None  # 시간 초과로 연결을 끊지 않음
    stt.end_call()


def test_runner_streams_rx_audio_only_while_speaking():
    from aicc_agent.audio import EnergyVAD
    from aicc_agent.device import Frame
    from aicc_agent.runner import CallRunner, RunnerConfig

    class Rec:
        def __init__(self):
            self.starts, self.sent = 0, 0

        def stream_start(self):
            self.starts += 1

        def stream_audio(self, pcm):
            self.sent += 1

    rec = Rec()
    r = CallRunner(None, None, rec, None, RunnerConfig())
    r.vad = EnergyVAD(threshold=300, end_silence_ms=200)
    loud, quiet = b"\xe8\x03" * 320, b"\x00\x00" * 320
    seq = [quiet] * 5 + [loud] * 10 + [quiet] * 15
    for i, pcm in enumerate(seq):
        r._feed_vad(Frame(pcm, i * 20))
    assert rec.starts == 1
    assert rec.sent == 10 + 10  # 말소리 10 + 끝 판정까지의 조용한 10(200ms), 앞뒤 침묵은 보내지 않음
