"""Optional, disabled-by-default VPS adapter for the Phase 3H Dell worker.

This module is intentionally not imported by the production bridge in this
phase. It provides a fixed SSH argv boundary and a bounded result contract for
a later Pulse integration. It never accepts a destination, URL, shell text, or
worker path from a request.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import subprocess
from typing import Any, Callable, Mapping


VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
FEATURE_FLAG_ENV = "VIDEO2IMPLEMENTATION_LOCAL_TRANSCRIPT_ENABLED"
ENABLED_VALUES = frozenset({"1", "true", "yes", "on"})
WORKER_TOKEN = "video2implementation-transcript"
SSH_BINARY = "/usr/bin/ssh"
SSH_CONFIG = "/home/example-user/.ssh/video2implementation_dell_config"
SSH_ALIAS = "video2implementation-dell"
TIMEOUT_SECONDS = 40.0
MAX_STDOUT_BYTES = 1_000_000
MAX_STDERR_BYTES = 16_000
MAX_TRANSCRIPT_CHARS = 300_000
MAX_TIMESTAMPED_CHARS = 300_000
MAX_SEGMENTS = 10_000
MAX_DIAGNOSTIC_CHARS = 1_000


class LocalTranscriptAdapterError(ValueError):
    """Raised for an invalid request or unsafe delegated response."""


@dataclass(frozen=True)
class LocalTranscriptConfig:
    enabled: bool = False
    timeout_seconds: float = TIMEOUT_SECONDS
    ssh_binary: str = SSH_BINARY
    ssh_config: str = SSH_CONFIG
    ssh_alias: str = SSH_ALIAS

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "LocalTranscriptConfig":
        values = os.environ if environ is None else environ
        raw = str(values.get(FEATURE_FLAG_ENV, "")).strip().lower()
        try:
            timeout = float(values.get("VIDEO2IMPLEMENTATION_LOCAL_TRANSCRIPT_TIMEOUT_SECONDS", TIMEOUT_SECONDS))
        except (TypeError, ValueError):
            timeout = TIMEOUT_SECONDS
        if timeout <= 0 or timeout > 300:
            timeout = TIMEOUT_SECONDS
        return cls(enabled=raw in ENABLED_VALUES, timeout_seconds=timeout)


@dataclass(frozen=True)
class LocalTranscriptResult:
    video_id: str
    status: str
    transcript: str | None
    segments: tuple[Mapping[str, Any], ...]
    timestamps: str | None
    duration: str | None
    method: str
    diagnostic: str | None
    node: str
    request_id: str | None = None
    elapsed_seconds: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "video_id": self.video_id,
            "status": self.status,
            "transcript": self.transcript,
            "segments": [dict(item) for item in self.segments],
            "timestamps": self.timestamps,
            "duration": self.duration,
            "method": self.method,
            "diagnostic": self.diagnostic,
            "node": self.node,
            "request_id": self.request_id,
            "elapsed_seconds": self.elapsed_seconds,
        }


def _bounded(value: Any, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def _validate_ids(video_id: str, request_id: str | None) -> None:
    if not isinstance(video_id, str) or not VIDEO_ID_RE.fullmatch(video_id):
        raise LocalTranscriptAdapterError("video_id_invalid")
    if request_id is not None and (not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id)):
        raise LocalTranscriptAdapterError("request_id_invalid")


def build_worker_argv(video_id: str, request_id: str | None = None, *, config: LocalTranscriptConfig | None = None) -> tuple[str, ...]:
    """Build the only allowed subprocess command; no shell interpolation occurs."""
    _validate_ids(video_id, request_id)
    current = config or LocalTranscriptConfig(enabled=True)
    command = (
        current.ssh_binary,
        "-F", current.ssh_config,
        current.ssh_alias,
        WORKER_TOKEN,
        video_id,
    )
    if request_id is not None:
        command += (request_id,)
    return command


def _result(video_id: str, status: str, *, request_id: str | None = None, diagnostic: str | None = None, **kwargs: Any) -> LocalTranscriptResult:
    return LocalTranscriptResult(
        video_id=video_id,
        status=status,
        transcript=kwargs.get("transcript"),
        segments=tuple(kwargs.get("segments") or ()),
        timestamps=kwargs.get("timestamps"),
        duration=kwargs.get("duration"),
        method=str(kwargs.get("method") or "none")[:80],
        diagnostic=_bounded(diagnostic, MAX_DIAGNOSTIC_CHARS),
        node=str(kwargs.get("node") or "dell_g15")[:80],
        request_id=request_id,
        elapsed_seconds=kwargs.get("elapsed_seconds"),
    )


def _validate_worker_payload(value: Any, video_id: str, request_id: str | None) -> LocalTranscriptResult:
    if not isinstance(value, Mapping):
        return _result(video_id, "failed", request_id=request_id, diagnostic="worker_object_required")
    if value.get("video_id") != video_id:
        return _result(video_id, "failed", request_id=request_id, diagnostic="worker_video_id_mismatch")
    status = value.get("status")
    if status not in {"available", "unavailable", "failed", "timeout"}:
        return _result(video_id, "failed", request_id=request_id, diagnostic="worker_status_invalid")
    transcript = _bounded(value.get("transcript"), MAX_TRANSCRIPT_CHARS)
    if transcript is not None and status == "available" and not transcript.strip():
        return _result(video_id, "failed", request_id=request_id, diagnostic="worker_transcript_empty")
    segments = value.get("segments")
    if not isinstance(segments, list) or len(segments) > MAX_SEGMENTS:
        return _result(video_id, "failed", request_id=request_id, diagnostic="worker_segments_invalid")
    bounded_segments: list[Mapping[str, Any]] = []
    for item in segments:
        if not isinstance(item, Mapping):
            return _result(video_id, "failed", request_id=request_id, diagnostic="worker_segment_invalid")
        bounded_item: dict[str, Any] = {}
        for key in ("timestamp", "text", "start", "duration"):
            if key in item:
                candidate = item[key]
                if key in {"timestamp", "text"}:
                    if not isinstance(candidate, str) or len(candidate) > 2_000:
                        return _result(video_id, "failed", request_id=request_id, diagnostic="worker_segment_text_invalid")
                    bounded_item[key] = candidate
                elif isinstance(candidate, (int, float)) and not isinstance(candidate, bool) and candidate >= 0:
                    bounded_item[key] = candidate
        if not bounded_item:
            return _result(video_id, "failed", request_id=request_id, diagnostic="worker_segment_empty")
        bounded_segments.append(bounded_item)
    timestamps = _bounded(value.get("timestamps"), MAX_TIMESTAMPED_CHARS)
    duration = value.get("duration") if isinstance(value.get("duration"), str) else None
    diagnostic = _bounded(value.get("diagnostic"), MAX_DIAGNOSTIC_CHARS)
    elapsed = value.get("elapsed_seconds")
    if not isinstance(elapsed, (int, float)) or isinstance(elapsed, bool) or elapsed < 0:
        elapsed = None
    return _result(
        video_id,
        status,
        transcript=transcript,
        segments=bounded_segments,
        timestamps=timestamps,
        duration=duration,
        method=value.get("method") if isinstance(value.get("method"), str) else "unknown",
        node=value.get("node") if isinstance(value.get("node"), str) else "dell_g15",
        diagnostic=diagnostic,
        request_id=request_id,
        elapsed_seconds=min(float(elapsed), 3600.0) if elapsed is not None else None,
    )


def extraction_patch(result: LocalTranscriptResult) -> dict[str, Any]:
    """Map the bounded result to the existing Pulse extraction shape."""
    transcript_status = "available" if result.status == "available" and result.transcript else "unavailable"
    errors = [result.diagnostic] if result.diagnostic and result.status != "available" else []
    return {
        "transcript": {
            "status": transcript_status,
            "text": result.transcript,
            "segments": list(result.segments),
            "timestamped_text": result.timestamps,
            "visual_evidence": "not_available",
            "duration": result.duration,
        },
        "transcript_status": transcript_status,
        "extraction_errors": errors,
        "local_transcript": {
            "node": result.node,
            "method": result.method,
            "status": result.status,
            "timestamps_available": bool(result.timestamps),
            "diagnostic": result.diagnostic,
        },
    }


class LocalTranscriptAdapter:
    """One-shot adapter; disabled configuration performs no subprocess call."""

    def __init__(self, config: LocalTranscriptConfig | None = None, *, process_factory: Callable[..., Any] = subprocess.Popen) -> None:
        self.config = config or LocalTranscriptConfig.from_env()
        self._process_factory = process_factory
        self.invocation_count = 0

    def fetch(self, video_id: str, request_id: str | None = None) -> LocalTranscriptResult:
        _validate_ids(video_id, request_id)
        if not self.config.enabled:
            return _result(video_id, "disabled", request_id=request_id, diagnostic="local_transcript_disabled")
        if self.invocation_count >= 1:
            return _result(video_id, "failed", request_id=request_id, diagnostic="local_transcript_call_limit_exceeded")
        self.invocation_count += 1
        command = build_worker_argv(video_id, request_id, config=self.config)
        try:
            process = self._process_factory(
                list(command), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, shell=False,
            )
            stdout, stderr = process.communicate(timeout=self.config.timeout_seconds)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.communicate()
            except Exception:
                pass
            return _result(video_id, "timeout", request_id=request_id, diagnostic="local_transcript_timeout")
        except OSError as exc:
            return _result(video_id, "failed", request_id=request_id, diagnostic=type(exc).__name__)
        if len(stdout) > MAX_STDOUT_BYTES or len(stderr) > MAX_STDERR_BYTES:
            return _result(video_id, "failed", request_id=request_id, diagnostic="local_transcript_output_too_large")
        try:
            payload = json.loads(stdout.decode("utf-8"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return _result(video_id, "failed", request_id=request_id, diagnostic="local_transcript_json_invalid")
        return _validate_worker_payload(payload, video_id, request_id)


__all__ = [
    "FEATURE_FLAG_ENV", "LocalTranscriptAdapter", "LocalTranscriptAdapterError",
    "LocalTranscriptConfig", "LocalTranscriptResult", "build_worker_argv",
    "extraction_patch",
]
