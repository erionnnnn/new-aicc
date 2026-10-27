"""aicc-transcribe 개인정보 가림·엑셀 작성 시험(음성 모델 없이)."""

from aicc_agent.transcribe import mask, write_xlsx


def test_mask_personal_info():
    t = mask("김영희 간병사님 번호 010-1234-5678 맞죠? 주민번호 800101-2345678, 계좌 110 123 456789")
    assert "김영희" not in t and "○○ 간병사님" in t
    assert "010-1234-5678" not in t and "[전화번호]" in t
    assert "800101-2345678" not in t and "[주민번호]" in t
    assert "456789" not in t
    assert mask("간병사님 안녕하세요") == "간병사님 안녕하세요"  # 호칭 자체는 남김
    assert mask("실장님이 연락드릴게요") == "실장님이 연락드릴게요"
    assert "[이메일]" in mask("abc@naver.com 으로")


def test_write_xlsx(tmp_path):
    from openpyxl import load_workbook
    out = tmp_path / "t.xlsx"
    write_xlsx(out, [{"id": "C0001", "date": "2026-10-27 10:00", "duration_s": 12.0, "mode": "한 채널(화자 구분 없음)",
                      "rows": [{"start": 0.0, "end": 1.2, "speaker": "", "text": "여보세요"}]}])
    wb = load_workbook(out)
    assert wb.sheetnames == ["통화목록", "발화", "안내"]
    assert wb["발화"]["E2"].value == "여보세요"
