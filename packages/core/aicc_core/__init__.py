"""AICC 공통 로직: 전화번호 정규화, 한국어 날짜·의도 해석, 통화 상태기계, 계약 모델.

이 패키지는 서버(aicc_api)와 PC 에이전트(aicc_agent)가 함께 사용하며 외부 I/O를 하지 않는다.
"""

__all__ = ["phone", "dates", "intents", "flow", "contracts", "voicepack"]
