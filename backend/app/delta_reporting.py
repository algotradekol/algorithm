"""Display-only valuations; never change sizing, stops or execution policy."""
import math
import datetime
from .delta_client import epoch_seconds

# Delta India's fixed accounting conversion, not a market FX quote.
# https://guides.delta.exchange/delta-exchange-india-user-guide/account-setup/usd-inr-rate
INDIA_USD_INR = 85.0


def finite(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def inr_rate(region, currency):
    return INDIA_USD_INR if region == "india" and currency == "USD" else None


def paper_margin(entry, qty, contract_value, margin_percent):
    values = [finite(v) for v in (entry, qty, contract_value, margin_percent)]
    if any(v is None or v <= 0 for v in values):
        return None
    return values[0] * values[1] * values[2] * values[3] / 100


def paper_row(row, region):
    result = dict(row)
    currency = row.get("quote_currency")
    if not currency and region == "india" and row.get("symbol") == "PAXGUSD":
        currency = "USD"  # Legacy India gold rows predate the currency snapshot.
    rate = inr_rate(region, currency)
    result["pnl_inr"] = None
    result["margin_inr"] = None
    if rate:
        pnl = finite(row.get("net_pnl") if "exit_time" in row else row.get("unrealized_pnl"))
        margin = finite(row.get("estimated_entry_margin"))
        result["pnl_inr"] = pnl * rate if pnl is not None else None
        result["margin_inr"] = margin * rate if margin is not None else None
    return result


def account_rows(rows, kind, region):
    """Expose only display fields; do not leak account identifiers or raw payloads."""
    output = []
    for row in rows:
        size = finite(row.get("size"))
        if kind == "positions" and size == 0:
            continue
        product = row.get("product") or {}
        currency = (product.get("settling_asset") or {}).get("symbol")
        # India account values are notionally USD even in compact API responses.
        currency = currency or ("USD" if region == "india" else None)
        rate = inr_rate(region, currency)
        margin = finite(row.get("margin"))  # Never invent per-order margin.
        output.append({
            "id": str(row.get("id") or row.get("product_id") or ""),
            "symbol": row.get("product_symbol") or product.get("symbol") or str(row.get("product_id", "--")),
            "side": ("--" if size is None else "BUY" if size > 0 else "SELL") if kind == "positions" else str(row.get("side", "--")).upper(),
            "lots": abs(size) if size is not None else None,
            "entry_price": finite(row.get("entry_price") if kind == "positions" else row.get("average_fill_price")),
            "limit_price": finite(row.get("limit_price")), "stop_price": finite(row.get("stop_price")),
            "margin": margin, "margin_inr": margin * rate if margin is not None and rate else None,
            "currency": currency, "state": row.get("state") or "open",
            "order_type": row.get("stop_order_type") or row.get("order_type") or "position",
            "time": account_time(row.get("updated_at") or row.get("created_at")),
        })
    return output


def account_time(value):
    if value is None:
        return None
    try:
        if finite(value) is not None:
            return datetime.datetime.fromtimestamp(epoch_seconds(value), datetime.timezone.utc).isoformat()
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or datetime.timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return None
