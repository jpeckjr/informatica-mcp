#!/usr/bin/env bash
# Dev setup for the Informatica MCP project.
#   First run:  creates the venv, installs deps, makes .env from the template,
#               then stops so you can fill in credentials.
#   Re-run:     runs the tests and one dry-run monitor pass.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo ">>> Creating virtualenv..."
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo ">>> Installing dependencies..."
pip install --upgrade pip >/dev/null
pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo ""
  echo ">>> Created .env from the template. Edit it now (IDMC_LOGIN_URL,"
  echo "    IDMC_USERNAME, IDMC_PASSWORD; keep STOP_DRY_RUN=true), then re-run:"
  echo "    ./setup.sh"
  exit 0
fi

echo ">>> Running unit tests (no live tenant needed)..."
python -m unittest test_monitor -v

echo ">>> Running one DRY-RUN monitor pass against IDMC_ORG..."
python monitor.py
echo ">>> Done. Review the output above and ./audit.log"
