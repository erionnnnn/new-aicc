"""업무 규칙(서버 측). 라우터는 입출력만 담당하고 판단은 여기서 한다."""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aicc_core.intents import interpret
from aicc_core.phone import mask_phone
from aicc_core.voicepack import check_pack
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from . import excel_io
from .deps import Principal, audit
from .errors import AppError
from .models import (
    CallEvent,
    CallSession,
    Campaign,
    CampaignTarget,
    Caregiver,
    ContactBlock,
    Device,
    ImportBatch,
    ImportRow,
    Organization,
    Recording,
    ScheduleChange,
    ScheduleObservation,
    VoicePack,
)
from .security import decrypt, decrypt_json, delete_object, encrypt, encrypt_json, phone_hmac, put_object

KST = ZoneInfo("Asia/Seoul")
LEASE_SECONDS = 15 * 60


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def today_kst() -> date:
    return datetime.now(KST).date()


def aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def kst_text(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return aware(dt).astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")


# ======================== 연락거부 ========================
def active_block(db: Session, org_id: str, phone_hash: str | None) -> ContactBlock | None:
    if not phone_hash:
        return None
    return db.scalar(select(ContactBlock).where(
        ContactBlock.organization_id == org_id, ContactBlock.phone_hash == phone_hash,
        ContactBlock.revoked_at.is_(None)))


def block_contact(db: Session, p: Principal, *, caregiver: Caregiver, reason: str, call_id: str | None = None,
                  evidence_event_id: str | None = None) -> ContactBlock:
    if not caregiver.phone_hash:
        raise AppError(422, "PHONE_MISSING", "연락처가 없는 간병인입니다")
    existing = active_block(db, p.org.id, caregiver.phone_hash)
    if existing:
        return existing
    blk = ContactBlock(organization_id=p.org.id, phone_hash=caregiver.phone_hash, phone_masked=caregiver.phone_masked,
                       caregiver_id=caregiver.id, reason=reason, source_call_id=call_id,
                       evidence_event_id=evidence_event_id, blocked_by=p.id)
    db.add(blk)
    db.flush()
    # 대기 중인 모든 캠페인 대상에서 같은 번호를 즉시 제외(재시도 목록 포함)
    same = select(Caregiver.id).where(Caregiver.organization_id == p.org.id,
                                      Caregiver.phone_hash == caregiver.phone_hash)
    db.execute(update(CampaignTarget).where(CampaignTarget.caregiver_id.in_(same),
                                            CampaignTarget.state == "QUEUED")
               .values(state="BLOCKED", last_outcome="REFUSED"))
    audit(db, p, "contact.block", "contact_block", blk.id, {"call_id": call_id, "reason": reason[:200]})
    return blk


def revoke_block(db: Session, p: Principal, block_id: str, reason: str) -> ContactBlock:
    blk = db.get(ContactBlock, block_id)
    if blk is None or blk.organization_id != p.org.id:
        raise AppError(404, "NOT_FOUND", "차단 기록이 없습니다")
    if blk.revoked_at:
        raise AppError(409, "ALREADY_REVOKED", "이미 해제된 차단입니다")
    if not reason or len(reason.strip()) < 5:
        raise AppError(422, "REASON_REQUIRED", "해제 사유와 근거를 5자 이상 입력하세요")
    blk.revoked_at = utcnow()
    blk.revoked_by = p.id
    blk.revoke_reason = reason.strip()
    audit(db, p, "contact.unblock", "contact_block", blk.id, {"reason": reason[:500]})
    return blk


# ======================== 업로드 ========================
def upload_import(db: Session, p: Principal, name: str, data: bytes) -> tuple[ImportBatch, dict]:
    parsed = excel_io.parse_workbook(name, data)
    fh = hashlib.sha256(data).hexdigest()
    dup = db.scalar(select(ImportBatch).where(ImportBatch.organization_id == p.org.id, ImportBatch.file_hash == fh)
                    .order_by(ImportBatch.created_at.desc()))
    key = put_object("imports", data)
    batch = ImportBatch(organization_id=p.org.id, file_name=name[:300], file_hash=fh, object_key=key,
                        sheet_name=parsed.sheet_name, header_row=parsed.header_row, headers=parsed.headers,
                        row_count=sum(1 for i, _ in enumerate(parsed.rows) if i + 1 > parsed.header_row),
                        created_by=p.id)
    db.add(batch)
    db.flush()
    for i, row in enumerate(parsed.rows):
        db.add(ImportRow(batch_id=batch.id, row_no=i + 1, raw_enc=encrypt_json(row)))
    audit(db, p, "import.upload", "import_batch", batch.id, {"rows": batch.row_count, "file_hash": fh[:12]})
    suggestion = excel_io.suggest_mapping(parsed.headers)
    return batch, {
        "import_id": batch.id, "file_name": batch.file_name, "sheet_name": parsed.sheet_name,
        "header_row": parsed.header_row, "detected_columns": parsed.headers, "row_count": batch.row_count,
        "suggested_mapping": suggestion,
        "duplicate_of": {"import_id": dup.id, "created_at": kst_text(dup.created_at)} if dup else None,
        "preview": _preview(parsed.rows, parsed.header_row, suggestion),
    }


def _preview(rows: list, header_row: int, mapping: dict) -> list[dict]:
    out = []
    for i in range(header_row, min(len(rows), header_row + 8)):
        r = rows[i]

        def cell(f: str, r=r) -> object:
            j = mapping.get(f)
            if j is None or j >= len(r):
                return None
            v = excel_io.json_to_cell(r[j])
            return v.isoformat() if hasattr(v, "isoformat") else v
        out.append({"row_no": i + 1, "external_no": cell("external_no"), "name": cell("name"),
                    "phone": _mask_raw(cell("phone")), "date": cell("date")})
    return out


def _mask_raw(v: object) -> str | None:
    if v is None:
        return None
    s = str(v)
    return s[:3] + "****" + s[-4:] if len(s) >= 8 else "****"


def _load_rows(db: Session, batch: ImportBatch) -> tuple[list[ImportRow], excel_io.ParsedSheet]:
    rows = list(db.scalars(select(ImportRow).where(ImportRow.batch_id == batch.id).order_by(ImportRow.row_no)))
    raw = [decrypt_json(r.raw_enc) for r in rows]
    width = max((len(r) for r in raw), default=0)
    parsed = excel_io.ParsedSheet(batch.sheet_name, batch.header_row, batch.headers, raw, max(width, len(batch.headers)))
    return rows, parsed


def get_batch(db: Session, p: Principal, batch_id: str) -> ImportBatch:
    b = db.get(ImportBatch, batch_id)
    if b is None or b.organization_id != p.org.id:
        raise AppError(404, "NOT_FOUND", "업로드 기록이 없습니다")
    return b


def validate_import(db: Session, p: Principal, batch: ImportBatch, mapping: dict, date_semantics: str | None) -> dict:
    if batch.status == "COMMITTED":
        raise AppError(409, "ALREADY_COMMITTED", "이미 저장된 업로드입니다")
    if date_semantics not in excel_io.DATE_SEMANTICS:
        raise AppError(422, "DATE_SEMANTICS_REQUIRED",
                       "근무확정일 열의 의미를 선택하세요(다음 근무 가능일 / 실제 배정 확정일)")
    width = len(batch.headers)
    clean: dict[str, int | None] = {}
    for f in excel_io.REQUIRED_FIELDS:
        v = mapping.get(f)
        if v is not None and (not isinstance(v, int) or v < 0 or v >= width):
            raise AppError(422, "MAPPING_INVALID", "매핑한 열 번호가 올바르지 않습니다", {"field": f})
        clean[f] = v
    rows, parsed = _load_rows(db, batch)
    try:
        results = excel_io.validate_rows(parsed, clean)
    except excel_io.ExcelRejected as e:
        raise AppError(422, e.code, e.message) from e
    blocked = 0
    for rr in results:
        if rr.kind == "DATA" and rr.phone and active_block(db, p.org.id, phone_hmac(p.org.id, rr.phone)):
            rr.warnings.append("BLOCKED")
            blocked += 1
    by_no = {r.row_no: r for r in rows}
    for rr in results:
        row = by_no[rr.row_no]
        row.errors = rr.errors
        row.warnings = rr.warnings
    batch.mapping = clean
    batch.date_semantics = date_semantics
    batch.validation_version += 1
    batch.status = "VALIDATED"
    data_rows = [r for r in results if r.kind == "DATA"]
    batch.valid_count = sum(1 for r in data_rows if not r.errors)
    err_list = excel_io.error_rows(results, parsed.rows, clean["date"] or 0)
    return {
        "validation_version": batch.validation_version,
        "data_rows": len(data_rows),
        "valid_count": batch.valid_count,
        "error_count": sum(1 for r in data_rows if r.errors),
        "dialable_count": sum(1 for r in data_rows if r.dialable),
        "blocked_count": blocked,
        "shared_phone_count": sum(1 for r in data_rows if "SHARED_PHONE" in r.warnings),
        "duplicate_count": sum(1 for r in data_rows if "DUP_ROW" in r.warnings or "DUP_EXTERNAL_NO_CONFLICT" in r.errors),
        "errors": err_list[:500],
    }


def commit_import(db: Session, p: Principal, batch: ImportBatch, validation_version: int) -> dict:
    if batch.status == "COMMITTED":
        raise AppError(409, "ALREADY_COMMITTED", "이미 저장된 업로드입니다")
    if batch.status != "VALIDATED" or batch.validation_version != validation_version:
        raise AppError(409, "VALIDATION_STALE", "검증 결과가 최신이 아닙니다. 다시 검증하세요")
    rows, parsed = _load_rows(db, batch)
    results = {r.row_no: r for r in excel_io.validate_rows(parsed, batch.mapping)}
    created = updated = kept = 0
    for row in rows:
        rr = results[row.row_no]
        if rr.kind != "DATA" or row.errors:
            continue
        cg = db.scalar(select(Caregiver).where(Caregiver.organization_id == p.org.id,
                                               Caregiver.external_no == rr.external_no))
        ph = phone_hmac(p.org.id, rr.phone) if rr.phone else None
        flags = ["SHARED_PHONE"] if "SHARED_PHONE" in row.warnings else []
        if cg is None:
            cg = Caregiver(organization_id=p.org.id, external_no=rr.external_no, name=rr.name,
                           phone_enc=encrypt(rr.phone.encode()) if rr.phone else None, phone_hash=ph,
                           phone_masked=mask_phone(rr.phone), confirmed_available_date=rr.date_value,
                           review_flags=flags)
            db.add(cg)
            db.flush()
            created += 1
        else:
            cg.name = rr.name
            if ph != cg.phone_hash:
                cg.phone_enc = encrypt(rr.phone.encode()) if rr.phone else None
                cg.phone_hash = ph
                cg.phone_masked = mask_phone(rr.phone)
            cg.review_flags = sorted(set([f for f in (cg.review_flags or []) if f != "SHARED_PHONE"] + flags))
            has_call_change = db.scalar(select(func.count()).select_from(ScheduleChange).where(
                ScheduleChange.caregiver_id == cg.id, ScheduleChange.actor_type == "agent")) or 0
            if cg.confirmed_available_date != rr.date_value:
                if has_call_change:
                    row.warnings = list(row.warnings) + ["DB_DATE_DIFFERS"]
                    kept += 1
                else:
                    cg.confirmed_available_date = rr.date_value
            cg.version += 1
            updated += 1
        row.caregiver_id = cg.id
    batch.status = "COMMITTED"
    audit(db, p, "import.commit", "import_batch", batch.id, {"created": created, "updated": updated})
    return {"batch_id": batch.id, "row_count": batch.row_count, "created": created, "updated": updated,
            "kept_call_confirmed": kept}


# ======================== 캠페인 ========================
def latest_pack(db: Session, org_id: str, pack_key: str) -> VoicePack | None:
    return db.scalar(select(VoicePack).where(VoicePack.organization_id == org_id, VoicePack.pack_key == pack_key)
                     .order_by(VoicePack.version.desc()))


def packs_dir(org_id: str) -> object:
    from .config import get_settings
    d = get_settings().data_dir / "voice" / org_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def create_campaign(db: Session, p: Principal, *, batch_id: str, caregiver_ids: list[str], voice_pack_id: str,
                    name: str | None) -> dict:
    batch = get_batch(db, p, batch_id)
    if batch.status != "COMMITTED":
        raise AppError(409, "BATCH_NOT_COMMITTED", "검증·저장을 마친 명부만 사용할 수 있습니다")
    pack = db.get(VoicePack, voice_pack_id)
    if pack is None or pack.organization_id != p.org.id:
        raise AppError(404, "NOT_FOUND", "음원팩이 없습니다")
    if not caregiver_ids:
        raise AppError(422, "NO_TARGETS", "대상을 1명 이상 선택하세요")
    if len(set(caregiver_ids)) != len(caregiver_ids):
        raise AppError(422, "DUPLICATE_TARGETS", "같은 대상이 중복 선택되었습니다")
    batch_cg = {r.caregiver_id: r for r in db.scalars(select(ImportRow).where(
        ImportRow.batch_id == batch.id, ImportRow.caregiver_id.is_not(None))).all()}
    pilot = pilot_enabled(p.org)
    tests = test_number_hashes(p.org) if pilot else set()
    camp = Campaign(organization_id=p.org.id, batch_id=batch.id, name=(name or batch.file_name)[:200],
                    voice_pack_id=pack.id, voice_pack_digest=pack.digest, is_mock=p.org.environment == "sandbox",
                    pilot=pilot, created_by=p.id)
    db.add(camp)
    db.flush()
    accepted, excluded = 0, []
    seen_phones: set[str] = set()
    for i, cid in enumerate(caregiver_ids):
        cg = db.get(Caregiver, cid)
        reason = None
        if cg is None or cg.organization_id != p.org.id or cid not in batch_cg:
            reason = "NOT_IN_BATCH"
        elif batch_cg[cid].errors:
            reason = "ROW_ERROR"
        elif "SHARED_PHONE" in (cg.review_flags or []):
            reason = "SHARED_PHONE_REVIEW"
        elif not cg.phone_hash:
            reason = "PHONE_MISSING"
        elif active_block(db, p.org.id, cg.phone_hash):
            reason = "BLOCKED"
        elif cg.phone_hash in seen_phones:
            reason = "DUPLICATE_PHONE"
        elif pilot and cg.phone_hash not in tests:
            reason = "NOT_TEST_NUMBER"  # 실통화 시험 모드: 등록한 시험 번호 외 발신 금지
        if reason:
            excluded.append({"caregiver_id": cid, "reason": reason})
            continue
        seen_phones.add(cg.phone_hash)
        db.add(CampaignTarget(campaign_id=camp.id, caregiver_id=cid, order_no=i))
        accepted += 1
    if accepted == 0:
        msg = "발신 가능한 대상이 없습니다"
        if pilot and any(e["reason"] == "NOT_TEST_NUMBER" for e in excluded):
            msg += " — 실통화 시험 모드에서는 설정에 등록한 시험 번호로만 걸 수 있습니다"
        raise AppError(422, "NO_DIALABLE_TARGETS", msg, {"excluded": excluded})
    audit(db, p, "campaign.create", "campaign", camp.id, {"accepted": accepted, "excluded": len(excluded)})
    return {"campaign_id": camp.id, "accepted_count": accepted, "excluded_targets": excluded,
            "is_mock": camp.is_mock, "pilot": camp.pilot}


def device_ready(dev: Device) -> dict:
    """실통화 준비 판정. USB 연결(heartbeat)과 실통화 검증(proof)을 구분한다."""
    caps = dev.capabilities or {}
    proof = dev.proof or {}
    hb = aware(dev.heartbeat_at)
    connected = bool(hb and utcnow() - hb < timedelta(seconds=30))
    if dev.kind == "mock":
        return {"connected": connected, "real_call_ready": False, "mock": True,
                "reasons": [] if connected else ["MOCK 에이전트가 연결되지 않았습니다"]}
    reasons = []
    if not connected:
        reasons.append("PC 에이전트/단말 연결 신호가 없습니다")
    for cap in ("dial", "hangup", "answer_detection", "rx_audio", "tx_audio", "recording"):
        if caps.get(cap) != "supported":
            reasons.append(f"{cap}: {caps.get(cap, 'unknown')}")
    for k, label in (("rx_verified", "상대 음성 수신(RX) 원격 검증"), ("tx_verified", "ARS 송출(TX) 원격 청취 검증"),
                     ("pc_silent_verified", "통화 중 PC 스피커 무출력 검증"),
                     ("recording_verified", "양방향 녹음 검증")):
        if not proof.get(k):
            reasons.append(f"미검증: {label}")
    return {"connected": connected, "real_call_ready": not reasons, "mock": False, "reasons": reasons}


def pilot_enabled(org: Organization) -> bool:
    return org.environment == "production" and bool((org.policies or {}).get("pilot_enabled"))


def test_number_hashes(org: Organization) -> set[str]:
    from aicc_core.phone import normalize_phone
    out = set()
    for raw in (org.policies or {}).get("test_numbers") or []:
        r = normalize_phone(str(raw))
        if r.ok:
            out.add(phone_hmac(org.id, r.normalized))
    return out


def policy_problems(org: Organization) -> list[str]:
    pol = org.policies or {}
    out = []
    if not org.company_name.strip():
        out.append("회사명이 설정되지 않았습니다")
    if not pol.get("retention_days"):
        out.append("녹음·전사 보관 기간이 설정되지 않았습니다")
    if not (pol.get("call_window_start") and pol.get("call_window_end")):
        out.append("통화 허용 시간이 설정되지 않았습니다")
    if not pol.get("max_call_seconds"):
        out.append("최대 통화 길이가 설정되지 않았습니다")
    if not pol.get("recording_notice_confirmed"):
        out.append("녹음 안내·고지 방식이 확정되지 않았습니다")
    if not pol.get("ai_external_transfer_confirmed"):
        out.append("AI 외부 전송 범위가 확정되지 않았습니다")
    return out


def preflight(db: Session, p: Principal, camp: Campaign, device_id: str | None) -> dict:
    checks: list[dict] = []

    def add(key: str, ok: bool, label: str, detail: list[str] | None = None) -> None:
        checks.append({"key": key, "ok": ok, "label": label, "detail": detail or []})

    queued = db.scalar(select(func.count()).select_from(CampaignTarget).where(
        CampaignTarget.campaign_id == camp.id, CampaignTarget.state == "QUEUED")) or 0
    env_ok = camp.is_mock == (p.org.environment == "sandbox")
    add("environment", env_ok, "캠페인 종류와 조직 종류 일치",
        [] if env_ok else ["조직 종류가 바뀌었습니다. 대상 선택에서 캠페인을 새로 만드세요"])
    add("targets", queued > 0, f"발신 대기 대상 {queued}명")
    pack = db.get(VoicePack, camp.voice_pack_id)
    if camp.is_mock:
        add("voice", pack is not None, "음원팩(MOCK 시험: 자리표시자 허용, 실통화 불가)")
    else:
        pc = check_pack(pack.manifest, packs_dir(p.org.id)) if pack else None
        add("voice", bool(pc and pc.dialable), "회사명·음원 준비", pc.problems if pc else ["음원팩 없음"])
        if camp.pilot:
            tests = test_number_hashes(p.org)
            bad = db.scalar(select(func.count()).select_from(CampaignTarget).join(
                Caregiver, Caregiver.id == CampaignTarget.caregiver_id).where(
                CampaignTarget.campaign_id == camp.id, CampaignTarget.state == "QUEUED",
                Caregiver.phone_hash.not_in(tests or {"-"}))) or 0
            add("pilot", pilot_enabled(p.org) and bad == 0, "실통화 시험: 대상 전원이 등록된 시험 번호",
                [] if bad == 0 else [f"시험 번호가 아닌 대상 {bad}명"] if pilot_enabled(p.org) else ["시험 모드가 꺼졌습니다"])
        else:
            pol = policy_problems(p.org)
            add("policy", not pol, "운영 정책(보관·허용시간·고지·AI 전송)", pol)
    dev = db.get(Device, device_id) if device_id else None
    if dev is None or dev.organization_id != p.org.id:
        add("device", False, "단말 선택", ["단말을 선택하세요"])
    else:
        r = device_ready(dev)
        if camp.is_mock:
            ok = dev.kind == "mock" and r["connected"]
            add("device", ok, "MOCK 에이전트 연결", r["reasons"] if dev.kind == "mock" else
                ["시험 조직에서는 MOCK 단말만 사용할 수 있습니다"])
        elif camp.pilot:
            caps = dev.capabilities or {}
            ok = dev.kind == "a24" and r["connected"] and caps.get("dial") != "unsupported"
            detail = [] if ok else (["A24 에이전트 연결 신호가 없습니다(USB·AICC 실행 확인)"] if dev.kind == "a24"
                                    else ["A24 단말을 선택하세요"])
            add("device", ok, "A24 연결(시험 모드: 실통화 검증 기록 없이 시험 번호만)", detail)
        else:
            ok = dev.kind == "a24" and r["real_call_ready"]
            add("device", ok, "A24 실통화 검증 유효", r["reasons"] if dev.kind == "a24" else
                ["운영 조직에서는 MOCK 단말로 발신할 수 없습니다"])
        busy = dev.active_call_id is not None
        add("idle", not busy, "다른 통화 없음", ["단말이 다른 통화를 처리 중입니다"] if busy else [])
        other = db.scalar(select(Campaign).where(Campaign.device_id == dev.id, Campaign.state == "RUNNING",
                                                 Campaign.id != camp.id))
        add("exclusive", other is None, "같은 단말의 다른 진행 캠페인 없음",
            [f"진행 중 캠페인: {other.name}"] if other else [])
    return {"ok": all(c["ok"] for c in checks), "checks": checks, "is_mock": camp.is_mock, "pilot": camp.pilot}


def get_campaign(db: Session, p: Principal, cid: str) -> Campaign:
    c = db.get(Campaign, cid)
    if c is None or c.organization_id != p.org.id:
        raise AppError(404, "NOT_FOUND", "캠페인이 없습니다")
    return c


def check_version(camp: Campaign, expected: int | None) -> None:
    if expected is None or expected != camp.version:
        raise AppError(409, "VERSION_CONFLICT", "다른 사용자가 먼저 변경했습니다. 새로고침하세요",
                       {"current_version": camp.version})


# ======================== 에이전트: 발신권 ========================
def in_call_window(org: Organization) -> bool:
    pol = org.policies or {}
    s, e = pol.get("call_window_start"), pol.get("call_window_end")
    if not s or not e:
        return False
    now = datetime.now(KST).strftime("%H:%M")
    return s <= now <= e


def claim_next(db: Session, p: Principal) -> dict:
    dev = p.device
    assert dev is not None
    if dev.active_call_id:
        call = db.get(CallSession, dev.active_call_id)
        return {"status": "BUSY", "active_call_id": dev.active_call_id,
                "message": "이전 통화의 단말 IDLE 확인 전에는 새 발신을 허용하지 않습니다",
                "call_state": call.state if call else None}
    camp = db.scalar(select(Campaign).where(Campaign.device_id == dev.id, Campaign.state == "RUNNING")
                     .order_by(Campaign.started_at))
    if camp is None:
        return {"status": "IDLE"}
    if camp.is_mock != (dev.kind == "mock") or camp.is_mock != (p.org.environment == "sandbox"):
        return {"status": "REFUSED", "message": "캠페인과 단말 종류가 맞지 않습니다"}
    if camp.pilot:
        if not pilot_enabled(p.org):
            camp.state = "PAUSED"
            return {"status": "REFUSED", "message": "실통화 시험 모드가 꺼져 있습니다"}
    elif not camp.is_mock:
        r = device_ready(dev)
        if not r["real_call_ready"]:
            camp.state = "PAUSED"
            return {"status": "REFUSED", "code": "DEVICE_AUDIO_UNVERIFIED", "message": "통화 음성 경로 검증이 필요합니다",
                    "reasons": r["reasons"]}
        if not in_call_window(p.org):
            return {"status": "WAIT", "message": "통화 허용 시간이 아닙니다"}
    while True:
        tgt = db.scalar(select(CampaignTarget).where(CampaignTarget.campaign_id == camp.id,
                                                     CampaignTarget.state == "QUEUED")
                        .order_by(CampaignTarget.order_no))
        if tgt is None:
            busy = db.scalar(select(func.count()).select_from(CampaignTarget).where(
                CampaignTarget.campaign_id == camp.id, CampaignTarget.state == "CALLING")) or 0
            if not busy:
                camp.state = "COMPLETED"
                camp.ended_at = utcnow()
            return {"status": "IDLE"}
        cg = db.get(Caregiver, tgt.caregiver_id)
        # 발신권 획득 직전 연락거부 재확인(설계 09), 시험 모드는 시험 번호 재확인
        if camp.pilot and (cg is None or cg.phone_hash not in test_number_hashes(p.org)):
            tgt.state = "CANCELLED"
            tgt.last_outcome = "NOT_TEST_NUMBER"
            continue
        if cg is None or active_block(db, p.org.id, cg.phone_hash):
            tgt.state = "BLOCKED"
            tgt.last_outcome = "REFUSED"
            continue
        break
    pack = db.get(VoicePack, camp.voice_pack_id)
    call = CallSession(organization_id=p.org.id, campaign_id=camp.id, target_id=tgt.id, caregiver_id=cg.id,
                       device_id=dev.id, is_mock=camp.is_mock, state="DIALING")
    db.add(call)
    db.flush()
    tgt.state = "CALLING"
    tgt.attempt_count += 1
    dev.active_call_id = call.id
    dev.lease_expires_at = utcnow() + timedelta(seconds=LEASE_SECONDS)
    audit(db, p, "call.claim", "call", call.id, {"campaign_id": camp.id})
    return {
        "status": "CALL", "call_id": call.id, "campaign_id": camp.id, "is_mock": camp.is_mock, "pilot": camp.pilot,
        "caregiver": {"id": cg.id, "name": cg.name, "external_no": cg.external_no},
        "phone": decrypt(cg.phone_enc).decode() if cg.phone_enc else None,
        "voice_pack": {"id": pack.id, "digest": pack.digest, "manifest": pack.manifest},
        "reference_date": today_kst().isoformat(),
        "max_call_seconds": int((p.org.policies or {}).get("max_call_seconds") or 300),
    }


def get_call_for_device(db: Session, p: Principal, call_id: str) -> CallSession:
    call = db.get(CallSession, call_id)
    if call is None or call.organization_id != p.org.id or call.device_id != p.id:
        raise AppError(404, "NOT_FOUND", "통화가 없습니다")
    return call


def record_events(db: Session, call: CallSession, events: list[dict]) -> dict:
    accepted, dup = 0, 0
    for ev in events:
        eid = str(ev["event_id"])
        if db.get(CallEvent, eid):
            dup += 1
            continue
        clash = db.scalar(select(CallEvent).where(CallEvent.call_id == call.id, CallEvent.seq == int(ev["seq"])))
        if clash:
            raise AppError(409, "SEQ_CONFLICT", "같은 순번의 다른 이벤트가 이미 있습니다", {"seq": ev["seq"]})
        occurred = datetime.fromisoformat(ev["occurred_at"]) if ev.get("occurred_at") else utcnow()
        db.add(CallEvent(id=eid, call_id=call.id, seq=int(ev["seq"]), turn_id=int(ev.get("turn_id") or 0),
                         type=str(ev["type"])[:40], payload=ev.get("payload") or {}, occurred_at=occurred))
        accepted += 1
        t = ev["type"]
        if t == "call.answered" and call.answered_at is None:
            call.answered_at = occurred
            call.state = "IN_CALL"
        elif t == "call.state":
            call.state = str((ev.get("payload") or {}).get("state", call.state))[:30]
        elif t == "flags":
            call.flags = dict(call.flags or {}, **(ev.get("payload") or {}))
    return {"accepted": accepted, "duplicates": dup}


def confirm_schedule(db: Session, p: Principal, call: CallSession, *, evidence_event_id: str,
                     expected_version: int | None) -> dict:
    """서버가 이벤트에서 날짜를 읽어 반영한다. 에이전트·AI 가 보낸 date 값만으로는 저장하지 않는다."""
    prior = db.scalar(select(ScheduleChange).where(ScheduleChange.evidence_event_id == evidence_event_id))
    if prior:
        cg = db.get(Caregiver, prior.caregiver_id)
        return {"status": "ALREADY_APPLIED", "new_date": prior.new_date.isoformat() if prior.new_date else None,
                "version": cg.version if cg else None}
    ev = db.get(CallEvent, evidence_event_id)
    if ev is None or ev.call_id != call.id or ev.type != "utterance.final":
        raise AppError(422, "EVIDENCE_INVALID", "동의 발화 이벤트가 이 통화에 없습니다")
    cand_id = (ev.payload or {}).get("candidate_event_id")
    cand = db.get(CallEvent, cand_id) if cand_id else None
    if cand is None or cand.call_id != call.id or cand.type != "schedule.candidate":
        raise AppError(422, "CANDIDATE_INVALID", "날짜 후보 이벤트가 이 통화에 없습니다")
    if cand.turn_id != ev.turn_id:
        raise AppError(422, "TURN_MISMATCH", "재확인 질문과 동의 발화의 턴이 다릅니다")
    try:
        cand_date = date.fromisoformat(cand.payload["date"])
    except (KeyError, ValueError) as e:
        raise AppError(422, "CANDIDATE_INVALID", "날짜 후보 형식이 올바르지 않습니다") from e
    ref = date.fromisoformat((cand.payload or {}).get("reference_date") or today_kst().isoformat())
    if cand_date < ref:
        raise AppError(422, "PAST_DATE", "지난 날짜는 반영하지 않습니다")
    text = str((ev.payload or {}).get("text") or "")
    it = interpret(text, "CONFIRM_DATE", ref)
    agreed = it.intent == "YES" or (it.intent == "DATE" and it.yes and not it.no and any(
        m.kind == "DATE" and m.value == cand_date for m in it.dates))
    if not agreed:
        raise AppError(422, "NOT_EXPLICIT_CONSENT", "명시적 동의 발화로 판정되지 않아 반영하지 않습니다")
    cg = db.get(Caregiver, call.caregiver_id)
    assert cg is not None
    if expected_version is not None and expected_version != cg.version:
        raise AppError(409, "VERSION_CONFLICT", "명부가 그 사이 변경되었습니다. 검수가 필요합니다",
                       {"current_version": cg.version})
    camp = db.get(Campaign, call.campaign_id)
    batch = db.get(ImportBatch, camp.batch_id) if camp else None
    field = "confirmed_available_date" if (batch and batch.date_semantics == "NEXT_AVAILABLE") else "next_available_date"
    old = getattr(cg, field)
    setattr(cg, field, cand_date)
    if field == "confirmed_available_date":
        cg.next_available_date = cand_date
    cg.availability_state = "CONFIRMED"
    cg.version += 1
    obs = db.scalar(select(ScheduleObservation).where(ScheduleObservation.call_id == call.id))
    if obs is None:
        obs = ScheduleObservation(call_id=call.id, caregiver_id=cg.id)
        db.add(obs)
    obs.available_date = cand_date
    obs.confirmation_state = "CONFIRMED"
    obs.candidate_event_id = cand.id
    obs.evidence_event_id = ev.id
    obs.applied = True
    db.add(ScheduleChange(organization_id=p.org.id, caregiver_id=cg.id, field=field, old_date=old,
                          new_date=cand_date, call_id=call.id, evidence_event_id=ev.id, evidence_text=text[:500],
                          actor_type="agent", actor_id=p.id, reason="통화 재확인 동의"))
    audit(db, p, "schedule.confirm", "caregiver", cg.id, {"call_id": call.id, "field": field})
    return {"status": "APPLIED", "field": field, "old_date": old.isoformat() if old else None,
            "new_date": cand_date.isoformat(), "version": cg.version}


REVIEW_OUTCOMES = {"UNCONFIRMED", "NO_RESPONSE", "CONSULT_REQUESTED", "WRONG_PERSON", "DEVICE_ERROR",
                   "REMOTE_HANGUP", "ANYTIME"}


def finish_call(db: Session, p: Principal, call: CallSession, *, outcome: str, observation: dict | None,
                review_reasons: list[str], phone_idle: bool) -> dict:
    from aicc_core.flow import OUTCOMES
    if outcome not in OUTCOMES:
        raise AppError(422, "OUTCOME_INVALID", "알 수 없는 통화 결과입니다")
    if call.ended_at is None:
        call.ended_at = utcnow()
        call.outcome = outcome
        call.state = "ENDED"
        call.review_reasons = review_reasons[:20]
        if outcome in REVIEW_OUTCOMES or review_reasons:
            call.review_state = "PENDING"
        tgt = db.get(CampaignTarget, call.target_id)
        if tgt and tgt.state == "CALLING":
            tgt.state = "BLOCKED" if outcome == "REFUSED" else "DONE"
            tgt.last_outcome = outcome
        cg = db.get(Caregiver, call.caregiver_id)
        if outcome == "REFUSED" and cg and not active_block(db, p.org.id, cg.phone_hash):
            # 통화 중 차단 요청이 서버에 닿지 못했어도 종료 보고에서 차단을 보장한다
            refusal = db.scalar(select(CallEvent).where(CallEvent.call_id == call.id,
                                                        CallEvent.type == "utterance.final")
                                .order_by(CallEvent.seq.desc()))
            block_contact(db, p, caregiver=cg, reason="통화 중 연락거부 요청", call_id=call.id,
                          evidence_event_id=refusal.id if refusal else None)
        if outcome == "DEVICE_ERROR":
            camp = db.get(Campaign, call.campaign_id)
            if camp and camp.state == "RUNNING":
                camp.state = "PAUSED"  # 장치 장애 후 자동 재개·재발신 금지(운영자 재개 필요)
                camp.version += 1
        if cg:
            cg.last_call_at = call.ended_at
            cg.last_outcome = outcome
            if outcome == "WRONG_PERSON":
                cg.review_flags = sorted(set((cg.review_flags or []) + ["WRONG_PERSON"]))
        if observation and cg:
            obs = db.scalar(select(ScheduleObservation).where(ScheduleObservation.call_id == call.id))
            if obs is None:
                obs = ScheduleObservation(call_id=call.id, caregiver_id=cg.id)
                db.add(obs)

            def d(k: str) -> date | None:
                v = observation.get(k)
                return date.fromisoformat(v) if v else None
            obs.work_status = observation.get("work_status")
            obs.end_date = d("end_date")
            if not obs.applied:  # 확정된 값은 에이전트 종료 보고로 덮지 않는다
                obs.available_date = d("available_date")
                state = observation.get("confirmation_state") or "UNCONFIRMED"
                obs.confirmation_state = "UNCONFIRMED" if state == "CONFIRMED" else state
            obs.range_start = d("range_start")
            obs.range_end = d("range_end")
            obs.raw_text = (observation.get("available_text") or observation.get("end_date_text") or "")[:500]
            obs.history = observation.get("history") or []
            if obs.work_status:
                cg.work_status = obs.work_status
            if obs.end_date:
                cg.end_date = obs.end_date
            if obs.confirmation_state in ("ANYTIME", "RANGE", "UNKNOWN"):
                cg.availability_state = obs.confirmation_state
        audit(db, p, "call.finish", "call", call.id, {"outcome": outcome})
    dev = db.get(Device, call.device_id)
    if dev and dev.active_call_id == call.id:
        if phone_idle:
            dev.active_call_id = None
            dev.lease_expires_at = None
            dev.phone_state = "IDLE"
        else:
            dev.phone_state = "NOT_IDLE"
    _maybe_complete(db, call.campaign_id)
    return {"status": "FINISHED", "device_released": bool(dev and dev.active_call_id is None)}


def _maybe_complete(db: Session, campaign_id: str) -> None:
    camp = db.get(Campaign, campaign_id)
    if camp is None:
        return
    open_n = db.scalar(select(func.count()).select_from(CampaignTarget).where(
        CampaignTarget.campaign_id == campaign_id, CampaignTarget.state.in_(("QUEUED", "CALLING")))) or 0
    calling = db.scalar(select(func.count()).select_from(CampaignTarget).where(
        CampaignTarget.campaign_id == campaign_id, CampaignTarget.state == "CALLING")) or 0
    if camp.state == "RUNNING" and open_n == 0:
        camp.state = "COMPLETED"
        camp.ended_at = utcnow()
    if camp.state == "STOPPING" and calling == 0:
        camp.state = "STOPPED"
        camp.ended_at = utcnow()


def heartbeat(db: Session, p: Principal, payload: dict) -> dict:
    dev = p.device
    assert dev is not None
    dev.heartbeat_at = utcnow()
    caps = payload.get("capabilities") or {}
    if dev.kind == "mock":
        caps = {k: "mock" for k in ("dial", "hangup", "answer_detection", "rx_audio", "tx_audio", "recording")}
    new_caps = {k: v for k, v in caps.items() if v in ("supported", "unsupported", "unknown", "mock")}
    if dev.kind == "a24" and new_caps != (dev.capabilities or {}):
        # 장치 능력이 바뀌면 이전 실통화 검증은 무효
        if dev.proof:
            dev.proof = {}
            dev.proof_version += 1
    dev.capabilities = new_caps
    if payload.get("probe_report"):
        dev.probe_report = payload["probe_report"]
    serial = payload.get("serial")
    if serial:
        sh = hashlib.sha256(f"{dev.organization_id}:{serial}".encode()).hexdigest()
        if dev.serial_hash and dev.serial_hash != sh:
            dev.proof = {}
            dev.proof_version += 1
        dev.serial_hash = sh
    state = payload.get("phone_state")
    if state:
        dev.phone_state = str(state)[:20]
        # DB가 종료를 기록해도 단말이 IDLE 임을 확인한 뒤에만 발신권을 푼다.
        if state == "IDLE" and dev.active_call_id:
            call = db.get(CallSession, dev.active_call_id)
            if call is None or call.ended_at is not None:
                dev.active_call_id = None
                dev.lease_expires_at = None
    commands = []
    camps = db.scalars(select(Campaign).where(Campaign.device_id == dev.id,
                                              Campaign.state.in_(("STOPPING", "PAUSED", "RUNNING")))).all()
    for c in camps:
        if c.state == "STOPPING":
            commands.append({"type": "STOP_NOW", "campaign_id": c.id})
        elif c.state == "PAUSED":
            commands.append({"type": "PAUSE_AFTER_CURRENT", "campaign_id": c.id})
    return {"ok": True, "commands": commands, "active_call_id": dev.active_call_id}


# ======================== 녹음 ========================
def store_recording(db: Session, p: Principal, call: CallSession, data: bytes, sha256: str, duration_ms: int) -> dict:
    rec = db.scalar(select(Recording).where(Recording.call_id == call.id))
    if rec and rec.state == "READY":
        if rec.sha256 == sha256:
            return {"status": "ALREADY_STORED", "recording_id": rec.id}
        raise AppError(409, "RECORDING_EXISTS", "이미 다른 녹음이 저장되었습니다")
    actual = hashlib.sha256(data).hexdigest()
    if rec is None:
        rec = Recording(call_id=call.id, is_mock=call.is_mock)
        db.add(rec)
    if actual != sha256:
        rec.state = "FAILED"
        rec.error = "무결성 검사 실패(해시 불일치)"
        db.flush()
        raise AppError(422, "CHECKSUM_MISMATCH", "녹음 파일 해시가 일치하지 않습니다")
    if not data.startswith(b"RIFF") or data[8:12] != b"WAVE":
        rec.state = "FAILED"
        rec.error = "WAV 형식이 아닙니다"
        db.flush()
        raise AppError(422, "RECORDING_FORMAT", "WAV 형식이 아닙니다")
    rec.object_key = put_object("recordings", data)
    rec.sha256 = actual
    rec.duration_ms = int(duration_ms)
    rec.state = "READY"
    rec.error = None
    days = int((p.org.policies or {}).get("retention_days") or 0)
    rec.expires_at = utcnow() + timedelta(days=days) if days else None
    audit(db, p, "recording.store", "recording", rec.id, {"call_id": call.id, "bytes": len(data)})
    return {"status": "STORED", "recording_id": rec.id}


def mark_recording_failed(db: Session, call: CallSession, error: str) -> None:
    rec = db.scalar(select(Recording).where(Recording.call_id == call.id))
    if rec is None:
        rec = Recording(call_id=call.id, is_mock=call.is_mock)
        db.add(rec)
    if rec.state != "READY":
        rec.state = "FAILED"
        rec.error = error[:500]


def purge_expired(db: Session, p: Principal | None = None) -> int:
    n = 0
    for rec in db.scalars(select(Recording).where(Recording.state == "READY", Recording.expires_at.is_not(None))):
        if aware(rec.expires_at) <= utcnow():
            if rec.object_key:
                delete_object(rec.object_key)
            rec.object_key = None
            rec.state = "EXPIRED"
            n += 1
            if p:
                audit(db, p, "recording.expire", "recording", rec.id, {})
    # 전사(발화 문장)도 조직 보관기간이 지나면 지운다(연락거부 차단 정보는 별도 보관)
    for org in db.scalars(select(Organization)):
        days = int((org.policies or {}).get("retention_days") or 0)
        if not days:
            continue
        cutoff = utcnow() - timedelta(days=days)
        old_calls = select(CallSession.id).where(CallSession.organization_id == org.id,
                                                 CallSession.ended_at.is_not(None), CallSession.ended_at < cutoff)
        for ev in db.scalars(select(CallEvent).where(CallEvent.call_id.in_(old_calls),
                                                     CallEvent.type.in_(("utterance.final", "ai.done")))):
            if (ev.payload or {}).get("text") not in (None, "[보관기간 만료로 삭제]"):
                ev.payload = dict(ev.payload, text="[보관기간 만료로 삭제]")
                n += 1
        for ch in db.scalars(select(ScheduleChange).where(ScheduleChange.call_id.in_(old_calls),
                                                          ScheduleChange.evidence_text.is_not(None))):
            ch.evidence_text = "[보관기간 만료로 삭제]"
        for ob in db.scalars(select(ScheduleObservation).where(ScheduleObservation.call_id.in_(old_calls),
                                                               ScheduleObservation.raw_text.is_not(None))):
            ob.raw_text = None
    return n
