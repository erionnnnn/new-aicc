"""ARS 음원팩 규격과 실통화 가능 여부 검사.

설계 04 ARS 음원 구성의 필수 키에 아래 두 가지를 추가했다(문서화된 확장).
- wrong_person: 잘못 연결된 상대에게 사과하고 종료하는 안내(설계 03 간병사 항목의 '번호 확인 대상' 처리용)
- 날짜 조각 month_1..month_12, day_1..day_31: confirm_date 재확인 시 "10월" "26일" + confirm_date 순서로 이어 붙인다.

자리표시자 문구([회사명], PLACEHOLDER, TODO)가 남아 있거나 파일이 없으면 실통화 불가로 판정한다.
"""

from __future__ import annotations

import calendar as _cal
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .scenarios import SCENARIO_KEYS, SCENARIOS

REQUIRED_KEYS = (
    "opening", "ask_working", "ask_end_date", "ask_available_date", "confirm_date", "ask_repeat",
    "explain_role", "callback", "refusal_ack", "closing", "ai_wait", "ai_failed", "wrong_person",
)
# 선택 음원: 없어도 실통화 가능.
# - ack: 상대가 말을 마치면 인식 전에 바로 내는 짧은 맞장구("네,")
# - sc_*: 기존 녹음 상황 응답(급여·병원 질문, 선호 조건, 운전 중 등 — scenarios.py)
# 날짜 문장 통째 녹음(통화 중 생성·추정 없이 미리 녹음한 고정 문장, 2026-10-28 사용자 요청 '최대한 많은 스크립트')
#  - confirm_MM_DD : "10월 28일부터 근무 가능하신 게 맞으실까요?"  → 있으면 조각 이어 붙이기 대신 사용
#  - endack_MM_DD  : "11월 8일까지 근무하시는군요. 그다음에는 언제부터 근무 가능하실까요?"
_ALL_DAYS = [(m, d) for m in range(1, 13) for d in range(1, _cal.monthrange(2024, m)[1] + 1)]  # 윤년 기준 366일
FULL_CONFIRM_KEYS = tuple(f"confirm_{m:02d}_{d:02d}" for m, d in _ALL_DAYS)
END_ACK_KEYS = tuple(f"endack_{m:02d}_{d:02d}" for m, d in _ALL_DAYS)
# 같은 상황의 다른 표현(매번 같은 말만 반복하면 기계처럼 들림) — 녹음된 것 중 무작위로 고른다
VARIANT_TEXTS = {
    "opening__v2": "안녕하세요, 선생님. [회사명]입니다. 다음 근무 일정 여쭤보려고 연락드렸어요. 잠깐 통화 괜찮으세요?",
    "ask_available_date__v2": "네, 그럼 다음 근무는 언제부터 가능하세요?",
    "ask_available_date__v3": "네, 선생님. 언제부터 근무하실 수 있으세요?",
    "ask_end_date__v2": "아, 지금 일하고 계시는군요. 지금 근무는 언제쯤 끝나세요?",
    "ask_repeat__v2": "죄송해요, 제가 잘 못 들었어요. 한 번만 다시 말씀해 주시겠어요?",
    "ask_repeat__v3": "네? 다시 한 번 말씀해 주실 수 있을까요?",
    "closing__v2": "네, 확인 감사합니다. 말씀하신 날짜로 반영해둘게요. 좋은 하루 보내세요.",
    "callback__v2": "네, 괜찮습니다. 편하실 때 다시 연락드릴게요. 감사합니다.",
    "wrong_person__v2": "아, 죄송합니다. 번호를 잘못 알고 연락드렸네요. 좋은 하루 보내세요.",
    "ai_wait__v2": "네, 잠시만요.",
    "ai_wait__v3": "아, 잠깐만요. 확인해 볼게요.",
    "sc_work_end_date__v2": "네, 알겠습니다. 그 다음 근무는 언제부터 가능하세요?",
    "sc_silence_check__v2": "여보세요, 선생님? 제 목소리 잘 들리세요?",
    "sc_custom_reask_date__v2": "아, 그러시군요. 그럼 언제부터 가능하실까요?",
    "sc_why_call__v2": "선생님 다음 근무 가능하신 날을 미리 알아두면, 맞는 일감이 생길 때 바로 안내드릴 수 있어서요. "
                       "언제부터 가능하신지만 알려주시면 돼요.",
    "sc_custom_intro__v2": "네, 좋은간병 김태희 실장입니다. 근무 일정 확인차 연락드렸어요.",
    "sc_emotional_hard__v2": "아이고, 많이 힘드셨겠어요, 선생님. 정말 고생 많으셨어요. 다음 근무 가능하신 날 정해지시면 알려주세요.",
    "sc_unknown_schedule__v2": "네, 괜찮습니다. 일정 정해지시면 편하게 연락 주세요. 감사합니다.",
}
EXTRA_TEXTS = {
    "closing_anytime": "네, 언제든 근무 가능하신 걸로 반영해둘게요. 조건 맞는 일감이 생기면 연락드릴게요. 감사합니다.",
    "closing_review": "네, 말씀 감사합니다. 일정은 담당자가 한 번 더 확인해서 연락드릴게요. 좋은 하루 보내세요.",
}


def _md(key: str) -> tuple[int, int]:
    _p, mm, dd = key.split("_")
    return int(mm), int(dd)


def variants_of(key: str) -> tuple[str, ...]:
    return tuple(k for k in VARIANT_TEXTS if k.split("__")[0] == key)


OPTIONAL_KEYS = ("ack",) + SCENARIO_KEYS + tuple(EXTRA_TEXTS) + tuple(VARIANT_TEXTS) + FULL_CONFIRM_KEYS + END_ACK_KEYS
DATE_PIECE_KEYS = tuple(f"month_{m}" for m in range(1, 13)) + tuple(f"day_{d}" for d in range(1, 32))
PLACEHOLDER_MARKERS = ("[회사명]", "PLACEHOLDER", "TODO", "자리표시자", "[이름]")

OPENING_TEXT = "안녕하세요, 선생님. [회사명]입니다. 다음 근무 일정 확인차 연락드렸어요. 잠깐 통화 괜찮으세요?"
# 예전 오프닝 문구(이 표시가 있으면 '최신 문구로 맞추기'가 새 오프닝으로 바꾼다)
OLD_OPENING_MARKERS = ("자동 안내입니다", "근무 확인차 연락드렸습니다", "다음 근무 일정 확인차 연락드렸어요.")

# 기본 스크립트 초안(설계 03 오프닝 초안 기반). 실제 녹음 전 관리자가 승인해야 한다.
DEFAULT_SCRIPTS = {
    # 2026-10-30 사용자 결정: 10초 오프닝 → 5초 안팎, '자동 안내입니다' 문구 제외(물어보면 자동 안내라고 답함)
    "opening": OPENING_TEXT,
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
    **{sc.key: sc.say for sc in SCENARIOS},
    **EXTRA_TEXTS,
    **VARIANT_TEXTS,
    **{k: f"{_md(k)[0]}월 {_md(k)[1]}일부터 근무 가능하신 게 맞으실까요?" for k in FULL_CONFIRM_KEYS},
    **{k: f"{_md(k)[0]}월 {_md(k)[1]}일까지 근무하시는군요. 그다음에는 언제부터 근무 가능하실까요?" for k in END_ACK_KEYS},
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
