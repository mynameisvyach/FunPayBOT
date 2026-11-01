#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [[ ! -x .venv/bin/python ]]; then
  PYTHON_BIN="${PYTHON_BIN:-python3}"
  "$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3,11), "Нужен Python 3.11+ (рекомендуется 3.12)"'
  "$PYTHON_BIN" -m venv .venv
fi
exec .venv/bin/python main.py --run "$@"
