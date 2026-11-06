"""서버 API 클라이언트(장치 토큰). 실패한 이벤트는 로컬 저널에 남기고 재연결 시 event_id 로 중복 없이 보낸다."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import httpx


class ApiError(RuntimeError):
    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.details = details or {}


class AgentClient:
    def __init__(self, http: httpx.Client | Any, token: str, journal: Path | None = None, base: str = ""):
        self.http = http
        self.base = base.rstrip("/")
        self.h = {"Authorization": f"Device {token}"}
        self.journal = journal

    def _req(self, method: str, path: str, *, json_body: dict | None = None, idem: str | None = None,
             files: dict | None = None, data: dict | None = None) -> dict:
        headers = dict(self.h)
        if idem:
            headers["Idempotency-Key"] = idem
        r = self.http.request(method, self.base + path, json=json_body, headers=headers, files=files, data=data)
        if r.status_code >= 400:
            try:
                err = r.json()["error"]
            except Exception:  # noqa: BLE001
                err = {"code": "HTTP_ERROR", "message": r.text[:200]}
            raise ApiError(r.status_code, err.get("code", "?"), err.get("message", ""), err.get("details"))
        return r.json()

    def heartbeat(self, **payload: Any) -> dict:
        return self._req("POST", "/api/v1/agent/heartbeat", json_body=payload)

    def claim(self) -> dict:
        return self._req("POST", "/api/v1/agent/claim")

    def events(self, call_id: str, events: list[dict]) -> dict:
        try:
            return self._req("POST", f"/api/v1/agent/calls/{call_id}/events", json_body={"events": events})
        except (httpx.HTTPError, ApiError) as e:
            if isinstance(e, ApiError) and e.status < 500:
                raise
            self._journal(call_id, events)
            raise

    def _journal(self, call_id: str, events: list[dict]) -> None:
        if not self.journal:
            return
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        with self.journal.open("a", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps({"call_id": call_id, **e}, ensure_ascii=False) + "\n")

    def replay_journal(self) -> int:
        if not self.journal or not self.journal.is_file():
            return 0
        lines = [json.loads(x) for x in self.journal.read_text(encoding="utf-8").splitlines() if x.strip()]
        by_call: dict[str, list[dict]] = {}
        for e in lines:
            by_call.setdefault(e.pop("call_id"), []).append(e)
        for cid, evs in by_call.items():
            self._req("POST", f"/api/v1/agent/calls/{cid}/events", json_body={"events": evs})
        self.journal.unlink()
        return len(lines)

    def confirm(self, call_id: str, evidence_event_id: str) -> dict:
        return self._req("POST", f"/api/v1/calls/{call_id}/schedule-confirmations",
                         json_body={"evidence_event_id": evidence_event_id}, idem=f"confirm-{evidence_event_id}")

    def block(self, call_id: str, evidence_event_id: str) -> dict:
        return self._req("POST", f"/api/v1/agent/calls/{call_id}/block",
                         json_body={"evidence_event_id": evidence_event_id})

    def finish(self, call_id: str, **payload: Any) -> dict:
        return self._req("POST", f"/api/v1/agent/calls/{call_id}/finish", json_body=payload)

    def upload_recording(self, call_id: str, wav: bytes, sha256: str, duration_ms: int) -> dict:
        return self._req("POST", f"/api/v1/agent/calls/{call_id}/recording",
                         files={"file": ("call.wav", wav, "audio/wav")},
                         data={"sha256": sha256, "duration_ms": str(duration_ms)})

    def post_analysis(self, call_id: str, analysis: dict) -> dict:
        return self._req("POST", f"/api/v1/agent/calls/{call_id}/analysis", json_body=analysis)

    def recording_failed(self, call_id: str, error: str) -> dict:
        return self._req("POST", f"/api/v1/agent/calls/{call_id}/recording-failed", json_body={"error": error})


def new_event_id() -> str:
    return str(uuid.uuid4())
