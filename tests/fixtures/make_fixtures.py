"""시험용 엑셀 픽스처 생성. 실제 개인정보가 아닌 합성 데이터만 사용한다.

실행: python tests/fixtures/make_fixtures.py
"""

from __future__ import annotations

import io
import zipfile
from datetime import date
from pathlib import Path

from openpyxl import Workbook

HERE = Path(__file__).resolve().parent

HEADERS = ["간병인 번호", "이름", "연락처", "근무확정일", "비고", "지역"]
ROWS = [
    ["00123", "김하나", "010-1000-0001", date(2026, 10, 20), "앞자리0 번호", "서울"],
    ["00124", "이두리", "010-1000-0002", "2026.11.02", "", "고양"],
    ["00125", "박세나", 1010000003, None, "숫자로 저장된 연락처", "인천"],   # 앞자리 0 손실 → 오류
    ["00126", "", "010-1000-0004", None, "이름 없음", ""],                # 이름 없음 → 오류
    [None, None, None, None, None, None],                                  # 빈 행(보존)
    ["00127", "최다섯", "010-1000-0005", "다음주", "", "서울"],            # 날짜 오류
    ["00128", "정여섯", "010-1000-0006", date(2026, 10, 30), "", "부천"],
    ["00128", "정여섯", "010-1000-0006", date(2026, 10, 30), "중복 행", "부천"],  # 같은 번호·같은 연락처 → 중복 경고
    ["00129", "한일곱", "010-1000-0007", None, "", "서울"],
    ["00130", "한여덟", "010-1000-0007", None, "같은 연락처 공유", "서울"],  # 연락처 공유 → 검수
    ["00131", "오아홉", "010-1000-0009", None, "", "수원"],
    ["00132", "윤열", "02-555-0010", date(2026, 11, 15), "유선", "서울"],
]


def roster_bytes(rows=ROWS, title: bool = True) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "명부"
    r = 1
    if title:
        ws.cell(1, 1, "간병사 명부(합성 시험 데이터)")
        r = 3
    for j, h in enumerate(HEADERS, 1):
        ws.cell(r, j, h)
    for i, row in enumerate(rows, r + 1):
        for j, v in enumerate(row, 1):
            c = ws.cell(i, j, v)
            if isinstance(v, date):
                c.number_format = "yyyy-mm-dd"
    ws.cell(r + 1, 7, "=1+1")  # 수식 셀(값 기준 내보내기 확인용, 머리글 없는 추가 열)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def clean_roster_bytes() -> bytes:
    rows = [r for r in ROWS if r[0] in ("00123", "00124", "00128", "00131", "00132") and r[4] != "중복 행"]
    return roster_bytes(rows)


def macro_bytes() -> bytes:
    src = io.BytesIO(roster_bytes())
    out = io.BytesIO()
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(out, "w") as zout:
        for item in zin.infolist():
            zout.writestr(item, zin.read(item.filename))
        zout.writestr("xl/vbaProject.bin", b"\x00fake-vba")
    return out.getvalue()


def main() -> None:
    (HERE / "sample_roster.xlsx").write_bytes(roster_bytes())
    (HERE / "clean_roster.xlsx").write_bytes(clean_roster_bytes())
    (HERE / "macro_inside.xlsx").write_bytes(macro_bytes())
    (HERE / "corrupt.xlsx").write_bytes(b"PK\x03\x04broken-not-a-real-zip")
    (HERE / "encrypted.xlsx").write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512)
    print("픽스처 생성 완료:", sorted(p.name for p in HERE.glob("*.xlsx")))


if __name__ == "__main__":
    main()
