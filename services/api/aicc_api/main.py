"""FastAPI 앱. 웹 관리화면 정적 파일도 같은 출처에서 제공한다(쿠키 SameSite=Strict)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import errors
from .config import get_settings
from .db import SessionLocal
from .migrate import upgrade
from .routers import admin, agent, auth, campaigns, roster, scenarios

log = logging.getLogger("aicc")


class _RedactFilter(logging.Filter):
    """일반 로그에서 전화번호 형태를 가린다(설계 12 모니터링)."""

    import re as _re
    _pat = _re.compile(r"0\d{1,2}[- ]?\d{3,4}[- ]?\d{4}")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._pat.sub("***-****-****", record.msg)
        return True


@asynccontextmanager
async def lifespan(_: FastAPI):
    upgrade()
    from .services import purge_expired
    with SessionLocal() as db:
        purge_expired(db)
        db.commit()
    import os
    if os.environ.get("AICC_LOCAL_AGENT") == "1" and os.environ.get("AICC_LEARN_NIGHTLY", "1") != "0":
        from .learning import start_nightly
        start_nightly(int(os.environ.get("AICC_LEARN_HOUR", "21")))  # 매일 밤 통화 기록으로 시나리오 제안
    yield


def create_app() -> FastAPI:
    for name in ("aicc", "uvicorn.access", "uvicorn.error"):
        logging.getLogger(name).addFilter(_RedactFilter())
    s = get_settings()
    app = FastAPI(title="간병사 일정 확인 AICC", version="0.1.0", lifespan=lifespan,
                  docs_url=None if s.is_production else "/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
    errors.install(app)
    for r in (auth.router, roster.router, campaigns.router, agent.router, admin.router, scenarios.router):
        app.include_router(r)

    @app.get("/api/v1/health")
    def health() -> dict:
        return {"ok": True, "env": s.env}

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # noqa: ANN001
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.url.path.startswith("/api/"):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    web = s.web_dist
    if web and (web / "index.html").is_file():
        app.mount("/assets", StaticFiles(directory=web / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):  # noqa: ANN202
            if path.startswith("api/"):
                return JSONResponse({"error": {"code": "NOT_FOUND", "message": "없는 경로입니다"}}, status_code=404)
            f = (web / path).resolve()
            if path and f.is_file() and Path(web).resolve() in f.parents:
                return FileResponse(f)
            return FileResponse(web / "index.html", headers={"Cache-Control": "no-store"})
    return app

