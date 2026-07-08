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

## Additions — 2026-02-17 (PART 3 · Execution & Dashboard Integration)
- **Auto-entry disabled by default** via `AUTO_ENTRY_ENABLED=false`. The
  legacy EMA-cross → WAIT_CONFIRMATION → ENTRY pipeline is fully preserved
  for backward compatibility but bypassed; `_step_idle` exits early with a
  diagnostic note. Re-enable with `AUTO_ENTRY_ENABLED=true` in `.env`.
- **Manual-mode SL/TP/Trail percent defaults** (env-configurable):
  `MANUAL_SL_PCT=15`, `MANUAL_TP_PCT=30`, `TRAIL_STEP_PCT=10`. All three
  are re-anchored to the **ACTUAL fill price** in
  `PositionManager.promote_to_open()` and `maybe_trail_stop()`. Auto-mode
  positions keep their ATR-based stops unchanged.
- **Editable, sticky Lot Size** in the UI:
    • `GET /api/bot/manual_lots` returns the drawdown-aware default
    • The lots input pre-fills from the auto value and refreshes every 5 s
    • As soon as the user edits, their value becomes authoritative until
      the trade is submitted (then resets) — premium ticks never overwrite
      a user-typed value
    • Locked once the position is open
- **Engine-tagged trade log** — new `trades` columns: `engine`,
  `confidence`, `reasons` (JSON), `sl_price`, `tp_price`. Idempotent
  schema migration for legacy DBs.
- **Broker connectivity badge** (🟢 Connected / 🔴 Disconnected) backed
  by `bot_state['broker_status']` (heartbeat-updated by the bot on every
  tick; flipped to disconnected on WS lapse).
- **Feed staleness gate** — `/api/bot/status` exposes `feed_stale` +
  `feed_stale_threshold_sec` (default 10s); Buy Call / Buy Put buttons
  are disabled when supervisor != RUNNING OR feed stale OR broker
  disconnected.
- **UI renamed** "Panic Exit" → "Exit Position" (same endpoint).
- **Manual entry confirmation dialog** now shows the selected engine,
  exact lots, and the configured SL/TP/Trail percentages.
- Manual_entry POST accepts `{direction, engine, lots, confidence,
  reasons}`. Engine + advisory snapshot are persisted on the resulting
  trade row.

## Additions — 2026-02-17 (SMC Engine v1.5 · PART 2 spec)
- New `data/swing_finder.py` (Bill Williams fractal swing detector, configurable lookback — default `SWING_WINDOW=5`)
- New `strategy/smc_engine.py` (v1.5) — fully deterministic SMC scorer with
  the user's exact weights (Trend 20 / Structure 15 / BOS-CHoCH 20 / Sweep 15
  / OB 15 / FVG 10 / Premium-Discount 5). PART 2 compliant:
    • HTF Trend derived from 15m **structure** (HH/HL vs LH/LL) — NOT EMA
    • Displacement detector: body > ATR × 1.5 AND close near candle extreme
    • Order Blocks based on confirmed displacement; tracks `mitigated` + `broken`
    • CHoCH = **first** structural reversal only; subsequent breaks become BOS
    • Market Regime classifier (Trending / Sideways / High-Vol / Low-Vol /
      Unclear). Regime only **attenuates** confidence — never suppresses
    • Wilder ATR (period 14)
- New configurable constant `SMC_MAX_SIGNAL_AGE_MIN` (env-driven, default 5).
  Signals auto-expire if not executed within the window; expiry logged with
  age for easy review.
- `main.py` registers a **dedicated 5m spot series** for SMC (3m/15m left
  untouched). `_update_smc_score` runs every loop tick during 09:20–15:00
  IST and persists JSON to `bot_state['smc_score']` including:
  `direction, confidence, grade, reasons, entry, stop_loss, target,
  market_structure, htf_trend, regime, signal_age_sec, signal_max_age_sec,
  bars_5m, bars_15m, timestamp`.
- `server.py` exposes `smc_score` as an **additive** field on
  `GET /api/bot/status` — no breaking changes to existing keys.
- `frontend/src/App.js`:
    • **Engine Selector** (radio: Indicator / SMC, persisted in localStorage)
    • **Twin Advisory cards** side-by-side: Indicator (left) + SMC (right)
    • SMC card shows Direction, Confidence %, Trade Grade (A+/A/B+/B/C/D),
      Entry, Stop Loss, Target, **HTF Trend (15m)**, **Market Structure (5m)**,
      **Regime**, **Signal Age**, Reasons, IST timestamp
    • Buy Call / Buy Put button glow follows the currently selected engine's
      bias only — buttons themselves unchanged
- `tests/test_smc_engine.py` — 17 new passing tests covering determinism,
  primitives (OB via displacement, FVG, structure, regime), HTF trend (now
  structure-based), confidence bounds. Suite total: **40 passing**.
- **Stability**: Indicator Engine (3m/15m, 09:45–14:45 IST, EMA9/21 + macro
  EMA20/50 + RSI/ADX/VWAP/VIX) is entirely untouched. SIM/LIVE modes,
  broker integration, FSM, sizing, and OCO are all preserved.

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


## Session update (2026-02-06 — post-fork verification)
- **Completed the incomplete synthetic SL/TP smoothing edit**: the `_ltp_history`
  deque had been declared in `__init__` but the actual median-smoothing was
  never wired into `_step_position_open`. Fixed by feeding raw `q["ltp"]`
  through a per-token `deque(maxlen=3)` and using the median as the price
  compared against `pos.target_price`, `pos.stop_price`, and passed into
  `positions.maybe_trail_stop(ltp)`. Nothing else changed — same thresholds,
  same exit routing, same order placement, same trailing logic.
- **New regression test** `test_synthetic_ltp_median_smoothing_absorbs_single_spike`
  in `tests/test_fsm.py` verifies:
    • One transient spike below stop / above target is medianed out
    • Two consecutive genuine breaches still trigger the exit
    • Deque behaviour matches the exact formula used in `main.py`
- **Full suite**: 68 passing (was 67; +1 new). No regressions.
- **Live smoke**: backend restarted cleanly, `GET /api/bot/status` returns
  200 with all expected keys (`setup_score`, `smc_score`, `fsm_state`, …).
- **No behavioural changes to**: SMC confidence, SMC Buy Call/Put signals,
  Indicator Engine, Telegram alerts, entry logic, SL/TP percentages, or
  trailing-SL step %.


## Session update (2026-02-06 — SMC warm-up gate removed, v1.6)
- **Removed the global warm-up gate** in `strategy/smc_engine.py::evaluate()`.
  Previously the engine returned `NEUTRAL/0/["warming_up"]` until it had
  ≥15 5 m bars AND ≥11 15 m bars (i.e. 12:00 IST). Now each detector's
  own guard clauses decide when it activates, so real confidence
  starts flowing as soon as any primitive lights up:
    • 10:10 IST — first 5 m swings → BOS/CHoCH/Sweep
    • 10:30 IST — ATR live → Displacement, OBs, regime classifier
    • 12:00–13:00 IST — 15 m HTF trend fills in (+20 weight)
- **Safety guard**: kept a one-line `if not bars_5m` early-return to
  prevent `bars_5m[-1].close` IndexError before 09:20 IST.
- **Informational reasons added** (additive only — never affect the
  numeric score, direction, or grade):
    • `"HTF pending — score cap 80"` while `htf_trend == NEUTRAL`
    • `"ATR pending — OB/Displacement offline"` while ATR unavailable
    • `"Swings pending — structure/BOS/Sweep offline"` while no swings
    • `"awaiting primitives · bars_5m=X bars_15m=Y · swings=Z · atr=…"`
      shown on genuine zero-score ties so the dashboard never renders
      a blank Reasons box.
- **Entry/SL/TP suppressed** to `None` when no valid swing structure
  exists (`rng_hi == rng_lo == 0.0` or `rng_hi <= rng_lo`), instead of
  the old ±50 bps synthetic envelope around spot. Dashboard now shows
  dashes until real impulse levels form.
- **Preserved unchanged**: every SMC weight, SWING_WINDOW=5, ATR_PERIOD=14,
  DISPLACEMENT_ATR_MULT=1.5, RECENT_EVENT_BARS=3, all detector internals,
  all regime multipliers, all confidence-band thresholds, all afternoon
  behaviour, entry logic, SL/TP percentages, trailing SL, Indicator
  Engine, Telegram thresholds/dedup, FSM.
- **Determinism verified**: given the same bars, the engine produces the
  same output. Only new pending-note strings appear when the underlying
  primitives are genuinely offline.
- **Test suite**: 72 passing (was 68; +4 new SMC warm-up tests, +1 date-
  brittle option-selector test fixed for calendar rollover). No regressions.
- **Live smoke**: `GET /api/bot/status` → HTTP 200; SMC path renders the
  expected `outside SMC window` payload (outside 09:20–15:15 IST).
- **User-acknowledged side effect**: users with a low `SMC_ALERT_THRESHOLD`
  may receive Telegram alerts earlier in the morning if confidence
  legitimately crosses the threshold before 12:00 IST.

## Session update (2026-02-06 — P0 execution-correctness pass, v1.7)

### Root cause of the entry-price mismatch
Two independent defects compounded:

1. **Stale contract cache**: `self._ce` / `self._pe` were picked ONCE at startup by `_refresh_atm_contracts()` and never refreshed as spot drifted. Clicks late in the day sent orders for the startup strikes, not the strikes the user was viewing in Angel One.
2. **LIVE MARKET fill_price bug**: `_handle_fill` read `ev.fill_price` (mapped from `msg["price"]` in the WS payload) — which is `0.0` for MARKET orders. `ev.avg_price` (mapped from `msg["averageprice"]`) holds the real fill. Invisible in SIM because `force_order()` synthesised both fields to the same value.

Additionally: `trades` had no `contract_symbol / token / strike / expiry / option_type / lot_size` columns, so the dashboard could not display which contract each entry belonged to. `reset_breakers` did not flush pending commands, allowing a queued manual entry to fire moments after SHUTDOWN was cleared. Daily profit lock at ₹1,500/lot was tripping SHUTDOWN after two winning trades.

### Fixes shipped (all P0)
- **P0-1** `main.py::_handle_manual_entry` unconditionally calls `_refresh_atm_contracts()` before picking the contract. Startup cache is never trusted for manual entries.
- **P0-2** `_place_entry` adds a one-shot `broker.ltp(...)` REST fallback when `_last_option_quote[token]` is empty (freshly-picked strike, WS not yet subscribed). Aborts cleanly if REST also fails — no more ₹100 synthetic fallback.
- **P0-3** `_handle_fill` prefers `ev.avg_price` (falls back to `ev.fill_price` only if `avg_price ≤ 0`). Applies to both entry and exit fills. SIM behaviour bit-identical (both fields already equal). Defensive abort if both are 0.
- **P0-4** Idempotent migration adds `contract_symbol, contract_token, strike, expiry, option_type, lot_size` to `trades`. `PendingEntry` / `OpenPosition` carry them; `insert_trade_entry` persists them; `/api/bot/stats::open_position` and `/api/bot/trades` expose them via `SELECT *`.
- **P0-5** New `atm_snapshot` bot_state key (refreshed every ~10s) exposed in `/api/bot/status`. Confirmation modal now shows the exact strike/expiry/token/premium that will be traded instead of "ATM weekly CALL".
- **P0-6** `_transition(SHUTDOWN)` and `reset_breakers()` both call `db.cancel_pending_commands(...)` — flushes all pending/running command rows so a stale click cannot fire after the state changes.
- **P0-7** `PROFIT_PER_LOT` constant deleted from `config.py`. `PnlGuard` now takes a single `daily_loss_cap` argument; profit branch of `evaluate()` removed. `SizingResult.daily_profit_lock` field removed.
- **P0-8** Loss cap intact (`daily_loss_cap_hit`), `MAX_TRADES_DAILY=4` intact.

### Files modified
- `backend/config.py` — removed `PROFIT_PER_LOT`
- `backend/risk/pnl_guard.py` — single-arg constructor, no profit branch
- `backend/risk/position_sizer.py` — removed `daily_profit_lock` from result
- `backend/strategy/position_manager.py` — added `strike/expiry/option_type/lot_size` to `PendingEntry` + `OpenPosition`, carried through `promote_to_open`
- `backend/database/sqlite_logger.py` — new columns migration, `insert_trade_entry` extended, new `cancel_pending_commands()` method
- `backend/main.py` — pre-entry refresh, REST LTP fallback, `avg_price` preference, `atm_snapshot` publisher, SHUTDOWN + reset command cancellation
- `backend/server.py` — `atm_snapshot` exposed via `/api/bot/status`
- `backend/tests/test_execution_correctness.py` — **NEW**, 14 regression tests
- `backend/tests/test_fsm.py` — PnlGuard tests updated (no profit branch)
- `backend/tests/test_position_sizer.py` — profit-lock assertion removed
- `frontend/src/App.js` — confirmation modal displays resolved contract; open-position card shows contract identity

### Migration summary
Idempotent `ALTER TABLE trades ADD COLUMN …` for six new columns. Runs automatically on daemon start via `SqliteLogger.__init__`. Existing rows stay untouched (NULL in new columns). Verified against `/app/backend/data_store/nifty_bot.db` — all six columns present.

### Test results
- Full suite: **86 passing** (was 72; +14 new). Zero regressions.
- Backend service restart: clean, `/api/bot/status` → HTTP 200 with `atm_snapshot` key present.
- End-to-end trace: BUY PUT and BUY CALL executed against a fake broker; broker payload, DB row, and open-position render all reference the **same** symbol/token/strike/expiry/premium with zero divergence. Reset-breakers and SHUTDOWN both cancel pending commands as designed.

### Untouched (per user's explicit instruction)
Indicator Engine, SMC weights, SMC confidence, HTF trend logic, `RECENT_EVENT_BARS`, regime multipliers, trailing SL, SL%, TP%, Telegram alerts, signal generation.

