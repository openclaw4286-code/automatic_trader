"""Central configuration for the ICT auto-trader.

All tunable parameters live here. No hardcoding in other modules.
Secrets are loaded from environment variables (.env).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Tuple
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


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


# ---------------------------------------------------------------------------
# ICT analysis parameters
# ---------------------------------------------------------------------------
SWING_LOOKBACK = 3               # bars on each side for swing pivot
FVG_MIN_ATR_MULT = 0.1           # FVG must be >= this * ATR to count
OB_LOOKBACK = 50                 # how far back to search for order blocks
STRUCTURE_LOOKBACK = 80          # bars used for BOS / CHoCH detection
LIQUIDITY_TOLERANCE_PCT = 0.0005 # 0.05% — equal-highs/lows tolerance
SWEEP_LOOKBACK = 50              # bars back when searching sweep target
ATR_PERIOD = 14


# ---------------------------------------------------------------------------
# Risk management (ICT fixed-fractional)
# ---------------------------------------------------------------------------
RISK_PER_TRADE = 0.015           # 1.5% of equity per trade
LEVERAGE_MAX = 50
MAX_MARGIN = 0.10                # 10% of equity — final margin cap
MAX_CONCURRENT_POSITIONS = 10
MIN_RR = 2.0                     # minimum reward-to-risk (TP vs SL)


# ---------------------------------------------------------------------------
# Exit management (partial TP, move-to-BE, trailing stop)
# ---------------------------------------------------------------------------
TP1_RR = 1.0                     # partial-TP level in R multiples
TP1_PORTION = 0.5                # fraction closed at TP1
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


# ---------------------------------------------------------------------------
# News & economic calendar — tried in order; first that works wins.
# ---------------------------------------------------------------------------
NEWS_SOURCES = [
    "cryptopanic",
    "rss_coindesk",
    "rss_cointelegraph",
    "forexfactory",
    "investing_economic",
]
NEWS_LOOKBACK_MIN = 60                  # how far back to fetch news
CRYPTOPANIC_TOKEN = os.getenv("CRYPTOPANIC_TOKEN", "")
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")


# ---------------------------------------------------------------------------
# LLM (Claude CLI)
# ---------------------------------------------------------------------------
CLAUDE_CLI = os.getenv("CLAUDE_CLI", "claude")
CLAUDE_MODEL = "opus"
CLAUDE_TIMEOUT_SEC = 120
LLM_PROMPT_MAX_CHARS = 12000


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
