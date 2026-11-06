"""사용자 제공 시나리오 2145건(합성 대화)으로 대화 흐름이 기대한 안내를 고르는지 측정한다.

실행: python tests/ars_eval.py   (요약·오답 상위 출력)
시험: tests/test_ars_corpus.py 가 전체 정답률 하한을 확인한다.
"""

from __future__ import annotations

import collections
import json
from datetime import date
from pathlib import Path

from aicc_core.flow import CallFlow, Play, StartAI
from aicc_core.scenarios import SCENARIO_KEYS

CORPUS = Path(__file__).parent / "fixtures" / "ars_dialogs_2145.json"
REF = date(2026, 10, 27)  # 시나리오 작성일(날짜 표현 해석 기준)
CONFIRM = {"CUSTOM_DATE_CONFIRM", "CUSTOM_CONFIRM_VARIANT", "CUSTOM_CONFIRM_TENTH", "CUSTOM_CONFIRM_CORRECTION",
           "CUSTOM_TOMORROW_CONFIRM", "CUSTOM_TODAY_CONFIRM", "CUSTOM_MONDAY_CONFIRM"}
SPECIAL = {
    "ACK_CONTINUE": {"ask_available_date"}, "WORKING_NOW": {"ask_end_date"}, "AVAILABLE_DATE": {"closing"},
    "AVAILABLE_NOW": {"closing"}, "BUSY": {"callback"}, "DO_NOT_CALL": {"refusal_ack"}, "WRONG_PERSON": {"wrong_person"},
    "UNKNOWN_SCHEDULE": {"sc_unknown_schedule", "sc_end_unknown"}, "CUSTOM_MANAGER": {"sc_custom_manager"},
    "CUSTOM_MANAGER_UNKNOWN": {"sc_custom_manager"},
}


def expected_keys(code: str) -> set[str]:
    if code in CONFIRM:
        return {"confirm_date"}
    return SPECIAL.get(code, {"sc_" + code.lower()})


def run(limit: int | None = None) -> dict:
    data = json.loads(CORPUS.read_text(encoding="utf-8"))["dialogs"][:limit]
    total = ok = 0
    per = collections.Counter()
    per_ok = collections.Counter()
    wrong = collections.Counter()
    examples: dict[tuple, str] = {}
    for dlg in data:
        f = CallFlow(REF, clips=set(SCENARIO_KEYS))
        f.answered()
        for i, (text, code) in enumerate(dlg["turns"]):
            if code is None:
                continue
            total += 1
            per[code] += 1
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
                per_ok[code] += 1
            else:
                wrong[(code, got)] += 1
                examples.setdefault((code, got), text)
                break  # 한 번 어긋나면 그 대화의 뒷부분은 비교하지 않는다
    return {"total": total, "ok": ok, "rate": ok / total if total else 0, "per": per, "per_ok": per_ok, "wrong": wrong,
            "examples": examples}


if __name__ == "__main__":
    r = run()
    print(f"정답 {r['ok']}/{r['total']} = {r['rate']:.1%}")
    print("\n오답 상위:")
    for (code, got), n in r["wrong"].most_common(40):
        print(f"  {n:4d}  기대 {code:28s} → {got:28s} 예: {r['examples'][(code, got)]}")
