"""한국 전화번호 정규화.

원칙(설계 04 업로드 처리 상세):
- 원본 문자열은 호출자가 별도로 보존한다.
- 앞자리 0이 이미 손실된 값(예: 1012345678)은 임의 복원하지 않고 오류로 돌려준다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_MOBILE = re.compile(r"^01[016789]\d{7,8}$")
_SEOUL = re.compile(r"^02\d{7,8}$")
_AREA = re.compile(r"^0[3-6][1-5]\d{7,8}$")
_INTERNET = re.compile(r"^070\d{8}$")
_REP = re.compile(r"^1[5-9]\d{6}$")  # 대표번호 1588-xxxx 등 — 발신 대상 아님


@dataclass(frozen=True)
class PhoneResult:
    normalized: str | None
    error: str | None  # None 이면 정상

    @property
    def ok(self) -> bool:
        return self.error is None


def normalize_phone(value: object) -> PhoneResult:
    if value is None:
        return PhoneResult(None, "PHONE_EMPTY")
    if isinstance(value, bool):
        return PhoneResult(None, "PHONE_INVALID")
    if isinstance(value, (int, float)):
        # 엑셀 숫자 셀: 앞자리 0이 이미 사라졌다. 복원하지 않는다.
        return PhoneResult(None, "PHONE_LEADING_ZERO_LOST")
    text = str(value).strip()
    if not text:
        return PhoneResult(None, "PHONE_EMPTY")
    digits = re.sub(r"[^\d+]", "", text)
    if digits.startswith("+82"):
        digits = "0" + digits[3:].lstrip("0")
    elif digits.startswith("82") and len(digits) >= 11 and not digits.startswith("0"):
        digits = "0" + digits[2:].lstrip("0")
    digits = digits.replace("+", "")
    if not digits:
        return PhoneResult(None, "PHONE_INVALID")
    if _REP.match(digits):
        return PhoneResult(None, "PHONE_NOT_PERSONAL")
    if not digits.startswith("0"):
        if re.match(r"^1[016789]\d{7,8}$", digits) or re.match(r"^2\d{7,8}$", digits):
            return PhoneResult(None, "PHONE_LEADING_ZERO_LOST")
        return PhoneResult(None, "PHONE_INVALID")
    if _MOBILE.match(digits) or _SEOUL.match(digits) or _AREA.match(digits) or _INTERNET.match(digits):
        return PhoneResult(digits, None)
    return PhoneResult(None, "PHONE_INVALID")


def mask_phone(normalized: str | None) -> str:
    """화면·로그용 마스킹. 010-****-5678"""
    if not normalized:
        return ""
    if len(normalized) >= 10:
        return f"{normalized[:3]}-****-{normalized[-4:]}"
    return "***" + normalized[-2:]


def format_phone(normalized: str) -> str:
    n = normalized
    if n.startswith("02"):
        return f"{n[:2]}-{n[2:-4]}-{n[-4:]}"
    return f"{n[:3]}-{n[3:-4]}-{n[-4:]}"
