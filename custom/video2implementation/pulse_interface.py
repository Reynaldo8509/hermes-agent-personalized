"""Side-effect-free Gemini-to-Pulse result contract.

This module carries references and independent capability states. It never
calls Gemini, YouTube, Telegram, Hermy HQ, or a document generator. Document
paths are generated from an application-owned analysis id; an LLM cannot
provide or override them.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import math
import re
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit


EXTRACTION_STATUS = "EXTRACTION_STATUS"
AUDIOVISUAL_STATUS = "AUDIOVISUAL_STATUS"
DOCUMENT_VALIDATION_STATUS = "DOCUMENT_VALIDATION_STATUS"
DELIVERY_STATUS = "DELIVERY_STATUS"

EXTRACTION_STATUSES = frozenset({"not_requested", "available", "partial", "failed"})
AUDIOVISUAL_STATUSES = frozenset({"not_requested", "available", "partial", "failed"})
DOCUMENT_STATUSES = frozenset({"not_requested", "valid", "partial", "invalid"})
FORMAT_STATUSES = frozenset({"not_requested", "generated", "failed"})
DOCUMENT_FORMATS = frozenset({"markdown", "docx", "pdf"})
DOCUMENT_EXTENSIONS = frozenset({"md", "docx", "pdf"})
DELIVERY_STATUSES = frozenset({"not_requested", "ready", "delivered", "failed"})
ANALYSIS_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,80}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PROVIDER_DIAGNOSTIC_FIELDS = frozenset({
    "http_status", "provider_code", "provider_status", "error_category",
    "error_message", "request_id", "elapsed_seconds",
})
MAX_PROVIDER_ERROR_MESSAGE = 1000
MAX_PROVIDER_REQUEST_ID = 200
MAX_PROVIDER_TEXT = 200
MAX_EXTRACTION_ERRORS = 20
MAX_EXTRACTION_ERROR_TEXT = 1500


class PulseInterfaceError(ValueError):
    """Raised for an unsafe reference or an invalid integration envelope."""


def _bounded_provider_diagnostics(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Keep only the bounded provider fields allowed across the process boundary."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise PulseInterfaceError("provider_diagnostics_object_required")
    unknown = set(value) - PROVIDER_DIAGNOSTIC_FIELDS
    if unknown:
        raise PulseInterfaceError("provider_diagnostics_field_not_allowed")
    output: dict[str, Any] = {}
    http_status = value.get("http_status")
    if http_status is not None:
        if isinstance(http_status, bool) or not isinstance(http_status, int) or not 100 <= http_status <= 599:
            raise PulseInterfaceError("provider_http_status_invalid")
        output["http_status"] = http_status
    provider_code = value.get("provider_code")
    if provider_code is not None:
        if isinstance(provider_code, bool) or not isinstance(provider_code, (int, str)):
            raise PulseInterfaceError("provider_code_invalid")
        if isinstance(provider_code, str) and (not provider_code.strip() or len(provider_code) > MAX_PROVIDER_TEXT):
            raise PulseInterfaceError("provider_code_invalid")
        output["provider_code"] = provider_code
    for field in ("provider_status", "error_category"):
        item = value.get(field)
        if item is not None:
            if not isinstance(item, str) or not item.strip() or len(item) > MAX_PROVIDER_TEXT:
                raise PulseInterfaceError(f"{field}_invalid")
            output[field] = item.strip()
    error_message = value.get("error_message")
    if error_message is not None:
        if not isinstance(error_message, str) or len(error_message) > MAX_PROVIDER_ERROR_MESSAGE:
            raise PulseInterfaceError("provider_error_message_invalid")
        output["error_message"] = error_message
    request_id = value.get("request_id")
    if request_id is not None:
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > MAX_PROVIDER_REQUEST_ID:
            raise PulseInterfaceError("provider_request_id_invalid")
        output["request_id"] = request_id.strip()
    elapsed = value.get("elapsed_seconds")
    if elapsed is not None:
        if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not math.isfinite(float(elapsed)) or not 0 <= float(elapsed) <= 3600:
            raise PulseInterfaceError("provider_elapsed_seconds_invalid")
        output["elapsed_seconds"] = round(float(elapsed), 3)
    return output or None


def _bounded_extraction_errors(value: Any) -> tuple[str, ...]:
    """Preserve extractor diagnostics as bounded strings, never arbitrary objects."""
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or len(value) > MAX_EXTRACTION_ERRORS:
        raise PulseInterfaceError("extraction_errors_invalid")
    output: list[str] = []
    for item in value:
        if not isinstance(item, str) or len(item) > MAX_EXTRACTION_ERROR_TEXT:
            raise PulseInterfaceError("extraction_error_item_invalid")
        text = item.strip()
        if text and text not in output:
            output.append(text)
    return tuple(output)


@dataclass(frozen=True)
class DocumentReference:
    analysis_id: str
    relative_path: str
    sha256: str
    size_bytes: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "analysis_id": self.analysis_id,
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


def _validate_analysis_id(analysis_id: str) -> str:
    if not isinstance(analysis_id, str) or not ANALYSIS_ID_RE.fullmatch(analysis_id):
        raise PulseInterfaceError("analysis_id_invalid")
    return analysis_id


def _controlled_path(project_root: str | Path, analysis_id: str, extension: str = "md") -> tuple[Path, str]:
    analysis_id = _validate_analysis_id(analysis_id)
    extension = str(extension).strip().lower().lstrip(".")
    if extension not in DOCUMENT_EXTENSIONS:
        raise PulseInterfaceError("document_extension_not_allowed")
    raw_root = Path(project_root).expanduser()
    if raw_root.is_symlink():
        raise PulseInterfaceError("controlled_root_symlink_forbidden")
    root = raw_root.resolve()
    artifacts = root / "artifacts"
    if artifacts.is_symlink():
        raise PulseInterfaceError("artifact_directory_symlink_forbidden")
    relative = Path("artifacts") / f"{analysis_id}.{extension}"
    raw_candidate = root / relative
    if raw_candidate.is_symlink():
        raise PulseInterfaceError("document_artifact_symlink_forbidden")
    candidate = raw_candidate.resolve()
    if root not in candidate.parents or candidate.name != f"{analysis_id}.{extension}":
        raise PulseInterfaceError("document_reference_outside_controlled_root")
    return candidate, relative.as_posix()


def document_reference_for_existing(project_root: str | Path, analysis_id: str, extension: str = "md") -> DocumentReference:
    """Return a reference to an application-generated controlled artifact."""
    path, relative = _controlled_path(project_root, analysis_id, extension)
    if not path.is_file():
        raise PulseInterfaceError("document_artifact_missing")
    content = path.read_bytes()
    return DocumentReference(analysis_id, relative, sha256(content).hexdigest(), len(content))


def _metadata_summary(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(metadata, Mapping):
        return {}
    allowed = ("title", "channel", "description_available", "technical_links_count", "duration", "published_at")
    return {key: metadata[key] for key in allowed if key in metadata and not isinstance(metadata[key], (dict, list))}


@dataclass(frozen=True)
class PulseAnalysisEnvelope:
    task_id: str
    video_id: str
    canonical_url: str
    analysis_id: str
    prompt_version: str
    extraction_status: str
    audiovisual_status: str
    document_validation_status: str
    delivery_status: str
    document_reference: DocumentReference | None
    document_formats: Mapping[str, Mapping[str, Any]]
    metadata: Mapping[str, Any]
    transcript_status: str | None
    errors: tuple[str, ...]
    limitations: tuple[str, ...]
    summary: str
    provider_metrics: Mapping[str, Any] | None = None
    provider_diagnostics: Mapping[str, Any] | None = None
    extraction_errors: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "video_id": self.video_id,
            "canonical_url": self.canonical_url,
            "analysis_id": self.analysis_id,
            "prompt_version": self.prompt_version,
            EXTRACTION_STATUS: self.extraction_status,
            AUDIOVISUAL_STATUS: self.audiovisual_status,
            DOCUMENT_VALIDATION_STATUS: self.document_validation_status,
            DELIVERY_STATUS: self.delivery_status,
            "document_reference": self.document_reference.as_dict() if self.document_reference else None,
            "document_formats": {str(key): dict(value) for key, value in self.document_formats.items()},
            "artifact_ref": self.document_reference.relative_path if self.document_reference else None,
            "artifact_sha256": self.document_reference.sha256 if self.document_reference else None,
            "metadata": dict(self.metadata),
            "transcript_status": self.transcript_status,
            "summary": self.summary,
            "errors": list(self.errors),
            "limitations": list(self.limitations),
            "provider_metrics": dict(self.provider_metrics) if self.provider_metrics else None,
            "provider_diagnostics": dict(self.provider_diagnostics) if self.provider_diagnostics else None,
            "extraction_errors": list(self.extraction_errors),
        }


def build_pulse_envelope(
    *,
    project_root: str | Path,
    task_id: str,
    video_id: str,
    canonical_url: str,
    analysis_id: str,
    prompt_version: str,
    extraction_status: str,
    audiovisual_status: str,
    document_validation_status: str,
    delivery_status: str = "not_requested",
    metadata: Mapping[str, Any] | None = None,
    transcript_status: str | None = None,
    errors: tuple[str, ...] = (),
    limitations: tuple[str, ...] = (),
    summary: str = "",
    provider_metrics: Mapping[str, Any] | None = None,
    provider_diagnostics: Mapping[str, Any] | None = None,
    extraction_errors: list[str] | tuple[str, ...] = (),
    document_formats: Mapping[str, Mapping[str, Any]] | None = None,
) -> PulseAnalysisEnvelope:
    """Build a compact result for future Pulse integration without side effects."""
    reference = document_reference_for_existing(project_root, analysis_id)
    if not isinstance(task_id, str) or not task_id.strip():
        raise PulseInterfaceError("task_id_required")
    if not isinstance(video_id, str) or not video_id.strip():
        raise PulseInterfaceError("video_id_required")
    parsed_url = urlsplit(canonical_url)
    if parsed_url.scheme != "https" or parsed_url.hostname not in {"youtube.com", "www.youtube.com", "youtu.be"}:
        raise PulseInterfaceError("canonical_url_invalid")
    allowed = (
        (extraction_status, EXTRACTION_STATUSES, "extraction_status_invalid"),
        (audiovisual_status, AUDIOVISUAL_STATUSES, "audiovisual_status_invalid"),
        (document_validation_status, DOCUMENT_STATUSES, "document_validation_status_invalid"),
        (delivery_status, DELIVERY_STATUSES, "delivery_status_invalid"),
    )
    for value, choices, error in allowed:
        if value not in choices:
            raise PulseInterfaceError(error)
    return PulseAnalysisEnvelope(
        task_id=task_id,
        video_id=video_id,
        canonical_url=canonical_url,
        analysis_id=analysis_id,
        prompt_version=prompt_version,
        extraction_status=extraction_status,
        audiovisual_status=audiovisual_status,
        document_validation_status=document_validation_status,
        delivery_status=delivery_status,
        document_reference=reference,
        document_formats=dict(document_formats or {"markdown": {"status": "generated", "reference": reference.as_dict()}}),
        metadata=_metadata_summary(metadata),
        transcript_status=transcript_status,
        errors=tuple(str(value) for value in errors),
        limitations=tuple(str(value) for value in limitations),
        summary=str(summary)[:1000],
        provider_metrics=dict(provider_metrics) if isinstance(provider_metrics, Mapping) else None,
        provider_diagnostics=_bounded_provider_diagnostics(provider_diagnostics),
        extraction_errors=_bounded_extraction_errors(extraction_errors),
    )


def build_pulse_envelope_without_artifact(
    *,
    task_id: str,
    video_id: str,
    canonical_url: str,
    analysis_id: str,
    prompt_version: str,
    extraction_status: str,
    audiovisual_status: str,
    document_validation_status: str,
    delivery_status: str = "not_requested",
    metadata: Mapping[str, Any] | None = None,
    transcript_status: str | None = None,
    errors: tuple[str, ...] = (),
    limitations: tuple[str, ...] = (),
    summary: str = "",
    provider_metrics: Mapping[str, Any] | None = None,
    provider_diagnostics: Mapping[str, Any] | None = None,
    extraction_errors: list[str] | tuple[str, ...] = (),
    document_formats: Mapping[str, Mapping[str, Any]] | None = None,
) -> PulseAnalysisEnvelope:
    """Build a compact result when no document artifact was produced.

    This is used for controlled states such as a disabled feature flag or a
    provider failure.  A successful document path must use
    :func:`build_pulse_envelope`, which verifies the artifact on disk.
    """
    _validate_analysis_id(analysis_id)
    if not isinstance(task_id, str) or not task_id.strip():
        raise PulseInterfaceError("task_id_required")
    if not isinstance(video_id, str) or not video_id.strip():
        raise PulseInterfaceError("video_id_required")
    parsed_url = urlsplit(canonical_url)
    if parsed_url.scheme != "https" or parsed_url.hostname not in {"youtube.com", "www.youtube.com", "youtu.be"}:
        raise PulseInterfaceError("canonical_url_invalid")
    allowed = (
        (extraction_status, EXTRACTION_STATUSES, "extraction_status_invalid"),
        (audiovisual_status, AUDIOVISUAL_STATUSES, "audiovisual_status_invalid"),
        (document_validation_status, DOCUMENT_STATUSES, "document_validation_status_invalid"),
        (delivery_status, DELIVERY_STATUSES, "delivery_status_invalid"),
    )
    for value, choices, error in allowed:
        if value not in choices:
            raise PulseInterfaceError(error)
    return PulseAnalysisEnvelope(
        task_id=task_id,
        video_id=video_id,
        canonical_url=canonical_url,
        analysis_id=analysis_id,
        prompt_version=prompt_version,
        extraction_status=extraction_status,
        audiovisual_status=audiovisual_status,
        document_validation_status=document_validation_status,
        delivery_status=delivery_status,
        document_reference=None,
        document_formats=dict(document_formats or {}),
        metadata=_metadata_summary(metadata),
        transcript_status=transcript_status,
        errors=tuple(str(value) for value in errors),
        limitations=tuple(str(value) for value in limitations),
        summary=str(summary)[:1000],
        provider_metrics=dict(provider_metrics) if isinstance(provider_metrics, Mapping) else None,
        provider_diagnostics=_bounded_provider_diagnostics(provider_diagnostics),
        extraction_errors=_bounded_extraction_errors(extraction_errors),
    )


def validate_pulse_envelope(value: Mapping[str, Any]) -> tuple[str, ...]:
    """Validate the compact envelope and reject raw documents or unsafe paths."""
    errors: list[str] = []
    required = {
        "task_id", "video_id", "canonical_url", "analysis_id", "prompt_version",
        EXTRACTION_STATUS, AUDIOVISUAL_STATUS, DOCUMENT_VALIDATION_STATUS, DELIVERY_STATUS,
        "document_reference", "artifact_ref", "artifact_sha256", "summary", "errors", "limitations",
    }
    errors.extend(f"field_missing:{key}" for key in sorted(required - set(value)))
    if any(key in value for key in ("document_markdown", "transcript", "full_transcript")):
        errors.append("raw_document_or_transcript_forbidden")
    provider_metrics = value.get("provider_metrics")
    if provider_metrics is not None:
        if not isinstance(provider_metrics, Mapping):
            errors.append("provider_metrics_object_required")
        else:
            invocation_count = provider_metrics.get("invocation_count")
            if invocation_count is not None and (
                not isinstance(invocation_count, int) or isinstance(invocation_count, bool)
                or invocation_count < 0 or invocation_count > 1
            ):
                errors.append("provider_invocation_count_invalid")
    document_formats = value.get("document_formats")
    if document_formats is not None:
        if not isinstance(document_formats, Mapping):
            errors.append("document_formats_object_required")
        else:
            analysis_id = value.get("analysis_id")
            for extension, entry in document_formats.items():
                if extension not in DOCUMENT_FORMATS:
                    errors.append("document_format_not_allowed")
                    continue
                if not isinstance(entry, Mapping):
                    errors.append("document_format_entry_invalid")
                    continue
                status = entry.get("status")
                if status not in FORMAT_STATUSES:
                    errors.append("document_format_status_invalid")
                reference = entry.get("reference")
                if status == "generated":
                    suffix = "md" if extension == "markdown" else extension
                    expected = f"artifacts/{analysis_id}.{suffix}"
                    if not isinstance(reference, Mapping) or reference.get("relative_path") != expected:
                        errors.append("document_format_reference_invalid")
                    elif Path(str(reference.get("relative_path"))).is_absolute() or ".." in Path(str(reference.get("relative_path"))).parts:
                        errors.append("document_format_path_unsafe")
                    elif not SHA256_RE.fullmatch(str(reference.get("sha256", ""))):
                        errors.append("document_format_hash_invalid")
                    elif not isinstance(reference.get("size_bytes"), int) or reference.get("size_bytes", 0) < 1:
                        errors.append("document_format_size_invalid")
                elif reference is not None:
                    errors.append("document_format_reference_unexpected")
    try:
        _bounded_provider_diagnostics(value.get("provider_diagnostics"))
    except PulseInterfaceError as exc:
        errors.append(str(exc))
    try:
        _bounded_extraction_errors(value.get("extraction_errors"))
    except PulseInterfaceError as exc:
        errors.append(str(exc))
    reference = value.get("document_reference")
    if reference is None:
        if value.get(DOCUMENT_VALIDATION_STATUS) != "not_requested":
            errors.append("document_reference_required_for_document_status")
        if value.get("artifact_ref") is not None or value.get("artifact_sha256") is not None:
            errors.append("artifact_reference_must_be_null_without_document")
    elif not isinstance(reference, Mapping):
        errors.append("document_reference_required")
    else:
        relative = reference.get("relative_path")
        analysis_id = reference.get("analysis_id")
        if not isinstance(relative, str) or relative != f"artifacts/{analysis_id}.md":
            errors.append("document_reference_not_generated")
        if isinstance(relative, str) and (relative.startswith("/") or ".." in Path(relative).parts):
            errors.append("document_reference_path_unsafe")
        if not isinstance(reference.get("sha256"), str) or not SHA256_RE.fullmatch(reference["sha256"]):
            errors.append("document_reference_hash_invalid")
        if value.get("artifact_ref") != relative:
            errors.append("artifact_ref_mismatch")
        if value.get("artifact_sha256") != reference.get("sha256"):
            errors.append("artifact_sha256_mismatch")
    status_sets = (
        (EXTRACTION_STATUS, EXTRACTION_STATUSES),
        (AUDIOVISUAL_STATUS, AUDIOVISUAL_STATUSES),
        (DOCUMENT_VALIDATION_STATUS, DOCUMENT_STATUSES),
        (DELIVERY_STATUS, DELIVERY_STATUSES),
    )
    for field_name, choices in status_sets:
        if field_name in value and value[field_name] not in choices:
            errors.append(f"{field_name.lower()}_invalid")
    return tuple(dict.fromkeys(errors))


__all__ = [
    "AUDIOVISUAL_STATUS", "DELIVERY_STATUS", "DOCUMENT_VALIDATION_STATUS",
    "EXTRACTION_STATUS", "DocumentReference", "PulseAnalysisEnvelope",
    "PulseInterfaceError", "build_pulse_envelope", "document_reference_for_existing",
    "build_pulse_envelope_without_artifact", "validate_pulse_envelope",
]
