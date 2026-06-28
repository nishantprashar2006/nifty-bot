# PRD — Nifty 50 Options Trading Bot (Angel One SmartAPI)

## Original problem statement
Production-grade systematic options-buyer on Nifty (lot size 65) driven by a
single-position-locked FSM. Drawdown-aware sizer, ATR-derived stops/targets,
liquidity gate, PnL guard with daily rupee circuit breaker, 4-table SQLite
ledger, dual-channel websocket with 30s heartbeat watchdog, and a strict
modular layout (config, broker, data, strategy, risk, database, main).

## Architecture / tasks done (2026-02-06)
- `config.py` — full constants set (EMAs, RSI, ADX, ATR, capital, breakers, times, WS, instruments, FSM enums)
- `database/sqlite_logger.py` — 4 indexed tables (trades, indicators, state_transitions, equity_curve), thread-safe singleton
- `broker/smartapi_client.py` — `_LiveSmartApiClient` (TOTP login + session validate + RMS/orders/LTP) + `_PaperSmartApiClient` fallback
- `broker/websocket_manager.py` — dual-channel ticks + order updates, exponential backoff reconnect, 30s heartbeat watchdog
- `data/candle_manager.py` — tick→3m/15m bar resampler with per-token registry, ring buffer, listener hooks
- `data/indicator_engine.py` — native pandas/NumPy EMA, RSI, ADX, ATR, VWAP; `VixTracker`; `IndicatorSnapshot`
- `data/option_selector.py` — daily Scrip Master cache + nearest-weekly ATM CE/PE picker
- `strategy/regime_filter.py` — 15m EMA(20/50) trend gate
- `strategy/signal_generator.py` — 3m EMA(9/21) crossover detector
- `strategy/confirmation_engine.py` — follow-through bar + RSI + VWAP + ADX acceleration
- `strategy/position_manager.py` — single-position lock, OCO bookkeeper, trailing stop ≥5pt, directional cooldown
- `risk/position_sizer.py` — full 7-step sequence (base lots → scale matrix → caps → premium guard → daily breakers → ATR stops)
- `risk/liquidity_gate.py` — spread % / volume / OI checks
- `risk/pnl_guard.py` — realized + unrealized PnL vs locked rupee caps
- `main.py` — central FSM driver, IST window checks, all 7 states wired with breakers + DB logging
- `tests/test_position_sizer.py` + `tests/test_fsm.py` — 25 passing unit tests
- Supervisor service `nifty_bot` registered (`autostart=false`) to allow manual start

## User personas
- Quant / algo trader running intraday Nifty option-buying strategies on Angel One
- Wants paper-mode dry runs first, then a one-flag flip to live trading

## Core requirements (static)
- Single position lock; no double entry
- Entry window 09:45–14:45 IST, VIX in [11, 22]
- 15m macro regime gate → 3m EMA cross → follow-through bar with RSI + VWAP + ADX acceleration
- ATR-based stops (1× SL, 2× TP); ≥5pt trailing
- Drawdown-aware lot scaling (1.0 / 0.75 / 0.50 / floor=1)
- Premium spike guard (≤25% of equity per position)
- Daily rupee breakers (-₹750/lot loss cap, +₹1500/lot profit lock)
- Circuit breakers: 4 trades, 2 consecutive losses, 3 API rejects, 3 WS fails
- 30s heartbeat watchdog → FORCED_EXIT
- 15:10 IST intraday square-off
- 4-table indexed SQLite ledger

## What's been implemented (2026-02-06)
- Complete bot with PAPER & LIVE modes; PAPER is default
- All FSM transitions log to SQLite
- Auto TOTP login via pyotp
- Unit-tested risk + FSM building blocks
- Supervisor-managed daemon entry point

## Additions — 2026-02-17 (SMC Engine + Twin Card)
- New `data/swing_finder.py` (Bill Williams fractal swing detector, lookback=2)
- New `strategy/smc_engine.py` — fully deterministic SMC scorer with the user's
  exact weights (Trend 20 / Structure 15 / BOS-CHoCH 20 / Sweep 15 / OB 15 /
  FVG 10 / Premium-Discount 5). Detects OB, FVG, BOS, CHoCH, Liquidity Sweeps,
  Premium/Discount, EQH/EQL. Pure function — same input → same SMCResult.
- `main.py` registers a dedicated **5m spot series** for SMC (3m/15m are NOT
  touched — the indicator engine stays exactly as before). `_update_smc_score`
  runs every loop tick during the 09:20–15:00 IST SMC window and persists a
  JSON payload into `bot_state['smc_score']`. Indicator engine and its window
  (09:45–14:45 IST, 3m EMA) remain entirely unmodified.
- `server.py` exposes `smc_score` as an **additive** field on
  `GET /api/bot/status` — no breaking changes to existing keys.
- `frontend/src/App.js`:
    • **Engine Selector** (radio: Indicator / SMC, persisted in localStorage)
    • **Twin Advisory cards** side-by-side: Indicator Setup Advisory (left)
      and new SMC Setup Advisory (right) showing direction (BUY CALL / BUY PUT),
      confidence %, Trade Grade (A+/A/B+/B/C/D), reasons, entry, stop loss,
      target, and IST timestamp.
    • Buy Call / Buy Put button glow follows the currently selected engine's
      bias only — buttons themselves unchanged.
- `tests/test_smc_engine.py` — 11 new passing tests covering determinism,
  primitives (OB, FVG, structure), HTF trend, and confidence bounds. Suite
  total: 36 passing.

## Prioritized backlog
- **P1**: Wire actual Angel SmartAPI websocket message format end-to-end against a real session (paper-tested today; live message shape may need micro-adjustments at first run)
- **P1**: Replace order-update WS fallback poller with a real subscription once SDK class name is confirmed in the current `smartapi-python` build
- **P2**: Add a small CLI to dump today's trades / equity curve from the SQLite ledger
- **P2**: Add Slack/Telegram webhook for trade entry/exit notifications
- **P3**: Backtest harness reading historical bars into the same FSM
- **P3**: Optional FastAPI/React monitoring dashboard (PnL, state, trades log) per the alternative scope discussed

## Next tasks
- User to confirm a paper-mode session end-to-end on a market day
- After at least one successful paper session, flip `PAPER_MODE=false` and run a single 1-lot pilot before scaling
