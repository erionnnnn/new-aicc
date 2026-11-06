"""캠페인 제어, 통화 조회·검수, 녹음 재생, 결과 내보내기."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import exporting, services
from ..db import get_db
from ..deps import Principal, audit, current_user, idem_lookup, idem_store, require
from ..errors import AppError
from ..models import (
    CallEvent,
    CallSession,
    Campaign,
    CampaignTarget,
    Caregiver,
    Device,
    Export,
    ImportBatch,
    Recording,
    ScheduleObservation,
    VoicePack,
)
from ..security import get_object, sign_token, verify_signed

router = APIRouter(prefix="/api/v1", tags=["campaigns"])


class CampaignIn(BaseModel):
    batch_id: str
    target_ids: list[str] = Field(min_length=1, max_length=10000)
    voice_pack_id: str
    name: str | None = Field(default=None, max_length=200)


@router.post("/campaigns", status_code=201)
def create(body: CampaignIn, p: Principal = Depends(current_user), db: Session = Depends(get_db),
           idempotency_key: str | None = Header(default=None)) -> JSONResponse:
    require(p, "upload_dial")
    hit = idem_lookup(db, p, "POST /campaigns", idempotency_key, body.model_dump())
    if hit:
        return JSONResponse(hit[1], status_code=hit[0])
    out = services.create_campaign(db, p, batch_id=body.batch_id, caregiver_ids=body.target_ids,
                                   voice_pack_id=body.voice_pack_id, name=body.name)
    idem_store(db, p, "POST /campaigns", idempotency_key, body.model_dump(), 201, out)
    db.commit()
    return JSONResponse(out, status_code=201)


def _camp_out(db: Session, c: Campaign) -> dict:
    counts = dict(db.execute(select(CampaignTarget.state, func.count()).where(CampaignTarget.campaign_id == c.id)
                             .group_by(CampaignTarget.state)).all())
    outcomes = dict(db.execute(select(CallSession.outcome, func.count()).where(CallSession.campaign_id == c.id)
                               .group_by(CallSession.outcome)).all())
    pack = db.get(VoicePack, c.voice_pack_id)
    total = sum(counts.values())
    done = counts.get("DONE", 0) + counts.get("BLOCKED", 0) + counts.get("CANCELLED", 0)
    return {"id": c.id, "name": c.name, "state": c.state, "version": c.version, "is_mock": c.is_mock, "pilot": c.pilot,
            "batch_id": c.batch_id, "device_id": c.device_id,
            "voice_pack": {"id": pack.id, "key": pack.pack_key, "version": pack.version} if pack else None,
            "total": total, "done": done, "target_states": counts,
            "outcomes": {k or "IN_PROGRESS": v for k, v in outcomes.items()},
            "created_at": services.kst_text(c.created_at), "started_at": services.kst_text(c.started_at),
            "ended_at": services.kst_text(c.ended_at)}


@router.get("/campaigns")
def list_campaigns(p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    require(p, "view_results")
    return [_camp_out(db, c) for c in db.scalars(select(Campaign).where(Campaign.organization_id == p.org.id)
                                                 .order_by(Campaign.created_at.desc()).limit(50))]


@router.get("/campaigns/{cid}")
def get_campaign(cid: str, p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    require(p, "view_results")
    return _camp_out(db, services.get_campaign(db, p, cid))


@router.get("/campaigns/{cid}/preflight")
def preflight(cid: str, device_id: str | None = None, p: Principal = Depends(current_user),
              db: Session = Depends(get_db)) -> dict:
    require(p, "upload_dial")
    c = services.get_campaign(db, p, cid)
    return services.preflight(db, p, c, device_id or c.device_id)


class StartIn(BaseModel):
    device_id: str
    expected_version: int


class VersionIn(BaseModel):
    expected_version: int


class StopIn(BaseModel):
    mode: str = Field(default="immediate", pattern="^immediate$")
    expected_version: int


@router.post("/campaigns/{cid}/start", status_code=202)
def start(cid: str, body: StartIn, p: Principal = Depends(current_user), db: Session = Depends(get_db),
          idempotency_key: str | None = Header(default=None)) -> JSONResponse:
    require(p, "upload_dial")
    route = f"POST /campaigns/{cid}/start"
    hit = idem_lookup(db, p, route, idempotency_key, body.model_dump())
    if hit:
        return JSONResponse(hit[1], status_code=hit[0])
    c = services.get_campaign(db, p, cid)
    services.check_version(c, body.expected_version)
    if c.state not in ("DRAFT", "PAUSED"):
        raise AppError(409, "STATE_CONFLICT", f"현재 상태({c.state})에서는 시작할 수 없습니다")
    pf = services.preflight(db, p, c, body.device_id)
    if not pf["ok"]:
        code = "DEVICE_AUDIO_UNVERIFIED" if any(ch["key"] == "device" and not ch["ok"] for ch in pf["checks"]) \
            else "PREFLIGHT_FAILED"
        raise AppError(409, code, "시작 조건을 충족하지 않습니다", {"checks": pf["checks"]})
    c.device_id = body.device_id
    c.state = "RUNNING"
    c.version += 1
    c.started_at = c.started_at or services.utcnow()
    audit(db, p, "campaign.start", "campaign", c.id, {"device_id": body.device_id, "is_mock": c.is_mock})
    out = {"command_id": idempotency_key, "state": "STARTING", "version": c.version,
           "note": "202는 명령 접수이며 실제 발신 완료를 뜻하지 않습니다"}
    idem_store(db, p, route, idempotency_key, body.model_dump(), 202, out)
    db.commit()
    return JSONResponse(out, status_code=202)


@router.post("/campaigns/{cid}/pause", status_code=202)
def pause(cid: str, body: VersionIn, p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    require(p, "upload_dial")
    c = services.get_campaign(db, p, cid)
    services.check_version(c, body.expected_version)
    if c.state != "RUNNING":
        raise AppError(409, "STATE_CONFLICT", "진행 중인 캠페인만 일시정지할 수 있습니다")
    c.state = "PAUSED"
    c.version += 1
    audit(db, p, "campaign.pause", "campaign", c.id)
    db.commit()
    return {"state": c.state, "version": c.version, "note": "현재 통화를 마친 뒤 다음 발신을 멈춥니다"}


@router.post("/campaigns/{cid}/stop", status_code=202)
def stop(cid: str, body: StopIn, p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    require(p, "upload_dial")
    c = services.get_campaign(db, p, cid)
    services.check_version(c, body.expected_version)
    if c.state in ("STOPPED", "COMPLETED"):
        raise AppError(409, "STATE_CONFLICT", "이미 종료된 캠페인입니다")
    calling = db.scalar(select(func.count()).select_from(CampaignTarget).where(
        CampaignTarget.campaign_id == c.id, CampaignTarget.state == "CALLING")) or 0
    from sqlalchemy import update
    db.execute(update(CampaignTarget).where(CampaignTarget.campaign_id == c.id, CampaignTarget.state == "QUEUED")
               .values(state="CANCELLED"))
    c.state = "STOPPING" if calling else "STOPPED"
    if not calling:
        c.ended_at = services.utcnow()
    c.version += 1
    audit(db, p, "campaign.stop", "campaign", c.id, {"calling": calling})
    db.commit()
    return {"state": c.state, "version": c.version,
            "note": "통화 종료 확인 중" if calling else "중단되었습니다",
            "manual_hangup_hint": "케이블이 빠져 종료 명령을 보낼 수 없으면 A24에서 직접 통화를 종료하세요"}


@router.get("/campaigns/{cid}/live")
def live(cid: str, p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    require(p, "view_results")
    c = services.get_campaign(db, p, cid)
    out = _camp_out(db, c)
    current = db.scalar(select(CallSession).where(CallSession.campaign_id == c.id, CallSession.ended_at.is_(None))
                        .order_by(CallSession.started_at.desc()))
    out["current"] = _call_detail(db, current, transcript_limit=30) if current else None
    recent = db.scalars(select(CallSession).where(CallSession.campaign_id == c.id, CallSession.ended_at.is_not(None))
                        .order_by(CallSession.ended_at.desc()).limit(20))
    out["recent"] = [_call_summary(db, x) for x in recent]
    queued = db.execute(select(CampaignTarget, Caregiver).join(Caregiver, Caregiver.id == CampaignTarget.caregiver_id)
                        .where(CampaignTarget.campaign_id == c.id, CampaignTarget.state == "QUEUED")
                        .order_by(CampaignTarget.order_no).limit(20)).all()
    out["queue"] = [{"name": cg.name, "external_no": cg.external_no, "phone": cg.phone_masked} for _, cg in queued]
    dev = db.get(Device, c.device_id) if c.device_id else None
    out["device"] = ({"id": dev.id, "name": dev.name, "kind": dev.kind, "phone_state": dev.phone_state,
                      **services.device_ready(dev)} if dev else None)
    return out


def _call_summary(db: Session, call: CallSession) -> dict:
    cg = db.get(Caregiver, call.caregiver_id)
    obs = db.scalar(select(ScheduleObservation).where(ScheduleObservation.call_id == call.id))
    rec = db.scalar(select(Recording).where(Recording.call_id == call.id))
    return {"id": call.id, "name": cg.name if cg else None, "external_no": cg.external_no if cg else None,
            "phone": cg.phone_masked if cg else None, "state": call.state, "outcome": call.outcome,
            "is_mock": call.is_mock, "started_at": services.kst_text(call.started_at),
            "answered_at": services.kst_text(call.answered_at), "ended_at": services.kst_text(call.ended_at),
            "review_state": call.review_state, "review_reasons": call.review_reasons or [],
            "confirmation_state": obs.confirmation_state if obs else None,
            "available_date": obs.available_date.isoformat() if obs and obs.available_date else None,
            "end_date": obs.end_date.isoformat() if obs and obs.end_date else None,
            "recording": {"id": rec.id, "state": rec.state, "error": rec.error} if rec else None}


def _call_detail(db: Session, call: CallSession, transcript_limit: int = 500) -> dict:
    out = _call_summary(db, call)
    evs = list(db.scalars(select(CallEvent).where(CallEvent.call_id == call.id).order_by(CallEvent.seq)))
    out["flags"] = call.flags or {}
    out["events"] = [{"id": e.id, "seq": e.seq, "turn": e.turn_id, "type": e.type,
                      "at": services.kst_text(e.occurred_at), "payload": _safe_payload(e)}
                     for e in evs[-transcript_limit:]]
    obs = db.scalar(select(ScheduleObservation).where(ScheduleObservation.call_id == call.id))
    out["observation"] = ({"work_status": obs.work_status, "end_date": str(obs.end_date or ""),
                           "available_date": str(obs.available_date or ""),
                           "range": [str(obs.range_start or ""), str(obs.range_end or "")],
                           "raw_text": obs.raw_text, "confirmation_state": obs.confirmation_state,
                           "evidence_event_id": obs.evidence_event_id, "history": obs.history or []}
                          if obs else None)
    cg = db.get(Caregiver, call.caregiver_id)
    out["caregiver"] = ({"id": cg.id, "confirmed_available_date": str(cg.confirmed_available_date or ""),
                         "next_available_date": str(cg.next_available_date or ""), "version": cg.version}
                        if cg else None)
    return out


def _safe_payload(e: CallEvent) -> dict:
    pl = dict(e.payload or {})
    pl.pop("phone", None)  # 방어적 제거
    return pl


@router.get("/calls/{call_id}")
def get_call(call_id: str, p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    require(p, "view_results")
    call = db.get(CallSession, call_id)
    if call is None or call.organization_id != p.org.id:
        raise AppError(404, "NOT_FOUND", "통화가 없습니다")
    return _call_detail(db, call)


@router.get("/reviews")
def reviews(state: str = "PENDING", p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> list:
    require(p, "view_results")
    stmt = select(CallSession).where(CallSession.organization_id == p.org.id)
    if state != "ALL":
        stmt = stmt.where(CallSession.review_state == state)
    return [_call_summary(db, c) for c in db.scalars(stmt.order_by(CallSession.started_at.desc()).limit(300))]


class ReviewIn(BaseModel):
    action: str = Field(pattern="^(done|hold)$")
    note: str | None = Field(default=None, max_length=500)


@router.post("/calls/{call_id}/review")
def review_call(call_id: str, body: ReviewIn, p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    require(p, "edit_dates")
    call = db.get(CallSession, call_id)
    if call is None or call.organization_id != p.org.id:
        raise AppError(404, "NOT_FOUND", "통화가 없습니다")
    call.review_state = "DONE" if body.action == "done" else "PENDING"
    if body.note:
        cg = db.get(Caregiver, call.caregiver_id)
        if cg:
            cg.review_note = body.note
    audit(db, p, f"review.{body.action}", "call", call.id, {})
    db.commit()
    return {"review_state": call.review_state}


# ------------------ 시간대별 연결률 ------------------
NOT_CONNECTED = {"NO_ANSWER", "LINE_BUSY", "DIAL_FAILED", "DEVICE_ERROR", "STOPPED"}
RESPONDED = {"CONFIRMED", "ANYTIME", "UNCONFIRMED", "CALLBACK_REQUESTED", "REFUSED", "WRONG_PERSON",
             "CONSULT_REQUESTED"}


@router.get("/stats/hourly")
def hourly_stats(days: int = 30, campaign_id: str | None = None, include_mock: bool = False,
                 p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """KST 발신 시각(시)별: 건수, 연결(받음)률, 응답(간병사가 의미 있는 답을 함)률, 날짜 확정률."""
    require(p, "view_results")
    from datetime import timedelta
    days = max(1, min(days, 365))
    since = services.utcnow() - timedelta(days=days)
    stmt = select(CallSession).where(CallSession.organization_id == p.org.id, CallSession.started_at >= since,
                                     CallSession.ended_at.is_not(None))
    if campaign_id:
        stmt = stmt.where(CallSession.campaign_id == campaign_id)
    if not include_mock:
        stmt = stmt.where(CallSession.is_mock.is_(False))
    rows: dict[int, dict] = {}
    for c in db.scalars(stmt):
        h = services.aware(c.started_at).astimezone(services.KST).hour
        r = rows.setdefault(h, {"hour": h, "calls": 0, "connected": 0, "responded": 0, "confirmed": 0})
        r["calls"] += 1
        # 사람이 받은 통화(음성사서함·무응답·통화중·발신 실패 제외)
        connected = c.outcome is not None and c.outcome not in NOT_CONNECTED and c.outcome != "VOICEMAIL"
        if connected:
            r["connected"] += 1
        if c.outcome in RESPONDED:
            r["responded"] += 1
        if c.outcome in ("CONFIRMED", "ANYTIME"):
            r["confirmed"] += 1

    def pct(a: int, b: int) -> float | None:
        return round(100.0 * a / b, 1) if b else None

    out = []
    for h in sorted(rows):
        r = rows[h]
        out.append({**r, "connect_rate": pct(r["connected"], r["calls"]),
                    "response_rate": pct(r["responded"], r["calls"]),
                    "confirm_rate": pct(r["confirmed"], r["calls"])})
    total = {k: sum(r[k] for r in rows.values()) for k in ("calls", "connected", "responded", "confirmed")}
    total.update(connect_rate=pct(total["connected"], total["calls"]),
                 response_rate=pct(total["responded"], total["calls"]),
                 confirm_rate=pct(total["confirmed"], total["calls"]))
    return {"days": days, "campaign_id": campaign_id, "include_mock": include_mock, "hours": out, "total": total}


# ------------------ 녹음 ------------------
@router.get("/recordings")
def list_recordings(p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    require(p, "view_results")
    rows = db.execute(select(Recording, CallSession).join(CallSession, CallSession.id == Recording.call_id)
                      .where(CallSession.organization_id == p.org.id).order_by(Recording.created_at.desc()).limit(300))
    out = []
    for rec, call in rows:
        cg = db.get(Caregiver, call.caregiver_id)
        out.append({"id": rec.id, "call_id": call.id, "name": cg.name if cg else None,
                    "external_no": cg.external_no if cg else None, "state": rec.state, "error": rec.error,
                    "duration_ms": rec.duration_ms, "is_mock": rec.is_mock, "call_ended": call.ended_at is not None,
                    "outcome": call.outcome, "created_at": services.kst_text(rec.created_at),
                    "expires_at": services.kst_text(rec.expires_at), "can_play": p.can("play_recording")})
    return out


@router.post("/recordings/{rec_id}/playback")
def playback(rec_id: str, p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    rec = db.get(Recording, rec_id)
    call = db.get(CallSession, rec.call_id) if rec else None
    if rec is None or call is None or call.organization_id != p.org.id:
        raise AppError(404, "NOT_FOUND", "녹음이 없습니다")
    if not p.can("play_recording"):
        audit(db, p, "recording.play_denied", "recording", rec.id, {})
        db.commit()
        raise AppError(403, "FORBIDDEN", "녹음 재생 권한이 없습니다")
    if call.ended_at is None:
        raise AppError(409, "CALL_IN_PROGRESS", "통화가 끝난 뒤에 재생할 수 있습니다")
    if rec.state != "READY":
        raise AppError(409, "RECORDING_NOT_READY", "재생할 수 있는 녹음이 없습니다", {"state": rec.state})
    token = sign_token({"rec": rec.id, "uid": p.id}, ttl_seconds=120)
    audit(db, p, "recording.play", "recording", rec.id, {"call_id": call.id})
    db.commit()
    return {"url": f"/api/v1/recordings/stream/{token}", "expires_in": 120}


@router.get("/recordings/stream/{token}")
def stream(token: str, p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    body = verify_signed(token)
    if not body or body.get("uid") != p.id:
        raise AppError(403, "PLAYBACK_TOKEN_INVALID", "재생 권한이 만료되었습니다")
    rec = db.get(Recording, body["rec"])
    if rec is None or rec.state != "READY" or not rec.object_key:
        raise AppError(404, "NOT_FOUND", "녹음이 없습니다")
    data = get_object(rec.object_key)
    return Response(data, media_type="audio/wav", headers={"Cache-Control": "no-store"})


# ------------------ 내보내기 ------------------
class ExportIn(BaseModel):
    batch_id: str | None = None
    campaign_id: str | None = None
    scope: str = Field(default="all", pattern="^(all|selected)$")


@router.post("/exports", status_code=202)
def create_export(body: ExportIn, p: Principal = Depends(current_user), db: Session = Depends(get_db),
                  idempotency_key: str | None = Header(default=None)) -> JSONResponse:
    if body.scope == "all":
        require(p, "export_all")
    else:
        require(p, "view_results")
    hit = idem_lookup(db, p, "POST /exports", idempotency_key, body.model_dump())
    if hit:
        return JSONResponse(hit[1], status_code=hit[0])
    camp = services.get_campaign(db, p, body.campaign_id) if body.campaign_id else None
    batch_id = camp.batch_id if camp else body.batch_id
    if not batch_id:
        raise AppError(422, "BATCH_REQUIRED", "명부 또는 캠페인을 지정하세요")
    batch = services.get_batch(db, p, batch_id)
    if batch.status != "COMMITTED":
        raise AppError(409, "BATCH_NOT_COMMITTED", "저장된 명부만 내보낼 수 있습니다")
    exp = exporting.build(db, p, batch=batch, campaign=camp, scope=body.scope)
    out = {"export_id": exp.id, "status": exp.state, "in_progress": exp.in_progress}
    idem_store(db, p, "POST /exports", idempotency_key, body.model_dump(), 202, out)
    db.commit()
    return JSONResponse(out, status_code=202)


@router.get("/exports/{exp_id}")
def get_export(exp_id: str, p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    require(p, "view_results")
    e = db.get(Export, exp_id)
    if e is None or e.organization_id != p.org.id:
        raise AppError(404, "NOT_FOUND", "내보내기 기록이 없습니다")
    return {"id": e.id, "status": e.state, "downloadable": e.state == "READY" and e.created_by == p.id,
            "in_progress": e.in_progress, "summary": e.summary, "created_at": services.kst_text(e.created_at)}


@router.get("/exports/{exp_id}/download")
def download(exp_id: str, p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    e = db.get(Export, exp_id)
    if e is None or e.organization_id != p.org.id or e.state != "READY" or not e.object_key:
        raise AppError(404, "NOT_FOUND", "내보내기 파일이 없습니다")
    if e.created_by != p.id and not p.can("export_all"):
        raise AppError(403, "FORBIDDEN", "다운로드 권한이 없습니다")
    batch = db.get(ImportBatch, e.batch_id)
    audit(db, p, "export.download", "export", e.id, {})
    db.commit()
    base = (batch.file_name.rsplit(".", 1)[0] if batch else "명부")
    suffix = "_진행중결과" if e.in_progress else "_결과"
    from urllib.parse import quote
    fname = quote(f"{base}{suffix}.xlsx")
    return Response(get_object(e.object_key),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{fname}",
                             "Cache-Control": "no-store"})
