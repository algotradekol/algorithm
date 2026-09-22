"""Best-effort Telegram alerts for Delta lifecycle events."""
from __future__ import annotations

import datetime
import html
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
    if digits <= 0:
        return f"{number:,.0f}"
    text = f"{number:,.{digits}f}"
    return text.rstrip("0").rstrip(".")


def _esc(value) -> str:
    return html.escape(str(value if value is not None else "--"), quote=False)


def _code(value) -> str:
    return f"<code>{_esc(value)}</code>"


def _bold(value) -> str:
    return f"<b>{_esc(value)}</b>"


def _underline(value) -> str:
    return f"<u>{_esc(value)}</u>"


def _pre(lines: Iterable[str]) -> str:
    return f"<pre>{_esc(chr(10).join(lines))}</pre>"


def _detail_rows(rows: Iterable[tuple[str, object]], label_width: int = 11) -> str:
    return _pre(f"{label:<{label_width}} {value}" for label, value in rows)


def _money_line(label: str, value, suffix: str = "") -> str:
    return f"<b>{_esc(label)}:</b> {_code(f'{_fmt_number(value, 4)} {suffix}'.strip())}"


def _money_text(value, suffix: str = "", digits: int = 4) -> str:
    return f"{_fmt_number(value, digits)} {suffix}".strip()


def _side_marker(side: str | None) -> str:
    side = str(side or "").upper()
    if side == "BUY":
        return "[BUY]"
    if side == "SELL":
        return "[SELL]"
    return "[TRADE]"


def _pnl_marker(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "[P&L]"
    if number > 0:
        return "[PROFIT]"
    if number < 0:
        return "[LOSS]"
    return "[FLAT]"


def _exit_reason_label(reason: str | None) -> str:
    value = str(reason or "--").replace("_", " ").title()
    if value == "Trailing Sl":
        return "Trailing SL"
    if value == "Sl":
        return "SL"
    return value


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


def send_telegram_alert(message: str, *, event: str = "telegram_alert", parse_mode: str | None = None) -> list[dict]:
    if not telegram_enabled():
        return []
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    results: list[dict] = []
    for chat_id in _chat_ids():
        try:
            payload = {
                "chat_id": chat_id,
                "text": message,
                "disable_web_page_preview": True,
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode
            response = requests.post(
                url,
                json=payload,
                timeout=(4, 8),
            )
            if response.status_code != 200:
                detail = response.text[:300]
                try:
                    response_json = response.json()
                except ValueError:
                    response_json = {}
                migrate_to = (response_json.get("parameters") or {}).get("migrate_to_chat_id")
                if migrate_to:
                    migrated_payload = {**payload, "chat_id": str(migrate_to)}
                    migrated = requests.post(
                        url,
                        json=migrated_payload,
                        timeout=(4, 8),
                    )
                    if migrated.status_code == 200:
                        results.append({
                            "chat_id": str(migrate_to),
                            "ok": True,
                            "http_status": migrated.status_code,
                            "migrated_from": chat_id,
                        })
                        delta_log(event, status="sent_after_chat_migration", chat_id=str(migrate_to), migrated_from=chat_id)
                        continue
                    results.append({
                        "chat_id": str(migrate_to),
                        "ok": False,
                        "http_status": migrated.status_code,
                        "response": migrated.text[:300],
                        "migrated_from": chat_id,
                    })
                    delta_log(
                        event,
                        status="failed_after_chat_migration",
                        chat_id=str(migrate_to),
                        migrated_from=chat_id,
                        http_status=migrated.status_code,
                        response=migrated.text[:300],
                    )
                    continue
                results.append({"chat_id": chat_id, "ok": False, "http_status": response.status_code, "response": detail})
                delta_log(event, status="failed", chat_id=chat_id, http_status=response.status_code, response=detail)
            else:
                results.append({"chat_id": chat_id, "ok": True, "http_status": response.status_code})
        except requests.RequestException as exc:
            results.append({"chat_id": chat_id, "ok": False, "error": str(exc)})
            delta_log(event, status="failed", chat_id=chat_id, error=str(exc))
    return results


def _context(position: dict, context: dict | None = None) -> dict:
    snapshot = position.get("signal_snapshot") or {}
    settings = snapshot.get("settings") if isinstance(snapshot.get("settings"), dict) else {}
    context = context or {}
    asset = str(context.get("asset") or snapshot.get("asset") or "gold").title()
    mode = str(context.get("mode") or snapshot.get("execution") or position.get("execution") or "paper").upper()
    minutes = context.get("minutes") or snapshot.get("timeframe") or ""
    timeframe = f"{minutes} min" if isinstance(minutes, int) else str(minutes).replace("m", " min")
    return {
        "asset": asset,
        "mode": mode,
        "timeframe": timeframe or "--",
        "symbol": position.get("symbol") or snapshot.get("symbol") or "--",
        "side": position.get("side") or "--",
        "qty": _fmt_number(position.get("qty"), 0),
        "entry": _fmt_number(position.get("entry_price")),
        "sl": _fmt_number(position.get("sl_price")),
        "target": _fmt_number(position.get("target_price")),
        "exit_mode": str(snapshot.get("silver_exit_policy") or settings.get("exit_mode") or "--").replace("_", " "),
    }


def _trade_header(title: str, position: dict, context: dict | None = None, marker: str | None = None) -> tuple[str, dict]:
    ctx = _context(position, context)
    badge = marker or _side_marker(ctx["side"])
    return (
        f"<b>{_esc(badge)} {title}</b>\n"
        f"<b>{_esc(ctx['asset'])}</b> / {_underline(ctx['timeframe'])} / {_code(ctx['mode'])}\n"
        f"{_code(ctx['symbol'])}",
        ctx,
    )


def alert_trade_open(position: dict, context: dict | None = None) -> None:
    if not _trade_alert_mode_allowed(position, context):
        return
    header, ctx = _trade_header("Delta Trade Opened", position, context)
    lines = [
        header,
        "",
        f"<b>Timeframe:</b> {_underline(ctx['timeframe'])}",
        _detail_rows(
            [
                ("Side", ctx["side"]),
                ("Lots", ctx["qty"]),
                ("Entry", ctx["entry"]),
                ("SL", ctx["sl"]),
                ("Target", ctx["target"]),
                ("Exit mode", ctx["exit_mode"]),
                ("Time", _fmt_time(position.get("entry_time"))),
            ]
        ),
    ]
    send_telegram_alert("\n".join(lines), event="telegram_trade_open_alert", parse_mode="HTML")


def alert_trade_close(trade: dict, context: dict | None = None) -> None:
    if not _trade_alert_mode_allowed(trade, context):
        return
    rate = inr_rate("india", trade.get("quote_currency") or "USD")
    net = trade.get("net_pnl")
    net_inr = float(net) * rate if net is not None and rate else None
    reason = _exit_reason_label(trade.get("exit_reason"))
    header, ctx = _trade_header("Delta Trade Exited", trade, context, marker=_pnl_marker(net))
    lines = [
        header,
        "",
        f"<b>Timeframe:</b> {_underline(ctx['timeframe'])}",
        f"<b>Exit reason:</b> {_underline(reason)}",
        _detail_rows(
            [
                ("Side", ctx["side"]),
                ("Lots", ctx["qty"]),
                ("Entry", ctx["entry"]),
                ("Exit", _fmt_number(trade.get("exit_price"))),
                ("Reason", reason),
                ("SL", ctx["sl"]),
                ("Target", ctx["target"]),
                ("Gross P&L", _money_text(trade.get("gross_pnl"), "USD")),
                ("Net P&L", _money_text(net, "USD")),
                ("Net INR", _money_text(net_inr, "INR")),
                ("Entry time", _fmt_time(trade.get("entry_time"))),
                ("Exit time", _fmt_time(trade.get("exit_time"))),
            ],
            label_width=10,
        ),
    ]
    send_telegram_alert("\n".join(lines), event="telegram_trade_close_alert", parse_mode="HTML")


def alert_wallet_transaction(transaction: dict, *, asset: str = "gold") -> None:
    kind = str(transaction.get("transaction_type") or "wallet").replace("_", " ").title()
    amount = transaction.get("amount_usd")
    rate = inr_rate("india", transaction.get("asset_symbol") or "USD")
    amount_inr = float(amount) * rate if amount is not None and rate else None
    marker = "[WALLET +]" if str(transaction.get("transaction_type") or "").lower() in {"deposit", "user_credit"} else "[WALLET -]"
    amount_text = _money_text(amount, transaction.get("asset_symbol") or "USD")
    time_text = _fmt_time(transaction.get("created_at"))
    lines = [
        f"<b>{_esc(marker)} Delta Wallet {kind}</b>",
        f"<b>Amount:</b> {_bold(amount_text)}",
        f"<b>Time:</b> {_bold(time_text)}",
        _detail_rows(
            [
                ("Asset", asset.title()),
                ("Amount", amount_text),
                ("Approx INR", _money_text(amount_inr, "INR")),
                ("Balance", _money_text(transaction.get("balance_after_usd"), "USD")),
                ("Reference", transaction.get("reference") or transaction.get("id") or "--"),
                ("Time", time_text),
            ]
        ),
    ]
    send_telegram_alert("\n".join(lines), event="telegram_wallet_alert", parse_mode="HTML")


def seed_seen_transactions(rows: Iterable[dict]) -> set[str]:
    return {str(row.get("id")) for row in rows if row.get("id") is not None}
