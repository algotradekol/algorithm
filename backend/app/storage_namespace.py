from __future__ import annotations

from . import config


def namespaced_value(value: str) -> str:
    normalized = str(value or "").strip()
    if not config.BROKER_KEY_SUFFIX:
        return normalized
    return f"{normalized}__{config.BROKER_KEY_SUFFIX}"


def current_and_legacy_values(value: str) -> list[str]:
    normalized = str(value or "").strip()
    namespaced = namespaced_value(normalized)
    if namespaced == normalized:
        return [normalized]
    return [namespaced, normalized]


def current_storage_values(value: str) -> list[str]:
    """Return only the active deployment's storage key(s).

    Strategy state such as paper trades, paper positions, saved setup-history
    rows, and dashboard snapshots must never cross-read a legacy unsuffixed
    row once BROKER_KEY_SUFFIX is enabled. Doing so makes two deployments that
    share one Supabase appear to borrow each other's data.
    """
    normalized = str(value or "").strip()
    namespaced = namespaced_value(normalized)
    if namespaced == normalized:
        return [normalized]
    return [namespaced]


def strip_current_namespace(value: str) -> str:
    normalized = str(value or "").strip()
    if config.BROKER_KEY_SUFFIX:
        suffix = f"__{config.BROKER_KEY_SUFFIX}"
        if normalized.endswith(suffix):
            return normalized[: -len(suffix)]
    return normalized
