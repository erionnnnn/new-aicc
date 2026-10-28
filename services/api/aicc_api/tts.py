"""안내 음성 자동 생성(TTS). 녹음 없이 음원팩 전체(필수 문구 + 날짜 조각)를 만든다.

엔진
- voicestudio: 이 PC 의 VoiceStudio(무료, 로컬). 상업 사용 가능한 모델(cosyvoice 등)만.
- gemini: 기존 녹음과 같은 Gemini 목소리(여성 Aoede, 남성 Charon). 이 PC 의 Gemini 키 사용, 인터넷 필요.
- edge: Microsoft Edge 온라인 신경망 음성(edge-tts) + miniaudio 로 16kHz 모노 WAV 변환. 인터넷 필요.
  여성 ko-KR-SunHiNeural, 남성 ko-KR-InJoonNeural. 특정 인물 목소리가 아닌 일반 합성 음성이다.
- sapi: Windows 내장 음성(System.Speech, 예: Microsoft Heami). 인터넷 불필요, 음질은 낮다.

생성한 음원도 관리자가 듣고 '승인'해야 실통화에 쓸 수 있다(설계 03: 승인된 음원만 사용).
"""

from __future__ import annotations

import asyncio
import base64
import io
import subprocess
import sys
import wave

VOICES = {"female": "ko-KR-SunHiNeural", "male": "ko-KR-InJoonNeural"}
TARGET_RATE = 16000


class TTSError(RuntimeError):
    pass


def _wav16k(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(TARGET_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def synth_edge(text: str, pack_key: str, rate: str = "+0%") -> bytes:  # pragma: no cover - 네트워크
    try:
        import edge_tts
        import miniaudio
    except ImportError as e:
        raise TTSError("edge-tts/miniaudio 가 설치되지 않았습니다(설치 스크립트를 다시 실행)") from e

    async def run() -> bytes:
        comm = edge_tts.Communicate(text, VOICES.get(pack_key, VOICES["female"]), rate=rate)
        out = bytearray()
        async for chunk in comm.stream():
            if chunk.get("type") == "audio":
                out.extend(chunk["data"])
        return bytes(out)

    try:
        mp3 = asyncio.run(run())
    except Exception as e:  # noqa: BLE001
        raise TTSError(f"온라인 음성 생성 실패({type(e).__name__}) — 인터넷 연결을 확인하거나 Windows 음성(sapi)을 쓰세요") from e
    if not mp3:
        raise TTSError("음성 데이터가 비어 있습니다")
    snd = miniaudio.decode(mp3, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=TARGET_RATE)
    return _wav16k(snd.samples.tobytes())


_SAPI_PS = r"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$ko = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -eq 'ko-KR' } | Select-Object -First 1
if (-not $ko) { Write-Error 'NO_KOREAN_VOICE'; exit 3 }
$s.SelectVoice($ko.VoiceInfo.Name)
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$ms = New-Object System.IO.MemoryStream
$s.SetOutputToAudioStream($ms, $fmt)
$text = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:AICC_TTS_TEXT))
$s.Speak($text)
[Console]::Out.Write([Convert]::ToBase64String($ms.ToArray()))
"""


def synth_sapi(text: str, pack_key: str) -> bytes:  # pragma: no cover - Windows 전용
    if not sys.platform.startswith("win"):
        raise TTSError("Windows 음성은 Windows 에서만 쓸 수 있습니다")
    import os
    env = dict(os.environ, AICC_TTS_TEXT=base64.b64encode(text.encode()).decode())
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _SAPI_PS], capture_output=True,
                       text=True, env=env, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        if "NO_KOREAN_VOICE" in r.stderr:
            raise TTSError("Windows 에 한국어 음성이 없습니다(설정 > 시간 및 언어 > 음성에서 한국어 음성 추가)")
        raise TTSError(f"Windows 음성 생성 실패: {r.stderr.strip()[:200]}")
    return _wav16k(base64.b64decode(r.stdout.strip()))


GEMINI_VOICES = {"female": "Aoede", "male": "Charon"}  # 기존 프로그램 v11.44 와 같은 목소리(VOICE_SELECTION_v11_44.md)
# 모델 이름은 자주 바뀐다(Gemini API 변경 기록 2026-10-27 확인: gemini-3.8-flash-tts GA 2026-09-22).
# 처음 성공한 모델을 파일(.aicc/tts_model.txt)에 고정해, 이후 안내·AI 답 모두 같은 모델·같은 목소리로 만든다.
GEMINI_TTS_MODELS = ("gemini-3.8-flash-tts", "gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts")
# 말투 지시문을 문장 앞에 붙이면 모델이 지시문까지 소리 내어 읽는 경우가 있어(2026-10-27 실통화 확인)
# 안내 문장만 보낸다. 같은 모델·같은 목소리·음량 정규화로 통일한다.
TTS_REV = "v2-plain"  # 만드는 방식이 바뀌면 올림 → '전체 목소리 통일'이 예전 방식 음원을 다시 만든다


def _model_file():  # noqa: ANN202
    import os
    from pathlib import Path
    return Path(os.environ.get("AICC_DATA_DIR", ".aicc")) / "tts_model.txt"


def pinned_model() -> str | None:
    try:
        m = _model_file().read_text(encoding="utf-8").strip()
        return m or None
    except OSError:
        return None


def _pin(model: str) -> None:
    try:
        f = _model_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(model, encoding="utf-8")
    except OSError:
        pass


def voice_tag(pack_key: str, engine: str = "gemini") -> str:
    """음원 출처 표시(같은 표시 = 같은 엔진·모델·목소리로 만든 음원 → '전체 목소리 통일'이 건너뜀)."""
    if engine == "voicestudio":
        c = vs_config(pack_key)
        # ':profile' — 저장된 프로필로만 만든 음원(그 전 무작위 목소리 음원은 '전체 만들기'에서 다시 만듦)
        return f"tts:voicestudio:{c['model']}:{c['voice']}:{TTS_REV}:profile"
    return f"tts:gemini:{pinned_model() or '?'}:{GEMINI_VOICES.get(pack_key, 'Aoede')}:{TTS_REV}"


# ---------------- VoiceStudio(이 PC 에 설치한 무료 로컬 음성 프로그램) ----------------
# OpenAI 호환 주소 POST {url}/v1/audio/speech (VoiceStudio backend/api/routers/openai_compat.py, 2026-10-28 확인)
# 모델 라이선스(예: OmniVoice 가중치 CC-BY-NC)는 회사가 판단한다 — 2026-10-28 사용자: 담당자와 협의 완료, OmniVoice 사용.
def vs_config(pack_key: str = "female") -> dict:
    import os
    env = {}
    try:
        from .routers.admin import _read_agent_env
        env = _read_agent_env()
    except Exception:  # noqa: BLE001
        pass
    g = lambda k, d="": (env.get(k) or os.environ.get(k) or d).strip()  # noqa: E731
    voice = g("AICC_VS_VOICE_MALE") if pack_key == "male" else g("AICC_VS_VOICE")
    return {"url": g("AICC_VS_URL", "http://127.0.0.1:3900").rstrip("/"), "model": g("AICC_VS_MODEL", "omnivoice"),
            "voice": voice or "default", "instruct": g("AICC_VS_INSTRUCT")}


def pack_engine(pack_key: str) -> str:
    """안내 음원팩을 마지막으로 만든 엔진(AI 답도 같은 엔진·목소리로 읽기 위함)."""
    try:
        return (_model_file().parent / f"pack_engine_{pack_key}.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return "gemini"


def set_pack_engine(pack_key: str, engine: str) -> None:
    try:
        f = _model_file().parent / f"pack_engine_{pack_key}.txt"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(engine, encoding="utf-8")
    except OSError:
        pass


_vs_profile_cache: dict[tuple[str, str], str] = {}


def vs_profiles(url: str) -> list[dict]:
    import httpx
    r = httpx.get(f"{url}/v1/audio/voices", timeout=10)
    r.raise_for_status()
    return [v for v in (r.json().get("voices") or []) if v.get("type") == "profile"]


def vs_profile_id(url: str, name: str) -> str:
    """설정한 이름을 VoiceStudio 의 '저장된 목소리 프로필' id 로 바꾼다. 없으면 생성하지 않고 알린다.

    VoiceStudio 는 프로필을 못 찾으면 이름을 엔진 프리셋으로 넘기고, OmniVoice 는 그때 매번 다른 목소리
    (남녀 무작위)를 만든다(2026-10-28 실사용 확인) — 그래서 반드시 저장된 프로필로만 만든다.
    """
    import unicodedata
    key = (url, name)
    if key in _vs_profile_cache:
        return _vs_profile_cache[key]
    if not name or name.strip().lower() == "default":
        raise TTSError("VoiceStudio 목소리 프로필 이름이 비어 있습니다. 설정 > 단말 > 이 PC 에이전트에 저장한 프로필 이름을 넣으세요")
    try:
        profs = vs_profiles(url)
    except Exception as e:  # noqa: BLE001
        raise TTSError(f"VoiceStudio 에 연결하지 못했습니다({url}). 프로그램이 켜져 있는지 확인하세요 ({type(e).__name__})") from e
    norm = lambda x: unicodedata.normalize("NFKC", str(x or "")).strip().casefold()  # noqa: E731
    hits = [v for v in profs if v.get("voice_id") == name or norm(v.get("name")) == norm(name)]
    if len(hits) != 1:
        names = ", ".join(str(v.get("name")) for v in profs[:15]) or "(없음)"
        why = "같은 이름이 여러 개입니다" if hits else "찾지 못했습니다"
        raise TTSError(f"VoiceStudio 에서 목소리 프로필 '{name}'을(를) {why}. 저장된 프로필: {names}. "
                       "음성 디자인 화면 왼쪽 아래 '프로필 이름'에 이름을 넣고 저장했는지 확인하세요")
    _vs_profile_cache[key] = hits[0]["voice_id"]
    return hits[0]["voice_id"]


def synth_voicestudio(text: str, pack_key: str) -> bytes:  # pragma: no cover - 로컬 프로그램 필요
    import httpx
    c = vs_config(pack_key)
    voice_id = vs_profile_id(c["url"], c["voice"])  # 저장된 프로필이 아니면 매번 다른(남녀 무작위) 목소리가 나온다
    body = {"model": c["model"], "input": text, "voice": voice_id, "response_format": "wav", "language": "ko"}
    if c["instruct"]:
        body["instructions"] = c["instruct"]  # 별도 칸으로 전달(문장에 섞지 않음 → 지시문을 읽어 버리는 문제 없음)
    try:
        r = httpx.post(f"{c['url']}/v1/audio/speech", json=body, timeout=180)
    except httpx.HTTPError as e:
        raise TTSError(f"VoiceStudio 에 연결하지 못했습니다({c['url']}). VoiceStudio 프로그램이 켜져 있는지 확인하세요 "
                       f"({type(e).__name__})") from e
    if r.status_code != 200:
        try:
            msg = r.json().get("error", {}).get("message") or r.text
        except ValueError:
            msg = r.text
        raise TTSError(f"VoiceStudio 음성 생성 실패({r.status_code}): {str(msg)[:200]}")
    data = r.content
    if data[:4] != b"RIFF":
        raise TTSError("VoiceStudio 가 WAV 가 아닌 응답을 보냈습니다")
    with wave.open(io.BytesIO(data)) as w:
        ch, sw, rate, pcm = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.readframes(w.getnframes())
    if sw != 2:
        raise TTSError(f"VoiceStudio 음성 형식(비트 {sw * 8})을 바꿀 수 없습니다")
    if ch > 1:
        from array import array
        a = array("h")
        a.frombytes(pcm)
        pcm = array("h", (sum(a[i:i + ch]) // ch for i in range(0, len(a), ch))).tobytes()
    from aicc_agent.audio import resample_linear
    return normalize_wav(_wav16k(resample_linear(pcm, rate, TARGET_RATE) if rate != TARGET_RATE else pcm))


def _pcm24_to_wav16k(data: bytes) -> bytes:
    """Gemini 음성(WAV 머리글 있거나 없는 24kHz 16bit 모노 PCM)을 16kHz WAV 로."""
    rate, pcm = 24000, data
    if data[:4] == b"RIFF":
        with wave.open(io.BytesIO(data)) as w:
            rate, pcm = w.getframerate(), w.readframes(w.getnframes())
            if w.getnchannels() != 1 or w.getsampwidth() != 2:
                raise TTSError("Gemini 음성 형식이 16bit 모노가 아닙니다")
    from aicc_agent.audio import resample_linear
    return _wav16k(resample_linear(pcm, rate, TARGET_RATE) if rate != TARGET_RATE else pcm)


_calls: list[float] = []
TTS_RPM = 8  # Gemini TTS 분당 한도 10회(AI Studio 사용량 화면, 2026-10-27) — 여유를 두고 8회


def _pace() -> None:
    """분당 요청 수를 TTS_RPM 이하로 맞춘다(한도에 걸려 1분씩 기다리는 것보다 빠름)."""
    import time
    now = time.monotonic()
    while _calls and now - _calls[0] > 60:
        _calls.pop(0)
    if len(_calls) >= TTS_RPM:
        time.sleep(60 - (now - _calls[0]) + 0.5)
        _calls.pop(0)
    _calls.append(time.monotonic())


def synth_gemini(text: str, pack_key: str, api_key: str | None, model: str | None = None) -> bytes:  # pragma: no cover
    """Gemini 목소리(여성 Aoede / 남성 Charon)로 만든다. 모델이 고정된 뒤에는 다른 모델로 바꾸지 않는다."""
    if not api_key:
        raise TTSError("Gemini 키가 없습니다(설정 > 단말 > 이 PC 에이전트에서 입력)")
    try:
        from google import genai
        from google.genai import types
    except ImportError as e:
        raise TTSError("google-genai 가 설치되지 않았습니다(설치 스크립트를 다시 실행)") from e
    import time
    client = genai.Client(api_key=api_key)
    voice = GEMINI_VOICES.get(pack_key, "Aoede")
    cfg = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))))
    pinned = model or pinned_model()
    models = [pinned] if pinned else list(GEMINI_TTS_MODELS)  # 고정되면 그 모델만(목소리 통일)
    last = ""
    for m in models:
        for attempt in range(6):
            try:
                _pace()
                resp = client.models.generate_content(model=m, contents=text, config=cfg)
                part = resp.candidates[0].content.parts[0]
                data = part.inline_data.data
                if isinstance(data, str):
                    data = base64.b64decode(data)
                if not data:
                    raise TTSError("빈 음성")
                if not pinned:
                    _pin(m)
                return normalize_wav(_pcm24_to_wav16k(data))
            except TTSError:
                raise
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if "API key not valid" in msg or "API_KEY_INVALID" in msg or "PERMISSION_DENIED" in msg:
                    raise TTSError("Gemini 키가 올바르지 않습니다. 설정 > 단말 > 이 PC 에이전트에서 Google AI Studio 에서 받은 "
                                   "키(보통 AIza 로 시작)를 다시 붙여 넣으세요. 브라우저가 로그인 비밀번호를 자동으로 채웠을 수 있습니다."
                                   ) from None
                last = f"{m}: {type(e).__name__} {msg[:160]}"
                if ("429" in msg or "RESOURCE_EXHAUSTED" in msg) and ("PerDay" in msg or "per day" in msg.lower()):
                    # 하루 사용 한도(무료 등급) — 기다려도 안 풀리므로 바로 알린다. 만든 것까지는 저장돼 이어서 할 수 있다
                    raise TTSError("Gemini 하루 사용 한도를 넘었습니다. 한도가 초기화된 뒤(보통 오후 4~5시 무렵) 같은 버튼을 다시 "
                                   "누르면 이어서 만듭니다.") from None
                if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                    time.sleep(62)  # 분당 요청 한도(TTS 10회/분) — 1분 창이 지나면 같은 모델로 재시도
                    continue
                if "503" in msg or "500" in msg:
                    time.sleep(5 * (attempt + 1))
                    continue
                break  # 모델 없음 → (고정 전이면) 다음 모델
    raise TTSError(f"Gemini 음성 생성 실패 — {last}")


def normalize_wav(wav16k: bytes, target_dbfs: float = -20.0, peak_dbfs: float = -1.5) -> bytes:
    """음량을 같은 크기로 맞춘다(안내마다 크기가 다르면 다른 목소리처럼 들림). 말소리 구간 RMS 기준."""
    import math
    from array import array
    with wave.open(io.BytesIO(wav16k)) as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            return wav16k
        rate, raw = w.getframerate(), w.readframes(w.getnframes())
    a = array("h")
    a.frombytes(raw)
    if not a:
        return wav16k
    win = max(1, rate // 50)  # 20ms
    levels = []
    for i in range(0, len(a) - win + 1, win):
        seg = a[i:i + win]
        levels.append(math.sqrt(sum(x * x for x in seg) / len(seg)))
    if not levels:
        return wav16k
    top = max(levels)
    voiced = [v for v in levels if v >= top * 0.1] or levels
    rms = math.sqrt(sum(v * v for v in voiced) / len(voiced))
    if rms < 1:
        return wav16k
    gain = (32768 * 10 ** (target_dbfs / 20)) / rms
    peak = max(abs(x) for x in a) or 1
    gain = min(gain, (32768 * 10 ** (peak_dbfs / 20)) / peak)
    out = array("h", (max(-32768, min(32767, int(x * gain))) for x in a))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(out.tobytes())
    return buf.getvalue()


def synthesize(engine: str, text: str, pack_key: str, *, api_key: str | None = None, model: str | None = None) -> bytes:
    if engine == "gemini":
        return synth_gemini(text, pack_key, api_key, model)
    if engine == "voicestudio":
        return synth_voicestudio(text, pack_key)
    if engine == "edge":
        return synth_edge(text, pack_key)
    if engine == "sapi":
        return synth_sapi(text, pack_key)
    if engine == "test":  # 시험용: 글자 수에 비례한 톤
        import math
        import struct
        n = TARGET_RATE * max(300, 70 * len(text)) // 1000
        pcm = struct.pack(f"<{n}h", *(int(2000 * math.sin(2 * math.pi * 300 * i / TARGET_RATE)) for i in range(n)))
        return _wav16k(pcm)
    raise TTSError(f"알 수 없는 음성 엔진: {engine}")
