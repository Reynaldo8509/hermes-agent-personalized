"""Private, local-only turn telemetry for Hermes.

This plugin deliberately records metadata only.  It never persists message
content, tool arguments, raw tool output, credentials, or provider payloads.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
from typing import Any

_LOCK = threading.Lock()
_MAX_FILE_BYTES = 32 * 1024 * 1024
_KEEP_ROTATIONS = 7


def _home() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def _telemetry_dir() -> Path:
    path = _home() / "telemetry"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _log_path() -> Path:
    return _telemetry_dir() / "turns.jsonl"


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _id(value: Any) -> str:
    raw = str(value or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12] if raw else ""


def _rotate_if_needed(path: Path) -> None:
    try:
        if not path.exists() or path.stat().st_size < _MAX_FILE_BYTES:
            return
        for index in range(_KEEP_ROTATIONS - 1, 0, -1):
            source = path.with_name(f"{path.name}.{index}")
            target = path.with_name(f"{path.name}.{index + 1}")
            if source.exists():
                source.replace(target)
        path.replace(path.with_name(f"{path.name}.1"))
    except OSError:
        return


def _append(record: dict[str, Any]) -> None:
    payload = dict(record)
    payload["timestamp"] = datetime.now(timezone.utc).isoformat()
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with _LOCK:
        path = _log_path()
        _rotate_if_needed(path)
        with path.open("a", encoding="utf-8") as handle:
            os.chmod(path, 0o600)
            handle.write(line + "\n")


def _compression_enabled() -> bool:
    try:
        from hermes_cli.config import load_config_readonly

        compression = (load_config_readonly() or {}).get("compression") or {}
        return bool(compression.get("enabled", False)) if isinstance(compression, dict) else False
    except Exception:
        return False


def _default_model() -> str:
    """Return the configured primary model without ever reading secrets."""
    try:
        from hermes_cli.config import load_config_readonly

        model = (load_config_readonly() or {}).get("model") or {}
        return str(model.get("default") or "") if isinstance(model, dict) else ""
    except Exception:
        return ""


def _on_pre_gateway_dispatch(event=None, **_: Any) -> None:
    source = getattr(event, "source", None)
    _append(
        {
            "event": "inbound",
            "channel": str(getattr(getattr(source, "platform", None), "value", "") or ""),
            "chat": _id(getattr(source, "chat_id", "")),
            "sender": _id(getattr(source, "user_id", "")),
        }
    )


def _on_post_api_request(
    *,
    task_id: str = "",
    session_id: str = "",
    platform: str = "",
    provider: str = "",
    model: str = "",
    api_call_count: int = 0,
    api_duration: float = 0.0,
    message_count: int = 0,
    usage: Any = None,
    assistant_content_chars: int = 0,
    assistant_tool_call_count: int = 0,
    finish_reason: str = "",
    response_model: Any = None,
    **_: Any,
) -> None:
    usage = usage if isinstance(usage, dict) else {}
    resolved_model = str(response_model or model or "")
    default_model = _default_model()
    fallback_used = bool(default_model and resolved_model and resolved_model != default_model)
    _append(
        {
            "event": "llm",
            "result": "ok",
            "task": _id(task_id),
            "session": _id(session_id),
            "channel": str(platform or ""),
            "provider": str(provider or ""),
            "model": resolved_model,
            "route": "fallback" if fallback_used else "default",
            "fallback_used": fallback_used,
            "request": _safe_int(api_call_count),
            "latency_ms": max(0, round(float(api_duration or 0) * 1000)),
            "messages": _safe_int(message_count),
            "prompt_tokens": _safe_int(usage.get("prompt_tokens", usage.get("input_tokens"))),
            "output_tokens": _safe_int(usage.get("completion_tokens", usage.get("output_tokens"))),
            "total_tokens": _safe_int(usage.get("total_tokens")),
            "assistant_chars": _safe_int(assistant_content_chars),
            "tool_calls": _safe_int(assistant_tool_call_count),
            "finish_reason": str(finish_reason or ""),
            "compression_configured": _compression_enabled(),
        }
    )


def _on_api_request_error(
    *,
    task_id: str = "",
    session_id: str = "",
    platform: str = "",
    provider: str = "",
    model: str = "",
    status_code: Any = None,
    error_type: str = "",
    **_: Any,
) -> None:
    _append(
        {
            "event": "llm",
            "result": "error",
            "task": _id(task_id),
            "session": _id(session_id),
            "channel": str(platform or ""),
            "provider": str(provider or ""),
            "model": str(model or ""),
            "status_code": _safe_int(status_code),
            "error_type": str(error_type or ""),
        }
    )


def _on_post_tool_call(
    *,
    tool_name: str = "",
    task_id: str = "",
    session_id: str = "",
    turn_id: str = "",
    duration_ms: Any = 0,
    status: str = "",
    error_type: str = "",
    **_: Any,
) -> None:
    _append(
        {
            "event": "tool",
            "result": str(status or "unknown"),
            "task": _id(task_id),
            "session": _id(session_id),
            "turn": _id(turn_id),
            "tool": str(tool_name or ""),
            "latency_ms": _safe_int(duration_ms),
            "error_type": str(error_type or ""),
        }
    )


def register(ctx):
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)
    ctx.register_hook("post_api_request", _on_post_api_request)
    ctx.register_hook("api_request_error", _on_api_request_error)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
