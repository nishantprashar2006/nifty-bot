import { useEffect, useState, useCallback } from "react";
import axios from "axios";
import { motion } from "framer-motion";
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid,
} from "recharts";
import {
  ActivityIcon, PowerIcon, PauseIcon, RotateCwIcon, ShieldAlertIcon,
  TrendingUpIcon, TrendingDownIcon, CircleDotIcon, DatabaseIcon,
  TargetIcon, WalletIcon, PercentIcon, BriefcaseIcon,
  Trash2Icon, ArrowUpRightIcon, ArrowDownRightIcon, XOctagonIcon,
} from "lucide-react";
import { Card } from "./components/ui/card";
import { Button } from "./components/ui/button";
import { Badge } from "./components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "./components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "./components/ui/tabs";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "./components/ui/alert-dialog";
import { Toaster, toast } from "sonner";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

const STATE_COLORS = {
  IDLE: "bg-slate-700/50 text-slate-200 border-slate-600",
  WAIT_CONFIRMATION: "bg-amber-900/40 text-amber-200 border-amber-700",
  ORDER_PENDING: "bg-blue-900/40 text-blue-200 border-blue-700",
  POSITION_OPEN: "bg-emerald-900/40 text-emerald-200 border-emerald-700",
  FORCED_EXIT: "bg-red-900/50 text-red-200 border-red-700",
  COOLDOWN: "bg-indigo-900/40 text-indigo-200 border-indigo-700",
  SHUTDOWN: "bg-zinc-900/60 text-zinc-300 border-zinc-700",
};

const SUP_COLORS = {
  RUNNING: "text-emerald-300",
  STOPPED: "text-zinc-400",
  STARTING: "text-amber-300",
  FATAL: "text-red-400",
  UNKNOWN: "text-zinc-500",
};

// All dates/times in this app are displayed in IST regardless of the
// viewer's timezone (VPN, browser locale). We force `timeZone: Asia/Kolkata`.
const IST_TZ = "Asia/Kolkata";

function fmtINR(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const sign = n < 0 ? "-" : "";
  const v = Math.abs(n).toLocaleString("en-IN", { maximumFractionDigits: 2 });
  return `${sign}₹${v}`;
}

function fmtTime(iso) {
  if (!iso) return "—";
  try {
    return new Intl.DateTimeFormat("en-IN", {
      timeZone: IST_TZ, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    }).format(new Date(iso));
  } catch {
    return iso;
  }
}

function fmtDateTime(iso) {
  if (!iso) return "—";
  try {
    return new Intl.DateTimeFormat("en-IN", {
      timeZone: IST_TZ, year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    }).format(new Date(iso)) + " IST";
  } catch {
    return iso;
  }
}

function fmtDateShort(iso) {
  if (!iso) return "—";
  try {
    return new Intl.DateTimeFormat("en-IN", {
      timeZone: IST_TZ, day: "2-digit", month: "short",
    }).format(new Date(iso));
  } catch {
    return iso;
  }
}

function StatCard({ label, value, sub, icon: Icon, tone = "default" }) {
  const toneCls =
    tone === "pos" ? "text-emerald-300"
    : tone === "neg" ? "text-red-300"
    : tone === "warn" ? "text-amber-300"
    : "text-zinc-100";
  return (
    <Card className="border-zinc-800 bg-zinc-950/70 backdrop-blur p-5 rounded-none">
      <div className="flex items-start justify-between">
        <div>
          <div className="text-[10px] uppercase tracking-[0.18em] text-zinc-500 font-mono">{label}</div>
          <div className={`mt-2 text-3xl font-mono font-semibold ${toneCls}`}>{value}</div>
          {sub && <div className="mt-1 text-xs text-zinc-500 font-mono">{sub}</div>}
        </div>
        {Icon && <Icon className="h-5 w-5 text-zinc-600" />}
      </div>
    </Card>
  );
}

function App() {
  const [status, setStatus] = useState(null);
  const [stats, setStats] = useState(null);
  const [trades, setTrades] = useState([]);
  const [equity, setEquity] = useState([]);
  const [transitions, setTransitions] = useState([]);
  const [diag, setDiag] = useState(null);
  // v2.4 — trigger analytics + daily loss protection
  const [triggerStats, setTriggerStats] = useState(null);
  const [dailyLoss, setDailyLoss] = useState(null);
  const [busy, setBusy] = useState(false);
  const [lastUpdate, setLastUpdate] = useState(null);

  // Live-toggle confirmation dialog
  const [confirmLive, setConfirmLive] = useState(false);
  const [pendingMode, setPendingMode] = useState(null);

  // Editable paper capital
  const [editingCap, setEditingCap] = useState(false);
  const [capInput, setCapInput] = useState("");

  // v2.2 P0 — controlled SIM Capital input. Local edits during typing
  // don't clobber the persisted value on the next status poll; blur
  // pushes to backend and syncs back.
  const [simCapInput, setSimCapInput] = useState("");
  const [simCapFocused, setSimCapFocused] = useState(false);

  // Reset confirmation
  const [confirmReset, setConfirmReset] = useState(false);

  // Manual entry confirmation
  const [confirmManual, setConfirmManual] = useState(null);  // null | "CALL" | "PUT"
  // v1.10 — timeline modal state
  const [timelineTrade, setTimelineTrade] = useState(null);
  const [timelineData, setTimelineData] = useState(null);
  useEffect(() => {
    if (!timelineTrade) { setTimelineData(null); return; }
    let cancelled = false;
    (async () => {
      try {
        const { data } = await axios.get(`${API}/bot/trade/${timelineTrade.trade_id}/timeline`);
        if (!cancelled) setTimelineData(data);
      } catch {
        if (!cancelled) setTimelineData({ events: [], count: 0, note: "Failed to load timeline" });
      }
    })();
    return () => { cancelled = true; };
  }, [timelineTrade]);

  // Engine selector: 'indicator' | 'smc' — drives which advisory's bias
  // lights up the Buy Call / Buy Put buttons. Persists across reloads.
  const [engine, setEngine] = useState(() =>
    (typeof window !== "undefined" && window.localStorage?.getItem("selectedEngine")) || "indicator"
  );
  useEffect(() => {
    try { window.localStorage?.setItem("selectedEngine", engine); } catch (_) { /* ignore */ }
  }, [engine]);

  // v2.3 — lots are auto-computed from capital (see execution-lots on
  // the sizing card). No manual override — Fixed Position Sizing is
  // the single source of truth. Slabs mirror
  // backend/main.py::calculate_execution_lots.
  const executionLots = (() => {
    const c = status?.broker_capital?.value || 0;
    if (c <= 0) return 1;
    if (c <= 50000) return 1;
    if (c <= 100000) return 2;
    if (c <= 150000) return 3;
    if (c <= 200000) return 4;
    return 5;
  })();

  const fetchAll = useCallback(async () => {
    try {
      const [s, st, t, e, tr, dg, ts, dls] = await Promise.all([
        axios.get(`${API}/bot/status`),
        axios.get(`${API}/bot/stats`),
        axios.get(`${API}/bot/trades?limit=50`),
        axios.get(`${API}/bot/equity?limit=200`),
        axios.get(`${API}/bot/transitions?limit=30`),
        axios.get(`${API}/bot/signal_diagnostic`),
        axios.get(`${API}/bot/trigger_stats?scope=today`),
        axios.get(`${API}/bot/daily_loss_status`),
      ]);
      setStatus(s.data);
      setStats(st.data);
      setTrades(t.data);
      setEquity(e.data);
      setTransitions(tr.data);
      setDiag(dg.data);
      setTriggerStats(ts.data);
      setDailyLoss(dls.data);
      setLastUpdate(new Date());
    } catch (err) {
      console.error("dashboard fetch failed", err);
    }
  }, []);

  useEffect(() => {
    fetchAll();
    const id = setInterval(fetchAll, 3000);
    return () => clearInterval(id);
  }, [fetchAll]);

  // v2.2 P0 — keep the SIM Capital input in sync with the persisted
  // value unless the user is actively editing it (focus guard).
  useEffect(() => {
    if (simCapFocused) return;
    if (status?.sim_capital != null) {
      setSimCapInput(String(Math.round(status.sim_capital)));
    }
  }, [status?.sim_capital, simCapFocused]);

  const control = async (action) => {
    if (busy) return;
    setBusy(true);
    try {
      const { data } = await axios.post(`${API}/bot/control`, { action });
      toast.success(`${action.toUpperCase()} → rc=${data.rc}`, { description: data.stdout || data.stderr || "" });
      await fetchAll();
    } catch (err) {
      toast.error(`Control failed: ${err?.response?.data?.detail || err.message}`);
    } finally {
      setBusy(false);
    }
  };

  const setMode = async (paperMode) => {
    if (busy) return;
    setBusy(true);
    try {
      await axios.post(`${API}/bot/mode`, { paper_mode: paperMode });
      toast.success(paperMode ? "Switched to PAPER mode" : "Switched to LIVE mode", {
        description: "Restarting bot to apply change…",
      });
      // Apply by restarting the bot (autostart=false so safe even if it wasn't running)
      try { await axios.post(`${API}/bot/control`, { action: "restart" }); } catch (_) { /* ignore */ }
      await fetchAll();
    } catch (err) {
      toast.error(`Mode change failed: ${err?.response?.data?.detail || err.message}`);
    } finally {
      setBusy(false);
      setConfirmLive(false);
    }
  };

  const applyTradingMode = async (mode) => {
    if (busy) return;
    setBusy(true);
    try {
      await axios.post(`${API}/bot/trading_mode`, { mode });
      toast.success(`Switched to ${mode.toUpperCase()} mode`, {
        description: "Restarting bot to apply change…",
      });
      try { await axios.post(`${API}/bot/control`, { action: "restart" }); } catch (_) { /* ignore */ }
      await fetchAll();
    } catch (err) {
      toast.error(`Mode change failed: ${err?.response?.data?.detail || err.message}`);
    } finally {
      setBusy(false);
      setConfirmLive(false);
      setPendingMode(null);
    }
  };

  const requestModeChange = (mode) => {
    if (mode === "live") {
      setPendingMode("live");
      setConfirmLive(true);
    } else {
      applyTradingMode(mode);
    }
  };

  const saveCapital = null; void saveCapital;  // removed: no PAPER mode

  const resetHistory = async () => {
    try {
      await axios.post(`${API}/bot/reset_history?scope=current_mode`);
      toast.success(`History wiped for ${status?.trading_mode?.toUpperCase()} mode`);
      setConfirmReset(false);
      await fetchAll();
    } catch (err) {
      toast.error(`Reset failed: ${err?.response?.data?.detail || err.message}`);
    }
  };

  const placeManualEntry = async (direction) => {
    setConfirmManual(null);
    if (busy) return;
    setBusy(true);
    try {
      // PART 3 — submit selected engine + (sticky) lots + advisory snapshot
      const selected = engine === "smc" ? smc : score;
      const confidence = engine === "smc"
        ? (selected.confidence ?? null)
        : (direction === "CALL" ? selected.call_score : selected.put_score) ?? null;
      const reasons = engine === "smc" ? (selected.reasons || []) : [
        `Indicator bias: ${selected.bias || "—"}`,
        `Strength: ${selected.strength || "—"}`,
      ];
      const { data } = await axios.post(`${API}/bot/manual_entry`, {
        direction,
        engine,
        confidence,
        reasons,
      });
      toast.success(`${direction} entry queued (#${data.cmd_id})`, {
        description: `Engine: ${engine.toUpperCase()} · Lots: ${data.lots ?? executionLots} · SL ${status?.manual_sl_pct ?? 15}% / TP ${status?.manual_tp_pct ?? 30}% / Trail ${status?.trail_step_pct ?? 10}%`,
      });
      // v2.2 — Fixed Position Sizing is fully deterministic; no sticky
      // override to reset.
      await fetchAll();
      // v1.13 — poll the command result so a broker-side rejection (RMS,
      // insufficient funds, exchange reject, invalid symbol, market closed)
      // never leaves a phantom "Queued" toast. The daemon completes the
      // command within a few hundred ms; we poll for up to ~6 s to cover
      // slow broker roundtrips.
      const pollCmdId = data.cmd_id;
      let done = false;
      for (let i = 0; i < 6 && !done; i++) {
        await new Promise((r) => setTimeout(r, 1000));
        try {
          const { data: cmds } = await axios.get(`${API}/bot/commands?limit=10`);
          const cmd = (cmds || []).find((x) => x.id === pollCmdId);
          if (!cmd || cmd.status === "pending" || cmd.status === null) continue;
          done = true;
          if (cmd.broker_status === "rejected" || cmd.broker_status === "not_submitted") {
            const tag = cmd.rejection?.tag === "PRECHECK_FAILED" ? "Order Not Submitted" : "Order Rejected";
            const desc = cmd.user_message || cmd.result || "Broker rejection";
            toast.error(tag, {
              description: `${desc}${cmd.broker_reason ? ` · Reason: ${cmd.broker_reason}` : ""}`,
              duration: 8000,
            });
            await fetchAll();
          }
        } catch (e) {
          // best-effort — keep silent, the fetchAll below refreshes state
        }
      }
    } catch (err) {
      toast.error(`Manual entry failed`, {
        description: err?.response?.data?.detail || err.message,
      });
    } finally {
      setBusy(false);
    }
  };

  const exitPosition = async () => {
    if (busy) return;
    setBusy(true);
    try {
      await axios.post(`${API}/bot/panic_exit`);
      toast.success("Exit Position queued — closing trade now");
      await fetchAll();
    } catch (err) {
      toast.error(`Exit failed: ${err?.response?.data?.detail || err.message}`);
    } finally {
      setBusy(false);
    }
  };

  const resetBreakers = async () => {
    if (busy) return;
    setBusy(true);
    try {
      const { data } = await axios.post(`${API}/bot/reset_state`);
      toast.success(`Breakers reset (cmd #${data.cmd_id})`, {
        description: "Counters cleared — if FSM was SHUTDOWN it has returned to IDLE.",
      });
      await fetchAll();
    } catch (err) {
      toast.error(`Reset failed: ${err?.response?.data?.detail || err.message}`);
    } finally {
      setBusy(false);
    }
  };

  const setOrderType = async (field, value) => {
    if (busy) return;
    setBusy(true);
    try {
      await axios.post(`${API}/bot/order_types`, { [field]: value });
      toast.success(`${field === "entry_order_type" ? "Entry" : "SL"} set to ${value.replace("STOPLOSS_", "SL-")}`, {
        description: "Restarting bot to apply…",
      });
      try { await axios.post(`${API}/bot/control`, { action: "restart" }); } catch (_) { /* ignore */ }
      await fetchAll();
    } catch (err) {
      toast.error(`Order-type change failed: ${err?.response?.data?.detail || err.message}`);
    } finally {
      setBusy(false);
    }
  };

  const fsm = status?.fsm_state || "IDLE";
  const sup = status?.supervisor_state || "UNKNOWN";
  // PART 3 §11/§12 — execution gating
  const brokerOk = status?.broker_status === "connected";
  const feedOk = status && status.feed_stale === false;
  const canTrade = sup === "RUNNING" && brokerOk && feedOk;
  const realized = status?.realized_pnl_today ?? 0;
  const eqSnap = status?.equity_snapshot;
  const openPos = stats?.open_position;
  const totalPnl = stats?.total_pnl ?? 0;
  const winRate = stats?.win_rate ?? 0;
  const closedTrades = stats?.closed_trades ?? 0;

  // Live broker ticks
  const lq = status?.live_quotes || {};
  const lastTickTs = lq.ts ? new Date(lq.ts * 1000) : null;
  const tickAgeSec = lastTickTs ? Math.max(0, Math.round((Date.now() - lastTickTs.getTime()) / 1000)) : null;
  const optionLtp = lq.option_ltp ?? null;
  // P0-Q1: only trust option_ltp when its token matches the currently-open
  // position. Prevents cross-contract bleed where the last displayed LTP
  // came from a previously-open contract that no longer flows.
  const optionLtpToken = lq.option_ltp_token ?? null;
  const optionLtpTs = lq.option_ltp_ts ? new Date(lq.option_ltp_ts * 1000) : null;
  const optionLtpAgeSec = optionLtpTs ? Math.max(0, Math.round((Date.now() - optionLtpTs.getTime()) / 1000)) : null;
  const optionLtpMatchesPos = openPos?.contract_token && optionLtpToken
    ? String(openPos.contract_token) === String(optionLtpToken)
    : true;   // no open position → no mismatch possible
  const optionLtpFresh = optionLtpAgeSec != null && optionLtpAgeSec <= 15 && optionLtpMatchesPos;
  const livePnl = openPos && optionLtp != null && optionLtpMatchesPos
    ? (optionLtp - openPos.entry_price) * openPos.qty
    : null;

  // Setup advisory (Task 1)
  const score = status?.setup_score || {};
  const scoreStale = score.updated
    ? (Date.now() - new Date(score.updated).getTime()) / 1000 > 10
    : false;

  // SMC advisory (independent engine)
  const smc = status?.smc_score || {};
  const smcStale = smc.updated
    ? (Date.now() - new Date(smc.updated).getTime()) / 1000 > 10
    : false;

  // P0-Q1 diagnostics: WebSocket feed health
  const wsHealth = status?.ws_health || {};
  const wsConnected = !!wsHealth.connected;
  const wsSecondsSinceTick = wsHealth.seconds_since_last_tick;
  const wsReconnectFails = wsHealth.reconnect_failures ?? 0;
  const wsSubscribedCount = wsHealth.subscribed_count ?? 0;
  const wsHealthy = wsConnected && wsSecondsSinceTick != null && wsSecondsSinceTick < 15;

  // P0-5: currently-resolved Near-OTM contracts. No longer refreshed on a
  // background timer (P0-Q2) — refreshed on click-time and whenever the
  // confirmation modal is opened via /api/bot/refresh_atm.
  const atm = status?.atm_snapshot || {};
  const atmLeg = confirmManual === "CALL" ? atm.ce : (confirmManual === "PUT" ? atm.pe : null);
  const atmStale = atm.ts ? (Date.now() / 1000 - atm.ts) > 20 : true;

  // Buy-button glow = bias from the currently selected engine only
  const selectedBias =
    engine === "smc" ? (smc.direction === "CALL" || smc.direction === "PUT" ? smc.direction : null)
                      : (score.bias === "CALL" || score.bias === "PUT" ? score.bias : null);
  const selectedStrength =
    engine === "smc" ? smc.strength : score.strength;
  const callGlow = selectedBias === "CALL" ? selectedStrength : null;
  const putGlow = selectedBias === "PUT" ? selectedStrength : null;

  function glowClass(strength) {
    if (strength === "STRONG") return "animate-pulse shadow-[0_0_24px_currentColor] ring-2 ring-current";
    if (strength === "GOOD")   return "shadow-[0_0_12px_currentColor] ring-1 ring-current";
    if (strength === "NEUTRAL") return "ring-1 ring-amber-500/70";
    return "";
  }

  const equityChartData = equity.map((p) => ({
    t: new Date(p.timestamp).getTime(),
    equity: p.current_equity,
    peak: p.peak_equity,
  }));

  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-100" data-testid="dashboard-root">
      <Toaster theme="dark" position="top-right" />

      {/* Header */}
      <header className="border-b border-zinc-800 bg-zinc-950/90 backdrop-blur sticky top-0 z-10">
        <div className="max-w-7xl mx-auto px-6 py-4 flex items-center justify-between flex-wrap gap-3">
          <div className="flex items-center gap-4">
            <div className="h-9 w-9 grid place-items-center bg-amber-500/10 border border-amber-500/30 rounded-none">
              <ActivityIcon className="h-4 w-4 text-amber-400" />
            </div>
            <div>
              <div className="text-xs uppercase tracking-[0.22em] text-zinc-500 font-mono">Nifty Options Bot</div>
              <h1 className="text-lg font-mono font-semibold text-zinc-100">SmartAPI · ATM Weekly · Lot 65</h1>
            </div>
          </div>

          <div className="flex items-center gap-4">
            {/* Trading mode — 2-way segmented control (SIM ↔ LIVE) */}
            <div className="flex items-center border border-zinc-800 bg-zinc-900/60 rounded-none divide-x divide-zinc-800" data-testid="mode-control">
              {[
                { id: "sim",   label: "SIM",   active: "bg-blue-600/80 text-zinc-950" },
                { id: "live",  label: "LIVE",  active: "bg-red-600/80 text-zinc-50" },
              ].map((m) => {
                const isActive = status?.trading_mode === m.id;
                return (
                  <button
                    key={m.id}
                    data-testid={`mode-${m.id}`}
                    disabled={busy || isActive}
                    onClick={() => requestModeChange(m.id)}
                    className={`px-4 py-1.5 text-xs font-mono font-semibold tracking-wider transition-colors disabled:cursor-default ${
                      isActive ? m.active : "text-zinc-500 hover:text-zinc-200 hover:bg-zinc-800/60"
                    }`}
                  >
                    {m.label}
                  </button>
                );
              })}
            </div>

            <Badge
              data-testid="badge-supervisor"
              variant="outline"
              className={`font-mono rounded-none border-zinc-700 bg-zinc-900 ${SUP_COLORS[sup]}`}
            >
              <CircleDotIcon className="h-3 w-3 mr-1.5" />
              {sup}
            </Badge>
            <div
              data-testid="last-tick"
              className="hidden md:flex items-center gap-1.5 text-[10px] font-mono px-2 py-1 border border-zinc-800 bg-zinc-900/60"
              title="Tick = a price update from Angel One. Green pulse = ticks flowing within the last 3 s."
            >
              <span className={`h-1.5 w-1.5 rounded-full ${
                tickAgeSec == null ? "bg-zinc-600"
                : tickAgeSec < 3 ? "bg-emerald-400 animate-pulse"
                : tickAgeSec < 10 ? "bg-amber-400"
                : "bg-red-400"
              }`} />
              <span className="text-zinc-500">tick</span>
              <span className="text-zinc-300">
                {tickAgeSec == null ? "—" : tickAgeSec < 60 ? `${tickAgeSec}s` : `${Math.round(tickAgeSec/60)}m`}
              </span>
              {lq.spot != null && (
                <>
                  <span className="text-zinc-600 mx-1">·</span>
                  <span className="text-amber-300">N {Math.round(lq.spot).toLocaleString("en-IN")}</span>
                </>
              )}
              {lq.vix != null && (
                <>
                  <span className="text-zinc-600 mx-1">·</span>
                  <span className="text-blue-300">VIX {lq.vix.toFixed(2)}</span>
                </>
              )}
              {/* P0-Q1: WS feed integrity strip — connected + subs + failure count */}
              <span className="text-zinc-600 mx-1">·</span>
              <span
                data-testid="ws-health-strip"
                className={wsHealthy ? "text-emerald-300" : "text-red-300"}
                title={
                  `WS ${wsConnected ? "connected" : "down"} · ` +
                  `${wsSubscribedCount} tokens subscribed · ` +
                  `${wsReconnectFails} reconnect failure(s) · ` +
                  `last tick ${wsSecondsSinceTick != null ? wsSecondsSinceTick.toFixed(0) + "s ago" : "—"}`
                }
              >
                ws {wsHealthy ? "OK" : "!"}  ({wsSubscribedCount})
              </span>
            </div>
            <span className="text-[10px] font-mono text-zinc-500 hidden md:inline">
              {lastUpdate ? `synced ${fmtTime(lastUpdate.toISOString())}` : "syncing…"}
            </span>
          </div>
        </div>
      </header>

      <main className="max-w-7xl mx-auto px-6 py-8 space-y-8">
        {/* Hero strip — FSM state + controls */}
        <section className="grid grid-cols-1 lg:grid-cols-[1.4fr_1fr] gap-6">
          <motion.div
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.4 }}
          >
            <Card className="border-zinc-800 bg-gradient-to-br from-zinc-900 to-zinc-950 p-8 rounded-none">
              <div className="text-[10px] uppercase tracking-[0.2em] text-zinc-500 font-mono mb-3">Current FSM state</div>
              <div className="flex items-baseline gap-4 flex-wrap">
                <span
                  data-testid="fsm-state"
                  className={`px-4 py-2 text-2xl font-mono font-semibold border ${STATE_COLORS[fsm] || STATE_COLORS.IDLE}`}
                >
                  {fsm}
                </span>
                <span className="text-xs font-mono text-zinc-500">
                  {status?.fsm_last_transition
                    ? `from ${status.fsm_last_transition.old_state} · ${fmtDateTime(status.fsm_last_transition.timestamp)}`
                    : "no transitions yet"}
                </span>
              </div>
              <div className="mt-6 grid grid-cols-2 sm:grid-cols-4 gap-x-6 gap-y-3 text-sm font-mono">
                <div>
                  <div className="text-[10px] uppercase tracking-wider text-zinc-500">Lots</div>
                  <div className="text-zinc-100" data-testid="fsm-lots">{executionLots}</div>
                </div>
                <div>
                  <div className="text-[10px] uppercase tracking-wider text-zinc-500">
                    {status?.trading_mode === "live" ? "Live Cash (RMS)" : "Sim Capital"}
                  </div>
                  <div className="text-zinc-100" data-testid="hero-capital">
                    {status?.broker_capital?.value != null
                      ? `₹${Math.round(status.broker_capital.value).toLocaleString("en-IN")}`
                      : (eqSnap ? fmtINR(eqSnap.current_equity) : "—")}
                  </div>
                </div>
                <div>
                  <div className="text-[10px] uppercase tracking-wider text-zinc-500">Peak</div>
                  <div className="text-zinc-100">{eqSnap ? fmtINR(eqSnap.peak_equity) : "—"}</div>
                </div>
                <div>
                  <div className="text-[10px] uppercase tracking-wider text-zinc-500">Drawdown</div>
                  <div className={(eqSnap?.drawdown_pct ?? 0) >= 0.1 ? "text-amber-300" : "text-zinc-100"}>
                    {eqSnap ? `${(eqSnap.drawdown_pct * 100).toFixed(2)}%` : "—"}
                  </div>
                </div>
              </div>

              {/* Paper-mode editable capital removed — SIM uses real Angel cash */}
            </Card>
          </motion.div>

          {/* Control panel */}
          <Card className="border-zinc-800 bg-zinc-950/70 p-6 rounded-none">
            <div className="text-[10px] uppercase tracking-[0.2em] text-zinc-500 font-mono mb-4">Bot control</div>
            <div className="grid grid-cols-3 gap-3">
              <Button
                data-testid="btn-start"
                disabled={busy || sup === "RUNNING"}
                onClick={() => control("start")}
                className="rounded-none bg-emerald-600 hover:bg-emerald-500 text-zinc-950 font-mono font-semibold disabled:opacity-40"
              >
                <PowerIcon className="h-4 w-4 mr-2" /> Start
              </Button>
              <Button
                data-testid="btn-stop"
                disabled={busy || sup !== "RUNNING"}
                onClick={() => control("stop")}
                className="rounded-none bg-zinc-800 hover:bg-zinc-700 text-zinc-100 font-mono font-semibold disabled:opacity-40"
              >
                <PauseIcon className="h-4 w-4 mr-2" /> Stop
              </Button>
              <Button
                data-testid="btn-restart"
                disabled={busy}
                onClick={() => control("restart")}
                className="rounded-none bg-amber-600 hover:bg-amber-500 text-zinc-950 font-mono font-semibold disabled:opacity-40"
              >
                <RotateCwIcon className="h-4 w-4 mr-2" /> Restart
              </Button>
            </div>
            <div className="mt-5 text-[11px] font-mono text-zinc-500 leading-relaxed border-t border-zinc-800 pt-4 space-y-2">
              {/* Order type toggles — compact 2-row matrix */}
              <div className="space-y-1.5 pb-2 border-b border-zinc-800">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[10px] uppercase tracking-wider text-zinc-500">Entry</span>
                  <div className="flex border border-zinc-800 divide-x divide-zinc-800">
                    {["MARKET", "LIMIT"].map((v) => {
                      const active = status?.entry_order_type === v;
                      return (
                        <button
                          key={v}
                          data-testid={`entry-${v.toLowerCase()}`}
                          disabled={busy || active}
                          onClick={() => setOrderType("entry_order_type", v)}
                          className={`px-2.5 py-0.5 text-[10px] font-mono font-semibold transition-colors disabled:cursor-default ${
                            active ? "bg-blue-600/80 text-zinc-950" : "text-zinc-500 hover:text-zinc-200 hover:bg-zinc-800/60"
                          }`}
                        >
                          {v}
                        </button>
                      );
                    })}
                  </div>
                </div>
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[10px] uppercase tracking-wider text-zinc-500">Stop loss</span>
                  <div className="flex border border-zinc-800 divide-x divide-zinc-800">
                    {[
                      { id: "STOPLOSS_MARKET", label: "SL-M" },
                      { id: "STOPLOSS_LIMIT", label: "SL-L" },
                    ].map((v) => {
                      const active = status?.sl_order_type === v.id;
                      return (
                        <button
                          key={v.id}
                          data-testid={`sl-${v.label.toLowerCase()}`}
                          disabled={busy || active}
                          onClick={() => setOrderType("sl_order_type", v.id)}
                          className={`px-2.5 py-0.5 text-[10px] font-mono font-semibold transition-colors disabled:cursor-default ${
                            active ? "bg-blue-600/80 text-zinc-950" : "text-zinc-500 hover:text-zinc-200 hover:bg-zinc-800/60"
                          }`}
                        >
                          {v.label}
                        </button>
                      );
                    })}
                  </div>
                </div>
                <div className="text-[10px] text-zinc-600 pt-0.5">Target always LIMIT (locks reward, zero slippage)</div>
              </div>

              {status === null ? (
                <div className="flex items-center gap-2 text-zinc-500">
                  <ShieldAlertIcon className="h-3.5 w-3.5" /> Loading mode…
                </div>
              ) : status.trading_mode === "sim" ? (
                <div className="flex items-center gap-2 text-blue-400">
                  <ShieldAlertIcon className="h-3.5 w-3.5" /> SIM — real Angel data, simulated order fills (safe)
                </div>
              ) : (
                <div className="flex items-center gap-2 text-red-400">
                  <ShieldAlertIcon className="h-3.5 w-3.5" /> LIVE — real orders to NSE/NFO
                </div>
              )}
              <div className="flex items-center gap-2 text-zinc-500">
                <DatabaseIcon className="h-3.5 w-3.5" /> {status?.db_path}
              </div>
              <button
                data-testid="btn-reset-history"
                onClick={() => setConfirmReset(true)}
                className="flex items-center gap-2 text-zinc-500 hover:text-red-400 transition-colors text-[11px] mt-1"
              >
                <Trash2Icon className="h-3.5 w-3.5" /> Reset history (current mode)
              </button>
            </div>
          </Card>
        </section>

        {/* Position card — always visible */}
        <Card
          data-testid="position-card"
          className={`p-5 rounded-none flex items-center justify-between flex-wrap gap-4 ${
            openPos ? "border-emerald-700/50 bg-emerald-950/20" : "border-zinc-800 bg-zinc-950/70"
          }`}
        >
          <div className="flex items-center gap-4 flex-1 min-w-0">
            <BriefcaseIcon className={`h-5 w-5 ${openPos ? "text-emerald-400" : "text-zinc-600"}`} />
            <div className="min-w-0">
              <div className={`text-[10px] uppercase tracking-[0.2em] font-mono ${
                openPos ? "text-emerald-400" : "text-zinc-500"
              }`}>
                {openPos ? "Open position" : "No open position"}
              </div>
              {openPos ? (
                <div className="font-mono text-zinc-100 mt-1">
                  <span className={openPos.direction === "CALL" ? "text-emerald-300" : "text-red-300"}>
                    {openPos.direction}
                  </span>
                  <span className="text-zinc-500"> · </span>
                  <span data-testid="open-pos-lots">
                    {openPos.lots ?? Math.floor((openPos.qty || 0) / 65)} lot{(openPos.lots ?? 1) === 1 ? "" : "s"}
                    <span className="text-zinc-500"> ({openPos.qty} qty)</span>
                  </span>
                  <span className="text-zinc-500"> · </span>
                  entry <span className="text-zinc-200">{fmtINR(openPos.entry_price)}</span>
                  {optionLtp != null && (
                    <>
                      <span className="text-zinc-500"> · </span>
                      <span data-testid="option-ltp" className={
                        !optionLtpMatchesPos ? "text-red-300" :
                        !optionLtpFresh ? "text-amber-300" : "text-zinc-200"
                      }>
                        LTP {fmtINR(optionLtp)}
                        {optionLtpAgeSec != null && (
                          <span className="text-[10px] text-zinc-500 ml-1">
                            ({optionLtpAgeSec}s ago)
                          </span>
                        )}
                      </span>
                    </>
                  )}
                  {openPos.contract_token && !optionLtpMatchesPos && (
                    <div data-testid="ltp-token-mismatch" className="mt-1 text-[11px] text-red-300 border border-red-800 bg-red-950/20 px-2 py-1">
                      ⚠ LTP shown is from a different contract ({optionLtpToken}). Live P&amp;L suppressed until this contract starts ticking.
                    </div>
                  )}
                  {openPos.source === "manual" && (
                    <span className="ml-2 px-1.5 py-0.5 text-[10px] border border-amber-700 text-amber-300 font-mono">
                      MANUAL
                    </span>
                  )}
                  {openPos.contract_symbol && (
                    <div
                      data-testid="open-pos-contract"
                      className="mt-1 text-[11px] text-amber-300"
                    >
                      {openPos.contract_symbol}
                      {openPos.strike != null && (
                        <span className="text-zinc-500"> · strike {openPos.strike}</span>
                      )}
                      {openPos.expiry && (
                        <span className="text-zinc-500"> · exp {openPos.expiry}</span>
                      )}
                      {openPos.contract_token && (
                        <span className="text-zinc-600"> · tok {openPos.contract_token}</span>
                      )}
                    </div>
                  )}
                  {livePnl != null && (
                    <div
                      data-testid="live-pnl"
                      className={`mt-2 text-base ${livePnl >= 0 ? "text-emerald-300" : "text-red-300"}`}
                    >
                      <span className="text-[10px] uppercase tracking-wider text-zinc-500 mr-2">Live P&L</span>
                      {livePnl >= 0 ? "+" : ""}{fmtINR(livePnl)}
                    </div>
                  )}
                </div>
              ) : (
                <div className="font-mono text-xs text-zinc-500 mt-1">
                  bot will fire one automatically — or use the manual entry buttons →
                </div>
              )}
            </div>
          </div>

          {/* Right side: manual entry / exit / counters */}
          <div className="flex items-center gap-3 flex-wrap">
            {/* PART 3 §11/§12 — Broker + Feed badges */}
            <div className="flex items-center gap-3 border-r border-zinc-800 pr-3">
              <div className="text-right">
                <div className="text-[10px] uppercase tracking-[0.2em] font-mono text-zinc-500">Broker</div>
                <div
                  data-testid="broker-status"
                  className={`font-mono text-xs mt-1 ${
                    brokerOk ? "text-emerald-300" : "text-red-300"
                  }`}
                >
                  {brokerOk ? "🟢 Connected" : "🔴 Disconnected"}
                </div>
              </div>
              <div className="text-right">
                <div className="text-[10px] uppercase tracking-[0.2em] font-mono text-zinc-500">Feed</div>
                <div
                  data-testid="feed-status"
                  className={`font-mono text-xs mt-1 ${
                    feedOk ? "text-emerald-300" : "text-red-300"
                  }`}
                >
                  {feedOk ? "🟢 Live" : "🔴 Stale"}
                </div>
              </div>
            </div>

            {openPos ? (
              <Button
                data-testid="btn-exit-position"
                onClick={exitPosition}
                disabled={busy}
                className="rounded-none bg-red-600 hover:bg-red-500 text-zinc-50 font-mono font-semibold disabled:opacity-40"
              >
                <XOctagonIcon className="h-4 w-4 mr-2" /> Exit Position
              </Button>
            ) : fsm === "SHUTDOWN" ? (
              <div
                data-testid="shutdown-notice"
                className="px-3 py-2 border border-amber-800 bg-amber-950/30 text-amber-300 text-xs font-mono max-w-xs text-right"
              >
                <div className="font-semibold">Trading halted for the day</div>
                <div className="mt-1 text-amber-400/80">
                  Counters auto-reset at next-day rollover (IST).
                </div>
              </div>
            ) : (
              <>
                <Button
                  data-testid="btn-buy-call"
                  onClick={() => { setConfirmManual("CALL"); axios.post(`${API}/bot/refresh_atm`).catch(() => {}); }}
                  disabled={busy || !canTrade}
                  className={`rounded-none bg-emerald-600 hover:bg-emerald-500 text-zinc-950 font-mono font-semibold disabled:opacity-40 text-emerald-300 ${glowClass(callGlow)}`}
                >
                  <ArrowUpRightIcon className="h-4 w-4 mr-2" /> Buy Call
                </Button>
                <Button
                  data-testid="btn-buy-put"
                  onClick={() => { setConfirmManual("PUT"); axios.post(`${API}/bot/refresh_atm`).catch(() => {}); }}
                  disabled={busy || !canTrade}
                  className={`rounded-none bg-red-600 hover:bg-red-500 text-zinc-50 font-mono font-semibold disabled:opacity-40 text-red-300 ${glowClass(putGlow)}`}
                >
                  <ArrowDownRightIcon className="h-4 w-4 mr-2" /> Buy Put
                </Button>
              </>
            )}
            <div className="text-right border-l border-zinc-800 pl-3">
              <div className="text-[10px] uppercase tracking-[0.2em] font-mono text-zinc-500">Closed trades</div>
              <div className="font-mono text-zinc-200 mt-1">
                <span data-testid="closed-trades-count">{closedTrades}</span>
                <span className="text-zinc-500"> · today </span>
                <span className="text-zinc-200">{status?.trades_today ?? 0}</span>
                <span className="text-zinc-500">/4</span>
              </div>
              {openPos && (
                <div className="text-[10px] font-mono text-zinc-500 mt-1">
                  opened {fmtDateTime(openPos.entry_time)}
                </div>
              )}
            </div>
          </div>
        </Card>

        {/* Engine selector — chooses which advisory drives the Buy buttons */}
        <Card data-testid="engine-selector" className="border-zinc-800 bg-zinc-950/70 p-4 rounded-none">
          <div className="flex items-center justify-between flex-wrap gap-3">
            <div>
              <div className="text-[10px] uppercase tracking-[0.2em] text-zinc-500 font-mono">Selected Engine</div>
              <div className="text-[10px] font-mono text-zinc-600 mt-1">
                Buy Call / Buy Put buttons execute the selected engine&apos;s signal. Engines remain fully independent.
              </div>
            </div>
            <div className="flex border border-zinc-800 divide-x divide-zinc-800">
              {[
                { id: "indicator", label: "INDICATOR" },
                { id: "smc",       label: "SMC" },
              ].map((opt) => {
                const active = engine === opt.id;
                return (
                  <button
                    key={opt.id}
                    data-testid={`engine-${opt.id}`}
                    onClick={() => setEngine(opt.id)}
                    className={`px-3 py-1.5 text-xs font-mono font-semibold tracking-wider transition-colors ${
                      active ? "bg-amber-500/80 text-zinc-950" : "text-zinc-500 hover:text-zinc-200 hover:bg-zinc-800/60"
                    }`}
                  >
                    {active && <span className="mr-1.5">●</span>}{opt.label}
                  </button>
                );
              })}
            </div>
          </div>
        </Card>

        {/* v1.15 — AUTO / MANUAL execution mode toggle */}
        <Card data-testid="mode-toggle-card" className="border-zinc-800 bg-zinc-950/70 p-4 rounded-none">
          <div className="flex items-center justify-between flex-wrap gap-3">
            <div>
              <div className="text-[10px] uppercase tracking-[0.2em] text-zinc-500 font-mono">Trading Execution Mode</div>
              <div className="text-[10px] font-mono text-zinc-600 mt-1">
                AUTO fires SMC entries automatically when confidence ≥ {status?.smc_auto_trade_threshold ?? 40}% OR when BOS + Structure aligns (ignores confidence). MANUAL requires Buy Call / Buy Put click.
              </div>
              {status?.auto_suspended_reason && (
                <div data-testid="auto-suspended-banner" className="mt-2 text-[11px] text-red-300 border border-red-800 bg-red-950/20 px-2 py-1">
                  🛑 AUTO SUSPENDED — {status.auto_suspended_reason}{" "}
                  <button
                    data-testid="auto-resume-btn"
                    className="ml-2 underline text-red-200 hover:text-red-100"
                    onClick={async () => {
                      try {
                        await axios.post(`${API}/bot/auto_resume`);
                        toast.success("Auto trading resumed");
                        await fetchAll();
                      } catch (e) {
                        toast.error(`Resume failed: ${e?.response?.data?.detail || e.message}`);
                      }
                    }}
                  >
                    Resume
                  </button>
                </div>
              )}
            </div>
            <div className="flex items-center gap-3">
              <div className="text-[10px] font-mono text-zinc-500 uppercase tracking-wider">
                Mode: <span className={`ml-1 ${status?.auto_trade_enabled ? "text-emerald-400" : "text-zinc-300"}`} data-testid="mode-badge">
                  {status?.trading_execution_mode ?? "MANUAL"}
                </span>
              </div>
              <div className="flex border border-zinc-800 divide-x divide-zinc-800">
                {[
                  { id: false, label: "MANUAL" },
                  { id: true, label: "AUTO" },
                ].map((opt) => {
                  const active = !!status?.auto_trade_enabled === opt.id;
                  return (
                    <button
                      key={String(opt.id)}
                      data-testid={`mode-${opt.label.toLowerCase()}`}
                      onClick={async () => {
                        try {
                          await axios.post(`${API}/bot/auto_mode`, { enabled: opt.id });
                          toast.success(`Switched to ${opt.label}`);
                          await fetchAll();
                        } catch (e) {
                          toast.error(`Mode change failed: ${e?.response?.data?.detail || e.message}`);
                        }
                      }}
                      className={`px-3 py-1.5 text-xs font-mono font-semibold tracking-wider transition-colors ${
                        active ? "bg-emerald-500/80 text-zinc-950" : "text-zinc-500 hover:text-zinc-200 hover:bg-zinc-800/60"
                      }`}
                    >
                      {active && <span className="mr-1.5">●</span>}{opt.label}
                    </button>
                  );
                })}
              </div>
            </div>
          </div>
        </Card>

        {/* v2.2 — Fixed Position Sizing card (SIM Capital editable · LIVE = broker RMS mirror) */}
        <Card data-testid="sizing-card" className="border-zinc-800 bg-zinc-950/70 p-4 rounded-none">
          <div className="flex flex-wrap items-start gap-4">
            <div className="flex-1 min-w-[220px]">
              <div className="text-[10px] uppercase tracking-[0.2em] text-zinc-500 font-mono mb-2">Position Sizing (Fixed)</div>
              <div className="text-[10px] font-mono text-zinc-600">
                Deterministic capital→lots mapping. SIM uses Simulation Capital · LIVE uses broker capital. Risk 2.5% · SL 15% · TP 30% · Trail 10%.
              </div>
              <div className="mt-3 grid grid-cols-2 gap-2 text-[11px] font-mono">
                <div className="text-zinc-500">Risk:</div><div className="text-zinc-300">2.5%</div>
                <div className="text-zinc-500">SL:</div><div className="text-zinc-300">15%</div>
                <div className="text-zinc-500">TP:</div><div className="text-zinc-300">30%</div>
                <div className="text-zinc-500">Trail:</div><div className="text-zinc-300">10%</div>
              </div>
            </div>
            <div className="flex flex-wrap gap-3 text-xs font-mono">
              {status?.trading_mode !== "live" ? (
                <label className="flex flex-col text-zinc-500">
                  <span className="text-[9px] uppercase tracking-widest mb-1">Sim Capital ₹</span>
                  <input
                    data-testid="sim-capital-input"
                    type="number" min="1" step="1000"
                    value={simCapInput}
                    onFocus={() => setSimCapFocused(true)}
                    onChange={(e) => setSimCapInput(e.target.value)}
                    onBlur={async (e) => {
                      setSimCapFocused(false);
                      const v = parseFloat(e.target.value);
                      const persisted = Math.round(status?.sim_capital || 0);
                      if (!(v > 0)) {
                        // Revert visually if user cleared or entered invalid value
                        setSimCapInput(String(persisted));
                        return;
                      }
                      if (Math.round(v) === persisted) return;  // no-op
                      try {
                        await axios.post(`${API}/bot/sizing_config`, { sim_capital: v });
                        toast.success(`SIM Capital saved: ₹${Math.round(v).toLocaleString("en-IN")}`);
                        await fetchAll();
                      } catch (err) {
                        toast.error(err?.response?.data?.detail || err.message);
                        setSimCapInput(String(persisted));
                      }
                    }}
                    className="w-32 bg-zinc-900 border border-zinc-800 px-2 py-1 text-amber-200"
                  />
                </label>
              ) : (
                <div className="flex flex-col text-zinc-500 justify-end">
                  <span className="text-[9px] uppercase tracking-widest mb-1">Broker Capital (RMS)</span>
                  <div data-testid="broker-capital-display" className="text-amber-200 text-sm">
                    ₹{Math.round(status?.broker_capital?.value || 0).toLocaleString("en-IN")}
                  </div>
                </div>
              )}
              <div className="flex flex-col text-zinc-500 justify-end">
                <span className="text-[9px] uppercase tracking-widest mb-1">Execution Lots</span>
                <div className="text-amber-200 text-sm" data-testid="execution-lots">
                  {executionLots}
                </div>
              </div>
            </div>
          </div>
        </Card>

        {/* v2.4 — Daily Loss Protection banner (only when suspended) */}
        {dailyLoss?.breached && (
          <Card
            data-testid="daily-loss-banner"
            className="border-red-800 bg-red-950/40 p-4 rounded-none"
          >
            <div className="flex items-start gap-3">
              <div className="text-red-400 font-mono text-xs uppercase tracking-widest">🛑 AUTO SUSPENDED</div>
              <div className="flex-1 font-mono text-xs text-zinc-300">
                <div className="text-red-200 font-semibold mb-1">Reason: Maximum Daily Loss Reached</div>
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 mt-2">
                  <div>
                    <div className="text-[9px] text-zinc-500 uppercase">Capital</div>
                    <div className="text-zinc-200">₹{Math.round(dailyLoss.capital || 0).toLocaleString("en-IN")}</div>
                  </div>
                  <div>
                    <div className="text-[9px] text-zinc-500 uppercase">Risk %</div>
                    <div className="text-zinc-200">{dailyLoss.risk_pct?.toFixed(2)}%</div>
                  </div>
                  <div>
                    <div className="text-[9px] text-zinc-500 uppercase">Maximum Loss</div>
                    <div className="text-zinc-200">₹{Math.round(dailyLoss.max_daily_loss || 0).toLocaleString("en-IN")}</div>
                  </div>
                  <div>
                    <div className="text-[9px] text-zinc-500 uppercase">Today&apos;s Loss</div>
                    <div className="text-red-300">₹{Math.round(dailyLoss.realized_pnl_today || 0).toLocaleString("en-IN")}</div>
                  </div>
                </div>
                <div className="mt-2 text-[10px] text-zinc-500">Resumes automatically on the next trading day, or press <em>Auto Resume</em>.</div>
              </div>
            </div>
          </Card>
        )}

        {/* v2.4 — Daily Loss Protection settings + status (always shown) */}
        {dailyLoss && !dailyLoss.breached && (
          <Card
            data-testid="daily-loss-card"
            className="border-zinc-800 bg-zinc-950/70 p-4 rounded-none"
          >
            <div className="flex flex-wrap items-center gap-6">
              <div className="flex-1 min-w-[220px]">
                <div className="text-[10px] uppercase tracking-[0.2em] text-zinc-500 font-mono mb-1">Daily Loss Protection</div>
                <div className="text-[10px] font-mono text-zinc-600">
                  Realized daily loss ≥ Max Daily Loss → AUTO suspended, alert sent, resume next trading day.
                </div>
              </div>
              <div className="flex flex-wrap gap-3 text-xs font-mono items-end">
                <label className="flex flex-col text-zinc-500">
                  <span className="text-[9px] uppercase tracking-widest mb-1">Risk %</span>
                  <input
                    data-testid="risk-pct-input"
                    type="number"
                    min="0.1"
                    max="100"
                    step="0.1"
                    defaultValue={dailyLoss.risk_pct?.toFixed(2)}
                    onBlur={async (e) => {
                      const v = parseFloat(e.target.value);
                      if (!(v > 0)) return;
                      try {
                        await axios.post(`${API}/bot/sizing_config`, { risk_pct: v });
                        toast.success(`Risk % saved: ${v.toFixed(2)}%`);
                        await fetchAll();
                      } catch (err) {
                        toast.error(err?.response?.data?.detail || err.message);
                      }
                    }}
                    className="w-24 bg-zinc-900 border border-zinc-800 px-2 py-1 text-amber-200"
                  />
                </label>
                <div className="flex flex-col text-zinc-500">
                  <span className="text-[9px] uppercase tracking-widest mb-1">Max Daily Loss</span>
                  <div data-testid="max-daily-loss" className="text-red-200 text-sm">
                    ₹{Math.round(dailyLoss.max_daily_loss || 0).toLocaleString("en-IN")}
                  </div>
                </div>
                <div className="flex flex-col text-zinc-500">
                  <span className="text-[9px] uppercase tracking-widest mb-1">Today&apos;s Realized</span>
                  <div
                    className={
                      (dailyLoss.realized_pnl_today || 0) >= 0
                        ? "text-emerald-300 text-sm"
                        : "text-red-300 text-sm"
                    }
                    data-testid="realized-today"
                  >
                    ₹{Math.round(dailyLoss.realized_pnl_today || 0).toLocaleString("en-IN")}
                  </div>
                </div>
                <div className="flex flex-col text-zinc-500">
                  <span className="text-[9px] uppercase tracking-widest mb-1">Remaining</span>
                  <div className="text-zinc-300 text-sm">
                    ₹{Math.round(dailyLoss.loss_remaining || 0).toLocaleString("en-IN")}
                  </div>
                </div>
              </div>
            </div>
          </Card>
        )}

        {/* v2.4 — Trigger analytics (per-trigger totals; today only) */}
        {triggerStats && (
          <Card
            data-testid="trigger-stats-card"
            className="border-zinc-800 bg-zinc-950/70 p-4 rounded-none"
          >
            <div className="flex flex-wrap items-baseline justify-between gap-2 mb-3">
              <div className="text-[10px] uppercase tracking-[0.2em] text-zinc-500 font-mono">Trigger Statistics (Today · IST)</div>
              <div className="text-[10px] font-mono text-zinc-600">
                Total: {triggerStats.total?.trades || 0} trades · Win {triggerStats.total?.win_rate_pct?.toFixed(1)}% ·{" "}
                <span className={((triggerStats.total?.net_pnl || 0) >= 0) ? "text-emerald-300" : "text-red-300"}>
                  ₹{Math.round(triggerStats.total?.net_pnl || 0).toLocaleString("en-IN")}
                </span>
              </div>
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
              {triggerStats.per_trigger?.map((row) => {
                const label = {
                  CONFIDENCE_THRESHOLD: "CONFIDENCE",
                  BOS_STRUCTURE: "BOS+STRUCTURE",
                  MANUAL: "MANUAL",
                }[row.trigger] || row.trigger;
                const accent = {
                  CONFIDENCE_THRESHOLD: "border-emerald-800 text-emerald-300",
                  BOS_STRUCTURE: "border-purple-800 text-purple-300",
                  MANUAL: "border-amber-800 text-amber-300",
                }[row.trigger] || "border-zinc-800 text-zinc-400";
                return (
                  <div
                    key={row.trigger}
                    data-testid={`trigger-stats-${row.trigger}`}
                    className={`border ${accent.split(" ")[0]} bg-zinc-900/40 p-3 font-mono text-xs`}
                  >
                    <div className={`text-[10px] uppercase tracking-widest mb-2 ${accent.split(" ")[1]}`}>{label}</div>
                    <div className="grid grid-cols-2 gap-1">
                      <div className="text-zinc-500">Trades:</div><div className="text-zinc-200">{row.trades}</div>
                      <div className="text-zinc-500">Wins:</div><div className="text-emerald-300">{row.wins}</div>
                      <div className="text-zinc-500">Losses:</div><div className="text-red-300">{row.losses}</div>
                      <div className="text-zinc-500">Win Rate:</div><div className="text-zinc-200">{row.win_rate_pct?.toFixed(1)}%</div>
                      <div className="text-zinc-500">Net PnL:</div>
                      <div className={row.net_pnl >= 0 ? "text-emerald-300" : "text-red-300"}>
                        ₹{Math.round(row.net_pnl || 0).toLocaleString("en-IN")}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          </Card>
        )}

        {/* Twin advisory cards — Indicator (left) and SMC (right), side by side */}
        <section className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Setup advisory — weighted Call/Put scores (Task 1) */}
        {score.timestamp ? (
          <Card
            data-testid="setup-advisory"
            className={`border-zinc-800 bg-zinc-950/70 p-5 rounded-none transition-shadow ${
              engine === "indicator" ? "ring-1 ring-amber-500/40" : ""
            }`}
          >
            <div className="flex items-center justify-between flex-wrap gap-2 mb-4">
              <div>
                <div className="text-[10px] uppercase tracking-[0.2em] text-zinc-500 font-mono">Indicator Setup Advisory</div>
                <div className="font-mono text-sm text-zinc-200 mt-1">
                  Bias:{" "}
                  <span className={
                    score.bias === "CALL" ? "text-emerald-300" :
                    score.bias === "PUT" ? "text-red-300" : "text-zinc-400"
                  }>{score.bias}</span>
                  <span className="text-zinc-600"> · </span>
                  <span className={
                    score.strength === "STRONG" ? "text-emerald-300" :
                    score.strength === "GOOD" ? "text-blue-300" :
                    score.strength === "NEUTRAL" ? "text-amber-300" :
                    "text-zinc-500"
                  }>{score.strength}</span>
                </div>
              </div>
              <div className="text-[10px] font-mono">
                {scoreStale ? (
                  <span className="text-red-400" data-testid="score-stale">⚠ stale ({score.timestamp})</span>
                ) : (
                  <span className="text-zinc-500">last updated {score.timestamp} IST</span>
                )}
              </div>
            </div>

            <div className="grid grid-cols-2 gap-4">
              {[
                { label: "Call score", val: score.call_score, color: "emerald" },
                { label: "Put score",  val: score.put_score,  color: "red" },
              ].map((s) => (
                <div key={s.label} className="space-y-1.5">
                  <div className="flex items-baseline justify-between font-mono">
                    <span className="text-[10px] uppercase tracking-wider text-zinc-500">{s.label}</span>
                    <span className={`text-2xl text-${s.color}-300`}>{s.val ?? 0}</span>
                  </div>
                  <div className="h-1.5 bg-zinc-900 border border-zinc-800">
                    <div
                      className={`h-full bg-${s.color}-500 transition-all`}
                      style={{ width: `${Math.min(100, s.val ?? 0)}%` }}
                    />
                  </div>
                </div>
              ))}
            </div>

            <div className="mt-3 pt-3 border-t border-zinc-800 text-[10px] font-mono text-zinc-500">
              base call <span className="text-zinc-300">{score.base_call}</span> · base put <span className="text-zinc-300">{score.base_put}</span> · liquidity penalty <span className="text-amber-300">−{score.penalty}</span>
              <span className="block mt-1">
                ≥60 STRONG · ≥45 GOOD · ≥30 NEUTRAL · ≥15 WEAK · &lt;15 AVOID. Score = EMA15m (20) + EMA9/21 3m (20) + ADX (10) + ADX-Δ (15) + VWAP (15) + VIX-band (10) − bid-ask spread penalty.
              </span>
            </div>
          </Card>
        ) : (
          <Card className="border-zinc-800 bg-zinc-950/70 p-5 rounded-none">
            <div className="text-[10px] uppercase tracking-[0.2em] text-zinc-500 font-mono">Indicator Setup Advisory</div>
            <div className="mt-4 text-xs font-mono text-zinc-500">warming up — waiting for first 3m bars…</div>
          </Card>
        )}

        {/* SMC Advisory — independent engine */}
        <Card
          data-testid="smc-advisory"
          className={`border-zinc-800 bg-zinc-950/70 p-5 rounded-none transition-shadow ${
            engine === "smc" ? "ring-1 ring-amber-500/40" : ""
          }`}
        >
          <div className="flex items-center justify-between flex-wrap gap-2 mb-4">
            <div>
              <div className="text-[10px] uppercase tracking-[0.2em] text-zinc-500 font-mono">SMC Setup Advisory</div>
              <div className="font-mono text-sm text-zinc-200 mt-1">
                Direction:{" "}
                <span className={
                  smc.direction === "CALL" ? "text-emerald-300" :
                  smc.direction === "PUT" ? "text-red-300" : "text-zinc-400"
                }>
                  {smc.direction === "CALL" ? "BUY CALL"
                    : smc.direction === "PUT" ? "BUY PUT"
                    : (smc.direction || "—")}
                </span>
                <span className="text-zinc-600"> · </span>
                <span className="text-amber-300" data-testid="smc-grade">
                  Grade {smc.grade ?? "—"}
                </span>
              </div>
            </div>
            <div className="text-[10px] font-mono">
              {smc.timestamp ? (
                smcStale ? (
                  <span className="text-red-400" data-testid="smc-stale">⚠ stale ({smc.timestamp})</span>
                ) : (
                  <span className="text-zinc-500">last updated {smc.timestamp} IST</span>
                )
              ) : (
                <span className="text-zinc-500">awaiting first tick…</span>
              )}
            </div>
          </div>

          <div className="space-y-1.5 mb-3">
            <div className="flex items-baseline justify-between font-mono">
              <span className="text-[10px] uppercase tracking-wider text-zinc-500">Confidence</span>
              <span
                data-testid="smc-confidence"
                className={
                  (smc.confidence ?? 0) >= 80 ? "text-2xl text-emerald-300"
                  : (smc.confidence ?? 0) >= 60 ? "text-2xl text-blue-300"
                  : (smc.confidence ?? 0) >= 40 ? "text-2xl text-amber-300"
                  : "text-2xl text-zinc-400"
                }
              >
                {smc.confidence ?? 0}%
              </span>
            </div>
            <div className="h-1.5 bg-zinc-900 border border-zinc-800">
              <div
                className={`h-full transition-all ${
                  smc.direction === "PUT" ? "bg-red-500" : "bg-emerald-500"
                }`}
                style={{ width: `${Math.min(100, smc.confidence ?? 0)}%` }}
              />
            </div>
          </div>

          <div className="grid grid-cols-3 gap-3 font-mono text-xs mb-3">
            <div>
              <div className="text-[10px] uppercase tracking-wider text-zinc-500">Entry</div>
              <div className="text-zinc-200 mt-0.5">
                {smc.entry != null ? smc.entry.toLocaleString("en-IN") : "—"}
              </div>
            </div>
            <div>
              <div className="text-[10px] uppercase tracking-wider text-zinc-500">Stop Loss</div>
              <div className="text-red-300 mt-0.5">
                {smc.stop_loss != null ? smc.stop_loss.toLocaleString("en-IN") : "—"}
              </div>
            </div>
            <div>
              <div className="text-[10px] uppercase tracking-wider text-zinc-500">Target</div>
              <div className="text-emerald-300 mt-0.5">
                {smc.target != null ? smc.target.toLocaleString("en-IN") : "—"}
              </div>
            </div>
          </div>

          {/* Structural context — Market Structure · HTF Trend · Regime */}
          <div className="grid grid-cols-3 gap-3 font-mono text-xs mb-3 border-t border-zinc-800 pt-3">
            <div>
              <div className="text-[10px] uppercase tracking-wider text-zinc-500">HTF Trend (15m)</div>
              <div
                data-testid="smc-htf-trend"
                className={`mt-0.5 ${
                  smc.htf_trend === "CALL" ? "text-emerald-300"
                  : smc.htf_trend === "PUT" ? "text-red-300"
                  : "text-zinc-400"
                }`}
              >
                {smc.htf_trend === "CALL" ? "Bullish (HH+HL)"
                  : smc.htf_trend === "PUT" ? "Bearish (LH+LL)"
                  : (smc.htf_trend || "—")}
              </div>
            </div>
            <div>
              <div className="text-[10px] uppercase tracking-wider text-zinc-500">Market Structure (5m)</div>
              <div
                data-testid="smc-structure"
                className={`mt-0.5 ${
                  smc.market_structure === "CALL" ? "text-emerald-300"
                  : smc.market_structure === "PUT" ? "text-red-300"
                  : "text-zinc-400"
                }`}
              >
                {smc.market_structure === "CALL" ? "Bullish (HH+HL)"
                  : smc.market_structure === "PUT" ? "Bearish (LH+LL)"
                  : (smc.market_structure || "—")}
              </div>
            </div>
            <div>
              <div className="text-[10px] uppercase tracking-wider text-zinc-500">Regime</div>
              <div
                data-testid="smc-regime"
                className={`mt-0.5 ${
                  smc.regime === "TRENDING" ? "text-emerald-300"
                  : smc.regime === "SIDEWAYS" ? "text-amber-300"
                  : smc.regime === "HIGH_VOL" ? "text-red-300"
                  : smc.regime === "LOW_VOL" ? "text-blue-300"
                  : "text-zinc-400"
                }`}
              >
                {smc.regime || "—"}
              </div>
            </div>
          </div>

          {/* Signal freshness — auto-expires after SMC_MAX_SIGNAL_AGE_MIN */}
          {smc.signal_age_sec != null && smc.signal_max_age_sec != null && (
            <div className="font-mono text-[10px] text-zinc-500 mb-3" data-testid="smc-signal-age">
              <span className="uppercase tracking-wider">Signal age </span>
              <span className={
                smc.signal_age_sec >= smc.signal_max_age_sec * 0.8
                  ? "text-amber-300" : "text-zinc-300"
              }>
                {smc.signal_age_sec}s
              </span>
              <span className="text-zinc-600"> / {smc.signal_max_age_sec}s · auto-expires</span>
            </div>
          )}

          {smc.reasons && smc.reasons.length > 0 && (
            <div className="border-t border-zinc-800 pt-3">
              <div className="text-[10px] uppercase tracking-wider text-zinc-500 font-mono mb-1.5">Reasons</div>
              <ul className="space-y-0.5 font-mono text-[11px] text-zinc-300" data-testid="smc-reasons">
                {smc.reasons.map((r, i) => (
                  <li key={i} className="leading-snug">· {r}</li>
                ))}
              </ul>
            </div>
          )}

          {/* P0-Q3: informational notes — warm-up hints, regime attenuation
              — kept SEPARATE from the weight-carrying reasons above. This
              is context, not evidence. */}
          {smc.notes && smc.notes.length > 0 && (
            <div className="border-t border-zinc-800 pt-3">
              <div className="text-[10px] uppercase tracking-wider text-zinc-500 font-mono mb-1.5">Notes</div>
              <ul className="space-y-0.5 font-mono text-[11px] text-zinc-500" data-testid="smc-notes">
                {smc.notes.map((n, i) => (
                  <li key={i} className="leading-snug italic">· {n}</li>
                ))}
              </ul>
            </div>
          )}

          <div className="mt-3 pt-3 border-t border-zinc-800 text-[10px] font-mono text-zinc-500">
            Grade: 95+ A+ · 90+ A · 85+ B+ · 80+ B · 75+ C · &lt;75 D.
            <span className="block mt-1">
              Weights: HTF Trend 20 · Structure 15 · BOS/CHoCH 20 · Sweep 15 · OB Retest 15 · FVG 10 · Premium/Discount 5.
              5m execution · 15m HTF (structure-based) · 09:20–15:15 IST · auto-expire after SMC_MAX_SIGNAL_AGE_MIN.
            </span>
          </div>
        </Card>
        </section>

        {/* Signal diagnostic */}
        {diag && diag.note && (
          <Card data-testid="signal-diagnostic" className="border-zinc-800 bg-zinc-950/70 p-5 rounded-none">
            <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
              <div className="text-[10px] uppercase tracking-[0.2em] text-zinc-500 font-mono">Signal diagnostic</div>
              <div className="text-[10px] text-zinc-600 font-mono">{diag.note}</div>
            </div>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-x-6 gap-y-2 text-xs font-mono">
              {[
                ["Bars 3m", diag.bars_3m, diag.bars_3m >= 22],
                ["Bars 15m", diag.bars_15m, diag.bars_15m >= 51],
                ["RSI", diag.rsi != null ? diag.rsi.toFixed(1) : "—", true],
                ["ADX", diag.adx != null ? diag.adx.toFixed(1) : "—", diag.adx != null ? diag.adx > diag.adx_min_req : false],
                ["ADX delta", diag.adx != null && diag.adx_prev != null ? (diag.adx - diag.adx_prev).toFixed(2) : "—",
                  diag.adx != null && diag.adx_prev != null ? (diag.adx - diag.adx_prev) > diag.adx_delta_req : false],
                ["VIX", diag.vix != null ? diag.vix.toFixed(2) : "—",
                  diag.vix != null && diag.vix >= diag.vix_band[0] && diag.vix <= diag.vix_band[1]],
                ["Macro trend", diag.ema_macro_fast > diag.ema_macro_slow ? "LONG bias" : diag.ema_macro_fast < diag.ema_macro_slow ? "SHORT bias" : "neutral", true],
                ["3m EMA9/21", diag.ema_fast_3m && diag.ema_slow_3m ? (diag.ema_fast_3m > diag.ema_slow_3m ? "F>S" : "F<S") : "—", true],
              ].map(([label, val, ok]) => (
                <div key={label}>
                  <div className="text-[10px] uppercase tracking-wider text-zinc-500">{label}</div>
                  <div className={`mt-0.5 ${ok === false ? "text-amber-300" : ok === true ? "text-emerald-300" : "text-zinc-200"}`}>
                    {String(val)}
                  </div>
                </div>
              ))}
            </div>
            <div className="mt-3 pt-3 border-t border-zinc-800 text-[10px] font-mono text-zinc-500">
              Need: ADX &gt; {diag.adx_min_req} · ADX delta &gt; {diag.adx_delta_req} ·
              RSI &gt; {diag.rsi_long_req} (long) or &lt; {diag.rsi_short_req} (short) ·
              VIX {diag.vix_band[0]}–{diag.vix_band[1]} · entry window 09:45–14:45 IST
            </div>
          </Card>
        )}

        {/* Stats row */}
        <section className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          <StatCard
            label="Total P&L (all-time)"
            value={fmtINR(totalPnl)}
            sub={`${closedTrades} closed trades`}
            icon={WalletIcon}
            tone={totalPnl > 0 ? "pos" : totalPnl < 0 ? "neg" : "default"}
          />
          <StatCard
            label="Realized P&L (today)"
            value={fmtINR(realized)}
            sub={`trades today: ${status?.trades_today ?? 0} / 4`}
            icon={realized >= 0 ? TrendingUpIcon : TrendingDownIcon}
            tone={realized > 0 ? "pos" : realized < 0 ? "neg" : "default"}
          />
          <StatCard
            label="Win rate"
            value={closedTrades ? `${(winRate * 100).toFixed(1)}%` : "—"}
            sub={closedTrades ? `${stats?.wins}W · ${stats?.losses}L` : "no closed trades yet"}
            icon={PercentIcon}
            tone={winRate >= 0.5 ? "pos" : winRate > 0 ? "warn" : "default"}
          />
          <StatCard
            label="Best / Worst"
            value={
              <span className="text-base font-mono">
                <span className="text-emerald-300">{stats?.best_trade ? fmtINR(stats.best_trade) : "—"}</span>
                <span className="text-zinc-600"> / </span>
                <span className="text-red-300">{stats?.worst_trade ? fmtINR(stats.worst_trade) : "—"}</span>
              </span>
            }
            sub={`avg ${stats?.avg_pnl ? fmtINR(stats.avg_pnl) : "—"}/trade`}
            icon={TargetIcon}
          />
        </section>

        {/* Equity chart */}
        <section>
          <Card className="border-zinc-800 bg-zinc-950/70 p-6 rounded-none">
            <div className="flex items-center justify-between mb-5">
              <div>
                <div className="text-[10px] uppercase tracking-[0.2em] text-zinc-500 font-mono">Equity curve</div>
                <div className="text-sm font-mono text-zinc-300 mt-1">{equity.length} sessions logged</div>
              </div>
            </div>
            <div className="h-72">
              {equity.length === 0 ? (
                <div className="h-full grid place-items-center text-zinc-600 font-mono text-sm">
                  No equity points yet — start the bot to begin logging.
                </div>
              ) : (
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={equityChartData} margin={{ top: 10, right: 20, left: 0, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#27272a" />
                    <XAxis
                      dataKey="t"
                      type="number"
                      domain={["dataMin", "dataMax"]}
                      tickFormatter={(v) => fmtDateShort(v)}
                      stroke="#52525b"
                      style={{ fontSize: 11, fontFamily: "monospace" }}
                    />
                    <YAxis stroke="#52525b" style={{ fontSize: 11, fontFamily: "monospace" }} />
                    <Tooltip
                      contentStyle={{ background: "#0a0a0a", border: "1px solid #27272a", fontFamily: "monospace", fontSize: 12 }}
                      labelFormatter={(v) => fmtDateTime(v)}
                      formatter={(v) => fmtINR(v)}
                    />
                    <Line type="monotone" dataKey="peak" stroke="#71717a" strokeWidth={1.5} dot={false} strokeDasharray="4 4" />
                    <Line type="monotone" dataKey="equity" stroke="#f59e0b" strokeWidth={2} dot={false} />
                  </LineChart>
                </ResponsiveContainer>
              )}
            </div>
          </Card>
        </section>

        {/* Tabs: trades + transitions */}
        <section>
          <Tabs defaultValue="trades">
            <TabsList className="bg-zinc-900 rounded-none border border-zinc-800">
              <TabsTrigger value="trades" data-testid="tab-trades" className="font-mono rounded-none">Trades</TabsTrigger>
              <TabsTrigger value="transitions" data-testid="tab-transitions" className="font-mono rounded-none">State transitions</TabsTrigger>
            </TabsList>

            <TabsContent value="trades" className="mt-4">
              <Card className="border-zinc-800 bg-zinc-950/70 rounded-none overflow-hidden">
                {trades.length === 0 ? (
                  <div className="p-8 text-center text-zinc-600 font-mono text-sm">No trades recorded yet.</div>
                ) : (
                  <Table>
                    <TableHeader>
                      <TableRow className="border-zinc-800 hover:bg-transparent">
                        <TableHead className="font-mono text-[10px] uppercase tracking-wider text-zinc-500">Trade ID</TableHead>
                        <TableHead className="font-mono text-[10px] uppercase tracking-wider text-zinc-500">Src</TableHead>
                        <TableHead className="font-mono text-[10px] uppercase tracking-wider text-zinc-500">Trigger</TableHead>
                        <TableHead className="font-mono text-[10px] uppercase tracking-wider text-zinc-500">Conf</TableHead>
                        <TableHead className="font-mono text-[10px] uppercase tracking-wider text-zinc-500">Dir</TableHead>
                        <TableHead className="font-mono text-[10px] uppercase tracking-wider text-zinc-500">Lots</TableHead>
                        <TableHead className="font-mono text-[10px] uppercase tracking-wider text-zinc-500">Qty</TableHead>
                        <TableHead className="font-mono text-[10px] uppercase tracking-wider text-zinc-500">Entry</TableHead>
                        <TableHead className="font-mono text-[10px] uppercase tracking-wider text-zinc-500">Exit</TableHead>
                        <TableHead className="font-mono text-[10px] uppercase tracking-wider text-zinc-500">PnL</TableHead>
                        <TableHead className="font-mono text-[10px] uppercase tracking-wider text-zinc-500">Reason</TableHead>
                        <TableHead className="font-mono text-[10px] uppercase tracking-wider text-zinc-500">Time</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {trades.map((t) => (
                        <TableRow
                          key={t.trade_id}
                          data-testid={`trade-row-${t.trade_id}`}
                          onClick={() => setTimelineTrade(t)}
                          className="border-zinc-800 hover:bg-zinc-900/40 font-mono text-sm cursor-pointer"
                          title="Click to view execution timeline"
                        >
                          <TableCell className="text-zinc-400">{t.trade_id}</TableCell>
                          <TableCell>
                            <span className={`px-1.5 py-0.5 text-[10px] border ${
                              t.source === "manual"
                                ? "border-amber-700 text-amber-300"
                                : "border-zinc-700 text-zinc-400"
                            }`}>
                              {(t.source || "auto").toUpperCase()}
                            </span>
                          </TableCell>
                          <TableCell>
                            <span
                              data-testid={`trade-trigger-${t.trade_id}`}
                              className={`px-1.5 py-0.5 text-[10px] border ${
                                t.trigger_reason === "BOS_STRUCTURE"
                                  ? "border-purple-700 text-purple-300"
                                  : t.trigger_reason === "CONFIDENCE_THRESHOLD"
                                    ? "border-emerald-700 text-emerald-300"
                                    : t.trigger_reason === "MANUAL"
                                      ? "border-amber-700 text-amber-300"
                                      : "border-zinc-700 text-zinc-500"
                              }`}
                            >
                              {({
                                CONFIDENCE_THRESHOLD: "CONFIDENCE",
                                BOS_STRUCTURE: "BOS+STRUCT",
                                MANUAL: "MANUAL",
                              })[t.trigger_reason] || "—"}
                            </span>
                          </TableCell>
                          <TableCell className="text-zinc-400 text-xs" data-testid={`trade-conf-${t.trade_id}`}>
                            {t.confidence != null ? `${t.confidence}%` : "—"}
                          </TableCell>
                          <TableCell>
                            <span className={t.direction === "CALL" ? "text-emerald-300" : "text-red-300"}>
                              {t.direction}
                            </span>
                          </TableCell>
                          <TableCell className="text-amber-300 font-semibold" data-testid={`trade-lots-${t.trade_id}`}>
                            {t.lots ?? Math.floor((t.qty || 0) / 65)}
                          </TableCell>
                          <TableCell className="text-zinc-300">{t.qty}</TableCell>
                          <TableCell className="text-zinc-300">{fmtINR(t.entry_price)}</TableCell>
                          <TableCell className="text-zinc-300">{t.exit_price != null ? fmtINR(t.exit_price) : "—"}</TableCell>
                          <TableCell className={t.pnl == null ? "text-zinc-500" : t.pnl >= 0 ? "text-emerald-300" : "text-red-300"}>
                            {t.pnl != null ? fmtINR(t.pnl) : "open"}
                          </TableCell>
                          <TableCell className="text-zinc-400 text-xs">{t.exit_reason || "—"}</TableCell>
                          <TableCell className="text-zinc-500 text-xs">{fmtDateTime(t.entry_time)}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                )}
              </Card>
            </TabsContent>

            <TabsContent value="transitions" className="mt-4">
              <Card className="border-zinc-800 bg-zinc-950/70 rounded-none overflow-hidden">
                {transitions.length === 0 ? (
                  <div className="p-8 text-center text-zinc-600 font-mono text-sm">No FSM transitions yet.</div>
                ) : (
                  <ul className="divide-y divide-zinc-800">
                    {transitions.map((tr, i) => (
                      <li key={i} className="px-5 py-3 flex items-center gap-4 font-mono text-sm">
                        <span className="text-xs text-zinc-500 w-44">{fmtDateTime(tr.timestamp)}</span>
                        <span className={`px-2 py-0.5 text-xs border ${STATE_COLORS[tr.old_state] || STATE_COLORS.IDLE}`}>
                          {tr.old_state}
                        </span>
                        <span className="text-zinc-600">→</span>
                        <span className={`px-2 py-0.5 text-xs border ${STATE_COLORS[tr.new_state] || STATE_COLORS.IDLE}`}>
                          {tr.new_state}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </Card>
            </TabsContent>
          </Tabs>
        </section>

        <footer className="text-center text-[10px] uppercase tracking-[0.2em] font-mono text-zinc-700 pt-8 pb-2">
          Single-position FSM · 4-table SQLite ledger · ATR stops · drawdown-aware sizing
        </footer>
      </main>

      {/* SIM/PAPER → LIVE confirmation dialog */}
      <AlertDialog open={confirmLive} onOpenChange={setConfirmLive}>
        <AlertDialogContent className="bg-zinc-950 border-red-800 rounded-none font-mono">
          <AlertDialogHeader>
            <AlertDialogTitle className="text-red-300 flex items-center gap-2">
              <ShieldAlertIcon className="h-5 w-5" /> Switch to LIVE mode?
            </AlertDialogTitle>
            <AlertDialogDescription className="text-zinc-400 text-sm leading-relaxed">
              The bot will start placing <span className="text-red-300 font-semibold">real orders</span> on
              NSE/NFO against your Angel One account. It will:
              <ul className="list-disc list-inside mt-3 space-y-1 text-zinc-300">
                <li>read your <span className="text-amber-300">actual net available cash</span> via <code className="text-amber-300">rmsLimit()</code></li>
                <li>size lots based on that real capital</li>
                <li>place real BUY / SELL / STOPLOSS_LIMIT orders</li>
              </ul>
              <span className="block mt-3 text-amber-300">Only proceed during market hours (09:15–15:30 IST) with a verified Angel One session.</span>
              <span className="block mt-2 text-zinc-500">If unsure, choose <span className="text-blue-300 font-semibold">SIM</span> first — it uses live Angel data but simulates orders.</span>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel
              data-testid="cancel-live"
              className="rounded-none border-zinc-700 bg-zinc-900 hover:bg-zinc-800 text-zinc-200 font-mono"
            >
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              data-testid="confirm-live"
              onClick={() => applyTradingMode("live")}
              className="rounded-none bg-red-600 hover:bg-red-500 text-zinc-950 font-mono font-semibold"
            >
              Yes, go LIVE
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Reset history confirmation dialog */}
      <AlertDialog open={confirmReset} onOpenChange={setConfirmReset}>
        <AlertDialogContent className="bg-zinc-950 border-zinc-800 rounded-none font-mono">
          <AlertDialogHeader>
            <AlertDialogTitle className="text-zinc-100 flex items-center gap-2">
              <Trash2Icon className="h-5 w-5 text-amber-400" /> Reset history?
            </AlertDialogTitle>
            <AlertDialogDescription className="text-zinc-400 text-sm leading-relaxed">
              This wipes the <span className="text-amber-300">equity curve</span> for the
              current <span className="text-amber-300">{status?.trading_mode?.toUpperCase()}</span> mode
              and clears <span className="text-amber-300">all closed trades</span>.
              State transitions and indicators are kept.
              <span className="block mt-2 text-zinc-500">Use this after switching modes so drawdown sizing starts fresh.</span>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel className="rounded-none border-zinc-700 bg-zinc-900 hover:bg-zinc-800 text-zinc-200 font-mono">
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              data-testid="confirm-reset"
              onClick={resetHistory}
              className="rounded-none bg-amber-600 hover:bg-amber-500 text-zinc-950 font-mono font-semibold"
            >
              Yes, wipe
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Manual entry confirmation dialog */}
      <AlertDialog open={confirmManual !== null} onOpenChange={(o) => !o && setConfirmManual(null)}>
        <AlertDialogContent className={`bg-zinc-950 rounded-none font-mono ${
          confirmManual === "CALL" ? "border-emerald-800" : "border-red-800"
        }`}>
          <AlertDialogHeader>
            <AlertDialogTitle className={`flex items-center gap-2 ${
              confirmManual === "CALL" ? "text-emerald-300" : "text-red-300"
            }`}>
              {confirmManual === "CALL"
                ? <><ArrowUpRightIcon className="h-5 w-5" /> Buy Call (manual entry)</>
                : <><ArrowDownRightIcon className="h-5 w-5" /> Buy Put (manual entry)</>
              }
            </AlertDialogTitle>
            <AlertDialogDescription className="text-zinc-400 text-sm leading-relaxed">
              {atmLeg ? (
                <div
                  data-testid="confirm-contract-card"
                  className={`mb-3 p-3 border rounded-none ${
                    atmStale ? "border-amber-800 bg-amber-950/20" : "border-zinc-700 bg-zinc-900"
                  }`}
                >
                  <div className="text-xs uppercase tracking-wider text-zinc-500 mb-1">
                    Resolved contract {atmStale && <span className="text-amber-300">(snapshot &gt;20s old)</span>}
                  </div>
                  <div className="text-amber-300 text-lg font-semibold" data-testid="confirm-contract-symbol">
                    {atmLeg.symbol || "—"}
                  </div>
                  <div className="grid grid-cols-2 gap-x-4 gap-y-1 mt-2 text-zinc-300">
                    <div>Strike: <span className="text-zinc-100" data-testid="confirm-strike">{atmLeg.strike ?? "—"}</span></div>
                    <div>Type: <span className="text-zinc-100" data-testid="confirm-option-type">{atmLeg.option_type || "—"}</span></div>
                    <div>Expiry: <span className="text-zinc-100" data-testid="confirm-expiry">{atmLeg.expiry || "—"}</span></div>
                    <div>Token: <span className="text-zinc-500 text-xs" data-testid="confirm-token">{atmLeg.token || "—"}</span></div>
                    <div className="col-span-2 mt-1">
                      Premium: <span className="text-amber-300 font-semibold" data-testid="confirm-premium">
                        {atmLeg.ltp != null ? `₹${Number(atmLeg.ltp).toFixed(2)}` : "—"}
                      </span>
                      {atm.spot && <span className="text-zinc-500 text-xs ml-2">(spot ₹{Number(atm.spot).toFixed(2)})</span>}
                    </div>
                  </div>
                </div>
              ) : (
                <div className="mb-3 p-3 border border-red-800 bg-red-950/20 text-red-300 text-sm" data-testid="confirm-contract-missing">
                  No ATM snapshot available yet — the bot may still be booting. Please retry in a few seconds.
                </div>
              )}
              The bot will refresh the strike again at execution time and buy
              the newest Near-OTM {confirmManual} contract. At execution the
              actual fill price becomes the entry price, SL/TP recalc from it.
              {status?.trading_mode === "live"
                ? <span className="block mt-1 text-red-300">Mode is LIVE — this places a REAL order on NSE/NFO.</span>
                : <span className="block mt-1 text-blue-300">Mode is SIM — order is simulated at the WS/REST premium above.</span>
              }
              <ul className="list-disc list-inside mt-3 space-y-1 text-zinc-300">
                <li>Engine: <span className="text-amber-300 uppercase">{engine}</span> (drives the SL/TP/Trail policy)</li>
                <li>Lots: <span className="text-amber-300">{executionLots}</span> (fixed by capital · SIM ₹{Math.round(status?.broker_capital?.value || 0).toLocaleString("en-IN")})</li>
                <li>Stop Loss: <span className="text-red-300">{status?.manual_sl_pct ?? 15}%</span> of fill price</li>
                <li>Target: <span className="text-emerald-300">{status?.manual_tp_pct ?? 30}%</span> of fill price</li>
                <li>Trailing step: <span className="text-amber-300">{status?.trail_step_pct ?? 10}%</span></li>
                <li>Single-position lock + cooldown after exit</li>
              </ul>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel
              data-testid="cancel-manual"
              className="rounded-none border-zinc-700 bg-zinc-900 hover:bg-zinc-800 text-zinc-200 font-mono"
            >
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              data-testid="confirm-manual"
              onClick={() => placeManualEntry(confirmManual)}
              className={`rounded-none font-mono font-semibold text-zinc-950 ${
                confirmManual === "CALL"
                  ? "bg-emerald-600 hover:bg-emerald-500"
                  : "bg-red-600 hover:bg-red-500"
              }`}
            >
              Confirm {confirmManual}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* v1.10 — Execution Timeline modal (opens on trade-row click) */}
      <AlertDialog open={!!timelineTrade} onOpenChange={(o) => !o && setTimelineTrade(null)}>
        <AlertDialogContent className="bg-zinc-950 border-zinc-800 rounded-none max-w-3xl max-h-[85vh] overflow-y-auto">
          <AlertDialogHeader>
            <AlertDialogTitle className="text-amber-300 font-mono">
              Execution Timeline · {timelineTrade?.trade_id}
            </AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="text-zinc-400 text-sm space-y-4">
                {/* Summary card */}
                {timelineTrade && (
                  <div className="grid grid-cols-3 gap-3 border border-zinc-800 p-3 bg-zinc-900/40 font-mono text-[11px]">
                    <div>Direction: <span className={timelineTrade.direction === "CALL" ? "text-emerald-300" : "text-red-300"}>{timelineTrade.direction}</span></div>
                    <div>Entry: <span className="text-zinc-100">{fmtINR(timelineTrade.entry_price)}</span></div>
                    <div>Exit: <span className="text-zinc-100">{timelineTrade.exit_price != null ? fmtINR(timelineTrade.exit_price) : "—"}</span></div>
                    <div>PnL: <span className={timelineTrade.pnl >= 0 ? "text-emerald-300" : "text-red-300"}>{timelineTrade.pnl != null ? fmtINR(timelineTrade.pnl) : "open"}</span></div>
                    <div>Exit Reason: <span className="text-amber-300">{timelineTrade.exit_reason || "—"}</span></div>
                    <div>Source: <span className="text-zinc-100">{timelineTrade.source || "—"}</span></div>
                    <div className="col-span-3">Contract: <span className="text-amber-300">{timelineTrade.contract_symbol || "—"}</span> {timelineTrade.strike && <span className="text-zinc-500">· strike {timelineTrade.strike}</span>} {timelineTrade.expiry && <span className="text-zinc-500">· exp {timelineTrade.expiry}</span>}</div>
                  </div>
                )}

                {/* Health snapshot */}
                {timelineData?.health && (
                  <div className="border border-zinc-800 p-3 bg-zinc-900/40 font-mono text-[11px]">
                    <div className="text-zinc-500 uppercase tracking-wider text-[10px] mb-1">Execution Health (now)</div>
                    <div className="grid grid-cols-2 gap-1">
                      <div>WS: <span className={timelineData.health.ws?.connected ? "text-emerald-300" : "text-red-300"}>{timelineData.health.ws?.connected ? "connected" : "down"}</span> {timelineData.health.ws?.subscribed_count != null && <span className="text-zinc-500">({timelineData.health.ws.subscribed_count} subs)</span>}</div>
                      <div>Broker: <span className={timelineData.health.broker === "connected" ? "text-emerald-300" : "text-red-300"}>{timelineData.health.broker || "—"}</span></div>
                      <div>Last tick: <span className="text-zinc-300">{timelineData.health.ws?.seconds_since_last_tick != null ? `${timelineData.health.ws.seconds_since_last_tick.toFixed(0)}s ago` : "—"}</span></div>
                      <div>WS reconnects: <span className="text-zinc-300">{timelineData.health.ws?.reconnect_failures ?? 0}</span></div>
                    </div>
                  </div>
                )}

                {/* Timeline */}
                {timelineData == null ? (
                  <div className="text-zinc-500 text-center py-6">Loading timeline…</div>
                ) : (timelineData.events || []).length === 0 ? (
                  <div className="text-zinc-500 text-center py-6 border border-zinc-800 bg-zinc-900/40">
                    {timelineData.note || "No events recorded."}
                  </div>
                ) : (
                  <ol data-testid="timeline-events" className="relative border-l border-zinc-800 ml-2 space-y-3 pt-2">
                    {timelineData.events.map((e) => {
                      const ico = e.event_type.startsWith("ENTRY") || e.event_type.includes("CONTRACT") || e.event_type === "ORDER_SUBMIT" || e.event_type === "ORDER_ACK" || e.event_type === "REST_LTP" || e.event_type === "ATM_REFRESH" ? "🟢"
                        : e.event_type.includes("SL") || e.event_type.includes("TP") || e.event_type.includes("PROTECTION") ? "🛡"
                        : e.event_type.includes("TRAIL") || e.event_type === "STOP_MODIFIED" ? "📈"
                        : e.event_type === "STALE_FEED" || e.event_type === "FORCED_EXIT" ? "⚠"
                        : e.event_type.startsWith("EXIT") ? "🔴" : "·";
                      return (
                        <li key={e.id} data-testid={`tl-${e.event_type}`} className="ml-4 pl-3 relative">
                          <span className="absolute -left-[9px] top-1 w-3 h-3 flex items-center justify-center text-[10px]">{ico}</span>
                          <div className="text-[10px] text-zinc-500 font-mono">{new Date(e.ts).toLocaleTimeString("en-IN", { hour12: false })}</div>
                          <div className="text-zinc-200 text-[12px] font-mono">{e.message}</div>
                          {e.payload && Object.keys(e.payload).length > 0 && (
                            <div className="text-[10px] text-zinc-500 font-mono mt-0.5 flex flex-wrap gap-x-3 gap-y-0.5">
                              {Object.entries(e.payload).map(([k, v]) => (
                                <span key={k}>{k}: <span className="text-zinc-300">{String(v)}</span></span>
                              ))}
                            </div>
                          )}
                        </li>
                      );
                    })}
                  </ol>
                )}
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel
              data-testid="timeline-close"
              className="rounded-none border-zinc-700 text-zinc-300 hover:bg-zinc-900 font-mono"
            >
              Close
            </AlertDialogCancel>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

export default App;
