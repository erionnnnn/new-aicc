"""통화 후 AI 분석(제안 전용).

통화가 끝나면 대화 전체(상담원 안내 문구 + 간병사 인식 문장)를 글 모델에 넘겨
근무 상태·종료일·가능일·선호 조건·현장 용건·한 줄 요약을 JSON 으로 뽑는다.

원칙
- 결과는 검수 화면의 'AI 제안'으로만 쓰인다. 서버는 이 값으로 날짜·상태를 바꾸지 않는다.
  날짜 반영은 지금처럼 통화 중 명시 동의(확인 질문에 '네')가 있을 때만 일어난다.
- 응답은 엄격히 검증한다: 형식이 틀린 날짜, 목록 밖 상태 값은 버린다.
- 요약·용건 문장에서 전화번호·긴 숫자는 가린다.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date

from .transcribe import mask

log = logging.getLogger("aicc.analysis")

WORK_STATUS = ("WORKING", "NOT_WORKING", "UNKNOWN")
WORK_STATUS_KO = {"WORKING": "근무 중", "NOT_WORKING": "근무 안 함", "UNKNOWN": "알 수 없음"}

ANALYSIS_PROMPT = """너는 간병 매칭 회사의 통화 검수 보조다. 아래는 상담원(자동 안내)과 간병사의 통화 기록이다.
'간병사:' 줄은 음성 인식 결과라 오타가 있을 수 있다. 기준일(오늘)은 {today} 이다.

간병사가 실제로 말한 내용만 근거로 다음 JSON 하나만 출력하라. 설명·코드블록 금지.
{{
  "work_status": "WORKING" | "NOT_WORKING" | "UNKNOWN",
  "end_date": "YYYY-MM-DD" 또는 null,        // 지금 현장이 끝나는 날
  "available_date": "YYYY-MM-DD" 또는 null,  // 새 일을 시작할 수 있는 날
  "preferences": ["..."],                     // 선호 조건(지역, 환자 상태, 기간, 요일, 성별 등). 짧게
  "issues": ["..."],                          // 현장 용건(급여, 민원, 교체 요청, 다른 문의 등). 짧게
  "summary": "한 문장 요약",
  "confidence": 0.0~1.0,
  "evidence": "날짜·상태의 근거가 된 간병사 말(짧게 인용)"
}}
규칙:
- 간병사가 말하지 않은 것은 추측하지 말고 null 또는 빈 목록.
- '다음 주 화요일', '월말' 같은 표현은 기준일로 날짜를 계산한다. 애매하면 null.
- 상담원 문장(질문)만 있고 간병사 답이 없으면 그 내용은 근거가 아니다.
- 이름·전화번호·주민번호 등 개인정보는 쓰지 않는다.

통화 기록:
{dialog}
"""

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def build_analysis_prompt(lines: list[str], today: date) -> str:
    dialog = "\n".join(line[:300] for line in lines[-80:])
    return ANALYSIS_PROMPT.format(today=today.isoformat(), dialog=dialog)


def _date(v: object, today: date) -> str | None:
    if not isinstance(v, str) or not _DATE.match(v.strip()):
        return None
    try:
        d = date.fromisoformat(v.strip())
    except ValueError:
        return None
    # 상식 밖 날짜는 버린다(과거 1년 이전·2년 이후)
    if (d - today).days < -366 or (d - today).days > 730:
        return None
    return d.isoformat()


def _short_list(v: object, n: int = 6) -> list[str]:
    if not isinstance(v, list):
        return []
    out = []
    for x in v:
        if isinstance(x, str) and x.strip():
            out.append(mask(x.strip())[:80])
        if len(out) >= n:
            break
    return out


def parse_analysis(text: str, today: date) -> dict | None:
    """모델 출력 → 검증된 dict. 해석할 수 없으면 None."""
    if not text:
        return None
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        return None
    try:
        raw = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    ws = raw.get("work_status")
    ws = ws if ws in WORK_STATUS else "UNKNOWN"
    try:
        conf = max(0.0, min(1.0, float(raw.get("confidence") or 0)))
    except (TypeError, ValueError):
        conf = 0.0
    summary = raw.get("summary") if isinstance(raw.get("summary"), str) else ""
    evidence = raw.get("evidence") if isinstance(raw.get("evidence"), str) else ""
    return {
        "work_status": ws,
        "end_date": _date(raw.get("end_date"), today),
        "available_date": _date(raw.get("available_date"), today),
        "preferences": _short_list(raw.get("preferences")),
        "issues": _short_list(raw.get("issues")),
        "summary": mask(summary.strip())[:200],
        "evidence": mask(evidence.strip())[:200],
        "confidence": round(conf, 2),
    }


class CallAnalyzer:
    name = "none"

    def analyze(self, lines: list[str], today: date) -> dict | None:  # pragma: no cover - 인터페이스
        raise NotImplementedError


class MockAnalyzer(CallAnalyzer):
    """테스트용: 고정 응답 문자열을 파싱한다."""

    name = "mock"

    def __init__(self, response: str):
        self.response = response
        self.prompts: list[str] = []

    def analyze(self, lines: list[str], today: date) -> dict | None:
        self.prompts.append(build_analysis_prompt(lines, today))
        return parse_analysis(self.response, today)




class LLMAnalyzer(CallAnalyzer):  # pragma: no cover - 외부 API
    """통화 후 분석(글 판단 AI: 기본 GPT, 설정에 따라 Gemini)."""

    def __init__(self, env: dict | None = None):
        from . import llm
        self.env = dict(os.environ) if env is None else env
        if not llm.available(self.env):
            raise RuntimeError("글 판단용 AI 키가 없습니다(OpenAI 또는 Gemini)")
        self.name = llm.name(self.env)

    def analyze(self, lines: list[str], today: date) -> dict | None:
        from . import llm
        try:
            raw = llm.complete(build_analysis_prompt(lines, today), json_mode=True, max_tokens=600, timeout=30,
                               env=self.env)
        except llm.LLMError as e:
            log.warning("통화 분석 실패(%s): %s", self.name, e)
            return None
        out = parse_analysis(raw, today)
        if out is not None:
            out["model"] = self.name
        return out


GeminiAnalyzer = LLMAnalyzer  # 예전 이름 호환
