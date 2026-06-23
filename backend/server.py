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

    return {
        "supervisor_state": sup_state,
        "trading_mode": _current_trading_mode(),
        "paper_mode": _current_paper_mode(),
        "paper_starting_capital": _current_paper_capital(),
        "fsm_state": fsm["new_state"] if fsm else "IDLE",
        "fsm_last_transition": fsm,
        "equity_snapshot": equity,
        "trades_today": int(trade_count),
        "realized_pnl_today": float(realized or 0.0),
        "db_path": DB_PATH,
        "server_time_utc": datetime.now(timezone.utc).isoformat(),
    }


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
    return {
        "closed_trades": closed,
        "total_pnl": float(row["total_pnl"] or 0.0),
        "wins": wins,
        "losses": int(row["losses"] or 0),
        "win_rate": round(win_rate, 4),
        "avg_pnl": float(row["avg_pnl"] or 0.0),
        "best_trade": float(row["best_trade"] or 0.0),
        "worst_trade": float(row["worst_trade"] or 0.0),
        "open_position": dict(open_row) if open_row else None,
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
        cur = c.execute(
            "INSERT INTO commands(timestamp, action, payload, status) "
            "VALUES (?, 'manual_entry', ?, 'pending')",
            (datetime.now(timezone.utc).isoformat(), json.dumps({"direction": direction})),
        )
        cmd_id = cur.lastrowid
    return {"queued": True, "cmd_id": cmd_id, "direction": direction}


@api.get("/bot/commands")
def list_commands(limit: int = 10) -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, timestamp, action, payload, status, result FROM commands "
            "ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


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
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM trades ORDER BY entry_time DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


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
