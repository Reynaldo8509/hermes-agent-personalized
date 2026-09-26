"""On-demand, least-privilege bridge from VPS Hermes/MAX to Toshiba P50."""
from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml

from tools.registry import tool_error, tool_result

NODE_CONFIG = Path("$HOME/hermes/config/toshiba-node.yaml")
SERVICE = "hermes-node.service"

def _node_config() -> dict[str, Any]:
    try:
        data = yaml.safe_load(NODE_CONFIG.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError("Configuracion del nodo Toshiba no disponible.") from exc
    if not isinstance(data, dict) or data.get("node_id") != "toshiba-p50-kali":
        raise RuntimeError("Identidad del nodo Toshiba no valida.")
    if data.get("controller") != "MAX" or data.get("peer_nodes") != []:
        raise RuntimeError("Toshiba solo admite control de MAX y no tiene nodos pares.")
    required = ("transport", "endpoint", "ssh_user", "ssh_key", "known_hosts", "runner")
    if data.get("transport") != "tailscale" or any(not data.get(key) for key in required[1:]):
        raise RuntimeError("Configuracion Tailscale del nodo Toshiba incompleta.")
    return data
START_TIMEOUT = 30
TASK_TIMEOUT = 180

STATUS_SCHEMA = {
    "name": "toshiba_node_status",
    "description": "Comprueba el estado del nodo Toshiba P50 por Tailscale sin arrancar Hermes.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}
TASK_SCHEMA = {
    "name": "toshiba_node_task",
    "description": (
        "Ejecuta una tarea de usuario en toshiba-p50-kali bajo demanda por Tailscale. "
        "MAX debe usarlo solo cuando la tarea requiera capacidades de esa Toshiba: "
        "nmap, tshark, tcpdump, ss/ip, DNS, traceroute, arp-scan, lectura/escritura "
        "de artefactos o Python sin privilegios. El agente se inicia antes y se detiene "
        "al terminar; no permite root, firewall, reboot, Tailscale ni cambios de usuarios."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": ["exec", "read_file", "write_file"]},
            "argv": {"type": "array", "items": {"type": "string"}, "maxItems": 32},
            "path": {"type": "string", "maxLength": 512},
            "content": {"type": "string", "maxLength": 1000000},
        },
        "required": ["operation"],
        "additionalProperties": False,
    },
}


def _ssh_base() -> list[str]:
    cfg = _node_config()
    return [
        "ssh", "-i", str(cfg["ssh_key"]), "-o", "IdentitiesOnly=yes",
        "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={cfg['known_hosts']}", "-o", "ConnectTimeout=10",
        f"{cfg['ssh_user']}@{cfg['endpoint']}",
    ]


def _ssh(*remote_args: str, timeout: int = TASK_TIMEOUT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(_ssh_base() + list(remote_args), capture_output=True, text=True, timeout=timeout, check=False)


def _service_active() -> bool:
    result = _ssh("systemctl", "--user", "is-active", SERVICE, timeout=15)
    return result.returncode == 0 and result.stdout.strip() == "active"


def _start_if_needed() -> bool:
    if _service_active():
        return False
    result = _ssh("systemctl", "--user", "start", SERVICE, timeout=20)
    if result.returncode != 0:
        raise RuntimeError("No se pudo iniciar hermes-node.service en Toshiba.")
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        if _service_active():
            return True
        time.sleep(1)
    raise RuntimeError("Toshiba no alcanzó estado active dentro del tiempo esperado.")


def _stop_if_started(started: bool) -> None:
    if not started:
        return
    _ssh("systemctl", "--user", "stop", SERVICE, timeout=20)


def _payload(args: dict[str, Any]) -> str:
    raw = json.dumps(args, ensure_ascii=False, separators=(",", ":")).encode()
    return base64.b64encode(raw).decode("ascii")


def _run_runner(args: dict[str, Any]) -> dict[str, Any]:
    result = _ssh(str(_node_config()["runner"]), _payload(args), timeout=TASK_TIMEOUT)
    if not result.stdout.strip():
        raise RuntimeError("El runner de Toshiba no devolvió resultado.")
    try:
        data = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Resultado inválido del runner de Toshiba.") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Resultado no válido del runner de Toshiba.")
    if result.returncode != 0 or not data.get("ok"):
        return {"success": False, "node_id": _node_config()["node_id"], **data}
    return {"success": True, "node_id": _node_config()["node_id"], **data}


def _handle_status(_: dict[str, Any], **__: Any) -> str:
    try:
        cfg = _node_config()
        service = _ssh("systemctl", "--user", "is-active", SERVICE, timeout=15)
        ping = _ssh("true", timeout=15)
        return tool_result({
            "success": ping.returncode == 0,
            "node_id": cfg["node_id"],
            "transport": cfg["transport"],
            "tailscale_ip": cfg["endpoint"],
            "ssh_reachable": ping.returncode == 0,
            "hermes_service": service.stdout.strip() or "inactive",
            "on_demand": True,
        })
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        return tool_error(f"Estado de Toshiba no disponible: {type(exc).__name__}.")


def _handle_task(args: dict[str, Any], **__: Any) -> str:
    operation = str(args.get("operation") or "").strip()
    if operation not in {"exec", "read_file", "write_file"}:
        return tool_error("Operación no permitida.")
    if operation == "exec" and not isinstance(args.get("argv"), list):
        return tool_error("exec requiere argv como lista.")
    if operation in {"read_file", "write_file"} and not isinstance(args.get("path"), str):
        return tool_error(f"{operation} requiere path.")
    started = False
    try:
        started = _start_if_needed()
        result = _run_runner({k: v for k, v in args.items() if k in {"operation", "argv", "path", "content"}})
        return tool_result({"transport": "tailscale", "on_demand_started": started, **result})
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        return tool_error(f"Tarea Toshiba no ejecutada: {str(exc) or type(exc).__name__}")
    finally:
        _stop_if_started(started)


def register(ctx: Any) -> None:
    ctx.register_tool(name="toshiba_node_status", toolset="hermy-hq", schema=STATUS_SCHEMA, handler=_handle_status, emoji="🖥️")
    ctx.register_tool(name="toshiba_node_task", toolset="hermy-hq", schema=TASK_SCHEMA, handler=_handle_task, emoji="🖥️")
