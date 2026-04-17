#!/usr/bin/env bash
# Interactive safe-start protocol for live trading.
#
# This script:
#   1) shows current equity / DRY_RUN / key config before arming
#   2) optionally pins conservative overrides via env vars (risk / cap)
#   3) refuses unless you type the confirmation phrase
#   4) runs the bot with LIVE_CONFIRMED=true so the in-process guard
#      allows real orders
#   5) tails the most important logs so problems are visible immediately
#
# Usage:   ./scripts/go_live.sh
# Stop:    Ctrl+C (graceful shutdown — cancels any in-flight entries
#          and exits). If you need to flatten first, cancel from the
#          Gate.io web UI.

set -euo pipefail

cd "$(dirname "$0")/.."

# shellcheck disable=SC1091
source .venv/bin/activate

SKIP_OVERRIDES=0
for arg in "$@"; do
  case "$arg" in
    --defaults|-d) SKIP_OVERRIDES=1 ;;
    --help|-h)
      echo "Usage: $0 [--defaults]"
      echo "  --defaults   skip the conservative-override prompts and use"
      echo "               config.py values (RISK 1.5%, LEV x50, 10 positions)."
      exit 0
      ;;
  esac
done

echo
echo "━━━━━━━━━━━━━━━━ live-trading safe-start ━━━━━━━━━━━━━━━━"
echo

# --- show current state --------------------------------------------------
python - <<'PY'
import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(dotenv_path=Path.cwd() / ".env")
dry = os.getenv("DRY_RUN", "true").lower()
print(f"  current DRY_RUN  = {dry}")
print(f"  GATE_API_KEY set = {'yes' if os.getenv('GATE_API_KEY') else 'NO — abort'}")
PY

# equity probe
python - <<'PY'
import asyncio, os, sys
sys.path.insert(0, os.getcwd())
from exchange.gateio import GateioFutures

async def _probe():
    ex = GateioFutures()
    try:
        await ex.load()
        eq = await ex.equity_usdt()
        print(f"  equity (USDT)   = {eq:.2f}")
    finally:
        await ex.close()

asyncio.run(_probe())
PY

# --- conservative overrides (skipped with --defaults) --------------------
if [ "$SKIP_OVERRIDES" = "1" ]; then
  echo
  echo "  ▶ --defaults: using config.py values (RISK 1.5%, LEV x50, 10 positions)"
else
  echo
  echo "  ▶ conservative overrides (env vars, in-process only):"
  read -r -p "    RISK_PER_TRADE  (% of equity, default 1.5, recommended first-live 0.5): " risk
  read -r -p "    MAX_CONCURRENT_POSITIONS (default 10, recommended first-live 1):        " cap
  read -r -p "    LEVERAGE_MAX   (default 50,   recommended first-live 10):               " lev

  export_override() {       # VAR name, user input, default
    local var="$1" val="$2" def="$3"
    if [ -n "$val" ]; then
      export "$var=$val"
      echo "    export $var=$val"
    else
      export "$var=$def"
      echo "    export $var=$def (default)"
    fi
  }

  echo "  ▶ applying:"
  export_override RISK_OVERRIDE "${risk:-}" ""
  export_override CAP_OVERRIDE "${cap:-}" ""
  export_override LEV_OVERRIDE "${lev:-}" ""
fi

# turn the numeric overrides into python -c config edits at start-up
# via a tiny monkeypatch file sourced by main.py through env
python - <<'PY' > /tmp/ict_override.py
import os, textwrap
out = ["import config"]
for k, cfg in [("RISK_OVERRIDE","RISK_PER_TRADE"),
               ("CAP_OVERRIDE","MAX_CONCURRENT_POSITIONS"),
               ("LEV_OVERRIDE","LEVERAGE_MAX")]:
    v = os.getenv(k)
    if v:
        cast = "int" if cfg != "RISK_PER_TRADE" else "lambda x: float(x)/100 if float(x)>1 else float(x)"
        out.append(f"config.{cfg} = ({cast})({v!r})")
print(textwrap.dedent("\n".join(out)))
PY

# --- force DRY_RUN=false for this session only ---------------------------
export DRY_RUN=false
export LIVE_CONFIRMED=true

# --- final confirmation --------------------------------------------------
echo
echo "  ✳️  about to run LIVE against Gate.io mainnet with the overrides above."
read -r -p "     type exactly 'I UNDERSTAND' to proceed: " ack
if [ "$ack" != "I UNDERSTAND" ]; then
  echo "  aborted (no confirmation)." >&2
  exit 1
fi

# --- launch + tail -------------------------------------------------------
echo
echo "  ▶ starting main runner.  Ctrl+C to stop gracefully."
echo

# start the bot in background, tail key logs in foreground
python -c '
import runpy, sys
exec(open("/tmp/ict_override.py").read())
runpy.run_module("runner.main", run_name="__main__")
' &
BOT_PID=$!
trap "echo; echo '▶ stopping bot (PID=$BOT_PID)'; kill -INT $BOT_PID 2>/dev/null; wait $BOT_PID 2>/dev/null; exit 0" INT TERM

sleep 2   # give the bot a moment to open log files
touch logs/executor.log logs/position.log logs/manager.log logs/llm_gate.log 2>/dev/null || true
tail -f logs/executor.log logs/position.log logs/manager.log logs/llm_gate.log
