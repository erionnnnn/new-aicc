"""T12·T13 날짜 판정 세트와 의도 분류 단위 시험."""

from datetime import date

import pytest
from aicc_core.dates import extract_dates
from aicc_core.intents import interpret
from aicc_core.phone import normalize_phone

REF = date(2026, 10, 24)  # 토요일


def first(text, ref=REF):
    ms = extract_dates(text, ref)
    return ms[0] if ms else None


@pytest.mark.parametrize("text,expected", [
    ("다음 주 월요일부터요", date(2026, 10, 26)),  # 설계 05 예시
    ("10월 26일부터 가능해요", date(2026, 10, 26)),
    ("26일부터 돼요", date(2026, 10, 26)),
    ("내일부터 가능해요", date(2026, 10, 25)),
    ("모레요", date(2026, 10, 26)),
    ("11월 3일", date(2026, 11, 3)),
    ("다음 달 3일이요", date(2026, 11, 3)),
    ("3일부터요", date(2026, 11, 3)),  # 지난 일자 → 다음 달
    ("이십육일부터요", date(2026, 10, 26)),
    ("시월 이십칠일", date(2026, 10, 27)),
    ("11/2 부터", date(2026, 11, 2)),
    ("수요일부터요", date(2026, 10, 28)),
    ("5일 후부터요", date(2026, 10, 29)),
])
def test_dates(text, expected):
    m = first(text)
    assert m is not None and m.kind == "DATE", text
    assert m.value == expected


def test_year_end_rollover():
    ref = date(2026, 12, 28)
    assert first("1월 3일부터요", ref).value == date(2027, 1, 3)
    assert first("5일부터요", ref).value == date(2027, 1, 5)


def test_end_and_start_roles():
    ms = extract_dates("오늘까지 근무하고 내일부터 가능해요", REF)
    assert [(m.value, m.role) for m in ms] == [(date(2026, 10, 24), "END"), (date(2026, 10, 25), "START")]
    m = first("25일까지 일해요")
    assert m.role == "END" and m.value == date(2026, 10, 25)


@pytest.mark.parametrize("text,kind", [
    ("월말쯤이요", "RANGE"), ("다음 주쯤", "RANGE"), ("언제든 가능해요", "ANYTIME"),
    ("잘 모르겠어요", "UNKNOWN"), ("다음 달 중순", "RANGE"),
])
def test_non_dates(text, kind):
    assert first(text).kind == kind


def test_no_false_dates():
    assert extract_dates("일요일은 쉬어요", REF)[0].kind == "DATE"  # 요일은 날짜
    assert extract_dates("일하고 있어요", REF) == []
    assert extract_dates("네 맞아요", REF) == []


@pytest.mark.parametrize("text,state,intent", [
    ("연락하지 마세요", "ASK_AVAILABLE_DATE", "REFUSAL"),
    ("다시는 전화하지 마세요", "OPENING", "REFUSAL"),
    ("번호 지워주세요", "CONFIRM_DATE", "REFUSAL"),
    ("오늘 안 돼요", "OPENING", "BUSY"),  # 영구 거부로 단정하지 않음
    ("지금 운전 중이라 나중에 전화주세요", "ASK_WORKING", "BUSY"),
    ("잘못 거셨어요", "OPENING", "WRONG_PERSON"),
    ("고객님의 전화기가 꺼져 있어 음성사서함으로", "OPENING", "VOICEMAIL"),
    ("네", "OPENING", "YES"),
    ("네 괜찮아요", "OPENING", "YES"),
    ("네", "ASK_WORKING", "WORKING"),
    ("아니요 쉬고 있어요", "ASK_WORKING", "NOT_WORKING"),
    ("지금 병원에서 일하고 있어요", "ASK_WORKING", "WORKING"),
    ("네 맞아요", "CONFIRM_DATE", "YES"),
    ("아니요", "CONFIRM_DATE", "NO"),
    ("아니 27일이요", "CONFIRM_DATE", "DATE"),
    ("급여는 얼마예요?", "ASK_AVAILABLE_DATE", "QUESTION"),
    ("누구세요?", "OPENING", "QUESTION"),
    ("뭐라고요?", "ASK_WORKING", "REPEAT"),
    ("지금은 바쁜데 26일부터 가능해요", "ASK_AVAILABLE_DATE", "DATE"),
])
def test_intents(text, state, intent):
    assert interpret(text, state, REF).intent == intent


@pytest.mark.parametrize("raw,norm,err", [
    ("010-1234-5678", "01012345678", None),
    ("+82 10-1234-5678", "01012345678", None),
    ("02-123-4567", "021234567", None),
    (1012345678, None, "PHONE_LEADING_ZERO_LOST"),
    ("1012345678", None, "PHONE_LEADING_ZERO_LOST"),
    ("", None, "PHONE_EMPTY"),
    ("1588-1234", None, "PHONE_NOT_PERSONAL"),
    ("010-12-34", None, "PHONE_INVALID"),
])
def test_phone(raw, norm, err):
    r = normalize_phone(raw)
    assert (r.normalized, r.error) == (norm, err)
