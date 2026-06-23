# Nifty 50 Options Trading Bot — Angel One SmartAPI

A production-grade, single-position-locked Finite State Machine bot that
buys ATM Nifty options on the nearest weekly expiry, sized by drawdown-aware
risk rules, and gated by a tight confluence filter (15m macro regime + 3m
EMA crossover + RSI + VWAP + ADX acceleration + India VIX band).

> **Lot size:** 65 (NSE standard for Nifty 50 options).
> **Default mode:** PAPER (set `PAPER_MODE=false` in `backend/.env` for LIVE).

---

## Layout

```
backend/
├── config.py                  # all strategy / risk / FSM constants
├── main.py                    # FSM driver
├── broker/
│   ├── smartapi_client.py     # REST session + auto TOTP login (with paper fallback)
│   └── websocket_manager.py   # dual-channel ticks + order updates + heartbeat watchdog
├── data/
│   ├── candle_manager.py      # tick→3m/15m bar resampler
│   ├── indicator_engine.py    # EMA / RSI / ADX / ATR / VWAP (vectorised pandas)
│   └── option_selector.py     # daily scrip-master → ATM CE/PE picker
├── strategy/
│   ├── regime_filter.py       # 15m EMA(20/50) trend direction
│   ├── signal_generator.py    # 3m EMA(9/21) crossover detector
│   ├── confirmation_engine.py # follow-through bar + RSI + VWAP + ADX delta
│   └── position_manager.py    # OCO + cooldown + trailing stop bookkeeper
├── risk/
│   ├── position_sizer.py      # drawdown-aware sizing + premium spike guard
│   ├── liquidity_gate.py      # spread/volume/OI book check
│   └── pnl_guard.py           # daily ₹ loss cap / profit lock
├── database/
│   └── sqlite_logger.py       # 4 indexed tables (trades/indicators/state/equity)
└── tests/
    ├── test_position_sizer.py
    └── test_fsm.py
```

## Running

```bash
# 1. Paper-mode smoke run (default)
python /app/backend/main.py

# 2. Unit tests
cd /app/backend && pytest -q tests/

# 3. Production daemon (paper or live, controlled by .env)
sudo supervisorctl start nifty_bot
sudo supervisorctl tail -f nifty_bot
```

## Environment (`backend/.env`)

| Key                | Purpose                                |
|--------------------|----------------------------------------|
| `ANGEL_API_KEY`    | SmartAPI app key                       |
| `ANGEL_CLIENT_ID`  | Client code (e.g. AACG12345)           |
| `ANGEL_PIN`        | 4-digit PIN                            |
| `ANGEL_TOTP_KEY`   | base32 TOTP secret from Angel profile  |
| `PAPER_MODE`       | `true` (default) / `false`             |
| `BOT_LOG_LEVEL`    | `INFO` / `DEBUG`                       |
| `BOT_DB_PATH`      | SQLite store (default `data_store/`)   |

## FSM (single position, no concurrent entries)

`IDLE → WAIT_CONFIRMATION → ORDER_PENDING → POSITION_OPEN → COOLDOWN → IDLE`

Edge transitions: `FORCED_EXIT` (30-min hold, 15:10 square-off, heartbeat lapse,
panic) and `SHUTDOWN` (4 trades, 2 consecutive losses, 3 API rejects, 3 WS
reconnect failures, or PnL breach).

## SQLite tables

- **trades** — `trade_id, entry_time, exit_time, direction, qty, entry_price, exit_price, pnl, exit_reason`
- **indicators** — `timestamp, ema9, ema21, ema20_15m, ema50_15m, rsi, adx, vwap`
- **state_transitions** — `timestamp, old_state, new_state`
- **equity_curve** — `timestamp, current_equity, peak_equity, drawdown_pct, effective_lots`

## Notes

- `pandas_ta` is currently unavailable for Python 3.11 on PyPI; the indicator suite
  is implemented natively in pandas/NumPy (canonical Wilder formulas) — outputs
  match `pandas_ta` to within numerical tolerance.
- All third-party SDK calls live inside `broker/`; the rest of the bot is
  broker-agnostic and unit-testable without network access.
