"""대화 시험(회귀): 사용자 제공 2145건 합성 대화로 상태기계가 기대한 안내를 고르는지.

tests/ars_eval.py 와 같은 판정. 서버의 '시나리오 제안 자동 검사'가 변경을 넣기 전·후 결과를 비교하는 데 쓴다.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .flow import CallFlow, Play, StartAI

REF = date(2026, 10, 27)
CONFIRM = {"CUSTOM_DATE_CONFIRM", "CUSTOM_CONFIRM_VARIANT", "CUSTOM_CONFIRM_TENTH", "CUSTOM_CONFIRM_CORRECTION",
           "CUSTOM_TOMORROW_CONFIRM", "CUSTOM_TODAY_CONFIRM", "CUSTOM_MONDAY_CONFIRM"}
SPECIAL = {
    "ACK_CONTINUE": {"ask_available_date"}, "WORKING_NOW": {"ask_end_date"}, "AVAILABLE_DATE": {"closing"},
    "AVAILABLE_NOW": {"closing"}, "BUSY": {"callback"}, "DO_NOT_CALL": {"refusal_ack"}, "WRONG_PERSON": {"wrong_person"},
    "UNKNOWN_SCHEDULE": {"sc_unknown_schedule", "sc_end_unknown"}, "CUSTOM_MANAGER": {"sc_custom_manager"},
    "CUSTOM_MANAGER_UNKNOWN": {"sc_custom_manager"},
}


def default_corpus() -> Path | None:
    for base in Path(__file__).resolve().parents:
        p = base / "tests" / "fixtures" / "ars_dialogs_2145.json"
        if p.is_file():
            return p
    return None


def expected_keys(code: str) -> set[str]:
    if code in CONFIRM:
        return {"confirm_date"}
    return SPECIAL.get(code, {"sc_" + code.lower()})


def run_corpus(path: Path | None = None, scenario_ext: dict | None = None, clips: set[str] | None = None,
               limit: int | None = None) -> dict:
    from .scenario_set import ScenarioSet
    path = path or default_corpus()
    if path is None:
        return {"total": 0, "ok": 0, "rate": None, "wrong": {}}
    data = json.loads(Path(path).read_text(encoding="utf-8"))["dialogs"][:limit]
    keys = set(clips) if clips is not None else set(ScenarioSet(scenario_ext).keys)
    total = ok = 0
    wrong: dict[str, int] = {}
    examples: dict[str, str] = {}
    for dlg in data:
        f = CallFlow(REF, clips=keys, scenario_ext=scenario_ext)
        f.answered()
        for i, (text, code) in enumerate(dlg["turns"]):
            if code is None:
                continue
            total += 1
            if f.finished:
                got = "(이미 종료)"
            else:
                acts = f.utterance(text, turn=f.turn, event_id=f"{dlg['id']}-{i}")
                if any(isinstance(a, StartAI) for a in acts):
                    got = "AI"
                    f.ai_done(turn=f.turn)
                else:
                    got = next((a.key for a in acts if isinstance(a, Play)), "(무응답)")
            if got in expected_keys(code):
                ok += 1
            else:
                k = f"{code} → {got}"
                wrong[k] = wrong.get(k, 0) + 1
                examples.setdefault(k, text)
                break
    return {"total": total, "ok": ok, "rate": ok / total if total else None, "wrong": wrong, "examples": examples}
