"""
charges.py — the shared charges engine. Both algos' trades run through
this to get Net P&L. Rates are stored in Supabase (charges_config
table) and editable from the frontend Charges panel.

IMPORTANT: the defaults below are the commonly published rates for
intraday equity delivery on NSE via discount brokers, current as of
this build. Broker charges (especially exchange transaction charges
and STT) do get revised by regulators/exchanges from time to time --
before trusting Net P&L numbers for real decisions, cross-check these
against a recent actual Fyers contract note and update charges_config
if anything's drifted.
"""

DEFAULT_CHARGES_CONFIG = {
    "brokerage_flat": 20.0,       # ₹ per executed order (per leg)
    "brokerage_pct": 0.03,        # % of turnover, whichever is LOWER than the flat fee applies
    "stt_pct": 0.025,             # % on SELL side turnover only (intraday equity)
    "exchange_pct": 0.00297,      # % on total turnover (buy + sell), NSE transaction charges
    "sebi_pct": 0.0001,           # % on total turnover (₹10 per crore)
    "gst_pct": 18.0,              # % on (brokerage + exchange charges + SEBI charges)
    "stamp_duty_pct": 0.003,      # % on BUY side turnover only
}


def calculate_charges(buy_value: float, sell_value: float, config: dict) -> dict:
    """
    buy_value / sell_value: total ₹ turnover on each side of a closed
    trade (entry_price * qty and exit_price * qty, on whichever side
    was buy vs sell for that trade's direction).
    """
    turnover = buy_value + sell_value

    brokerage_buy = min(config["brokerage_flat"], config["brokerage_pct"] / 100 * buy_value) if buy_value else 0
    brokerage_sell = min(config["brokerage_flat"], config["brokerage_pct"] / 100 * sell_value) if sell_value else 0
    brokerage = brokerage_buy + brokerage_sell

    stt = config["stt_pct"] / 100 * sell_value
    exchange_charges = config["exchange_pct"] / 100 * turnover
    sebi_charges = config["sebi_pct"] / 100 * turnover
    gst = config["gst_pct"] / 100 * (brokerage + exchange_charges + sebi_charges)
    stamp_duty = config["stamp_duty_pct"] / 100 * buy_value

    total_charges = brokerage + stt + exchange_charges + sebi_charges + gst + stamp_duty
    gross_pnl = sell_value - buy_value
    net_pnl = gross_pnl - total_charges

    return {
        "brokerage": round(brokerage, 2),
        "stt": round(stt, 2),
        "exchange_charges": round(exchange_charges, 2),
        "sebi_charges": round(sebi_charges, 2),
        "gst": round(gst, 2),
        "stamp_duty": round(stamp_duty, 2),
        "total_charges": round(total_charges, 2),
        "gross_pnl": round(gross_pnl, 2),
        "net_pnl": round(net_pnl, 2),
    }


def get_charges_config() -> dict:
    from .supabase_client import run_with_supabase
    result = run_with_supabase(
        lambda supabase: supabase.table("charges_config").select("*").eq("id", 1).execute()
    )
    if result.data:
        row = result.data[0]
        return {k: row[k] for k in DEFAULT_CHARGES_CONFIG}
    return DEFAULT_CHARGES_CONFIG.copy()


def set_charges_config(config: dict):
    from .supabase_client import run_with_supabase
    run_with_supabase(
        lambda supabase: supabase.table("charges_config").upsert({"id": 1, **config}).execute()
    )
