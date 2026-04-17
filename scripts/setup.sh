#!/usr/bin/env bash
# One-shot local environment bootstrap.
#
# Usage:   ./scripts/setup.sh
# Idempotent — safe to re-run.

set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  echo "▶ creating .venv"
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "▶ upgrading pip"
pip install --upgrade pip >/dev/null

echo "▶ installing requirements"
pip install -r requirements.txt

if [ ! -f .env ]; then
  echo "▶ copying .env.example -> .env (edit this file next)"
  cp .env.example .env
fi

echo "▶ checking claude CLI"
if ! command -v claude >/dev/null 2>&1; then
  cat <<'MSG'
!! claude CLI not found on PATH.
   Install with the official installer:
     npm i -g @anthropic-ai/claude-code
   then log in:
     claude login
   (a Max subscription is required to call opus via -p)
MSG
else
  claude --version || true
fi

echo
echo "✅ setup complete."
echo "   1) edit .env (GATE_API_KEY / GATE_API_SECRET, DRY_RUN)"
echo "   2) python scripts/doctor.py"
echo "   3) python -m runner.main"
