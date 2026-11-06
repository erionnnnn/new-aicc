"""녹음 대본(JSON) 만들기 — 외부 TTS 도구(VoiceCraft 형식)에 넣어 mp3 를 만든 뒤 AICC '녹음 zip 올리기'로 올린다.

    python scripts/make_voice_script.py            → docs/voice/aicc_voice_script_{female,male}.json

문구는 코드(aicc_core.voicepack·scenarios)에서 그대로 뽑으므로, 문구를 고치면 이 스크립트를 다시 실행한다.
파일명 규칙 '번호_칸이름.mp3'(남성은 _MALE)을 AICC 가 읽어 자동 배정한다. 모두 고정 문장(날짜 추정·빈칸 없음).
"""

from __future__ import annotations

import json
from pathlib import Path

from aicc_core.faq import FAQS
from aicc_core.scenarios import BASE_SCENARIO_KEYS, CURRENT_TEXTS, SCENARIOS
from aicc_core.voicepack import (
    DATE_PIECE_KEYS,
    DEFAULT_SCRIPTS,
    END_ACK_KEYS,
    EXTRA_TEXTS,
    FULL_CONFIRM_KEYS,
    REQUIRED_KEYS,
    VARIANT_TEXTS,
)

OUT = Path(__file__).resolve().parents[1] / "docs" / "voice"
COMPANY, SPEAKER = "좋은간병", "김태희 실장"
VOICES = (("female", "여성", "김태희 실장", ""), ("male", "남성", "남성 실장", "_MALE"))


def text(key: str) -> str:
    if key in DATE_PIECE_KEYS:
        n = int(key.split("_")[1])
        return f"{n}월" if key.startswith("month") else f"{n}일"
    t = CURRENT_TEXTS.get(key, DEFAULT_SCRIPTS.get(key, ""))
    return t.replace("[회사명]", COMPANY).replace("{speaker}", SPEAKER)


def keys() -> list[tuple[str, str]]:
    unused = {s.key for s in SCENARIOS if s.kind == "UNUSED"}
    base = [k for k in (*REQUIRED_KEYS, "ack", *BASE_SCENARIO_KEYS, *EXTRA_TEXTS) if k not in unused]
    out = [(k, "차분하고 친절함") for k in base]
    out += [(k, "차분하고 친절함") for k in VARIANT_TEXTS]
    out += [(k, "차분함") for k in DATE_PIECE_KEYS]
    out += [(k, "차분하고 친절함") for k in (*FULL_CONFIRM_KEYS, *END_ACK_KEYS)]
    # 예상 질문 200개(2026-10-29 추가) — 기존 번호가 바뀌지 않도록 맨 뒤에 붙인다(표에 적은 분야 순서)
    out += [("sc_faq_" + f.code.lower(), "차분하고 친절함") for f in FAQS]
    return out


def main() -> None:
    items = keys()
    assert len({k for k, _ in items}) == len(items)
    OUT.mkdir(parents=True, exist_ok=True)
    for slug, gender, speaker, suffix in VOICES:
        data = []
        for i, (k, style) in enumerate(items, 1):
            t = text(k)
            assert t and "{" not in t and "[" not in t, k
            data.append({"speaker": speaker, "gender": gender, "age": "젊은 성인", "pitch": "적당한 음조", "style": style,
                         "text": t, "filename": f"{i:03d}_{k}{suffix}.mp3"})
        path = OUT / f"aicc_voice_script_{slug}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{path} ({len(data)}개)")


if __name__ == "__main__":
    main()
