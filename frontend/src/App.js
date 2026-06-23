import { useEffect, useState, useCallback } from "react";
import axios from "axios";
import { motion } from "framer-motion";
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid,
} from "recharts";
import {
  ActivityIcon, PowerIcon, PauseIcon, RotateCwIcon, ShieldAlertIcon,
  TrendingUpIcon, TrendingDownIcon, CircleDotIcon, DatabaseIcon,
} from "lucide-react";
import { Card } from "./components/ui/card";
import { Button } from "./components/ui/button";
import { Badge } from "./components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "./components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "./components/ui/tabs";
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

function fmtINR(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const sign = n < 0 ? "-" : "";
  const v = Math.abs(n).toLocaleString("en-IN", { maximumFractionDigits: 2 });
  return `${sign}₹${v}`;
}

function fmtTime(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleTimeString("en-IN", { hour12: false });
  } catch {
    return iso;
  }
}

function fmtDateTime(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("en-IN", { hour12: false });
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
  const [trades, setTrades] = useState([]);
  const [equity, setEquity] = useState([]);
  const [transitions, setTransitions] = useState([]);
  const [busy, setBusy] = useState(false);
  const [lastUpdate, setLastUpdate] = useState(null);

  const fetchAll = useCallback(async () => {
    try {
      const [s, t, e, tr] = await Promise.all([
        axios.get(`${API}/bot/status`),
        axios.get(`${API}/bot/trades?limit=50`),
        axios.get(`${API}/bot/equity?limit=200`),
        axios.get(`${API}/bot/transitions?limit=30`),
      ]);
      setStatus(s.data);
      setTrades(t.data);
      setEquity(e.data);
      setTransitions(tr.data);
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

  const fsm = status?.fsm_state || "IDLE";
  const sup = status?.supervisor_state || "UNKNOWN";
  const realized = status?.realized_pnl_today ?? 0;
  const eqSnap = status?.equity_snapshot;

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
        <div className="max-w-7xl mx-auto px-6 py-4 flex items-center justify-between">
          <div className="flex items-center gap-4">
            <div className="h-9 w-9 grid place-items-center bg-amber-500/10 border border-amber-500/30 rounded-none">
              <ActivityIcon className="h-4 w-4 text-amber-400" />
            </div>
            <div>
              <div className="text-xs uppercase tracking-[0.22em] text-zinc-500 font-mono">Nifty Options Bot</div>
              <h1 className="text-lg font-mono font-semibold text-zinc-100">SmartAPI · ATM Weekly · Lot 65</h1>
            </div>
          </div>

          <div className="flex items-center gap-3">
            <Badge
              data-testid="badge-mode"
              variant="outline"
              className={`font-mono rounded-none border ${status === null ? "border-zinc-700 text-zinc-500 bg-zinc-900" : status.paper_mode ? "border-amber-600/60 text-amber-300 bg-amber-950/40" : "border-red-600/60 text-red-300 bg-red-950/40"}`}
            >
              {status === null ? "—" : status.paper_mode ? "PAPER" : "LIVE"}
            </Badge>
            <Badge
              data-testid="badge-supervisor"
              variant="outline"
              className={`font-mono rounded-none border-zinc-700 bg-zinc-900 ${SUP_COLORS[sup]}`}
            >
              <CircleDotIcon className="h-3 w-3 mr-1.5" />
              {sup}
            </Badge>
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
                  <div className="text-zinc-100">{eqSnap?.effective_lots ?? "—"}</div>
                </div>
                <div>
                  <div className="text-[10px] uppercase tracking-wider text-zinc-500">Equity</div>
                  <div className="text-zinc-100">{eqSnap ? fmtINR(eqSnap.current_equity) : "—"}</div>
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
            <div className="mt-5 text-[11px] font-mono text-zinc-500 leading-relaxed border-t border-zinc-800 pt-4">
              <div className="flex items-center gap-2 text-amber-400 mb-2">
                <ShieldAlertIcon className="h-3.5 w-3.5" /> Mode: {status?.paper_mode ? "Paper (safe)" : "LIVE — real orders"}
              </div>
              <div className="flex items-center gap-2 text-zinc-500">
                <DatabaseIcon className="h-3.5 w-3.5" /> {status?.db_path}
              </div>
            </div>
          </Card>
        </section>

        {/* Stats row */}
        <section className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          <StatCard
            label="Realized PnL (today)"
            value={fmtINR(realized)}
            icon={realized >= 0 ? TrendingUpIcon : TrendingDownIcon}
            tone={realized > 0 ? "pos" : realized < 0 ? "neg" : "default"}
          />
          <StatCard
            label="Trades today"
            value={status?.trades_today ?? "—"}
            sub="cap: 4 / day"
            icon={ActivityIcon}
            tone={status?.trades_today >= 4 ? "warn" : "default"}
          />
          <StatCard
            label="Effective lots"
            value={eqSnap?.effective_lots ?? "—"}
            sub="lot size: 65"
            tone="default"
          />
          <StatCard
            label="Drawdown"
            value={eqSnap ? `${(eqSnap.drawdown_pct * 100).toFixed(2)}%` : "—"}
            sub={eqSnap?.drawdown_pct >= 0.1 ? "sizing scaled down" : "no scaling"}
            tone={(eqSnap?.drawdown_pct ?? 0) >= 0.1 ? "warn" : "default"}
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
                      tickFormatter={(v) => new Date(v).toLocaleDateString("en-IN", { day: "2-digit", month: "short" })}
                      stroke="#52525b"
                      style={{ fontSize: 11, fontFamily: "monospace" }}
                    />
                    <YAxis stroke="#52525b" style={{ fontSize: 11, fontFamily: "monospace" }} />
                    <Tooltip
                      contentStyle={{ background: "#0a0a0a", border: "1px solid #27272a", fontFamily: "monospace", fontSize: 12 }}
                      labelFormatter={(v) => new Date(v).toLocaleString("en-IN")}
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
                        <TableHead className="font-mono text-[10px] uppercase tracking-wider text-zinc-500">Dir</TableHead>
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
                        <TableRow key={t.trade_id} className="border-zinc-800 hover:bg-zinc-900/40 font-mono text-sm">
                          <TableCell className="text-zinc-400">{t.trade_id}</TableCell>
                          <TableCell>
                            <span className={t.direction === "CALL" ? "text-emerald-300" : "text-red-300"}>
                              {t.direction}
                            </span>
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
    </div>
  );
}

export default App;
