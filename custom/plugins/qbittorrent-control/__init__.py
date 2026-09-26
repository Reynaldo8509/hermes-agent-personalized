"""Deterministic, bounded qBittorrent controls for authorized magnet links."""
import asyncio, concurrent.futures, importlib.util, json, os, re, sys, time, urllib.parse, urllib.request
from pathlib import Path
from agent.secret_scope import get_secret


def _load_torrent_search_module():
    module_name = "hermes_qbittorrent_control_torrent_search"
    loaded = sys.modules.get(module_name)
    if loaded is not None:
        return loaded
    source = Path(__file__).with_name("torrent_search.py")
    spec = importlib.util.spec_from_file_location(module_name, source)
    if spec is None or spec.loader is None:
        raise ImportError("torrent_search.py could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_torrent_search_module = _load_torrent_search_module()
_execute_torrent_search = _torrent_search_module.execute


def _set_operation_cancellation_event(event):
    return _torrent_search_module.set_operation_cancellation_event(event)


def _reset_operation_cancellation_event(token):
    _torrent_search_module.reset_operation_cancellation_event(token)

MEDIA_RE = re.compile(r"\.(?:mp4|mkv|avi|mov|webm|m4v|mp3|flac|wav|aac|ogg|jpg|jpeg|png|gif|srt|vtt)(?:[?#].*)?$", re.I)
MEDIA_PATH = "/mnt/raspberrypi4-ssd/media"
FILES_PATH = "/mnt/raspberrypi4-ssd/Descargas"
BTIH_RE = re.compile(r"^(?:[0-9a-f]{40}|[a-z2-7]{32})$", re.I)

def _run(coro):
    try: asyncio.get_running_loop()
    except RuntimeError: return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result(timeout=20)

async def _ha(method, path, payload=None):
    import aiohttp
    base=(get_secret("HASS_URL","http://127.0.0.1:8123") or "").rstrip("/")
    token=get_secret("HASS_TOKEN","") or ""
    if not token: raise RuntimeError("Home Assistant no tiene token configurado")
    headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        async with s.request(method,base+path,headers=headers,json=payload) as r:
            body=await r.text()
            if r.status>=400: raise RuntimeError(f"Home Assistant HTTP {r.status}")
            return json.loads(body) if body else {}

def _go_url():
    return (get_secret("GOPEED_URL","") or "").rstrip("/")

def _go_req(method,path,payload=None):
    url=_go_url()+path; token=get_secret("GOPEED_API_TOKEN","") or ""
    if not url or not token: raise RuntimeError("Gopeed no tiene URL o token configurado")
    data=json.dumps(payload).encode() if payload is not None else None
    req=urllib.request.Request(url,data=data,method=method,headers={"X-Api-Token":token,"Content-Type":"application/json","Accept":"application/json"})
    with urllib.request.urlopen(req,timeout=15) as r: return json.loads(r.read(512*1024)) if r.readable() else {}

def _q_req(method,path,fields=None):
    base=(os.environ.get("QBITTORRENT_URL","") or "").rstrip("/")
    if not base: raise RuntimeError("qBittorrent no tiene URL configurada")
    api_key=os.environ.get("QBITTORRENT_API_KEY","") or ""
    if not api_key: raise RuntimeError("qBittorrent no tiene API key configurada")
    fields=fields or {}
    if method == "GET":
        query=urllib.parse.urlencode(fields)
        url=base+path+("?"+query if query else "")
        data=None
    else:
        url=base+path
        data=urllib.parse.urlencode(fields).encode()
    req=urllib.request.Request(url,data=data,method=method,headers={"Authorization":f"Bearer {api_key}","Content-Type":"application/x-www-form-urlencoded","Accept":"application/json"})
    with urllib.request.urlopen(req,timeout=15) as r: return r.read().decode(errors="replace")

def _extract_magnet(raw):
    magnet=(raw or "").strip()
    if len(magnet)>4096 or any(ord(c)<32 or c.isspace() for c in magnet):
        raise ValueError("el magnet contiene espacios, saltos de línea o tamaño no permitido")
    if magnet.lower().count("magnet:?") != 1 or not magnet.lower().startswith("magnet:?"):
        raise ValueError("se requiere un único enlace magnet")
    parsed=urllib.parse.urlsplit(magnet)
    values=urllib.parse.parse_qs(parsed.query,keep_blank_values=True).get("xt",[])
    btih=[x[9:] for x in values if x.lower().startswith("urn:btih:")]
    if len(values)!=1 or len(btih)!=1 or not BTIH_RE.fullmatch(btih[0]):
        raise ValueError("el magnet debe contener un BTIH hexadecimal o base32 válido")
    return magnet, btih[0].lower()

def _torrent_info(infohash):
    raw=_q_req("GET","/api/v2/torrents/info",{"hashes":infohash})
    try: data=json.loads(raw or "[]")
    except json.JSONDecodeError as exc: raise RuntimeError("respuesta qBittorrent inválida") from exc
    return data if isinstance(data,list) else []

def _add_magnet(raw,path):
    try: magnet,infohash=_extract_magnet(raw)
    except ValueError as exc: return f"Magnet rechazado: {exc}."
    try:
        existing=_torrent_info(infohash)
        if existing:
            return "ℹ️ El torrent ya estaba registrado en qBittorrent."
        _q_req("POST","/api/v2/torrents/add",{"urls":magnet,"savepath":path})
        for _ in range(10):
            found=_torrent_info(infohash)
            if found:
                item=found[0] if isinstance(found[0],dict) else {}
                name=item.get("name") or "pendiente"
                size=item.get("size")
                size_text=f"\nTamaño: {size} bytes" if isinstance(size,(int,float)) and size else ""
                return f"✅ Descarga añadida a qBittorrent\nNodo: Raspberry Pi 4\nEstado: En cola\nNombre: {name}{size_text}"
            time.sleep(0.5)
        return "❌ qBittorrent aceptó la solicitud, pero el torrent no apareció en la verificación posterior."
    except Exception as exc:
        return f"No pude iniciar la descarga: {type(exc).__name__}."

def _valid_url(url):
    return bool(re.match(r"^(?:https?://|magnet:\?)\S+$",url,re.I)) and len(url)<=4096

def _kind_url(raw):
    parts=raw.split()
    if not parts: return None,None
    url=parts[-1]
    if not _valid_url(url): return None,None
    media=any(x in raw.casefold() for x in ("media","video","pelicula","musica","audio","imagen")) or bool(MEDIA_RE.search(url))
    return url, MEDIA_PATH if media else FILES_PATH

def _add(raw, forced_path=None):
    text=(raw or "").strip()
    if text.lower().startswith("magnet:"):
        return _add_magnet(text,forced_path or FILES_PATH)
    url,path=_kind_url(text)
    if not url: return "Uso: /descarga media <magnet> o /descarga archivo <magnet>."
    try:
        if url.lower().endswith(".torrent"):
            return _add_magnet(url,forced_path or path)
        _go_req("POST","/api/v1/tasks",{"req":{"url":url},"opt":{"path":forced_path or path}})
        return f"Gopeed: descarga enviada a {forced_path or path}."
    except Exception as exc:
        return f"No pude iniciar la descarga: {type(exc).__name__}."

def _generic_add(raw):
    parts=(raw or "").strip().split(None,1)
    if len(parts)!=2 or parts[0].casefold() not in {"media","archivo","descargas"}:
        return "Uso: /descarga media <magnet> o /descarga archivo <magnet>."
    return _add(parts[1],MEDIA_PATH if parts[0].casefold()=="media" else FILES_PATH)


_MEDIA_INTENT_PREFIX = re.compile(
    r"^(?:descarga|descargar|download|busca|buscar|search|find|obt[eé]n|obten|consigue|quiero|dame|trae)\b[,:;.!\s]*",
    re.I,
)
_MEDIA_COPY_PREFIX = re.compile(
    r"^(?:una?|single|one)\s+(?:(?:[úu]nica|unica|solo|sola|single)\s+)?(?:copia|copy|archivo|file)\s+de\s+",
    re.I,
)
_MEDIA_VERSION_WORD = re.compile(r"\bversi[oó]n\b|\bversion\b", re.I)


def _extract_media_query(raw):
    """Extract a content query without moving policy decisions into Telegram routing."""
    text = " ".join(str(raw or "").split()).strip()
    if not text:
        return ""
    text = _MEDIA_INTENT_PREFIX.sub("", text, count=1)
    text = _MEDIA_COPY_PREFIX.sub("", text, count=1)
    text = _MEDIA_VERSION_WORD.sub(" ", text)
    text = re.sub(r"\b(?:por favor|please)\b", " ", text, flags=re.I)
    text = re.sub(r"\s*[,;:]\s*", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" \t\r\n,;:.")
    return text


def _is_direct_media_request(raw):
    text = str(raw or "").strip()
    if text.casefold().startswith("magnet:"):
        return True
    parts = text.split()
    return bool(parts and _valid_url(parts[-1]))


def _compact_size(value):
    try:
        size = float(value)
    except (TypeError, ValueError):
        return "no disponible"
    if size <= 0:
        return "no disponible"
    return f"{size:.2f}".rstrip("0").rstrip(".") + " GiB"


def _compact_language(value):
    return {
        "LAT": "LAT",
        "EN": "English",
        "UNKNOWN": "Desconocido",
    }.get(str(value or "").upper(), str(value or "Desconocido"))


def _compact_error(code):
    return {
        "BTDIG_NO_MATCHES": "BTDig no encontró coincidencias",
        "BTDIG_HTTP_ERROR": "BTDig devolvió un error HTTP",
        "BTDIG_RATE_LIMIT": "BTDig aplicó un límite temporal",
        "BTDIG_PARSER_ERROR": "No se pudo interpretar la respuesta de BTDig",
        "METADATA_TIMEOUT": "El torrent no entregó metadata a tiempo",
        "NO_VALID_VIDEO_FILE": "Ningún archivo de video cumplió las políticas",
        "NO_VALID_CANDIDATES": "Ningún candidato cumplió las políticas",
        "CLEANUP_FAILED": "No se pudo limpiar el candidato temporal",
        "START_UNCERTAIN": "El inicio no pudo confirmarse; no se probó otro candidato",
        "QBITTORRENT_UNAVAILABLE": "qBittorrent no está disponible",
        "REQUEST_CANCELLED": "La solicitud fue cancelada",
        "ALREADY_DOWNLOADING": "La solicitud ya tiene una descarga en curso",
        "DUPLICATE_TORRENT": "La misma solicitud ya fue iniciada",
    }.get(str(code or ""), str(code or "Error no especificado"))


def _format_torrent_search_reply(raw):
    """Keep Telegram replies short while retaining the structured tool contract."""
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return str(raw or "")
    if not isinstance(payload, dict):
        return str(raw or "")

    selected = payload.get("selected") or {}
    preferences = payload.get("preferences") or {}
    if payload.get("ok") and payload.get("state") == "started":
        file_name = str(selected.get("file_name") or selected.get("torrent_name") or "no disponible")
        file_name = file_name.rsplit("/", 1)[-1]
        extension = str(preferences.get("extension") or "").lstrip(".").upper()
        if not extension and "." in file_name:
            extension = file_name.rsplit(".", 1)[-1].upper()
        quality = str(selected.get("quality") or "NORMAL").replace("_QUALITY", "")
        lines = [
            "✅ Descarga iniciada",
            f"Archivo: {file_name}",
            f"Resolución: {preferences.get('resolution') or 'no indicada'}",
            f"Tipo: {extension or 'no indicado'}",
            f"Tamaño: {_compact_size(selected.get('file_size_gib'))}",
            f"Idioma: {_compact_language(selected.get('language'))}",
            f"Versión: {quality}",
        ]
        source = str(selected.get("source") or "").strip()
        if source:
            lines.append(f"Origen: {source}")
        return "\n".join(lines)

    if payload.get("error") == "ALREADY_DOWNLOADING" and selected:
        name = str(selected.get("torrent_name") or selected.get("name") or "la solicitud")
        return f"ℹ️ La solicitud ya tiene una descarga en curso\nTorrent: {name}"

    code = payload.get("error") or "ERROR"
    lines = [f"❌ Descarga no iniciada\nMotivo: {_compact_error(code)}"]
    attempts = payload.get("preflight_attempts")
    if isinstance(attempts, int) and attempts > 0:
        lines.append(f"Candidatos revisados: {attempts}")
    return "\n".join(lines)


def _media_command(raw):
    """Preserve direct media links; route name-based requests to the smart tool."""
    text = str(raw or "").strip()
    if not text:
        return "Falta el nombre del contenido. Uso: /descarga_media <contenido>."
    if _is_direct_media_request(text):
        return _add(text, MEDIA_PATH)
    query = _extract_media_query(text)
    if not query:
        return "Falta el nombre del contenido. Uso: /descarga_media <contenido>."
    return _format_torrent_search_reply(
        _torrent_search_download({"query": query, "media_type": "video"})
    )

def _go_status():
    payload=_go_req("GET","/api/v1/tasks"); tasks=payload.get("data",[])
    return f"Gopeed: tareas={len(tasks) if isinstance(tasks,list) else 0}."

def _status(_raw=""):
    try:
        states=_run(_ha("GET","/api/states")); ids={"sensor.qbittorrent_descargas_totales","sensor.qbittorrent_descargas_activas","sensor.estado_de_las_descargas_qbittorrent"}
        v={x["entity_id"]:x.get("state","unknown") for x in states if x.get("entity_id") in ids}
        q="qBittorrent: total="+v.get("sensor.qbittorrent_descargas_totales","sin dato")+", activas="+v.get("sensor.qbittorrent_descargas_activas","sin dato")
    except Exception as exc: q=f"qBittorrent: error ({type(exc).__name__})"
    try: g=_go_status()
    except Exception as exc: g=f"Gopeed: error ({type(exc).__name__})"
    return q+"; "+g+"."

def _control(raw, action):
    target=(raw or "all").strip().casefold(); out=[]
    if target in ("all","todos","qbit","qbittorrent"):
        try:
            service="qbittorrent_pause_all" if action=="stop" else "qbittorrent_resume_all"
            _run(_ha("POST",f"/api/services/rest_command/{service}",{})); out.append("qBittorrent OK")
        except Exception as exc: out.append(f"qBittorrent error ({type(exc).__name__})")
    if target in ("all","todos","gopeed"):
        try:
            _go_req("PUT",f"/api/v1/tasks/{'pause' if action=='stop' else 'continue'}"); out.append("Gopeed OK")
        except Exception as exc: out.append(f"Gopeed error ({type(exc).__name__})")
    return "; ".join(out) or "Proveedor no permitido."


TORRENT_SEARCH_SCHEMA = {
    "name": "torrent_search_download",
    "description": (
        "Busca torrents mediante el plugin BTDig activo, aplica una política determinista "
        "de idioma/calidad/recencia y, solo tras obtener metadata, inicia como máximo un "
        "torrent con exactamente un archivo de video seleccionado. El LLM no decide el "
        "magnet, el tamaño, los archivos ni las prioridades."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Contenido solicitado; no incluyas un magnet."},
            "media_type": {"type": "string", "enum": ["video"], "default": "video"},
            "min_size_gib": {"type": "number", "minimum": 0, "default": 1},
            "max_size_gib": {"type": "number", "minimum": 0, "default": 5},
            "language_preference": {
                "type": "array", "items": {"type": "string", "enum": ["lat", "en", "unknown"]},
                "default": ["lat", "en", "unknown"],
            },
            "prefer_recent": {"type": "boolean", "default": True},
            "allow_cam": {"type": "boolean", "default": True},
            "dry_run": {"type": "boolean", "default": False},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}


def _torrent_search_download(args):
    """Structured Hermes entrypoint; all critical checks remain in Python."""
    return json.dumps(_execute_torrent_search(_q_req, args or {}), ensure_ascii=False, separators=(",", ":"))

def register(ctx):
    ctx.register_tool(
        name="torrent_search_download",
        toolset="qbittorrent",
        schema=TORRENT_SEARCH_SCHEMA,
        handler=_torrent_search_download,
        description="Búsqueda BTDig con preflight de metadata y selección de un único archivo",
        emoji="⬇️",
    )
    ctx.register_command("descarga",_generic_add,"Añade un magnet autorizado a qBittorrent en media o Descargas","")
    ctx.register_command("descarga-media",_media_command,"Descarga media directa o mediante búsqueda inteligente","")
    ctx.register_command("descarga-archivo",lambda raw:_add(raw,FILES_PATH),"Descarga archivos en el SSD mediante qBittorrent","")
    ctx.register_command("descargas-status",_status,"Estado de Gopeed y qBittorrent","")
    ctx.register_command("gopeed-status",lambda raw:_go_status(),"Estado de Gopeed","")
    ctx.register_command("descargas-stop",lambda raw:_control(raw,"stop"),"Detiene descargas","")
    ctx.register_command("descargas-inicio",lambda raw:_control(raw,"start"),"Reanuda descargas","")
