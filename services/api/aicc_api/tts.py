"""안내 음성 자동 생성(TTS). 녹음 없이 음원팩 전체(필수 문구 + 날짜 조각)를 만든다.

엔진
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


def synthesize(engine: str, text: str, pack_key: str) -> bytes:
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
