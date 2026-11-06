from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class AppError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


def _body(code: str, message: str, details: dict | None = None) -> dict:
    return {"error": {"code": code, "message": message, "request_id": uuid.uuid4().hex[:12], "details": details or {}}}


def install(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(_body(exc.code, exc.message, exc.details), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # 입력값 원문은 개인정보일 수 있어 위치·종류만 돌려준다.
        details = {"fields": [{"loc": list(e.get("loc", [])), "type": e.get("type")} for e in exc.errors()]}
        return JSONResponse(_body("INVALID_REQUEST", "요청 형식이 올바르지 않습니다", details), status_code=400)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {401: "UNAUTHENTICATED", 403: "FORBIDDEN", 404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(
            exc.status_code, "HTTP_ERROR")
        return JSONResponse(_body(code, str(exc.detail)), status_code=exc.status_code)
