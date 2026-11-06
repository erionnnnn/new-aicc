"""엑셀 읽기·검증·내보내기(설계 04 업로드 처리 상세, 엑셀 출력 규칙).

원칙
- xlsx 만 허용. xlsm·매크로 포함·암호화·구형 xls·손상 파일은 거절한다.
- 셀 값은 '값 기준'으로 읽는다(data_only). 수식·외부 링크는 실행하거나 복제하지 않는다.
- 원본 행 순서, 기존 값, 추가 열을 그대로 보존한다. 문자열 셀의 앞자리 0은 문자열로 유지한다.
- 숫자로 저장돼 앞자리 0이 사라진 연락처는 복원하지 않고 오류로 표시한다.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

from aicc_core.phone import mask_phone, normalize_phone
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS = 10_000
REQUIRED_FIELDS = ("external_no", "name", "phone", "date")
FIELD_LABELS = {"external_no": "간병인 번호", "name": "이름", "phone": "연락처", "date": "근무확정일"}
SYNONYMS = {
    "external_no": ("간병인번호", "간병인 번호", "간병사번호", "간병사 번호", "번호", "id", "코드", "회원번호"),
    "name": ("이름", "성명", "간병인", "간병사", "간병인명", "간병사명", "name"),
    "phone": ("연락처", "전화번호", "휴대폰", "휴대전화", "핸드폰", "phone", "전화", "휴대폰번호"),
    "date": ("근무확정일", "근무 확정일", "근무가능일", "다음근무가능일", "가능일", "확정일"),
}
DATE_SEMANTICS = ("NEXT_AVAILABLE", "ASSIGNMENT_CONFIRMED")
ADDED_COLUMNS = ("현재근무상태", "근무종료일", "다음근무가능일", "확인상태", "통화결과", "연락거부",
                 "마지막통화시각", "검수메모", "데이터구분")

ERROR_TEXT = {
    "EXTERNAL_NO_EMPTY": "간병인 번호가 비어 있습니다",
    "NAME_EMPTY": "이름이 비어 있습니다",
    "PHONE_EMPTY": "연락처가 비어 있습니다",
    "PHONE_INVALID": "연락처 형식이 올바르지 않습니다",
    "PHONE_LEADING_ZERO_LOST": "연락처가 숫자로 저장돼 앞자리 0이 사라졌습니다(자동 복원하지 않음)",
    "PHONE_NOT_PERSONAL": "대표번호 등 개인 연락처가 아닌 번호입니다",
    "DATE_INVALID": "근무확정일을 날짜로 읽을 수 없습니다",
    "DUP_EXTERNAL_NO_CONFLICT": "같은 간병인 번호에 서로 다른 연락처가 있습니다(검수 필요)",
    "DUP_ROW": "같은 간병인 번호가 이미 앞 행에 있습니다(중복 발신 방지)",
    "SHARED_PHONE": "같은 연락처를 여러 간병인이 쓰고 있습니다(확인 후 발신)",
    "BLOCKED": "연락거부 번호입니다(발신 제외)",
    "DB_DATE_DIFFERS": "통화로 확인된 기존 일정과 파일 날짜가 달라 기존 일정을 유지했습니다",
}


class ExcelRejected(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ParsedSheet:
    sheet_name: str
    header_row: int  # 1-based
    headers: list[str]
    rows: list[list[Any]]  # 모든 행(머리글 이전 행 포함), JSON 직렬화 가능한 값
    width: int


@dataclass
class RowResult:
    row_no: int
    kind: str  # PREAMBLE | HEADER | DATA | EMPTY
    external_no: str | None = None
    name: str | None = None
    phone: str | None = None
    phone_raw: Any = None
    date_value: date | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def dialable(self) -> bool:
        return self.kind == "DATA" and not self.errors and not any(
            w in ("SHARED_PHONE", "BLOCKED", "DUP_ROW") for w in self.warnings)


# ---------- 셀 값 직렬화 ----------
def cell_to_json(v: Any) -> Any:
    if isinstance(v, datetime):
        return {"$dt": v.isoformat()}
    if isinstance(v, date):
        return {"$date": v.isoformat()}
    if isinstance(v, time):
        return {"$time": v.isoformat()}
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


def json_to_cell(v: Any) -> Any:
    if isinstance(v, dict):
        if "$dt" in v:
            return datetime.fromisoformat(v["$dt"])
        if "$date" in v:
            return date.fromisoformat(v["$date"])
        if "$time" in v:
            return time.fromisoformat(v["$time"])
    return v


# ---------- 파일 검사 ----------
def inspect_file(name: str, data: bytes) -> None:
    lower = name.lower()
    if len(data) > MAX_BYTES:
        raise ExcelRejected("FILE_TOO_LARGE", "파일이 10MB를 넘습니다")
    if lower.endswith((".xlsm", ".xlsb", ".xltm")):
        raise ExcelRejected("MACRO_NOT_ALLOWED", "매크로 포함 형식(xlsm 등)은 업로드할 수 없습니다")
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        raise ExcelRejected("ENCRYPTED_OR_LEGACY", "암호화되었거나 구형(xls) 파일입니다. 암호를 풀고 xlsx로 저장해 주세요")
    if not lower.endswith(".xlsx"):
        raise ExcelRejected("UNSUPPORTED_TYPE", "xlsx 파일만 업로드할 수 있습니다")
    if data[:2] != b"PK":
        raise ExcelRejected("CORRUPT_FILE", "엑셀 파일을 읽을 수 없습니다(손상 또는 형식 불일치)")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = set(z.namelist())
            if any(n.lower().endswith("vbaproject.bin") for n in names):
                raise ExcelRejected("MACRO_NOT_ALLOWED", "매크로가 포함된 파일은 업로드할 수 없습니다")
            ct = z.read("[Content_Types].xml").decode("utf-8", "ignore") if "[Content_Types].xml" in names else ""
            if "macroEnabled" in ct:
                raise ExcelRejected("MACRO_NOT_ALLOWED", "매크로 사용 문서는 업로드할 수 없습니다")
            if "xl/workbook.xml" not in names:
                raise ExcelRejected("CORRUPT_FILE", "엑셀 통합문서 구조가 없습니다")
    except zipfile.BadZipFile as e:
        raise ExcelRejected("CORRUPT_FILE", "엑셀 파일을 읽을 수 없습니다(손상)") from e


def parse_workbook(name: str, data: bytes) -> ParsedSheet:
    inspect_file(name, data)
    try:
        wb = load_workbook(io.BytesIO(data), data_only=True, read_only=False)
    except Exception as e:  # noqa: BLE001 - openpyxl 은 다양한 예외를 던진다
        raise ExcelRejected("CORRUPT_FILE", "엑셀 파일을 읽을 수 없습니다") from e
    ws = wb.worksheets[0]
    rows: list[list[Any]] = []
    width = 0
    for r in ws.iter_rows(values_only=True):
        vals = [cell_to_json(v) for v in r]
        while vals and (vals[-1] is None or vals[-1] == ""):
            vals.pop()
        rows.append(vals)
        width = max(width, len(vals))
    while rows and not rows[-1]:
        rows.pop()
    if not rows:
        raise ExcelRejected("EMPTY_FILE", "내용이 없는 파일입니다")
    header_idx = None
    for i, r in enumerate(rows[:15]):
        texts = [v for v in r if isinstance(v, str) and v.strip()]
        if len(texts) >= 2:
            header_idx = i
            break
    if header_idx is None:
        raise ExcelRejected("HEADER_NOT_FOUND", "머리글 행을 찾지 못했습니다")
    if len(rows) - header_idx - 1 > MAX_ROWS:
        raise ExcelRejected("TOO_MANY_ROWS", f"데이터 행이 {MAX_ROWS:,}행을 넘습니다")
    headers = []
    for j in range(width):
        v = rows[header_idx][j] if j < len(rows[header_idx]) else None
        headers.append(str(v).strip() if v not in (None, "") else f"열{j + 1}")
    return ParsedSheet(ws.title, header_idx + 1, headers, rows, width)


def suggest_mapping(headers: list[str]) -> dict[str, int | None]:
    norm = [re.sub(r"\s+", "", h).lower() for h in headers]
    out: dict[str, int | None] = {}
    used: set[int] = set()
    for f in ("phone", "date", "external_no", "name"):
        idx = None
        for syn in SYNONYMS[f]:
            s = re.sub(r"\s+", "", syn).lower()
            for j, h in enumerate(norm):
                if j not in used and h == s:
                    idx = j
                    break
            if idx is not None:
                break
        if idx is None:
            for syn in SYNONYMS[f]:
                s = re.sub(r"\s+", "", syn).lower()
                for j, h in enumerate(norm):
                    if j not in used and s in h and len(s) >= 2:
                        idx = j
                        break
                if idx is not None:
                    break
        out[f] = idx
        if idx is not None:
            used.add(idx)
    return out


def _to_text(v: Any) -> str | None:
    v = json_to_cell(v)
    if v is None:
        return None
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    s = str(v).strip()
    return s or None


def parse_date_cell(v: Any) -> tuple[date | None, bool]:
    """(값, 오류여부). 빈 셀은 (None, False)."""
    v = json_to_cell(v)
    if v is None or (isinstance(v, str) and not v.strip()):
        return None, False
    if isinstance(v, datetime):
        return v.date(), False
    if isinstance(v, date):
        return v, False
    if isinstance(v, str):
        s = v.strip()
        m = re.fullmatch(r"(\d{4})\s*[-./년]\s*(\d{1,2})\s*[-./월]\s*(\d{1,2})\s*일?", s) or \
            re.fullmatch(r"(\d{4})(\d{2})(\d{2})", s)
        if m:
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3))), False
            except ValueError:
                return None, True
    return None, True


def validate_rows(parsed: ParsedSheet, mapping: dict[str, int | None]) -> list[RowResult]:
    missing = [f for f in REQUIRED_FIELDS if mapping.get(f) is None]
    if missing:
        raise ExcelRejected("MAPPING_INCOMPLETE", "필수 열 매핑이 비어 있습니다: " +
                            ", ".join(FIELD_LABELS[f] for f in missing))
    cols = [mapping[f] for f in REQUIRED_FIELDS]
    if len(set(cols)) != len(cols):
        raise ExcelRejected("MAPPING_DUPLICATE", "같은 열을 두 항목에 매핑할 수 없습니다")
    results: list[RowResult] = []
    for i, row in enumerate(parsed.rows):
        row_no = i + 1
        if row_no < parsed.header_row:
            results.append(RowResult(row_no, "PREAMBLE"))
            continue
        if row_no == parsed.header_row:
            results.append(RowResult(row_no, "HEADER"))
            continue

        def get(f: str, row=row) -> Any:
            j = mapping[f]
            return row[j] if j is not None and j < len(row) else None

        if all(v in (None, "") for v in row):
            results.append(RowResult(row_no, "EMPTY"))
            continue
        rr = RowResult(row_no, "DATA")
        rr.external_no = _to_text(get("external_no"))
        rr.name = _to_text(get("name"))
        rr.phone_raw = get("phone")
        if not rr.external_no:
            rr.errors.append("EXTERNAL_NO_EMPTY")
        if not rr.name:
            rr.errors.append("NAME_EMPTY")
        pr = normalize_phone(json_to_cell(rr.phone_raw))
        if pr.ok:
            rr.phone = pr.normalized
        else:
            rr.errors.append(pr.error or "PHONE_INVALID")
        d, bad = parse_date_cell(get("date"))
        rr.date_value = d
        if bad:
            rr.errors.append("DATE_INVALID")
        results.append(rr)

    # 중복 검사
    by_no: dict[str, list[RowResult]] = {}
    by_phone: dict[str, set[str]] = {}
    for rr in results:
        if rr.kind != "DATA":
            continue
        if rr.external_no:
            by_no.setdefault(rr.external_no, []).append(rr)
        if rr.phone and rr.external_no:
            by_phone.setdefault(rr.phone, set()).add(rr.external_no)
    for _no, group in by_no.items():
        if len(group) < 2:
            continue
        phones = {g.phone for g in group if g.phone}
        if len(phones) > 1:
            for g in group:
                g.errors.append("DUP_EXTERNAL_NO_CONFLICT")
        else:
            for g in group[1:]:
                g.warnings.append("DUP_ROW")
    for rr in results:
        if rr.kind == "DATA" and rr.phone and len(by_phone.get(rr.phone, ())) > 1:
            rr.warnings.append("SHARED_PHONE")
    return results


# ---------- 내보내기 ----------
@dataclass
class ExportRow:
    row_no: int
    kind: str
    raw: list[Any]
    date_override: date | None = None  # 근무확정일 열에 반영할 확정값(None 이면 원본 유지)
    added: dict[str, Any] = field(default_factory=dict)


_HEADER_FILL = PatternFill("solid", fgColor="EEF2F6")
_ADDED_FILL = PatternFill("solid", fgColor="FFF4E5")


def build_export(*, sheet_name: str, header_row: int, headers: list[str], width: int, date_col: int,
                 rows: list[ExportRow], calls: list[dict], changes: list[dict], errors: list[dict],
                 summary: dict, in_progress: bool, is_mock: bool) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "명부"
    added_start = width + 1
    for r in rows:
        excel_row = r.row_no
        if r.kind == "HEADER":
            for j in range(width):
                c = ws.cell(excel_row, j + 1, headers[j] if j < len(headers) else None)
                c.font = Font(bold=True)
                c.fill = _HEADER_FILL
            for k, name in enumerate(ADDED_COLUMNS):
                c = ws.cell(excel_row, added_start + k, name)
                c.font = Font(bold=True)
                c.fill = _ADDED_FILL
            continue
        for j, v in enumerate(r.raw):
            val = json_to_cell(v)
            if r.kind == "DATA" and j == date_col and r.date_override is not None:
                val = r.date_override
            c = ws.cell(excel_row, j + 1, val)
            if isinstance(val, str):
                c.number_format = "@"  # 앞자리 0 보존
            elif isinstance(val, datetime):
                c.number_format = "yyyy-mm-dd hh:mm"
            elif isinstance(val, date):
                c.number_format = "yyyy-mm-dd"
        if r.kind == "DATA":
            for k, name in enumerate(ADDED_COLUMNS):
                val = r.added.get(name)
                c = ws.cell(excel_row, added_start + k, val)
                if isinstance(val, date) and not isinstance(val, datetime):
                    c.number_format = "yyyy-mm-dd"
                elif isinstance(val, str):
                    c.number_format = "@"
    ws.freeze_panes = ws.cell(header_row + 1, 1)
    for j in range(1, added_start + len(ADDED_COLUMNS)):
        ws.column_dimensions[get_column_letter(j)].width = 16

    def table(title: str, cols: list[str], items: list[dict]) -> None:
        sh = wb.create_sheet(title)
        for j, col in enumerate(cols, 1):
            c = sh.cell(1, j, col)
            c.font = Font(bold=True)
            c.fill = _HEADER_FILL
            sh.column_dimensions[get_column_letter(j)].width = 18
        for i, it in enumerate(items, 2):
            for j, col in enumerate(cols, 1):
                v = it.get(col)
                c = sh.cell(i, j, v)
                if isinstance(v, str):
                    c.number_format = "@"
                    c.alignment = Alignment(wrap_text=len(v) > 40)
                elif isinstance(v, date) and not isinstance(v, datetime):
                    c.number_format = "yyyy-mm-dd"
        sh.freeze_panes = "A2"

    table("통화이력", ["통화ID", "간병인 번호", "이름", "연락처(마스킹)", "시작(KST)", "응답(KST)", "종료(KST)",
                   "통화결과", "근무상태", "종료일 후보", "가능일 후보", "확인상태", "검수사유", "녹음상태", "데이터구분"], calls)
    table("변경이력", ["간병인 번호", "이름", "항목", "기존 날짜", "새 날짜", "근거 발화", "통화ID", "확인자 유형",
                   "확인자", "사유", "시각(KST)"], changes)
    table("오류행", ["행 번호", "오류코드", "설명", "간병인 번호", "이름", "연락처(마스킹)", "근무확정일 원본"], errors)
    s = wb.create_sheet("요약")
    s.cell(1, 1, "항목").font = Font(bold=True)
    s.cell(1, 2, "값").font = Font(bold=True)
    items = [("상태", "진행 중 결과(최종 아님)" if in_progress else "최종 결과"),
             ("데이터구분", "MOCK 시험 데이터 — 실제 통화 결과 아님" if is_mock else "실통화")]
    items += list(summary.items())
    for i, (k, v) in enumerate(items, 2):
        s.cell(i, 1, k)
        s.cell(i, 2, v)
    s.column_dimensions["A"].width = 28
    s.column_dimensions["B"].width = 40
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def error_rows(results: list[RowResult], parsed_rows: list[list[Any]], date_col: int) -> list[dict]:
    out = []
    for rr in results:
        codes = rr.errors + [w for w in rr.warnings if w in ("SHARED_PHONE", "BLOCKED", "DUP_ROW", "DB_DATE_DIFFERS")]
        for code in codes:
            raw = parsed_rows[rr.row_no - 1] if rr.row_no - 1 < len(parsed_rows) else []
            dv = raw[date_col] if date_col < len(raw) else None
            out.append({
                "행 번호": rr.row_no, "오류코드": code, "설명": ERROR_TEXT.get(code, code),
                "간병인 번호": rr.external_no, "이름": rr.name, "연락처(마스킹)": mask_phone(rr.phone),
                "근무확정일 원본": str(json_to_cell(dv)) if dv is not None else None,
            })
    return out
