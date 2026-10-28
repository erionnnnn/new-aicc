"""MeloTTS 로컬 음성 서버(이 PC 의 CPU 로 한국어 음성 생성).

AICC 와 다른 Python 환경(.venv-melo, Python 3.10)에서 돈다. MeloTTS 는 Python 3.9 기준으로 개발되어
AICC 환경(3.11+)과 의존성이 충돌하기 때문이다. AICC 는 http 로만 부른다.

  GET  /health             → {"ok": true, "language": "KR", "speakers": [...], "sample_rate": 24000}
  POST /tts {"text", "speed"} → audio/wav (24kHz, 모노, PCM 16비트)

실행: .venv-melo\\Scripts\\python.exe scripts\\melo_server.py --port 3910
외부에서 접근하지 못하도록 127.0.0.1 에만 연다.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OUT_RATE = 24000
MAX_TEXT = 500

_model = None
_speaker = None
_lock = threading.Lock()


def _patch_g2pkk() -> None:
    """g2pkk(한국어 발음 변환)는 Windows 에서 eunjeon 을 요구하는데, eunjeon 은 Windows 설치 파일이 없어
    C++ 빌드 도구가 있어야 설치된다. Windows 설치 파일이 있는 python-mecab-ko 를 쓰도록 바꾼다."""
    try:
        import g2pkk.g2pkk as g
    except ImportError:
        return

    def get_mecab(self):  # noqa: ANN001, ANN202
        import mecab  # python-mecab-ko
        return mecab.MeCab()

    g.G2p.check_mecab = lambda self: None
    g.G2p.get_mecab = get_mecab


def _patch_mecab_ja() -> None:
    """MeloTTS 는 한국어만 써도 일본어 모듈을 불러오며 MeCab.Tagger() 를 만든다. 기본값은 'unidic'(별도로
    수백 MB 를 내려받아야 함)이라, 함께 설치되는 작은 unidic_lite 사전을 쓰게 한다(한국어 음성에는 영향 없음)."""
    try:
        import MeCab
        import unidic_lite
    except ImportError:
        return
    import os
    orig = MeCab.try_import_unidic

    def pick() -> str | None:
        d = orig()
        if d and os.path.exists(os.path.join(d, "mecabrc")):
            return d  # 큰 unidic 을 이미 내려받았다면 그것을 쓴다
        return unidic_lite.DICDIR

    MeCab.try_import_unidic = pick


def load(device: str = "cpu") -> None:
    global _model, _speaker
    _patch_g2pkk()
    _patch_mecab_ja()
    from melo.api import TTS

    t0 = time.monotonic()
    _model = TTS(language="KR", device=device)
    ids = _model.hps.data.spk2id
    _speaker = ids["KR"] if "KR" in ids else next(iter(ids.values()))
    print(f"[melo] 한국어 모델 준비 완료 ({time.monotonic() - t0:.1f}초, 화자 {list(ids.keys())})", flush=True)


def synth(text: str, speed: float) -> bytes:
    import numpy as np
    import soundfile as sf

    with _lock:  # 모델은 한 번에 한 문장만
        audio = _model.tts_to_file(text, _speaker, None, speed=speed, quiet=True)
    sr = int(_model.hps.data.sampling_rate)
    audio = np.asarray(audio, dtype=np.float32)
    if sr != OUT_RATE:
        import librosa
        audio = librosa.resample(audio, orig_sr=sr, target_sr=OUT_RATE)
    buf = io.BytesIO()
    sf.write(buf, np.clip(audio, -1.0, 1.0), OUT_RATE, subtype="PCM_16", format="WAV")
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/health":
            ids = list(_model.hps.data.spk2id.keys()) if _model else []
            return self._json(200, {"ok": _model is not None, "language": "KR", "speakers": ids,
                                    "sample_rate": OUT_RATE})
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/tts":
            return self._json(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(min(n, 100_000)) or b"{}")
            text = str(req.get("text") or "").strip()
            speed = float(req.get("speed") or 1.0)
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"error": "요청 형식 오류"})
        if not text or len(text) > MAX_TEXT:
            return self._json(400, {"error": f"문장은 1~{MAX_TEXT}자"})
        speed = max(0.6, min(1.6, speed))
        try:
            wav = synth(text, speed)
        except Exception as e:  # noqa: BLE001
            return self._json(500, {"error": f"{type(e).__name__}: {str(e)[:200]}"})
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(wav)))
        self.end_headers()
        self.wfile.write(wav)

    def log_message(self, fmt: str, *args) -> None:  # noqa: ANN002
        pass  # 문장 내용(개인정보 가능)을 로그에 남기지 않는다


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=3910)
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--warmup", action="store_true", help="모델을 내려받고 한 문장 만들어 본 뒤 종료(설치 확인용)")
    a = ap.parse_args()
    load(a.device)
    if a.warmup:
        t0 = time.monotonic()
        wav = synth("안녕하세요, 좋은간병입니다.", 1.0)
        print(f"[melo] 시험 문장 생성 성공: {len(wav)} 바이트, {time.monotonic() - t0:.1f}초", flush=True)
        return 0
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"[melo] http://127.0.0.1:{a.port} 대기 중", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
