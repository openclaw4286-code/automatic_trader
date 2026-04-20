"""Central configuration for the ICT auto-trader.

All tunable parameters live here. No hardcoding in other modules.
Secrets are loaded from environment variables (.env).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Explicit path so dotenv does not walk the call stack (which breaks
# when config.py is imported from a `python - <<EOF` heredoc).
load_dotenv(dotenv_path=Path(__file__).parent / ".env")


KST = ZoneInfo("Asia/Seoul")


# ---------------------------------------------------------------------------
# Exchange / account
# ---------------------------------------------------------------------------
EXCHANGE_ID = "gateio"
MARKET_TYPE = "swap"           # USDT-perpetual futures
QUOTE_CCY = "USDT"
SETTLE_CCY = "USDT"

GATE_API_KEY = os.getenv("GATE_API_KEY", "")
GATE_API_SECRET = os.getenv("GATE_API_SECRET", "")

DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------
UNIVERSE_SIZE = 30                       # top N by CoinGecko market cap
UNIVERSE_REFRESH_MIN = 60                # refresh cadence (minutes)
COINGECKO_URL = "https://api.coingecko.com/api/v3/coins/markets"
EXCLUDE_SYMBOLS = {"USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE"}  # stables


# ---------------------------------------------------------------------------
# Timeframes (ICT top-down)
# ---------------------------------------------------------------------------
HTF_TIMEFRAME = "1h"
MTF_TIMEFRAME = "15m"
LTF_TIMEFRAME = "3m"

HTF_LOOKBACK = 300                        # bars for HTF analysis
MTF_LOOKBACK = 300
LTF_LOOKBACK = 300


# ---------------------------------------------------------------------------
# Sessions (KST) — algorithm only evaluates inside these windows
# Format: (start "HH:MM", end "HH:MM").  Wraps past midnight if end < start.
# ---------------------------------------------------------------------------
SESSIONS_KST: List[Tuple[str, str]] = [
    ("09:00", "12:00"),
    ("13:00", "15:00"),
    ("16:00", "20:00"),
    ("21:00", "00:30"),
    ("22:30", "01:00"),
    ("02:00", "05:00"),
]

# Weekdays (Python Monday=0 ... Sunday=6) on which the scanner may run.
# Crypto itself is 24/7 but Asia / London / NY session liquidity patterns
# only exist Mon-Fri; weekend books are thin and often manipulative.
TRADE_WEEKDAYS_KST: set[int] = {0, 1, 2, 3, 4}

# Block new entries this many minutes before the weekly last-session end,
# and flatten all orders + positions at that end so we never hold over the
# weekend.
WEEKEND_FREEZE_LEAD_MIN = 60


# ---------------------------------------------------------------------------
# ICT analysis parameters
# ---------------------------------------------------------------------------
SWING_LOOKBACK = 3               # bars on each side for swing pivot
FVG_MIN_ATR_MULT = 0.05          # FVG must be >= this * ATR to count
OB_LOOKBACK = 50                 # how far back to search for order blocks
STRUCTURE_LOOKBACK = 80          # bars used for BOS / CHoCH detection
LIQUIDITY_TOLERANCE_PCT = 0.001  # equal-highs/lows tolerance for sweep
SWEEP_LOOKBACK = 50              # bars back when searching sweep target
ATR_PERIOD = 14

# How far back we accept the most recent MTF structure event (BOS/CHoCH)
# when validating alignment with HTF bias. Larger = more frequent signals.
MTF_EVENT_LOOKBACK_BARS = 30

# HTF sweep is treated as a *bonus* rather than a hard requirement. Set
# HTF_SWEEP_REQUIRED=True to restore the old strict behaviour.
HTF_SWEEP_REQUIRED = False

# ---------------------------------------------------------------------------
# Quality filters (research-backed)
# ---------------------------------------------------------------------------
# ATR-padded SL: SL = structural ± SL_ATR_PAD * ATR(MTF). Pads beyond
# stop-hunt noise. Engle (1982) ARCH; Bollerslev (1986) GARCH literature
# on volatility-scaled risk control.
SL_ATR_PAD = 0.3

# Volume confirmation. Karpoff (1987) "The Relation Between Price Changes
# and Trading Volume": moves on above-average volume have ~1.5-2x stronger
# follow-through. Trigger candles below the threshold are rejected.
VOL_SMA_BARS = 20
VOL_MULT_TRIGGER = 1.3       # LTF trigger candle volume / SMA20 must be >=
VOL_MULT_SWEEP = 1.5         # liquidity-sweep candle volume / SMA20 must be >=
SWEEP_WICK_RATIO = 0.6       # the rejecting wick must cover >= 60% of range

# HTF momentum filter (Moskowitz, Ooi, Pedersen 2012 — time-series momentum).
# Reject signals where HTF EMA slope disagrees with the structural bias.
HTF_MOMENTUM_FILTER = True
HTF_EMA_PERIOD = 50
HTF_EMA_SLOPE_BARS = 5


# ---------------------------------------------------------------------------
# Risk management (ICT fixed-fractional)
# ---------------------------------------------------------------------------
RISK_PER_TRADE = 0.025           # 2.5% of equity per trade
LEVERAGE_MAX = 50
MAX_MARGIN = 0.10                # 10% of equity — final margin cap
MAX_CONCURRENT_POSITIONS = 10
MIN_RR = 1.5                     # minimum reward-to-risk (TP vs SL)
MIN_SL_PCT = 0.004               # reject setups whose SL is tighter than this
                                 # (0.4%) — fees dominate the risk budget on
                                 # ultra-tight stops


# ---------------------------------------------------------------------------
# Exit management (multi-tier TP, BE, trailing stop)
# ---------------------------------------------------------------------------
# Multi-tier TP per van Tharp — splits exit into TP1 (1R), TP2 (2R), runner.
# Empirical: typical day-trading systems gain 10-15% expectancy vs single TP.
# Sum of TP1_PORTION + TP2_PORTION + runner = 1.0  (runner is implicit).
TP1_RR = 1.0
TP1_PORTION = 0.4
TP2_RR = 2.0
TP2_PORTION = 0.3
BREAKEVEN_AFTER_TP1 = True       # move SL to entry once TP1 fills
TRAIL_ENABLED = True
TRAIL_ACTIVATION_RR = 1.5        # trail activates after price is this far in R
TRAIL_DISTANCE_R = 0.8           # trailing SL sits this many R behind the high

# Protection guard — if SL or TP cannot be attached to a live position
# after this many tick retries, we emergency-close at market.
PROTECTION_MAX_RETRY = 2


# ---------------------------------------------------------------------------
# Execution cadence
# ---------------------------------------------------------------------------
SCAN_INTERVAL_SEC = 60                  # ICT scan cadence
POSITION_POLL_SEC = 15                  # live position monitor cadence
LLM_GATE_INTERVAL_SEC = 600             # LLM re-evaluates every 10 min
ORDER_TTL_SEC = 600                     # limit order expiry (matches LLM window)

# After we place an entry on (symbol, direction), block re-entry on the
# same (symbol, direction) for this many minutes. Stops the bot from
# re-firing on the same persistent ICT setup. Opposite direction is
# still allowed (valid reversal signals pass through).
SAME_DIRECTION_COOLDOWN_MIN = 60

# At start-up, cancel every open order and flatten every open position
# so the bot never inherits unmanaged state from a previous (possibly
# buggy) run. Set CLEAN_START=false if you want to keep existing state.
CLEAN_START = os.getenv("CLEAN_START", "true").lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# News & economic calendar — tried in order; first that works wins.
# ---------------------------------------------------------------------------
NEWS_SOURCES = [
    "cryptopanic",
    "rss_google_news",
    "rss_cointelegraph",
    "rss_bitcoin_magazine",
    "rss_decrypt",
    "rss_theblock",
    "rss_coindesk",
    "rss_reddit_crypto",
    "forexfactory",
    "investing_economic",
]
NEWS_LOOKBACK_MIN = 120                 # how far back to fetch news (minutes)
# News-age buckets used by the LLM prompt. Markets are roughly
# semi-strong-form efficient: older headlines have already moved into
# the chart and should not, on their own, justify a directional /
# WAIT verdict. The thresholds below were chosen for crypto (24/7
# retail-driven absorption faster than equities — Tetlock 2007 style
# sentiment decay).
NEWS_BREAKING_MIN = 15      # ≤15m: not yet priced in — can solo-trigger
NEWS_RECENT_MIN = 60        # 15-60m: partially absorbed — context only
                            # >60m: stale — background regime only
CRYPTOPANIC_TOKEN = os.getenv("CRYPTOPANIC_TOKEN", "")
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")


# ---------------------------------------------------------------------------
# LLM (Claude CLI)
# ---------------------------------------------------------------------------
CLAUDE_CLI = os.getenv("CLAUDE_CLI", "claude")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "opus")
CLAUDE_TIMEOUT_SEC = 120
LLM_PROMPT_MAX_CHARS = 12000

# Override the gate behaviour without touching the CLI:
#   claude        — run claude -p --model $CLAUDE_MODEL (default)
#   always_pass   — skip CLI, always allow trades (unblocks signals during
#                   CLI issues — USE CAREFULLY, no news filtering)
#   always_wait   — skip CLI, always block trades (kill switch)
LLM_GATE_MODE = os.getenv("LLM_GATE_MODE", "claude").lower()

# What to do if the CLI raises (quota exhausted, network, etc.):
#   wait  — default to WAIT (safe, blocks trades)
#   pass  — default to PASS (keeps trading when LLM is down)
LLM_FAIL_MODE = os.getenv("LLM_FAIL_MODE", "wait").lower()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_ROTATE_MB = 20
LOG_BACKUPS = 10

# Single rolling files — overwritten every LLM cycle, no history kept.
LLM_PROMPT_FILE = os.path.join(LOG_DIR, "last_llm_prompt.txt")
LLM_RESPONSE_FILE = os.path.join(LOG_DIR, "last_llm_response.txt")
LLM_VERDICT_FILE = os.path.join(LOG_DIR, "last_llm_verdict.json")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RuntimeInfo:
    dry_run: bool = DRY_RUN
    exchange: str = EXCHANGE_ID
    quote: str = QUOTE_CCY
    htf: str = HTF_TIMEFRAME
    mtf: str = MTF_TIMEFRAME
    ltf: str = LTF_TIMEFRAME
    sessions_kst: Tuple[Tuple[str, str], ...] = field(
        default_factory=lambda: tuple(SESSIONS_KST)
    )


RUNTIME = RuntimeInfo()
