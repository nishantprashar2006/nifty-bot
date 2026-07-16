"""
server.py
=========
FastAPI monitoring layer for the Nifty Options Bot.
Reads the bot's SQLite ledger (no MongoDB needed for the dashboard).
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, APIRouter, HTTPException
from pydantic import BaseModel
from starlette.middleware.cors import CORSMiddleware

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

# Local config so the API surfaces the same defaults the bot daemon uses.
import config  # noqa: E402

DB_PATH = os.environ.get("BOT_DB_PATH", str(ROOT_DIR / "data_store" / "nifty_bot.db"))
PAPER_MODE = os.environ.get("PAPER_MODE", "true").lower() == "true"

app = FastAPI(title="Nifty Bot Dashboard")
api = APIRouter(prefix="/api")


# ──────────────────────────────────────────────────── DB helpers
@contextmanager
def _conn():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        yield con
    finally:
        con.close()


def _ensure_tables() -> None:
    """Mirror the bot's schema so the dashboard works even before first bot run."""
    schema = [
        """CREATE TABLE IF NOT EXISTS trades (
            trade_id TEXT PRIMARY KEY, entry_time TEXT NOT NULL, exit_time TEXT,
            direction TEXT NOT NULL, qty INTEGER NOT NULL, entry_price REAL NOT NULL,
            exit_price REAL, pnl REAL, exit_reason TEXT)""",
        """CREATE TABLE IF NOT EXISTS indicators (
            timestamp TEXT NOT NULL, ema9 REAL, ema21 REAL, ema20_15m REAL,
            ema50_15m REAL, rsi REAL, adx REAL, vwap REAL)""",
        """CREATE TABLE IF NOT EXISTS state_transitions (
            timestamp TEXT NOT NULL, old_state TEXT NOT NULL, new_state TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS equity_curve (
            timestamp TEXT PRIMARY KEY, current_equity REAL NOT NULL,
            peak_equity REAL NOT NULL, drawdown_pct REAL NOT NULL,
            effective_lots INTEGER NOT NULL,
            trading_mode TEXT NOT NULL DEFAULT 'paper')""",
        """CREATE TABLE IF NOT EXISTS commands (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
            action TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending', result TEXT)""",
        """CREATE TABLE IF NOT EXISTS bot_state (
            key TEXT PRIMARY KEY, value TEXT NOT NULL, updated TEXT NOT NULL)""",
    ]
    with _conn() as c:
        for s in schema:
            c.execute(s)
        for ddl in (
            "ALTER TABLE equity_curve ADD COLUMN trading_mode TEXT NOT NULL DEFAULT 'paper'",
            "ALTER TABLE trades ADD COLUMN source TEXT NOT NULL DEFAULT 'auto'",
        ):
            try:
                c.execute(ddl)
            except sqlite3.OperationalError:
                pass


_ensure_tables()


# ──────────────────────────────────────────────────── Models
class ControlRequest(BaseModel):
    action: str   # "start" | "stop" | "restart" | "panic"


class ModeRequest(BaseModel):
    paper_mode: bool


class TradingModeRequest(BaseModel):
    mode: str   # "sim" | "live"


class PaperCapitalRequest(BaseModel):
    capital: float


class ManualEntryRequest(BaseModel):
    direction: str   # "CALL" | "PUT"
    # PART 3 — optional engine selector + user-edited lot size + advisory
    # snapshot. The bot records these on the resulting trade row.
    engine: Optional[str] = None        # "indicator" | "smc"
    lots: Optional[int] = None
    confidence: Optional[int] = None
    reasons: Optional[list[str]] = None


class OrderTypeRequest(BaseModel):
    entry_order_type: Optional[str] = None   # "MARKET" | "LIMIT"
    sl_order_type: Optional[str] = None      # "STOPLOSS_MARKET" | "STOPLOSS_LIMIT"


# ──────────────────────────────────────────────────── .env helpers
ENV_FILE = ROOT_DIR / ".env"


def _read_env_value(key: str) -> Optional[str]:
    if not ENV_FILE.exists():
        return None
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _update_env_value(key: str, value: str) -> None:
    """In-place key=value rewrite; appends if missing. Preserves other keys."""
    if not ENV_FILE.exists():
        ENV_FILE.write_text(f"{key}={value}\n")
        return
    lines = ENV_FILE.read_text().splitlines()
    found = False
    for i, ln in enumerate(lines):
        if ln.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(lines) + "\n")


def _read_bot_state(key: str, default: str = "") -> str:
    """v1.15 — read a value from the daemon's bot_state key/value store."""
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT value FROM bot_state WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default
    except Exception:
        return default


def _write_bot_state(key: str, value: str) -> None:
    from datetime import datetime, timezone
    try:
        with _conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO bot_state(key, value, updated) VALUES (?, ?, ?)",
                (key, value, datetime.now(timezone.utc).isoformat()),
            )
    except Exception:
        pass


# ──────────────────────────────────────────────────── Routes
@api.get("/")
def root() -> dict[str, Any]:
    # Re-read env each call so the toggle reflects without backend restart
    mode = _current_trading_mode()
    return {
        "name": "Nifty Options Bot Dashboard",
        "trading_mode": mode,
        "paper_mode": mode == "paper",
    }


def _current_trading_mode() -> str:
    raw = (_read_env_value("TRADING_MODE") or "").strip().lower()
    if raw in {"sim", "live"}:
        return raw
    # Backward-compat fallback
    paper = (_read_env_value("PAPER_MODE") or "true").lower() == "true"
    return "sim" if paper else "live"


def _current_paper_mode() -> bool:
    return _current_trading_mode() == "paper"


def _current_paper_capital() -> float:
    try:
        return float(_read_env_value("PAPER_STARTING_CAPITAL") or 200_000)
    except ValueError:
        return 200_000.0


def _current_entry_order_type() -> str:
    v = (_read_env_value("ENTRY_ORDER_TYPE") or "MARKET").upper()
    return v if v in {"MARKET", "LIMIT"} else "MARKET"


def _current_sl_order_type() -> str:
    v = (_read_env_value("SL_ORDER_TYPE") or "STOPLOSS_MARKET").upper()
    return v if v in {"STOPLOSS_MARKET", "STOPLOSS_LIMIT"} else "STOPLOSS_MARKET"


@api.get("/bot/status")
def bot_status() -> dict[str, Any]:
    # supervisor status
    try:
        out = subprocess.run(
            ["supervisorctl", "status", "nifty_bot"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception as exc:
        out = f"unreachable: {exc}"

    sup_state = "UNKNOWN"
    if "RUNNING" in out:
        sup_state = "RUNNING"
    elif "STOPPED" in out:
        sup_state = "STOPPED"
    elif "FATAL" in out:
        sup_state = "FATAL"
    elif "STARTING" in out:
        sup_state = "STARTING"

    # latest FSM state
    with _conn() as c:
        row = c.execute(
            "SELECT timestamp, old_state, new_state FROM state_transitions "
            "ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        fsm = dict(row) if row else None

        eq_row = c.execute(
            "SELECT timestamp, current_equity, peak_equity, drawdown_pct, effective_lots "
            "FROM equity_curve ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        equity = dict(eq_row) if eq_row else None

        today = datetime.now(timezone.utc).date().isoformat()
        trade_count = c.execute(
            "SELECT COUNT(*) FROM trades WHERE entry_time LIKE ?", (f"{today}%",)
        ).fetchone()[0]
        realized = c.execute(
            "SELECT COALESCE(SUM(pnl),0) FROM trades WHERE entry_time LIKE ?",
            (f"{today}%",),
        ).fetchone()[0]

    # Broker connectivity — independent of supervisor status. The bot writes
    # 'broker_status' (connected | disconnected | error) to bot_state on every
    # WS heartbeat / REST refresh. If missing, infer from the live-tick age.
    quotes = _live_quotes()
    broker_status = _broker_status() or (
        "connected" if quotes.get("updated") else "unknown"
    )

    # Feed staleness — last tick older than FEED_STALE_SECONDS disables Buy
    feed_stale = False
    if quotes.get("updated"):
        try:
            ts = datetime.fromisoformat(quotes["updated"].replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            feed_stale = age > _feed_stale_threshold()
        except Exception:
            feed_stale = True
    else:
        feed_stale = True

    return {
        "supervisor_state": sup_state,
        "trading_mode": _current_trading_mode(),
        "paper_mode": _current_paper_mode(),
        "paper_starting_capital": _current_paper_capital(),
        "entry_order_type": _current_entry_order_type(),
        "sl_order_type": _current_sl_order_type(),
        "fsm_state": fsm["new_state"] if fsm else "IDLE",
        "fsm_last_transition": fsm,
        "equity_snapshot": equity,
        "trades_today": int(trade_count),
        "realized_pnl_today": float(realized or 0.0),
        "live_quotes": quotes,
        "setup_score": _setup_score(),
        "smc_score": _smc_score(),
        "atm_snapshot": _atm_snapshot(),
        "ws_health": _ws_health(),
        "db_path": DB_PATH,
        "server_time_utc": datetime.now(timezone.utc).isoformat(),
        # PART 3 — execution gating + manual-mode policy
        "broker_status": broker_status,
        "feed_stale": feed_stale,
        "feed_stale_threshold_sec": _feed_stale_threshold(),
        "auto_entry_enabled": (
            _read_env_value("AUTO_ENTRY_ENABLED") or "false"
        ).lower() == "true",
        "manual_sl_pct": float(_read_env_value("MANUAL_SL_PCT") or (config.MANUAL_SL_PCT * 100)),
        "manual_tp_pct": float(_read_env_value("MANUAL_TP_PCT") or (config.MANUAL_TP_PCT * 100)),
        "trail_step_pct": float(_read_env_value("TRAIL_STEP_PCT") or (config.TRAIL_STEP_PCT * 100)),
        "smc_max_signal_age_min": int(_read_env_value("SMC_MAX_SIGNAL_AGE_MIN") or config.MAX_SIGNAL_AGE_MINUTES),
        "telegram_enabled": (
            _read_env_value("TELEGRAM_ENABLED") or "false"
        ).lower() == "true",
        "smc_alert_threshold": int(_read_env_value("SMC_ALERT_THRESHOLD") or config.SMC_ALERT_THRESHOLD),
        # v1.15 — auto-trade dashboard state
        "smc_auto_trade_threshold": int(_read_env_value("SMC_AUTO_TRADE_THRESHOLD") or config.SMC_AUTO_TRADE_THRESHOLD),
        "trading_execution_mode": _read_bot_state("auto_trade_enabled", "false").lower() == "true" and "AUTO" or "MANUAL",
        "auto_trade_enabled": _read_bot_state("auto_trade_enabled", "false").lower() == "true",
        "auto_suspended_reason": _read_bot_state("auto_suspended_reason", "") or None,
        "default_lots": int(_read_bot_state("default_lots", "1") or 1),
        # v2.0 — auto risk sizing
        "sizing_mode": _read_bot_state("sizing_mode", "manual"),
        "risk_pct": float(_read_bot_state("risk_pct", "1.0") or 1.0),
        "max_lots": int(_read_bot_state("max_lots", "5") or 5),
        "sim_capital": float(_read_bot_state("sim_capital", "200000") or 200000),
        "broker_capital": _broker_capital_snapshot(),
    }


def _broker_capital_snapshot() -> dict[str, Any]:
    """v2.2 P0 — single authoritative capital number the UI displays.

    In SIM mode this is the persisted sim_capital (user-editable).
    In LIVE mode this is the latest broker RMS snapshot pushed by the
    bot's main loop (`_publish_broker_capital`), falling back to the
    most recent equity_curve.current_equity if the bot hasn't
    written a snapshot yet.
    """
    import json as _json
    mode = _current_trading_mode()
    if mode == "sim":
        try:
            v = float(_read_bot_state("sim_capital", "200000") or 200000)
        except Exception:
            v = 200_000.0
        return {"value": v, "source": "sim", "trading_mode": "sim"}
    # LIVE mode
    raw = _read_bot_state("broker_capital", "")
    if raw:
        try:
            d = _json.loads(raw)
            return {
                "value": float(d.get("value") or 0.0),
                "source": "broker",
                "trading_mode": "live",
                "ts": d.get("ts"),
            }
        except Exception:
            pass
    # Fallback: latest equity_curve row
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT current_equity FROM equity_curve "
                "ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
        if row:
            return {
                "value": float(row["current_equity"]),
                "source": "equity_curve",
                "trading_mode": "live",
            }
    except Exception:
        pass
    return {"value": 0.0, "source": "unknown", "trading_mode": "live"}


def _feed_stale_threshold() -> int:
    try:
        return int(_read_env_value("FEED_STALE_SECONDS") or 10)
    except ValueError:
        return 10


def _broker_status() -> Optional[str]:
    """Latest broker connectivity status posted by the bot, or None."""
    import json
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT value FROM bot_state WHERE key='broker_status'"
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    try:
        return json.loads(row["value"]).get("state")
    except Exception:
        return None


def _live_quotes() -> dict[str, Any]:
    """Latest broker ticks the bot has cached (spot / VIX / option LTP / ts)."""
    import json, time as _time
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT value, updated FROM bot_state WHERE key='live_quotes'"
            ).fetchone()
    except sqlite3.OperationalError:
        return {}
    if not row:
        return {}
    try:
        data = json.loads(row["value"])
    except Exception:
        return {}
    data["updated"] = row["updated"]
    # v1.14 — Phase 7 measurement: age-of-latest-tick so the UI can render
    # "P&L latency" and operators can quantify staleness rather than guess.
    now_ts = _time.time()
    try:
        if data.get("option_ltp_ts"):
            data["option_ltp_age_ms"] = int(max(0.0, (now_ts - float(data["option_ltp_ts"])) * 1000))
    except Exception:
        pass
    try:
        if data.get("spot_ts"):
            data["spot_age_ms"] = int(max(0.0, (now_ts - float(data["spot_ts"])) * 1000))
    except Exception:
        pass
    return data


def _setup_score() -> dict[str, Any]:
    """Weighted Setup Score (Task 1) — Call/Put bias + strength + timestamp."""
    import json
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT value, updated FROM bot_state WHERE key='setup_score'"
            ).fetchone()
    except sqlite3.OperationalError:
        return {}
    if not row:
        return {}
    try:
        d = json.loads(row["value"])
        d["updated"] = row["updated"]
        return d
    except Exception:
        return {}


def _smc_score() -> dict[str, Any]:
    """Independent SMC engine result — direction, confidence, grade, reasons,
    entry/SL/TP. Updated every bot loop tick during 09:20–15:00 IST."""
    import json
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT value, updated FROM bot_state WHERE key='smc_score'"
            ).fetchone()
    except sqlite3.OperationalError:
        return {}
    if not row:
        return {}
    try:
        d = json.loads(row["value"])
        d["updated"] = row["updated"]
        return d
    except Exception:
        return {}


def _atm_snapshot() -> dict[str, Any]:
    """P0-5: currently-resolved Near-OTM CE & PE picks with fresh premiums.
    Published by the bot every ~10s and on every manual-entry click. The
    dashboard reads this to show the exact contract in the confirm modal
    so what you SEE is what will actually be TRADED."""
    import json
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT value, updated FROM bot_state WHERE key='atm_snapshot'"
            ).fetchone()
    except sqlite3.OperationalError:
        return {}
    if not row:
        return {}
    try:
        d = json.loads(row["value"])
        d["updated"] = row["updated"]
        return d
    except Exception:
        return {}


def _ws_health() -> dict[str, Any]:
    """P0-Q1/diagnostics: WebSocket feed integrity — connected flag,
    seconds since last tick, reconnect failure count, currently-subscribed
    token list. Refreshed by the bot every tick."""
    import json
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT value, updated FROM bot_state WHERE key='ws_health'"
            ).fetchone()
    except sqlite3.OperationalError:
        return {}
    if not row:
        return {}
    try:
        d = json.loads(row["value"])
        d["updated"] = row["updated"]
        return d
    except Exception:
        return {}



@api.get("/bot/stats")
def bot_stats() -> dict[str, Any]:
    """All-time aggregate stats from the trades table."""
    with _conn() as c:
        row = c.execute(
            """
            SELECT
              COUNT(*) AS total_trades,
              COALESCE(SUM(pnl), 0) AS total_pnl,
              COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) AS wins,
              COALESCE(SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END), 0) AS losses,
              COALESCE(AVG(pnl), 0) AS avg_pnl,
              COALESCE(MAX(pnl), 0) AS best_trade,
              COALESCE(MIN(pnl), 0) AS worst_trade
            FROM trades
            WHERE pnl IS NOT NULL
            """
        ).fetchone()
        open_row = c.execute(
            "SELECT * FROM trades WHERE exit_time IS NULL ORDER BY entry_time DESC LIMIT 1"
        ).fetchone()
    closed = int(row["total_trades"] or 0)
    wins = int(row["wins"] or 0)
    win_rate = (wins / closed) if closed else 0.0
    # Derive lot count from qty (Nifty options = 65 / lot). Same logic
    # everywhere — keeps the dashboard from hard-coding 65.
    try:
        from config import LOT_SIZE_NIFTY
    except Exception:
        LOT_SIZE_NIFTY = 65
    open_payload = None
    if open_row:
        open_payload = dict(open_row)
        try:
            open_payload["lots"] = int(open_payload.get("qty") or 0) // LOT_SIZE_NIFTY
        except Exception:
            open_payload["lots"] = None
    return {
        "closed_trades": closed,
        "total_pnl": float(row["total_pnl"] or 0.0),
        "wins": wins,
        "losses": int(row["losses"] or 0),
        "win_rate": round(win_rate, 4),
        "avg_pnl": float(row["avg_pnl"] or 0.0),
        "best_trade": float(row["best_trade"] or 0.0),
        "worst_trade": float(row["worst_trade"] or 0.0),
        "lot_size": LOT_SIZE_NIFTY,
        "open_position": open_payload,
    }


@api.post("/bot/order_types")
def set_order_types(req: OrderTypeRequest) -> dict[str, Any]:
    """Update entry / SL order type in .env. Caller restarts bot to apply."""
    changed = {}
    if req.entry_order_type is not None:
        v = req.entry_order_type.upper().strip()
        if v not in {"MARKET", "LIMIT"}:
            raise HTTPException(status_code=400, detail=f"bad entry_order_type: {v}")
        _update_env_value("ENTRY_ORDER_TYPE", v)
        changed["entry_order_type"] = v
    if req.sl_order_type is not None:
        v = req.sl_order_type.upper().strip()
        if v not in {"STOPLOSS_MARKET", "STOPLOSS_LIMIT"}:
            raise HTTPException(status_code=400, detail=f"bad sl_order_type: {v}")
        _update_env_value("SL_ORDER_TYPE", v)
        changed["sl_order_type"] = v
    if not changed:
        raise HTTPException(status_code=400, detail="no fields provided")
    return {"updated": changed, "note": "Restart the bot for the change to take effect."}


@api.get("/bot/signal_diagnostic")
def signal_diagnostic() -> dict[str, Any]:
    """Snapshot of why no signal fired. Updated every bot tick while in IDLE."""
    import json
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT value, updated FROM bot_state WHERE key='signal_diag'"
            ).fetchone()
    except sqlite3.OperationalError:
        return {}
    if not row:
        return {}
    try:
        d = json.loads(row["value"])
        d["updated"] = row["updated"]
        return d
    except Exception:
        return {}


@api.post("/bot/force_close_orphan")
def force_close_orphan() -> dict[str, Any]:
    """Mark any DB-orphaned open trade as exited. Use when the bot was
    restarted while holding a position — the DB row stays `exit_time IS NULL`
    but the in-memory PositionManager forgot the position on boot."""
    with _conn() as c:
        row = c.execute(
            "SELECT trade_id, entry_price, qty FROM trades "
            "WHERE exit_time IS NULL ORDER BY entry_time DESC LIMIT 1"
        ).fetchone()
        if not row:
            return {"closed": False, "note": "no open trade in DB"}
        c.execute(
            "UPDATE trades SET exit_time=?, exit_price=?, pnl=?, exit_reason=? "
            "WHERE trade_id=?",
            (datetime.now(timezone.utc).isoformat(), row["entry_price"], 0.0,
             "MANUAL_FORCE_CLOSE", row["trade_id"]),
        )
    return {"closed": True, "trade_id": row["trade_id"]}


@api.get("/bot/manual_lots")
def manual_lots_default() -> dict[str, Any]:
    """Return the operator's persisted manual lot count.

    Prefers the value the user last saved via `POST /bot/default_lots`
    (persisted in `bot_state.default_lots`). Falls back to the legacy
    drawdown-aware auto-size only when the operator has never set a
    preference — this preserves backward compatibility for a fresh
    install but honours user intent for every established deployment.
    """
    import math
    saved = _read_bot_state("default_lots", "")
    if saved:
        try:
            v = int(saved)
            if v >= 1:
                return {
                    "default_lots": v,
                    "current_equity": _current_paper_capital(),
                    "drawdown_pct": 0.0,
                    "trading_mode": _current_trading_mode(),
                    "source": "user_saved",
                }
        except Exception:
            pass
    capital = _current_paper_capital()
    with _conn() as c:
        eq = c.execute(
            "SELECT current_equity, peak_equity, drawdown_pct, effective_lots "
            "FROM equity_curve WHERE trading_mode=? "
            "ORDER BY timestamp DESC LIMIT 1",
            (_current_trading_mode(),),
        ).fetchone()
    if eq and eq["effective_lots"]:
        lots = int(eq["effective_lots"])
        eff_eq = float(eq["current_equity"])
        dd = float(eq["drawdown_pct"])
    else:
        try:
            from config import CAPITAL_PER_LOT, MIN_LOTS, MAX_LOTS_DYNAMIC
        except Exception:
            CAPITAL_PER_LOT, MIN_LOTS, MAX_LOTS_DYNAMIC = 50_000, 1, 50
        lots = max(MIN_LOTS, min(MAX_LOTS_DYNAMIC, math.floor(capital / CAPITAL_PER_LOT)))
        eff_eq, dd = capital, 0.0
    return {
        "default_lots": lots,
        "current_equity": eff_eq,
        "drawdown_pct": dd,
        "trading_mode": _current_trading_mode(),
        "source": "auto",
    }


@api.post("/bot/reset_state")
def reset_breakers() -> dict[str, Any]:
    """DEPRECATED (v1.10) — retained as a NO-OP for backward compatibility.

    Manual mid-session reset was removed per user request: once the daily
    loss cap or trade cap trips, the bot stays shut down for the rest of
    the day. Counters reset AUTOMATICALLY at the next daily rollover in
    IST via `_daily_rollover_if_needed()` in the bot daemon.
    """
    return {
        "queued": False,
        "cmd_id": None,
        "note": "reset_breakers is disabled; counters auto-reset at next-day rollover in IST",
    }


@api.post("/bot/manual_entry")
def manual_entry(req: ManualEntryRequest) -> dict[str, Any]:
    """Queue a discretionary CALL or PUT entry. The bot picks this up on its
    next loop tick (≤ 0.5 s) and runs it through the same FSM rails as auto
    entries: sizing, premium-spike guard, ATR-based SL/TP, OCO target/stop,
    ≥5 pt trailing, 30-min hold limit, 15:10 IST square-off, and post-exit
    cooldown."""
    direction = req.direction.upper().strip()
    if direction not in {"CALL", "PUT"}:
        raise HTTPException(status_code=400, detail="direction must be CALL or PUT")
    with _conn() as c:
        open_row = c.execute(
            "SELECT trade_id FROM trades WHERE exit_time IS NULL LIMIT 1"
        ).fetchone()
    if open_row:
        raise HTTPException(status_code=409, detail="another position is already open")
    try:
        sup = subprocess.run(
            ["supervisorctl", "status", "nifty_bot"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        sup = ""
    if "RUNNING" not in sup:
        raise HTTPException(status_code=409, detail="bot is not running — start it first")

    import json
    with _conn() as c:
        payload = {
            "direction": direction,
            "engine": (req.engine or "indicator").lower(),
            "lots": req.lots,
            "confidence": req.confidence,
            "reasons": req.reasons or [],
        }
        cur = c.execute(
            "INSERT INTO commands(timestamp, action, payload, status) "
            "VALUES (?, 'manual_entry', ?, 'pending')",
            (datetime.now(timezone.utc).isoformat(), json.dumps(payload)),
        )
        cmd_id = cur.lastrowid
    return {
        "queued": True, "cmd_id": cmd_id,
        "direction": direction, "engine": payload["engine"], "lots": payload["lots"],
    }


@api.get("/bot/commands")
def list_commands(limit: int = 10) -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, timestamp, action, payload, status, result FROM commands "
            "ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        item = dict(r)
        # v1.13 — decode structured rejection payload written by
        # _handle_manual_entry so the UI can surface the broker's
        # real reason (RMS message, insufficient funds figures, etc.)
        result = item.get("result") or ""
        item["broker_status"] = None
        item["broker_reason"] = None
        item["user_message"] = result if item.get("status") in (None, "", "ok", "fail") else result
        for tag in ("PRECHECK_FAILED", "BROKER_REJECTED"):
            prefix = f"{tag}: "
            if isinstance(result, str) and result.startswith(prefix):
                import json as _json
                try:
                    ctx = _json.loads(result[len(prefix):])
                    item["broker_status"] = ctx.get("broker_status")
                    item["broker_reason"] = ctx.get("broker_reason")
                    item["user_message"] = ctx.get("user_message") or result
                    item["rejection"] = {"tag": tag, **ctx}
                except Exception:
                    pass
                break
        out.append(item)
    return out


# ─── v1.15 auto-trade dashboard endpoints ───────────────────────────
class AutoModeRequest(BaseModel):
    enabled: bool


@api.post("/bot/auto_mode")
def set_auto_mode(req: AutoModeRequest) -> dict[str, Any]:
    """Toggle AUTO / MANUAL execution mode. No restart required."""
    _write_bot_state("auto_trade_enabled", "true" if req.enabled else "false")
    # Clear any lingering suspension when the operator explicitly opts in.
    if req.enabled:
        _write_bot_state("auto_suspended_reason", "")
    return {
        "auto_trade_enabled": req.enabled,
        "mode": "AUTO" if req.enabled else "MANUAL",
    }


class DefaultLotsRequest(BaseModel):
    lots: int


@api.post("/bot/default_lots")
def set_default_lots(req: DefaultLotsRequest) -> dict[str, Any]:
    """Persist the default lot size for the next AUTO entry."""
    if req.lots <= 0:
        raise HTTPException(status_code=400, detail="lots must be a positive integer")
    _write_bot_state("default_lots", str(int(req.lots)))
    return {"default_lots": int(req.lots)}


@api.post("/bot/auto_resume")
def auto_resume() -> dict[str, Any]:
    """Clear an AUTO_SUSPENDED reason after the operator has investigated."""
    _write_bot_state("auto_suspended_reason", "")
    return {"auto_suspended_reason": None}


# ─── v2.0 sizing endpoints ──────────────────────────────────────────
class SizingConfigRequest(BaseModel):
    sizing_mode: Optional[str] = None          # "manual" | "auto_risk"
    risk_pct: Optional[float] = None           # 0 < x <= 10
    max_lots: Optional[int] = None             # >= 1
    sim_capital: Optional[float] = None        # > 0
    default_lots: Optional[int] = None         # >= 1 (manual mode)


@api.post("/bot/sizing_config")
def set_sizing_config(req: SizingConfigRequest) -> dict[str, Any]:
    """v2.0 — persist auto-risk position-sizing settings. All fields
    optional; only supplied ones are written. Rejects out-of-range values."""
    if req.sizing_mode is not None:
        if req.sizing_mode not in ("manual", "auto_risk"):
            raise HTTPException(status_code=400, detail="sizing_mode must be 'manual' or 'auto_risk'")
        _write_bot_state("sizing_mode", req.sizing_mode)
    if req.risk_pct is not None:
        if req.risk_pct <= 0 or req.risk_pct > 10:
            raise HTTPException(status_code=400, detail="risk_pct must be >0 and <=10")
        _write_bot_state("risk_pct", str(float(req.risk_pct)))
    if req.max_lots is not None:
        if req.max_lots < 1:
            raise HTTPException(status_code=400, detail="max_lots must be >=1")
        _write_bot_state("max_lots", str(int(req.max_lots)))
    if req.sim_capital is not None:
        if req.sim_capital <= 0:
            raise HTTPException(status_code=400, detail="sim_capital must be >0")
        _write_bot_state("sim_capital", str(float(req.sim_capital)))
    if req.default_lots is not None:
        if req.default_lots < 1:
            raise HTTPException(status_code=400, detail="default_lots must be >=1")
        _write_bot_state("default_lots", str(int(req.default_lots)))
    return get_sizing_config()


@api.get("/bot/sizing_config")
def get_sizing_config() -> dict[str, Any]:
    return {
        "sizing_mode": _read_bot_state("sizing_mode", "manual"),
        "risk_pct": float(_read_bot_state("risk_pct", "1.0") or 1.0),
        "max_lots": int(_read_bot_state("max_lots", "5") or 5),
        "sim_capital": float(_read_bot_state("sim_capital", "200000") or 200000),
        "default_lots": int(_read_bot_state("default_lots", "1") or 1),
    }


@api.post("/bot/panic_exit")
def panic_exit() -> dict[str, Any]:
    """Force-close any open position via the bot's FORCED_EXIT path."""
    import json
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO commands(timestamp, action, payload, status) "
            "VALUES (?, 'panic_exit', ?, 'pending')",
            (datetime.now(timezone.utc).isoformat(), json.dumps({})),
        )
        return {"queued": True, "cmd_id": cur.lastrowid}


@api.post("/bot/refresh_atm")
def refresh_atm() -> dict[str, Any]:
    """P0-Q2: on-demand ATM refresh. Called by the frontend when the
    confirmation modal opens so the modal shows the freshest possible
    strike/expiry/premium — without the daemon needing to hammer REST on
    a background timer. Read the resulting `atm_snapshot` on the next
    `/api/bot/status` poll (usually within ~1s)."""
    import json
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO commands(timestamp, action, payload, status) "
            "VALUES (?, 'refresh_atm', ?, 'pending')",
            (datetime.now(timezone.utc).isoformat(), json.dumps({})),
        )
        return {"queued": True, "cmd_id": cur.lastrowid}


@api.get("/bot/broker_audit")
def broker_audit(limit: int = 50, method: Optional[str] = None) -> dict[str, Any]:
    """v1.14 — Phase Y broker API response audit.

    Read-only diagnostic feed of the last `limit` broker interactions
    (placeOrder / modifyOrder / cancelOrder / order_book / positions).
    Never influences trading behaviour — the daemon records rows via
    a transparent wrapper and this endpoint simply reads them back.
    """
    limit = max(1, min(int(limit or 50), 500))
    sql = (
        "SELECT id, method, request_ts, response_ts, latency_ms, ok, "
        "error_code, error_message, broker_order_id, exchange_order_id, "
        "request_summary, response_summary FROM broker_audit_log"
    )
    params: tuple = ()
    if method:
        sql += " WHERE method = ?"
        params = (method,)
    sql += " ORDER BY id DESC LIMIT ?"
    params = (*params, limit)
    try:
        with _conn() as c:
            rows = c.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return {"entries": [], "count": 0}
    entries = [dict(r) for r in rows]
    for e in entries:
        e["ok"] = bool(e.get("ok"))
    return {"entries": entries, "count": len(entries)}


@api.get("/bot/trade/{trade_id}/timeline")
def trade_timeline(trade_id: str) -> dict[str, Any]:
    """v1.10 — ordered execution-audit timeline for a single trade.

    Read-only. Reads the `execution_events` table populated by the bot
    daemon's `TimelineLogger`. Returns entries in chronological order
    alongside a lightweight snapshot of the current bot health (ws +
    broker connectivity) for the "Execution Health" card at the top of
    the timeline modal.

    Every trade produces its own timeline; if a trade has zero events
    (e.g. very old rows from before v1.10) we still return HTTP 200 with
    an empty `events` list and a `note` explaining why.
    """
    import json
    events: list[dict[str, Any]] = []
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT id, ts, event_type, message, payload "
                "FROM execution_events WHERE trade_id=? ORDER BY id",
                (trade_id,),
            ).fetchall()
        for r in rows:
            item = {"id": r["id"], "ts": r["ts"],
                    "event_type": r["event_type"], "message": r["message"]}
            try:
                item["payload"] = json.loads(r["payload"]) if r["payload"] else {}
            except Exception:
                item["payload"] = {}
            events.append(item)
    except sqlite3.OperationalError:
        # Table doesn't exist yet (fresh DB before daemon booted)
        events = []

    # Also include the trade row itself so the summary card can render
    # without a second round-trip.
    trade: Optional[dict[str, Any]] = None
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT * FROM trades WHERE trade_id=?", (trade_id,),
            ).fetchone()
            if row:
                trade = {k: row[k] for k in row.keys()}
    except sqlite3.OperationalError:
        pass

    return {
        "trade_id": trade_id,
        "trade": trade,
        "events": events,
        "count": len(events),
        "health": {
            "ws": _ws_health(),
            "broker": _broker_status(),
        },
        "note": (
            None if events
            else "No timeline events recorded for this trade (older than v1.10 or logging failed)."
        ),
    }




@api.post("/bot/mode")
def set_mode(req: ModeRequest) -> dict[str, Any]:
    """Legacy 2-mode toggle. Use /bot/trading_mode for the 3-mode endpoint."""
    new_mode = "paper" if req.paper_mode else "live"
    _update_env_value("TRADING_MODE", new_mode)
    _update_env_value("PAPER_MODE", "true" if req.paper_mode else "false")
    return {
        "paper_mode": req.paper_mode,
        "trading_mode": new_mode,
        "note": "Restart the bot for the change to take effect.",
    }


@api.post("/bot/trading_mode")
def set_trading_mode(req: TradingModeRequest) -> dict[str, Any]:
    """Set one of: sim | live. Caller should restart the bot."""
    mode = req.mode.lower().strip()
    if mode not in {"sim", "live"}:
        raise HTTPException(status_code=400, detail=f"unsupported mode: {mode}")
    _update_env_value("TRADING_MODE", mode)
    # Keep legacy flag aligned for any downstream callers
    _update_env_value("PAPER_MODE", "true" if mode == "sim" else "false")
    return {
        "trading_mode": mode,
        "note": "Restart the bot for the change to take effect.",
    }


@api.post("/bot/paper_capital")
def set_paper_capital(req: PaperCapitalRequest) -> dict[str, Any]:
    if req.capital <= 0:
        raise HTTPException(status_code=400, detail="capital must be > 0")
    _update_env_value("PAPER_STARTING_CAPITAL", str(int(req.capital)))
    return {
        "paper_starting_capital": float(req.capital),
        "note": "Restart the bot to apply the new starting capital.",
    }


@api.get("/bot/trades")
def bot_trades(limit: int = 50) -> list[dict[str, Any]]:
    try:
        from config import LOT_SIZE_NIFTY
    except Exception:
        LOT_SIZE_NIFTY = 65
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM trades ORDER BY entry_time DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["lots"] = int(d.get("qty") or 0) // LOT_SIZE_NIFTY
        except Exception:
            d["lots"] = None
        out.append(d)
    return out


@api.get("/bot/equity")
def bot_equity(limit: int = 200) -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM equity_curve ORDER BY timestamp ASC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


@api.get("/bot/transitions")
def bot_transitions(limit: int = 30) -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM state_transitions ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


@api.get("/bot/indicators")
def bot_indicators(limit: int = 30) -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM indicators ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


@api.post("/bot/control")
def bot_control(req: ControlRequest) -> dict[str, Any]:
    """Drive supervisor / panic without rolling our own daemon."""
    action = req.action.lower()
    if action not in {"start", "stop", "restart"}:
        raise HTTPException(status_code=400, detail=f"unsupported action: {action}")
    try:
        proc = subprocess.run(
            ["sudo", "supervisorctl", action, "nifty_bot"],
            capture_output=True, text=True, timeout=20,
        )
        return {
            "action": action,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
            "rc": proc.returncode,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@api.post("/bot/reset_history")
def reset_history(scope: str = "current_mode") -> dict[str, Any]:
    """Wipe equity_curve + trades. Defaults to current mode only.

    scope = 'current_mode' → only rows tagged with the active TRADING_MODE
    scope = 'all'          → nuke all rows (use sparingly)
    """
    mode = _current_trading_mode()
    with _conn() as c:
        if scope == "all":
            c.execute("DELETE FROM equity_curve")
            c.execute("DELETE FROM trades")
            c.execute("DELETE FROM state_transitions")
            c.execute("DELETE FROM indicators")
        else:
            c.execute("DELETE FROM equity_curve WHERE trading_mode = ?", (mode,))
            # Trades aren't mode-tagged; clear all trades for a clean equity reset
            c.execute("DELETE FROM trades")
    return {"reset_scope": scope, "trading_mode": mode}


app.include_router(api)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)
