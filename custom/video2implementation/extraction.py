"""Bounded, deterministic normalization for Pulse YouTube extraction results.

Providers are injected by the caller. This module performs no network access,
subprocess execution, database writes, or interpretation of content as code.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any, Mapping
from urllib.parse import urlparse
import re

MAX_METADATA_CHARS = 64_000
MAX_TRANSCRIPT_CHARS = 200_000
MAX_SEGMENTS = 20_000
MAX_LINKS = 100
LINK_RE = re.compile(r"https?://[^\s<>()\[\]{}]+", re.IGNORECASE)


@dataclass(frozen=True)
class ExtractionLimits:
    timeout_seconds: int = 60
    max_retries: int = 1
    max_external_calls: int = 2
    max_metadata_chars: int = MAX_METADATA_CHARS
    max_transcript_chars: int = MAX_TRANSCRIPT_CHARS
    max_segments: int = MAX_SEGMENTS


def _bounded_text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:limit] if len(text) <= limit else text[:limit] + "…"


def _links(description: str | None) -> list[str]:
    if not description:
        return []
    links = []
    for match in LINK_RE.findall(description):
        link = match.rstrip(".,;:!?")
        parsed = urlparse(link)
        if parsed.scheme == "https" and parsed.netloc and link not in links:
            links.append(link)
        if len(links) >= MAX_LINKS:
            break
    return links


def normalize_metadata(raw: Mapping[str, Any] | None, limits: ExtractionLimits = ExtractionLimits()) -> dict[str, Any]:
    """Normalize known fields and preserve unavailable fields as null."""
    if not isinstance(raw, Mapping):
        return {
            "title": None, "channel": None, "description": None, "links": [],
            "chapters": None, "duration_seconds": None, "accessibility": "unavailable",
            "status": "unavailable",
        }
    description = _bounded_text(raw.get("description"), limits.max_metadata_chars)
    duration = raw.get("duration_seconds")
    if not isinstance(duration, (int, float)) or not isfinite(float(duration)) or duration < 0:
        duration = None
    chapters = raw.get("chapters")
    if not isinstance(chapters, list):
        chapters = None
    return {
        "title": _bounded_text(raw.get("title"), 1024),
        "channel": _bounded_text(raw.get("channel"), 512),
        "description": description,
        "links": _links(description),
        "chapters": chapters,
        "duration_seconds": duration,
        "accessibility": raw.get("accessibility") or "unknown",
        "status": raw.get("status") or "available",
    }


def normalize_transcript(raw: Mapping[str, Any] | None, limits: ExtractionLimits = ExtractionLimits()) -> dict[str, Any]:
    """Normalize ordered timestamped segments; never claims visual evidence."""
    if not isinstance(raw, Mapping):
        return {
            "status": "unavailable", "language": None, "is_generated": None,
            "segments": [], "text": None, "visual_evidence": "not_available",
        }
    source_error = raw.get("error")
    if source_error:
        return {
            "status": "unavailable", "language": raw.get("language"),
            "is_generated": raw.get("is_generated"), "segments": [],
            "text": None, "error": str(source_error)[:512],
            "visual_evidence": "not_available",
        }
    source_segments = raw.get("segments")
    if not isinstance(source_segments, list):
        return {
            "status": "unavailable", "language": raw.get("language"),
            "is_generated": raw.get("is_generated"), "segments": [],
            "text": None, "visual_evidence": "not_available",
        }
    if len(source_segments) > limits.max_segments:
        return {
            "status": "rejected", "language": raw.get("language"),
            "is_generated": raw.get("is_generated"), "segments": [],
            "text": None, "error": "transcript_segment_limit_exceeded",
            "visual_evidence": "not_available",
        }
    segments = []
    for item in source_segments:
        if not isinstance(item, Mapping):
            continue
        text = str(item.get("text") or "").strip()
        start = item.get("start")
        duration = item.get("duration")
        if not text or not isinstance(start, (int, float)) or not isinstance(duration, (int, float)):
            continue
        if not isfinite(float(start)) or not isfinite(float(duration)) or start < 0 or duration < 0:
            continue
        segments.append({"text": text, "start": float(start), "duration": float(duration)})
    text = " ".join(item["text"] for item in segments)
    if len(text) > limits.max_transcript_chars:
        return {
            "status": "rejected", "language": raw.get("language"),
            "is_generated": raw.get("is_generated"), "segments": [],
            "text": None, "error": "transcript_size_limit_exceeded",
            "visual_evidence": "not_available",
        }
    return {
        "status": "available" if segments else "empty",
        "language": raw.get("language"),
        "is_generated": raw.get("is_generated"),
        "segments": segments,
        "text": text or None,
        "visual_evidence": "not_available",
    }


def build_extraction_result(
    task_id: str,
    video_id: str,
    canonical_url: str,
    metadata: Mapping[str, Any] | None,
    transcript: Mapping[str, Any] | None,
    extraction_errors: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the bounded result Pulse will eventually return to MAX."""
    normalized_metadata = normalize_metadata(metadata)
    return {
        "task_id": task_id,
        "video_id": video_id,
        "canonical_url": canonical_url,
        "metadata": normalized_metadata,
        "transcript": normalize_transcript(transcript),
        "chapters": normalized_metadata.get("chapters") if metadata else None,
        "technical_links": normalized_metadata.get("links", []) if metadata else [],
        "source_status": "partial" if extraction_errors else "extracted",
        "extraction_errors": [dict(item) for item in (extraction_errors or [])],
    }
