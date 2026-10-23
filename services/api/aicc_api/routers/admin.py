"""관리자: 운영 정책, 음원팩, 단말 등록·실통화 검증 기록, 감사 로그, 보관기간 정리."""

from __future__ import annotations

import io
import os
import re
import wave

from aicc_core.voicepack import DATE_PIECE_KEYS, REQUIRED_KEYS, check_pack, default_manifest, manifest_digest
from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import services
from ..db import get_db
from ..deps import Principal, audit, current_user, require
from ..errors import AppError
from ..models import AuditLog, Device, User, VoicePack
from ..security import new_token, token_hash

router = APIRouter(prefix="/api/v1", tags=["admin"])

POLICY_KEYS = {
    "retention_days": int, "call_window_start": str, "call_window_end": str, "max_call_seconds": int,
    "max_attempts": int, "retry_interval_minutes": int, "recording_notice_confirmed": bool,
    "recording_notice_text": str, "ai_external_transfer_confirmed": bool, "silence_seconds": int,
}


@router.get("/settings")
def get_settings_(p: Principal = Depends(current_user)) -> dict:
    require(p, "view_results")
    return {"organization": {"name": p.org.name, "environment": p.org.environment, "company_name": p.org.company_name},
            "policies": p.org.policies or {}, "policy_problems": services.policy_problems(p.org)}


class SettingsIn(BaseModel):
    company_name: str | None = Field(default=None, max_length=200)
    policies: dict = {}


@router.put("/settings")
def put_settings(body: SettingsIn, p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    require(p, "settings")
    pol = dict(p.org.policies or {})
    for k, v in body.policies.items():
        if k not in POLICY_KEYS:
            raise AppError(422, "UNKNOWN_POLICY", f"알 수 없는 정책 항목: {k}")
        typ = POLICY_KEYS[k]
        if v is None or v == "":
            pol.pop(k, None)
            continue
        try:
            v = typ(v) if typ is not bool else bool(v)
        except (TypeError, ValueError) as e:
            raise AppError(422, "POLICY_INVALID", f"{k} 값이 올바르지 않습니다") from e
        if k in ("call_window_start", "call_window_end") and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise AppError(422, "POLICY_INVALID", "시간은 HH:MM 형식입니다")
        if typ is int and v < 0:
            raise AppError(422, "POLICY_INVALID", f"{k} 값이 올바르지 않습니다")
        pol[k] = v
    org = db.merge(p.org)
    org.policies = pol
    if body.company_name is not None:
        org.company_name = body.company_name.strip()
    audit(db, p, "settings.update", "organization", org.id, {"keys": sorted(body.policies.keys())})
    db.commit()
    return {"ok": True, "policy_problems": services.policy_problems(org)}


# ------------------ 음원팩 ------------------
def _pack_out(db: Session, p: Principal, vp: VoicePack) -> dict:
    chk = check_pack(vp.manifest, services.packs_dir(p.org.id))
    m = vp.manifest
    return {"id": vp.id, "key": vp.pack_key, "version": vp.version, "digest": vp.digest,
            "speaker_name": m.get("speaker_name"), "gender": m.get("gender"), "company_name": m.get("company_name"),
            "approved_by": m.get("approved_by"), "dialable": chk.dialable, "problems": chk.problems,
            "missing_count": len(chk.missing),
            "clips": {k: {"text": c.get("text"), "has_file": bool(c.get("file")),
                          "duration_ms": c.get("duration_ms")} for k, c in m.get("clips", {}).items()},
            "required_keys": list(REQUIRED_KEYS), "date_piece_keys": list(DATE_PIECE_KEYS)}


@router.get("/voice-packs")
def list_packs(p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    require(p, "view_results")
    out = []
    for key in ("female", "male"):
        vp = services.latest_pack(db, p.org.id, key)
        if vp:
            out.append(_pack_out(db, p, vp))
    return out


def _new_version(db: Session, p: Principal, key: str, manifest: dict) -> VoicePack:
    cur = services.latest_pack(db, p.org.id, key)
    ver = (cur.version + 1) if cur else 1
    manifest = dict(manifest, version=ver)
    vp = VoicePack(organization_id=p.org.id, pack_key=key, version=ver, manifest=manifest,
                   digest=manifest_digest(manifest), created_by=p.id)
    db.add(vp)
    db.flush()
    return vp


@router.post("/voice-packs/init", status_code=201)
def init_packs(p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    require(p, "settings")
    out = []
    for key, gender, speaker in (("female", "F", "김태희 실장"), ("male", "M", "")):
        if services.latest_pack(db, p.org.id, key) is None:
            m = default_manifest(key, gender, speaker)
            m["company_name"] = p.org.company_name
            vp = _new_version(db, p, key, m)
            audit(db, p, "voicepack.init", "voice_pack", vp.id, {"key": key})
            out.append(_pack_out(db, p, vp))
    db.commit()
    return out


class PackPatch(BaseModel):
    speaker_name: str | None = Field(default=None, max_length=50)
    texts: dict[str, str] = {}


@router.put("/voice-packs/{key}")
def update_pack(key: str, body: PackPatch, p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    require(p, "settings")
    cur = services.latest_pack(db, p.org.id, key)
    if cur is None:
        raise AppError(404, "NOT_FOUND", "음원팩이 없습니다")
    m = {**cur.manifest, "clips": {k: dict(v) for k, v in cur.manifest["clips"].items()}}
    if body.speaker_name is not None:
        m["speaker_name"] = body.speaker_name.strip()
    for k, t in body.texts.items():
        if k not in m["clips"]:
            raise AppError(422, "UNKNOWN_CLIP", f"알 수 없는 음원 키: {k}")
        if m["clips"][k].get("text") != t:
            m["clips"][k]["text"] = t
            m["clips"][k]["file"] = None  # 문구가 바뀌면 기존 녹음은 무효
    m["company_name"] = p.org.company_name
    m["approved_by"] = None
    vp = _new_version(db, p, key, m)
    audit(db, p, "voicepack.update", "voice_pack", vp.id, {"key": key, "texts": sorted(body.texts)})
    db.commit()
    return _pack_out(db, p, vp)


@router.post("/voice-packs/{key}/clips/{clip}")
async def upload_clip(key: str, clip: str, file: UploadFile = File(...), p: Principal = Depends(current_user),
                      db: Session = Depends(get_db)) -> dict:
    require(p, "settings")
    cur = services.latest_pack(db, p.org.id, key)
    if cur is None or clip not in cur.manifest.get("clips", {}):
        raise AppError(404, "NOT_FOUND", "음원 키가 없습니다")
    data = await file.read(20 * 1024 * 1024)
    try:
        with wave.open(io.BytesIO(data)) as w:
            if w.getnchannels() != 1 or w.getsampwidth() != 2:
                raise AppError(422, "CLIP_FORMAT", "16비트 모노 WAV만 지원합니다")
            dur = int(w.getnframes() * 1000 / w.getframerate())
            rate = w.getframerate()
    except wave.Error as e:
        raise AppError(422, "CLIP_FORMAT", "WAV 파일을 읽을 수 없습니다") from e
    import hashlib
    h = hashlib.sha256(data).hexdigest()[:16]
    fname = f"{key}/{clip}-{h}.wav"
    path = services.packs_dir(p.org.id) / fname
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    m = {**cur.manifest, "clips": {k: dict(v) for k, v in cur.manifest["clips"].items()}}
    m["clips"][clip].update({"file": fname, "duration_ms": dur, "sample_rate": rate, "sha256": h})
    m["approved_by"] = None
    m["company_name"] = p.org.company_name
    vp = _new_version(db, p, key, m)
    audit(db, p, "voicepack.clip_upload", "voice_pack", vp.id, {"clip": clip})
    db.commit()
    return _pack_out(db, p, vp)


@router.get("/voice-packs/{key}/clips/{clip}")
def get_clip(key: str, clip: str, p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    require(p, "view_results")
    cur = services.latest_pack(db, p.org.id, key)
    c = (cur.manifest.get("clips", {}).get(clip) if cur else None) or {}
    if not c.get("file"):
        raise AppError(404, "NOT_FOUND", "녹음 파일이 없습니다")
    return Response((services.packs_dir(p.org.id) / c["file"]).read_bytes(), media_type="audio/wav")


@router.post("/voice-packs/{key}/approve")
def approve(key: str, p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    require(p, "settings")
    cur = services.latest_pack(db, p.org.id, key)
    if cur is None:
        raise AppError(404, "NOT_FOUND", "음원팩이 없습니다")
    m = dict(cur.manifest, company_name=p.org.company_name, approved_by=p.user.username)
    chk = check_pack(m, services.packs_dir(p.org.id))
    if not chk.dialable:
        raise AppError(422, "PACK_NOT_READY", "승인할 수 없습니다", {"problems": chk.problems})
    vp = _new_version(db, p, key, m)
    audit(db, p, "voicepack.approve", "voice_pack", vp.id, {"key": key})
    db.commit()
    return _pack_out(db, p, vp)


# ------------------ 단말 ------------------
class DeviceIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: str = Field(pattern="^(mock|a24)$")
    bind_local: bool = False  # 이 PC의 통합 실행 프로그램 에이전트에 토큰을 바로 연결


class ProofIn(BaseModel):
    rx_verified: bool = False
    tx_verified: bool = False
    pc_silent_verified: bool = False
    recording_verified: bool = False
    test_calls: int = Field(ge=0, le=1000)
    remote_listener: str = Field(min_length=1, max_length=100)
    environment: dict = {}  # 모델 세부번호, Android/One UI, 통신사, PC OS, 케이블, 오디오 장치 ID
    notes: str = Field(default="", max_length=2000)


def _dev_out(d: Device) -> dict:
    return {"id": d.id, "name": d.name, "kind": d.kind, "capabilities": d.capabilities or {},
            "probe_report": d.probe_report or {}, "proof": d.proof or {}, "proof_version": d.proof_version,
            "verified_at": services.kst_text(d.verified_at), "heartbeat_at": services.kst_text(d.heartbeat_at),
            "phone_state": d.phone_state, "active_call_id": d.active_call_id, **services.device_ready(d)}


@router.get("/devices")
def list_devices(p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    require(p, "view_results")
    return [_dev_out(d) for d in db.scalars(select(Device).where(Device.organization_id == p.org.id))]


@router.post("/devices", status_code=201)
def create_device(body: DeviceIn, p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    require(p, "settings")
    if body.kind == "mock" and p.org.environment != "sandbox":
        raise AppError(409, "MOCK_NOT_ALLOWED", "운영 조직에는 MOCK 단말을 등록할 수 없습니다")
    token = new_token()
    d = Device(organization_id=p.org.id, name=body.name, kind=body.kind, token_hash=token_hash(token))
    db.add(d)
    db.flush()
    audit(db, p, "device.create", "device", d.id, {"kind": body.kind, "bind_local": body.bind_local})
    db.commit()
    bound = False
    if body.bind_local and os.environ.get("AICC_LOCAL_AGENT") == "1":
        _write_agent_env(token, body.kind)
        bound = True
    return {"id": d.id, "token": token, "bound_local": bound,
            "note": "이 PC 에이전트에 연결했습니다." if bound else "토큰은 지금 한 번만 표시됩니다. 에이전트 설정에 입력하세요."}


def _write_agent_env(token: str, kind: str) -> None:
    from ..config import get_settings
    path = get_settings().data_dir / "agent.env"
    old = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                old[k] = v
    old.update({"AICC_DEVICE_TOKEN": token, "AICC_DEVICE_KIND": kind})
    path.write_text("# 통합 실행 프로그램 에이전트 설정(자동 생성). 저장소에 올리지 마십시오.\n"
                    + "".join(f"{k}={v}\n" for k, v in old.items()), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


@router.post("/devices/{dev_id}/rotate-token")
def rotate(dev_id: str, p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    require(p, "settings")
    d = db.get(Device, dev_id)
    if d is None or d.organization_id != p.org.id:
        raise AppError(404, "NOT_FOUND", "단말이 없습니다")
    token = new_token()
    d.token_hash = token_hash(token)
    audit(db, p, "device.rotate_token", "device", d.id, {})
    db.commit()
    return {"token": token}


@router.post("/devices/{dev_id}/proof")
def set_proof(dev_id: str, body: ProofIn, p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """사람이 원격 단말로 청취 확인한 실통화 시험 결과를 기록한다(설계 11 A24 필수 실통화 시험)."""
    require(p, "settings")
    d = db.get(Device, dev_id)
    if d is None or d.organization_id != p.org.id:
        raise AppError(404, "NOT_FOUND", "단말이 없습니다")
    if d.kind != "a24":
        raise AppError(409, "NOT_REAL_DEVICE", "MOCK 단말에는 실통화 검증을 기록할 수 없습니다")
    if not d.probe_report:
        raise AppError(409, "PROBE_REQUIRED", "먼저 PC에서 aicc-probe 진단을 실행해 결과를 올려야 합니다")
    all_ok = body.rx_verified and body.tx_verified and body.pc_silent_verified and body.recording_verified
    if all_ok and body.test_calls < 10:
        raise AppError(422, "INSUFFICIENT_TESTS", "승인된 테스트 통화 10회 이상이 필요합니다(설계 08 H1)")
    d.proof = body.model_dump()
    d.proof_version += 1
    d.verified_by = p.id
    d.verified_at = services.utcnow()
    audit(db, p, "device.proof", "device", d.id, body.model_dump())
    db.commit()
    return _dev_out(d)


# ------------------ 감사·정리 ------------------
@router.get("/audit")
def audit_logs(limit: int = 200, p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    require(p, "settings")
    users = {u.id: u.username for u in db.scalars(select(User).where(User.organization_id == p.org.id))}
    devs = {d.id: d.name for d in db.scalars(select(Device).where(Device.organization_id == p.org.id))}
    return [{"at": services.kst_text(a.occurred_at), "actor": users.get(a.actor_id) or devs.get(a.actor_id) or a.actor_id,
             "actor_type": a.actor_type, "action": a.action, "resource": f"{a.resource_type}:{a.resource_id or ''}",
             "detail": a.detail}
            for a in db.scalars(select(AuditLog).where(AuditLog.organization_id == p.org.id)
                                .order_by(AuditLog.occurred_at.desc()).limit(min(limit, 1000)))]


@router.post("/admin/purge-expired")
def purge(p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    require(p, "settings")
    n = services.purge_expired(db, p)
    db.commit()
    return {"expired": n}
