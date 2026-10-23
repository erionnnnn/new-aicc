from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..deps import PERMISSIONS, SESSION_COOKIE, Principal, audit, current_user, require
from ..errors import AppError
from ..models import AuthSession, Organization, User
from ..security import hash_password, new_token, token_hash, verify_password
from ..services import utcnow

router = APIRouter(prefix="/api/v1", tags=["auth"])


class BootstrapIn(BaseModel):
    org_name: str = Field(min_length=1, max_length=200)
    environment: str = Field(pattern="^(sandbox|production)$")
    username: str = Field(min_length=3, max_length=100)
    password: str = Field(min_length=10, max_length=200)


class LoginIn(BaseModel):
    username: str
    password: str


def _user_out(u: User, org: Organization) -> dict:
    p = Principal("user", u.id, org, user=u)
    return {"id": u.id, "username": u.username, "role": u.role, "perms": u.perms or {},
            "can": {k: p.can(k) for k in PERMISSIONS},
            "organization": {"id": org.id, "name": org.name, "environment": org.environment,
                             "company_name": org.company_name}}


@router.get("/auth/status")
def status(db: Session = Depends(get_db)) -> dict:
    n = db.scalar(select(func.count()).select_from(User)) or 0
    return {"bootstrapped": n > 0, "env": get_settings().env}


@router.post("/auth/bootstrap", status_code=201)
def bootstrap(body: BootstrapIn, db: Session = Depends(get_db)) -> dict:
    if (db.scalar(select(func.count()).select_from(User)) or 0) > 0:
        raise AppError(409, "ALREADY_BOOTSTRAPPED", "이미 초기 설정이 끝났습니다")
    org = Organization(name=body.org_name, environment=body.environment)
    db.add(org)
    db.flush()
    u = User(organization_id=org.id, username=body.username, password_hash=hash_password(body.password),
             role="admin", perms={"play_recording": True})
    db.add(u)
    db.flush()
    audit(db, Principal("user", u.id, org, user=u), "org.bootstrap", "organization", org.id,
          {"environment": body.environment})
    db.commit()
    return {"organization_id": org.id, "user_id": u.id}


@router.post("/auth/login")
def login(body: LoginIn, response: Response, db: Session = Depends(get_db)) -> dict:
    u = db.scalar(select(User).where(User.username == body.username))
    if u is None or not u.active or not verify_password(body.password, u.password_hash):
        raise AppError(401, "LOGIN_FAILED", "아이디 또는 비밀번호가 올바르지 않습니다")
    token = new_token()
    hours = get_settings().session_hours
    db.add(AuthSession(token_hash=token_hash(token), user_id=u.id, expires_at=utcnow() + timedelta(hours=hours)))
    org = db.get(Organization, u.organization_id)
    audit(db, Principal("user", u.id, org, user=u), "auth.login", "user", u.id)
    db.commit()
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict", max_age=hours * 3600,
                        secure=get_settings().is_production)
    return {"token": token, "user": _user_out(u, org)}


@router.post("/auth/logout")
def logout(response: Response, p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    for s in db.scalars(select(AuthSession).where(AuthSession.user_id == p.id)):
        db.delete(s)
    db.commit()
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@router.get("/auth/me")
def me(p: Principal = Depends(current_user)) -> dict:
    return _user_out(p.user, p.org)


class UserIn(BaseModel):
    username: str = Field(min_length=3, max_length=100)
    password: str = Field(min_length=10, max_length=200)
    role: str = Field(pattern="^(admin|operator|reviewer)$")
    perms: dict[str, bool] = {}


class UserPatch(BaseModel):
    role: str | None = Field(default=None, pattern="^(admin|operator|reviewer)$")
    perms: dict[str, bool] | None = None
    active: bool | None = None
    password: str | None = Field(default=None, min_length=10, max_length=200)


@router.get("/users")
def list_users(p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    require(p, "settings")
    return [{"id": u.id, "username": u.username, "role": u.role, "perms": u.perms or {}, "active": u.active}
            for u in db.scalars(select(User).where(User.organization_id == p.org.id).order_by(User.username))]


@router.post("/users", status_code=201)
def create_user(body: UserIn, p: Principal = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    require(p, "settings")
    if db.scalar(select(User).where(User.username == body.username)):
        raise AppError(409, "USERNAME_TAKEN", "이미 있는 아이디입니다")
    perms = {k: bool(v) for k, v in body.perms.items() if k in PERMISSIONS}
    u = User(organization_id=p.org.id, username=body.username, password_hash=hash_password(body.password),
             role=body.role, perms=perms)
    db.add(u)
    db.flush()
    audit(db, p, "user.create", "user", u.id, {"role": body.role, "perms": perms})
    db.commit()
    return {"id": u.id}


@router.patch("/users/{user_id}")
def patch_user(user_id: str, body: UserPatch, p: Principal = Depends(current_user),
               db: Session = Depends(get_db)) -> dict:
    require(p, "settings")
    u = db.get(User, user_id)
    if u is None or u.organization_id != p.org.id:
        raise AppError(404, "NOT_FOUND", "사용자가 없습니다")
    if u.id == p.id and (body.active is False or (body.role and body.role != "admin")):
        raise AppError(409, "SELF_LOCKOUT", "자기 자신의 관리자 권한은 해제할 수 없습니다")
    if body.role:
        u.role = body.role
    if body.perms is not None:
        u.perms = {k: bool(v) for k, v in body.perms.items() if k in PERMISSIONS}
    if body.active is not None:
        u.active = body.active
    if body.password:
        u.password_hash = hash_password(body.password)
        for s in db.scalars(select(AuthSession).where(AuthSession.user_id == u.id)):
            db.delete(s)
    audit(db, p, "user.update", "user", u.id, {"role": u.role, "perms": u.perms, "active": u.active,
                                                "password_reset": bool(body.password)})
    db.commit()
    return {"ok": True}
