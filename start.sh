#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x backend/.venv/bin/python ]; then
  python3 -m venv backend/.venv
fi
backend/.venv/bin/python -m pip install -r backend/requirements.txt
if [ ! -f frontend/dist/index.html ]; then
  (cd frontend && npm ci && npm run build)
fi
cd backend
exec .venv/bin/python -m uvicorn app.main:app --host "${INNOVATION_HOST:-127.0.0.1}" --port "${INNOVATION_PORT:-8081}"
