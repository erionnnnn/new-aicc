from __future__ import annotations

import base64
import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

os.environ.setdefault("AICC_ENV", "test")
os.environ["AICC_DATA_KEY"] = base64.urlsafe_b64encode(b"k" * 32).decode()
os.environ["AICC_HMAC_KEY"] = "test-hmac-key-not-for-production"


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("AICC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AICC_WEB_DIST", str(tmp_path / "noweb"))
    from aicc_api.config import get_settings
    from aicc_api.db import reset_engine
    get_settings.cache_clear()
    reset_engine()
    from aicc_api.main import create_app
    from fastapi.testclient import TestClient
    with TestClient(create_app()) as c:
        yield c
    reset_engine()
    get_settings.cache_clear()


class Api:
    """시험용 클라이언트 래퍼. Bearer 토큰으로 인증."""

    def __init__(self, client, token: str | None = None, device: str | None = None):
        self.c = client
        self.token = token
        self.device = device

    def _h(self, extra=None, idem=False):
        h = {}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if self.device:
            h["Authorization"] = f"Device {self.device}"
        if idem:
            h["Idempotency-Key"] = idem if isinstance(idem, str) else uuid.uuid4().hex
        h.update(extra or {})
        return h

    def get(self, url, **kw):
        return self.c.get(url, headers=self._h(), **kw)

    def post(self, url, json=None, idem=False, **kw):
        return self.c.post(url, json=json, headers=self._h(idem=idem), **kw)

    def put(self, url, json=None):
        return self.c.put(url, json=json, headers=self._h())

    def patch(self, url, json=None):
        return self.c.patch(url, json=json, headers=self._h())


def login(client, username, password) -> Api:
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    client.cookies.clear()
    return Api(client, r.json()["token"])


@pytest.fixture()
def admin(app_client):
    r = app_client.post("/api/v1/auth/bootstrap", json={"org_name": "시험 조직", "environment": "sandbox",
                                                         "username": "admin", "password": "admin-pass-123"})
    assert r.status_code == 201, r.text
    return login(app_client, "admin", "admin-pass-123")


def make_user(admin: Api, username: str, role: str, perms=None) -> Api:
    r = admin.post("/api/v1/users", json={"username": username, "password": "user-pass-1234", "role": role,
                                          "perms": perms or {}})
    assert r.status_code == 201, r.text
    return login(admin.c, username, "user-pass-1234")


def upload_commit(api: Api, data: bytes, semantics="NEXT_AVAILABLE", name="명부.xlsx") -> dict:
    r = api.c.post("/api/v1/imports", files={"file": (name, data)}, headers=api._h())
    assert r.status_code == 201, r.text
    up = r.json()
    v = api.post(f"/api/v1/imports/{up['import_id']}/validate",
                 json={"mapping": up["suggested_mapping"], "date_semantics": semantics})
    assert v.status_code == 200, v.text
    c = api.post(f"/api/v1/imports/{up['import_id']}/commit",
                 json={"validation_version": v.json()["validation_version"]})
    assert c.status_code == 201, c.text
    return {"upload": up, "validate": v.json(), "commit": c.json(), "batch_id": up["import_id"]}
