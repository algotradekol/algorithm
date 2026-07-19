import datetime
from zoneinfo import ZoneInfo
from uuid import uuid4

import requests

from app.config import ENTRY_CHECK_TIME, GEMINI_API_KEY, GEMINI_MODEL, MARKET_OPEN_TIME, SQUARE_OFF_TIME
from app.supabase_client import supabase


SYSTEM_PROMPT = """You are the in-app AI copilot for an algo paper trading dashboard.
You help the user understand the interface, charts, strategy settings, scan bottlenecks,
paper P&L, Fyers connection status, and deployment/system issues. Be precise and practical.
When the user asks about "this page", "what I am seeing", "this chart", or similar,
prioritize APP CONTEXT JSON.page_context.active_tab, active_section, history, chart,
and visible_page_text before giving a generic dashboard explanation.
Format answers for easy reading in the app: use short Markdown sections, bullet lists,
and brief conversational paragraphs. Never put multiple bullet points on one line.
Avoid raw asterisks as separators; use proper Markdown bullets on their own lines.
Do not infer that empty trades, empty logs, or zero candidates are caused by strict filters
unless scan_results.scan_time exists for today and the scan result explicitly shows candidates
were rejected by filters. If the market is not open yet, or the 9:16 scan has not run yet,
say that first and clearly.
Do not claim real broker execution happened unless the provided app context shows it.
This is paper trading, not financial advice."""


class AIProviderRateLimitError(RuntimeError):
    pass


class AIProviderError(RuntimeError):
    pass


def list_sessions(user_id: str) -> list[dict]:
    result = supabase.table("ai_chat_sessions").select("*").eq("user_id", user_id).order("updated_at", desc=True).limit(30).execute()
    return result.data or []


def create_session(user_id: str, title: str = "New chat") -> dict:
    result = supabase.table("ai_chat_sessions").insert({
        "id": str(uuid4()),
        "user_id": user_id,
        "title": title[:80] or "New chat",
        "updated_at": _now(),
    }).execute()
    return result.data[0]


def get_messages(session_id: str) -> list[dict]:
    result = supabase.table("ai_chat_messages").select("*").eq("session_id", session_id).order("created_at").execute()
    return result.data or []


def delete_session(user_id: str, session_id: str) -> dict:
    result = supabase.table("ai_chat_sessions").select("id").eq("id", session_id).eq("user_id", user_id).execute()
    if not result.data:
        return {"deleted": False}
    supabase.table("ai_chat_messages").delete().eq("session_id", session_id).execute()
    supabase.table("ai_chat_sessions").delete().eq("id", session_id).eq("user_id", user_id).execute()
    return {"deleted": True}


def send_message(user_id: str, session_id: str | None, message: str, page_context: dict | None = None) -> dict:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured on the backend")

    session = _ensure_session(user_id, session_id, message)
    app_context = build_app_context(page_context or {})
    recent_messages = get_messages(session["id"])[-30:]

    supabase.table("ai_chat_messages").insert({
        "session_id": session["id"],
        "role": "user",
        "content": message,
        "context": page_context or {},
    }).execute()

    answer = call_gemini(message, recent_messages, app_context)

    supabase.table("ai_chat_messages").insert({
        "session_id": session["id"],
        "role": "assistant",
        "content": answer,
        "context": app_context,
    }).execute()
    supabase.table("ai_chat_sessions").update({"updated_at": _now(), "title": session.get("title") or message[:80]}).eq("id", session["id"]).execute()

    return {"session": session, "answer": answer, "messages": get_messages(session["id"])}


def build_app_context(page_context: dict) -> dict:
    from app.engine import SCAN_RESULTS, STRATEGIES, get_engine_status
    from app.fyers_client import get_connection_status
    from app.charges import get_charges_config
    from app.strategy_settings import get_settings

    market_clock = _market_clock()
    algos = {}
    for algo_id, strategy in STRATEGIES.items():
      broker = strategy.broker
      scan_result = SCAN_RESULTS.get(algo_id)
      algos[algo_id] = {
          "display_name": getattr(strategy, "display_name", algo_id),
          "summary": _safe(lambda: broker.summary()),
          "positions": _safe(lambda: broker.open_positions()),
          "recent_trades": _safe(lambda: broker.recent_trades(limit=30)),
          "settings": _safe(lambda: get_settings(algo_id)),
          "scan_results": scan_result,
          "scan_state": _scan_state(scan_result, market_clock),
      }

    return {
        "timestamp": _now(),
        "market_clock": market_clock,
        "page_context": page_context,
        "engine": get_engine_status(),
        "fyers": get_connection_status(),
        "charges": _safe(get_charges_config),
        "algos": algos,
        "interpretation_rules": {
            "empty_trades": "Empty trade logs usually mean no paper trades have happened yet. Before market open or before the 9:16 scan, do not blame filters.",
            "empty_scan": "If scan_results is missing or says no scan run yet today, explain that results appear after the 9:16 AM scan.",
            "strict_filters": "Only mention strict filters when scan_results has a scan_time and rejection data showing filters rejected candidates.",
        },
    }


def call_gemini(message: str, recent_messages: list[dict], app_context: dict) -> str:
    history_text = "\n".join([f"{m['role']}: {m['content']}" for m in recent_messages[-30:]])
    prompt = f"""{SYSTEM_PROMPT}

APP CONTEXT JSON:
{app_context}

RECENT CHAT HISTORY:
{history_text}

USER MESSAGE:
{message}
"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    response = requests.post(
        url,
        headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=60,
    )
    if response.status_code == 429:
        raise AIProviderRateLimitError(
            "Gemini API quota/rate limit hit. Wait a bit, reduce rapid chat retries, or check the Gemini API key billing/quota in Google AI Studio or Google Cloud."
        )
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        detail = _extract_provider_error(response)
        raise AIProviderError(f"Gemini API error {response.status_code}: {detail}") from exc
    data = response.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        return "I received an empty response from Gemini. Please try again."


def _ensure_session(user_id: str, session_id: str | None, message: str) -> dict:
    if session_id:
        result = supabase.table("ai_chat_sessions").select("*").eq("id", session_id).eq("user_id", user_id).execute()
        if result.data:
            return result.data[0]
    return create_session(user_id, message[:80] or "New chat")


def _safe(fn):
    try:
        return fn()
    except Exception as exc:
        return {"error": str(exc)}


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _market_clock() -> dict:
    now = datetime.datetime.now(ZoneInfo("Asia/Kolkata"))
    current_time = now.strftime("%H:%M")
    market_open = current_time >= MARKET_OPEN_TIME
    entry_scan_due = current_time >= ENTRY_CHECK_TIME
    after_squareoff = current_time >= SQUARE_OFF_TIME
    if not market_open:
        phase = "before_market_open"
    elif not entry_scan_due:
        phase = "market_open_before_9_16_scan"
    elif after_squareoff:
        phase = "after_square_off"
    else:
        phase = "market_open_after_9_16_scan"
    return {
        "timezone": "Asia/Kolkata",
        "date": now.date().isoformat(),
        "time": current_time,
        "market_open_time": MARKET_OPEN_TIME,
        "entry_scan_time": ENTRY_CHECK_TIME,
        "square_off_time": SQUARE_OFF_TIME,
        "phase": phase,
        "market_open": market_open,
        "entry_scan_due": entry_scan_due,
        "after_squareoff": after_squareoff,
    }


def _scan_state(scan_result: dict | None, market_clock: dict) -> dict:
    if not scan_result or not scan_result.get("scan_time"):
        return {
            "status": "not_run_today",
            "plain_english": "The 9:16 AM scan has not produced results yet. Empty candidates/trades should not be blamed on filters.",
        }
    scan_date = str(scan_result.get("scan_time", ""))[:10]
    if scan_date != market_clock["date"]:
        return {
            "status": "stale",
            "plain_english": "The available scan result is from a previous date, not today's live market session.",
        }
    return {
        "status": "ran_today",
        "plain_english": "Today's scan has run; rejection/candidate counts can be interpreted from scan_results.",
    }


def _extract_provider_error(response: requests.Response) -> str:
    try:
        data = response.json()
        return data.get("error", {}).get("message") or response.text[:500]
    except Exception:
        return response.text[:500] or response.reason
