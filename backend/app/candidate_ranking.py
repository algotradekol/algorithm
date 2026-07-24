"""Deterministic, explainable ranking for already-qualified paper-trade signals."""

from __future__ import annotations

from collections import defaultdict


FILTER_WEIGHTS = {
    "gap_strength": 0.22,
    "sector_alignment": 0.10,
    "volume_ratio": 0.14,
    "rsi_momentum": 0.14,
    "adx_strength": 0.14,
    "vwap_distance": 0.13,
    "supertrend_margin": 0.13,
}


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _number(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _indicator_value(row: dict, name: str) -> float | None:
    return _number((row.get("indicator_results") or {}).get(name, {}).get("value"))


def _sector_name(row: dict) -> str:
    sector = str(row.get("sector") or row.get("industry") or row.get("sector_name") or "").strip()
    return sector or "Unclassified"


def _signed_open_change_pct(row: dict) -> float:
    open_price = _number(row.get("open"))
    prev_close = _number(row.get("prev_close"))
    if not open_price or not prev_close:
        return 0.0
    return ((open_price - prev_close) / prev_close) * 100


def build_sector_breakdown(candidates: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in candidates:
        grouped[_sector_name(row)].append(row)

    breakdown = []
    for sector, rows in grouped.items():
        signed_moves = [_signed_open_change_pct(row) for row in rows if _number(row.get("prev_close"))]
        avg_move = sum(signed_moves) / len(signed_moves) if signed_moves else 0.0
        selected = sum(1 for row in rows if row.get("selected_for_trade"))
        buy = sum(1 for row in rows if row.get("side") == "BUY")
        sell = sum(1 for row in rows if row.get("side") == "SELL")
        avg_score = sum(_number(row.get("composite_score")) or 0.0 for row in rows) / len(rows) if rows else 0.0
        breakdown.append({
            "sector": sector,
            "rows": len(rows),
            "buy": buy,
            "sell": sell,
            "selected": selected,
            "avg_score": round(avg_score, 2),
            "direction": "bullish" if avg_move > 0 else "bearish" if avg_move < 0 else "neutral",
            "avg_move_pct": round(avg_move, 3),
            "alignment_strength": round(_clamp(abs(avg_move) / 2.0), 4),
        })

    return sorted(breakdown, key=lambda row: (-row["selected"], -row["avg_score"], row["sector"]))


def _sector_alignment(row: dict) -> float:
    context = row.get("_sector_context") or {}
    sector_direction = float(context.get("direction") or 0.0)
    sector_strength = float(context.get("strength") or 0.0)
    side = row.get("side")
    aligned = (side == "BUY" and sector_direction >= 0) or (side == "SELL" and sector_direction <= 0)
    return sector_strength if aligned else 0.0


def _directional_distance(price: float | None, reference: float | None, side: str) -> float:
    if not price or not reference:
        return 0.0
    distance = (price - reference) / reference if side == "BUY" else (reference - price) / reference
    # A one-percent confirmation distance is considered full strength. Larger
    # moves do not earn unbounded influence over the final rank.
    return _clamp(distance / 0.01)


def score_candidate(row: dict, settings: dict, profile: str) -> dict:
    """Return normalized scoring components and a 0-100 composite score.

    Ranking never turns a failed candidate into a trade. It only orders rows
    that already passed the strategy's required entry conditions.
    """
    side = row.get("side")
    gap_pct = _number(row.get("gap_pct")) or 0.0
    gap_strength = _clamp(gap_pct / 2.0)
    sector_alignment = _sector_alignment(row)
    if profile == "simple":
        score = (gap_strength * 0.8) + (sector_alignment * 0.2)
        return {
            "score": round(score * 100, 2),
            "components": {
                "gap_strength": round(gap_strength, 4),
                "sector_alignment": round(sector_alignment, 4),
            },
            "method": "gap_and_sector_alignment",
        }

    price = _number(row.get("ltp")) or _number(row.get("close")) or _number(row.get("open"))
    volume = _indicator_value(row, "volume")
    min_volume = _number(settings.get("min_volume")) or 0.0
    volume_ratio = _clamp((volume or 0.0) / min_volume / 5.0) if min_volume > 0 else 0.0

    rsi = _indicator_value(row, "rsi")
    if side == "BUY":
        threshold = _number(settings.get("rsi_buy_threshold")) or 55.0
        rsi_momentum = _clamp(((rsi or 0.0) - threshold) / max(1.0, 80.0 - threshold))
    else:
        threshold = _number(settings.get("rsi_sell_threshold")) or 45.0
        rsi_momentum = _clamp((threshold - (rsi or 100.0)) / max(1.0, threshold - 20.0))

    adx = _indicator_value(row, "adx")
    adx_threshold = _number(settings.get("adx_threshold")) or 20.0
    adx_strength = _clamp(((adx or 0.0) - adx_threshold) / max(1.0, 50.0 - adx_threshold))
    vwap_distance = _directional_distance(price, _indicator_value(row, "vwap"), side)
    supertrend_margin = _directional_distance(price, _indicator_value(row, "supertrend"), side)
    components = {
        "gap_strength": gap_strength,
        "sector_alignment": sector_alignment,
        "volume_ratio": volume_ratio,
        "rsi_momentum": rsi_momentum,
        "adx_strength": adx_strength,
        "vwap_distance": vwap_distance,
        "supertrend_margin": supertrend_margin,
    }
    indicator_results = row.get("indicator_results") or {}
    enabled_weights = {
        "gap_strength": FILTER_WEIGHTS["gap_strength"],
        "sector_alignment": FILTER_WEIGHTS["sector_alignment"],
        "volume_ratio": FILTER_WEIGHTS["volume_ratio"] if indicator_results.get("volume", {}).get("enabled") else 0.0,
        "rsi_momentum": FILTER_WEIGHTS["rsi_momentum"] if indicator_results.get("rsi", {}).get("enabled") else 0.0,
        "adx_strength": FILTER_WEIGHTS["adx_strength"] if indicator_results.get("adx", {}).get("enabled") else 0.0,
        "vwap_distance": FILTER_WEIGHTS["vwap_distance"] if indicator_results.get("vwap", {}).get("enabled") else 0.0,
        "supertrend_margin": FILTER_WEIGHTS["supertrend_margin"] if indicator_results.get("supertrend", {}).get("enabled") else 0.0,
    }
    active_weight = sum(enabled_weights.values())
    # Disabled filters must not silently affect selection order. Normalize the
    # remaining weights so a scan ranks on its actual evidence.
    score = sum(components[name] * enabled_weights[name] for name in FILTER_WEIGHTS) / active_weight if active_weight else 0.0
    return {
        "score": round(score * 100, 2),
        "components": {name: round(value, 4) for name, value in components.items()},
        "method": "weighted_filter_score",
    }


def rank_candidates(candidates: list[dict], settings: dict, profile: str) -> list[dict]:
    """Annotate candidates in score order; symbol breaks exact-score ties."""
    sector_context: dict[str, dict[str, float]] = {}
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in candidates:
        grouped[_sector_name(row)].append(row)
    for sector, rows in grouped.items():
        signed_moves = [_signed_open_change_pct(row) for row in rows if _number(row.get("prev_close"))]
        avg_move = sum(signed_moves) / len(signed_moves) if signed_moves else 0.0
        sector_context[sector] = {
            "direction": avg_move,
            "strength": _clamp(abs(avg_move) / 2.0),
        }

    for row in candidates:
        row["_sector_context"] = sector_context.get(_sector_name(row), {"direction": 0.0, "strength": 0.0})
        ranking = score_candidate(row, settings, profile)
        row["composite_score"] = ranking["score"]
        row["score_breakdown"] = ranking["components"]
        row["ranking_method"] = ranking["method"]
    ranked = sorted(candidates, key=lambda row: (-float(row["composite_score"]), str(row.get("symbol") or "")))
    for index, row in enumerate(ranked, start=1):
        row["rank"] = index
    return ranked


def select_ranked_candidates(ranked: list[dict], settings: dict) -> list[dict]:
    """Apply total/side caps after scoring, with ranked overflow support."""
    max_total = max(0, int(settings.get("max_trades_per_day") or 0))
    side_caps = {
        "BUY": max(0, int(settings.get("max_buy_trades") or 0)),
        "SELL": max(0, int(settings.get("max_sell_trades") or 0)),
    }
    available = {side: sum(row.get("side") == side for row in ranked) for side in side_caps}
    selected: list[dict] = []
    selected_ids: set[int] = set()
    side_counts = {"BUY": 0, "SELL": 0}

    # First satisfy each side's configured allocation using the global score
    # order. The daily total is always a hard ceiling.
    for row in ranked:
        side = row.get("side")
        if len(selected) >= max_total or side not in side_caps or side_counts[side] >= side_caps[side]:
            continue
        selected.append(row)
        selected_ids.add(id(row))
        side_counts[side] += 1

    # If one side has fewer qualifying rows than its allocation, the other
    # side may fill the unused daily slots in the same score order.
    for row in ranked:
        if len(selected) >= max_total or id(row) in selected_ids:
            continue
        side = row.get("side")
        other = "SELL" if side == "BUY" else "BUY"
        if side in side_caps and available.get(other, 0) < side_caps[other]:
            selected.append(row)
            selected_ids.add(id(row))
            side_counts[side] += 1
    return selected
