"""시나리오 자료형(scenarios.py·faq.py 공용, 순환 import 방지)."""

from __future__ import annotations

from dataclasses import dataclass


# 실제 상담원은 간병사를 '선생님'으로 부른다(회사 통화녹음 분석, 2026-10-27, 사용자 승인).
# 기존 녹음은 '간병사님'이라, 문구가 바뀐 칸은 녹음을 쓰지 않고 같은 Gemini 목소리로 다시 만든다.
def honorific(text: str) -> str:
    return text.replace("간병사님", "선생님")


@dataclass(frozen=True)
class Scenario:
    code: str
    kind: str
    keywords: tuple[str, ...]
    text: str
    note: str = ""            # 관찰 기록에 남길 메모(선호 조건 등)
    outcome: str | None = None  # END 일 때 통화 결과
    review: str | None = None   # END 일 때 검수 사유

    @property
    def key(self) -> str:
        return "sc_" + self.code.lower()

    @property
    def say(self) -> str:
        """지금 쓰는 문구(호칭 '선생님'). text 는 기존 녹음에 실제로 들어 있는 말."""
        return honorific(self.text)
