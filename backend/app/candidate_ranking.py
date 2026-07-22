"""Deterministic, explainable ranking for already-qualified paper-trade signals."""

from __future__ import annotations


FILTER_WEIGHTS = {
    "gap_strength": 0.25,
    "volume_ratio": 0.15,
    "rsi_momentum": 0.15,
    "adx_strength": 0.15,
    "vwap_distance": 0.15,
    "supertrend_margin": 0.15,
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
    if profile == "simple":
        return {
            "score": round(gap_strength * 100, 2),
            "components": {"gap_strength": round(gap_strength, 4)},
            "method": "gap_strength_only",
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
        "volume_ratio": volume_ratio,
        "rsi_momentum": rsi_momentum,
        "adx_strength": adx_strength,
        "vwap_distance": vwap_distance,
        "supertrend_margin": supertrend_margin,
    }
    score = sum(components[name] * FILTER_WEIGHTS[name] for name in FILTER_WEIGHTS)
    return {
        "score": round(score * 100, 2),
        "components": {name: round(value, 4) for name, value in components.items()},
        "method": "weighted_filter_score",
    }


def rank_candidates(candidates: list[dict], settings: dict, profile: str) -> list[dict]:
    """Annotate candidates in score order; symbol breaks exact-score ties."""
    for row in candidates:
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
