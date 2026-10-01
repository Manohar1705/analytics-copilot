"""observability.py - the only file that talks to Langfuse.

Records one row per AI call (step name, model, tokens, latency, status) and
reads those rows back for the LLM Insights page. It never sends prompts,
sample rows or answers, only metadata. If the Langfuse keys are missing,
every function here quietly does nothing, so the app still runs without it.
"""

from __future__ import annotations

import os
import threading
from typing import Any

ENVIRONMENT = "analytics-copilot"  # keeps this app's rows apart from other apps in the project

_client: Any = None
_client_failed = False
_lock = threading.Lock()


def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass


def enabled() -> bool:
    _load_env()
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


def _get_client() -> Any:
    """Create the Langfuse client once. Returns None when it is not configured."""
    global _client, _client_failed
    if _client is not None or _client_failed:
        return _client
    with _lock:
        if _client is not None or _client_failed:
            return _client
        if not enabled():
            _client_failed = True
            return None
        try:
            from langfuse import Langfuse

            host = os.getenv("LANGFUSE_HOST") or os.getenv("LANGFUSE_BASE_URL")
            _client = Langfuse(
                public_key=os.getenv("LANGFUSE_PUBLIC_KEY"),
                secret_key=os.getenv("LANGFUSE_SECRET_KEY"),
                host=host,
                environment=ENVIRONMENT,
                flush_interval=1,  # send in the background every second
            )
        except Exception:  # noqa: BLE001 - monitoring must never break the app
            _client_failed = True
            _client = None
    return _client


class Call:
    """One AI call being timed. Ends exactly once."""

    def __init__(self, generation: Any):
        self._generation = generation
        self._done = False

    def finish(self, usage: Any = None, error: str | None = None) -> None:
        if self._done or self._generation is None:
            return
        self._done = True
        try:
            update: dict[str, Any] = {}
            if usage is not None:
                prompt = getattr(usage, "prompt_tokens", None) or 0
                completion = getattr(usage, "completion_tokens", None) or 0
                total = getattr(usage, "total_tokens", None) or (prompt + completion)
                update["usage_details"] = {"input": prompt, "output": completion, "total": total}
            if error:
                update["level"] = "ERROR"
                update["status_message"] = error[:200]
            if update:
                self._generation.update(**update)
            self._generation.end()
        except Exception:  # noqa: BLE001
            pass


def new_trace_id() -> str | None:
    """One id per question, so its 2-3 AI calls are grouped together."""
    client = _get_client()
    if client is None:
        return None
    try:
        return client.create_trace_id()
    except Exception:  # noqa: BLE001
        return None


def start_call(step: str, model: str, provider: str, session_id: str | None, trace_id: str | None) -> Call:
    client = _get_client()
    if client is None:
        return Call(None)
    try:
        from langfuse import propagate_attributes

        kwargs: dict[str, Any] = {
            "name": step,
            "as_type": "generation",
            "model": model,
            "metadata": {"provider": provider},
        }
        if trace_id:
            kwargs["trace_context"] = {"trace_id": trace_id}
        if session_id:
            with propagate_attributes(session_id=session_id):
                generation = client.start_observation(**kwargs)
        else:
            generation = client.start_observation(**kwargs)
        return Call(generation)
    except Exception:  # noqa: BLE001
        return Call(None)


def fetch_calls(limit: int = 100) -> list[dict[str, Any]]:
    """Latest AI calls of this app, newest first. Raises on connection problems."""
    client = _get_client()
    if client is None:
        raise RuntimeError("Langfuse keys are not set (LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY).")
    response = client.api.observations.get_many(
        type="GENERATION",
        environment=ENVIRONMENT,
        fields="core,basic,model,usage,metrics",
        limit=limit,
    )
    rows = []
    for item in response.data:
        usage = item.usage_details or {}
        rows.append(
            {
                "name": item.name,
                "start_time": item.start_time,
                "model": item.model,
                "cost": item.total_cost,
                "latency": item.latency,
                "input_tokens": usage.get("input"),
                "output_tokens": usage.get("output"),
                "total_tokens": usage.get("total"),
                "status": "error" if str(item.level or "").upper() == "ERROR" else "ok",
                "trace_id": item.trace_id,
                "session_id": item.session_id,
            }
        )
    rows.sort(key=lambda r: r["start_time"], reverse=True)
    return rows