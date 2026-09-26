"""Bounded /alexa command using the public Home Assistant tool surface."""

import json
import re
import time
import asyncio
import concurrent.futures
import html
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from zoneinfo import ZoneInfo

from agent.secret_scope import get_secret


_ECHO_device_two_MEDIA_PLAYER = "media_player.sala_general_echo_dot_de_device_two"
_ALEXA_TARGETS = {
    "echo_device_two": "notify.sala_general_echo_dot_de_device_two_speak",
    "device_two": "notify.sala_general_echo_dot_de_device_two_speak",
    "todas": "notify.en_todas_partes_announce",
    "all": "notify.en_todas_partes_announce",
}
_SPEAK_COOLDOWN_SECONDS = 15
_last_speech_at: dict[str, float] = {}
_URL_RE = re.compile(r"(?:https?://|www\.|t\.me/|discord\.gg/|bit\.ly/|tinyurl\.com/)", re.IGNORECASE)
_SENSITIVE_SPEECH_RE = re.compile(
    r"\b(?:otp|contrase(?:n|ñ)a|password|token|api[ _-]?key|clave de acceso|c[oó]digo de verificaci[oó]n)\b",
    re.IGNORECASE,
)
_NEWS_FEED = "https://news.google.com/rss/search"
_ECUADOR_TZ = ZoneInfo("America/Guayaquil")
_NEWS_MAX_ITEMS = 6
_NEWS_SUMMARY_LIMIT = 440


def _tool_payload(raw: str) -> dict:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"error": raw}
    return parsed if isinstance(parsed, dict) else {"error": "Respuesta inválida de Home Assistant"}


def _get_config() -> tuple[str, str]:
    """Resolve Home Assistant credentials from Hermes' profile-scoped secrets."""
    return (
        (get_secret("HASS_URL", "http://127.0.0.1:8123") or "").rstrip("/"),
        get_secret("HASS_TOKEN", "") or "",
    )


async def _ha_json(method: str, path: str, payload: dict | None = None) -> object:
    """Perform one bounded Home Assistant request without relying on core internals."""
    import aiohttp

    base_url, token = _get_config()
    if not token:
        raise RuntimeError("Home Assistant no tiene un token configurado")
    kwargs = {
        "headers": {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        "timeout": aiohttp.ClientTimeout(total=15),
    }
    if payload is not None:
        kwargs["json"] = payload
    async with aiohttp.ClientSession() as session:
        async with session.request(method, f"{base_url}{path}", **kwargs) as response:
            response.raise_for_status()
            return await response.json()


def _run_async(coro):
    """Run a coroutine even when a gateway event loop is already active."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result(timeout=20)
    return asyncio.run(coro)


def _ha_call(domain: str, service: str, entity_id: str, data: dict) -> dict:
    try:
        result = _run_async(_ha_json(
            "POST", f"/api/services/{domain}/{service}",
            {**data, "entity_id": entity_id},
        ))
    except Exception as exc:
        return {"error": f"Home Assistant rechazó {domain}.{service}: {exc}"}
    return {"result": {"success": True, "response": result}}


def _ha_state(entity_id: str) -> dict:
    try:
        result = _run_async(_ha_json("GET", f"/api/states/{entity_id}"))
    except Exception as exc:
        return {"error": f"No pude leer el estado de Home Assistant: {exc}"}
    return {"result": result}


def _parse_percent(value: str) -> int | None:
    value = value.strip().removesuffix("%")
    if not value.isdigit():
        return None
    percent = int(value)
    return percent if 0 <= percent <= 100 else None


def _volume_request(text: str) -> tuple[str, int] | None:
    """Return only explicit, bounded volume requests."""
    words = text.casefold().split()
    if len(words) == 2 and words[0] == "volumen":
        percent = _parse_percent(words[1])
        return ("absolute", percent) if percent is not None else None
    if len(words) == 3 and words[0] == "volumen" and words[1] in {"subir", "bajar"}:
        percent = _parse_percent(words[2])
        return (words[1], percent) if percent is not None else None
    if len(words) == 3 and words[0] in {"subir", "bajar"} and words[1] == "volumen":
        percent = _parse_percent(words[2])
        return (words[0], percent) if percent is not None else None
    return None


def _set_echo_device_two_volume(mode: str, percent: int) -> str:
    target = percent
    if mode != "absolute":
        current = _ha_state(_ECHO_device_two_MEDIA_PLAYER)
        attrs = current.get("result", {}).get("attributes", {})
        current_level = attrs.get("volume_level")
        if not isinstance(current_level, (int, float)):
            return "No pude leer el volumen actual del Echo de device_two; no hice cambios."
        current_percent = round(current_level * 100)
        target = min(100, max(0, current_percent + (percent if mode == "subir" else -percent)))
    outcome = _ha_call(
        "media_player", "volume_set", _ECHO_device_two_MEDIA_PLAYER,
        {"volume_level": target / 100},
    )
    if outcome.get("error"):
        return f"No pude cambiar el volumen de Alexa: {outcome['error']}"
    return f"Volumen del Echo de device_two ajustado al {target}%."


def _speak(target: str, message: str) -> str:
    """Deliver constrained speech through an allowlisted HA notify target."""
    if target not in _ALEXA_TARGETS:
        return "Destino Alexa no permitido."
    if not message:
        return "El mensaje para Alexa no puede estar vacío."
    if len(message) > 500:
        return "El mensaje para Alexa supera el límite de 500 caracteres."
    if _URL_RE.search(message) or _SENSITIVE_SPEECH_RE.search(message):
        return "El mensaje para Alexa contiene contenido no permitido."
    now = time.monotonic()
    remaining = _SPEAK_COOLDOWN_SECONDS - (now - _last_speech_at.get(target, 0.0))
    if remaining > 0:
        return f"Alexa está limitada temporalmente; intenta de nuevo en {int(remaining) + 1} segundos."

    outcome = _ha_call(
        "notify", "send_message", _ALEXA_TARGETS[target], {"message": message},
    )
    if outcome.get("error"):
        return f"Alexa no pudo anunciar el mensaje: {outcome['error']}"
    _last_speech_at[target] = now
    return f"Mensaje enviado a Alexa ({target})."


def _fetch_ecuador_news() -> tuple[str, list[dict[str, str]]]:
    """Fetch recent Ecuador headlines and return the local date plus items."""
    now = datetime.now(_ECUADOR_TZ)
    date_label = now.strftime("%d/%m/%Y")
    query = f"Noticias Ecuador {now:%Y-%m-%d} when:1d"
    url = _NEWS_FEED + "?" + urllib.parse.urlencode({
        "q": query, "hl": "es-419", "gl": "EC", "ceid": "EC:es-419",
    })
    request = urllib.request.Request(url, headers={"User-Agent": "Hermes-News/1.0"})
    with urllib.request.urlopen(request, timeout=12) as response:
        root = ET.fromstring(response.read(512 * 1024))
    items = []
    for item in root.findall("./channel/item"):
        title = html.unescape(re.sub(r"<[^>]+>", "", item.findtext("title", ""))).strip()
        source = html.unescape(item.findtext("source", "")).strip()
        if title:
            items.append({"title": title, "source": source})
        if len(items) >= _NEWS_MAX_ITEMS:
            break
    return date_label, items


def _summarize_ecuador_news(date_label: str, items: list[dict[str, str]]) -> str:
    """Produce a short, source-bounded Spanish bulletin for Alexa."""
    facts = "\n".join(
        f"- {item['title']}" + (f" ({item['source']})" if item["source"] else "")
        for item in items
    )
    prompt = (
        "Redacta un resumen hablado de noticias de Ecuador para Alexa. "
        f"Fecha local: {date_label}. Usa únicamente estos titulares y no inventes datos. "
        "Escribe en español, sin enlaces, sin viñetas, sin introducción larga, "
        "máximo 390 caracteres. Si hay temas repetidos, agrúpalos.\n" + facts
    )
    payload = json.dumps({
        "model": "qwen3-8b-local",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 220,
        "temperature": 0.1,
        "stream": False,
    }).encode("utf-8")
    request = urllib.request.Request(
        "http://127.0.0.1:18088/v1/chat/completions", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=35) as response:
        result = json.loads(response.read(128 * 1024))
    text = (((result.get("choices") or [{}])[0]).get("message") or {}).get("content", "")
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    text = re.sub(r"https?://\S+", "", text).strip()
    return re.sub(r"\s+", " ", text)[:_NEWS_SUMMARY_LIMIT].strip()


def _fallback_ecuador_news(date_label: str, items: list[dict[str, str]]) -> str:
    """Return a truthful title digest when the local summarizer is unavailable."""
    digest = "; ".join(item["title"] for item in items)
    return f"Noticias de Ecuador del {date_label}: {digest}"[:_NEWS_SUMMARY_LIMIT]


def _command_ecuador_news(raw: str) -> str:
    """Fetch today's Ecuador news and announce it on the Echo of device_two."""
    try:
        date_label, items = _fetch_ecuador_news()
    except Exception as exc:
        return f"No pude consultar las noticias de Ecuador de hoy: {exc}"
    if not items:
        return f"No encontré titulares de Ecuador publicados hoy ({date_label})."
    try:
        summary = _summarize_ecuador_news(date_label, items)
    except Exception:
        summary = _fallback_ecuador_news(date_label, items)
    if not summary:
        summary = _fallback_ecuador_news(date_label, items)
    return _speak("echo_device_two", summary)


def _command(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return "Uso: /alexa <mensaje>. Destino predeterminado: Echo de device_two."
    volume = _volume_request(text)
    if volume is not None:
        return _set_echo_device_two_volume(*volume)
    if "volumen" in text.casefold().split():
        return "Uso de volumen: /alexa volumen 50, /alexa subir volumen 10 o /alexa bajar volumen 10."
    if text.casefold().startswith("todas confirmo "):
        # Compatibility with the previous syntax; `confirmo` is no longer a
        # required second step for an authenticated Telegram command.
        text = "todas " + text[len("todas confirmo "):].strip()
    if text.casefold().startswith("todas "):
        message = text[len("todas "):].strip()
        if not message:
            return "Uso: /alexa todas <mensaje>."
        return _speak("todas", message)
    return _speak("echo_device_two", text)


def register(ctx):
    ctx.register_command(
        "alexa",
        _command,
        "Anuncia un mensaje por Alexa; por defecto usa el Echo de device_two",
        "",
    )
    ctx.register_command(
        "noticias_ecuador_hoy",
        _command_ecuador_news,
        "Lee por Alexa un resumen de las noticias actuales de Ecuador",
        "",
    )
