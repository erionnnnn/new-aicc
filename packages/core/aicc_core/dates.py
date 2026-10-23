"""한국어 통화 발화에서 날짜를 추출한다.

설계 05 날짜 예시, 11 날짜 판정 세트를 기준으로 한다.
- 결과는 항상 '후보'다. 확정은 상태기계의 재확인 + 명시적 동의로만 이뤄진다.
- 기준일(reference)은 한국시간 통화일이다. 호출자가 Asia/Seoul 날짜를 넘긴다.
- '월말쯤', '다음 주' 같은 표현은 RANGE 로 남기고 날짜를 추정하지 않는다.
- '25일까지 근무'만으로 26일 가능을 추정하지 않는다(역할 END 만 부여).
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

WEEKDAYS = "월화수목금토일"


@dataclass(frozen=True)
class DateMention:
    kind: str  # DATE | RANGE | ANYTIME | UNKNOWN
    value: date | None = None
    range_start: date | None = None
    range_end: date | None = None
    role: str | None = None  # END(까지) | START(부터·가능) | None
    span: tuple[int, int] = (0, 0)
    source: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)


# ---------- 한자어 숫자 → 아라비아 숫자 ----------
_SINO = {"일": 1, "이": 2, "삼": 3, "사": 4, "오": 5, "육": 6, "륙": 6, "칠": 7, "팔": 8, "구": 9}
_MONTH_WORDS = {
    "십이월": "12월", "십일월": "11월", "시월": "10월", "구월": "9월", "팔월": "8월", "칠월": "7월",
    "유월": "6월", "육월": "6월", "오월": "5월", "사월": "4월", "삼월": "3월", "이월": "2월", "일월": "1월",
}
_DAY_WORD = re.compile(r"((?:[이삼]?십)?[일이삼사오육칠팔구]?)일(?!요일)")


def _sino_to_int(token: str) -> int | None:
    if not token:
        return None
    total = 0
    if "십" in token:
        head, _, tail = token.partition("십")
        total += (_SINO.get(head, 0) if head else 1) * 10
        token = tail
    if token:
        if len(token) != 1 or token not in _SINO:
            return None
        total += _SINO[token]
    return total or None


def _normalize(text: str) -> str:
    t = text.replace(" ", " ")
    for word, rep in _MONTH_WORDS.items():
        t = re.sub(word + r"(?!요일)", rep, t)

    def day_repl(m: re.Match[str]) -> str:
        n = _sino_to_int(m.group(1))
        if n is None or not (1 <= n <= 31):
            return m.group(0)
        # '내일', '모레' 등은 숫자 앞 글자가 없으므로 그대로 둔다(group1 비어 있으면 None).
        return f"{n}일"

    # 앞 글자가 한글 숫자로만 이뤄진 경우에만 변환(단어 경계 근사)
    t = re.sub(r"(?<![가-힣])" + _DAY_WORD.pattern, day_repl, t)
    t = re.sub(r"(\d)\s+(월|일)", r"\1\2", t)
    return t


def _role_after(text: str, end: int) -> str | None:
    tail = text[end:end + 12]
    if re.match(r"\s*(?:쯤|정도|경)?\s*까지", tail):
        return "END"
    if re.match(r"\s*(?:쯤|정도|경)?\s*(?:부터|이후|후로|되|돼|가능|괜찮|할\s*수|하면|나갈|들어갈|일할|근무\s*가능)", tail):
        return "START"
    return None


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _resolve_month_day(ref: date, month: int, day: int) -> tuple[date | None, tuple[str, ...]]:
    cand = _safe_date(ref.year, month, day)
    if cand is None:
        return None, ("INVALID_DATE",)
    if cand < ref - timedelta(days=30):
        nxt = _safe_date(ref.year + 1, month, day)
        return nxt, ("ROLLED_TO_NEXT_YEAR",)
    return cand, ()


def _resolve_day_only(ref: date, day: int) -> tuple[date | None, tuple[str, ...]]:
    if day >= ref.day:
        cand = _safe_date(ref.year, ref.month, day)
        if cand:
            return cand, ("MONTH_INFERRED",)
        return None, ("INVALID_DATE",)
    y, m = (ref.year + 1, 1) if ref.month == 12 else (ref.year, ref.month + 1)
    cand = _safe_date(y, m, day)
    if cand:
        return cand, ("MONTH_INFERRED", "NEXT_MONTH")
    return None, ("INVALID_DATE",)


def _week_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _month_end(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("YMD", re.compile(r"(\d{4})\s*[년./-]\s*(\d{1,2})\s*[월./-]\s*(\d{1,2})\s*일?")),
    ("NEXTMONTH_DAY", re.compile(r"(다음\s*달|담달|다음\s*월|이번\s*달|이달)\s*(\d{1,2})일")),
    ("MD", re.compile(r"(\d{1,2})월\s*(\d{1,2})일")),
    ("SLASH", re.compile(r"(?<![\d/])(\d{1,2})\s*/\s*(\d{1,2})(?![\d/])")),
    ("WEEKDAY", re.compile(r"(다다음\s*주|다음\s*주|담주|이번\s*주|금주|요번\s*주)?\s*([월화수목금토일])요일")),
    ("REL_DAYS", re.compile(r"(\d{1,2})일\s*(?:후|뒤)")),
    ("DAY", re.compile(r"(?<![\d월])(\d{1,2})일(?!\s*(?:후|뒤|간|동안|요일))")),
    ("TODAY", re.compile(r"오늘")),
    ("TOMORROW", re.compile(r"내일")),
    ("DAY_AFTER", re.compile(r"모레|모래")),
    ("DAY_AFTER2", re.compile(r"글피")),
    ("MONTH_END", re.compile(r"(다음\s*달|담달|이번\s*달|이달)?\s*(월말|말일|말쯤|월\s*말)")),
    ("MONTH_START", re.compile(r"(다음\s*달|담달|이번\s*달|이달)?\s*(월초|초쯤|초순)")),
    ("MONTH_MID", re.compile(r"(다음\s*달|담달|이번\s*달|이달)?\s*(중순)")),
    ("NEXT_WEEK", re.compile(r"(다음\s*주|담주)(?!\s*[월화수목금토일]요일)")),
    ("ANYTIME", re.compile(r"언제든|언제라도|아무\s*때나|아무\s*때|상시|항상\s*가능")),
    ("UNKNOWN", re.compile(r"모르겠|몰라요|모르겠어|미정|정해지지|정해진\s*게\s*없|아직\s*몰|확실하지\s*않|잘\s*모르")),
]


def extract_dates(text: str, reference: date) -> list[DateMention]:
    """발화에서 날짜 후보를 위치 순으로 반환한다. 겹치는 부분은 앞선(더 구체적인) 패턴이 차지한다."""
    norm = _normalize(text)
    taken: list[tuple[int, int]] = []
    out: list[DateMention] = []

    def free(s: int, e: int) -> bool:
        return all(e <= a or s >= b for a, b in taken)

    for name, pat in _PATTERNS:
        for m in pat.finditer(norm):
            s, e = m.span()
            if not free(s, e):
                continue
            mention = _build(name, m, norm, reference)
            if mention is None:
                continue
            taken.append((s, e))
            out.append(mention)
    out.sort(key=lambda x: x.span[0])
    return out


def _with_month_scope(prefix: str | None, ref: date) -> tuple[int, int]:
    if prefix and re.match(r"다음|담", prefix):
        return (ref.year + 1, 1) if ref.month == 12 else (ref.year, ref.month + 1)
    return ref.year, ref.month


def _build(name: str, m: re.Match[str], norm: str, ref: date) -> DateMention | None:
    s, e = m.span()
    role = _role_after(norm, e)
    src = m.group(0)
    if name == "YMD":
        d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return DateMention("DATE", d, role=role, span=(s, e), source=src, notes=() if d else ("INVALID_DATE",)) if d else None
    if name == "MD" or name == "SLASH":
        mo, dy = int(m.group(1)), int(m.group(2))
        if not (1 <= mo <= 12 and 1 <= dy <= 31):
            return None
        d, notes = _resolve_month_day(ref, mo, dy)
        if d is None:
            return DateMention("UNKNOWN", None, role=role, span=(s, e), source=src, notes=notes)
        return DateMention("DATE", d, role=role, span=(s, e), source=src, notes=notes)
    if name == "NEXTMONTH_DAY":
        y, mo = _with_month_scope(m.group(1), ref)
        d = _safe_date(y, mo, int(m.group(2)))
        if d is None:
            return DateMention("UNKNOWN", None, role=role, span=(s, e), source=src, notes=("INVALID_DATE",))
        return DateMention("DATE", d, role=role, span=(s, e), source=src)
    if name == "DAY":
        dy = int(m.group(1))
        if not 1 <= dy <= 31:
            return None
        d, notes = _resolve_day_only(ref, dy)
        if d is None:
            return DateMention("UNKNOWN", None, role=role, span=(s, e), source=src, notes=notes)
        return DateMention("DATE", d, role=role, span=(s, e), source=src, notes=notes)
    if name == "REL_DAYS":
        n = int(m.group(1))
        return DateMention("DATE", ref + timedelta(days=n), role=role or "START", span=(s, e), source=src)
    if name == "WEEKDAY":
        prefix = (m.group(1) or "").replace(" ", "")
        wd = WEEKDAYS.index(m.group(2))
        mon = _week_monday(ref)
        if prefix in ("다음주", "담주"):
            d = mon + timedelta(days=7 + wd)
        elif prefix == "다다음주":
            d = mon + timedelta(days=14 + wd)
        elif prefix in ("이번주", "금주", "요번주"):
            d = mon + timedelta(days=wd)
            if d < ref:
                return DateMention("DATE", d, role=role, span=(s, e), source=src, notes=("PAST_DATE",))
        else:
            delta = (wd - ref.weekday()) % 7 or 7
            d = ref + timedelta(days=delta)
        return DateMention("DATE", d, role=role, span=(s, e), source=src)
    if name == "TODAY":
        return DateMention("DATE", ref, role=role, span=(s, e), source=src)
    if name == "TOMORROW":
        return DateMention("DATE", ref + timedelta(days=1), role=role, span=(s, e), source=src)
    if name == "DAY_AFTER":
        return DateMention("DATE", ref + timedelta(days=2), role=role, span=(s, e), source=src)
    if name == "DAY_AFTER2":
        return DateMention("DATE", ref + timedelta(days=3), role=role, span=(s, e), source=src)
    if name in ("MONTH_END", "MONTH_START", "MONTH_MID"):
        y, mo = _with_month_scope(m.group(1), ref)
        if name == "MONTH_END":
            a, b = date(y, mo, 25), _month_end(y, mo)
        elif name == "MONTH_START":
            if not m.group(1) and ref.day > 10:
                y, mo = (ref.year + 1, 1) if ref.month == 12 else (ref.year, ref.month + 1)
            a, b = date(y, mo, 1), date(y, mo, 10)
        else:
            a, b = date(y, mo, 11), date(y, mo, 20)
        return DateMention("RANGE", None, range_start=a, range_end=b, role=role, span=(s, e), source=src)
    if name == "NEXT_WEEK":
        mon = _week_monday(ref) + timedelta(days=7)
        return DateMention("RANGE", None, range_start=mon, range_end=mon + timedelta(days=6), role=role,
                           span=(s, e), source=src)
    if name == "ANYTIME":
        return DateMention("ANYTIME", None, role="START", span=(s, e), source=src)
    if name == "UNKNOWN":
        return DateMention("UNKNOWN", None, role=role, span=(s, e), source=src)
    return None


def korean_date_phrase(d: date) -> str:
    return f"{d.month}월 {d.day}일"
