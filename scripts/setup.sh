#!/usr/bin/env bash
# 처음 설치: Python 가상환경 + 의존성 + 웹 빌드
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m venv .venv
.venv/bin/pip install --upgrade pip >/dev/null
.venv/bin/pip install -e ".[dev]"
(cd apps/web && npm ci && npm run build)
echo "설치 완료. 실행: .venv/bin/aicc"
