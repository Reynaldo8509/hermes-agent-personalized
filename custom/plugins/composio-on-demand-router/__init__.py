"""Cache-first, read-only bridge to Composio Connect."""
from __future__ import annotations
import hashlib, json, re, time
from pathlib import Path
from typing import Any
from hermes_constants import get_hermes_home

ROOT = get_hermes_home()
CACHE = ROOT / "cache" / "composio-integrations.json"
REGISTRY = ROOT / "integrations" / "direct-integrations.json"
SERVER = "composio-full-access"
TTL = 600
MCP_CALL: Any = None
DISABLED = {"twitter", "x"}
READ = {"GET", "LIST", "FETCH", "LOOKUP", "SEARCH", "READ", "RECENT", "FIND"}
WRITE = {"SEND", "DELETE", "UPDATE", "CREATE", "POST", "PUT", "PATCH", "MOVE", "ARCHIVE", "MODIFY", "ADD", "REMOVE", "REPLY", "FORWARD", "PUBLISH", "BUY", "UPLOAD", "SHARE"}
BOOTSTRAP = ("gmail", "googledrive")
PREFERRED: dict[str, str] = {}
FAILED: set[str] = set()

def _toolkit(value: object) -> str:
    name = str(value or "").strip().lower().replace("-", "_")
    if not re.fullmatch(r"[a-z0-9_]{2,64}", name):
        raise ValueError("toolkit inválido")
    if name in DISABLED:
        raise ValueError("X/Twitter no está habilitado en Hermes")
    return name

def _cache() -> dict:
    try:
        data = json.loads(CACHE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("integrations"), dict):
            return data
    except (OSError, ValueError, TypeError):
        pass
    return {"schema": 2, "generated_at": 0, "integrations": {}, "coverage": "parcial"}

def _save(data: dict) -> None:
    try:
        CACHE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(CACHE)
        CACHE.chmod(0o600)
    except OSError:
        pass

def _unwrap(envelope: Any) -> dict:
    if not isinstance(envelope, dict) or envelope.get("ok") is False:
        raise RuntimeError("falló la conexión MCP de Composio")
    value = envelope.get("result", envelope)
    if isinstance(value, dict) and isinstance(value.get("content"), list):
        for item in value["content"]:
            if isinstance(item, dict) and item.get("type") == "text":
                try:
                    parsed = json.loads(item.get("text", ""))
                    if isinstance(parsed, dict):
                        return parsed
                except (ValueError, TypeError):
                    continue
        raise RuntimeError("Composio devolvió una respuesta no interpretable")
    if isinstance(value, str):
        decoder = json.JSONDecoder()
        try:
            value, _end = decoder.raw_decode(value.lstrip())
        except ValueError as exc:
            raise RuntimeError("respuesta MCP de Composio inválida") from exc
    if not isinstance(value, dict):
        raise RuntimeError("respuesta MCP de Composio inválida")
    return value

def _mcp(name: str, args: dict, timeout: int = 60) -> dict:
    if not callable(MCP_CALL):
        raise RuntimeError("cliente MCP de Composio no enlazado")
    result = _unwrap(MCP_CALL(SERVER, name, args, timeout=timeout))
    if result.get("successful") is False or result.get("error"):
        data = result.get("data")
        message = data.get("message") if isinstance(data, dict) else None
        raise RuntimeError(str(message or result.get("error") or "Composio informó un error"))
    return result

def _data(response: dict) -> dict:
    value = response.get("data", {})
    return value if isinstance(value, dict) else {}

def _direct(toolkit: str | None = None) -> list:
    try:
        rows = json.loads(REGISTRY.read_text(encoding="utf-8")).get("integrations", [])
    except (OSError, ValueError, TypeError, AttributeError):
        return []
    if not isinstance(rows, list):
        return []
    return [{k: row.get(k) for k in ("toolkit", "id", "capabilities") if row.get(k) is not None}
            for row in rows if isinstance(row, dict) and
            (toolkit is None or str(row.get("toolkit", "")).lower() == toolkit)]

def _accounts(value: Any) -> list:
    if not isinstance(value, list):
        return []
    keys = ("id", "alias", "status", "is_default", "account_type")
    return [{k: row[k] for k in keys if row.get(k) is not None}
            for row in value if isinstance(row, dict)]

def _fresh(row: Any) -> bool:
    try:
        return time.time() - float(row.get("checked_at", 0)) < TTL
    except (AttributeError, TypeError, ValueError):
        return False

def _refresh(toolkits: list[str]) -> dict:
    cache = _cache()
    names = sorted({_toolkit(x) for x in toolkits if str(x).lower() not in DISABLED})
    if not names:
        return cache
    response = _mcp("COMPOSIO_MANAGE_CONNECTIONS",
                    {"toolkits": [{"name": n, "action": "list"} for n in names]})
    rows = _data(response).get("results", {})
    if not isinstance(rows, dict):
        rows = {}
    now = time.time()
    for name in names:
        row = rows.get(name, {})
        row = row if isinstance(row, dict) else {}
        accounts = _accounts(row.get("accounts"))
        status = str(row.get("status") or ("active" if accounts else "not_connected")).upper()
        entry = cache["integrations"].get(name, {})
        entry = entry if isinstance(entry, dict) else {}
        entry.update({"toolkit": name, "status": status, "accounts": accounts, "checked_at": now})
        cache["integrations"][name] = entry
    cache["generated_at"] = now
    cache["coverage"] = "toolkits consultados; Connect consumer key no enumera todas las conexiones del proyecto"
    _save(cache)
    return cache

def _status(name: str, force: bool = False) -> dict:
    cache = _cache()
    row = cache["integrations"].get(name)
    if force or not _fresh(row):
        cache = _refresh([name])
        row = cache["integrations"].get(name, {})
    return row if isinstance(row, dict) else {"toolkit": name, "status": "UNKNOWN", "accounts": []}

def _search_statuses(statuses: Any, cache: dict) -> None:
    if not isinstance(statuses, list):
        return
    for row in statuses:
        if not isinstance(row, dict) or not row.get("toolkit"):
            continue
        try:
            name = _toolkit(row["toolkit"])
        except ValueError:
            continue
        entry = cache["integrations"].get(name, {})
        entry = entry if isinstance(entry, dict) else {}
        entry.update({"toolkit": name,
                      "status": "ACTIVE" if row.get("has_active_connection") else "NOT_CONNECTED",
                      "accounts": _accounts(row.get("accounts")), "checked_at": time.time()})
        cache["integrations"][name] = entry

def _catalog_rows(cache: dict) -> list:
    rows = []
    for name, row in sorted(cache.get("integrations", {}).items()):
        if name in DISABLED or not isinstance(row, dict):
            continue
        rows.append({"toolkit": name, "status": row.get("status", "UNKNOWN"),
                     "accounts": [{"alias": a.get("alias"), "status": a.get("status"),
                                   "is_default": a.get("is_default")}
                                  for a in row.get("accounts", []) if isinstance(a, dict)],
                     "actions_cached": row.get("tools", []),
                     "read_tools_cached": row.get("read_tools", [])})
    return rows

def integration_catalog(force: bool = False) -> str:
    cache = _cache()
    names = set(cache["integrations"])
    stale = force or not names or any(not _fresh(cache["integrations"].get(n)) for n in names)
    if stale:
        statuses = []
        try:
            found = _mcp("COMPOSIO_SEARCH_TOOLS", {
                "queries": [{"use_case": "List connected apps and identify available read-only lookup tools"}],
                "session": {"generate_id": True}, "search_strategy": "tool_search"})
            statuses = _data(found).get("toolkit_connection_statuses", [])
        except Exception:
            pass
        _search_statuses(statuses, cache)
        names.update(BOOTSTRAP)
        names.update(row.get("toolkit") for row in statuses if isinstance(row, dict) and row.get("toolkit"))
        try:
            cache = _refresh(sorted(names))
        except Exception:
            _search_statuses(statuses, cache)
            cache["generated_at"] = time.time()
            _save(cache)
    rows = _catalog_rows(cache)
    return json.dumps({"success": True,
        "active_composio": [r for r in rows if r["status"] in ("ACTIVE", "CONNECTED")],
        "other_composio": [r for r in rows if r["status"] not in ("ACTIVE", "CONNECTED")],
        "direct": _direct(),
        "cache": {"path": str(CACHE), "ttl_seconds": TTL, "generated_at": cache.get("generated_at"),
                  "fresh": bool(cache.get("generated_at") and time.time()-float(cache["generated_at"]) < TTL)},
        "coverage": cache.get("coverage", "parcial; Connect permite consultar estado por toolkit")}, ensure_ascii=False)

def integration_status(toolkit: str) -> str:
    name = _toolkit(toolkit)
    row = _status(name)
    return json.dumps({"success": True, "toolkit": name, "direct": _direct(name),
                       "composio": {k: row.get(k) for k in ("status", "accounts", "checked_at")},
                       "cache_ttl_seconds": TTL, "source": "Composio Connect"}, ensure_ascii=False)

def _is_read(value: Any) -> bool:
    slug = str(value or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9_]{3,120}", slug):
        return False
    words = set(slug.split("_"))
    return bool(words & READ) and not bool(words & WRITE)

def composio_discover(toolkit: str, intent: str) -> str:
    name = _toolkit(toolkit)
    use = " ".join(str(intent or "").split())
    if not use or len(use) > 500:
        raise ValueError("intent debe tener entre 1 y 500 caracteres")
    status = _status(name)
    if status.get("status") not in ("ACTIVE", "CONNECTED"):
        return json.dumps({"success": False, "toolkit": name, "status": status.get("status"),
                           "result": "Sin conexión activa; no se buscaron herramientas."}, ensure_ascii=False)
    cache = _cache()
    entry = cache["integrations"].setdefault(name, {})
    key = hashlib.sha256(use.casefold().encode()).hexdigest()
    old = entry.setdefault("discoveries", {}).get(key)
    if isinstance(old, dict) and time.time()-float(old.get("cached_at", 0)) < TTL:
        return json.dumps({k:v for k,v in old.items() if k != "cached_at"} | {"cached": True}, ensure_ascii=False)
    response = _mcp("COMPOSIO_SEARCH_TOOLS", {
        "queries": [{"use_case": f"{use} using {name}"}],
        "session": {"generate_id": True}, "search_strategy": "tool_search"})
    data = _data(response)
    session = data.get("session", {})
    session_id = str(session.get("id") or "") if isinstance(session, dict) else ""
    _search_statuses(data.get("toolkit_connection_statuses"), cache)
    results = data.get("results", [])
    plan = results[0] if isinstance(results, list) and results and isinstance(results[0], dict) else {}
    candidates = []
    for field in ("primary_tool_slugs", "related_tool_slugs"):
        if isinstance(plan.get(field), list):
            candidates.extend(s for s in plan[field] if re.fullmatch(r"[A-Z0-9_]{3,120}", str(s).upper()))
    candidates = list(dict.fromkeys(str(s).upper() for s in candidates))[:12]
    schemas = {}
    if candidates:
        got = _mcp("COMPOSIO_GET_TOOL_SCHEMAS",
                   {"tool_slugs": candidates, "include": ["input_schema"], "session_id": session_id})
        schema_map = _data(got).get("tool_schemas", {})
        if isinstance(schema_map, dict):
            for slug in candidates:
                item = schema_map.get(slug)
                if not isinstance(item, dict) or not isinstance(item.get("input_schema"), dict):
                    continue
                owner = re.sub(r"[^A-Z0-9]", "", str(item.get("toolkit", "")).upper())
                expected = re.sub(r"[^A-Z0-9]", "", name.upper())
                if owner and owner != expected:
                    continue
                schemas[slug] = item["input_schema"]
    available = list(schemas)
    read_slugs = [slug for slug in available if _is_read(slug)]
    result = {"success": True, "toolkit": name, "intent_sha256": key,
              "available_tools": available, "read_tools": read_slugs,
              "input_schemas": schemas, "session_id": session_id,
              "guidance": plan.get("execution_guidance", "")}
    entry = cache["integrations"].setdefault(name, {})
    entry["tools"] = sorted(set(entry.get("tools", [])) | set(available))
    entry["read_tools"] = sorted(set(entry.get("read_tools", [])) | set(read_slugs))
    entry.setdefault("discoveries", {})[key] = {**result, "cached_at": time.time()}
    cache["generated_at"] = time.time()
    _save(cache)
    return json.dumps(result, ensure_ascii=False)

def composio_read(toolkit: str, slug: str, data_json: str = "{}", account: str = "") -> str:
    name, tool = _toolkit(toolkit), str(slug or "").strip().upper()
    if not _is_read(tool):
        return json.dumps({"success": False, "result": "BLOQUEADO: solo herramientas de lectura."}, ensure_ascii=False)
    try:
        args = json.loads(data_json or "{}")
        if not isinstance(args, dict) or len(json.dumps(args, ensure_ascii=False)) > 12000:
            raise ValueError
    except ValueError as exc:
        raise ValueError("data_json debe ser un objeto JSON de hasta 12 KB") from exc
    status = _status(name)
    if status.get("status") not in ("ACTIVE", "CONNECTED"):
        return json.dumps({"success": False, "toolkit": name, "result": "Sin conexión activa."}, ensure_ascii=False)
    cache = _cache()
    entry = cache["integrations"].get(name, {})
    discoveries = entry.get("discoveries", {}) if isinstance(entry, dict) else {}
    allowed = {s for d in discoveries.values() if isinstance(d, dict) and time.time()-float(d.get("cached_at", 0)) < TTL
               for s in d.get("read_tools", [])}
    if tool not in allowed:
        return json.dumps({"success": False, "toolkit": name,
                           "result": "BLOQUEADO: llama composio_discover antes para ese slug."}, ensure_ascii=False)
    accounts = [a for a in status.get("accounts", []) if isinstance(a, dict)
                and str(a.get("status", "ACTIVE")).upper() in ("ACTIVE", "CONNECTED")]
    selected = str(account or "").strip()
    if len(accounts) > 1 and not selected:
        return json.dumps({"success": False, "toolkit": name,
            "accounts": [{"alias": a.get("alias"), "is_default": a.get("is_default")} for a in accounts],
            "result": "Especifica account como alias o ID de la cuenta que deseas consultar."}, ensure_ascii=False)
    if not selected and accounts:
        selected = str(accounts[0].get("id") or accounts[0].get("alias") or "")
    session_id = next((str(d.get("session_id")) for d in discoveries.values()
                       if isinstance(d, dict) and tool in d.get("read_tools", []) and d.get("session_id")), "")
    call = {"tools": [{"tool_slug": tool, "arguments": args}],
            "sync_response_to_workbench": False, "current_step": "READ_ONLY_LOOKUP"}
    if selected:
        call["tools"][0]["account"] = selected
    if session_id:
        call["session_id"] = session_id
    try:
        response = _mcp("COMPOSIO_MULTI_EXECUTE_TOOL", call, timeout=90)
    except Exception:
        entry["checked_at"], entry["discoveries"] = 0, {}
        _save(cache)
        return json.dumps({"success": False, "toolkit": name, "tool": tool,
            "result": "Falló la consulta; el estado se invalidó para revalidarlo."}, ensure_ascii=False)
    payload = json.dumps(response.get("data", response), ensure_ascii=False)
    return json.dumps({"success": True, "toolkit": name, "tool": tool,
        "account": next((a.get("alias") for a in accounts if a.get("id") == selected or a.get("alias") == selected), selected),
        "result": payload[:16000]}, ensure_ascii=False)

def _route(task_id: str = "", user_message: str = "", **kwargs: Any):
    del kwargs
    text = " " + " ".join(str(user_message or "").lower().split()) + " "
    terms = {"gmail": ("gmail", "correo", "email", "inbox", "bandeja de entrada"),
        "googledrive": ("google drive", "drive"), "googlecalendar": ("google calendar", "calendario"),
        "github": ("github", "repositorio", "pull request", "issue"), "linkedin": ("linkedin",),
        "youtube": ("youtube",), "reddit": ("reddit",), "notion": ("notion",),
        "homeassistant": ("home assistant", "casa inteligente", "domótica"), "telegram": ("telegram",),
        "slack": ("slack",), "discord": ("discord",)}
    name = next((n for n, words in terms.items() if any(w in text for w in words)), None)
    if not name:
        return None
    if task_id:
        PREFERRED[str(task_id)] = name
    return {"context": f"INTEGRATION ROUTE: may need {name}. Use integration_catalog (cache first), integration_status, composio_discover, then composio_read with a discovered read-only slug. Do not use the parallel Hermy HQ Gmail account unless the user selected that route. Do not invoke Composio MCP meta-tools directly or use web search before checking this route."}

def _block_web(tool_name: str = "", task_id: str = "", **kwargs: Any):
    del kwargs
    if tool_name == "web_search" and task_id in PREFERRED and task_id not in FAILED:
        return {"action": "block", "message": "Comprueba estado, descubrimiento y lectura de la integración; web search es fallback tras un fallo confirmado."}
    return None

def _schema(name: str, description: str, properties: dict, required: list) -> dict:
    return {"name": name, "description": description, "parameters": {"type": "object", "properties": properties,
            "required": required, "additionalProperties": False}}

def _handler(function):
    def call(params: dict, **kwargs):
        try:
            value = function(**params)
            try:
                decoded = json.loads(value)
                if kwargs.get("task_id") and decoded.get("success") is False:
                    FAILED.add(str(kwargs["task_id"]))
            except (ValueError, TypeError, AttributeError):
                pass
            return json.dumps({"result": value}, ensure_ascii=False)
        except (ValueError, RuntimeError, PermissionError) as exc:
            return json.dumps({"result": f"ERROR: {exc}"}, ensure_ascii=False)
    return call

def register(ctx: Any) -> None:
    global MCP_CALL
    MCP_CALL = ctx.call_mcp
    ctx.register_tool(name="integration_catalog", toolset="composio-on-demand",
        schema=_schema("integration_catalog", "Consulta caché de conexiones Composio e integraciones nativas; refresca conexiones conocidas cada 10 minutos.",
                       {"force": {"type": "boolean", "default": False}}, []), handler=_handler(integration_catalog))
    ctx.register_tool(name="integration_status", toolset="composio-on-demand",
        schema=_schema("integration_status", "Comprueba si una integración concreta está activa en Composio y Hermes.",
                       {"toolkit": {"type": "string"}}, ["toolkit"]), handler=_handler(integration_status))
    ctx.register_tool(name="composio_discover", toolset="composio-on-demand",
        schema=_schema("composio_discover", "Descubre y cachea herramientas y esquemas de lectura para una consulta; no inventes slugs.",
                       {"toolkit": {"type": "string"}, "intent": {"type": "string"}}, ["toolkit", "intent"]),
        handler=_handler(composio_discover))
    ctx.register_tool(name="composio_read", toolset="composio-on-demand",
        schema=_schema("composio_read", "Lee datos de Composio solo con slug descubierto; bloquea herramientas con efectos de escritura.",
                       {"toolkit": {"type": "string"}, "slug": {"type": "string"}, "data_json": {"type": "string", "default": "{}"},
                        "account": {"type": "string"}}, ["toolkit", "slug"]), handler=_handler(composio_read))
    ctx.register_hook("pre_llm_call", _route)
    ctx.register_hook("pre_tool_call", _block_web)

