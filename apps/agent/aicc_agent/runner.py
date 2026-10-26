"""한 통화 실행기: 장치 ↔ VAD ↔ STT ↔ 상태기계 ↔ ARS 캐시 ↔ TX, 그리고 서버 보고.

빠른 응답 경로(설계 08): RX → VAD(발화 종료) → STT 최종 결과 → 현재 상태 한정 의도·날짜 해석 → 캐시된 ARS → TX.
생성형 AI 는 상태기계가 StartAI 를 낸 예외 턴에만 호출한다.

측정 경계: 이 모듈이 기록하는 지연은 '로컬 파이프라인'(VAD 발화 종료 → 첫 TX 프레임 기록)이다.
설계 목표의 경계(상대 발화 종료 → 상대 전화기에서 응답 시작)는 망 지연을 포함하므로 원격 계측이 필요하다.
"""

from __future__ import annotations

import concurrent.futures as cf
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from aicc_core.flow import (
    BlockContact,
    CallFlow,
    ConfirmSchedule,
    End,
    HangUp,
    MarkReview,
    Play,
    StartAI,
    StopTx,
)
from aicc_core.voicepack import confirm_date_sequence

from .ai import ExceptionAI
from .audio import EnergyVAD, read_wav_mono16, split_frames, tone
from .client import AgentClient, ApiError, new_event_id
from .device import DeviceAdapter, DeviceError, Frame
from .recorder import DualRecorder
from .stt import STTAdapter

log = logging.getLogger("aicc.agent")
NO_BARGE_IN = {"refusal_ack", "closing", "wrong_person", "callback", "ai_failed"}


@dataclass
class RunnerConfig:
    silence_ms: int = 6000
    ring_timeout_ms: int = 40000
    poll_ms: int = 200
    tx_chunk_ms: int = 200
    vad_end_ms: int = 350
    vad_threshold: float = 300.0
    ai_wait_after_ms: int = 1200
    ai_timeout_s: float = 10.0
    barge_in: bool = True
    state_check_ms: int = 1000


@dataclass
class CallSummary:
    call_id: str
    outcome: str | None = None
    review_reasons: list[str] = field(default_factory=list)
    latencies_ms: list[int] = field(default_factory=list)
    confirmed_date: str | None = None
    errors: list[str] = field(default_factory=list)


class VoiceBank:
    """음원팩 캐시. 실통화에서는 승인된 녹음만 사용하고, MOCK 에서만 자리표시 톤을 허용한다."""

    def __init__(self, manifest: dict, fetch, is_mock: bool):  # noqa: ANN001
        self.manifest = manifest
        self.fetch = fetch
        self.is_mock = is_mock
        self.cache: dict[str, bytes] = {}

    def pcm(self, key: str) -> bytes:
        if key in self.cache:
            return self.cache[key]
        clip = (self.manifest.get("clips") or {}).get(key) or {}
        data = self.fetch(key) if clip.get("file") else None
        if data:
            pcm = read_wav_mono16(data)
        elif self.is_mock:
            pcm = tone(max(300, 60 * len(clip.get("text") or key)), 523.0, 1500)
        else:
            raise DeviceError(f"승인된 음원이 없습니다: {key}")
        self.cache[key] = pcm
        return pcm

    def render(self, play: Play) -> list[str]:
        if play.key == "confirm_date":
            return confirm_date_sequence(int(play.params["month"]), int(play.params["day"]))
        return [play.key]


class CallRunner:
    def __init__(self, client: AgentClient, device: DeviceAdapter, stt: STTAdapter, ai: ExceptionAI,
                 config: RunnerConfig | None = None):
        self.client = client
        self.device = device
        self.stt = stt
        self.ai = ai
        self.cfg = config or RunnerConfig()
        self.stop_requested = False

    # ---------------- 이벤트 ----------------
    def _emit(self, type_: str, turn: int, payload: dict | None = None, event_id: str | None = None) -> str:
        eid = event_id or new_event_id()
        self._seq += 1
        self._buf.append({"event_id": eid, "seq": self._seq, "turn_id": turn, "type": type_,
                          "occurred_at": datetime.now(timezone.utc).isoformat(), "payload": payload or {}})
        return eid

    def _flush(self) -> None:
        if not self._buf:
            return
        evs, self._buf = self._buf, []
        self.client.events(self.call_id, evs)

    # ---------------- 실행 ----------------
    def run(self, claim: dict) -> CallSummary:
        self.call_id = claim["call_id"]
        self.is_mock = bool(claim.get("is_mock"))
        self.pilot = bool(claim.get("pilot"))
        if hasattr(self.device, "allow_silent_tx"):
            self.device.allow_silent_tx = self.pilot
            self.device.silent_tx_used = False
        self._seq = 0
        self._buf: list[dict] = []
        self.summary = CallSummary(self.call_id)
        self.recorder = DualRecorder()
        self.vad = EnergyVAD(self.cfg.vad_threshold, self.cfg.vad_end_ms)
        pack = claim["voice_pack"]
        self.voice = VoiceBank(pack["manifest"], lambda k: self._fetch_clip(pack["id"], k), self.is_mock)
        self.flow = CallFlow(date.fromisoformat(claim["reference_date"]))
        self.max_call_ms = int(claim.get("max_call_seconds") or 300) * 1000
        self.candidate_event: dict[int, str] = {}
        self.pending_vad_end: int | None = None
        self.prompt_end_ms = 0
        self.confirm_failed = False
        self.rx_speech_frames = 0
        self.ai_wait_played = False
        try:
            self._call(claim["phone"])
        except DeviceError as e:
            self.summary.errors.append(str(e)[:200])
            self._apply(self.flow.device_error(type(e).__name__))
        except ApiError as e:
            # 서버 확인 실패 시 날짜를 추정하지 않고 종료
            self.summary.errors.append(f"API {e.code}")
            try:
                self.device.hangup()
            except DeviceError:
                self.summary.review_reasons.append("HANGUP_FAILED_MANUAL")
            if not self.flow.finished:
                self.flow.review_reasons.append("SERVER_ERROR")
                self.flow._end("UNCONFIRMED")  # noqa: SLF001 - 상태기계 종료 표시
        self._finalize()
        return self.summary

    def _fetch_clip(self, pack_id: str, key: str) -> bytes | None:
        try:
            r = self.client.http.get(f"{self.client.base}/api/v1/agent/voice/{pack_id}/clips/{key}",
                                     headers=self.client.h)
        except Exception:  # noqa: BLE001
            return None
        return r.content if r.status_code == 200 else None

    def _call(self, phone: str) -> None:
        self.flow.dialing()
        self._emit("call.dialing", 0, {"mock": self.is_mock})
        self.device.dial(phone)
        t0 = self.device.now_ms()
        while True:
            st = self.device.get_call_state()
            elapsed = self.device.now_ms() - t0
            if st.state == "ACTIVE":
                self.answered_ms = self.device.now_ms()
                self._emit("call.answered", 0, {"detail": st.detail, "source": "device_state"})
                self._flush()
                self._apply(self.flow.answered())
                break
            if st.state in ("IDLE", "ENDED") and elapsed > 1000:
                reason = "BUSY" if elapsed < 6000 else "NO_ANSWER"
                self._emit("call.not_answered", 0, {"reason": reason, "elapsed_ms": elapsed})
                self._apply(self.flow.not_answered(reason))
                return
            if st.state == "UNKNOWN" and not self.is_mock and elapsed > 3000:
                # 응답 여부를 판정할 수 없으면 대화를 시작하지 않는다(가짜 응답 금지)
                self._emit("call.answer_unknown", 0, st.detail)
                self.device.hangup()
                self.flow.review_reasons.append("ANSWER_DETECTION_UNKNOWN")
                self._apply(self.flow.device_error("answer_detection_unknown"))
                return
            if elapsed > self.cfg.ring_timeout_ms:
                self.device.hangup()
                self._apply(self.flow.not_answered("NO_ANSWER"))
                return
            self._tick(self.cfg.poll_ms, record=False)
        self._converse()

    def _tick(self, ms: int, record: bool = True) -> list[Frame]:
        frames = self.device.read_rx_frames(ms)
        if record:
            self.recorder.add_rx(frames)
        if not self.is_mock and not frames:
            time.sleep(ms / 1000)
        return frames

    def _converse(self) -> None:
        last_check = self.device.now_ms()
        while not self.flow.finished:
            if self.stop_requested:
                self._apply(self.flow.stop())
                break
            now = self.device.now_ms()
            if now - self.answered_ms > self.max_call_ms:
                self._apply(self.flow.timeout())
                break
            frames = self._tick(self.cfg.poll_ms)
            self._process_rx(frames)
            if self.flow.finished:
                break
            now = self.device.now_ms()
            if not self.vad.in_speech and self.prompt_end_ms and now - self.prompt_end_ms > self.cfg.silence_ms:
                self.prompt_end_ms = now
                self._emit("silence.timeout", self.flow.turn, {"ms": self.cfg.silence_ms})
                self._apply(self.flow.silence(turn=self.flow.turn))
            if now - last_check >= self.cfg.state_check_ms:
                last_check = now
                st = self.device.get_call_state()
                if st.state in ("IDLE", "ENDED"):
                    self._emit("call.remote_hangup", self.flow.turn, st.detail)
                    self._apply(self.flow.remote_hangup())

    def _process_rx(self, frames: list[Frame]) -> bool:
        """반환: 발화 시작이 감지됐는지(끼어들기 판정용)."""
        started = False
        for f in frames:
            if f.pcm and f.speech:
                self.rx_speech_frames += 1
            for ev in self.vad.feed(f):
                if ev.kind == "SPEECH_START":
                    started = True
                elif ev.kind == "SPEECH_END" and not self.flow.finished:
                    self._on_utterance(ev.frames, ev.t_ms)
        return started

    def _on_utterance(self, frames: list[Frame], vad_end_ms: int) -> None:
        res = self.stt.transcribe(frames)
        if not res.text.strip():
            return
        turn = self.flow.turn
        payload = {"text": res.text, "stt_engine": res.engine, "stt_latency_ms": res.latency_ms,
                   "confidence": res.confidence, "state": self.flow.state}
        if self.flow.state == "CONFIRM_DATE" and turn in self.candidate_event:
            payload["candidate_event_id"] = self.candidate_event[turn]
        eid = self._emit("utterance.final", turn, payload)
        self.pending_vad_end = vad_end_ms
        self.prompt_end_ms = 0
        self._apply(self.flow.utterance(res.text, turn=turn, event_id=eid))

    # ---------------- 액션 실행 ----------------
    def _apply(self, actions: list) -> None:  # noqa: C901
        for a in actions:
            if isinstance(a, Play):
                self._play(a)
            elif isinstance(a, StopTx):
                self.device.stop_tx()
                self._emit("tx.stop", self.flow.turn, {"reason": a.reason})
            elif isinstance(a, StartAI):
                self._ai(a)
            elif isinstance(a, ConfirmSchedule):
                self._flush()
                try:
                    r = self.client.confirm(self.call_id, a.evidence_event_id)
                    self.summary.confirmed_date = r.get("new_date")
                    self._emit("schedule.confirmed", a.turn, {"date": r.get("new_date"), "status": r.get("status")})
                except ApiError as e:
                    self.confirm_failed = True
                    self.flow.review_reasons.append(f"CONFIRM_REJECTED:{e.code}")
                    self._emit("schedule.confirm_rejected", a.turn, {"code": e.code})
            elif isinstance(a, BlockContact):
                self._flush()
                try:
                    self.client.block(self.call_id, a.evidence_event_id)
                    self._emit("contact.blocked", self.flow.turn, {"evidence_event_id": a.evidence_event_id})
                except ApiError as e:
                    # 서버 종료 보고(REFUSED)에서 다시 차단을 보장한다
                    self.flow.review_reasons.append(f"BLOCK_DEFERRED:{e.code}")
            elif isinstance(a, MarkReview):
                if a.reason not in self.flow.review_reasons:
                    self.flow.review_reasons.append(a.reason)
            elif isinstance(a, HangUp):
                try:
                    self.device.hangup()
                    self._emit("call.hangup", self.flow.turn, {"reason": a.reason})
                except DeviceError:
                    self.flow.review_reasons.append("HANGUP_FAILED_MANUAL")
            elif isinstance(a, End):
                self.summary.outcome = a.outcome

    def _play(self, a: Play) -> None:
        if a.key == "confirm_date":
            cid = self._emit("schedule.candidate", a.turn, {"date": a.params["date"],
                                                           "reference_date": self.flow.reference.isoformat()})
            self.candidate_event[a.turn] = cid
        keys = self.voice.render(a)
        pcm = b"".join(self.voice.pcm(k) for k in keys)
        self._emit("play", a.turn, {"key": a.key, "clips": keys, "params": a.params})
        start = self.device.now_ms()
        if self.pending_vad_end is not None:
            lat = max(0, start - self.pending_vad_end)
            self.summary.latencies_ms.append(lat)
            self._emit("metric.local_latency", a.turn, {"vad_end_to_tx_start_ms": lat,
                                                         "boundary": "local_pipeline_not_remote"})
            self.pending_vad_end = None
        frames = split_frames(pcm, start)
        step = max(1, self.cfg.tx_chunk_ms // 20)
        written = 0
        for i in range(0, len(frames), step):
            chunk = frames[i:i + step]
            t = self.device.now_ms()
            for j, f in enumerate(chunk):
                f.t_ms = t + j * 20
            written += self.device.write_tx_frames(chunk)
            self.recorder.add_tx(chunk)
            if self.cfg.barge_in and a.key not in NO_BARGE_IN and not self.is_mock:
                rx = self._tick(1)
                if self._process_rx(rx):
                    self.device.stop_tx()
                    self._emit("tx.barge_in", a.turn, {"at_frame": i})
                    break
        self._emit("tx.written", a.turn, {"frames": written, "remote_heard": "UNVERIFIED"})
        self.device.tx_segment_done(a.key)
        self.prompt_end_ms = self.device.now_ms()

    def _ai(self, a: StartAI) -> None:
        self._emit("ai.start", a.turn, {"return_state": a.return_state})
        self._flush()
        with cf.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(self.ai.respond, a.question, timeout_s=self.cfg.ai_timeout_s)
            try:
                reply = fut.result(timeout=self.cfg.ai_wait_after_ms / 1000)
            except cf.TimeoutError:
                if not self.ai_wait_played:
                    self.ai_wait_played = True
                    self._play(Play("ai_wait", a.turn, {}))
                try:
                    reply = fut.result(timeout=self.cfg.ai_timeout_s)
                except cf.TimeoutError:
                    reply = None
        if self.flow.turn != a.turn or self.flow.finished:
            self._emit("ai.discarded", a.turn, {"reason": "stale_turn"})  # 지난 턴 AI 오디오 폐기
            return
        if reply is None or not reply.ok or not (reply.pcm or (reply.text and self.is_mock)):
            self._emit("ai.failed", a.turn, {"error": getattr(reply, "error", "timeout") if reply else "timeout"})
            self._apply(self.flow.ai_failed(turn=a.turn))
            return
        pcm = reply.pcm or tone(max(400, 60 * len(reply.text or "")), 660.0, 1500)
        frames = split_frames(pcm, self.device.now_ms())
        for f in frames:
            f.t_ms = self.device.now_ms()
            self.device.write_tx_frames([f])
            self.recorder.add_tx([f])
        self._emit("ai.done", a.turn, {"text": reply.text, "latency_ms": reply.latency_ms, "engine": self.ai.name})
        self._apply(self.flow.ai_done(turn=a.turn))

    # ---------------- 마무리 ----------------
    def _finalize(self) -> None:
        outcome = self.flow.outcome or "DEVICE_ERROR"
        if outcome == "CONFIRMED" and self.confirm_failed:
            outcome = "UNCONFIRMED"
        silent = bool(getattr(self.device, "silent_tx_used", False))
        if silent:
            self.flow.review_reasons.append("PILOT_NO_TX_DEVICE")
        self._emit("flags", self.flow.turn, {"rx_speech_frames": self.rx_speech_frames,
                                             "tx_remote_heard": "NOT_SENT" if silent else "UNVERIFIED",
                                             "mock": self.is_mock, "pilot": self.pilot})
        self._emit("call.ended", self.flow.turn, {"outcome": outcome})
        self.summary.outcome = outcome
        self.summary.review_reasons = list(self.flow.review_reasons)
        try:
            self._flush()
        except Exception as e:  # noqa: BLE001
            self.summary.errors.append(f"event flush failed: {type(e).__name__}")
        if self.recorder.rx or self.recorder.tx:
            try:
                rec = self.recorder.finish()
                self.client.upload_recording(self.call_id, rec.wav, rec.sha256, rec.duration_ms)
            except Exception as e:  # noqa: BLE001
                self.summary.errors.append(f"recording: {type(e).__name__}")
                try:
                    self.client.recording_failed(self.call_id, f"업로드 실패: {type(e).__name__}")
                except Exception:  # noqa: BLE001
                    pass
        idle = False
        for _ in range(5):
            try:
                idle = self.device.get_call_state().state == "IDLE"
            except DeviceError:
                idle = False
                break
            if idle:
                break
            self._safe_wait(300)
        self.client.finish(self.call_id, outcome=outcome, observation=self.flow.obs.as_dict(),
                           review_reasons=self.summary.review_reasons, phone_idle=idle)

    def _safe_wait(self, ms: int) -> None:
        try:
            self.device.read_rx_frames(ms)
        except DeviceError:
            pass
