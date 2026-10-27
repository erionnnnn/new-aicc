"""aicc-transcribe — 회사 통화녹음을 이 PC 에서 글로 바꾸고 개인정보를 가린 엑셀을 만든다.

목적: ARS 문구·상황 응답을 실제 통화 말투에 맞게 다시 만들기 위한 분석 자료.
- 녹음 원본은 PC 밖으로 나가지 않는다. 결과 엑셀만 분석용으로 공유한다.
- 전화번호·주민번호·계좌/카드로 보이는 긴 숫자·이메일·'○○님/씨' 앞 이름을 가린다.
  (말로 읽은 숫자 '공일공…', 호칭 없는 이름은 다 못 가릴 수 있다 → 공유 전에 한 번 훑어볼 것)
- 파일 이름에도 전화번호가 들어 있는 경우가 많아(예: 01012345678_261027_1720.m4a) 이름 대신 일련 ID 를 쓴다.
- 좌우 채널이 다르게 녹음된 파일은 채널별로 따로 받아 적어 화자를 구분한다(어느 쪽이 상담원인지는 표시만).

사용 예
    aicc-transcribe "D:\\통화녹음"
    aicc-transcribe "D:\\통화녹음" --model medium --limit 50
중간에 멈춰도 다시 실행하면 이미 끝낸 파일은 건너뛴다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

AUDIO_EXT = {".m4a", ".mp3", ".wav", ".amr", ".3gp", ".aac", ".ogg", ".flac", ".opus", ".wma", ".mp4"}
PROMPT = ("간병사 근무 일정 확인 통화입니다. 네. 아니요. 여보세요. 지금 근무 중이에요. 다음 주 월요일부터 가능해요. "
          "11월 3일. 병원. 환자. 간병비. 일당. 실장님.")

_MASKS = [
    (re.compile(r"\d{6}\s*[-–]\s*[1-4]\d{6}"), "[주민번호]"),
    (re.compile(r"(?:\+?82[-\s]?)?0\d{1,2}[-\s.]?\d{3,4}[-\s.]?\d{4}"), "[전화번호]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[이메일]"),
    (re.compile(r"\d[\d\s-]{9,}\d"), "[긴숫자]"),
    # 호칭 앞 2~4자 한글 이름(실장님·간병사님·여사님·선생님·씨·님)
    (re.compile(r"(?<![가-힣])([가-힣]{2,4})(?=\s?(?:간병사님|간병인님|실장님|여사님|선생님|어머님|아버님|씨|님)(?![가-힣]))"),
     "○○"),
]
_KEEP_BEFORE_HONORIFIC = {"간병사", "간병인", "실장", "보호자", "환자", "고객", "선생", "사장", "팀장", "과장", "대리",
                          "기사", "어머", "아버", "할머", "할아버", "여사", "언니", "이모", "누구", "다들", "모두"}


def mask(text: str) -> str:
    for pat, rep in _MASKS:
        if rep == "○○":
            text = pat.sub(lambda m: m.group(1) if m.group(1) in _KEEP_BEFORE_HONORIFIC else "○○", text)
        else:
            text = pat.sub(rep, text)
    return text


def _channels_differ(left, right) -> bool:  # noqa: ANN001 - numpy 배열
    """좌우가 거의 같으면 모노로 본다(일반 휴대폰 녹음). 다르면 화자별 채널로 본다."""
    import numpy as np
    if left is None or right is None or len(left) == 0:
        return False
    n = min(len(left), len(right), 16000 * 120)
    a, b = left[:n], right[:n]
    if float(np.abs(a - b).mean()) < 1e-3:
        return False
    ea, eb = np.abs(a), np.abs(b)
    # 한쪽만 소리 나는 구간이 많으면 화자 분리 녹음
    only = ((ea > 0.02) & (eb < 0.005)) | ((eb > 0.02) & (ea < 0.005))
    return float(only.mean()) > 0.05


def _segments(model, audio, vad: bool):  # noqa: ANN001, ANN202
    segs, _ = model.transcribe(audio, language="ko", beam_size=5, vad_filter=vad, initial_prompt=PROMPT,
                               condition_on_previous_text=False)
    return [(float(s.start), float(s.end), s.text.strip()) for s in segs if s.text.strip()]


def transcribe_file(model, path: Path) -> dict:  # noqa: ANN001
    from faster_whisper import decode_audio
    stereo = decode_audio(str(path), sampling_rate=16000, split_stereo=True)
    left, right = stereo if isinstance(stereo, tuple) else (stereo, None)
    rows = []
    if right is not None and _channels_differ(left, right):
        mode = "좌우 채널 분리"
        for spk, ch in (("채널1", left), ("채널2", right)):
            rows += [(s, e, spk, t) for s, e, t in _segments(model, ch, True)]
        rows.sort()
        dur = len(left) / 16000
    else:
        mono = decode_audio(str(path), sampling_rate=16000)
        mode = "한 채널(화자 구분 없음)"
        rows = [(s, e, "", t) for s, e, t in _segments(model, mono, True)]
        dur = len(mono) / 16000
    return {"mode": mode, "duration_s": round(dur, 1),
            "rows": [{"start": round(s, 1), "end": round(e, 1), "speaker": spk, "text": mask(t)} for s, e, spk, t in rows]}


def write_xlsx(out: Path, results: list[dict]) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    head = Font(name="Arial", bold=True, color="FFFFFF")
    fill = PatternFill("solid", fgColor="1F3864")
    ws = wb.active
    ws.title = "통화목록"
    ws.append(["통화ID", "녹음 날짜(파일 수정일)", "길이(초)", "녹음 방식", "문장 수", "전체 대화(가림 처리)"])
    for r in results:
        ws.append([r["id"], r["date"], r["duration_s"], r["mode"], len(r["rows"]),
                   " / ".join((f"[{x['speaker']}] " if x["speaker"] else "") + x["text"] for x in r["rows"])[:32000]])
    ws2 = wb.create_sheet("발화")
    ws2.append(["통화ID", "시작(초)", "끝(초)", "화자", "문장(가림 처리)"])
    for r in results:
        for x in r["rows"]:
            ws2.append([r["id"], x["start"], x["end"], x["speaker"], x["text"]])
    ws3 = wb.create_sheet("안내")
    for line in ("이 파일은 aicc-transcribe 로 이 PC 에서 만든 분석용 자료입니다.",
                 "전화번호·주민번호·긴 숫자·이메일·호칭 앞 이름은 자동으로 가렸습니다.",
                 "말로 읽은 숫자(공일공…)나 호칭 없이 부른 이름은 남아 있을 수 있으니 공유 전에 훑어보세요.",
                 "녹음 원본과 파일 이름은 들어 있지 않습니다(통화ID 는 순번).",
                 "'좌우 채널 분리' 통화는 채널1/채널2 가 서로 다른 화자입니다."):
        ws3.append([line])
    for sh, widths in ((ws, [10, 20, 10, 20, 10, 120]), (ws2, [10, 10, 10, 8, 100]), (ws3, [110])):
        for i, w in enumerate(widths):
            sh.column_dimensions[chr(65 + i)].width = w
        for c in sh[1]:
            if sh is not ws3:
                c.font, c.fill = head, fill
        for row in sh.iter_rows(min_row=2):
            for c in row:
                c.font = Font(name="Arial")
                c.alignment = Alignment(vertical="top", wrap_text=c.column_letter in ("E", "F"))
        sh.freeze_panes = "A2" if sh is not ws3 else None
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - 실제 모델 필요
    ap = argparse.ArgumentParser(prog="aicc-transcribe", description="통화녹음 → 개인정보 가린 대화 엑셀(이 PC 에서 처리)")
    ap.add_argument("folder", help="녹음 파일이 있는 폴더(하위 폴더 포함)")
    ap.add_argument("--out", default=None, help="결과 엑셀 경로(기본: .aicc\\transcripts_날짜.xlsx)")
    ap.add_argument("--model", default="small", help="small(기본, 빠름) / medium(더 정확, 처음 1.5GB 내려받음)")
    ap.add_argument("--limit", type=int, default=0, help="앞에서부터 이 개수만 처리(시험용)")
    a = ap.parse_args(argv)
    src = Path(a.folder)
    if not src.is_dir():
        print(f"폴더가 없습니다: {src}")
        return 2
    files = sorted(p for p in src.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXT)
    if a.limit:
        files = files[: a.limit]
    if not files:
        print("녹음 파일(m4a·mp3·wav·amr 등)을 찾지 못했습니다.")
        return 2
    data = Path(".aicc")
    cache = data / "transcribe_cache.jsonl"
    done: dict[str, dict] = {}
    if cache.is_file():
        for line in cache.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
                done[d["key"]] = d
            except (ValueError, KeyError):
                pass
    print(f"녹음 {len(files)}개 — 모델 {a.model} 준비 중(처음엔 내려받느라 오래 걸릴 수 있음)…")
    import os

    from faster_whisper import WhisperModel
    model = WhisperModel(a.model, device=os.environ.get("AICC_WHISPER_DEVICE", "cpu"), compute_type="int8",
                         cpu_threads=os.cpu_count() or 4)
    results = []
    t0 = time.time()
    with cache.open("a", encoding="utf-8") as fc:
        for i, f in enumerate(files, 1):
            st = f.stat()
            key = hashlib.sha256(f"{f.resolve()}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:20]
            if key not in done:
                try:
                    r = transcribe_file(model, f)
                except Exception as e:  # noqa: BLE001
                    print(f"  [{i}/{len(files)}] 건너뜀(읽기 실패: {type(e).__name__})")
                    continue
                r["key"] = key
                r["date"] = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
                fc.write(json.dumps(r, ensure_ascii=False) + "\n")
                fc.flush()
                done[key] = r
            r = dict(done[key])
            r["id"] = f"C{i:04d}"
            results.append(r)
            el = time.time() - t0
            print(f"  [{i}/{len(files)}] {r['duration_s']}초 · {r['mode']} · 문장 {len(r['rows'])}개 (경과 {int(el // 60)}분)")
    out = Path(a.out) if a.out else data / f"transcripts_{datetime.now():%Y%m%d_%H%M}.xlsx"
    write_xlsx(out, results)
    print(f"\n완료: {out.resolve()}")
    print("공유 전에 '발화' 시트를 훑어보고, 남은 개인정보가 있으면 지운 뒤 올려 주세요.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
