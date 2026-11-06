"""결과 엑셀 생성(F13). 명부·통화이력·변경이력·오류행·요약 시트."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import excel_io
from .deps import Principal, audit
from .models import (
    CallSession,
    Campaign,
    CampaignTarget,
    Caregiver,
    Export,
    ImportBatch,
    Recording,
    ScheduleChange,
    ScheduleObservation,
    User,
)
from .security import put_object
from .services import _load_rows, active_block, kst_text

OUTCOME_KO = {
    "CONFIRMED": "일정 확인완료", "ANYTIME": "상시 가능 응답", "UNCONFIRMED": "미확인", "CALLBACK_REQUESTED": "재연락 요청",
    "REFUSED": "연락거부", "WRONG_PERSON": "잘못된 상대", "VOICEMAIL": "음성사서함", "NO_ANSWER": "부재중",
    "LINE_BUSY": "통화중", "DIAL_FAILED": "발신 실패", "NO_RESPONSE": "무응답", "CONSULT_REQUESTED": "상담 요청",
    "REMOTE_HANGUP": "상대 종료", "DEVICE_ERROR": "장치 오류", "STOPPED": "운영자 중단",
}
STATE_KO = {
    "CONFIRMED": "통화 확인완료", "ANYTIME": "상시 가능(날짜 미확정)", "RANGE": "범위만 응답(미확정)",
    "UNKNOWN": "모름(미확정)", "UNCONFIRMED": "미확인(기존값 유지)",
}
WORK_KO = {"WORKING": "근무중", "NOT_WORKING": "근무 안 함"}


def build(db: Session, p: Principal, *, batch: ImportBatch, campaign: Campaign | None, scope: str) -> Export:
    rows, parsed = _load_rows(db, batch)
    mapping = batch.mapping or {}
    date_col = mapping.get("date")
    date_col = date_col if isinstance(date_col, int) else -1
    calls_q = select(CallSession).where(CallSession.organization_id == p.org.id)
    if campaign:
        calls_q = calls_q.where(CallSession.campaign_id == campaign.id)
    else:
        camp_ids = select(Campaign.id).where(Campaign.batch_id == batch.id)
        calls_q = calls_q.where(CallSession.campaign_id.in_(camp_ids))
    calls = list(db.scalars(calls_q.order_by(CallSession.started_at)))
    call_ids = {c.id for c in calls}
    latest: dict[str, CallSession] = {}
    for c in calls:
        latest[c.caregiver_id] = c
    obs = {o.call_id: o for o in db.scalars(select(ScheduleObservation).where(
        ScheduleObservation.call_id.in_(call_ids)))} if call_ids else {}
    recs = {r.call_id: r for r in db.scalars(select(Recording).where(Recording.call_id.in_(call_ids)))} if call_ids else {}
    cg_ids = [r.caregiver_id for r in rows if r.caregiver_id]
    cgs = {c.id: c for c in db.scalars(select(Caregiver).where(Caregiver.id.in_(cg_ids)))} if cg_ids else {}
    changes = list(db.scalars(select(ScheduleChange).where(
        ScheduleChange.caregiver_id.in_(cg_ids), ScheduleChange.created_at >= batch.created_at)
        .order_by(ScheduleChange.created_at))) if cg_ids else []
    changed_field = {(ch.caregiver_id, ch.field) for ch in changes}
    manual = {ch.caregiver_id for ch in changes if ch.actor_type == "user"}
    selected: set[str] | None = None
    if campaign and scope == "selected":
        selected = set(db.scalars(select(CampaignTarget.caregiver_id).where(CampaignTarget.campaign_id == campaign.id)))
    is_mock = any(c.is_mock for c in calls) or bool(campaign and campaign.is_mock)

    export_rows: list[excel_io.ExportRow] = []
    seen_cg: set[str] = set()
    for r, raw in zip(rows, parsed.rows, strict=True):
        kind = "PREAMBLE" if r.row_no < batch.header_row else "HEADER" if r.row_no == batch.header_row else (
            "EMPTY" if all(v in (None, "") for v in raw) else "DATA")
        er = excel_io.ExportRow(r.row_no, kind, raw)
        if kind == "DATA":
            cg = cgs.get(r.caregiver_id) if r.caregiver_id else None
            if cg and selected is not None and cg.id not in selected:
                cg = None
            if cg and cg.id not in seen_cg:
                seen_cg.add(cg.id)
                if batch.date_semantics == "NEXT_AVAILABLE" and (cg.id, "confirmed_available_date") in changed_field:
                    er.date_override = cg.confirmed_available_date
                call = latest.get(cg.id)
                o = obs.get(call.id) if call else None
                state = o.confirmation_state if o else None
                avail = None
                if o and state == "CONFIRMED":
                    avail = o.available_date
                elif o and state == "ANYTIME":
                    avail = "상시 가능"
                elif o and state == "RANGE" and o.range_start:
                    avail = f"범위 {o.range_start}~{o.range_end} (미확정)"
                confirm = "미통화(기존값 유지)" if call is None else STATE_KO.get(state or "UNCONFIRMED", "미확인(기존값 유지)")
                if cg.id in manual:
                    confirm += " / 검수자 수정"
                notes = list(call.review_reasons or []) if call else []
                if cg.review_note:
                    notes.append(cg.review_note)
                cond = [n for h in ((o.history or []) if o else []) if h.get("event") == "NOTES" for n in h.get("notes", [])]
                if cond:
                    notes.append("조건: " + ", ".join(dict.fromkeys(cond)))
                er.added = {
                    "현재근무상태": WORK_KO.get((o.work_status if o else None) or "", ""),
                    "근무종료일": o.end_date if o and o.end_date else None,
                    "다음근무가능일": avail,
                    "확인상태": confirm,
                    "통화결과": OUTCOME_KO.get(call.outcome or "", call.outcome or "진행 중") if call else "",
                    "연락거부": "Y" if active_block(db, p.org.id, cg.phone_hash) else "",
                    "마지막통화시각": kst_text(call.ended_at or call.started_at) if call else None,
                    "검수메모": "; ".join(notes) or None,
                    "데이터구분": "MOCK 시험(실통화 아님)" if (call and call.is_mock) else "",
                }
            elif cg:
                er.added = {"확인상태": "중복 행(앞 행 참조)"}
            else:
                er.added = {"확인상태": "오류·제외 행" if r.errors else "대상 아님"}
        export_rows.append(er)

    users = {u.id: u.username for u in db.scalars(select(User).where(User.organization_id == p.org.id))}
    by_id = {c.id: c for c in db.scalars(select(Caregiver).where(Caregiver.organization_id == p.org.id,
                                                                 Caregiver.id.in_({c.caregiver_id for c in calls})))} \
        if calls else {}
    call_rows = []
    for c in calls:
        cg = by_id.get(c.caregiver_id)
        o = obs.get(c.id)
        rec = recs.get(c.id)
        call_rows.append({
            "통화ID": c.id, "간병인 번호": cg.external_no if cg else None, "이름": cg.name if cg else None,
            "연락처(마스킹)": cg.phone_masked if cg else None, "시작(KST)": kst_text(c.started_at),
            "응답(KST)": kst_text(c.answered_at), "종료(KST)": kst_text(c.ended_at),
            "통화결과": OUTCOME_KO.get(c.outcome or "", c.outcome or "진행 중"),
            "근무상태": WORK_KO.get((o.work_status if o else None) or "", ""),
            "종료일 후보": o.end_date if o else None, "가능일 후보": o.available_date if o else None,
            "확인상태": STATE_KO.get(o.confirmation_state, o.confirmation_state) if o else "",
            "검수사유": ", ".join(c.review_reasons or []) or None,
            "녹음상태": {"READY": "저장됨", "FAILED": "저장 실패", "EXPIRED": "보관기간 만료",
                     "PENDING": "업로드 대기"}.get(rec.state, rec.state) if rec else "없음",
            "데이터구분": "MOCK 시험" if c.is_mock else "실통화",
        })
    cg_all = {c.id: c for c in db.scalars(select(Caregiver).where(Caregiver.id.in_({ch.caregiver_id for ch in changes})))} \
        if changes else {}
    change_rows = [{
        "간병인 번호": cg_all[ch.caregiver_id].external_no if ch.caregiver_id in cg_all else None,
        "이름": cg_all[ch.caregiver_id].name if ch.caregiver_id in cg_all else None,
        "항목": {"confirmed_available_date": "근무확정일", "next_available_date": "다음근무가능일"}.get(ch.field, ch.field),
        "기존 날짜": ch.old_date, "새 날짜": ch.new_date, "근거 발화": ch.evidence_text, "통화ID": ch.call_id,
        "확인자 유형": "통화 재확인" if ch.actor_type == "agent" else "검수자",
        "확인자": users.get(ch.actor_id, ch.actor_id if ch.actor_type == "user" else "AICC 에이전트"),
        "사유": ch.reason, "시각(KST)": kst_text(ch.created_at),
    } for ch in changes if call_ids & {ch.call_id} or ch.actor_type == "user"]

    results = []
    for r in rows:
        if r.errors or r.warnings:
            rr = excel_io.RowResult(r.row_no, "DATA", errors=list(r.errors or []), warnings=list(r.warnings or []))
            raw = parsed.rows[r.row_no - 1]

            def g(f: str, raw=raw) -> object:
                j = mapping.get(f)
                return excel_io._to_text(raw[j]) if isinstance(j, int) and j < len(raw) else None
            rr.external_no, rr.name = g("external_no"), g("name")
            from aicc_core.phone import normalize_phone
            rr.phone = normalize_phone(g("phone")).normalized
            results.append(rr)
    err_rows = excel_io.error_rows(results, parsed.rows, date_col if date_col >= 0 else 0)
    for e in err_rows:  # 연락처는 마스킹(원본 명부 시트에 원래 값 존재)
        e["연락처(마스킹)"] = e.get("연락처(마스킹)") or None

    data_rows = sum(1 for e in export_rows if e.kind == "DATA")
    targets = 0
    if campaign:
        targets = len(list(db.scalars(select(CampaignTarget.id).where(CampaignTarget.campaign_id == campaign.id))))
    summary = {
        "원본 파일": batch.file_name, "업로드 행 수(머리글 제외)": batch.row_count, "데이터 행 수": data_rows,
        "오류 행 수": sum(1 for r in rows if r.errors), "캠페인 대상 수": targets if campaign else "-",
        "통화 시도 수": len(calls), "일정 확인완료 통화 수": sum(1 for c in calls if c.outcome == "CONFIRMED"),
        "날짜 변경 건수": len(change_rows),
        "연락거부 통화 수": sum(1 for c in calls if c.outcome == "REFUSED"),
        "진행 중 통화 수": sum(1 for c in calls if c.ended_at is None),
        "근무확정일 열 의미": {"NEXT_AVAILABLE": "다음 근무 가능일(통화 확인 시 갱신)",
                       "ASSIGNMENT_CONFIRMED": "실제 배정 확정일(갱신 안 함, 다음근무가능일 열에 기록)"}.get(
            batch.date_semantics or "", "미지정"),
    }
    in_progress = bool(campaign and campaign.state in ("RUNNING", "PAUSED", "STOPPING", "DRAFT")) or any(
        c.ended_at is None for c in calls)
    data = excel_io.build_export(
        sheet_name=batch.sheet_name, header_row=batch.header_row, headers=batch.headers,
        width=max(len(batch.headers), max((len(r) for r in parsed.rows), default=0)), date_col=date_col,
        rows=export_rows, calls=call_rows, changes=change_rows, errors=err_rows, summary=summary,
        in_progress=in_progress, is_mock=is_mock)
    exp = Export(organization_id=p.org.id, batch_id=batch.id, campaign_id=campaign.id if campaign else None,
                 scope=scope, in_progress=in_progress, state="READY", object_key=put_object("exports", data),
                 summary={k: v for k, v in summary.items() if isinstance(v, (int, str))}, created_by=p.id)
    db.add(exp)
    db.flush()
    audit(db, p, "export.create", "export", exp.id, {"batch_id": batch.id, "scope": scope, "in_progress": in_progress})
    return exp
