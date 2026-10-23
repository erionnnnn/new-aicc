"""인증·권한·감사·멱등성 공통 의존성."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .errors import AppError
from .models import AuditLog, AuthSession, Device, IdempotencyKey, Organization, User
from .security import token_hash

SESSION_COOKIE = "aicc_session"

# 설계 03 권한표. perms 키가 True 인 경우에만 '별도 권한' 허용.
PERMISSIONS = {
    "upload_dial": {"admin": True, "operator": True, "reviewer": False},
    "view_results": {"admin": True, "operator": True, "reviewer": True},
    "play_recording": {"admin": "perm", "operator": "perm", "reviewer": True},
    "edit_dates": {"admin": True, "operator": "perm", "reviewer": True},
    "block_contact": {"admin": True, "operator": True, "reviewer": True},
    "unblock_contact": {"admin": True, "operator": False, "reviewer": False},
    "export_all": {"admin": True, "operator": "perm", "reviewer": "perm"},
    "settings": {"admin": True, "operator": False, "reviewer": False},
}


@dataclass
class Principal:
    kind: str  # user | device
    id: str
    org: Organization
    user: User | None = None
    device: Device | None = None

    def can(self, perm: str) -> bool:
        if self.kind != "user" or self.user is None:
            return False
        rule = PERMISSIONS[perm].get(self.user.role, False)
        if rule == "perm":
            return bool((self.user.perms or {}).get(perm))
        return bool(rule)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def current_user(request: Request, db: Session = Depends(get_db),
                 authorization: str | None = Header(default=None)) -> Principal:
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    else:
        token = request.cookies.get(SESSION_COOKIE)
        # 쿠키 인증의 상태 변경 요청은 사용자 정의 헤더를 요구한다(CSRF 완화).
        if token and request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-aicc") != "1":
            raise AppError(403, "CSRF_HEADER_REQUIRED", "요청 헤더가 필요합니다")
    if not token:
        raise AppError(401, "UNAUTHENTICATED", "로그인이 필요합니다")
    sess = db.get(AuthSession, token_hash(token))
    if sess is None or _aware(sess.expires_at) < _now():
        raise AppError(401, "SESSION_EXPIRED", "세션이 만료되었습니다. 다시 로그인하세요")
    user = db.get(User, sess.user_id)
    if user is None or not user.active:
        raise AppError(401, "UNAUTHENTICATED", "로그인이 필요합니다")
    org = db.get(Organization, user.organization_id)
    assert org is not None
    return Principal("user", user.id, org, user=user)


def current_device(db: Session = Depends(get_db), authorization: str | None = Header(default=None)) -> Principal:
    if not authorization or not authorization.startswith("Device "):
        raise AppError(401, "DEVICE_UNAUTHENTICATED", "장치 토큰이 필요합니다")
    dev = db.scalar(select(Device).where(Device.token_hash == token_hash(authorization[7:])))
    if dev is None:
        raise AppError(401, "DEVICE_UNAUTHENTICATED", "장치 토큰이 올바르지 않습니다")
    org = db.get(Organization, dev.organization_id)
    assert org is not None
    return Principal("device", dev.id, org, device=dev)


def require(p: Principal, perm: str) -> None:
    if not p.can(perm):
        raise AppError(403, "FORBIDDEN", "권한이 없습니다", {"permission": perm})


def audit(db: Session, p: Principal, action: str, resource_type: str, resource_id: str | None,
          detail: dict | None = None) -> None:
    db.add(AuditLog(organization_id=p.org.id, actor_type=p.kind, actor_id=p.id, action=action,
                    resource_type=resource_type, resource_id=resource_id, detail=detail or {}))


# ---------- 멱등성 ----------
def body_hash(body: object) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


def idem_lookup(db: Session, p: Principal, route: str, key: str | None, body: object) -> tuple[int, dict] | None:
    if not key:
        raise AppError(400, "IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key 헤더가 필요합니다")
    row = db.get(IdempotencyKey, (p.org.id, p.id, route, key))
    if row is None:
        return None
    if row.body_hash != body_hash(body):
        raise AppError(409, "IDEMPOTENCY_KEY_REUSED", "같은 키로 다른 요청을 보낼 수 없습니다")
    return row.status_code, row.response


def idem_store(db: Session, p: Principal, route: str, key: str, body: object, status: int, response: dict) -> None:
    db.add(IdempotencyKey(organization_id=p.org.id, actor_id=p.id, route=route, key=key,
                          body_hash=body_hash(body), status_code=status, response=response))
