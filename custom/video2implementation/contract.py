"""Deterministic contract and planning helpers for video.youtube.analyze.

This module performs no HTTP, LLM, Telegram, Composio, database, or file I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from ipaddress import ip_address
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit
import re

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
ERROR_CODE_RE = re.compile(r"^[a-z0-9_]{1,64}$")
ALLOWED_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"})
ALLOWED_FORMATS = frozenset({"markdown", "json", "docx", "pdf"})
ANALYSIS_TYPES = frozenset({"summary", "transcript", "metadata", "video2implementation"})
ALLOWED_DESTINATION_ALIASES = frozenset({"telegram_owner", "vps_documents"})
ALLOWED_DELIVERY_TARGETS = frozenset({"telegram", "vps"})
STATUSES = frozenset({"queued", "approved", "running", "done", "failed", "rejected"})


class VideoTaskContractError(ValueError):
    """Raised when untrusted task input violates the phase-1 contract."""


@dataclass(frozen=True)
class YouTubeReference:
    video_id: str
    normalized_url: str
    source_host: str


def parse_youtube_url(value: str) -> YouTubeReference:
    """Parse one HTTPS YouTube video URL without performing network I/O."""
    if not isinstance(value, str) or not value.strip():
        raise VideoTaskContractError("video_url_required")
    parsed = urlsplit(value.strip())
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() != "https":
        raise VideoTaskContractError("youtube_https_required")
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise VideoTaskContractError("youtube_authority_invalid")
    if host not in ALLOWED_HOSTS:
        try:
            ip_address(host)
        except ValueError:
            pass
        raise VideoTaskContractError("youtube_domain_not_allowed")

    video_id = ""
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        if parsed.path.rstrip("/") == "/watch":
            video_id = parse_qs(parsed.query, keep_blank_values=True).get("v", [""])[0]
        else:
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) == 2 and parts[0].lower() in {"shorts", "live"}:
                video_id = parts[1]
    else:
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) == 1:
            video_id = parts[0]
    if not VIDEO_ID_RE.fullmatch(video_id):
        raise VideoTaskContractError("youtube_video_id_invalid")
    return YouTubeReference(
        video_id=video_id,
        normalized_url=f"https://www.youtube.com/watch?v={video_id}",
        source_host=host,
    )


def _text(value: Any, field: str, limit: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise VideoTaskContractError(f"{field}_invalid")
    return value.strip()


def _destination_alias(value: Any) -> str:
    alias = _text(value, "destination_alias", 64)
    if alias not in ALLOWED_DESTINATION_ALIASES or "/" in alias or "\\" in alias:
        raise VideoTaskContractError("destination_alias_not_allowed")
    return alias


@dataclass(frozen=True)
class VideoTaskContract:
    """Validated transport object mapped onto an existing AgentRequest row.

    task_id maps to AgentRequest.id; requester maps to AgentRequest.origin.
    Destination aliases are logical names resolved by trusted configuration,
    never by the LLM.
    """
    task_id: str
    requester: str
    video_url: str
    video_id: str
    analysis_type: str
    requested_formats: tuple[str, ...]
    delivery_targets: tuple[str, ...]
    destination_alias: str
    status: str = "queued"
    error_code: str | None = None
    timestamps: Mapping[str, str] | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "VideoTaskContract":
        reference = parse_youtube_url(data.get("video_url"))
        task_id = _text(data.get("task_id"), "task_id", 128)
        requester = _text(data.get("requester"), "requester", 128)
        analysis_type = _text(data.get("analysis_type"), "analysis_type", 64).lower()
        if analysis_type not in ANALYSIS_TYPES:
            raise VideoTaskContractError("analysis_type_not_allowed")

        formats = data.get("requested_formats")
        if not isinstance(formats, (list, tuple)) or not formats:
            raise VideoTaskContractError("requested_formats_invalid")
        normalized_formats = tuple(dict.fromkeys(str(item).strip().lower() for item in formats))
        if any(item not in ALLOWED_FORMATS for item in normalized_formats):
            raise VideoTaskContractError("requested_format_not_allowed")

        targets = data.get("delivery_targets")
        if not isinstance(targets, (list, tuple)) or not targets:
            raise VideoTaskContractError("delivery_targets_invalid")
        normalized_targets = tuple(_text(item, "delivery_target", 64) for item in targets)
        if any(item not in ALLOWED_DELIVERY_TARGETS for item in normalized_targets):
            raise VideoTaskContractError("delivery_target_not_allowed")

        status = _text(data.get("status", "queued"), "status", 32).lower()
        if status not in STATUSES:
            raise VideoTaskContractError("status_not_allowed")
        error_code = data.get("error_code")
        if error_code is not None and (
            not isinstance(error_code, str) or not ERROR_CODE_RE.fullmatch(error_code)
        ):
            raise VideoTaskContractError("error_code_invalid")
        timestamps = data.get("timestamps")
        if timestamps is not None and not isinstance(timestamps, Mapping):
            raise VideoTaskContractError("timestamps_invalid")

        supplied_id = data.get("video_id")
        if supplied_id is not None and supplied_id != reference.video_id:
            raise VideoTaskContractError("video_id_mismatch")
        return cls(
            task_id=task_id,
            requester=requester,
            video_url=reference.normalized_url,
            video_id=reference.video_id,
            analysis_type=analysis_type,
            requested_formats=normalized_formats,
            delivery_targets=normalized_targets,
            destination_alias=_destination_alias(data.get("destination_alias")),
            status=status,
            error_code=error_code,
            timestamps=dict(timestamps) if timestamps is not None else None,
        )

    @classmethod
    def from_agent_request(cls, row: Mapping[str, Any]) -> "VideoTaskContract":
        """Use existing AgentRequest fields without creating another table."""
        metadata = row.get("metadata") if isinstance(row.get("metadata"), Mapping) else {}
        data = dict(metadata)
        data.update({
            "task_id": row.get("id"),
            "requester": row.get("origin"),
            "status": row.get("status", "queued"),
            "timestamps": {
                key: row[key] for key in ("createdAt", "updatedAt", "startedAt", "finishedAt")
                if row.get(key) is not None
            },
        })
        return cls.from_mapping(data)

    def dedupe_key(self) -> str:
        material = "|".join((
            self.video_id, self.analysis_type, *self.requested_formats,
            self.destination_alias,
        ))
        return sha256(material.encode("utf-8")).hexdigest()


def classify_pulse_intent(request_kind: str | None, text: str) -> str:
    """Recognize only the new explicit intent; preserve existing social routing."""
    if request_kind == "video.youtube.analyze" or re.search(
        r"\bvideo\.youtube\.analyze\b", text or "", re.I
    ):
        return "video.youtube.analyze"
    return "pulse.social"


def select_capabilities(task: VideoTaskContract) -> tuple[dict[str, Any], ...]:
    """Return a plan only; never invoke a tool or provider."""
    capabilities: list[dict[str, Any]] = [
        {"name": "youtube_metadata", "mode": "planned", "external_call": False},
        {"name": "transcript_extraction", "mode": "planned", "external_call": False},
    ]
    if task.analysis_type == "video2implementation":
        capabilities.extend((
            {"name": "video2implementation_prompt", "mode": "pending_prompt_file", "external_call": False},
            {"name": "audiovisual_analysis", "mode": "not_activated", "external_call": False},
        ))
    if {"markdown", "json"} & set(task.requested_formats):
        capabilities.append({"name": "deterministic_document", "mode": "planned", "external_call": False})
    if {"docx", "pdf"} & set(task.requested_formats):
        capabilities.append({"name": "office_document_export", "mode": "pending_runtime", "external_call": False})
    capabilities.append({
        "name": "delivery", "mode": "planned",
        "destination_alias": task.destination_alias, "external_call": False,
    })
    return tuple(capabilities)
