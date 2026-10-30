"""통화로 시나리오를 만들고 고치는 기능(2026-10-30 사용자 승인: '제안 → 자동 검사 → 사람 승인 → 반영').

① 기록   통화 이벤트에서 '규칙이 못 알아들어 AI 로 간 말', 'AI 가 고른 녹음 답', '답 직후 상대 종료'를 모은다.
② 분석   AI 고르기로 연결된 말은 그 답의 표현 추가(ADD_EXAMPLES)로 바로 제안하고,
         연결 못 된 말은 글 모델이 비슷한 것끼리 묶어 새 답(NEW)이나 표현 추가를 제안한다.
③ 검사   문구 규칙(호칭·금액·약속·길이), 2145건 대화 시험(변경 전과 같아야 함), 예시 말 연결을 확인한다.
④ 승인   사람이 승인한 것만 ScenarioChange 로 저장 → 다음 발신권(claim)부터 에이전트가 사용.
⑤ 측정   답마다 재생 횟수와 '재생 직후 상대 종료' 비율.
자동 반영은 회사 설정(policies.auto_apply_examples)이 켜져 있고, 검사를 통과한 '표현 추가'만.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta

from aicc_core.scenario_set import EDITABLE_KINDS, LEARNED_PREFIX, ScenarioSet, phrase_keyword
from aicc_core.scenarios import normalize
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import CallEvent, CallSession, Organization, ScenarioChange, ScenarioProposal

log = logging.getLogger("aicc.learning")

ALLOWED_OUTCOMES = ("CALLBACK_REQUESTED", "UNCONFIRMED")
ALLOWED_REVIEWS = (None, "PAY_REVIEW", "CONTRACT_REVIEW", "CONTACT_REVIEW", "CARE_REVIEW", "DIFFICULTY_REVIEW", "HEALTH",
                   "REPLACEMENT_REVIEW")
BANNED = ("만원", "%", "퍼센트", "무조건", "보장", "확정해 드", "확정해드", "약속드", "바로 배정", "꼭 드릴", "반드시 드")


def _mask(text: str) -> str:
    try:
        from aicc_agent.transcribe import mask
        return mask(text)
    except Exception:  # noqa: BLE001
        return re.sub(r"\d[\d\s-]{6,}\d", "[숫자]", text)


# ---------------------------------------------------------------- 묶음(에이전트로 내려감)
def scenario_ext(db: Session, org_id: str) -> dict:
    learned, disabled, extra, examples = [], [], {}, {}
    rows = db.scalars(select(ScenarioChange).where(ScenarioChange.organization_id == org_id,
                                                   ScenarioChange.active.is_(True))
                      .order_by(ScenarioChange.created_at)).all()
    for r in rows:
        d = r.data or {}
        if r.action == "LEARNED":
            learned.append({"code": r.code, **{k: d.get(k) for k in ("kind", "text", "keywords", "examples", "note",
                                                                      "outcome", "review")}})
        elif r.action == "DISABLE":
            disabled.append(r.code)
        elif r.action == "ADD_KEYWORDS":
            extra.setdefault(r.code, []).extend(d.get("keywords") or [])
            examples.setdefault(r.code, []).extend(d.get("examples") or [])
    return {"learned": learned, "disabled": sorted(set(disabled)), "extra_keywords": extra, "examples": examples}


# ---------------------------------------------------------------- ① 기록
def collect(db: Session, org_id: str, days: int = 7) -> dict:
    """최근 통화에서 AI 로 넘어간 말(+AI 가 고른 답)과 답별 재생·직후 종료를 모은다."""
    from .services import utcnow
    since = utcnow() - timedelta(days=days)
    calls = db.scalars(select(CallSession).where(CallSession.organization_id == org_id,
                                                 CallSession.is_mock.is_(False),
                                                 CallSession.started_at >= since)).all()
    unmatched: list[dict] = []
    plays: dict[str, int] = {}
    hang_after: dict[str, int] = {}
    for c in calls:
        evs = list(db.scalars(select(CallEvent).where(CallEvent.call_id == c.id).order_by(CallEvent.seq)))
        last_utt = None
        for i, e in enumerate(evs):
            pl = e.payload or {}
            if e.type == "utterance.final":
                last_utt = pl
            elif e.type == "ai.start" and last_utt and (last_utt.get("text") or "").strip():
                item = {"text": _mask(str(last_utt["text"]))[:120], "state": pl.get("return_state") or last_utt.get("state"),
                        "call_id": c.id, "routed": None, "ai_text": None, "hangup_after": False}
                for e2 in evs[i + 1:i + 8]:
                    if e2.type == "ai.routed":
                        item["routed"] = (e2.payload or {}).get("code")
                    elif e2.type == "ai.done":
                        item["ai_text"] = ((e2.payload or {}).get("text") or "")[:120]
                    elif e2.type == "call.remote_hangup":
                        item["hangup_after"] = True
                    elif e2.type == "utterance.final":
                        break
                unmatched.append(item)
            elif e.type == "play":
                key = str(pl.get("key") or "")
                if not key.startswith("sc_"):
                    continue
                plays[key] = plays.get(key, 0) + 1
                nxt = [x.type for x in evs[i + 1:i + 6]]
                if "call.remote_hangup" in nxt and "utterance.final" not in nxt[:nxt.index("call.remote_hangup")]:
                    hang_after[key] = hang_after.get(key, 0) + 1
    return {"unmatched": unmatched, "plays": plays, "hangup_after": hang_after, "calls": len(calls)}


def answer_stats(db: Session, org_id: str, days: int = 30) -> list[dict]:
    c = collect(db, org_id, days)
    out = []
    for key, n in sorted(c["plays"].items(), key=lambda x: -x[1]):
        h = c["hangup_after"].get(key, 0)
        out.append({"key": key, "code": key[3:].upper(), "plays": n, "hangup_after": h,
                    "hangup_rate": round(100 * h / n, 1) if n else None})
    return out


# ---------------------------------------------------------------- ② 제안
PROPOSE_PROMPT = """너는 간병 매칭 회사의 전화 응대 시나리오 설계자다. 회사는 간병사에게 전화해 '다음 근무를 언제부터 할 수 있는지'만 확인한다.
아래는 최근 통화에서 자동 응답이 알아듣지 못해 AI 로 넘어간 간병사의 말들이다(번호: 말 / 그때 묻던 것).

{items}

이미 있는 녹음 답 목록(코드: 답):
{catalog}

할 일: 뜻이 같은 말끼리 묶어서, 묶음마다 하나를 제안한다.
- 이미 있는 답 중 뜻이 정확히 맞는 것이 있으면 {{"action":"ADD_EXAMPLES","target_code":"그 코드","examples":[번호...]}}
- 없으면 새 녹음 답 {{"action":"NEW","code_hint":"영문_대문자_짧게","kind":"INFO|REVIEW|PREF|END","text":"답 문장","examples":[번호...],
  "note":"메모(선호·사정, 10자 이내, 없으면 빈칸)","outcome":"END 일 때만 CALLBACK_REQUESTED 또는 UNCONFIRMED","review":"담당자 확인이 필요하면 PAY_REVIEW|CONTRACT_REVIEW|CONTACT_REVIEW|CARE_REVIEW|DIFFICULTY_REVIEW|HEALTH, 아니면 null","why":"이유"}}
새 답 문장 규칙:
- 상대를 '선생님'이라 부르고, 한두 문장 90자 이내, 전화 말투.
- 금액·수수료·배정·일정 확정을 약속하거나 사실처럼 말하지 않는다(모르면 '실제 일감 소개 때 안내', '담당 실장님이 확인해 연락').
- INFO·PREF·REVIEW 답은 끝에 근무 가능 날짜를 부드럽게 다시 묻는다. END 는 정중히 마무리한다.
- 한 번만 나온 애매한 말, 날짜 답, 단순 네/아니요는 제안하지 않는다.
JSON 하나만: {{"proposals":[...]}}
"""


def build_propose_prompt(items: list[dict], catalog: list[tuple[str, str]]) -> str:
    lines = "\n".join(f"{i}: {it['text']} / {it.get('state') or '-'}" for i, it in enumerate(items))
    cat = "\n".join(f"{c}: {t}" for c, t in catalog)
    return PROPOSE_PROMPT.format(items=lines, catalog=cat)


def gemini_llm(prompt: str) -> str:  # pragma: no cover - 외부 API
    from google import genai
    from google.genai import types

    from .routers.admin import _read_agent_env
    key = _read_agent_env().get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("Gemini 키가 없습니다(설정 > 단말 > 이 PC 에이전트)")
    client = genai.Client(api_key=key)
    cfg = types.GenerateContentConfig(temperature=0.2, max_output_tokens=4000, response_mime_type="application/json")
    last = None
    for m in ("gemini-3.8-flash", "gemini-3.5-flash-lite"):
        try:
            return client.models.generate_content(model=m, contents=prompt, config=cfg).text or ""
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"제안 생성 실패: {last}")


def _slug(hint: str) -> str:
    h = re.sub(r"[^A-Z0-9_]", "", (hint or "").upper().replace(" ", "_"))[:30].strip("_")
    return LEARNED_PREFIX + (h or uuid.uuid4().hex[:6].upper())


def _clean_draft(p: dict, items: list[dict], codes: set[str]) -> tuple[str, str | None, dict] | None:
    action = p.get("action")
    idx = [i for i in (p.get("examples") or []) if isinstance(i, int) and 0 <= i < len(items)]
    examples = list(dict.fromkeys(items[i]["text"] for i in idx))
    if not examples:
        return None
    if action == "ADD_EXAMPLES":
        tc = p.get("target_code")
        if tc not in codes:
            return None
        return "ADD_EXAMPLES", tc, {"examples": examples}
    if action == "NEW":
        kind = p.get("kind")
        text = str(p.get("text") or "").strip()
        if kind not in EDITABLE_KINDS or not text:
            return None
        outcome = p.get("outcome") if kind == "END" else None
        review = p.get("review") if p.get("review") in ALLOWED_REVIEWS else None
        if kind == "END" and outcome not in ALLOWED_OUTCOMES:
            outcome = "UNCONFIRMED"
        return "NEW", None, {"code": _slug(p.get("code_hint") or ""), "kind": kind, "text": text[:150],
                             "examples": examples, "note": str(p.get("note") or "")[:10], "outcome": outcome,
                             "review": review, "why": str(p.get("why") or "")[:200]}
    return None


def generate(db: Session, org: Organization, llm=gemini_llm, days: int = 7) -> list[ScenarioProposal]:  # noqa: ANN001
    """최근 통화로 제안을 만든다(이미 대기 중인 같은 제안은 다시 만들지 않음)."""
    data = collect(db, org.id, days)
    items = data["unmatched"]
    ext = scenario_ext(db, org.id)
    sset = ScenarioSet(ext)
    # 이미 제안한 말(대기·승인·거절 모두)과 이미 배운 표현은 다시 제안하지 않는다
    prev = db.scalars(select(ScenarioProposal).where(ScenarioProposal.organization_id == org.id)).all()
    seen = {normalize(t) for pr in prev for t in (pr.draft or {}).get("examples", [])}
    known = {normalize(t) for v in ext["examples"].values() for t in v} | \
            {normalize(t) for d in ext["learned"] for t in (d.get("examples") or [])}
    fresh = [it for it in items if normalize(it["text"]) not in seen | known]
    made: list[ScenarioProposal] = []
    # (a) AI 고르기로 이미 연결된 말 → 그 답의 표현 추가(글 모델 없이)
    by_code: dict[str, list[dict]] = {}
    for it in fresh:
        if it["routed"] and it["routed"] in sset.by_code:
            by_code.setdefault(it["routed"], []).append(it)
    for code, its in by_code.items():
        made.append(_new_proposal(db, org, "ADD_EXAMPLES", code, {"examples": [x["text"] for x in its][:10]}, its, ext))
    # (b) 연결 못 된 말 → 글 모델이 묶어 제안
    rest = [it for it in fresh if not it["routed"]][:150]
    if len(rest) >= 1 and llm is not None:
        catalog = [(s.code, re.sub(r"\s+", " ", s.say)[:60]) for s in sset.scenarios if s.kind in EDITABLE_KINDS]
        raw = llm(build_propose_prompt(rest, catalog))
        m = re.search(r"\{.*\}", raw or "", re.S)
        try:
            props = json.loads(m.group(0)).get("proposals", []) if m else []
        except json.JSONDecodeError:
            props = []
        for p in props[:30]:
            c = _clean_draft(p, rest, set(sset.by_code))
            if c is None:
                continue
            action, target, draft = c
            ev = [it for it in rest if it["text"] in draft["examples"]]
            made.append(_new_proposal(db, org, action, target or draft.get("code"), draft, ev, ext))
    db.flush()
    if (org.policies or {}).get("auto_apply_examples"):
        for pr in made:
            if pr.action == "ADD_EXAMPLES" and pr.checks.get("ok"):
                approve(db, org, pr, actor_id="auto", note="자동 반영(표현 추가, 검사 통과)")
    return made


def _new_proposal(db: Session, org: Organization, action: str, target: str | None, draft: dict, ev: list[dict],
                  ext: dict) -> ScenarioProposal:
    pr = ScenarioProposal(organization_id=org.id, status="PENDING", action=action, target_code=target, draft=draft,
                          evidence={"items": ev[:20], "count": len(ev)}, checks={})
    pr.checks = check(pr, ext)
    db.add(pr)
    return pr


# ---------------------------------------------------------------- ③ 검사
def apply_to_ext(ext: dict, action: str, target: str | None, draft: dict) -> dict:
    new = json.loads(json.dumps(ext))
    if action == "ADD_EXAMPLES":
        kws = [k for k in (phrase_keyword(t) for t in draft.get("examples", [])) if k]
        new["extra_keywords"].setdefault(target, []).extend(kws)
        new["examples"].setdefault(target, []).extend(draft.get("examples", []))
    elif action == "NEW":
        kws = draft.get("keywords") or [k for k in (phrase_keyword(t) for t in draft.get("examples", [])) if k]
        new["learned"].append({"code": draft["code"], "kind": draft["kind"], "text": draft["text"], "keywords": kws,
                               "examples": draft.get("examples", []), "note": draft.get("note") or "",
                               "outcome": draft.get("outcome"), "review": draft.get("review")})
    return new


_CORPUS_CACHE: dict[str, dict] = {}


def _corpus(ext: dict) -> dict:
    from aicc_core.regression import run_corpus
    k = json.dumps(ext, sort_keys=True, ensure_ascii=False)
    if k not in _CORPUS_CACHE:
        r = run_corpus(scenario_ext=ext)
        if len(_CORPUS_CACHE) > 20:
            _CORPUS_CACHE.clear()
        _CORPUS_CACHE[k] = {"ok": r["ok"], "total": r["total"], "wrong": dict(list(r["wrong"].items())[:5])}
    return _CORPUS_CACHE[k]


def check(pr: ScenarioProposal, ext: dict) -> dict:
    errors, warnings = [], []
    d = pr.draft or {}
    if pr.action == "NEW":
        t = d.get("text") or ""
        if "간병사님" in t:
            errors.append("호칭은 '선생님'으로")
        bad = [b for b in BANNED if b in t]
        if bad:
            errors.append(f"확정 안 된 금액·약속 표현: {', '.join(bad)}")
        if len(t) > 120:
            errors.append("답이 너무 깁니다(120자 이내)")
        if d.get("kind") == "END" and d.get("outcome") not in ALLOWED_OUTCOMES:
            errors.append("종료 답에는 결과(재연락/미확정)가 필요합니다")
        if d.get("kind") in ("INFO", "PREF", "REVIEW") and not re.search(r"언제|날짜|며칠|가능하", t):
            warnings.append("답 끝에 근무 가능 날짜를 다시 묻지 않습니다")
    after = apply_to_ext(ext, pr.action, pr.target_code, d)
    before_r, after_r = _corpus(ext), _corpus(after)
    if after_r["ok"] < before_r["ok"]:
        errors.append(f"대화 시험 정답이 줄어듭니다({before_r['ok']} → {after_r['ok']}/{after_r['total']})")
    sset = ScenarioSet(after)
    target = pr.target_code if pr.action == "ADD_EXAMPLES" else d.get("code")
    ex = []
    for t in d.get("examples", []):
        m = sset.match(t)
        ex.append({"text": t, "matched": m.code if m else None, "ok": bool(m and m.code == target)})
    stolen = [e for e in ex if e["matched"] and e["matched"] != target]
    if stolen:
        warnings.append(f"예시 {len(stolen)}개는 다른 답({stolen[0]['matched']})이 먼저 잡습니다")
    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "corpus": {"before": before_r["ok"], "after": after_r["ok"], "total": after_r["total"]}, "examples": ex}


# ---------------------------------------------------------------- ④ 승인
def approve(db: Session, org: Organization, pr: ScenarioProposal, *, actor_id: str, edits: dict | None = None,
            note: str | None = None) -> ScenarioChange:
    if pr.status != "PENDING":
        raise ValueError("이미 처리된 제안입니다")
    d = dict(pr.draft or {}, **(edits or {}))
    if edits:
        pr.draft = d
        pr.checks = check(pr, scenario_ext(db, org.id))
    if not pr.checks.get("ok"):
        raise ValueError("자동 검사를 통과하지 못한 제안입니다: " + "; ".join(pr.checks.get("errors", [])))
    if pr.action == "ADD_EXAMPLES":
        kws = [k for k in (phrase_keyword(t) for t in d.get("examples", [])) if k]
        ch = ScenarioChange(organization_id=org.id, code=pr.target_code, action="ADD_KEYWORDS",
                            data={"keywords": kws, "examples": d.get("examples", [])}, proposal_id=pr.id,
                            created_by=actor_id)
    else:
        code = d["code"]
        exists = db.scalar(select(ScenarioChange).where(ScenarioChange.organization_id == org.id,
                                                        ScenarioChange.code == code))
        if exists:
            code = f"{code}_{uuid.uuid4().hex[:4].upper()}"
        kws = d.get("keywords") or [k for k in (phrase_keyword(t) for t in d.get("examples", [])) if k]
        ch = ScenarioChange(organization_id=org.id, code=code, action="LEARNED",
                            data={"kind": d["kind"], "text": d["text"], "keywords": kws, "examples": d.get("examples", []),
                                  "note": d.get("note") or "", "outcome": d.get("outcome"), "review": d.get("review")},
                            proposal_id=pr.id, created_by=actor_id)
    db.add(ch)
    pr.status = "APPROVED"
    pr.decided_by = actor_id
    pr.decided_at = datetime.now().astimezone()
    pr.decision_note = note
    db.flush()
    return ch


# ---------------------------------------------------------------- 매일 밤 자동 분석(통합 실행 프로그램 안에서)
def start_nightly(hour: int = 21) -> None:  # pragma: no cover - 백그라운드 스레드
    def loop() -> None:
        from zoneinfo import ZoneInfo

        from .db import SessionLocal
        kst = ZoneInfo("Asia/Seoul")
        last = None
        while True:
            now = datetime.now(kst)
            if now.hour == hour and last != now.date():
                last = now.date()
                try:
                    with SessionLocal() as db:
                        for org in db.scalars(select(Organization).where(Organization.environment == "production")):
                            made = generate(db, org)
                            log.info("시나리오 제안 %d건 생성(%s)", len(made), org.name)
                        db.commit()
                except Exception as e:  # noqa: BLE001
                    log.warning("밤 시나리오 분석 실패: %s", e)
            time.sleep(300)

    threading.Thread(target=loop, daemon=True, name="scenario-nightly").start()
