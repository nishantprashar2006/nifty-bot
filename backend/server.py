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
            effective_lots INTEGER NOT NULL)""",
    ]
    with _conn() as c:
        for s in schema:
            c.execute(s)


_ensure_tables()


# ──────────────────────────────────────────────────── Models
class ControlRequest(BaseModel):
    action: str   # "start" | "stop" | "restart" | "panic"


# ──────────────────────────────────────────────────── Routes
@api.get("/")
def root() -> dict[str, Any]:
    return {"name": "Nifty Options Bot Dashboard", "paper_mode": PAPER_MODE}


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
        "paper_mode": PAPER_MODE,
        "fsm_state": fsm["new_state"] if fsm else "IDLE",
        "fsm_last_transition": fsm,
        "equity_snapshot": equity,
        "trades_today": int(trade_count),
        "realized_pnl_today": float(realized or 0.0),
        "db_path": DB_PATH,
        "server_time_utc": datetime.now(timezone.utc).isoformat(),
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


app.include_router(api)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)
