from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)
_ENDPOINT = "https://serpapi.com/search.json"
_SECRETS = Path.home() / ".hermes" / "secrets" / "google-search.env"


def _read_secret(name: str) -> str:
    value = os.getenv(name, "").strip()
    if value:
        return value
    try:
        for line in _SECRETS.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, raw = line.split("=", 1)
            if key.strip() == name:
                return raw.strip().strip("\"'")
    except OSError:
        pass
    return ""


class GoogleCSEWebSearchProvider(WebSearchProvider):
    @property
    def name(self) -> str:
        return "google-serpapi"

    @property
    def display_name(self) -> str:
        return "Google Search via SerpApi"

    def is_available(self) -> bool:
        return bool(_read_secret("SERPAPI_KEY_GOOGLE_SEARCH"))

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return False

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        import httpx

        key = _read_secret("SERPAPI_KEY_GOOGLE_SEARCH")
        if not key:
            return {"success": False, "error": "SERPAPI_KEY_GOOGLE_SEARCH is not configured"}
        count = max(1, min(int(limit), 10))
        try:
            response = httpx.get(
                _ENDPOINT,
                params={"engine": "google", "api_key": key, "q": query[:500], "num": count},
                timeout=15,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            logger.warning("SerpApi HTTP error %s; fallback may be used", status)
            return {"success": False, "error": f"SerpApi returned HTTP {status}"}
        except (httpx.RequestError, ValueError) as exc:
            return {"success": False, "error": f"SerpApi request failed: {exc}"}

        results = []
        for position, item in enumerate(payload.get("organic_results", [])[:count], 1):
            results.append({
                "title": str(item.get("title", "")),
                "url": str(item.get("link", "")),
                "description": str(item.get("snippet", "")),
                "position": position,
            })
        return {"success": True, "data": {"web": results}}


def register(ctx) -> None:
    ctx.register_web_search_provider(GoogleCSEWebSearchProvider())
