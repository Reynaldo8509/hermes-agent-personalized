"""On-demand, least-privilege bridge from VPS Hermes/MAX to Laptop-device_two."""
from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path
from typing import Any

import yaml

from tools.registry import tool_error, tool_result

NODE_CONFIG = Path("$HOME/hermes/config/device_two-node.yaml")
TASK_TIMEOUT = 180


def _node_config() -> dict[str, Any]:
    try:
        data = yaml.safe_load(NODE_CONFIG.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError("Configuracion del nodo Laptop-device_two no disponible.") from exc
    required = ("transport", "endpoint", "ssh_user", "ssh_key", "known_hosts", "runner")
    if not isinstance(data, dict) or data.get("node_id") != "Laptop-device_two":
        raise RuntimeError("Identidad del nodo Laptop-device_two no valida.")
    if data.get("controller") != "MAX" or data.get("peer_nodes") != []:
        raise RuntimeError("Laptop-device_two solo admite control de MAX y no tiene nodos pares.")
    if data.get("transport") != "tailscale" or any(not data.get(key) for key in required[1:]):
        raise RuntimeError("Configuracion Tailscale del nodo Laptop-device_two incompleta.")
    return data


STATUS_SCHEMA = {
    "name": "device_two_node_status",
    "description": "Comprueba el estado de Laptop-device_two por Tailscale sin iniciar un agente persistente.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}
TASK_SCHEMA = {
    "name": "device_two_node_task",
    "description": (
        "Ejecuta una tarea de usuario en Laptop-device_two bajo demanda por Tailscale, incluso sin sesión gráfica. "
        "Usar solo para capacidades de esa laptop: PowerShell, Python, red, DNS, "
        "nmap, tshark, tcpdump, netcat, lectura/escritura del workspace y Escritorios de perfiles Windows, "
        "además de descubrir las rutas de Escritorio. "
        "No permite root, exit-node, Tailscale, firewall, reboot ni cambios de usuarios."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": ["exec", "list-desktops", "network-status", "dns-resolve", "tcp-test", "read_file", "write_file"]},
            "argv": {"type": "array", "items": {"type": "string"}, "maxItems": 32},
            "hostname": {"type": "string", "maxLength": 253},
            "port": {"type": "integer", "minimum": 1, "maximum": 65535},
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


def _payload(args: dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(args, ensure_ascii=False, separators=(",", ":")).encode()).decode("ascii")


def _run_runner(args: dict[str, Any]) -> dict[str, Any]:
    cfg = _node_config()
    remote = f"powershell.exe -NoProfile -ExecutionPolicy Bypass -File {cfg['runner']} {_payload(args)}"
    result = subprocess.run(_ssh_base() + [remote], capture_output=True, text=True, timeout=TASK_TIMEOUT, check=False)
    if not result.stdout.strip():
        raise RuntimeError("El runner de Laptop-device_two no devolvio resultado.")
    try:
        data = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Resultado invalido del runner de Laptop-device_two.") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Resultado no valido del runner de Laptop-device_two.")
    return {"success": result.returncode == 0 and bool(data.get("ok")), "node_id": cfg["node_id"], **data}


def _handle_status(_: dict[str, Any], **__: Any) -> str:
    try:
        cfg = _node_config()
        result = subprocess.run(_ssh_base() + ["hostname"], capture_output=True, text=True, timeout=15, check=False)
        return tool_result({
            "success": result.returncode == 0,
            "node_id": cfg["node_id"],
            "transport": cfg["transport"],
            "tailscale_ip": cfg["endpoint"],
            "ssh_reachable": result.returncode == 0,
            "hostname": result.stdout.strip(),
            "on_demand": True,
            "persistent_agent": False,
        })
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        return tool_error(f"Estado de Laptop-device_two no disponible: {type(exc).__name__}.")


def _handle_task(args: dict[str, Any], **__: Any) -> str:
    operation = str(args.get("operation") or "").strip()
    allowed = {"exec", "list-desktops", "network-status", "dns-resolve", "tcp-test", "read_file", "write_file"}
    if operation not in allowed:
        return tool_error("Operacion no permitida.")
    if operation == "exec" and not isinstance(args.get("argv"), list):
        return tool_error("exec requiere argv como lista.")
    if operation in {"read_file", "write_file"} and not isinstance(args.get("path"), str):
        return tool_error(f"{operation} requiere path.")
    try:
        payload = {k: v for k, v in args.items() if k in {"operation", "argv", "hostname", "port", "path", "content"}}
        return tool_result({"transport": "tailscale", "on_demand": True, **_run_runner(payload)})
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        return tool_error(f"Tarea Laptop-device_two no ejecutada: {str(exc) or type(exc).__name__}")


def register(ctx: Any) -> None:
    ctx.register_tool(name="device_two_node_status", toolset="hermy-hq", schema=STATUS_SCHEMA, handler=_handle_status, emoji="💻")
    ctx.register_tool(name="device_two_node_task", toolset="hermy-hq", schema=TASK_SCHEMA, handler=_handle_task, emoji="💻")
