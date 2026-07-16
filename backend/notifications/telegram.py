"""
notifications/telegram.py
=========================
Telegram push-notification module for the Nifty Options bot.

Contract:
    • This module NEVER touches trading logic.
    • Alerts are advisory only, monitoring the SMC engine.
    • Any Telegram failure is swallowed — the bot continues normally.

Dedup rule (per user spec):
    Send an alert only when the CURRENT SMC signal differs from the
    LAST SENT alert. "Different" means either
        (a) direction changed (CALL ↔ PUT) OR
        (b) confidence value changed
    Signals below the threshold are ignored entirely.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)

TELEGRAM_TIMEOUT_SEC = 5


class TelegramNotifier:
    """Lightweight, side-effect-free-on-failure Telegram client."""

    API_BASE = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self) -> None:
        self.enabled: bool = (
            config.TELEGRAM_ENABLED
            and bool(config.TELEGRAM_BOT_TOKEN)
            and bool(config.TELEGRAM_CHAT_ID)
        )
        self.threshold: int = max(0, min(100, config.SMC_ALERT_THRESHOLD))
        # State — remembered ONLY in memory so a bot restart resets dedup.
        self._last_alert: Optional[dict] = None
        self._startup_sent: bool = False

        if not self.enabled:
            logger.info(
                "Telegram notifications DISABLED "
                "(TELEGRAM_ENABLED=%s, token=%s, chat=%s)",
                config.TELEGRAM_ENABLED,
                "set" if config.TELEGRAM_BOT_TOKEN else "empty",
                "set" if config.TELEGRAM_CHAT_ID else "empty",
            )
        else:
            logger.info(
                "Telegram notifications ENABLED  threshold=%d %%", self.threshold
            )

    # ─────────────────────────────────────────────── low-level send
    def _send(self, text: str) -> bool:
        """POST to Telegram Bot API. Returns True on success. Swallows all
        exceptions — trading loop must never see them."""
        if not self.enabled:
            return False
        try:
            url = self.API_BASE.format(token=config.TELEGRAM_BOT_TOKEN)
            resp = requests.post(
                url,
                json={
                    "chat_id": config.TELEGRAM_CHAT_ID,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=TELEGRAM_TIMEOUT_SEC,
            )
            if resp.status_code != 200:
                logger.warning(
                    "Telegram send failed  status=%d  body=%s",
                    resp.status_code, resp.text[:200],
                )
                return False
            return True
        except Exception:
            logger.warning("Telegram send raised — continuing", exc_info=True)
            return False

    # ─────────────────────────────────────────────── one-off startup ping
    def send_startup(self) -> None:
        """Fired once when the bot boots (only if Telegram is configured)."""
        if not self.enabled or self._startup_sent:
            return
        now_ist = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime(
            "%H:%M IST · %d %b %Y"
        )
        text = (
            "✅ <b>Nifty Trading Bot Started</b>\n\n"
            "Telegram connection successful.\n"
            "Monitoring SMC Engine...\n\n"
            f"<i>Time: {now_ist}</i>"
        )
        if self._send(text):
            self._startup_sent = True
            logger.info("Telegram startup ping delivered.")

    # ─────────────────────────────────────────────── main alert path
    def maybe_notify_smc(self, smc_payload: dict) -> None:
        """Called after every SMC scoring tick. Applies threshold + dedup
        and sends a formatted alert only when both are satisfied."""
        if not self.enabled:
            return
        direction = str(smc_payload.get("direction") or "")
        try:
            confidence = int(smc_payload.get("confidence") or 0)
        except (TypeError, ValueError):
            return

        # Rule 1: threshold gate
        if confidence < self.threshold:
            return
        # Rule 2: direction must be an actionable side (not NEUTRAL / OFF)
        if direction not in {"CALL", "PUT"}:
            return
        # Rule 3: dedup against last sent alert
        last = self._last_alert
        if last and last.get("direction") == direction and last.get("confidence") == confidence:
            return

        text = self._format(smc_payload, direction, confidence)
        if self._send(text):
            self._last_alert = {"direction": direction, "confidence": confidence}
            logger.info(
                "Telegram SMC alert sent  %s %d%%  grade=%s",
                direction, confidence, smc_payload.get("grade"),
            )

    # ─────────────────────────────────────────────── message formatter
    @staticmethod
    def _format(payload: dict, direction: str, confidence: int) -> str:
        side = "BUY CALL" if direction == "CALL" else "BUY PUT"
        htf_raw = payload.get("htf_trend")
        htf = {"CALL": "Bullish", "PUT": "Bearish"}.get(htf_raw, htf_raw or "—")
        struct_raw = payload.get("market_structure")
        struct = {"CALL": "Bullish HH+HL", "PUT": "Bearish LH+LL"}.get(
            struct_raw, struct_raw or "—"
        )
        grade = payload.get("grade") or "—"
        regime = payload.get("regime") or "—"
        reasons_list = payload.get("reasons") or []
        # Limit to first 5 reasons — keeps the message readable
        reasons_block = (
            "\n".join(f"• {r}" for r in reasons_list[:5]) if reasons_list else "—"
        )
        ts = payload.get("timestamp") or ""
        entry = payload.get("entry")
        stop = payload.get("stop_loss")
        target = payload.get("target")

        levels = ""
        if entry is not None:
            levels = (
                f"\n\n<b>Entry:</b> ₹{entry}"
                f"\n<b>Stop Loss:</b> ₹{stop}"
                f"\n<b>Target:</b> ₹{target}"
            )
        return (
            "🚨 <b>SMC SIGNAL</b>\n\n"
            f"<b>Direction:</b> {side}\n"
            f"<b>Confidence:</b> {confidence}%\n"
            f"<b>Trade Grade:</b> {grade}\n"
            f"<b>Regime:</b> {regime}\n\n"
            f"<b>HTF Trend:</b> {htf}\n"
            f"<b>Market Structure:</b> {struct}"
            f"{levels}\n\n"
            f"<b>Reasons:</b>\n{reasons_block}\n\n"
            f"<i>Time: {ts} IST</i>"
        )


    # ─── v2.3 BOS + Structure informational alert ───────────────────
    def send_bos_structure_signal(self, payload: dict) -> None:
        """v2.3 Phase 2 — fires the instant BOS+Structure aligns
        (Bullish BOS + HH+HL → CALL, Bearish BOS + LH+LL → PUT).
        IGNORES confidence threshold — the whole point of this alert.
        Caller is responsible for candle-level dedup (see main.py)."""
        if not self.enabled:
            return
        direction = str(payload.get("direction") or "")
        confidence = int(payload.get("confidence") or 0)
        side = "BUY CALL" if direction == "CALL" else "BUY PUT"
        htf_raw = payload.get("htf_trend")
        htf = {"CALL": "Bullish", "PUT": "Bearish"}.get(htf_raw, htf_raw or "—")
        struct_raw = payload.get("market_structure")
        struct = {"CALL": "Bullish HH+HL", "PUT": "Bearish LH+LL"}.get(
            struct_raw, struct_raw or "—"
        )
        bos_raw = payload.get("bos")
        bos = {"CALL": "Bullish", "PUT": "Bearish"}.get(bos_raw, bos_raw or "—")
        ts = payload.get("timestamp") or ""
        text = (
            "⚡ <b>BOS + STRUCTURE</b>\n\n"
            f"<b>Direction:</b> {side}\n"
            f"<b>Trigger:</b> BOS + Structure Rule\n"
            f"<b>Confidence:</b> {confidence}%\n\n"
            f"<b>BOS:</b> {bos}\n"
            f"<b>Structure:</b> {struct}\n"
            f"<b>HTF Trend:</b> {htf}\n\n"
            f"<i>Time: {ts} IST</i>"
        )
        try:
            self._send(text)
        except Exception:
            pass


    # ─── v2.4 — Daily Loss + EOD Summary ────────────────────────────
    def send_daily_loss_hit(self, payload: dict) -> None:
        """Suspension alert — realized daily loss ≥ configured cap."""
        if not self.enabled:
            return
        cap_rs = float(payload.get("capital") or 0.0)
        risk = float(payload.get("risk_pct") or 0.0)
        max_loss = float(payload.get("max_loss") or 0.0)
        realized = float(payload.get("realized_pnl") or 0.0)
        text = (
            "🛑 <b>AUTO SUSPENDED</b>\n\n"
            "<b>Daily Loss Limit Reached</b>\n\n"
            f"<b>Capital:</b> ₹{cap_rs:,.0f}\n"
            f"<b>Risk:</b> {risk:.2f}%\n"
            f"<b>Maximum Loss:</b> ₹{max_loss:,.0f}\n"
            f"<b>Today's Loss:</b> ₹{realized:,.0f}\n\n"
            "<i>Trading resumes tomorrow, or press Resume.</i>"
        )
        try:
            self._send(text)
        except Exception:
            pass

    def send_eod_summary(self, summary: dict) -> None:
        """One-shot end-of-day recap sent after 15:10 IST square-off."""
        if not self.enabled:
            return
        date = summary.get("date") or ""
        trades = int(summary.get("trades") or 0)
        wins = int(summary.get("wins") or 0)
        losses = int(summary.get("losses") or 0)
        win_rate = float(summary.get("win_rate_pct") or 0.0)
        pnl = float(summary.get("realized_pnl") or 0.0)
        auto_sus = "Yes" if summary.get("auto_suspended") else "No"
        max_hit = "Yes" if summary.get("max_daily_loss_hit") else "No"

        lines = [
            "📊 <b>Daily Trading Summary</b>\n",
            f"<b>Date:</b> {date}\n",
            f"<b>Trades:</b> {trades}",
            f"<b>Wins:</b> {wins}",
            f"<b>Losses:</b> {losses}",
            f"<b>Win Rate:</b> {win_rate:.1f}%",
            f"<b>Realized PnL:</b> ₹{pnl:,.0f}\n",
        ]
        if summary.get("per_trigger"):
            lines.append("<b>Trigger Breakdown</b>")
            for t in summary["per_trigger"]:
                label = {
                    "CONFIDENCE_THRESHOLD": "CONFIDENCE",
                    "BOS_STRUCTURE": "BOS+STRUCTURE",
                    "MANUAL": "MANUAL",
                }.get(t.get("trigger"), t.get("trigger") or "UNKNOWN")
                lines.append(
                    f"• <b>{label}</b> — Trades: {t['trades']} · "
                    f"PnL: ₹{t['net_pnl']:,.0f}"
                )
            lines.append("")
        lines.append(f"<b>AUTO Suspended:</b> {auto_sus}")
        lines.append(f"<b>Maximum Daily Loss Hit:</b> {max_hit}")
        try:
            self._send("\n".join(lines))
        except Exception:
            pass

    # ─── v1.15 auto-trade notifications ─────────────────────────────
    def send_mode_change(self, new_mode: str, lots: int, threshold: int) -> None:
        if not self.enabled:
            return
        icon = "🟢" if new_mode.upper() == "AUTO" else "⚪"
        text = (
            f"{icon} <b>{new_mode.upper()} MODE</b>\n\n"
            f"<b>Threshold:</b> {threshold}%\n"
            f"<b>Lots:</b> {lots}"
        )
        try:
            self._send(text)
        except Exception:
            pass

    def send_auto_entry(self, direction: str, confidence: int, reasons: list,
                        ok: bool, msg: str) -> None:
        if not self.enabled:
            return
        icon = "🚀" if ok else "⚠️"
        head = "AUTO BUY " + ("CALL" if direction == "CALL" else "PUT")
        reasons_block = "\n".join(f"• {r}" for r in reasons[:4]) if reasons else "—"
        status = "" if ok else f"\n<b>Result:</b> {msg}"
        text = (
            f"{icon} <b>{head}</b>\n\n"
            f"<b>Confidence:</b> {confidence}%\n"
            f"<b>Reasons:</b>\n{reasons_block}"
            f"{status}"
        )
        try:
            self._send(text)
        except Exception:
            pass

    def send_auto_suspended(self, reason: str) -> None:
        if not self.enabled:
            return
        text = (
            "🛑 <b>AUTO SUSPENDED</b>\n\n"
            f"<b>Reason:</b> {reason}\n\n"
            "Auto entries paused. Resume from dashboard when safe."
        )
        try:
            self._send(text)
        except Exception:
            pass

    def send_auto_cancelled(self, reason: str, calc: dict) -> None:
        if not self.enabled:
            return
        text = (
            "⚠️ <b>AUTO ENTRY CANCELLED</b>\n\n"
            f"<b>Reason:</b> {reason}\n"
            f"<b>Capital:</b> ₹{calc.get('capital', 0):,.0f}\n"
            f"<b>Calculated Lots:</b> {calc.get('calculated_lots', '—')}\n"
            f"<b>Max Lots:</b> {calc.get('max_lots', '—')}"
        )
        try:
            self._send(text)
        except Exception:
            pass

    def send_auto_preflight_failed(
        self,
        direction: str,
        confidence: int,
        reasons: list,
        ctx: dict,
        contract_symbol: str = "",
        lots: int = 1,
    ) -> None:
        """v2.0.1 Bug 4 — clean human-readable pre-flight failure alert.

        `ctx` is `NiftyOptionsBot._last_reject_context` populated by
        `_place_entry`. Never dump raw JSON to Telegram — that stays in the
        DEBUG logs. The alert is one screen-ful of glanceable text.
        """
        if not self.enabled:
            return
        icon = "⚠️"
        head = f"AUTO BUY {'CALL' if direction == 'CALL' else 'PUT'}"
        broker_reason = (ctx or {}).get("broker_reason") or "insufficient_funds"
        user_msg = (ctx or {}).get("user_message") or ""
        # Try to pretty-print pre-flight numbers if present.
        avail = (ctx or {}).get("available")
        req = (ctx or {}).get("required")
        detail_lines = [f"<b>Reason:</b> {broker_reason.replace('_', ' ').title()}"]
        if avail is not None:
            detail_lines.append(f"<b>Available:</b> ₹{avail:,.2f}")
        if req is not None:
            detail_lines.append(f"<b>Required:</b> ₹{req:,.2f}")
        if contract_symbol:
            detail_lines.append(f"<b>Contract:</b> {contract_symbol}")
        detail_lines.append(f"<b>Lots:</b> {lots}")
        text = (
            f"{icon} <b>{head}</b>\n"
            f"<b>Confidence:</b> {confidence}%\n\n"
            f"<b>Pre-flight Failed</b>\n"
            + "\n".join(detail_lines)
            + "\n\nOrder NOT submitted. Auto trading suspended."
        )
        try:
            self._send(text)
        except Exception:
            pass


