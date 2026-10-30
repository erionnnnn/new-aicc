"""통화마다 쓰는 시나리오 묶음 = 코드의 기본 시나리오 + 회사가 승인한 변경(학습된 답·끄기·표현 추가).

2026-10-30 '전화를 계속 하면 시나리오를 만들고 고치는 기능': 승인된 변경은 서버 DB 에 있고, 발신권(claim)과 함께
에이전트로 내려와 이 묶음으로 합쳐진다. 코드의 기본 시나리오는 그대로 두고 덧붙이기만 한다.

ext 형식(서버 → 에이전트)
  {"learned": [{"code": "LRN_…", "kind": "INFO|REVIEW|PREF|END", "text": "…", "keywords": [...], "examples": [...],
                "note": "", "outcome": null, "review": null}],
   "disabled": ["FAQ_…"],                       # 끈 답(INFO·REVIEW·PREF·END 만)
   "extra_keywords": {"FAQ_…": ["정규식", …]},  # 기존 답에 더한 표현
   "examples": {"코드": ["실제 들어온 말", …]}}  # AI 녹음 고르기 참고용
"""

from __future__ import annotations

import dataclasses
import re

from .scenario_types import Scenario
from .scenarios import SCENARIOS, normalize

EDITABLE_KINDS = ("INFO", "REVIEW", "PREF", "END")
LEARNED_PREFIX = "LRN_"


def _ok_regex(k: str) -> bool:
    if not isinstance(k, str) or len(k) < 3 or len(k) > 200:
        return False
    try:
        re.compile(k)
    except re.error:
        return False
    return True


_CACHE: dict[str, re.Pattern] = {}


def _compile(k: str) -> re.Pattern:
    p = _CACHE.get(k)
    if p is None:
        p = _CACHE[k] = re.compile(k)
    return p


def phrase_keyword(text: str) -> str | None:
    """실제 들어온 말 → 그 표현만 찾는 안전한 키워드(공백·문장부호 뺀 그대로, 4자 이상)."""
    t = normalize(text)
    return re.escape(t) if len(t) >= 4 else None


class ScenarioSet:
    def __init__(self, ext: dict | None = None):
        ext = ext or {}
        disabled = {c for c in ext.get("disabled") or [] if isinstance(c, str)}
        extra = ext.get("extra_keywords") or {}
        self.examples: dict[str, list[str]] = {k: list(v)[:20] for k, v in (ext.get("examples") or {}).items()
                                               if isinstance(v, list)}
        base = []
        for s in SCENARIOS:
            if s.code in disabled and s.kind in EDITABLE_KINDS:
                continue
            add = tuple(k for k in extra.get(s.code) or [] if _ok_regex(k))
            base.append(dataclasses.replace(s, keywords=s.keywords + add) if add else s)
        learned = []
        for d in ext.get("learned") or []:
            try:
                code = str(d["code"])
                kind = str(d["kind"])
                text = str(d["text"]).strip()
            except (KeyError, TypeError):
                continue
            if not code.startswith(LEARNED_PREFIX) or kind not in EDITABLE_KINDS or not text or code in disabled:
                continue
            if kind == "END" and not d.get("outcome"):
                continue
            kws = tuple(k for k in d.get("keywords") or [] if _ok_regex(k))
            learned.append(Scenario(code, kind, kws, text, str(d.get("note") or "")[:30], d.get("outcome") or None,
                                    d.get("review") or None))
            if d.get("examples"):
                self.examples.setdefault(code, [str(x) for x in d["examples"]][:20])
        self.learned = tuple(learned)
        self.scenarios: tuple[Scenario, ...] = tuple(base) + self.learned  # 학습된 답은 맨 뒤(기존 답이 우선)
        self.by_code = {s.code: s for s in self.scenarios}
        self.disabled = frozenset(disabled)
        # 정규식 미리 컴파일(키워드가 수백 개라 re 모듈 캐시(512)를 넘으면 매번 다시 컴파일돼 느려짐)
        self._compiled = [(s, s.kind, [_compile(k) for k in s.keywords]) for s in self.scenarios]

    def match(self, text: str, kinds: tuple[str, ...] = ("DELETE", "REVIEW", "PREF", "END", "INFO")) -> Scenario | None:
        t = normalize(text)
        if not t:
            return None
        for s, kind, pats in self._compiled:
            if kind in kinds and any(p.search(t) for p in pats):
                return s
        return None

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(s.key for s in self.scenarios)
