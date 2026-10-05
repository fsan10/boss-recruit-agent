#!/usr/bin/env bash
set -euo pipefail
RECRUIT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$RECRUIT_ROOT"
RECRUIT_BOOTSTRAP="${RECRUIT_BOOTSTRAP_PYTHON:-python3}"
if [[ ! -x .venv/bin/python ]]; then
  "$RECRUIT_BOOTSTRAP" -c 'import sys; assert sys.version_info >= (3,12), "Python 3.12+ required"'
  "$RECRUIT_BOOTSTRAP" -m venv .venv
fi
.venv/bin/python -c 'import sys; assert sys.version_info >= (3,12), "Python 3.12+ required"'
if command -v uv >/dev/null 2>&1; then
  uv pip install --python .venv/bin/python -r requirements.lock
else
  .venv/bin/python -m pip --version >/dev/null 2>&1 || .venv/bin/python -m ensurepip --upgrade
  .venv/bin/python -m pip install -r requirements.lock
fi
printf '%s\n' 'Recruit workbench: http://127.0.0.1:8765 (on this computer; use --port to change it)'
exec .venv/bin/python -m recruit_agent serve "$@"
