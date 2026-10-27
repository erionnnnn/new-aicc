"""T01·T02·T03·T14·T18 (서버 API, 실기기 아님)."""

from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path

from conftest import make_user, upload_commit
from make_fixtures import clean_roster_bytes, macro_bytes, roster_bytes
from openpyxl import load_workbook

FIX = Path(__file__).parent / "fixtures"


def _upload(api, name, data):
    return api.c.post("/api/v1/imports", files={"file": (name, data)}, headers=api._h())


def test_t01_upload_mapping_and_leading_zero(admin):
    r = _upload(admin, "명부.xlsx", roster_bytes())
    assert r.status_code == 201, r.text
    up = r.json()
    assert up["header_row"] == 3
    assert up["suggested_mapping"] == {"phone": 2, "date": 3, "external_no": 0, "name": 1}
    assert up["preview"][0]["external_no"] == "00123"  # 앞자리 0 유지
    assert "1000" not in str(up["preview"])  # 미리보기 연락처 마스킹


def test_t02_rejects_bad_files(admin):
    cases = {
        "a.xlsm": (roster_bytes(), "MACRO_NOT_ALLOWED"),
        "b.xlsx": (macro_bytes(), "MACRO_NOT_ALLOWED"),
        "c.xlsx": ((FIX / "corrupt.xlsx").read_bytes(), "CORRUPT_FILE"),
        "d.xlsx": ((FIX / "encrypted.xlsx").read_bytes(), "ENCRYPTED_OR_LEGACY"),
        "e.csv": (b"a,b\n1,2", "UNSUPPORTED_TYPE"),
    }
    for name, (data, code) in cases.items():
        r = _upload(admin, name, data)
        assert r.status_code == 422, name
        assert r.json()["error"]["code"] == code, name


def test_t02_row_validation_and_dial_exclusion(admin):
    res = upload_commit(admin, roster_bytes())
    v = res["validate"]
    codes = {(e["행 번호"], e["오류코드"]) for e in v["errors"]}
    # 행 번호는 엑셀 실제 행(머리글 3행)
    assert (6, "PHONE_LEADING_ZERO_LOST") in codes
    assert (7, "NAME_EMPTY") in codes
    assert (9, "DATE_INVALID") in codes
    assert (11, "DUP_ROW") in codes
    assert (12, "SHARED_PHONE") in codes and (13, "SHARED_PHONE") in codes
    assert v["data_rows"] == 11 and v["error_count"] == 3
    items = admin.get(f"/api/v1/caregivers?batch_id={res['batch_id']}").json()["items"]
    dialable = {i["external_no"] for i in items if i["dialable"]}
    assert dialable == {"00123", "00124", "00128", "00131", "00132"}


def test_date_semantics_required(admin):
    up = _upload(admin, "x.xlsx", clean_roster_bytes()).json()
    r = admin.post(f"/api/v1/imports/{up['import_id']}/validate", json={"mapping": up["suggested_mapping"]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "DATE_SEMANTICS_REQUIRED"


def test_t03_export_preserves_rows_values_and_sheets(admin):
    res = upload_commit(admin, roster_bytes())
    r = admin.post("/api/v1/exports", json={"batch_id": res["batch_id"]}, idem=True)
    assert r.status_code == 202, r.text
    eid = r.json()["export_id"]
    d = admin.get(f"/api/v1/exports/{eid}/download")
    assert d.status_code == 200
    wb = load_workbook(io.BytesIO(d.content))
    assert wb.sheetnames == ["명부", "통화이력", "변경이력", "오류행", "요약"]
    ws = wb["명부"]
    src = load_workbook(io.BytesIO(roster_bytes()), data_only=True)["명부"]
    # 원본 행 순서·값 보존(수식은 값 기준 — 캐시 값이 없으면 빈 셀)
    for row in range(1, src.max_row + 1):
        for col in range(1, 7):
            a, b = src.cell(row, col).value, ws.cell(row, col).value
            if isinstance(a, datetime):
                a = a.date()
            if isinstance(b, datetime):
                b = b.date()
            assert a == b, (row, col, a, b)
    assert ws.cell(4, 1).value == "00123" and ws.cell(4, 1).number_format == "@"
    assert ws.cell(3, 7).value == "현재근무상태"
    assert ws.cell(8, 1).value is None  # 빈 행 유지
    assert ws.cell(4, 10).value == "미통화(기존값 유지)"
    errs = wb["오류행"]
    assert errs.max_row >= 7
    summary = {wb["요약"].cell(i, 1).value: wb["요약"].cell(i, 2).value for i in range(1, wb["요약"].max_row + 1)}
    assert summary["데이터 행 수"] == 11 and summary["오류 행 수"] == 3


def test_reupload_duplicate_warning_and_db_date_kept(admin):
    data = clean_roster_bytes()  # 같은 파일(바이트 동일) — 생성 시각이 바뀌지 않게 한 번만 만든다
    upload_commit(admin, data)
    r = _upload(admin, "same.xlsx", data)
    assert r.json()["duplicate_of"] is not None


def test_t14_block_survives_reupload(admin):
    res = upload_commit(admin, clean_roster_bytes())
    items = admin.get(f"/api/v1/caregivers?batch_id={res['batch_id']}").json()["items"]
    target = next(i for i in items if i["external_no"] == "00124")
    b = admin.post("/api/v1/contact-blocks", json={"caregiver_id": target["caregiver_id"], "reason": "전화로 거부"})
    assert b.status_code == 201
    # 다른 간병인 번호로 같은 연락처를 재업로드해도 차단
    rows = [["99999", "새이름", "010-1000-0002", None, "", ""]]
    res2 = upload_commit(admin, roster_bytes(rows))
    assert res2["validate"]["blocked_count"] == 1
    items2 = admin.get(f"/api/v1/caregivers?batch_id={res2['batch_id']}").json()["items"]
    assert items2[0]["blocked"] and not items2[0]["dialable"]
    packs = admin.post("/api/v1/voice-packs/init").json()
    r = admin.post("/api/v1/campaigns", json={"batch_id": res2["batch_id"], "target_ids": [items2[0]["caregiver_id"]],
                                              "voice_pack_id": packs[0]["id"]}, idem=True)
    assert r.status_code == 422 and r.json()["error"]["code"] == "NO_DIALABLE_TARGETS"
    # 해제는 관리자만, 사유 필수, 기록은 남음
    op = make_user(admin, "op1", "operator")
    bid = admin.get("/api/v1/contact-blocks").json()[0]["id"]
    assert op.post(f"/api/v1/contact-blocks/{bid}/revoke", json={"reason": "본인 재동의 확인"}).status_code == 403
    assert admin.post(f"/api/v1/contact-blocks/{bid}/revoke", json={"reason": "짧음"}).status_code in (400, 422)
    assert admin.post(f"/api/v1/contact-blocks/{bid}/revoke", json={"reason": "본인 재동의 서면 확인"}).status_code == 200
    hist = admin.get("/api/v1/contact-blocks?include_revoked=true").json()
    assert hist[0]["revoked_at"] and hist[0]["revoke_reason"]


def test_t18_permissions_and_audit(admin):
    res = upload_commit(admin, clean_roster_bytes())
    reviewer = make_user(admin, "rev1", "reviewer")
    operator = make_user(admin, "op2", "operator")
    # 검수자는 업로드 불가
    r = reviewer.c.post("/api/v1/imports", files={"file": ("a.xlsx", clean_roster_bytes())}, headers=reviewer._h())
    assert r.status_code == 403
    # 운영자는 별도 권한 없이는 전체 내보내기 불가
    r = operator.post("/api/v1/exports", json={"batch_id": res["batch_id"], "scope": "all"}, idem=True)
    assert r.status_code == 403
    # 운영자는 설정 변경 불가
    assert operator.put("/api/v1/settings", json={"company_name": "X"}).status_code == 403
    # 운영자는 별도 권한 없이는 날짜 수정 불가
    cg = operator.get(f"/api/v1/caregivers?batch_id={res['batch_id']}").json()["items"][0]
    r = operator.patch(f"/api/v1/caregivers/{cg['caregiver_id']}/schedule",
                       json={"date": "2026-11-01", "expected_version": cg["version"], "reason": "확인"})
    assert r.status_code == 403
    logs = admin.get("/api/v1/audit").json()
    assert any(entry["action"] == "import.commit" for entry in logs)
    # 감사 로그·오류 응답에 전화번호 원문이 없어야 함
    assert "010-1000" not in str(logs) and "0101000" not in str(logs)


def test_manual_edit_requires_reason_and_version(admin):
    res = upload_commit(admin, clean_roster_bytes())
    rev = make_user(admin, "rev2", "reviewer")
    cg = rev.get(f"/api/v1/caregivers?batch_id={res['batch_id']}").json()["items"][0]
    url = f"/api/v1/caregivers/{cg['caregiver_id']}/schedule"
    assert rev.patch(url, json={"date": "2026-11-01", "expected_version": cg["version"]}).status_code == 400
    ok = rev.patch(url, json={"date": "2026-11-01", "expected_version": cg["version"], "reason": "전화 재확인"})
    assert ok.status_code == 200
    stale = rev.patch(url, json={"date": "2026-11-05", "expected_version": cg["version"], "reason": "재수정"})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "VERSION_CONFLICT"
    ch = rev.get(f"/api/v1/caregivers/{cg['caregiver_id']}/changes").json()
    assert ch[-1]["old_date"] == "2026-10-20" and ch[-1]["new_date"] == "2026-11-01"


def test_csrf_header_required_for_cookie_auth(app_client, admin):
    app_client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-pass-123"})
    r = app_client.post("/api/v1/voice-packs/init")
    assert r.status_code == 403 and r.json()["error"]["code"] == "CSRF_HEADER_REQUIRED"
    assert app_client.post("/api/v1/voice-packs/init", headers={"X-AICC": "1"}).status_code == 201


def test_voice_pack_auto_created_tts_and_approve(admin):
    packs = admin.get("/api/v1/voice-packs").json()  # 버튼 없이 자동 생성
    assert {p["key"] for p in packs} == {"female", "male"}
    r = admin.post("/api/v1/voice-packs/female/tts", json={"engine": "test"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "COMPANY_REQUIRED"
    admin.put("/api/v1/settings", json={"company_name": "좋은케어"})
    r = admin.post("/api/v1/voice-packs/female/tts", json={"engine": "test"})
    assert r.status_code == 200, r.text
    out = r.json()
    from aicc_core.voicepack import DATE_PIECE_KEYS, OPTIONAL_KEYS, REQUIRED_KEYS
    assert out["generated"] == len(REQUIRED_KEYS) + len(DATE_PIECE_KEYS) + len(OPTIONAL_KEYS) and out["missing_count"] == 0
    assert "좋은케어" in out["clips"]["opening"]["text"] and "[회사명]" not in out["clips"]["opening"]["text"]
    assert out["problems"] == ["관리자 승인 기록이 없습니다"]
    ok = admin.post("/api/v1/voice-packs/female/approve").json()
    assert ok["dialable"] is True
    assert admin.get("/api/v1/voice-packs/female/clips/opening").content[:4] == b"RIFF"
    # 남성 팩은 안내 이름이 없으면 생성 불가
    assert admin.post("/api/v1/voice-packs/male/tts", json={"engine": "test"}).json()["error"]["code"] == "SPEAKER_REQUIRED"


def test_clip_upload_converts_stereo_44k_wav(admin):
    import wave as _w
    admin.get("/api/v1/voice-packs")
    buf = io.BytesIO()
    with _w.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x10\x00\x10\x00" * 44100)
    r = admin.c.post("/api/v1/voice-packs/female/clips/closing", files={"file": ("c.wav", buf.getvalue())},
                     headers=admin._h())
    assert r.status_code == 200, r.text
    data = admin.get("/api/v1/voice-packs/female/clips/closing").content
    with _w.open(io.BytesIO(data)) as w:
        assert (w.getnchannels(), w.getframerate()) == (1, 16000)


def test_account_cli_list_and_reset(admin, monkeypatch, capsys):
    from aicc_api import admin_cli
    assert admin_cli.main(["list"]) == 0
    assert "admin" in capsys.readouterr().out
    pw = iter(["new-pass-12345", "new-pass-12345"])
    monkeypatch.setattr(admin_cli.getpass, "getpass", lambda prompt="": next(pw))
    assert admin_cli.main(["reset", "admin"]) == 0
    r = admin.c.post("/api/v1/auth/login", json={"username": "admin", "password": "new-pass-12345"})
    assert r.status_code == 200
    assert admin_cli.main(["reset", "nobody"]) == 1


def _wav(ms_segments, rate=24000):
    """[(ms, amp)] 구간을 이어 붙인 모노 WAV."""
    import io as _io
    import math as _m
    import struct as _st
    import wave as _w
    vals = []
    for ms, amp in ms_segments:
        n = rate * ms // 1000
        vals += [int(amp * _m.sin(2 * _m.pi * 220 * i / rate)) for i in range(n)]
    buf = _io.BytesIO()
    with _w.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(_st.pack(f"<{len(vals)}h", *vals))
    return buf.getvalue()


def test_import_legacy_recordings_zip(admin):
    import io as _io
    import zipfile

    from aicc_core.scenarios import LEGACY_MAP, SCENARIOS
    admin.get("/api/v1/voice-packs")
    admin.put("/api/v1/settings", json={"company_name": "좋은간병"})
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        codes = {c for c, _ in LEGACY_MAP.values()} | {s.code for s in SCENARIOS}
        for code in codes:
            body = _wav([(100, 0), (300, 8000), (250, 0), (900, 8000)]) if code == "ACK_CONTINUE" else _wav([(400, 6000)])
            z.writestr(f"recordings/{code}.wav", body)
            z.writestr(f"recordings/{code}_MALE.wav", _wav([(300, 6000)]))
        z.writestr("recordings/README.txt", "x")
    r = admin.c.post("/api/v1/voice-packs/female/import-legacy", files={"file": ("recordings.zip", buf.getvalue())},
                     headers=admin._h())
    assert r.status_code == 200, r.text
    out = r.json()
    assert "opening" in out["imported"] and "sc_ask_pay" in out["imported"] and "ack" in out["imported"]
    assert out["clips"]["opening"]["text"].startswith("안녕하세요, 간병사님")
    ack_ms = out["clips"]["ack"]["duration_ms"]
    assert 300 <= ack_ms <= 600  # 앞의 '네,'(0.3초)만 잘림, 뒤 문장은 제외
    assert out["clips"]["ask_working"]["has_file"] is False  # 녹음 없는 자리는 비어 있음(Gemini 로 채움)
    r = admin.c.post("/api/v1/voice-packs/male/import-legacy", files={"file": ("recordings.zip", buf.getvalue())},
                     headers=admin._h())
    assert r.status_code == 200 and "opening" in r.json()["imported"]
