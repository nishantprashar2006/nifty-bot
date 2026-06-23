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
PROFIT_PER_LOT = 1_500    # ₹ per lot daily profit lock
MAX_POSITION_VALUE_PCT = 0.25  # nominal trade value <= 25% of equity

# ────────────────────────────────────────────────────────────────────
# 3. Circuit breakers
# ────────────────────────────────────────────────────────────────────
MAX_TRADES_DAILY = 4
MAX_CONSECUTIVE_LOSSES = 2
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
MAX_HOLD_TIME_MIN = 30
REENTRY_BLOCK_MIN = 15           # directional cooldown on stop-out side
COOLDOWN_AFTER_EXIT_MIN = 10     # post-exit system rest
TRAILING_TRIGGER_STEP = 5.0      # premium points
LIMIT_SLIP_BUFFER_PCT = 0.005    # 0.5% protective slip on entry
FILL_TOLERANCE_PCT = 0.01        # reject fills > 1% above expected

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
    STOP_LOSS = "STOP_LOSS"
    TIME_STOP = "TIME_STOP"
    SQUARE_OFF = "SQUARE_OFF"
    HEARTBEAT = "HEARTBEAT"
    MANUAL = "MANUAL"
    REJECTED = "REJECTED"


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

ANGEL_API_KEY = os.getenv("ANGEL_API_KEY", "")
ANGEL_CLIENT_ID = os.getenv("ANGEL_CLIENT_ID", "")
ANGEL_PIN = os.getenv("ANGEL_PIN", "")
ANGEL_TOTP_KEY = os.getenv("ANGEL_TOTP_KEY", "")
