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


## Session update (2026-02-06 — Post-v1.7 quote-pipeline & regression pass, v1.8)

### What was broken
The v1.7 pass fixed contract-selection correctness but introduced two
downstream regressions:
1. **Quote pipeline freeze**: `_refresh_atm_contracts` now ran every 10 s
   and drifted `_ce`/`_pe` continuously, but the WebSocket subscription
   was frozen at startup — so LTPs stopped flowing for freshly-picked
   strikes, and `live_quotes.option_ltp` bled across contracts.
2. **Duplicate reasons on the dashboard**: warm-up + regime notes were
   appended to both `reasons_call` and `reasons_put`, then concatenated
   on NEUTRAL ties → each note appeared twice.
3. Semantic bug: "HTF pending" note fired at any NEUTRAL trend — even at
   15:00 IST with plenty of data — confusing "warming up" with "genuine
   neutral verdict".

### P0-Q items shipped
- **P0-Q1** WebSocket resubscribe on strike change
  - `WebSocketManager.resubscribe(subs)` + `subscribed_tokens()` +
    `health()` API; holds a live `_sdk_ws` reference so `subscribe(...)`
    can be re-called without a reconnect.
  - `_refresh_atm_contracts` compares old vs new tokens and calls
    `ws.resubscribe(...)` on change (cached-only if socket is down).
  - `_place_entry` seeds `_last_option_quote[contract.token]` AND
    `live_quotes.option_ltp/token/ts` with the pre-entry premium so the
    first dashboard frame after entry is never stale.
  - `_on_tick` tags option ticks with `option_ltp_token`; the frontend
    suppresses Live P&L when the token doesn't match the open position
    (kills cross-contract bleed).
- **P0-Q2** No more periodic REST hammer
  - Removed the 10-second `_refresh_atm_contracts` timer.
  - New `POST /api/bot/refresh_atm` endpoint + `refresh_atm` command
    action; the confirmation modal invokes it on open so the preview
    stays fresh without a background REST hammer.
- **P0-Q3** Notes separated from Reasons
  - `SMCResult.notes: list[str]` — warm-up hints + regime attenuation
    live here. `SMCResult.reasons` stays weight-only.
  - Exposed as `smc_score.notes` in `/api/bot/status`; dashboard renders
    them in a subtle italic "Notes" strip.
  - HTF/ATR/Swings notes now distinguish "warming up (X/Y bars)" from
    "detector ran and returned NEUTRAL" — fixes the misleading label at
    late-day NEUTRAL verdicts.
- **WS health diagnostics**
  - New `bot_state.ws_health` key (published every tick) with
    `connected`, `last_tick_ts`, `seconds_since_last_tick`,
    `reconnect_failures`, `subscribed_tokens`, `subscribed_count`.
  - Exposed at `/api/bot/status.ws_health`.
  - Dashboard header shows a compact `ws OK (N)` / `ws ! (N)` strip
    with tooltip carrying the full detail.
- **Quote freshness in the UI**
  - `live_quotes.option_ltp_ts` timestamp + `option_ltp_token` added.
  - Open-position card shows `LTP ₹X (Ns ago)` in colour that reflects
    freshness (green ≤ 15 s, amber older, red on token mismatch).
  - Live P&L now suppressed when LTP is from a different contract, with
    a visible amber banner explaining why.

### HTF diagnostic answers (code-level)
- No `htf_pending` state flag exists; the note is a pure function of
  `ctx.htf_trend == "NEUTRAL"` recomputed every tick.
- The previous single-label bug conflated warm-up (data missing) with a
  genuine NEUTRAL verdict. Fixed by splitting into two distinct labels.
- HTF `NEUTRAL` at 15:00 with 23+ 15m bars is now correctly labelled
  "HTF NEUTRAL — no clean HH+HL or LH+LL structure" — not "warming up".

### Files modified
- `backend/broker/websocket_manager.py` — resubscribe/subscribed_tokens/
  health API; SDK ws reference + sub_lock; init fields.
- `backend/main.py` — remove periodic ATM timer; WS resubscribe hook in
  refresh; LTP seed on entry; ws_health publisher; option_token stamp on
  live_quotes; refresh_atm command action.
- `backend/strategy/smc_engine.py` — `SMCResult.notes` field;
  reasons/notes split; two-label warm-up disambiguation.
- `backend/server.py` — `_ws_health()` reader; `/api/bot/refresh_atm`
  endpoint; expose ws_health in `/api/bot/status`; cleaned up stray
  duplicate code block at end of file.
- `backend/tests/test_execution_correctness.py` — +8 new tests
- `backend/tests/test_smc_engine.py` — 4 tests updated for notes/reasons
  split, +2 new (dedup, semantic distinction, weight-only reasons).
- `frontend/src/App.js` — WS health strip; LTP freshness & staleness
  colouring; token-mismatch banner; notes list in SMC card; refresh_atm
  invoked on modal open; ws_health hooked into status polling.

### Test results
- **97 passed** (was 86; +11 new for P0-Q1/Q2/Q3 + WS diagnostics + HTF
  semantic disambiguation). Zero regressions.
- Backend restart clean; `/api/bot/status` returns HTTP 200 with the
  new `ws_health` and `smc_score.notes` keys in the payload.

### Untouched
Indicator Engine, SMC weights, SMC confidence weights, HTF weight (+20),
`RECENT_EVENT_BARS`, regime thresholds/multipliers, `SWING_WINDOW`,
trailing SL, SL%, TP%, Telegram alerts, signal generation, entry logic.


## Session update (2026-02-06 — protection telemetry & audit trail, v1.9)

### What this pass shipped
Following the T-8261be7125 execution audit, all 5 quality-of-life
improvements requested by the user landed together, with no algorithm
changes to Indicator/SMC/entry logic.

#### P0 — Stale-quote circuit breaker
- New `config.STALE_QUOTE_EXIT_SEC` (default 25s, env-overridable, 0 disables).
- `_step_position_open` reads the option quote's `ts` field; when the
  position has been held for at least the threshold AND the last tick is
  older than the threshold, fires `FORCED_EXIT` with reason `STALE_FEED`.
- Prevents the "frozen ₹63 LTP" scenario from ever becoming a real loss:
  when the feed dies, the bot flattens instead of drifting to the
  time-stop.
- New `ExitReason.STALE_FEED` enum value.

#### P0 — Protection-state telemetry on every trade row
- Six new columns on `trades` (idempotent migration):
  `initial_sl_price`, `initial_tp_price`, `final_stop_price`,
  `trail_bumps`, `highest_ltp`, `lowest_ltp`, `exit_trigger`.
- `OpenPosition` now carries live counterparts (`initial_stop_price`,
  `initial_target_price`, `trail_bumps`, `highest_ltp_seen`,
  `lowest_ltp_seen`), updated on every tick.
- `_finalize_exit` writes them all through `update_trade_exit(...)`.
- **Result**: future audits like the T-8261be7125 debate can be answered
  in a single SQL query — no arithmetic reconstruction, no ambiguity
  about whether trailing fired.

#### P1 — TRAILING_STOP vs STOP_LOSS exit-reason differentiation
- New `ExitReason.TRAILING_STOP` enum value.
- `_finalize_exit` inspects `pos.trail_bumps` and `pos.stop_price >
  pos.initial_stop_price` — labels the exit `TRAILING_STOP` when trailing
  fired at least once, `STOP_LOSS` otherwise. Preserves the explicit
  `reason` override used by MAX_HOLD/SQUARE_OFF/MANUAL/etc.

#### P1 — Persist trail_anchor across restart
- New `_persist_live_position_state()` writes a snapshot of the position's
  mutable protection state (stop_price, trail_anchor, trail_bumps,
  hi/lo/initial fields) into `bot_state.live_position` on every trail
  bump — cheap, one INSERT OR REPLACE per bump (rare).
- `_recover_orphan_trade` reads the snapshot at boot, guards against
  stale snapshots via `trade_id + contract_token` match, and restores
  the position with the correct anchor. Trailing resumes from where it
  left off; no more "restart resets anchor to entry".

#### P1 — Post-place broker verification
- After `_place_protective_legs` places both legs, `_verify_protection_legs_placed()`
  peeks at `broker.order_book()` and confirms both ids are present with
  a non-terminal status. If missing, `_retry_missing_protection_legs()`
  re-places ONLY the missing leg(s) (never duplicates a leg that's
  already resting). If still missing on the second look, force
  `REJECTED`-labelled forced exit. If `order_book()` itself raises,
  we don't flip to false-negative flatten (fail-open — safer).

### Files modified
- `backend/config.py` — new ExitReason values + STALE_QUOTE_EXIT_SEC
- `backend/database/sqlite_logger.py` — 7 new columns + extended
  `update_trade_exit(...)`
- `backend/strategy/position_manager.py` — OpenPosition telemetry fields +
  trail_bumps increment inside `maybe_trail_stop`
- `backend/main.py` — stale-quote CB, live_position snapshot,
  orphan-recovery restore, protection-leg verification + retry, exit
  reason differentiation, hi/lo tracking on the smoothed LTP
- `backend/tests/test_execution_correctness.py` — +7 new regression tests

### Test results
- **104 passed** (was 97; +7 new). Zero regressions.
- Backend restart clean; `/api/bot/status` → HTTP 200.
- Live DB migration verified: all 7 new columns present in
  `/app/backend/data_store/nifty_bot.db`.

### T-8261be7125 status
The audit's inference was **correct** — trailing SL fired once from
₹71.485 to ₹79.895 after the option touched ≥ ₹92.51. With v1.9 in
place, the same audit for future trades reduces to:
```
SELECT trade_id, initial_sl_price, final_stop_price, trail_bumps,
       highest_ltp, exit_trigger FROM trades WHERE trade_id='...';
```
No arithmetic, no inference.

### Untouched (per your explicit hold)
Indicator Engine, SMC weights, HTF trend logic, RECENT_EVENT_BARS,
regime thresholds/multipliers, SWING_WINDOW, trailing SL step,
SL%/TP%, Telegram alerts, signal generation, entry logic.


## Session update (2026-02-06 — Execution Timeline + Reset removal, v1.10)

### 1. Execution Timeline (observability-only)
- New isolated module `backend/execution_timeline.py` with `TimelineLogger`
  writing to a new `execution_events` table (idempotent DDL).
- `NiftyOptionsBot` now emits events at every meaningful execution point:
  ENTRY_CLICK, ATM_REFRESH, CONTRACT_SELECTED, REST_LTP, ORDER_SUBMIT,
  ORDER_ACK, ENTRY_FILL, SL_PLACED, TP_PLACED, TRAIL_BUMP, EXIT_FILL,
  STALE_FEED, FORCED_EXIT, NOTE.
- Pre-fill events use a session key (S-…); rekeyed to the real trade_id
  the moment `_handle_fill` promotes the pending entry, so the UI sees
  ONE contiguous timeline per trade.
- Safe helper `NiftyOptionsBot._tl()` guards every log call — a missing
  timeline attribute or a writer exception is a silent no-op (trading
  loop never crashes because of an audit-logging failure).
- New endpoint `GET /api/bot/trade/{trade_id}/timeline` returns the
  ordered events + the trade summary row + a live health snapshot (WS
  + broker) for the "Execution Health" card at the top of the modal.
- Frontend: trade rows are now clickable → open a side-modal titled
  "Execution Timeline". Icons per category (🟢 entry · 🛡 protection ·
  📈 trail · ⚠ safety · 🔴 exit). Chronological, expandable payload
  per row.

### 2. Reset Breakers removed (per user request)
- `NiftyOptionsBot.reset_breakers()` is now a documented NO-OP.
- `POST /api/bot/reset_state` returns `{queued: false, note: "…"}` for
  backward compatibility.
- New `_daily_rollover_if_needed()` runs once per tick — when the IST
  calendar date changes, zeroes `_trades_today`, `_consecutive_losses`,
  `_api_reject_count`, and flips SHUTDOWN → IDLE if flat. **Manual
  mid-day reset is no longer possible; counters auto-reset at
  next-day rollover only.**
- Frontend "Reset Breakers" button replaced by an amber notice:
  "Trading halted for the day · Counters auto-reset at next-day
  rollover (IST)."

### 3. Files touched
- **New**: `backend/execution_timeline.py`
- **Backend**: `main.py` (timeline instrumentation, `_tl` helper,
  rollover, reset no-op), `server.py` (timeline endpoint, reset
  endpoint no-op), `strategy/position_manager.py` (no change to logic —
  telemetry from v1.9 already in place).
- **Tests**: `tests/test_execution_correctness.py` (+4 timeline tests).
- **Frontend**: `App.js` (timeline modal, trade-row click handler,
  Reset Breakers → notice).

### 4. Explanation of capital-to-lots (for user reference)
- `risk/position_sizer.py::update_equity_and_size`:
  - Tiered base lots by equity: <50k=1, <100k=2, <200k=3, else min(4, MAX_LOTS_DYNAMIC).
  - Drawdown attenuation: >10 % = ×0.5, >5 % = ×0.75, else ×1.0.
  - `effective_lots = max(1, floor(base × scale))`.
- Broker balance comes from `broker.get_net_available_cash()` in
  `broker/smartapi_client.py` → SmartAPI RMS endpoint (`getRMS`)
  returns `net` = the "Available for New Positions" figure on the
  Angel One app. Called once at bot startup and at each morning
  sizing tick.

### 5. Test results
- **108 passed** (was 104; +4 new for timeline). Zero regressions.
- Backend restart clean; `/api/bot/status` → 200; timeline endpoint → 200;
  reset_state endpoint returns the no-op note as expected.

### 6. Untouched (per your explicit hold)
Indicator Engine, SMC weights, SMC confidence, HTF trend logic,
BOS/CHoCH/OB/FVG/Sweep detection, Premium-Discount, signal generation,
entry/exit rules, SL%, TP%, Trailing SL%, regime thresholds &
multipliers, SWING_WINDOW, RECENT_EVENT_BARS, WebSocket logic, quote
pipeline, broker integration, FSM transitions, risk management,
position sizing, Telegram alerts, existing API behavior.



## 2026-02-06 — P0 Fix: Telegram AttributeError on VPS boot
- **Root cause:** A prior edit accidentally captured the tail of `NiftyOptionsBot.__init__` (including `self.telegram = TelegramNotifier()`, `_exit_reason_hint`, `_spread_history`, `_ltp_history`) inside the body of `_tl_rekey()`. Because `_tl_rekey` was never called during boot, `self.telegram` was never assigned, so `self.telegram.send_startup()` crashed with `AttributeError`.
- **Fix (main.py):**
  - Restored all misplaced attributes to `__init__` tail (lines ~134-154).
  - Reduced `_tl_rekey()` to a minimal 9-line guarded shim identical in shape to `_tl()`.
  - Added `getattr(self, "telegram", None)` belt-and-suspenders guards at both call sites (`start()` startup ping line ~240, SMC notify tick line ~1650).
- **Regression test:** `/app/backend/tests/test_bot_init_attributes.py` — 3 tests locking in that `telegram`, `_exit_reason_hint`, `_spread_history`, `_ltp_history`, `timeline`, `_timeline_session` all exist immediately after `NiftyOptionsBot()` construction.
- **Verification:** Full suite 111/111 passing (108 pre-existing + 3 new). Testing agent iteration_3 confirmed 100% backend pass, no regressions.
- **Untouched (per user directive):** Trading logic, SMC scoring, risk management, execution pipeline, UI.

## 2026-02-06 — P1: HTF Trend Detector Refinement (R1 — EQ tolerance)
- **Problem:** HTF was returning NEUTRAL almost all the time; contributing ~0 confidence and capping SMC score at ~50%.
- **Phase 1 audit outcome:** Two dominant causes — (1) strict `>`/`<` in `detect_structure` kills the verdict on equal endpoints, (2) `SWING_WINDOW=5` on 15m makes swings sparse. R2/R3/R4 alternatives evaluated; user approved R1 only.
- **Change (single file):** `/app/backend/strategy/smc_engine.py::detect_structure` — added `_bps_diff` helper and rewrote decision matrix: equal endpoints on one side (within `EQ_TOLERANCE_BPS=5 bps`) treated as "flat on that side"; the OTHER side must still be strictly directional. CALL requires at least one strict-up side AND no strict-down side (mirror for PUT). Uses the existing SMC-native EQH/EQL tolerance — no new constants, no new primitives.
- **Untouched (per user directive):** `SWING_WINDOW`, BOS, CHoCH, OB, FVG, sweeps, premium/discount, confidence weights, indicator engine, execution, risk, UI. Verified by testing agent.
- **New tests:** `/app/backend/tests/test_htf_structure_r1.py` (16 regression tests). `/app/backend/tests/benchmark_htf_r1.py` (before/after A/B harness across 1200 synthetic 15m sessions).
- **Benchmark results (seed=7, n=200/scenario, testing_agent verified):**
  - Trending scenarios (n=800): false-NEUTRAL 38.0% → 25.0% (**34.2% relative reduction**). Correct-direction 62.0% → 75.0%.
  - Sideways / choppy (n=400): directional flips 27.0% → 21.2% (**did not increase** — actually improved because the tolerance is stricter for genuine ranges where BOTH sides land flat).
- **Full pytest suite:** 127/127 passing.
- **Known limitation carried forward:** Cause #4 (post-BOS consolidation reads NEUTRAL) not addressed by R1 — deferred to R2 (BOS memory into HTF) pending live measurement of R1.


## 2026-02-06 — P0: ORDER_PENDING → IDLE despite live broker fill (Fix A + Fix B)
- **Reported symptom:** Manual BUY at Angel; broker executed order; SmartWebSocket `on_order(status="complete")` not delivered in ≤20s; FSM cancelled in-memory pending and went IDLE while an unprotected live position existed at the broker.
- **Root cause (proven by code lines + 8 matching log fingerprints in `/var/log/supervisor/nifty_bot.err.log`):** `_step_order_pending` (main.py) treated the WebSocket fill event as the sole source of truth. At `ORDER_TIMEOUT_SEC=20s` it unconditionally called `broker.cancel_order` (which raised `SmartApiError` for the already-COMPLETE order, silently swallowed by broad `except`) then cleared pending and transitioned to IDLE. `_recover_orphan_trade` only ran at boot, not during runtime.
- **Fix scope (surgical):** ONLY `_step_order_pending` modified + new private helper `_reconcile_order_pending_timeout(p)` added right after it (+~150 lines). Two new observability event constants added to `execution_timeline.Event`: `ORDER_PENDING_RECONCILE_ORDERBOOK`, `ORDER_PENDING_RECONCILE_POSITION`. Nothing else touched — verified by testing agent.
- **Behaviour:** At the 20s timeout boundary, before firing legacy cancel/IDLE:
  1. Poll `broker.order_book()`. If the pending `order_id` shows `status=complete` with a usable averageprice → synthesize an `OrderEvent(status="complete", ...)` and route through the **unchanged** `_handle_fill(ev)` pipeline (promote_to_open → protective legs → DB insert → POSITION_OPEN → timeline).
  2. If order_book cannot prove completion, poll `broker.positions()`. If an NFO position with matching token/symbol and expected direction has non-zero net qty → same synthetic OrderEvent route through `_handle_fill`.
  3. Only if BOTH sources fail → execute the legacy cancel + IDLE branch (behaviour preserved).
- **Idempotency guarantees:**
  - Early guard: `if self.positions.pending_entry is None or self.positions.has_open_position: return False` — reconciliation is a no-op if WS fill already fired.
  - Existing `_handle_fill` guard: `if pending and ev.order_id == pending.order_id` — second layer of defence against double-promote.
  - Recovery priority: WebSocket wins by firing first (clears pending); order_book wins if WS missed; positions() wins if order_book stale.
- **Untouched (per user directive, verified by testing agent):** SMC engine, indicator engine, HTF, confidence, BOS, CHoCH, FVG, OB, sweeps, premium/discount, risk management, position sizing, telegram, dashboard, timeline UI, database schema, FSM architecture, broker wrapper APIs, order placement logic, `_handle_fill`, `_place_protective_legs`.
- **New tests:** `/app/backend/tests/test_order_pending_reconcile.py` (12 tests covering all 7 mandated scenarios plus 5 robustness cases: order_book raise fallback, both-raise legacy preservation, zero avg-price fall-through, unrelated position no-adopt, back-to-back idempotency).
- **Full suite:** 139/139 passing (127 pre-existing + 12 new). Testing agent iteration_5 → PASS.
- **Explicitly deferred per user:** periodic watchdog, background reconciliation thread, new FSM states.


## 2026-02-06 — P1: Pre-flight margin check + broker rejection surfacing (v1.13)
- **Problem:** Manual BUY was rejected by Angel One (insufficient funds) but the UI still showed "Order Queued". User could not tell whether the order was actually pending. Bot never displayed the broker rejection reason.
- **Fix Part A — Pre-flight margin check (advisory, non-blocking):** In `_place_entry`, before calling `broker.place_order`, compute `required = qty × premium × (1 + PREFLIGHT_MARGIN_BUFFER_PCT=5%)` and compare against `broker.get_net_available_cash()`. If insufficient → return False, populate `self._last_reject_context` with structured payload, emit `Event.PRECHECK_FAILED`, NO broker submission, NO ORDER_PENDING transition. If RMS lookup fails → log and continue to broker (broker remains authority, never block).
- **Fix Part B — Broker rejection handling:** Enhanced `SmartApiError` catch in `_place_entry` to capture the FULL exception message, populate `self._last_reject_context` with `{phase:'broker', broker_status:'rejected', broker_reason:<verbatim>, user_message, symbol, qty, ref_price}`, emit `Event.ORDER_REJECTED` timeline event. Zero PendingEntry / zero trade row / zero protection / zero ORDER_PENDING transition. `_handle_manual_entry` returns `(False, 'PRECHECK_FAILED: <json>' | 'BROKER_REJECTED: <json>')` written into `commands.result`.
- **API:** `GET /api/bot/commands` now parses the structured result and adds `broker_status`, `broker_reason`, `user_message`, `rejection.tag` fields. Success rows carry nulls for these.
- **UI:** `App.js::placeManual` polls `/bot/commands` for up to 6 s after clicking Buy Call/Put. If the daemon's completed command has `broker_status ∈ {'rejected','not_submitted'}`, shows a red `toast.error("Order Rejected"|"Order Not Submitted", user_message + broker_reason)` and refreshes state.
- **New config:** `PREFLIGHT_MARGIN_BUFFER_PCT = 0.05` in config.py.
- **New timeline events:** `Event.PRECHECK_FAILED`, `Event.ORDER_REJECTED`.
- **Untouched (per user directive, verified by testing agent):** SMC engine, indicator engine, HTF, BOS/CHoCH/FVG/OB/sweeps/premium-discount, risk management, position sizer, `_handle_fill`, `_place_protective_legs`, v1.12 order-pending reconciliation, DB schema, timeline schema, WebSocket layer, FSM architecture, broker wrapper APIs, order placement algorithm.
- **New tests:** `/app/backend/tests/test_preflight_and_rejection.py` (9 tests: enough balance normal path; insufficient balance blocks broker call; preflight-pass-broker-rejects; RMS/exchange rejection message preserved verbatim; RMS lookup failure does NOT block; PRECHECK/REJECTED timeline events exactly once; `list_commands` structured decoding; `_handle_manual_entry` returns structured PRECHECK JSON).
- **Full suite:** 148/148 passing (139 pre-existing + 9 new). Testing agent iteration_6 → PASS. Frontend smoke screenshot confirmed dashboard still renders.
- **Explicitly deferred per user:** automatic lot reduction, automatic margin optimization, automatic retries, dashboard redesign, background reconciliation thread, new FSM states.


## 2026-02-06 — P0/P1: Live Execution Hardening (v1.14)
Reported issues fixed: (1) protection SELL orders rejected with "Please set your order price in multiples of 5 paise"; (2) broker rejection reasons not visible in UI; (3) 20-30s ORDER_PENDING → OPEN_POSITION delay; (4) P&L latency observability.

**Phase 2 tick-size (root cause of Issues 1 & 2):** New module-level `_tick_round(price, tick=0.05)` in `main.py`. Replaced 11 `round(price, 2)` sites (entry LIMIT, initial TP/SL, retry TP/SL, trailing SL_MARKET/SL_LIMIT) so every broker-bound price snaps to the 5-paise NFO grid.

**Phase 3 + 5 rejection surfacing:** `_place_protective_legs` rewritten with per-leg try/except. New timeline events emit the FULL broker message: `TP_REJECTED`, `SL_REJECTED`, `TRAIL_REJECTED`. Trailing SL replacement failure now emits `TRAIL_REJECTED` before FORCED_EXIT.

**Phase 6 early reconcile:** new `PENDING_EARLY_RECONCILE_SEC = 5`. `_step_order_pending` fires `_reconcile_order_pending_timeout` at age≥5s (guarded once per pending). Cuts recovery from 20-30s to ~5s. Timeline `BROKER_DELAY` event on early recovery.

**Phase 7 latency measurement:** `/api/bot/status.live_quotes` now includes `option_ltp_age_ms` and `spot_age_ms` for UI-side P&L staleness display. Pure observability, no behaviour change.

**Phase 8 protection health:** post-place verification emits `PROTECTION_HEALTH_OK` when both legs are visible in the order book, or `PROTECTION_HEALTH_FAIL` + FORCED_EXIT when incomplete.

**Phase X (Addition 1) timeout safety attestation:** `_step_order_pending` writes a structured `PENDING_TIMEOUT` timeline event capturing the whole safety chain `{order_id, age_seconds, reconcile_recovered, cancel_requested, cancel_ok, cancel_error, final_state, broker_audit_tail}`. New `_broker_still_has_position(p)` safety belt — refuses IDLE transition while broker still shows a matching NFO net position; FSM stays ORDER_PENDING instead of abandoning a live position.

**Phase Y (Addition 2) broker API audit:** new `/app/backend/broker_audit.py` with `BrokerAudit` class and transparent `AuditedBroker` proxy. Only the 5 audited methods (place_order, modify_order, cancel_order, order_book, positions) are instrumented — everything else passes through unchanged. Records `{method, request_ts, response_ts, latency_ms, ok, error_code, error_message, broker_order_id, exchange_order_id, request_summary, response_summary}` to (a) in-memory ring bounded to 100 and (b) new `broker_audit_log` SQLite table (schema + 2 indexes added). New endpoint `GET /api/bot/broker_audit?limit=50&method=?` for UI + diagnostics.

**Untouched (per user directive):** SMC engine, indicator engine, HTF, confidence, BOS/CHoCH/FVG/OB/sweeps/premium-discount, signal generation, entry rules, exit rules, SL%/TP%/Trailing SL%, risk management, position sizing, dashboard strategy logic, Telegram, existing timeline events (all new events additive).

**Regression tests:** 19 new in `/app/backend/tests/test_execution_hardening_v14.py` covering: tick-round correctness, protection tick-size snapping, TP/SL rejection surfacing, protection health OK/FAIL, early reconcile at 5s, once-per-pending guard, PENDING_TIMEOUT attestation on cancel and on recovery, broker-still-has-position refusal, broker audit transparency, exception recording, ring bounding, persistence sink.

**Full suite:** 167/167 passing (148 pre-existing + 19 new). Testing agent iteration_7 → PASS. No regressions in iterations 3-6.

