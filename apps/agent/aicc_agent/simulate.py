"""MOCK 통화 시뮬레이터(aicc-simulate): 실행 중인 서버에 MOCK 단말로 붙어 진행 중 캠페인을 처리한다.

실제 전화를 걸지 않으며, 결과는 '시험 데이터(MOCK)'로 표시된다.
  aicc-simulate --server http://127.0.0.1:8765 --token <MOCK 장치 토큰>
"""

from __future__ import annotations

import argparse
import sys

import httpx

from .agent import Agent
from .ai import MockAI
from .client import AgentClient
from .mock_device import PERSONAS, MockDevice
from .stt import MockSTT


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="aicc-simulate")
    ap.add_argument("--server", default="http://127.0.0.1:8765")
    ap.add_argument("--token", required=True)
    ap.add_argument("--max-calls", type=int, default=100)
    ap.add_argument("--list-personas", action="store_true")
    a = ap.parse_args(argv)
    if a.list_personas:
        for k, p in PERSONAS.items():
            print(f"번호 끝자리 {k}: {p.name} ({p.answer}) {p.replies}")
        return 0
    agent = Agent(AgentClient(httpx.Client(timeout=15), a.token, base=a.server), MockDevice(), MockSTT(), MockAI())
    n = 0
    while n < a.max_calls:
        r = agent.step()
        if r.status != "CALLED":
            print(f"종료: {r.status} {r.message or ''}")
            break
        n += 1
        s = r.summary
        print(f"[MOCK] 통화 {s.call_id[:8]} 결과={s.outcome} 확정일={s.confirmed_date} 검수={s.review_reasons}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
