"""암호화·해시·토큰. 전화번호와 API 키는 로그·오류 메시지에 넣지 않는다."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings


def _fernet() -> Fernet:
    return Fernet(get_settings().data_key)


def encrypt(data: bytes) -> bytes:
    return _fernet().encrypt(data)


def decrypt(token: bytes) -> bytes:
    return _fernet().decrypt(token)


def encrypt_json(obj: object) -> bytes:
    return encrypt(json.dumps(obj, ensure_ascii=False, default=str).encode())


def decrypt_json(token: bytes) -> object:
    return json.loads(decrypt(token))


def phone_hmac(org_id: str, normalized_phone: str) -> str:
    """조직 범위 전화번호 HMAC — 단순 해시는 번호 대입에 취약하므로 서버 비밀값 사용(설계 09)."""
    msg = f"{org_id}:{normalized_phone}".encode()
    return hmac.new(get_settings().hmac_key, msg, hashlib.sha256).hexdigest()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


# ---------- 비밀번호 (PBKDF2-SHA256) ----------
_ITER = 310_000


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, _ITER)
    return f"pbkdf2_sha256${_ITER}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, it, salt_b64, dk_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), base64.b64decode(salt_b64), int(it))
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except (ValueError, TypeError):
        return False


# ---------- 짧은 수명 서명 토큰(녹음 재생 등) ----------
def sign_token(payload: dict, ttl_seconds: int) -> str:
    body = dict(payload, exp=int(time.time()) + ttl_seconds)
    raw = base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode()).decode().rstrip("=")
    sig = hmac.new(get_settings().hmac_key, raw.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{raw}.{sig}"


def verify_signed(token: str) -> dict | None:
    try:
        raw, sig = token.rsplit(".", 1)
    except ValueError:
        return None
    good = hmac.new(get_settings().hmac_key, raw.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(good, sig):
        return None
    pad = "=" * (-len(raw) % 4)
    body = json.loads(base64.urlsafe_b64decode(raw + pad))
    if body.get("exp", 0) < time.time():
        return None
    return body


# ---------- 암호화 객체 저장소(로컬 디렉터리; 운영 시 비공개 객체 저장소 어댑터로 교체) ----------
def _objects_dir() -> Path:
    p = get_settings().data_dir / "objects"
    p.mkdir(parents=True, exist_ok=True)
    return p


def put_object(prefix: str, data: bytes) -> str:
    key = f"{prefix}/{secrets.token_hex(16)}"
    path = _objects_dir() / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encrypt(data))
    return key


def get_object(key: str) -> bytes:
    path = (_objects_dir() / key).resolve()
    if _objects_dir().resolve() not in path.parents:
        raise ValueError("invalid object key")
    try:
        return decrypt(path.read_bytes())
    except InvalidToken as e:  # 손상 또는 키 불일치
        raise ValueError("object integrity check failed") from e


def delete_object(key: str) -> bool:
    path = (_objects_dir() / key).resolve()
    if _objects_dir().resolve() not in path.parents:
        return False
    if path.exists():
        path.unlink()
        return True
    return False
