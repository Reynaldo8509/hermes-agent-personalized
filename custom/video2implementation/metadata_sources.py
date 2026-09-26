"""Read-only planning for complete YouTube metadata.

No provider is called here. A validated video id is used directly; this module
does not search by keywords and does not turn descriptions into commands.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlencode
import re

from .contract import parse_youtube_url

DESCRIPTION_LINK_RE = re.compile(r"https://[^\s<>()\[\]{}]+", re.IGNORECASE)


@dataclass(frozen=True)
class MetadataSourcePlan:
    video_id: str
    canonical_url: str
    sources: tuple[dict[str, Any], ...]
    description_source: str
    credential_available: bool
    call_allowed: bool = False
    chapters_supported_by_selected_source: bool = False


def technical_links_from_description(description: str | None) -> list[str]:
    """Extract only HTTPS links actually present in a returned description."""
    if not description:
        return []
    links: list[str] = []
    for raw in DESCRIPTION_LINK_RE.findall(description):
        link = raw.rstrip(".,;:!?")
        if link not in links:
            links.append(link)
    return links


def build_youtube_data_api_request(video_url: str, *, credential_available: bool) -> dict[str, Any]:
    """Build, but never send, a videos.list request for one validated video id."""
    reference = parse_youtube_url(video_url)
    params = {"part": "snippet,contentDetails", "id": reference.video_id}
    return {
        "provider": "youtube_data_api_v3",
        "method": "GET",
        "endpoint": "https://www.googleapis.com/youtube/v3/videos",
        "query": urlencode(params),
        "params": params,
        "credential_available": bool(credential_available),
        "call_allowed": False,
        "expected_fields": [
            "snippet.title", "snippet.channelTitle", "snippet.description",
            "snippet.publishedAt", "contentDetails.duration",
        ],
        "chapters": None,
        "chapters_reason": "videos.list snippet/contentDetails does not provide a chapter list",
    }


def build_metadata_plan(
    video_url: str,
    *,
    youtube_api_key_present: bool = False,
    composio_catalog: Mapping[str, Any] | None = None,
) -> MetadataSourcePlan:
    """Describe the safe source order without making any external request."""
    reference = parse_youtube_url(video_url)
    sources: list[dict[str, Any]] = [
        {
            "provider": "youtube_oembed",
            "status": "available_for_basic_fields",
            "fields": ["title", "channel"],
            "description": None,
            "call_allowed": False,
        }
    ]
    # Hermes has a Composio-on-demand route for the YouTube toolkit. The
    # router requires composio_discover to return exact slugs and schemas
    # before composio_read is permitted; do not hardcode an unverified slug.
    sources.append({
        "provider": "composio_on_demand",
        "toolkit": "youtube",
        "status": "requires_exact_discovery",
        "capabilities": ["query/search", "content/metadata-or-transcript"],
        "exact_slugs": [],
        "call_allowed": False,
    })
    exact_catalog = bool(composio_catalog and composio_catalog.get("youtube_video_metadata"))
    if exact_catalog:
        sources.append({
            "provider": "composio_catalog_verified",
            "status": "planned",
            "fields": list(composio_catalog.get("youtube_video_metadata") or []),
            "call_allowed": False,
        })
    api_request = build_youtube_data_api_request(
        reference.normalized_url, credential_available=youtube_api_key_present,
    )
    api_request["status"] = "planned" if youtube_api_key_present else "blocked_credential_not_present"
    sources.append(api_request)
    return MetadataSourcePlan(
        video_id=reference.video_id,
        canonical_url=reference.normalized_url,
        sources=tuple(sources),
        description_source=("youtube_data_api_v3" if youtube_api_key_present else "unavailable"),
        credential_available=bool(youtube_api_key_present),
    )


def normalize_complete_metadata(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Preserve unavailable values explicitly and derive links only from description."""
    if not isinstance(raw, Mapping):
        return {
            "title": None, "channel": None, "description": None, "technical_links": [],
            "duration": None, "published_at": None, "chapters": None,
            "source_status": "unavailable",
            "unavailable_reasons": ["metadata_source_unavailable"],
        }
    description = raw.get("description") if isinstance(raw.get("description"), str) else None
    return {
        "title": raw.get("title") if isinstance(raw.get("title"), str) else None,
        "channel": raw.get("channel") if isinstance(raw.get("channel"), str) else None,
        "description": description,
        "technical_links": technical_links_from_description(description),
        "duration": raw.get("duration") if isinstance(raw.get("duration"), (int, float)) else None,
        "published_at": raw.get("published_at") if isinstance(raw.get("published_at"), str) else None,
        "chapters": raw.get("chapters") if isinstance(raw.get("chapters"), list) else None,
        "source_status": raw.get("source_status") or "partial",
        "unavailable_reasons": list(raw.get("unavailable_reasons") or []),
    }
