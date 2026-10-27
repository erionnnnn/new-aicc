"""ARS 음원팩 규격과 실통화 가능 여부 검사.

설계 04 ARS 음원 구성의 필수 키에 아래 두 가지를 추가했다(문서화된 확장).
- wrong_person: 잘못 연결된 상대에게 사과하고 종료하는 안내(설계 03 간병사 항목의 '번호 확인 대상' 처리용)
- 날짜 조각 month_1..month_12, day_1..day_31: confirm_date 재확인 시 "10월" "26일" + confirm_date 순서로 이어 붙인다.

자리표시자 문구([회사명], PLACEHOLDER, TODO)가 남아 있거나 파일이 없으면 실통화 불가로 판정한다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

REQUIRED_KEYS = (
    "opening", "ask_working", "ask_end_date", "ask_available_date", "confirm_date", "ask_repeat",
    "explain_role", "callback", "refusal_ack", "closing", "ai_wait", "ai_failed", "wrong_person",
)
# 선택 음원: 없어도 실통화 가능. ack = 상대가 말을 마치면 인식 전에 바로 내는 짧은 맞장구("네,")
OPTIONAL_KEYS = ("ack",)
DATE_PIECE_KEYS = tuple(f"month_{m}" for m in range(1, 13)) + tuple(f"day_{d}" for d in range(1, 32))
PLACEHOLDER_MARKERS = ("[회사명]", "PLACEHOLDER", "TODO", "자리표시자", "[이름]")

# 기본 스크립트 초안(설계 03 오프닝 초안 기반). 실제 녹음 전 관리자가 승인해야 한다.
DEFAULT_SCRIPTS = {
    "opening": "안녕하세요. [회사명]에서 근무 일정 확인을 맡고 있는 {speaker}의 자동 안내입니다. "
               "전담 실장님과는 별도로 다음 근무 가능 일정을 확인드리고 있어요. 잠시 통화 괜찮으실까요?",
    "ask_working": "지금 간병 근무 중이신가요?",
    "ask_end_date": "현재 근무는 언제까지 하실 예정이세요?",
    "ask_available_date": "그럼 다음 근무는 며칠부터 가능하세요?",
    "confirm_date": "부터 근무 가능하신 게 맞으실까요?",
    "ask_repeat": "죄송하지만 다시 한 번 말씀해 주시겠어요?",
    "explain_role": "저는 근무 일정만 확인드리고 있고, 일자리 배정은 전담 실장님이 따로 연락드려요.",
    "callback": "네, 알겠습니다. 편하실 때 다시 연락드릴게요.",
    "refusal_ack": "네, 앞으로 연락드리지 않겠습니다. 죄송합니다. 안녕히 계세요.",
    "closing": "확인 감사합니다. 좋은 하루 보내세요.",
    "ai_wait": "잠시만요.",
    "ai_failed": "죄송합니다. 지금은 답변드리기 어려워서 담당자가 다시 연락드리겠습니다.",
    "wrong_person": "잘못 연락드려 죄송합니다. 좋은 하루 보내세요.",
    "ack": "네,",
}


def with_optional(manifest: dict) -> dict:
    """예전 음원팩에 선택 음원 칸이 없으면 빈 칸으로 채운 사본을 돌려준다."""
    clips = {k: dict(v) for k, v in (manifest.get("clips") or {}).items()}
    for k in OPTIONAL_KEYS:
        clips.setdefault(k, {"file": None, "text": DEFAULT_SCRIPTS[k]})
    return {**manifest, "clips": clips}


@dataclass
class PackCheck:
    dialable: bool
    problems: list[str]
    missing: list[str]


def default_manifest(pack_id: str, gender: str, speaker: str) -> dict:
    clips = {k: {"file": None, "text": v.replace("{speaker}", speaker)} for k, v in DEFAULT_SCRIPTS.items()}
    for k in DATE_PIECE_KEYS:
        n = int(k.split("_")[1])
        clips[k] = {"file": None, "text": f"{n}월" if k.startswith("month") else f"{n}일"}
    return {
        "id": pack_id, "gender": gender, "speaker_name": speaker, "version": 1,
        "company_name": "", "approved_by": None, "clips": clips,
    }


def check_pack(manifest: dict, base_dir: Path | None) -> PackCheck:
    problems: list[str] = []
    missing: list[str] = []
    if not manifest.get("speaker_name"):
        problems.append("안내 인물 이름이 비어 있습니다")
    if not manifest.get("company_name"):
        problems.append("회사명이 입력되지 않았습니다")
    if not manifest.get("approved_by"):
        problems.append("관리자 승인 기록이 없습니다")
    clips = manifest.get("clips") or {}
    for key in REQUIRED_KEYS + DATE_PIECE_KEYS:
        clip = clips.get(key)
        if not clip:
            missing.append(key)
            continue
        text = clip.get("text") or ""
        if any(mark in text for mark in PLACEHOLDER_MARKERS):
            problems.append(f"{key}: 자리표시자 문구가 남아 있습니다")
        f = clip.get("file")
        if not f:
            missing.append(key)
        elif base_dir is not None and not (base_dir / f).is_file():
            missing.append(key)
    if missing:
        problems.append(f"녹음 파일 없음 {len(missing)}개")
    return PackCheck(dialable=not problems, problems=problems, missing=missing)


def manifest_digest(manifest: dict) -> str:
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def confirm_date_sequence(month: int, day: int) -> list[str]:
    return [f"month_{month}", f"day_{day}", "confirm_date"]
