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
    "pilot_enabled": bool, "test_numbers": list,
}


@router.get("/settings")
def get_settings_(p: Principal = Depends(current_user)) -> dict:
    require(p, "view_results")
    return {"organization": {"name": p.org.name, "environment": p.org.environment, "company_name": p.org.company_name},
            "policies": p.org.policies or {}, "policy_problems": services.policy_problems(p.org)}


class SettingsIn(BaseModel):
    company_name: str | None = Field(default=None, max_length=200)
    environment: str | None = Field(default=None, pattern="^(sandbox|production)$")
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
        if typ is list:
            from aicc_core.phone import normalize_phone
            items = v if isinstance(v, list) else str(v).replace(",", "\n").splitlines()
            clean = []
            for it in items:
                if not str(it).strip():
                    continue
                r = normalize_phone(str(it).strip())
                if not r.ok:
                    raise AppError(422, "POLICY_INVALID", f"시험 번호 형식 오류: {str(it)[:20]}")
                clean.append(r.normalized)
            pol[k] = sorted(set(clean))[:20]
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
    if body.environment and body.environment != org.environment:
        from ..models import Campaign
        running = db.scalar(select(Campaign).where(Campaign.organization_id == org.id,
                                                   Campaign.state.in_(("RUNNING", "STOPPING"))))
        if running:
            raise AppError(409, "CAMPAIGN_RUNNING", "진행 중인 캠페인을 끝낸 뒤 전환하세요")
        audit(db, p, "org.environment", "organization", org.id, {"from": org.environment, "to": body.environment})
        org.environment = body.environment  # 기존 MOCK 결과는 is_mock 표시가 그대로 남는다
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
    if services.latest_pack(db, p.org.id, "female") is None:
        ensure_default_packs(db, p)  # '기본 음원팩 만들기' 없이도 항상 문구표가 보이게 한다
        db.commit()
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
    out = [_pack_out(db, p, vp) for vp in ensure_default_packs(db, p)]
    db.commit()
    return out


def ensure_default_packs(db: Session, p: Principal) -> list[VoicePack]:
    made = []
    for key, gender, speaker in (("female", "F", "김태희 실장"), ("male", "M", "")):
        if services.latest_pack(db, p.org.id, key) is None:
            m = default_manifest(key, gender, speaker)
            m["company_name"] = p.org.company_name
            vp = _new_version(db, p, key, m)
            audit(db, p, "voicepack.init", "voice_pack", vp.id, {"key": key})
            made.append(vp)
    return made


def _apply_company(text: str, company: str) -> str:
    return text.replace("[회사명]", company) if company else text


def _store_clip(p: Principal, key: str, clip: str, wav16k: bytes) -> dict:
    import hashlib
    h = hashlib.sha256(wav16k).hexdigest()[:16]
    fname = f"{key}/{clip}-{h}.wav"
    path = services.packs_dir(p.org.id) / fname
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(wav16k)
    with wave.open(io.BytesIO(wav16k)) as w:
        dur = int(w.getnframes() * 1000 / w.getframerate())
    return {"file": fname, "duration_ms": dur, "sample_rate": 16000, "sha256": h}


def _to_wav16k(data: bytes) -> bytes:
    """WAV(아무 채널·샘플레이트)·MP3·FLAC 을 16kHz 16bit 모노 WAV 로 바꾼다."""
    try:
        with wave.open(io.BytesIO(data)) as w:
            if w.getnchannels() == 1 and w.getsampwidth() == 2 and w.getframerate() == 16000:
                return data
    except (wave.Error, EOFError):
        pass
    try:
        import miniaudio
    except ImportError as e:
        raise AppError(422, "CLIP_FORMAT", "16kHz 16비트 모노 WAV 만 올릴 수 있습니다(변환 모듈 없음)") from e
    try:
        snd = miniaudio.decode(data, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=16000)
    except miniaudio.DecodeError as e:
        raise AppError(422, "CLIP_FORMAT", "WAV·MP3·FLAC 파일만 올릴 수 있습니다(m4a 는 MP3/WAV 로 저장해 주세요)") from e
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(snd.samples.tobytes())
    return buf.getvalue()


class TTSIn(BaseModel):
    engine: str = Field(default="edge", pattern="^(edge|sapi|test)$")
    only_missing: bool = False


@router.post("/voice-packs/{key}/tts")
def generate_tts(key: str, body: TTSIn, p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """회사명을 넣은 문구로 음원팩 전체(필수 13 + 날짜 조각 43)를 자동 음성으로 만든다. 승인은 따로 한다."""
    from .. import tts

    require(p, "settings")
    if body.engine == "test" and p.org.environment != "sandbox":
        raise AppError(422, "ENGINE_NOT_ALLOWED", "시험용 엔진은 시험 조직에서만 쓸 수 있습니다")
    if not p.org.company_name.strip():
        raise AppError(422, "COMPANY_REQUIRED", "먼저 설정 > 운영 정책에서 회사명을 입력하세요")
    cur = services.latest_pack(db, p.org.id, key)
    if cur is None:
        raise AppError(404, "NOT_FOUND", "음원팩이 없습니다")
    m = {**cur.manifest, "clips": {k: dict(v) for k, v in cur.manifest["clips"].items()}}
    if not m.get("speaker_name"):
        raise AppError(422, "SPEAKER_REQUIRED", "안내 인물 이름을 먼저 저장하세요")
    made, failed = 0, []
    for clip, c in m["clips"].items():
        if body.only_missing and c.get("file"):
            continue
        text = _apply_company(c.get("text") or "", p.org.company_name.strip())
        if not text.strip():
            failed.append({"clip": clip, "error": "문구 없음"})
            continue
        try:
            wav16k = tts.synthesize(body.engine, text, key)
        except tts.TTSError as e:
            failed.append({"clip": clip, "error": str(e)})
            if made == 0:  # 첫 문구부터 실패하면 같은 원인이므로 바로 중단
                break
            continue
        c.update(_store_clip(p, key, clip, wav16k), text=text, source=f"tts:{body.engine}")
        made += 1
    if made:
        m["company_name"] = p.org.company_name
        m["approved_by"] = None
        vp = _new_version(db, p, key, m)
        audit(db, p, "voicepack.tts", "voice_pack", vp.id, {"key": key, "engine": body.engine, "made": made,
                                                            "failed": len(failed)})
        db.commit()
        out = _pack_out(db, p, vp)
    else:
        out = _pack_out(db, p, cur)
    if failed and not made:
        raise AppError(502, "TTS_FAILED", failed[0]["error"], {"failed": failed[:5]})
    return {**out, "generated": made, "failed": failed}


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
    wav16k = _to_wav16k(data)
    m = {**cur.manifest, "clips": {k: dict(v) for k, v in cur.manifest["clips"].items()}}
    m["clips"][clip].update(_store_clip(p, key, clip, wav16k), source="upload")
    m["clips"][clip]["text"] = _apply_company(m["clips"][clip].get("text") or "", p.org.company_name.strip())
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
    if body.kind == "mock" and p.org.environment != "sandbox":  # 운영 조직에는 MOCK 등록 불가
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


def _agent_env_path():  # noqa: ANN202
    from ..config import get_settings
    return get_settings().data_dir / "agent.env"


def _read_agent_env() -> dict[str, str]:
    path = _agent_env_path()
    old: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                old[k.strip()] = v.strip()
    return old


def _write_agent_env(token: str | None, kind: str | None, extra: dict[str, str | None] | None = None) -> None:
    path = _agent_env_path()
    old = _read_agent_env()
    if token:
        old.update({"AICC_DEVICE_TOKEN": token, "AICC_DEVICE_KIND": kind or "mock"})
    for k, v in (extra or {}).items():
        if v is None or v == "":
            old.pop(k, None)
        else:
            old[k] = v.replace("\n", " ").strip()
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


# ------------------ 이 PC 에이전트 설정(통합 실행 프로그램 전용) ------------------
AGENT_KEYS = {"AICC_STT_ENGINE", "AICC_WHISPER_MODEL", "AICC_TX_DEVICE", "AICC_GEMINI_MODEL", "GEMINI_API_KEY",
              "AICC_ADB_SERIAL"}


def _require_local() -> None:
    if os.environ.get("AICC_LOCAL_AGENT") != "1":
        raise AppError(409, "NOT_LOCAL", "이 기능은 PC 통합 실행 프로그램(AICC 아이콘)에서만 쓸 수 있습니다")


@router.get("/local-agent")
def local_agent(p: Principal = Depends(current_user)) -> dict:
    require(p, "settings")
    _require_local()
    env = _read_agent_env()
    status_file = _agent_env_path().parent / "agent_status.txt"
    outputs: list[dict] | None = None
    try:
        import sounddevice as sd
        default = sd.query_devices(kind="output")["name"]
        outputs = [{"index": i, "name": d["name"], "is_default": d["name"] == default}
                   for i, d in enumerate(sd.query_devices()) if d["max_output_channels"] > 0]
    except Exception:  # noqa: BLE001
        outputs = None
    return {
        "device_kind": env.get("AICC_DEVICE_KIND"), "has_token": bool(env.get("AICC_DEVICE_TOKEN")),
        "stt_engine": env.get("AICC_STT_ENGINE") or "faster-whisper",
        "whisper_model": env.get("AICC_WHISPER_MODEL") or "small",
        "tx_device": env.get("AICC_TX_DEVICE") or "", "gemini_model": env.get("AICC_GEMINI_MODEL") or "",
        "uplink_jar": env.get("AICC_UPLINK_JAR") or "",
        "tx_adb_target": env.get("AICC_TX_ADB_TARGET") or "", "tx_rate": env.get("AICC_TX_RATE") or "16000",
        "tx_channels": env.get("AICC_TX_CHANNELS") or "1", "tx_start_cmd": env.get("AICC_TX_START_CMD") or "",
        "has_gemini_key": bool(env.get("GEMINI_API_KEY")),  # 키 원문은 돌려주지 않는다
        "status": status_file.read_text(encoding="utf-8") if status_file.is_file() else None,
        "output_devices": outputs,
    }


class LocalAgentIn(BaseModel):
    stt_engine: str | None = Field(default=None, pattern="^(faster-whisper)$")
    whisper_model: str | None = Field(default=None, pattern="^(tiny|base|small|medium|large-v3|large-v3-turbo)$")
    tx_device: str | None = Field(default=None, max_length=200)
    gemini_model: str | None = Field(default=None, max_length=100, pattern=r"^$|^(models/)?gemini[A-Za-z0-9_.\-]*$")
    gemini_api_key: str | None = Field(default=None, max_length=200)  # 빈 문자열이면 삭제, None 이면 유지
    tx_adb_target: str | None = Field(default=None, max_length=140,
                                pattern=r"^$|^(tcp:[0-9]+|localabstract:[A-Za-z0-9_.\-]+|localfilesystem:[A-Za-z0-9_./\-]+)$")
    tx_rate: str | None = Field(default=None, pattern="^(8000|16000|24000|32000|44100|48000)$")
    uplink_jar: str | None = Field(default=None, max_length=400)
    tx_channels: str | None = Field(default=None, pattern="^(1|2)$")
    tx_start_cmd: str | None = Field(default=None, max_length=300, pattern=r"^$|^shell [A-Za-z0-9_ ./:=\-]+$")


@router.put("/local-agent")
def put_local_agent(body: LocalAgentIn, p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    require(p, "settings")
    _require_local()
    extra: dict[str, str | None] = {}
    if body.stt_engine is not None:
        extra["AICC_STT_ENGINE"] = body.stt_engine
    if body.whisper_model is not None:
        extra["AICC_WHISPER_MODEL"] = body.whisper_model
    if body.tx_device is not None:
        extra["AICC_TX_DEVICE"] = body.tx_device
    if body.gemini_model is not None:
        extra["AICC_GEMINI_MODEL"] = body.gemini_model
    if body.gemini_api_key is not None:
        extra["GEMINI_API_KEY"] = body.gemini_api_key
    if body.uplink_jar:
        from pathlib import Path as _P
        jp = _P(body.uplink_jar.strip().strip('"'))
        if jp.suffix.lower() != ".jar" or not jp.is_file():
            raise AppError(422, "JAR_NOT_FOUND", f"송출 모듈 JAR 파일을 찾을 수 없습니다: {jp}")
        body.uplink_jar = str(jp)
    for field, key in (("uplink_jar", "AICC_UPLINK_JAR"), ("tx_adb_target", "AICC_TX_ADB_TARGET"), ("tx_rate", "AICC_TX_RATE"),
                       ("tx_channels", "AICC_TX_CHANNELS"), ("tx_start_cmd", "AICC_TX_START_CMD")):
        v = getattr(body, field)
        if v is not None:
            extra[key] = v
    _write_agent_env(None, None, extra)  # 파일이 바뀌면 통합 실행 프로그램이 에이전트를 다시 시작한다
    audit(db, p, "local_agent.update", "organization", p.org.id,
          {k: ("***" if k == "GEMINI_API_KEY" else v) for k, v in extra.items()})
    db.commit()
    return {"ok": True}
