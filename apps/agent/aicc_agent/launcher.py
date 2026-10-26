"""AICC 통합 실행 프로그램(단일 실행).

1) 이미 실행 중이면 웹 화면만 연다(중복 실행 방지)
2) DB 마이그레이션 → 서버(API+웹) 기동(127.0.0.1)
3) 이 PC에 연결된 단말 설정(.aicc/agent.env)이 있으면 에이전트를 함께 실행, 설정이 바뀌면 재시작
4) 브라우저로 관리화면을 연다

'단일 실행'은 '브라우저만으로 통화 가능'을 뜻하지 않는다. A24 USB 음성 경로가 검증되지 않으면
실통화 기능은 서버에서 잠겨 있다.
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import threading
import time
import webbrowser
from pathlib import Path

log = logging.getLogger("aicc.launcher")


def _port_open(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _read_env(p: Path) -> dict[str, str]:
    out = {}
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def build_agent(env: dict[str, str], base: str):  # noqa: ANN201
    import httpx

    from .a24 import A24Device, Adb, AudioRoute
    from .agent import Agent
    from .ai import make_ai
    from .client import AgentClient
    from .mock_device import MockDevice
    from .stt import make_stt

    data = Path(os.environ.get("AICC_DATA_DIR", ".aicc"))
    kind = env.get("AICC_DEVICE_KIND", "mock")
    client = AgentClient(httpx.Client(timeout=15), env["AICC_DEVICE_TOKEN"], journal=data / "agent_journal.jsonl",
                         base=base)
    if kind == "mock":
        return Agent(client, MockDevice(), make_stt("mock"), make_ai("mock"))
    from .scrcpy_rx import ScrcpyRx, TxOutput

    serial = env.get("AICC_ADB_SERIAL") or None
    rx = tx = route = None
    if env.get("AICC_RX_MODE", "scrcpy") == "scrcpy":
        # 상대 음성은 scrcpy(voice-call-downlink)로 USB 수신, PC 재생 없음
        rx = ScrcpyRx(serial=serial, scrcpy=env.get("AICC_SCRCPY") or None,
                      source=env.get("AICC_SCRCPY_SOURCE") or None, workdir=data / "tmp")
        if env.get("AICC_TX_DEVICE"):
            tx = TxOutput(env["AICC_TX_DEVICE"])
    elif env.get("AICC_RX_DEVICE") and env.get("AICC_TX_DEVICE"):
        route = AudioRoute(env["AICC_RX_DEVICE"], env["AICC_TX_DEVICE"])
    dev = A24Device(Adb(serial=serial), route, state_file=data / "probe_state.json", rx=rx, tx=tx)
    from .ai import FailingAI

    stt = make_stt(env.get("AICC_STT_ENGINE") or "faster-whisper")
    if env.get("GEMINI_API_KEY") and env.get("AICC_GEMINI_MODEL"):
        os.environ["GEMINI_API_KEY"] = env["GEMINI_API_KEY"]
        os.environ["AICC_GEMINI_MODEL"] = env["AICC_GEMINI_MODEL"]
        ai = make_ai("gemini-live")
    else:
        ai = FailingAI()  # 키가 없으면 예외 질문은 '상담 요청'으로 넘기고 날짜는 저장하지 않는다
    return Agent(client, dev, stt, ai)


def supervise_agent(env_file: Path, base: str, stop: threading.Event) -> None:  # pragma: no cover
    current = None
    mtime = 0.0
    agent_stop = threading.Event()
    while not stop.is_set():
        m = env_file.stat().st_mtime if env_file.is_file() else 0.0
        if m != mtime:
            mtime = m
            agent_stop.set()
            if current:
                current.join(timeout=30)
            agent_stop = threading.Event()
            env = _read_env(env_file)
            if env.get("AICC_DEVICE_TOKEN"):
                try:
                    agent = build_agent(env, base)
                except Exception as e:  # noqa: BLE001
                    log.error("에이전트 시작 실패: %s", e)
                    (env_file.parent / "agent_status.txt").write_text(f"ERROR {type(e).__name__}: {e}", encoding="utf-8")
                    current = None
                else:
                    current = threading.Thread(target=agent.run_forever, args=(agent_stop,), daemon=True,
                                               name="aicc-agent")
                    current.start()
                    log.info("에이전트 시작(%s)", env.get("AICC_DEVICE_KIND", "mock"))
                    (env_file.parent / "agent_status.txt").write_text(f"OK {env.get('AICC_DEVICE_KIND', 'mock')}",
                                                                      encoding="utf-8")
        stop.wait(2.0)
    agent_stop.set()


def _pause(msg: str) -> None:  # pragma: no cover
    """바탕화면 아이콘으로 실행했을 때 창이 바로 닫혀 원인을 못 보는 일을 막는다."""
    try:
        input(msg)
    except (EOFError, KeyboardInterrupt):
        pass


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - 프로세스 실행
    import traceback

    try:
        return _run(argv)
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001
        err = traceback.format_exc()
        print("\n[AICC 실행 오류] 아래 내용을 캡처해서 담당자에게 보내 주세요.\n")
        print(err)
        try:
            data = Path(os.environ.get("AICC_DATA_DIR", ".aicc"))
            data.mkdir(parents=True, exist_ok=True)
            (data / "aicc_error.log").write_text(err, encoding="utf-8")
            print(f"오류 기록 파일: {(data / 'aicc_error.log').resolve()}")
        except OSError:
            pass
        _pause("\nEnter 를 누르면 창을 닫습니다...")
        return 1


def _run(argv: list[str] | None = None) -> int:  # pragma: no cover - 프로세스 실행
    ap = argparse.ArgumentParser(prog="aicc", description="간병사 일정 확인 AICC 실행")
    ap.add_argument("--host", default=os.environ.get("AICC_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("AICC_PORT", "8765")))
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args(argv)
    data_dir = Path(os.environ.get("AICC_DATA_DIR", ".aicc"))
    data_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(data_dir / "aicc.log", encoding="utf-8")])
    url = f"http://{'127.0.0.1' if a.host in ('0.0.0.0', '') else a.host}:{a.port}"
    if _port_open("127.0.0.1", a.port):
        print(f"이미 실행 중입니다. 관리화면을 엽니다: {url}")
        if not a.no_browser:
            webbrowser.open(url)
        return 0
    os.environ.setdefault("AICC_LOCAL_AGENT", "1")
    import uvicorn
    from aicc_api.config import get_settings
    from aicc_api.main import create_app

    s = get_settings()
    server = uvicorn.Server(uvicorn.Config(create_app(), host=a.host, port=a.port, log_level="info"))
    t = threading.Thread(target=server.run, daemon=True, name="aicc-server")
    t.start()
    for _ in range(300):
        if _port_open("127.0.0.1", a.port):
            break
        if not t.is_alive():
            break
        time.sleep(0.1)
    if not _port_open("127.0.0.1", a.port):
        raise RuntimeError(f"서버가 시작되지 않았습니다. 기록 파일을 확인하세요: {(data_dir / 'aicc.log').resolve()}")
    stop = threading.Event()
    threading.Thread(target=supervise_agent, args=(s.data_dir / "agent.env", url, stop), daemon=True).start()
    print(f"\nAICC 관리화면: {url}\n데이터 위치: {s.data_dir}\n종료하려면 이 창에서 Ctrl+C\n")
    if not a.no_browser:
        webbrowser.open(url)
    try:
        while t.is_alive():
            t.join(1.0)
    except KeyboardInterrupt:
        pass
    stop.set()
    server.should_exit = True
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
