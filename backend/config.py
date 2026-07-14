"""
config.py
=========
Isolated location for all strategy rules, risk variables per lot,
liquidity criteria, time windows, and FSM constants. NO logic lives here.
"""
from __future__ import annotations

import os
from datetime import time
from enum import Enum, auto
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# ────────────────────────────────────────────────────────────────────
# 1. Strategy / Indicator constants
# ────────────────────────────────────────────────────────────────────
EMA_FAST = 9
EMA_SLOW = 21
EMA_MACRO_FAST = 20
EMA_MACRO_SLOW = 50

RSI_PERIOD = 14
RSI_LONG = 55
RSI_SHORT = 45
ADX_MIN = 20
ADX_DELTA_MIN = 0.5  # ADX_current - ADX_previous > 0.5

ATR_PERIOD = 14
ATR_SL_MULT = 1.0
ATR_TP_MULT = 2.0

# ────────────────────────────────────────────────────────────────────
# 2. Capital / sizing
# ────────────────────────────────────────────────────────────────────
CAPITAL_PER_LOT = 50_000
MIN_LOTS = 1
MAX_LOTS_DYNAMIC = 10
LOT_SIZE_NIFTY = 65  # NSE standard lot size for Nifty 50 options

LOSS_PER_LOT = 750        # ₹ per lot daily loss cap
# NOTE: Daily profit LOCK removed by user request (P0-7). The bot now trades
# until (a) MAX_TRADES_DAILY, (b) daily loss cap, or (c) user stops it.
# The old PROFIT_PER_LOT constant is intentionally deleted so any lingering
# reference fails loud rather than silently re-enabling the profit shutdown.
MAX_POSITION_VALUE_PCT = 0.25  # nominal trade value <= 25% of equity

# ────────────────────────────────────────────────────────────────────
# 3. Circuit breakers
# ────────────────────────────────────────────────────────────────────
MAX_TRADES_DAILY = 4
MAX_API_REJECT_EVENTS = 3        # terminal session threshold
MAX_WS_RECONNECT_FAILS = 3       # consecutive WS connection failures

# ────────────────────────────────────────────────────────────────────
# 4. Time windows (IST)
# ────────────────────────────────────────────────────────────────────
ENTRY_WINDOW_START = time(9, 45)
ENTRY_WINDOW_END = time(14, 45)
INTRADAY_SQUARE_OFF = time(15, 10)

# ────────────────────────────────────────────────────────────────────
# 5. Order / position lifecycle
# ────────────────────────────────────────────────────────────────────
ORDER_TIMEOUT_SEC = 20
# v1.14 — Early reconciliation window inside ORDER_PENDING. If the WebSocket
# fill event hasn't arrived by this age, poll broker.order_book() once and
# adopt if COMPLETE. Cuts the observed 20-30s "still ORDER_PENDING" delay
# down to ~5s. Never triggers a cancel by itself; runs at most once per
# pending; falls back cleanly to the 20s ORDER_TIMEOUT_SEC branch.
PENDING_EARLY_RECONCILE_SEC = 5
MAX_HOLD_TIME_MIN = 30
REENTRY_BLOCK_MIN = 15           # directional cooldown on stop-out side
COOLDOWN_AFTER_EXIT_MIN = 10     # post-exit system rest
TRAILING_TRIGGER_STEP = 5.0      # premium points
LIMIT_SLIP_BUFFER_PCT = 0.005    # 0.5% protective slip (only used in LIMIT mode)
FILL_TOLERANCE_PCT = 0.01        # reject LIMIT fills > 1% above expected
MARKET_FILL_TOLERANCE_PCT = 0.03 # MARKET orders tolerate up to 3% slippage

# v1.13 — Pre-flight margin buffer over the raw premium × qty cost.
# Covers slippage + broker fees; purely advisory (broker remains authority).
PREFLIGHT_MARGIN_BUFFER_PCT = 0.05

# ────────────────────────────────────────────────────────────────────
# 6. Liquidity
# ────────────────────────────────────────────────────────────────────
MAX_BID_ASK_SPREAD_PCT = 0.015
MINIMUM_VOLUME = 5_000
MINIMUM_OI = 10_000

# ────────────────────────────────────────────────────────────────────
# 7. India VIX gate
# ────────────────────────────────────────────────────────────────────
VIX_MIN = 11.0
VIX_MAX = 22.0

# ────────────────────────────────────────────────────────────────────
# 8. Heartbeat / connectivity
# ────────────────────────────────────────────────────────────────────
WS_HEARTBEAT_TIMEOUT_SEC = 30    # >30s silence ⇒ FORCED_EXIT
WS_BACKOFF_BASE_SEC = 2
WS_BACKOFF_CAP_SEC = 60

# ────────────────────────────────────────────────────────────────────
# 9. Instruments
# ────────────────────────────────────────────────────────────────────
NIFTY_SPOT_TOKEN = "99926000"
NIFTY_SYMBOL = "NIFTY"
INDIA_VIX_TOKEN = "99926017"
SCRIP_MASTER_URL = (
    "https://margincalculator.angelone.in/"
    "OpenAPI_File/files/OpenAPIScripMaster.json"
)

# ────────────────────────────────────────────────────────────────────
# 10. FSM states
# ────────────────────────────────────────────────────────────────────
class State(Enum):
    IDLE = auto()
    WAIT_CONFIRMATION = auto()
    ORDER_PENDING = auto()
    POSITION_OPEN = auto()
    FORCED_EXIT = auto()
    COOLDOWN = auto()
    SHUTDOWN = auto()


class Direction(Enum):
    LONG = "CALL"      # buy a CE
    SHORT = "PUT"      # buy a PE


class ExitReason(Enum):
    TARGET = "TARGET"
    STOP_LOSS = "STOP_LOSS"                  # initial (non-trailing) SL hit
    TRAILING_STOP = "TRAILING_STOP"          # SL after ≥ 1 trail bump
    TIME_STOP = "TIME_STOP"
    SQUARE_OFF = "SQUARE_OFF"
    HEARTBEAT = "HEARTBEAT"
    MANUAL = "MANUAL"
    REJECTED = "REJECTED"
    STALE_FEED = "STALE_FEED"                # quote stream frozen safety-exit


# Stale-quote circuit breaker: force-exit if we have an open position and
# the option token has not received a WS tick for this many seconds.
# 0 disables the check (backward compatible / dev only).
STALE_QUOTE_EXIT_SEC: int = int(os.environ.get("STALE_QUOTE_EXIT_SEC", "25"))


# ────────────────────────────────────────────────────────────────────
# 11. Environment-driven toggles
# ────────────────────────────────────────────────────────────────────
# Two trading modes:
#   • sim   — REAL Angel websocket data + REAL cash read, SIMULATED order fills
#   • live  — real everything (real orders fire to NSE/NFO)
_raw_mode = os.getenv("TRADING_MODE", "").strip().lower()
if _raw_mode in {"sim", "live"}:
    TRADING_MODE: str = _raw_mode
else:
    # Backward-compat: derive from legacy PAPER_MODE flag; default to sim
    TRADING_MODE = "sim" if os.getenv("PAPER_MODE", "true").lower() == "true" else "live"

# Convenience flags used across the codebase
USE_LIVE_BROKER: bool = True                              # both modes use Angel
USE_LIVE_DATA: bool = True                                # both modes need real ticks
SIMULATE_ORDERS: bool = TRADING_MODE == "sim"             # sim → fake fills
PAPER_MODE: bool = TRADING_MODE == "sim"                  # legacy alias

LOG_LEVEL: str = os.getenv("BOT_LOG_LEVEL", "INFO").upper()
DB_PATH: str = os.getenv("BOT_DB_PATH", "/app/backend/data_store/nifty_bot.db")

# Entry order type — MARKET (default, guaranteed fill on breakouts) or LIMIT
ENTRY_ORDER_TYPE: str = os.getenv("ENTRY_ORDER_TYPE", "MARKET").upper()
if ENTRY_ORDER_TYPE not in {"MARKET", "LIMIT"}:
    ENTRY_ORDER_TYPE = "MARKET"

# SL leg type — STOPLOSS_MARKET (SL-M, default, protects against gap-throughs)
# or STOPLOSS_LIMIT (cheaper in calm markets, but can miss fills on gaps)
SL_ORDER_TYPE: str = os.getenv("SL_ORDER_TYPE", "STOPLOSS_MARKET").upper()
if SL_ORDER_TYPE not in {"STOPLOSS_MARKET", "STOPLOSS_LIMIT"}:
    SL_ORDER_TYPE = "STOPLOSS_MARKET"

ANGEL_API_KEY = os.getenv("ANGEL_API_KEY", "")
ANGEL_CLIENT_ID = os.getenv("ANGEL_CLIENT_ID", "")
ANGEL_PIN = os.getenv("ANGEL_PIN", "")
ANGEL_TOTP_KEY = os.getenv("ANGEL_TOTP_KEY", "")

# ────────────────────────────────────────────────────────────────────
# 12. SMC Engine (independent — does NOT influence indicator engine)
# ────────────────────────────────────────────────────────────────────
SMC_WINDOW_START = time(9, 20)
SMC_WINDOW_END = time(15, 15)
# Auto-expire a generated SMC signal after this many minutes if it hasn't
# been executed. Configurable via env so users can tune without code changes.
try:
    MAX_SIGNAL_AGE_MINUTES: int = int(os.getenv("SMC_MAX_SIGNAL_AGE_MIN", "5"))
except ValueError:
    MAX_SIGNAL_AGE_MINUTES = 5

# ────────────────────────────────────────────────────────────────────
# 13. Manual-entry execution (PART 3 spec)
# ────────────────────────────────────────────────────────────────────
# When false, the auto-entry pipeline (EMA-cross signal generator + WAIT
# CONFIRMATION → ENTRY) is bypassed. The bot only enters when the user
# clicks Buy Call / Buy Put on the dashboard. Setting this to true revives
# the original ATR-based auto path (preserved for backward compatibility).
AUTO_ENTRY_ENABLED: bool = os.getenv("AUTO_ENTRY_ENABLED", "false").lower() == "true"

# Manual SL / TP / trailing — percent of FILLED premium (not theoretical).
def _pct(env_key: str, default: float) -> float:
    try:
        return float(os.getenv(env_key, str(default)))
    except ValueError:
        return default

MANUAL_SL_PCT: float = _pct("MANUAL_SL_PCT", 15.0) / 100.0      # 15 % stop
MANUAL_TP_PCT: float = _pct("MANUAL_TP_PCT", 30.0) / 100.0      # 30 % target
TRAIL_STEP_PCT: float = _pct("TRAIL_STEP_PCT", 10.0) / 100.0    # 10 % trail step

# ────────────────────────────────────────────────────────────────────
# 14. Telegram notifications (advisory only — never touches trading)
# ────────────────────────────────────────────────────────────────────
TELEGRAM_ENABLED: bool = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "").strip()
try:
    SMC_ALERT_THRESHOLD: int = int(os.getenv("SMC_ALERT_THRESHOLD", "40"))
    # v1.15 — Auto trading uses the same signal threshold as advisory alerts.
    # Runtime toggle lives in `bot_state.auto_trade_enabled` (dashboard driven).
    SMC_AUTO_TRADE_THRESHOLD: int = int(os.getenv("SMC_AUTO_TRADE_THRESHOLD", str(SMC_ALERT_THRESHOLD)))
except ValueError:
    SMC_ALERT_THRESHOLD = 40
    SMC_AUTO_TRADE_THRESHOLD = 40
