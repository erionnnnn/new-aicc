"""한 통화 실행기: 장치 ↔ VAD ↔ STT ↔ 상태기계 ↔ ARS 캐시 ↔ TX, 그리고 서버 보고.

빠른 응답 경로(설계 08): RX → VAD(발화 종료) → STT 최종 결과 → 현재 상태 한정 의도·날짜 해석 → 캐시된 ARS → TX.
생성형 AI 는 상태기계가 StartAI 를 낸 예외 턴에만 호출한다.

측정 경계: 이 모듈이 기록하는 지연은 '로컬 파이프라인'(VAD 발화 종료 → 첫 TX 프레임 기록)이다.
설계 목표의 경계(상대 발화 종료 → 상대 전화기에서 응답 시작)는 망 지연을 포함하므로 원격 계측이 필요하다.
"""

from __future__ import annotations

import concurrent.futures as cf
import logging
import os
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
from aicc_core.scenarios import SCENARIO_KEYS
from aicc_core.voicepack import confirm_date_sequence

from .ai import ExceptionAI, build_prompt
from .audio import EnergyVAD, read_wav_mono16, rms, split_frames, tone
from .client import AgentClient, ApiError, new_event_id
from .device import FRAME_MS, DeviceAdapter, DeviceError, Frame
from .recorder import DualRecorder
from .stt import STTAdapter

log = logging.getLogger("aicc.agent")
PRELOAD_KEYS = ("opening", "ack", "ask_working", "ask_end_date", "ask_available_date", "closing", "ask_repeat",
                "callback", "refusal_ack", "ai_wait", "wrong_person", "explain_role", *SCENARIO_KEYS)
NO_BARGE_IN = {"refusal_ack", "closing", "wrong_person", "callback", "ai_failed"}


@dataclass
class RunnerConfig:
    silence_ms: int = 6000
    ring_timeout_ms: int = 40000
    poll_ms: int = 200
    listen_ms: int = 40           # 재생 중 상대 음성 확인 간격
    greeting_wait_ms: int = 1500  # 받자마자 '여보세요' 하는 동안 기다림
    barge_threshold: float = 600.0  # 안내 재생 중에는 평소(300)보다 큰 소리만 상대 말로 본다(메아리·잡음 오인 방지)
    barge_min_ms: int = 400         # 그 소리가 이만큼 이어져야 끼어들기로 인정
    filler_min_speech_ms: int = 300 # 이보다 짧은 소리에는 '네,' 맞장구를 내지 않음
    filler: bool = False            # '네,' 맞장구: 너무 잦다는 의견(2026-10-27)으로 기본 끔. AICC_FILLER=1 로 켬
    real_vad_end_ms: int = 600    # 실통화: '네 통화 / 가능합니다' 처럼 숨 쉬는 사이에 문장이 쪼개지지 않게
    tx_chunk_ms: int = 200
    vad_end_ms: int = 350
    vad_threshold: float = 300.0
    ai_wait_after_ms: int = 3000  # AI 답이 이보다 늦을 때만 '잠시만요'(매번 나오면 기계 같음)
    ai_timeout_s: float = 12.0  # 글 생성 + 같은 목소리 음성 생성(두 단계)
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

    def has(self, key: str) -> bool:
        return bool(((self.manifest.get("clips") or {}).get(key) or {}).get("file"))

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
        self._remember(type_, payload or {})
        if type_ in ("call.answered", "call.not_answered", "call.answer_unknown", "play", "utterance.final",
                     "ai.start", "ai.failed", "schedule.confirmed", "schedule.confirm_rejected", "silence.timeout",
                     "call.remote_hangup", "call.ended", "tx.barge_in", "call.greeting", "play.filler",
                     "rx.ignored_during_tx",
                     "call.dial_not_started", "metric.local_latency", "ai.done"):
            p = dict(payload or {})
            if "text" in p:
                p["text"] = str(p["text"])[:60]
            log.info("[통화 %s] %s %s", getattr(self, "call_id", "?")[:8], type_,
                     {k: v for k, v in p.items() if k in ("key", "text", "reason", "outcome", "date", "code",
                                                         "stt_latency_ms", "detail", "error", "state")})
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
        self._log: list[tuple[str, str]] = []
        self._buf: list[dict] = []
        self.summary = CallSummary(self.call_id)
        self.recorder = DualRecorder()
        self.vad = EnergyVAD(self.cfg.vad_threshold,
                             self.cfg.vad_end_ms if self.is_mock else self.cfg.real_vad_end_ms)
        self._pending_utts: list = []
        pack = claim["voice_pack"]
        self.voice = VoiceBank(pack["manifest"], lambda k: self._fetch_clip(pack["id"], k), self.is_mock)
        clips = {k for k, c in (pack["manifest"].get("clips") or {}).items() if c.get("file")}
        conv = getattr(self.ai, "name", "") not in ("failing",) and not self.is_mock
        self.flow = CallFlow(date.fromisoformat(claim["reference_date"]), clips=clips, ai_conversation=conv)
        if hasattr(self.ai, "voice"):  # AI 목소리를 녹음과 같게(여성 Aoede / 남성 Charon)
            self.ai.voice = "Charon" if pack["manifest"].get("gender") == "male" else "Aoede"
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
            log.error("[통화 %s] 장치 오류: %s", self.call_id[:8], e)
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
        self._preload_queue = self._preload_list()
        t0 = self.device.now_ms()
        progressed = False  # 발신이 실제로 시작된 것(DIALING/ALERTING/OFFHOOK)을 한 번이라도 봤는지
        last_state = None
        while True:
            st = self.device.get_call_state()
            elapsed = self.device.now_ms() - t0
            if st.state != last_state:
                log.info("[통화 %s] 단말 상태 %s %s (%dms)", self.call_id[:8], st.state, st.detail, elapsed)
                last_state = st.state
            if st.state not in ("IDLE", "ENDED"):
                progressed = True
                self._preload_next()  # 신호가 가는 동안 1개씩 미리 올림(응답 감지를 늦추지 않게)
            if st.state == "ACTIVE":
                self.answered_ms = self.device.now_ms()
                self._emit("call.answered", 0, {"detail": st.detail, "source": "device_state"})
                self._flush()
                if not self.is_mock:
                    self._wait_greeting()
                self._apply(self.flow.answered())
                break
            if st.state in ("IDLE", "ENDED") and not progressed and not self.is_mock:
                if elapsed <= 8000:
                    self._tick(self.cfg.poll_ms, record=False)  # 전화 앱이 아직 발신을 시작하지 않았을 수 있음
                    continue
                # 발신 자체가 시작되지 않음: '통화중'으로 꾸미지 않고 장치 오류로 남긴다
                self._emit("call.dial_not_started", 0, st.detail)
                self.device.hangup()
                self._apply(self.flow.device_error("dial_not_started"))
                return
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

    def _preload_list(self) -> list[str]:
        if self.is_mock or not hasattr(self.device, "tx_preload"):
            return []
        return [k for k in PRELOAD_KEYS if self.voice.has(k)]

    def _preload_next(self) -> None:
        """신호가 가는 동안 고정 안내를 휴대폰에 미리 올려 둔다(재생 시작 지연 단축). 실패해도 통화는 계속."""
        if not getattr(self, "_preload_queue", None):
            return
        key = self._preload_queue.pop(0)
        try:
            self.device.tx_preload(key, self.voice.pcm(key))
        except DeviceError as e:
            log.warning("[통화 %s] 안내 미리 올리기 실패(%s): %s", self.call_id[:8], key, e)
            self._preload_queue = []

    def _wait_greeting(self) -> None:
        """받자마자 상대가 '여보세요'라고 하면 끝날 때까지 기다렸다가 안내를 시작한다(안내와 겹침 방지).

        이 인사말은 질문에 대한 답이 아니므로 인식·판정에 쓰지 않는다(녹음에는 남는다).
        """
        t0 = self.device.now_ms()
        vad = EnergyVAD(self.cfg.vad_threshold, 400)
        spoke = False
        while True:
            for f in self._tick(self.cfg.listen_ms):
                for ev in vad.feed(f):
                    if ev.kind == "SPEECH_START":
                        spoke = True
                    elif ev.kind == "SPEECH_END":
                        self._emit("call.greeting", 0, {"ms": self.device.now_ms() - t0})
                        return
            el = self.device.now_ms() - t0
            if (not spoke and el >= self.cfg.greeting_wait_ms) or el >= 4000:
                return

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
        self._filler(frames)
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
        if not self.is_mock and getattr(self.device, "tx_async", lambda: False)():
            self._play_async(a, pcm, frames, start)
            return
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

    def _play_async(self, a: Play, pcm: bytes, frames: list[Frame], start: int) -> None:
        """파일 송출 방식: 재생을 시작해 두고, 끝날 때까지 상대 음성을 계속 듣는다.

        - 재생 중 '평소보다 큰 소리가 barge_min_ms 이상' 이어질 때만 끼어들기로 보고 즉시 멈춘다.
          (우리 안내의 메아리·잡음으로 오프닝이 잘린 사례, 2026-10-27 2차 실통화)
        - 끼어들기로 인정되지 않은 채 끝난 소리는 버린다(메아리를 상대 답으로 인식하지 않게).
        - 끼어든 발화는 지금 안내에 대한 답으로 처리한다(재생 뒤로 밀려 다음 질문에 붙지 않게).
        """
        self._wait_tx_idle()
        keys = self.voice.render(a)
        self.device.tx_play_async(pcm, keys[0] if len(keys) == 1 else None)
        barge_ok = self.cfg.barge_in and a.key not in NO_BARGE_IN
        need = max(1, self.cfg.barge_min_ms // FRAME_MS)
        loud_run = 0
        stopped_at: int | None = None
        while True:
            done = self.device.tx_poll()
            rx = self._tick(self.cfg.listen_ms)
            for f in rx:
                evs = self.vad.feed(f)
                if f.pcm and f.speech:
                    self.rx_speech_frames += 1
                if stopped_at is None:
                    loud_run = loud_run + 1 if f.pcm and rms(f.pcm) >= self.cfg.barge_threshold else 0
                    if barge_ok and not done and loud_run >= need:
                        self.device.stop_tx()
                        stopped_at = self.device.now_ms()
                        self._emit("tx.barge_in", a.turn, {"after_ms": stopped_at - start})
                for ev in evs:
                    if ev.kind != "SPEECH_END" or self.flow.finished:
                        continue
                    if stopped_at is not None:
                        self._pending_utts.append((ev.frames, ev.t_ms))
                    else:
                        self._emit("rx.ignored_during_tx", a.turn, {"ms": len(ev.frames) * FRAME_MS})
            if done or stopped_at is not None:
                break
        if stopped_at is None and self.vad.in_speech and loud_run == 0:
            self.vad.reset()  # 재생 중 시작된 작은 소리(메아리)는 다음 답으로 넘기지 않는다
        end = stopped_at or self.device.now_ms()
        played = [f for f in frames if f.t_ms < end]
        self.recorder.add_tx(played)
        self._emit("tx.written", a.turn, {"frames": len(played), "remote_heard": "UNVERIFIED"})
        self.prompt_end_ms = self.device.now_ms()
        utts, self._pending_utts = self._pending_utts, []
        for fr, t in utts:
            if not self.flow.finished:
                self._on_utterance(fr, t)

    def _wait_tx_idle(self) -> None:
        """앞서 낸 '네,' 맞장구가 아직 재생 중이면 끝날 때까지 기다린다(보통 이미 끝나 있음)."""
        t0 = time.monotonic()
        while not self.device.tx_poll():
            if time.monotonic() - t0 > 5:
                self.device.stop_tx()
                break
            time.sleep(0.02)

    def _filler(self, frames: list[Frame]) -> None:
        """상대가 말을 마치면 인식(약 2초) 전에 짧은 '네,'를 바로 내보내 공백을 메운다. 녹음 'ack'가 있을 때만."""
        if not (self.cfg.filler or os.environ.get("AICC_FILLER") == "1"):
            return
        if self.is_mock or not getattr(self.device, "tx_async", lambda: False)() or not self.voice.has("ack"):
            return
        voiced = sum(1 for f in frames if f.pcm and rms(f.pcm) >= self.cfg.vad_threshold) * FRAME_MS
        if voiced < self.cfg.filler_min_speech_ms:
            return
        try:
            self._wait_tx_idle()
            pcm = self.voice.pcm("ack")
            self.device.tx_play_async(pcm, "ack")
            self.recorder.add_tx(split_frames(pcm, self.device.now_ms()))
            self._emit("play.filler", self.flow.turn, {"key": "ack"})
        except DeviceError as e:
            log.warning("[통화 %s] 맞장구 재생 실패: %s", self.call_id[:8], e)

    def _remember(self, type_: str, payload: dict) -> None:
        """AI 에 넘길 최근 대화(상담원 안내 문구 / 간병사 말)."""
        log_ = getattr(self, "_log", None)
        if log_ is None:
            return
        if type_ == "play":
            clips = (getattr(self, "voice", None) and self.voice.manifest.get("clips")) or {}
            if payload.get("key") == "confirm_date" and payload.get("params"):
                txt = f"{payload['params'].get('month')}월 {payload['params'].get('day')}일부터 근무 가능하신 게 맞으실까요?"
            else:
                txt = (clips.get(payload.get("key")) or {}).get("text") or payload.get("key")
            log_.append(("say", txt))
        elif type_ == "utterance.final":
            log_.append(("hear", payload.get("text", "")))
        elif type_ == "ai.done" and payload.get("text"):
            log_.append(("say", payload["text"]))

    def _dialog_lines(self) -> list[str]:
        out = []
        for e in self._log:
            if e[0] == "say":
                out.append(f"상담원: {e[1]}")
            else:
                out.append(f"간병사: {e[1]}")
        return out

    def _ai(self, a: StartAI) -> None:
        self._emit("ai.start", a.turn, {"return_state": a.return_state})
        self._flush()
        with cf.ThreadPoolExecutor(max_workers=1) as ex:
            ctx = build_prompt(a.question, a.return_state, self._dialog_lines())
            fut = ex.submit(self.ai.respond, a.question, timeout_s=self.cfg.ai_timeout_s, context=ctx)
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
        if self.flow.ai_conversation and not self.is_mock and getattr(self.device, "tx_async", lambda: False)():
            # 대화형: 상태를 먼저 되돌린 뒤 재생 → AI 말 도중 끼어든 답도 원래 질문의 답으로 처리
            self._emit("ai.done", a.turn, {"text": reply.text, "latency_ms": reply.latency_ms, "engine": self.ai.name})
            log.info("[통화 %s] AI: %s (%dms)", self.call_id[:8], reply.text, reply.latency_ms)
            acts = self.flow.ai_done(turn=a.turn)
            self._play_async(Play("ai", a.turn, {}), pcm, split_frames(pcm, self.device.now_ms()), self.device.now_ms())
            self._apply(acts)
            return
        frames = split_frames(pcm, self.device.now_ms())
        for f in frames:
            f.t_ms = self.device.now_ms()
            self.device.write_tx_frames([f])
            self.recorder.add_tx([f])
        self.device.tx_segment_done("ai")
        self._emit("ai.done", a.turn, {"text": reply.text, "latency_ms": reply.latency_ms, "engine": self.ai.name})
        log.info("[통화 %s] AI: %s (%dms)", self.call_id[:8], reply.text, reply.latency_ms)
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
