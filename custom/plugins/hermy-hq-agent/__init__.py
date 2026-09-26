"""Hermy HQ agent-state and Max delegation tools for the VPS Hermes profile."""

from __future__ import annotations

import json
import os
import re
import subprocess
import asyncio
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from tools.registry import tool_error, tool_result

_HQ_URL = os.environ.get("HERMY_HQ_URL", "http://192.0.2.10:3200").rstrip("/")
_HQ_ENV_FILE = Path("$HOME/hermes/hq/.env")
_ALLOWED_AGENT_IDS = {"max", "codex", "atlas", "aegis", "milo", "pulse", "ledger", "domus"}
_APP_ROOT = Path("$HOME/hermes/app")
_LOCAL_RUNTIME_TERMS = re.compile(
    r"\b(?:version|versi[oó]n|commit|revision|revisi[oó]n|estado)\b.*\b(?:hermes|sistema|instalad|ejecut|esta[á]s|donde est[aá]s)\b"
    r"|\bmodelo\s+y\s+versi[oó]n\s+actual\b",
    re.IGNORECASE,
)
_EXTERNAL_VERSION_TERMS = re.compile(r"\b(?:[uú]ltim[ao]s?|publicad[ao]|upstream|nous|github|release|lanzamiento)\b", re.IGNORECASE)
_MAX_STATUS_TERMS = re.compile(r"\b(?:consulta(?:r)?|pregunta(?:r)?|estado|activo|actividad|tarea)\b.*\bmax\b|\bmax\b.*\b(?:estado|activo|actividad|tarea)\b", re.IGNORECASE)

HERMY_AGENT_STATUS_SCHEMA: Dict[str, Any] = {
    "name": "hermy_agent_status",
    "description": "Consulta el estado real, tarea actual y actividad reciente de Max u otro agente de Hermy HQ.",
    "parameters": {
        "type": "object",
        "properties": {"agent_id": {"type": "string", "description": "ID del agente; omitir para listar todos."}},
        "additionalProperties": False,
    },
}

HERMY_DELEGATE_TO_MAX_SCHEMA: Dict[str, Any] = {
    "name": "hermy_delegate_to_max",
    "description": "Encola una tarea no física para Max en Hermy HQ. Max decide si la resuelve o la delega a sus subordinados.",
    "parameters": {
        "type": "object",
        "properties": {
            "instruction": {"type": "string", "description": "Objetivo concreto para Max."},
            "title": {"type": "string", "description": "Título breve opcional."},
        },
        "required": ["instruction"],
        "additionalProperties": False,
    },
}

HERMES_RUNTIME_STATUS_SCHEMA: Dict[str, Any] = {
    "name": "hermes_runtime_status",
    "description": "Obtiene la versión Git, revisión, servicio y salud del Hermes local que procesa esta conversación. Úsala para 'qué versión se ejecuta aquí', 'sistema donde estás instalado' o estado local; no uses web_search ni Home Assistant.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}


def _internal_secret() -> Optional[str]:
    """Read only the shared internal API secret; never return or log it."""
    try:
        for line in _HQ_ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("INTERNAL_API_SECRET="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                return value or None
    except OSError:
        return None
    return None


def _available() -> bool:
    return bool(_internal_secret())


def _request(method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Any:
    secret = _internal_secret()
    if not secret:
        raise RuntimeError("La credencial interna de Hermy HQ no está disponible.")
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        f"{_HQ_URL}{path}", data=body, method=method,
        headers={"x-internal-secret": secret, "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"Hermy HQ devolvió HTTP {exc.code}.") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError("Hermy HQ no está disponible en la red privada.") from exc


def _handle_status(args: Dict[str, Any], **_: Any) -> str:
    requested = str(args.get("agent_id") or "").strip().lower()
    if requested and requested not in _ALLOWED_AGENT_IDS:
        return tool_error("Agente no reconocido. Usa Max, Codex, Atlas, Aegis, Milo, Pulse, Ledger o Domus.")
    try:
        agents = _request("GET", "/api/agents")
    except RuntimeError as exc:
        return tool_error(str(exc))
    if not isinstance(agents, list):
        return tool_error("Hermy HQ devolvió un estado de agentes no válido.")
    selected = [agent for agent in agents if str(agent.get("id", "")).lower() == requested] if requested else agents
    if requested and not selected:
        return tool_error(f"No hay estado disponible para {requested}.")
    return tool_result({"success": True, "agents": selected})


def _handle_delegate_to_max(args: Dict[str, Any], **_: Any) -> str:
    instruction = " ".join(str(args.get("instruction") or "").split())
    if not instruction or len(instruction) > 6000:
        return tool_error("La instrucción para Max debe contener entre 1 y 6000 caracteres.")
    title = " ".join(str(args.get("title") or "").split()) or f"Hermes: {instruction[:160]}"
    try:
        response = _request("POST", "/api/hermes/dispatch", {
            "kind": "max.chief-of-staff",
            "title": title[:200],
            "prompt": instruction,
            "sideEffecting": False,
        })
    except RuntimeError as exc:
        return tool_error(str(exc))
    request_data = response.get("request", {}) if isinstance(response, dict) else {}
    return tool_result({
        "success": True,
        "delegated_to": "max",
        "request_id": request_data.get("id"),
        "status": request_data.get("status"),
        "message": "Tarea encolada para Max; Max decidirá la delegación interna.",
    })


def _command_output(*command: str) -> str:
    """Run a fixed local status command without accepting model input."""
    try:
        completed = subprocess.run(
            list(command), text=True, capture_output=True, timeout=10, check=False,
        )
    except OSError:
        return "unavailable"
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else "unavailable"


def _configured_model() -> str:
    """Read the configured runtime model without exposing secrets or making network calls."""
    try:
        import yaml
        config = yaml.safe_load((Path.home() / "hermes/config.yaml").read_text(encoding="utf-8")) or {}
        model = config.get("model", {}) if isinstance(config, dict) else {}
        if isinstance(model, dict):
            provider = str(model.get("provider") or "")
            name = str(model.get("default") or "")
            return "/".join(part for part in (provider, name) if part) or "unavailable"
    except Exception:
        pass
    return "unavailable"


def _installed_hermes_version() -> str:
    """Return package metadata for the running Hermes installation, never web data."""
    try:
        from importlib.metadata import version
        return version("hermes-agent")
    except Exception:
        return "unavailable"


def _handle_runtime_status(_: Dict[str, Any], **__: Any) -> str:
    return tool_result({
        "success": True,
        "runtime": "vps",
        "git_revision": _command_output("git", "-C", str(_APP_ROOT), "rev-parse", "HEAD"),
        "git_describe": _command_output("git", "-C", str(_APP_ROOT), "describe", "--always", "--tags", "--dirty"),
        "commit_subject": _command_output("git", "-C", str(_APP_ROOT), "log", "-1", "--format=%s"),
        "installed_version": _installed_hermes_version(),
        "model": _configured_model(),
        "hermes_service": _command_output("systemctl", "is-active", "hermes-vps.service"),
        "gateway_service": _command_output("systemctl", "is-active", "hermes-gateway-vps.service"),
    })


def _pre_llm_call(user_message: str = "", **_: Any) -> Optional[Dict[str, str]]:
    text = str(user_message or "")
    if _MAX_STATUS_TERMS.search(text):
        return {"context": "RUTA OBLIGATORIA: para consultar estado, actividad o tarea de Max usa hermy_agent_status con agent_id='max'. No uses Home Assistant, web_search ni inventes una consulta a Max."}
    if _LOCAL_RUNTIME_TERMS.search(text) and not _EXTERNAL_VERSION_TERMS.search(text):
        return {"context": "RUTA OBLIGATORIA: esta es una pregunta sobre el Hermes local que procesa el mensaje. Usa hermes_runtime_status y responde con su resultado. No uses web_search ni herramientas de Home Assistant."}
    return None


def _pre_gateway_dispatch(event: Any, gateway: Any = None, **_: Any) -> Optional[Dict[str, str]]:
    """Answer deterministic local-status requests without queueing behind Qwen."""
    if gateway is None:
        return None
    text = str(getattr(event, "text", "") or "")
    wants_runtime = bool(_LOCAL_RUNTIME_TERMS.search(text)) and not bool(_EXTERNAL_VERSION_TERMS.search(text))
    wants_max = bool(_MAX_STATUS_TERMS.search(text))
    if not (wants_runtime or wants_max):
        return None
    source = getattr(event, "source", None)
    adapter = gateway._adapter_for_source(source) if source else None
    chat_id = getattr(source, "chat_id", None) if source else None
    if not adapter or not chat_id:
        return None
    facts = []
    if wants_runtime:
        runtime = json.loads(_handle_runtime_status({}))
        facts.append(
            "Hermes VPS: versión instalada "
            f"v{runtime.get('installed_version', 'no disponible')} · revisión {runtime.get('git_describe', 'no disponible')} · modelo {runtime.get('model', 'no disponible')} · servicio {runtime.get('hermes_service', 'no disponible')} · gateway {runtime.get('gateway_service', 'no disponible')}."
        )
    if wants_max:
        state = json.loads(_handle_status({"agent_id": "max"}))
        agents = state.get("agents", []) if isinstance(state, dict) else []
        if agents:
            agent = agents[0]
            facts.append(f"Max: {agent.get('status', 'estado no disponible')}.")
        else:
            facts.append("Max: estado no disponible.")
    asyncio.get_running_loop().create_task(adapter.send(chat_id, "HECHO VERIFICADO: " + " ".join(facts)))
    return {"action": "skip", "reason": "deterministic-hermes-hermy-status"}


def _pre_tool_call(tool_name: str = "", args: Any = None, **_: Any) -> Optional[Dict[str, str]]:
    name = str(tool_name or "").lower()
    values = args if isinstance(args, dict) else {}
    if name == "web_search":
        query = str(values.get("query") or "")
        if re.search(r"\bversi[oó]n\s+actual\b.*\bhermes\b|\bhermes\b.*\bversi[oó]n\s+actual\b", query, re.IGNORECASE):
            return {"action": "block", "message": "BLOQUEADO: la versión del Hermes en ejecución es un dato local. Usa hermes_runtime_status. Para la última versión publicada, formula explícitamente una consulta de upstream."}
    if name == "ha_list_services" and str(values.get("domain") or "").lower() in {"hermes", "local", "system"}:
        return {"action": "block", "message": "BLOQUEADO: Max y Hermes no son servicios de Home Assistant. Usa hermy_agent_status para Max o hermes_runtime_status para el runtime local."}
    return None


_CONTROL_AGENT_IDS = ("max", "codex", "atlas", "aegis", "pulse", "ledger", "domus", "milo")
_TELEGRAM_CONTROL_COMMANDS = tuple(
    f"{action}_{agent}" for action in ("pause", "resume") for agent in _CONTROL_AGENT_IDS
)


def _control_command_fallback(raw_args: str = "") -> str:
    """Fail safe if the gateway interceptor is unavailable."""
    return "Telegram control authorization failed; no change was applied."


def register(ctx: Any) -> None:
    ctx.register_tool(
        name="hermy_agent_status", toolset="hermy_hq", schema=HERMY_AGENT_STATUS_SCHEMA,
        handler=_handle_status, check_fn=_available, emoji="🧭",
    )
    ctx.register_tool(
        name="hermy_delegate_to_max", toolset="hermy_hq", schema=HERMY_DELEGATE_TO_MAX_SCHEMA,
        handler=_handle_delegate_to_max, check_fn=_available, emoji="🧭",
    )
    ctx.register_tool(
        name="hermes_runtime_status", toolset="hermy_hq", schema=HERMES_RUNTIME_STATUS_SCHEMA,
        handler=_handle_runtime_status, emoji="🧭",
    )
    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("pre_gateway_dispatch", _pre_gateway_dispatch)

    for command_name in _TELEGRAM_CONTROL_COMMANDS:
        action, agent_id = command_name.split("_", 1)
        label = "Pause" if action == "pause" else "Resume"
        ctx.register_command(
            command_name, _control_command_fallback,
            f"{label} agent {agent_id.title()} from Telegram",
        )
        # Gateway dispatch normalizes underscores to hyphens internally.
        ctx.register_command(
            command_name.replace("_", "-"), _control_command_fallback,
            f"{label} agent {agent_id.title()} from Telegram",
        )
