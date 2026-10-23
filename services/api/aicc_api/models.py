"""DB 모델(설계 09). 모든 업무 테이블은 organization_id 로 범위를 나눈다.

- ID: UUID 문자열(36자) — SQLite/PostgreSQL 공통
- 업무 날짜: DATE, 시스템 시각: UTC timezone-aware
- 전화번호: 암호문(phone_enc) + 조직 HMAC(phone_hash) + 마스킹 표시값
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def uid() -> str:
    return str(uuid.uuid4())


def now() -> datetime:
    return datetime.now(timezone.utc)


class TZDateTime(DateTime):
    def __init__(self) -> None:
        super().__init__(timezone=True)


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(200))
    # sandbox: MOCK 단말만 허용, 결과가 시험 데이터임을 표시 / production: 실기기만 허용
    environment: Mapped[str] = mapped_column(String(20), default="sandbox")
    company_name: Mapped[str] = mapped_column(String(200), default="")
    policies: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=now)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(20))  # admin | operator | reviewer
    perms: Mapped[dict] = mapped_column(JSON, default=dict)  # 운영자 '별도 권한'
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=now)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime())
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=now)


class Caregiver(Base):
    __tablename__ = "caregivers"
    __table_args__ = (UniqueConstraint("organization_id", "external_no"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    external_no: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200))
    phone_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    phone_hash: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    phone_masked: Mapped[str] = mapped_column(String(30), default="")
    # 근무확정일 열(업로드 의미가 NEXT_AVAILABLE 일 때만 통화 결과로 갱신)
    confirmed_available_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 별도 '다음근무가능일'(업로드 의미가 ASSIGNMENT_CONFIRMED 일 때 통화 결과 저장)
    next_available_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    work_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    availability_state: Mapped[str | None] = mapped_column(String(20), nullable=True)
    review_flags: Mapped[list] = mapped_column(JSON, default=list)
    last_call_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    last_outcome: Mapped[str | None] = mapped_column(String(30), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime(), default=now, onupdate=now)


class ImportBatch(Base):
    __tablename__ = "import_batches"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    file_name: Mapped[str] = mapped_column(String(300))
    file_hash: Mapped[str] = mapped_column(String(64), index=True)
    object_key: Mapped[str] = mapped_column(String(300))  # 암호화 원본 파일
    sheet_name: Mapped[str] = mapped_column(String(200), default="")
    header_row: Mapped[int] = mapped_column(Integer, default=1)
    headers: Mapped[list] = mapped_column(JSON, default=list)
    mapping: Mapped[dict] = mapped_column(JSON, default=dict)
    date_semantics: Mapped[str | None] = mapped_column(String(30), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="UPLOADED")
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    valid_count: Mapped[int] = mapped_column(Integer, default=0)
    validation_version: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=now)


class ImportRow(Base):
    __tablename__ = "import_rows"
    __table_args__ = (UniqueConstraint("batch_id", "row_no"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    batch_id: Mapped[str] = mapped_column(ForeignKey("import_batches.id"), index=True)
    row_no: Mapped[int] = mapped_column(Integer)  # 엑셀 행 번호(1부터)
    raw_enc: Mapped[bytes] = mapped_column(LargeBinary)  # 원본 셀 값 JSON 암호문
    caregiver_id: Mapped[str | None] = mapped_column(ForeignKey("caregivers.id"), nullable=True, index=True)
    errors: Mapped[list] = mapped_column(JSON, default=list)
    warnings: Mapped[list] = mapped_column(JSON, default=list)


class VoicePack(Base):
    __tablename__ = "voice_packs"
    __table_args__ = (UniqueConstraint("organization_id", "pack_key", "version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    pack_key: Mapped[str] = mapped_column(String(50))  # female | male
    version: Mapped[int] = mapped_column(Integer, default=1)
    manifest: Mapped[dict] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(32))
    created_by: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=now)


class Device(Base):
    __tablename__ = "devices"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(20))  # mock | a24
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    serial_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)
    probe_report: Mapped[dict] = mapped_column(JSON, default=dict)
    # 사람의 실통화 검증 기록(원격 청취 확인 등). 에이전트가 스스로 설정할 수 없다.
    proof: Mapped[dict] = mapped_column(JSON, default=dict)
    proof_version: Mapped[int] = mapped_column(Integer, default=0)
    verified_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    phone_state: Mapped[str | None] = mapped_column(String(20), nullable=True)
    active_call_id: Mapped[str | None] = mapped_column(String(36), unique=True, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=now)


class Campaign(Base):
    __tablename__ = "campaigns"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("import_batches.id"))
    name: Mapped[str] = mapped_column(String(200))
    voice_pack_id: Mapped[str] = mapped_column(ForeignKey("voice_packs.id"))
    voice_pack_digest: Mapped[str] = mapped_column(String(32))
    device_id: Mapped[str | None] = mapped_column(ForeignKey("devices.id"), nullable=True)
    is_mock: Mapped[bool] = mapped_column(Boolean, default=False)
    state: Mapped[str] = mapped_column(String(20), default="DRAFT")
    # DRAFT | RUNNING | PAUSED | STOPPING | STOPPED | COMPLETED
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=now)
    started_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)


class CampaignTarget(Base):
    __tablename__ = "campaign_targets"
    __table_args__ = (UniqueConstraint("campaign_id", "caregiver_id"), Index("ix_ct_campaign_state", "campaign_id", "state"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"), index=True)
    caregiver_id: Mapped[str] = mapped_column(ForeignKey("caregivers.id"))
    order_no: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(20), default="QUEUED")
    # QUEUED | CALLING | DONE | BLOCKED | CANCELLED
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_outcome: Mapped[str | None] = mapped_column(String(30), nullable=True)


class CallSession(Base):
    __tablename__ = "call_sessions"
    __table_args__ = (Index("ix_calls_target_started", "target_id", "started_at"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"), index=True)
    target_id: Mapped[str] = mapped_column(ForeignKey("campaign_targets.id"))
    caregiver_id: Mapped[str] = mapped_column(ForeignKey("caregivers.id"), index=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"))
    is_mock: Mapped[bool] = mapped_column(Boolean, default=False)
    state: Mapped[str] = mapped_column(String(30), default="DIALING")
    started_at: Mapped[datetime] = mapped_column(TZDateTime(), default=now)
    answered_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(30), nullable=True)
    review_reasons: Mapped[list] = mapped_column(JSON, default=list)
    review_state: Mapped[str] = mapped_column(String(20), default="NONE")  # NONE | PENDING | DONE
    flags: Mapped[dict] = mapped_column(JSON, default=dict)  # rx_verified/tx_verified 등 판정 분리 표시


class CallEvent(Base):
    __tablename__ = "call_events"
    __table_args__ = (UniqueConstraint("call_id", "seq"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # 에이전트 발급 event_id(멱등)
    call_id: Mapped[str] = mapped_column(ForeignKey("call_sessions.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    turn_id: Mapped[int] = mapped_column(Integer, default=0)
    type: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(TZDateTime())


class ScheduleObservation(Base):
    __tablename__ = "schedule_observations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    call_id: Mapped[str] = mapped_column(ForeignKey("call_sessions.id"), unique=True)
    caregiver_id: Mapped[str] = mapped_column(ForeignKey("caregivers.id"), index=True)
    work_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    available_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    range_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    range_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmation_state: Mapped[str] = mapped_column(String(20), default="UNCONFIRMED")
    candidate_event_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    evidence_event_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    history: Mapped[list] = mapped_column(JSON, default=list)
    applied: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=now)


class ScheduleChange(Base):
    """변경 이력은 추가만 허용(수정·삭제 API 없음)."""

    __tablename__ = "schedule_changes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    caregiver_id: Mapped[str] = mapped_column(ForeignKey("caregivers.id"), index=True)
    field: Mapped[str] = mapped_column(String(40))
    old_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    new_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    call_id: Mapped[str | None] = mapped_column(ForeignKey("call_sessions.id"), nullable=True)
    evidence_event_id: Mapped[str | None] = mapped_column(String(36), unique=True, nullable=True)
    evidence_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_type: Mapped[str] = mapped_column(String(20))  # agent | user
    actor_id: Mapped[str] = mapped_column(String(36))
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=now)


class ContactBlock(Base):
    __tablename__ = "contact_blocks"
    __table_args__ = (Index("ix_blocks_org_phone", "organization_id", "phone_hash"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"))
    phone_hash: Mapped[str] = mapped_column(String(64))
    phone_masked: Mapped[str] = mapped_column(String(30), default="")
    caregiver_id: Mapped[str | None] = mapped_column(ForeignKey("caregivers.id"), nullable=True)
    reason: Mapped[str] = mapped_column(Text)
    source_call_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    evidence_event_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    blocked_by: Mapped[str] = mapped_column(String(36))
    blocked_at: Mapped[datetime] = mapped_column(TZDateTime(), default=now)
    revoked_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class Recording(Base):
    __tablename__ = "recordings"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    call_id: Mapped[str] = mapped_column(ForeignKey("call_sessions.id"), unique=True)
    object_key: Mapped[str | None] = mapped_column(String(300), nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    channels: Mapped[int] = mapped_column(Integer, default=2)
    state: Mapped[str] = mapped_column(String(20), default="PENDING")  # PENDING|READY|FAILED|EXPIRED
    is_mock: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=now)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(String(36), index=True)
    actor_type: Mapped[str] = mapped_column(String(20))
    actor_id: Mapped[str] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(60))
    resource_type: Mapped[str] = mapped_column(String(40))
    resource_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(TZDateTime(), default=now, index=True)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    organization_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    route: Mapped[str] = mapped_column(String(200), primary_key=True)
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    body_hash: Mapped[str] = mapped_column(String(64))
    status_code: Mapped[int] = mapped_column(Integer)
    response: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=now)


class Export(Base):
    __tablename__ = "exports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("import_batches.id"))
    campaign_id: Mapped[str | None] = mapped_column(ForeignKey("campaigns.id"), nullable=True)
    scope: Mapped[str] = mapped_column(String(20), default="all")
    in_progress: Mapped[bool] = mapped_column(Boolean, default=False)
    state: Mapped[str] = mapped_column(String(20), default="PENDING")
    object_key: Mapped[str | None] = mapped_column(String(300), nullable=True)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=now)
