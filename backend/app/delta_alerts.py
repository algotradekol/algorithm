"""Best-effort Telegram alerts for Delta lifecycle events."""
from __future__ import annotations

import datetime
import os
from typing import Iterable

import requests

from .delta_log import delta_log
from .delta_reporting import inr_rate


def _truthy(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _chat_ids() -> list[str]:
    raw = os.getenv("TELEGRAM_CHAT_IDS") or os.getenv("TELEGRAM_CHAT_ID") or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


def telegram_enabled() -> bool:
    return (
        _truthy(os.getenv("TELEGRAM_ALERTS_ENABLED"), True)
        and bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip())
        and bool(_chat_ids())
    )


def _trade_alert_mode_allowed(position: dict, context: dict | None = None) -> bool:
    raw = os.getenv("TELEGRAM_ALERT_MODES", "live")
    allowed = {item.strip().lower() for item in raw.split(",") if item.strip()}
    if "all" in allowed:
        return True
    snapshot = position.get("signal_snapshot") or {}
    context = context or {}
    mode = str(context.get("mode") or snapshot.get("execution") or position.get("execution") or "paper").lower()
    return mode in allowed


def _fmt_number(value, digits=2) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "--"
    text = f"{number:,.{digits}f}"
    return text.rstrip("0").rstrip(".")


def _fmt_time(value=None) -> str:
    try:
        if value is None:
            dt = datetime.datetime.now(datetime.timezone.utc)
        elif isinstance(value, datetime.datetime):
            dt = value
        else:
            dt = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone(datetime.timedelta(hours=5, minutes=30))).strftime("%d/%m/%Y %H:%M:%S IST")
    except (TypeError, ValueError):
        return str(value or "--")


def send_telegram_alert(message: str, *, event: str = "telegram_alert") -> None:
    if not telegram_enabled():
        return
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chat_id in _chat_ids():
        try:
            response = requests.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": message,
                    "disable_web_page_preview": True,
                },
                timeout=(4, 8),
            )
            if response.status_code != 200:
                delta_log(event, status="failed", chat_id=chat_id, http_status=response.status_code, response=response.text[:300])
        except requests.RequestException as exc:
            delta_log(event, status="failed", chat_id=chat_id, error=str(exc))


def _context_lines(position: dict, context: dict | None = None) -> list[str]:
    snapshot = position.get("signal_snapshot") or {}
    settings = snapshot.get("settings") if isinstance(snapshot.get("settings"), dict) else {}
    context = context or {}
    asset = str(context.get("asset") or snapshot.get("asset") or "gold").title()
    mode = str(context.get("mode") or snapshot.get("execution") or position.get("execution") or "paper").upper()
    minutes = context.get("minutes") or snapshot.get("timeframe") or ""
    timeframe = f"{minutes} min" if isinstance(minutes, int) else str(minutes).replace("m", " min")
    return [
        f"Mode: {mode}",
        f"Asset: {asset}",
        f"Timeframe: {timeframe or '--'}",
        f"Symbol: {position.get('symbol') or snapshot.get('symbol') or '--'}",
        f"Side: {position.get('side') or '--'}",
        f"Lots: {_fmt_number(position.get('qty'), 0)}",
        f"Entry: {_fmt_number(position.get('entry_price'))}",
        f"SL: {_fmt_number(position.get('sl_price'))}",
        f"Target: {_fmt_number(position.get('target_price'))}",
        f"Exit mode: {snapshot.get('silver_exit_policy') or settings.get('exit_mode') or '--'}",
    ]


def alert_trade_open(position: dict, context: dict | None = None) -> None:
    if not _trade_alert_mode_allowed(position, context):
        return
    lines = ["Delta trade opened", *_context_lines(position, context), f"Time: {_fmt_time(position.get('entry_time'))}"]
    send_telegram_alert("\n".join(lines), event="telegram_trade_open_alert")


def alert_trade_close(trade: dict, context: dict | None = None) -> None:
    if not _trade_alert_mode_allowed(trade, context):
        return
    rate = inr_rate("india", trade.get("quote_currency") or "USD")
    net = trade.get("net_pnl")
    net_inr = float(net) * rate if net is not None and rate else None
    lines = [
        "Delta trade exited",
        *_context_lines(trade, context),
        f"Exit: {_fmt_number(trade.get('exit_price'))}",
        f"Reason: {trade.get('exit_reason') or '--'}",
        f"Gross P&L: {_fmt_number(trade.get('gross_pnl'), 4)} USD",
        f"Net P&L: {_fmt_number(net, 4)} USD",
        f"Net INR: {_fmt_number(net_inr, 2)}",
        f"Entry time: {_fmt_time(trade.get('entry_time'))}",
        f"Exit time: {_fmt_time(trade.get('exit_time'))}",
    ]
    send_telegram_alert("\n".join(lines), event="telegram_trade_close_alert")


def alert_wallet_transaction(transaction: dict, *, asset: str = "gold") -> None:
    kind = str(transaction.get("transaction_type") or "wallet").replace("_", " ").title()
    amount = transaction.get("amount_usd")
    rate = inr_rate("india", transaction.get("asset_symbol") or "USD")
    amount_inr = float(amount) * rate if amount is not None and rate else None
    lines = [
        f"Delta wallet {kind}",
        f"Asset: {asset.title()}",
        f"Amount: {_fmt_number(amount, 4)} {transaction.get('asset_symbol') or 'USD'}",
        f"Approx INR: {_fmt_number(amount_inr, 2)}",
        f"Balance after: {_fmt_number(transaction.get('balance_after_usd'), 4)} USD",
        f"Reference: {transaction.get('reference') or transaction.get('id') or '--'}",
        f"Time: {_fmt_time(transaction.get('created_at'))}",
    ]
    send_telegram_alert("\n".join(lines), event="telegram_wallet_alert")


def seed_seen_transactions(rows: Iterable[dict]) -> set[str]:
    return {str(row.get("id")) for row in rows if row.get("id") is not None}
