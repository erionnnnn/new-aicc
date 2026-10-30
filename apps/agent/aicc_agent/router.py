"""AI 녹음 선택: 규칙으로 못 알아들은 말에, 이미 녹음된 답 중 가장 맞는 것을 글 모델이 고른다.

2026-10-30 실전: AI 가 답 문장을 만들고(2~3초) 그 문장을 음성으로 만드는 데(6~7초) 8~10초 걸려, 처음 전화를 받은
사람이 기다리지 못하고 끊음. 녹음된 270여 개 답 중에서 고르기만 하면 음성 생성이 없어 2초 안팎이고, 목소리가
항상 녹음과 같으며 확인 안 된 말을 지어낼 수 없다. 맞는 녹음이 없을 때(NONE)만 기존 AI 답으로 넘어간다.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time

log = logging.getLogger("aicc.router")

ROUTER_PROMPT = """너는 간병사에게 다음 근무 가능 일정을 묻는 전화의 응답 선택기다.
상대(간병사)가 방금 한 말에 가장 알맞은 '녹음된 답' 하나를 아래 목록에서 고른다.

[지금 묻고 있는 것] {goal}
[최근 대화]
{dialog}
[상대가 방금 한 말] {text}

[녹음된 답 목록] (코드: 답 내용)
{candidates}

규칙:
- 상대 말의 뜻(질문·요청·사정)에 정확히 맞는 답만 고른다. 애매하게 비슷한 것은 고르지 않는다.
- 상대가 날짜를 말했거나 단순히 '네/아니요'면 NONE.
- 맞는 답이 없으면 NONE.
JSON 하나만 출력: {{"code": "코드 또는 NONE", "why": "짧은 이유"}}
"""



def build_candidates(scenarios, clips: set[str], examples: dict | None = None) -> list[tuple[str, str]]:  # noqa: ANN001
    """녹음이 있는 상황 답(INFO·REVIEW·PREF·END)만 후보로. 승인된 실제 표현이 있으면 '(예: …)'로 붙인다."""
    out = []
    for s in scenarios:
        if s.kind in ("INFO", "REVIEW", "PREF", "END") and s.key in clips:
            label = re.sub(r"\s+", " ", s.say)[:70]
            ex = (examples or {}).get(s.code) or []
            if ex:
                label += " (예: " + " / ".join(str(x)[:30] for x in ex[:3]) + ")"
            out.append((s.code, label))
    return out


def build_router_prompt(text: str, goal: str, dialog: list[str], candidates: list[tuple[str, str]]) -> str:
    cand = "\n".join(f"{c}: {t}" for c, t in candidates)
    return ROUTER_PROMPT.format(goal=goal, dialog="\n".join(dialog[-6:]) or "(없음)", text=text, candidates=cand)


def parse_choice(raw: str, codes: set[str]) -> str | None:
    """모델 출력 → 후보 안의 코드 또는 None."""
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    code = None
    if m:
        try:
            code = str(json.loads(m.group(0)).get("code") or "").strip()
        except (json.JSONDecodeError, AttributeError):
            code = None
    if code is None:
        m2 = re.search(r"\b(FAQ_[A-Z_]+|[A-Z][A-Z_]{2,})\b", raw)
        code = m2.group(1) if m2 else None
    if not code or code.upper() == "NONE":
        return None
    return code if code in codes else None


class RecordingRouter:
    name = "none"

    def choose(self, text: str, goal: str, dialog: list[str], candidates: list[tuple[str, str]],
               timeout_s: float = 3.5) -> str | None:  # pragma: no cover - 인터페이스
        raise NotImplementedError


class MockRouter(RecordingRouter):
    name = "mock"

    def __init__(self, answer: str | None):
        self.answer = answer
        self.prompts: list[str] = []

    def choose(self, text, goal, dialog, candidates, timeout_s=3.5):  # noqa: ANN001, ANN201
        self.prompts.append(build_router_prompt(text, goal, dialog, candidates))
        return parse_choice(json.dumps({"code": self.answer or "NONE"}), {c for c, _ in candidates})


class LLMRouter(RecordingRouter):  # pragma: no cover - 외부 API
    """글 판단 AI(기본 GPT, 설정에 따라 Gemini)로 녹음 답 고르기."""

    def __init__(self, env: dict | None = None):
        from . import llm
        self.env = dict(os.environ) if env is None else env
        if not llm.available(self.env):
            raise RuntimeError("글 판단용 AI 키가 없습니다(OpenAI 또는 Gemini)")
        self.name = llm.name(self.env)

    def choose(self, text, goal, dialog, candidates, timeout_s=3.0):  # noqa: ANN001, ANN201
        import concurrent.futures as cf

        from . import llm
        prompt = build_router_prompt(text, goal, dialog, candidates)
        t0 = time.monotonic()
        ex = cf.ThreadPoolExecutor(max_workers=1)
        try:
            raw = ex.submit(llm.complete, prompt, json_mode=True, max_tokens=80, timeout=timeout_s + 2,
                            env=self.env).result(timeout=timeout_s)
        except cf.TimeoutError:
            log.info("AI 녹음 선택(%s): 시간 초과(%dms)", self.name, int((time.monotonic() - t0) * 1000))
            return None
        except Exception as e:  # noqa: BLE001
            log.warning("AI 녹음 선택(%s) 실패: %s", self.name, str(e)[:150])
            return None
        finally:
            ex.shutdown(wait=False)
        code = parse_choice(raw, {c for c, _ in candidates})
        log.info("AI 녹음 선택(%s): %s (%dms)", self.name, code or "없음", int((time.monotonic() - t0) * 1000))
        return code


GeminiRouter = LLMRouter  # 예전 이름 호환
