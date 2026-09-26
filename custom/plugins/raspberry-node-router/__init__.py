"""Bridge bajo demanda de Hermes/MAX a RaspberryPi4 por Tailscale."""
from __future__ import annotations
import base64, json, subprocess
from pathlib import Path
from typing import Any
import yaml
from tools.registry import tool_error, tool_result

CFG = Path("$HOME/hermes/config/raspberry-node.yaml")

def config():
    try: data = yaml.safe_load(CFG.read_text())
    except (OSError, yaml.YAMLError) as exc: raise RuntimeError("config RaspberryPi4 no disponible") from exc
    req = ("transport", "endpoint", "ssh_user", "ssh_key", "known_hosts", "runner")
    if not isinstance(data, dict) or data.get("node_id") != "RaspberryPi4": raise RuntimeError("identidad RaspberryPi4 invalida")
    if data.get("controller") != "MAX" or data.get("peer_nodes") != []: raise RuntimeError("RaspberryPi4 solo admite MAX y no tiene pares")
    if data.get("transport") != "tailscale" or any(not data.get(x) for x in req[1:]): raise RuntimeError("config Tailscale incompleta")
    return data

def ssh():
    c = config()
    return ["ssh", "-i", str(c["ssh_key"]), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={c['known_hosts']}", "-o", "ConnectTimeout=10", f"{c['ssh_user']}@{c['endpoint']}"]

def run(args):
    c = config(); payload = base64.b64encode(json.dumps(args, separators=(",", ":")).encode()).decode("ascii")
    p = subprocess.run(ssh() + [f"python3 {c['runner']} {payload}"], capture_output=True, text=True, timeout=180, check=False)
    if not p.stdout.strip(): raise RuntimeError("runner RaspberryPi4 sin respuesta")
    try: data = json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, TypeError) as exc: raise RuntimeError("respuesta invalida del runner RaspberryPi4") from exc
    return {"success": p.returncode == 0 and bool(data.get("ok")), "node_id": c["node_id"], **data}

STATUS = {"name": "raspberry_node_status", "description": "Estado de RaspberryPi4 por Tailscale sin agente persistente.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}
TASK = {"name": "raspberry_node_task", "description": "Tarea bajo demanda en RaspberryPi4. Preserva Tailscale/exit node, nginx/Home Assistant y failover. No hay escritorio: cua-driver solo esta instalado.", "parameters": {"type": "object", "properties": {"operation": {"type": "string", "enum": ["exec", "sudo-exec", "network-status", "cua-status", "read_file", "write_file"]}, "argv": {"type": "array", "items": {"type": "string"}, "maxItems": 64}, "path": {"type": "string", "maxLength": 1024}, "content": {"type": "string", "maxLength": 1000000}}, "required": ["operation"], "additionalProperties": False}}

def status(_, **__):
    try:
        c = config(); p = subprocess.run(ssh() + ["hostname"], capture_output=True, text=True, timeout=15, check=False)
        return tool_result({"success": p.returncode == 0, "node_id": c["node_id"], "transport": c["transport"], "tailscale_ip": c["endpoint"], "ssh_reachable": p.returncode == 0, "hostname": p.stdout.strip(), "on_demand": True, "persistent_agent": False, "provides_exit_node": True, "desktop": False})
    except Exception as exc: return tool_error(f"RaspberryPi4 no disponible: {type(exc).__name__}")

def task(a, **__):
    op = str(a.get("operation") or "")
    if op not in {"exec", "sudo-exec", "network-status", "cua-status", "read_file", "write_file"}: return tool_error("operacion no permitida")
    if op in {"exec", "sudo-exec"} and not isinstance(a.get("argv"), list): return tool_error(f"{op} requiere argv")
    if op in {"read_file", "write_file"} and not isinstance(a.get("path"), str): return tool_error(f"{op} requiere path")
    try: return tool_result({"transport": "tailscale", "on_demand": True, **run({k: v for k, v in a.items() if k in {"operation", "argv", "path", "content"}})})
    except Exception as exc: return tool_error(f"RaspberryPi4 no ejecutada: {str(exc) or type(exc).__name__}")

def register(ctx: Any):
    ctx.register_tool(name="raspberry_node_status", toolset="hermy-hq", schema=STATUS, handler=status, emoji="🥧")
    ctx.register_tool(name="raspberry_node_task", toolset="hermy-hq", schema=TASK, handler=task, emoji="🥧")
