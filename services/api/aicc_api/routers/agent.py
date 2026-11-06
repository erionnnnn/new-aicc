"""PC 에이전트 전용 API(장치 토큰 인증).

설계 10 은 outbound WSS 를 제안했다. MVP 는 같은 원칙(에이전트가 서버로만 연결, 짧은 수명 명령,
서버가 번호·차단을 검증하고 발신권을 한 번만 발급)을 HTTPS 폴링으로 구현한다.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Header, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import services
from ..db import get_db
from ..deps import Principal, current_device, idem_lookup, idem_store
from ..errors import AppError
from ..models import CallEvent, Caregiver, VoicePack

router = APIRouter(prefix="/api/v1", tags=["agent"])


class HeartbeatIn(BaseModel):
    capabilities: dict[str, str] = {}
    phone_state: str | None = None
    serial: str | None = Field(default=None, max_length=100)
    probe_report: dict | None = None
    agent_version: str | None = None


@router.post("/agent/heartbeat")
def heartbeat(body: HeartbeatIn, p: Principal = Depends(current_device), db: Session = Depends(get_db)) -> dict:
    out = services.heartbeat(db, p, body.model_dump())
    db.commit()
    return out


@router.post("/agent/claim")
def claim(p: Principal = Depends(current_device), db: Session = Depends(get_db)) -> dict:
    out = services.claim_next(db, p)
    db.commit()
    return out


class EventIn(BaseModel):
    event_id: str = Field(min_length=8, max_length=36)
    seq: int
    turn_id: int = 0
    type: str = Field(max_length=40)
    occurred_at: str | None = None
    payload: dict = {}


class EventsIn(BaseModel):
    events: list[EventIn] = Field(max_length=500)


@router.post("/agent/calls/{call_id}/events")
def events(call_id: str, body: EventsIn, p: Principal = Depends(current_device), db: Session = Depends(get_db)) -> dict:
    call = services.get_call_for_device(db, p, call_id)
    out = services.record_events(db, call, [e.model_dump() for e in body.events])
    db.commit()
    return out


class ConfirmIn(BaseModel):
    observation_id: str | None = None
    evidence_event_id: str
    expected_version: int | None = None


@router.post("/calls/{call_id}/schedule-confirmations")
def confirm(call_id: str, body: ConfirmIn, p: Principal = Depends(current_device), db: Session = Depends(get_db),
            idempotency_key: str | None = Header(default=None)) -> JSONResponse:
    route = f"POST /calls/{call_id}/schedule-confirmations"
    hit = idem_lookup(db, p, route, idempotency_key, body.model_dump())
    if hit:
        return JSONResponse(hit[1], status_code=hit[0])
    call = services.get_call_for_device(db, p, call_id)
    out = services.confirm_schedule(db, p, call, evidence_event_id=body.evidence_event_id,
                                    expected_version=body.expected_version)
    idem_store(db, p, route, idempotency_key, body.model_dump(), 200, out)
    db.commit()
    return JSONResponse(out, status_code=200)


class AgentBlockIn(BaseModel):
    evidence_event_id: str
    reason: str = "통화 중 연락거부 요청"


@router.post("/agent/calls/{call_id}/block", status_code=201)
def block(call_id: str, body: AgentBlockIn, p: Principal = Depends(current_device), db: Session = Depends(get_db)) -> dict:
    call = services.get_call_for_device(db, p, call_id)
    ev = db.get(CallEvent, body.evidence_event_id)
    if ev is None or ev.call_id != call.id:
        raise AppError(422, "EVIDENCE_INVALID", "근거 발화 이벤트가 이 통화에 없습니다")
    cg = db.get(Caregiver, call.caregiver_id)
    blk = services.block_contact(db, p, caregiver=cg, reason=body.reason, call_id=call.id,
                                 evidence_event_id=ev.id)
    db.commit()
    return {"block_id": blk.id}


class FinishIn(BaseModel):
    outcome: str
    observation: dict | None = None
    review_reasons: list[str] = []
    phone_idle: bool = False


@router.post("/agent/calls/{call_id}/finish")
def finish(call_id: str, body: FinishIn, p: Principal = Depends(current_device), db: Session = Depends(get_db)) -> dict:
    call = services.get_call_for_device(db, p, call_id)
    out = services.finish_call(db, p, call, outcome=body.outcome, observation=body.observation,
                               review_reasons=body.review_reasons, phone_idle=body.phone_idle)
    db.commit()
    return out


@router.post("/agent/calls/{call_id}/recording", status_code=201)
async def recording(call_id: str, file: UploadFile = File(...), sha256: str = Form(...), duration_ms: int = Form(0),
                    p: Principal = Depends(current_device), db: Session = Depends(get_db)) -> dict:
    call = services.get_call_for_device(db, p, call_id)
    data = await file.read(200 * 1024 * 1024)
    try:
        out = services.store_recording(db, p, call, data, sha256.lower(), duration_ms)
    except AppError:
        db.commit()  # 실패 상태 기록
        raise
    db.commit()
    return out


class AnalysisIn(BaseModel):
    """통화 후 AI 분석 결과(제안 전용). 서버는 이 값으로 날짜·상태를 바꾸지 않는다."""
    work_status: str = Field(pattern="^(WORKING|NOT_WORKING|UNKNOWN)$")
    end_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    available_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    preferences: list[str] = Field(default=[], max_length=10)
    issues: list[str] = Field(default=[], max_length=10)
    summary: str = Field(default="", max_length=300)
    evidence: str = Field(default="", max_length=300)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    model: str | None = Field(default=None, max_length=80)
    engine: str | None = Field(default=None, max_length=40)


@router.post("/agent/calls/{call_id}/analysis")
def call_analysis(call_id: str, body: AnalysisIn, p: Principal = Depends(current_device),
                  db: Session = Depends(get_db)) -> dict:
    call = services.get_call_for_device(db, p, call_id)
    data = body.model_dump()
    data["preferences"] = [x[:80] for x in data["preferences"]]
    data["issues"] = [x[:80] for x in data["issues"]]
    data["at"] = services.kst_text(services.utcnow())
    call.flags = dict(call.flags or {}, ai_analysis=data)  # 새 dict 로 바꿔 JSON 변경이 저장되게
    db.commit()
    return {"ok": True}


class RecFailIn(BaseModel):
    error: str = Field(max_length=500)


@router.post("/agent/calls/{call_id}/recording-failed")
def recording_failed(call_id: str, body: RecFailIn, p: Principal = Depends(current_device),
                     db: Session = Depends(get_db)) -> dict:
    call = services.get_call_for_device(db, p, call_id)
    services.mark_recording_failed(db, call, body.error)
    db.commit()
    return {"ok": True}


@router.get("/agent/voice/{pack_id}/clips/{clip}")
def voice_clip(pack_id: str, clip: str, p: Principal = Depends(current_device), db: Session = Depends(get_db)):
    """에이전트가 ARS 음원을 캐시하기 위해 내려받는다(캠페인 시작 당시 버전 고정)."""
    from fastapi.responses import Response

    vp = db.get(VoicePack, pack_id)
    if vp is None or vp.organization_id != p.org.id:
        raise AppError(404, "NOT_FOUND", "음원팩이 없습니다")
    c = (vp.manifest.get("clips") or {}).get(clip) or {}
    if not c.get("file"):
        raise AppError(404, "NOT_FOUND", "녹음 파일이 없습니다")
    path = services.packs_dir(p.org.id) / c["file"]
    if not path.is_file():
        raise AppError(404, "NOT_FOUND", "녹음 파일이 없습니다")
    return Response(path.read_bytes(), media_type="audio/wav")
