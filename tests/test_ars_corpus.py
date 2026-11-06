"""사용자 제공 시나리오 2145건(합성)으로 대화 흐름 회귀 시험 — 기대 안내를 고르는 비율 하한."""

from ars_eval import run


def test_corpus_accuracy():
    r = run()
    assert r["total"] > 5000
    assert r["rate"] >= 0.99, r["wrong"].most_common(10)
