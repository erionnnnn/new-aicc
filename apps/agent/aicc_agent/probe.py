"""A24 실기기 진단 도구(aicc-probe). 설계 11 'A24 필수 실통화 시험 절차'를 보조한다.

이 도구는 사실을 '수집'하고 사람의 확인을 '기록'할 뿐, 스스로 통과를 선언하지 않는다.
최종 실통화 준비는 관리자가 웹 [설정 > 단말]에서 원격 청취 확인 결과(10회 이상)를 기록해야 활성화된다.

사용 예
  aicc-probe env                                   # 환경 수집(adb·기기·Android·통신사·오디오 장치)
  aicc-probe call --number 010XXXXXXXX --approved  # 승인된 시험 번호로 발신·응답감지·종료 확인
  aicc-probe rx --number 010XXXXXXXX --approved     # scrcpy 로 상대 음성(RX) 수신 시험
  aicc-probe audio --number 010XXXXXXXX --rx 3 --tx 5 --approved   # 사운드카드 RX 녹음·TX 송출 시험
  aicc-probe upload --server http://127.0.0.1:8765 --token <장치토큰>
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .a24 import A24Device, Adb, AudioRoute
from .audio import read_wav_mono16, rms, split_frames, tone, wav_bytes
from .device import DeviceError
from .scrcpy_rx import ScrcpyRx, scrcpy_path

DATA = Path(os.environ.get("AICC_DATA_DIR", ".aicc"))
REPORT = DATA / "probe_report.json"
STATE = DATA / "probe_state.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    except ValueError:
        return {}


def _save(p: Path, d: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def _ask(q: str) -> bool:
    ans = input(f"{q} [y/N]: ").strip().lower()
    return ans in ("y", "yes", "예", "네")


def collect_env(adb: Adb | None = None) -> dict:
    adb = adb or Adb()
    rep: dict = {"collected_at": _now(), "pc": {"os": platform.platform(), "python": sys.version.split()[0]},
                 "facts": [], "unknowns": []}
    if not adb.available():
        rep["adb"] = None
        rep["unknowns"].append("adb 미설치 — Android platform-tools 설치 필요")
        return rep
    try:
        devs = adb.devices()
    except DeviceError as e:
        rep["adb"] = {"error": str(e)}
        return rep
    rep["adb"] = {"devices": devs}
    ready = [d for d in devs if d["state"] == "device"]
    if not ready:
        rep["unknowns"].append("USB 디버깅 허용된 단말 없음")
        return rep
    props = {}
    for k in ("ro.product.model", "ro.product.device", "ro.build.version.release", "ro.build.version.sdk",
              "ro.build.version.oneui", "ro.build.display.id", "gsm.operator.alpha", "gsm.sim.operator.alpha"):
        try:
            props[k] = adb.getprop(k)
        except DeviceError:
            props[k] = None
    rep["device_props"] = props
    model = props.get("ro.product.model") or ""
    rep["facts"].append(f"연결 단말 모델: {model or '알 수 없음'}")
    if model and not model.upper().startswith("SM-A245"):
        rep["unknowns"].append("모델명이 SM-A245(갤럭시 A24 계열)로 보이지 않음 — 설계 기준 기기와 다를 수 있음")
    try:
        st = A24Device(adb).get_call_state()
        rep["call_state_probe"] = {"state": st.state, **st.detail}
        if "precise" in st.detail:
            rep["facts"].append("precise call state 노출됨(응답 감지 후보) — 실제 응답 시점 일치 여부는 call 시험으로 확인")
        else:
            rep["unknowns"].append("precise call state 미노출 — 발신 통화의 응답 감지 불가 가능성")
    except DeviceError as e:
        rep["call_state_probe"] = {"error": str(e)}
    try:
        import sounddevice as sd
        rep["audio_devices"] = [{"index": i, "name": d["name"], "in": d["max_input_channels"],
                                 "out": d["max_output_channels"]} for i, d in enumerate(sd.query_devices())]
        rep["default_output"] = sd.query_devices(kind="output")["name"]
    except Exception as e:  # noqa: BLE001
        rep["audio_devices"] = None
        rep["unknowns"].append(f"오디오 장치 목록 확인 불가({type(e).__name__}) — `pip install .[voice]` 필요")
    sc = scrcpy_path()
    if sc:
        try:
            out = subprocess.run([sc, "--version"], capture_output=True, text=True, timeout=10).stdout
            rep["scrcpy"] = {"path": sc, "version": out.splitlines()[0] if out else None}
            rep["facts"].append(f"scrcpy 발견: {rep['scrcpy']['version']}")
        except (OSError, subprocess.TimeoutExpired) as e:
            rep["scrcpy"] = {"path": sc, "error": type(e).__name__}
    else:
        rep["scrcpy"] = None
        rep["unknowns"].append("scrcpy 미설치 — RX(상대 음성) 수신에 필요. AICC_SCRCPY 로 경로 지정 가능")
    rep["unknowns"].append("RX: scrcpy voice-call-downlink 로 수신 가능 여부는 `aicc-probe rx` 로 확인")
    rep["unknowns"].append("TX: scrcpy 는 송신 미지원 — PC→통화 송출 경로 미검증")
    return rep


def cmd_env(_: argparse.Namespace) -> int:
    rep = collect_env()
    _save(REPORT, rep)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    print(f"\n저장: {REPORT}")
    return 0


def _require_approval(a: argparse.Namespace) -> None:
    if not a.approved:
        print("승인된 시험 번호만 사용할 수 있습니다. 내부 승인을 받았다면 --approved 를 붙이세요.")
        sys.exit(2)


def cmd_call(a: argparse.Namespace) -> int:
    _require_approval(a)
    dev = A24Device(Adb())
    st0 = dev.get_call_state()
    if st0.state not in ("IDLE",):
        print(f"단말이 IDLE 이 아닙니다({st0.state}). 진행하지 않습니다.")
        return 1
    log = []
    dev.dial(a.number)
    print("발신 요청을 보냈습니다. 원격 시험 단말에서 약 10초 뒤에 전화를 받아 주세요.")
    t0 = time.monotonic()
    while time.monotonic() - t0 < a.seconds:
        st = dev.get_call_state()
        log.append({"t": round(time.monotonic() - t0, 2), "state": st.state, **st.detail})
        print(f"{log[-1]['t']:6.2f}s  {st.state}  {st.detail}")
        time.sleep(0.5)
    dev.hangup()
    time.sleep(1.5)
    end = dev.get_call_state()
    state = _load(STATE)
    state["call_log"] = log
    state["dial_ok"] = _ask("원격 단말에 전화가 울렸습니까?")
    state["answer_detection_ok"] = _ask("원격에서 받은 순간 위 로그가 ACTIVE 로 바뀌었고, 그 전에는 ACTIVE 가 아니었습니까?")
    state["hangup_ok"] = end.state == "IDLE" and _ask("종료 명령 후 양쪽 통화가 끊겼습니까?")
    state["tested_at"] = _now()
    _save(STATE, state)
    print(json.dumps({k: state[k] for k in ("dial_ok", "answer_detection_ok", "hangup_ok")}, ensure_ascii=False))
    return 0


def cmd_audio(a: argparse.Namespace) -> int:  # pragma: no cover - 실기기 필요
    _require_approval(a)
    route = AudioRoute(int(a.rx) if a.rx.isdigit() else a.rx, int(a.tx) if a.tx.isdigit() else a.tx)
    dev = A24Device(Adb(), route)
    dev.dial(a.number)
    input("원격 단말에서 전화를 받은 뒤 Enter 를 누르세요...")
    pcm = read_wav_mono16(Path(a.clip).read_bytes()) if a.clip else tone(3000, 440.0, 4000)
    print("TX: 시험 음성을 송출합니다(원격 청취자는 들리는 내용을 기록하세요).")
    dev.write_tx_frames(split_frames(pcm, dev.now_ms()))
    print(f"RX: {a.seconds}초 동안 녹음합니다. 원격 청취자는 시험 문장 10개를 읽어 주세요.")
    frames = dev.read_rx_frames(a.seconds * 1000)
    dev.hangup()
    route.close()
    out = DATA / f"probe_rx_{int(time.time())}.wav"
    out.write_bytes(wav_bytes(b"".join(f.pcm for f in frames)))
    print(f"RX 녹음 저장: {out} — 재생해서 원격 문장이 들리는지 확인하세요(PC 마이크 소리가 아니어야 함).")
    state = _load(STATE)
    state["rx_audio_ok"] = _ask("RX 녹음에서 원격 문장이 또렷하게 들리고, PC 마이크·스피커 소리는 아닙니까?")
    state["tx_audio_ok"] = _ask("원격 청취자가 TX 시험 음성을 처음부터 끝까지 들었습니까?")
    state["pc_silent_ok"] = _ask("통화 중 PC 스피커/기본 출력에서 아무 소리도 나지 않았습니까?")
    state["recording_ok"] = state["rx_audio_ok"] and state["tx_audio_ok"]
    state["audio_tested_at"] = _now()
    state["audio_route"] = {"rx": a.rx, "tx": a.tx}
    _save(STATE, state)
    return 0


def cmd_rx(a: argparse.Namespace) -> int:  # pragma: no cover - 실기기 필요
    """scrcpy 로 통화 수신 음성(RX)만 녹음해 확인한다. PC 스피커로는 재생하지 않는다."""
    _require_approval(a)
    rx = ScrcpyRx(source=a.source, workdir=DATA / "tmp")
    dev = A24Device(Adb(), rx=rx)
    if dev.get_call_state().state != "IDLE":
        print("단말이 IDLE 이 아닙니다. 진행하지 않습니다.")
        return 1
    dev.dial(a.number)
    input("원격 단말에서 전화를 받은 뒤 Enter 를 누르세요. 원격 청취자는 시험 문장 10개를 읽어 주세요...")
    frames = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < a.seconds:
        frames += dev.read_rx_frames(500)
        print(f"\r수신 {len(frames) * 20 / 1000:5.1f}초", end="")
    dev.hangup()
    pcm = b"".join(f.pcm for f in frames)
    out = DATA / f"probe_rx_scrcpy_{int(time.time())}.wav"
    out.write_bytes(wav_bytes(pcm))
    loud = sum(1 for f in frames if rms(f.pcm) > 300)
    print(f"\n저장: {out}  (소리 있는 구간 {loud * 20 / 1000:.1f}초 / 전체 {len(frames) * 20 / 1000:.1f}초)")
    state = _load(STATE)
    state["rx_audio_ok"] = loud > 0 and _ask("저장된 파일을 헤드셋으로 들어 보니 원격 문장이 또렷하게 들립니까?")
    state["rx_mode"] = f"scrcpy:{a.source}"
    state["rx_tested_at"] = _now()
    _save(STATE, state)
    return 0


def cmd_upload(a: argparse.Namespace) -> int:
    import httpx

    from .client import AgentClient
    rep = _load(REPORT)
    rep["probe_state"] = _load(STATE)
    c = AgentClient(httpx.Client(timeout=10), a.token, base=a.server)
    c.heartbeat(capabilities=A24Device(Adb(), state_file=STATE).probe_capabilities(), probe_report=rep)
    print("진단 결과를 서버에 올렸습니다. 관리자 화면에서 실통화 검증 결과를 기록하세요.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="aicc-probe", description="A24 실기기 진단")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("env", help="환경 수집")
    pc = sub.add_parser("call", help="발신·응답감지·종료 시험")
    pc.add_argument("--number", required=True)
    pc.add_argument("--seconds", type=int, default=30)
    pc.add_argument("--approved", action="store_true")
    pa = sub.add_parser("audio", help="RX/TX 오디오 시험")
    pa.add_argument("--number", required=True)
    pa.add_argument("--rx", required=True, help="RX 입력 장치 번호/이름")
    pa.add_argument("--tx", required=True, help="TX 출력 장치 번호/이름(PC 기본 스피커 금지)")
    pa.add_argument("--clip", help="송출할 16bit mono WAV(없으면 시험 톤)")
    pa.add_argument("--seconds", type=int, default=20)
    pa.add_argument("--approved", action="store_true")
    pr = sub.add_parser("rx", help="scrcpy 로 통화 수신 음성(RX) 시험")
    pr.add_argument("--number", required=True)
    pr.add_argument("--seconds", type=int, default=20)
    pr.add_argument("--source", default="voice-call-downlink")
    pr.add_argument("--approved", action="store_true")
    pu = sub.add_parser("upload", help="진단 결과 서버 업로드")
    pu.add_argument("--server", default="http://127.0.0.1:8765")
    pu.add_argument("--token", required=True)
    a = ap.parse_args(argv)
    handlers = {"env": cmd_env, "call": cmd_call, "audio": cmd_audio, "upload": cmd_upload, "rx": cmd_rx, None: cmd_env}
    try:
        return handlers[a.cmd](a)
    except DeviceError as e:
        print(f"\n[진단 중단] {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
