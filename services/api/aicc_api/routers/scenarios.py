"""시나리오 관리: 답 목록·끄기, AI 제안 승인, 답별 효과(2026-10-30 학습 기능)."""

from __future__ import annotations

import re

from aicc_core.scenario_set import EDITABLE_KINDS, ScenarioSet
from aicc_core.scenarios import BASE_SCENARIO_KEYS
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import learning, services
from ..db import get_db
from ..deps import Principal, audit, current_user, require
from ..errors import AppError
from ..models import ScenarioChange, ScenarioProposal

router = APIRouter(prefix="/api/v1", tags=["scenarios"])
BASE = set(BASE_SCENARIO_KEYS)


@router.get("/scenarios")
def list_scenarios(p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    require(p, "view_results")
    ext = learning.scenario_ext(db, p.org.id)
    full = ScenarioSet({**ext, "disabled": []})  # 꺼진 것도 목록에는 보이게
    off = set(ext["disabled"])
    pack = services.latest_pack(db, p.org.id, "female")
    clips = (pack.manifest.get("clips") or {}) if pack else {}
    out = []
    for s in full.scenarios:
        if s.kind not in EDITABLE_KINDS:
            continue
        src = "learned" if s.code.startswith("LRN_") else ("base" if s.key in BASE else "faq")
        out.append({"code": s.code, "key": s.key, "kind": s.kind, "text": s.say, "source": src,
                    "enabled": s.code not in off, "examples": (ext["examples"].get(s.code) or [])[:10],
                    "extra_keywords": len(ext["extra_keywords"].get(s.code) or []),
                    "has_recording": bool((clips.get(s.key) or {}).get("file")), "outcome": s.outcome, "review": s.review})
    return out


class ToggleIn(BaseModel):
    enabled: bool


@router.put("/scenarios/{code}")
def toggle(code: str, body: ToggleIn, p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    require(p, "settings")
    s = ScenarioSet(learning.scenario_ext(db, p.org.id) | {"disabled": []}).by_code.get(code)
    if s is None or s.kind not in EDITABLE_KINDS:
        raise AppError(404, "NOT_FOUND", "끄거나 켤 수 있는 답이 아닙니다")
    rows = db.scalars(select(ScenarioChange).where(ScenarioChange.organization_id == p.org.id,
                                                   ScenarioChange.code == code, ScenarioChange.action == "DISABLE",
                                                   ScenarioChange.active.is_(True))).all()
    if body.enabled:
        for r in rows:
            r.active = False
    elif not rows:
        db.add(ScenarioChange(organization_id=p.org.id, code=code, action="DISABLE", data={}, created_by=p.id))
    audit(db, p, "scenario.toggle", "scenario", None, {"code": code, "enabled": body.enabled})
    db.commit()
    return {"code": code, "enabled": body.enabled}


@router.get("/scenarios/stats")
def stats(days: int = 30, p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    require(p, "view_results")
    days = max(1, min(days, 365))
    c = learning.collect(db, p.org.id, days)
    rows = learning.answer_stats(db, p.org.id, days)
    total_ai = len(c["unmatched"])
    routed = sum(1 for x in c["unmatched"] if x["routed"])
    return {"days": days, "calls": c["calls"], "answers": rows,
            "ai": {"total": total_ai, "routed": routed, "generated": total_ai - routed,
                   "hangup_after": sum(1 for x in c["unmatched"] if x["hangup_after"])}}


def _prop_out(pr: ScenarioProposal) -> dict:
    return {"id": pr.id, "status": pr.status, "action": pr.action, "target_code": pr.target_code, "draft": pr.draft,
            "evidence": pr.evidence, "checks": pr.checks, "created_at": services.kst_text(pr.created_at),
            "decided_at": services.kst_text(pr.decided_at), "decision_note": pr.decision_note}


@router.get("/scenario-proposals")
def proposals(status: str = "PENDING", p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> list:
    require(p, "view_results")
    stmt = select(ScenarioProposal).where(ScenarioProposal.organization_id == p.org.id)
    if status != "ALL":
        stmt = stmt.where(ScenarioProposal.status == status)
    return [_prop_out(x) for x in db.scalars(stmt.order_by(ScenarioProposal.created_at.desc()).limit(200))]


class GenIn(BaseModel):
    days: int = Field(default=7, ge=1, le=60)


@router.post("/scenario-proposals/generate")
def generate(body: GenIn, p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    require(p, "settings")
    try:
        made = learning.generate(db, p.org, llm=learning.gemini_llm, days=body.days)
    except RuntimeError as e:
        raise AppError(502, "LEARNING_FAILED", str(e)[:300]) from e
    audit(db, p, "scenario.generate", "scenario", None, {"made": len(made)})
    db.commit()
    return {"made": len(made), "proposals": [_prop_out(x) for x in made]}


class EditIn(BaseModel):
    text: str | None = Field(default=None, max_length=150)
    kind: str | None = Field(default=None, pattern="^(INFO|REVIEW|PREF|END)$")
    outcome: str | None = Field(default=None, pattern="^(CALLBACK_REQUESTED|UNCONFIRMED)$")
    note: str | None = Field(default=None, max_length=10)
    examples: list[str] | None = Field(default=None, max_length=20)


class DecideIn(BaseModel):
    edits: EditIn | None = None
    note: str | None = Field(default=None, max_length=300)


def _get(db: Session, p: Principal, pid: str) -> ScenarioProposal:
    pr = db.get(ScenarioProposal, pid)
    if pr is None or pr.organization_id != p.org.id:
        raise AppError(404, "NOT_FOUND", "제안이 없습니다")
    return pr


@router.post("/scenario-proposals/{pid}/recheck")
def recheck(pid: str, body: DecideIn, p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """고친 문구로 자동 검사만 다시(저장하지 않음)."""
    require(p, "settings")
    pr = _get(db, p, pid)
    edits = {k: v for k, v in (body.edits.model_dump() if body.edits else {}).items() if v is not None}
    tmp = ScenarioProposal(action=pr.action, target_code=pr.target_code, draft=dict(pr.draft or {}, **edits))
    return {"checks": learning.check(tmp, learning.scenario_ext(db, p.org.id))}


@router.post("/scenario-proposals/{pid}/approve")
def approve(pid: str, body: DecideIn, p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    require(p, "settings")
    pr = _get(db, p, pid)
    edits = {k: v for k, v in (body.edits.model_dump() if body.edits else {}).items() if v is not None}
    try:
        ch = learning.approve(db, p.org, pr, actor_id=p.id, edits=edits or None, note=body.note)
    except ValueError as e:
        raise AppError(409, "PROPOSAL_REJECTED_BY_CHECK", str(e)) from e
    voice = {}
    if ch.action == "LEARNED":
        voice = _add_clip(db, p, ch.code, ch.data["text"])
    audit(db, p, "scenario.approve", "scenario", pr.id, {"code": ch.code, "action": ch.action})
    db.commit()
    return {"proposal": _prop_out(pr), "code": ch.code, "voice": voice}


@router.post("/scenario-proposals/{pid}/reject")
def reject(pid: str, body: DecideIn, p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    require(p, "settings")
    pr = _get(db, p, pid)
    if pr.status != "PENDING":
        raise AppError(409, "ALREADY_DECIDED", "이미 처리된 제안입니다")
    pr.status = "REJECTED"
    pr.decided_by = p.id
    pr.decided_at = services.utcnow()
    pr.decision_note = body.note
    audit(db, p, "scenario.reject", "scenario", pr.id, {})
    db.commit()
    return _prop_out(pr)


def _add_clip(db: Session, p: Principal, code: str, text: str) -> dict:
    """새 답을 음원팩(여성·남성)에 넣고, 쓰던 음성 엔진으로 바로 녹음해 본다(실패하면 '빈칸만 채우기'로)."""
    from aicc_core.voicepack import with_optional

    from .. import tts
    from .admin import _apply_company, _new_version, _read_agent_env, _store_clip
    key = "sc_" + code.lower()
    out = {}
    env = _read_agent_env()
    for pack_key in ("female", "male"):
        cur = services.latest_pack(db, p.org.id, pack_key)
        if cur is None:
            continue
        m = with_optional(cur.manifest)
        m["clips"][key] = {"file": None, "text": text}
        engine = tts.pack_engine(pack_key)
        made = False
        if engine in ("gemini", "gemini-live", "melo", "voicestudio"):
            try:
                said = _apply_company(text, p.org.company_name.strip())
                wav = tts.synthesize(engine, said, pack_key, api_key=env.get("GEMINI_API_KEY") or None)
                m["clips"][key].update(_store_clip(p, pack_key, key, wav), text=said,
                                       source=tts.voice_tag(pack_key, engine))
                made = True
            except Exception as e:  # noqa: BLE001
                out[pack_key + "_error"] = re.sub(r"\s+", " ", str(e))[:200]
        if made:
            m["approved_by"] = None  # 새 녹음이 들어감 → 들어 보고 다시 승인
        # 녹음 없이 빈 칸만 생긴 경우는 들을 것이 없으므로 기존 승인을 유지(리뷰 2026-10-30: 승인만 해도 새 캠페인이 막힘)
        _new_version(db, p, pack_key, m)
        out[pack_key] = "녹음됨(승인 필요)" if made else "녹음 필요(빈칸만 채우기)"
    return out
