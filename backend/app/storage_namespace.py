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


def strip_current_namespace(value: str) -> str:
    normalized = str(value or "").strip()
    if config.BROKER_KEY_SUFFIX:
        suffix = f"__{config.BROKER_KEY_SUFFIX}"
        if normalized.endswith(suffix):
            return normalized[: -len(suffix)]
    return normalized
