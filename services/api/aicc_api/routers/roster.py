"""명부 업로드·검증·저장, 대상 조회, 수동 일정 수정, 연락거부 관리."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, File, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .. import excel_io, services
from ..db import get_db
from ..deps import Principal, audit, current_user, require
from ..errors import AppError
from ..models import Caregiver, ContactBlock, ImportBatch, ImportRow, ScheduleChange, User

router = APIRouter(prefix="/api/v1", tags=["roster"])


@router.post("/imports", status_code=201)
async def upload(file: UploadFile = File(...), p: Principal = Depends(current_user),
                 db: Session = Depends(get_db)) -> dict:
    require(p, "upload_dial")
    data = await file.read(excel_io.MAX_BYTES + 1)
    try:
        _, out = services.upload_import(db, p, file.filename or "upload.xlsx", data)
    except excel_io.ExcelRejected as e:
        raise AppError(422, e.code, e.message) from e
    db.commit()
    return out


@router.get("/imports")
def list_imports(p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    require(p, "view_results")
    return [{"id": b.id, "file_name": b.file_name, "status": b.status, "row_count": b.row_count,
             "valid_count": b.valid_count, "date_semantics": b.date_semantics,
             "created_at": services.kst_text(b.created_at)}
            for b in db.scalars(select(ImportBatch).where(ImportBatch.organization_id == p.org.id)
                                .order_by(ImportBatch.created_at.desc()).limit(50))]


@router.get("/imports/{import_id}")
def get_import(import_id: str, p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    require(p, "view_results")
    b = services.get_batch(db, p, import_id)
    return {"id": b.id, "file_name": b.file_name, "status": b.status, "headers": b.headers,
            "header_row": b.header_row, "mapping": b.mapping, "date_semantics": b.date_semantics,
            "row_count": b.row_count, "valid_count": b.valid_count, "validation_version": b.validation_version,
            "suggested_mapping": excel_io.suggest_mapping(b.headers)}


class ValidateIn(BaseModel):
    mapping: dict[str, int | None]
    date_semantics: str | None = None


@router.post("/imports/{import_id}/validate")
def validate(import_id: str, body: ValidateIn, p: Principal = Depends(current_user),
             db: Session = Depends(get_db)) -> dict:
    require(p, "upload_dial")
    b = services.get_batch(db, p, import_id)
    out = services.validate_import(db, p, b, body.mapping, body.date_semantics)
    db.commit()
    return out


class CommitIn(BaseModel):
    validation_version: int


@router.post("/imports/{import_id}/commit", status_code=201)
def commit(import_id: str, body: CommitIn, p: Principal = Depends(current_user),
           db: Session = Depends(get_db)) -> dict:
    require(p, "upload_dial")
    b = services.get_batch(db, p, import_id)
    out = services.commit_import(db, p, b, body.validation_version)
    db.commit()
    return out


@router.get("/caregivers")
def list_caregivers(batch_id: str = Query(...), q: str | None = None, status: str | None = None,
                    cursor: int = 0, limit: int = Query(200, le=1000), p: Principal = Depends(current_user),
                    db: Session = Depends(get_db)) -> dict:
    require(p, "view_results")
    b = services.get_batch(db, p, batch_id)
    stmt = (select(ImportRow, Caregiver).join(Caregiver, Caregiver.id == ImportRow.caregiver_id, isouter=True)
            .where(ImportRow.batch_id == b.id, ImportRow.row_no > b.header_row).order_by(ImportRow.row_no))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Caregiver.name.like(like), Caregiver.external_no.like(like),
                              Caregiver.phone_masked.like(f"%{q.strip()[-4:]}%")))
    items = []
    seen: set[str] = set()
    for row, cg in db.execute(stmt):
        if cg is None and not row.errors:
            continue  # 빈 행
        blocked = bool(cg and services.active_block(db, p.org.id, cg.phone_hash))
        dup = bool(cg and cg.id in seen)
        if cg:
            seen.add(cg.id)
        reason = None
        if row.errors:
            reason = "ROW_ERROR"
        elif blocked:
            reason = "BLOCKED"
        elif dup or "DUP_ROW" in (row.warnings or []):
            reason = "DUPLICATE"
        elif cg and "SHARED_PHONE" in (cg.review_flags or []):
            reason = "SHARED_PHONE_REVIEW"
        item = {
            "row_no": row.row_no, "caregiver_id": cg.id if cg else None,
            "external_no": cg.external_no if cg else None, "name": cg.name if cg else None,
            "phone": cg.phone_masked if cg else None,
            "confirmed_available_date": cg.confirmed_available_date.isoformat() if cg and cg.confirmed_available_date else None,
            "next_available_date": cg.next_available_date.isoformat() if cg and cg.next_available_date else None,
            "work_status": cg.work_status if cg else None, "last_outcome": cg.last_outcome if cg else None,
            "last_call_at": services.kst_text(cg.last_call_at) if cg else None,
            "errors": row.errors or [], "warnings": row.warnings or [], "blocked": blocked,
            "dialable": reason is None and cg is not None, "exclude_reason": reason,
            "review_flags": cg.review_flags if cg else [], "version": cg.version if cg else None,
        }
        if status == "dialable" and not item["dialable"]:
            continue
        if status == "excluded" and item["dialable"]:
            continue
        items.append(item)
    page = items[cursor:cursor + limit]
    nxt = cursor + limit if cursor + limit < len(items) else None
    return {"items": page, "next_cursor": nxt, "total": len(items)}


class ScheduleIn(BaseModel):
    date: date | None
    field: str = Field(default="auto", pattern="^(auto|confirmed_available_date|next_available_date)$")
    expected_version: int
    reason: str = Field(min_length=2, max_length=500)
    call_id: str | None = None


@router.patch("/caregivers/{caregiver_id}/schedule")
def edit_schedule(caregiver_id: str, body: ScheduleIn, p: Principal = Depends(current_user),
                  db: Session = Depends(get_db)) -> dict:
    require(p, "edit_dates")
    cg = db.get(Caregiver, caregiver_id)
    if cg is None or cg.organization_id != p.org.id:
        raise AppError(404, "NOT_FOUND", "간병인이 없습니다")
    if body.expected_version != cg.version:
        raise AppError(409, "VERSION_CONFLICT", "다른 사용자가 먼저 변경했습니다. 최신 값을 확인하세요",
                       {"current_version": cg.version,
                        "current": {"confirmed_available_date": str(cg.confirmed_available_date or ""),
                                    "next_available_date": str(cg.next_available_date or "")}})
    field = body.field
    if field == "auto":
        last = db.scalar(select(ImportBatch).join(ImportRow, ImportRow.batch_id == ImportBatch.id)
                         .where(ImportRow.caregiver_id == cg.id).order_by(ImportBatch.created_at.desc()))
        field = "confirmed_available_date" if (last and last.date_semantics == "NEXT_AVAILABLE") else "next_available_date"
    old = getattr(cg, field)
    setattr(cg, field, body.date)
    cg.version += 1
    cg.availability_state = "MANUAL"
    db.add(ScheduleChange(organization_id=p.org.id, caregiver_id=cg.id, field=field, old_date=old, new_date=body.date,
                          call_id=body.call_id, actor_type="user", actor_id=p.id, reason=body.reason.strip()))
    audit(db, p, "schedule.manual_edit", "caregiver", cg.id, {"field": field, "call_id": body.call_id})
    db.commit()
    return {"version": cg.version, "field": field}


class ReviewFlagIn(BaseModel):
    clear_flags: list[str] = []
    note: str | None = Field(default=None, max_length=500)
    expected_version: int


@router.patch("/caregivers/{caregiver_id}/review")
def review_caregiver(caregiver_id: str, body: ReviewFlagIn, p: Principal = Depends(current_user),
                     db: Session = Depends(get_db)) -> dict:
    require(p, "edit_dates")
    cg = db.get(Caregiver, caregiver_id)
    if cg is None or cg.organization_id != p.org.id:
        raise AppError(404, "NOT_FOUND", "간병인이 없습니다")
    if body.expected_version != cg.version:
        raise AppError(409, "VERSION_CONFLICT", "다른 사용자가 먼저 변경했습니다", {"current_version": cg.version})
    cg.review_flags = [f for f in (cg.review_flags or []) if f not in body.clear_flags]
    if body.note is not None:
        cg.review_note = body.note
    cg.version += 1
    audit(db, p, "caregiver.review", "caregiver", cg.id, {"cleared": body.clear_flags})
    db.commit()
    return {"version": cg.version, "review_flags": cg.review_flags}


@router.get("/caregivers/{caregiver_id}/changes")
def caregiver_changes(caregiver_id: str, p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> list:
    require(p, "view_results")
    cg = db.get(Caregiver, caregiver_id)
    if cg is None or cg.organization_id != p.org.id:
        raise AppError(404, "NOT_FOUND", "간병인이 없습니다")
    users = {u.id: u.username for u in db.scalars(select(User).where(User.organization_id == p.org.id))}
    return [{"field": c.field, "old_date": str(c.old_date or ""), "new_date": str(c.new_date or ""),
             "actor": users.get(c.actor_id, "AICC 에이전트"), "actor_type": c.actor_type, "reason": c.reason,
             "evidence_text": c.evidence_text, "call_id": c.call_id, "at": services.kst_text(c.created_at)}
            for c in db.scalars(select(ScheduleChange).where(ScheduleChange.caregiver_id == cg.id)
                                .order_by(ScheduleChange.created_at))]


# ------------------ 연락거부 ------------------
class BlockIn(BaseModel):
    caregiver_id: str
    reason: str = Field(min_length=2, max_length=500)
    call_id: str | None = None


class RevokeIn(BaseModel):
    reason: str = Field(min_length=5, max_length=1000)


@router.get("/contact-blocks")
def list_blocks(q: str | None = None, include_revoked: bool = False, p: Principal = Depends(current_user),
                db: Session = Depends(get_db)) -> list[dict]:
    require(p, "view_results")
    stmt = select(ContactBlock).where(ContactBlock.organization_id == p.org.id)
    if not include_revoked:
        stmt = stmt.where(ContactBlock.revoked_at.is_(None))
    users = {u.id: u.username for u in db.scalars(select(User).where(User.organization_id == p.org.id))}
    out = []
    for b in db.scalars(stmt.order_by(ContactBlock.blocked_at.desc())):
        cg = db.get(Caregiver, b.caregiver_id) if b.caregiver_id else None
        item = {"id": b.id, "phone": b.phone_masked, "caregiver_name": cg.name if cg else None,
                "external_no": cg.external_no if cg else None, "reason": b.reason, "source_call_id": b.source_call_id,
                "blocked_by": users.get(b.blocked_by, "AICC 에이전트"), "blocked_at": services.kst_text(b.blocked_at),
                "revoked_at": services.kst_text(b.revoked_at), "revoked_by": users.get(b.revoked_by or "", None),
                "revoke_reason": b.revoke_reason}
        if q and q not in f"{item['caregiver_name']}{item['external_no']}{item['phone']}":
            continue
        out.append(item)
    return out


@router.post("/contact-blocks", status_code=201)
def create_block(body: BlockIn, p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    require(p, "block_contact")
    cg = db.get(Caregiver, body.caregiver_id)
    if cg is None or cg.organization_id != p.org.id:
        raise AppError(404, "NOT_FOUND", "간병인이 없습니다")
    blk = services.block_contact(db, p, caregiver=cg, reason=body.reason, call_id=body.call_id)
    db.commit()
    return {"id": blk.id, "phone": blk.phone_masked}


@router.post("/contact-blocks/{block_id}/revoke")
def revoke(block_id: str, body: RevokeIn, p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    require(p, "unblock_contact")
    services.revoke_block(db, p, block_id, body.reason)
    db.commit()
    return {"ok": True}


