"""Read the latest authenticated mobile network/location reports for Hermes."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tools.registry import tool_error, tool_result

_STATE_FILE = Path("$HOME/hermes/network-ingest/state/state.json")
_SCHEMA: dict[str, Any] = {
    "name": "hermes_network_presence",
    "description": (
        "Consulta los últimos informes recibidos desde el atajo Hermes de iPhone: "
        "ubicación, red Wi-Fi, IP local y hora de Ecuador (America/Guayaquil). "
        "Indica siempre la hora local; nunca conviertas ni presentes UTC; "
        "si el dato está antiguo, no lo presentes como ubicación en tiempo real."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "device": {"type": "string", "description": "Nombre del dispositivo opcional; vacío devuelve todos."}
        },
        "additionalProperties": False,
    },
}


def _handle_presence(args: dict[str, Any], **_: Any) -> str:
    requested = " ".join(str(args.get("device") or "").split()).casefold()
    try:
        state = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return tool_result({"success": True, "status": "no_reports", "message": "Todavía no se ha recibido ningún informe del atajo."})
    except (OSError, json.JSONDecodeError):
        return tool_error("El archivo privado de presencia no se pudo leer como JSON válido.")
    devices = state.get("devices") if isinstance(state, dict) else None
    if not isinstance(devices, dict):
        return tool_error("El archivo de presencia no tiene el formato esperado.")
    selected = [value for name, value in devices.items()
                if isinstance(value, dict) and (not requested or requested in str(name).casefold())]
    if requested and not selected:
        return tool_result({"success": True, "status": "device_not_found", "updated_at": state.get("updated_at"), "devices": []})
    return tool_result({
        "success": True,
        "status": "available" if selected else "no_reports",
        "updated_at": state.get("updated_at"),
        "devices": selected,
    })


def register(ctx: Any) -> None:
    ctx.register_tool(
        name="hermes_network_presence",
        toolset="network_presence",
        schema=_SCHEMA,
        handler=_handle_presence,
        emoji="📍",
    )
