"""Small, deterministic intent router for Hermes' local Qwen route.

It does not call another model.  Clearly informational requests receive a
short per-turn policy and accidental tool calls are blocked before execution.
Operational, live-state, and ambiguous requests remain fully capable.
"""
from __future__ import annotations

import re
import time
from typing import Any

_DIRECT: dict[str, float] = {}
_TTL_SECONDS = 900
_ACTION = re.compile(
    r"\b(?:ejecuta|haz|revisa|verifica|comprueba|conecta(?:te)?|instala|"
    r"actualiza|reinicia|apaga|enciende|abre|cierra|busca|escanea|crea|"
    r"modifica|cambia|elimina|borra|sube|baja|envia|manda|lanza|corre|"
    r"run|execute|check|connect|install|update|restart|scan|delete)\b",
    re.IGNORECASE,
)
_LIVE = re.compile(
    r"\b(?:estado|activo|disponible|conectado|funcionando|ahora|hoy|actual|"
    r"en vivo|ultimo|último|log(?:s)?|error(?:es)?|servicio|proceso|"
    r"max|milo|domus|alexa|home assistant|telegram|red local|dispositivo)\b",
    re.IGNORECASE,
)
_DIRECT_CUE = re.compile(
    r"(?:^|\b)(?:que es|qué es|explica|define|resum[ei]|compara|por que|"
    r"por qué|como funciona|cómo funciona|diferencia|recomienda|opinion|"
    r"opinión|ayuda|cuentame|cuéntame)\b|\?",
    re.IGNORECASE,
)


def _normalise(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def classify(text: Any) -> str:
    """Return direct, operational, or ambiguous without LLM inference."""
    message = _normalise(text)
    if not message:
        return "ambiguous"
    if _ACTION.search(message) or _LIVE.search(message):
        return "operational"
    if _DIRECT_CUE.search(message):
        return "direct"
    return "ambiguous"


def _prune() -> None:
    cutoff = time.monotonic() - _TTL_SECONDS
    for task_id, created in tuple(_DIRECT.items()):
        if created < cutoff:
            _DIRECT.pop(task_id, None)


def _pre_llm_call(user_message: str = "", task_id: str = "", **kwargs: Any):
    del kwargs
    _prune()
    if classify(user_message) != "direct":
        return None
    if task_id:
        _DIRECT[str(task_id)] = time.monotonic()
    return {
        "context": (
            "ROUTER DE INTENCIÓN: esta es una consulta informativa y no requiere "
            "estado en vivo ni una acción. Responde directamente, de forma breve y "
            "técnica. No uses herramientas, no simules verificaciones y no inventes "
            "datos del entorno. Si falta un hecho local específico, indícalo."
        )
    }


def _pre_tool_call(tool_name: str = "", task_id: str = "", **kwargs: Any):
    del kwargs
    _prune()
    if str(task_id) not in _DIRECT:
        return None
    return {
        "action": "block",
        "message": (
            "BLOQUEADO POR ROUTER: esta consulta fue clasificada como informativa. "
            "Responde directamente sin ejecutar herramientas."
        ),
    }


def register(ctx: Any) -> None:
    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_hook("pre_tool_call", _pre_tool_call)
