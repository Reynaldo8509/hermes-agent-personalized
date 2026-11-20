"""Guardrails for model tool selection and bounded recovery."""
from __future__ import annotations
import json
import re
import time
from typing import Any

COOLDOWN_SECONDS = 600
MAX_CALLS_PER_TASK = 6
MAX_DISCOVERY_PER_TASK = 3
INTERNAL_TOOLS = {"integration_catalog", "integration_status", "composio_discover", "composio_read", "x_latest_post"}
COMPOSIO_TOOLS = INTERNAL_TOOLS
COUNTS: dict[str, int] = {}
DISCOVERY_COUNTS: dict[str, int] = {}
FAILURE_UNTIL: dict[str, float] = {}

def _task(value: Any) -> str:
    return str(value or "global")[:160]

def _tool(value: Any) -> str:
    return str(value or "").strip().lower()

def _integration(value: Any) -> str:
    return re.sub(r"[^a-z0-9_-]", "", str(value or "").strip().lower())[:64]

def mark_failure(toolkit: str) -> None:
    if toolkit:
        FAILURE_UNTIL[_integration(toolkit)] = time.time() + COOLDOWN_SECONDS

def cooldown_active(toolkit: str) -> bool:
    key = _integration(toolkit)
    until = FAILURE_UNTIL.get(key, 0.0)
    if until and until <= time.time():
        FAILURE_UNTIL.pop(key, None)
        return False
    return until > time.time()

def _command(args: Any) -> str:
    if not isinstance(args, dict):
        return ""
    for key in ("command", "cmd", "script", "shell"):
        value = args.get(key)
        if isinstance(value, str):
            return value.strip()
    return ""

def _looks_like_internal_command(command: str) -> str | None:
    first = command.split("\n", 1)[0].strip()
    first = re.sub(r"^(?:/usr/bin/)?(?:bash|sh|zsh)\s+(?:-lc?|-c)\s+", "", first)
    token = re.split(r"[\s;&|()<>]", first, maxsplit=1)[0].strip("'\"`").lower()
    return token if token in INTERNAL_TOOLS else None

def _pre_tool_call(tool_name: str = "", args: Any = None, task_id: str = "", **kwargs: Any):
    del kwargs
    name = _tool(tool_name)
    task = _task(task_id)
    if name == "terminal":
        internal = _looks_like_internal_command(_command(args))
        if internal:
            return {"action": "block", "message": f"BLOQUEADO: {internal} es una herramienta Hermes, no un comando de terminal. Invócala mediante una llamada estructurada."}
    if name not in COMPOSIO_TOOLS:
        return None
    COUNTS[task] = COUNTS.get(task, 0) + 1
    if COUNTS[task] > MAX_CALLS_PER_TASK:
        return {"action": "block", "message": "BLOQUEADO: límite de 6 llamadas de integración por tarea; usa el resultado o el fallback."}
    if name == "composio_discover":
        DISCOVERY_COUNTS[task] = DISCOVERY_COUNTS.get(task, 0) + 1
        if DISCOVERY_COUNTS[task] > MAX_DISCOVERY_PER_TASK:
            return {"action": "block", "message": "BLOQUEADO: límite de 3 descubrimientos; evita repetir la exploración."}
    if name == "composio_read":
        if not isinstance(args, dict) or not args.get("toolkit") or not args.get("slug"):
            return {"action": "block", "message": "BLOQUEADO: composio_read requiere toolkit y slug; no se ejecutó una llamada incompleta."}
        if cooldown_active(str(args.get("toolkit"))):
            return {"action": "block", "message": "BLOQUEADO: esta integración está en pausa por 10 minutos tras un fallo; usa el fallback."}
    return None

def _pre_llm_call(user_message: str = "", **kwargs: Any):
    del kwargs
    text = str(user_message or "").lower()
    if not any(word in text for word in ("composio", "gmail", "correo", "github", "drive", "calendar", "linkedin", "youtube", "reddit", "notion", "twitter", " x ")):
        return None
    return {"context": "TOOL SAFETY: las herramientas registradas nunca son comandos de terminal. Usa llamadas estructuradas con sus parámetros requeridos; no inventes slugs. No repitas una herramienta fallida más de una vez: registra el fallo, activa el fallback definido y responde con evidencia verificable."}

def _post_tool_call(tool_name: str = "", status: str = "", result: Any = "", **kwargs: Any):
    del kwargs
    if _tool(tool_name) not in COMPOSIO_TOOLS:
        return None
    raw = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    if str(status).lower() in {"error", "failed", "failure"} or re.search(r"successful\"?\s*:\s*false|HTTP\s+40[23]|credits?\s+(?:depleted|exhausted)", raw, re.I):
        try:
            payload = json.loads(raw)
            toolkit = payload.get("toolkit", "") if isinstance(payload, dict) else ""
        except (TypeError, ValueError):
            toolkit = ""
        if toolkit:
            mark_failure(str(toolkit))
    return None

def register(ctx: Any) -> None:
    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("post_tool_call", _post_tool_call)
