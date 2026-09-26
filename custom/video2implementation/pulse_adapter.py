"""Isolated Pulse adapter for the optional VIDEO2IMPLEMENTATION capability.

The adapter is deliberately not imported by the production bridge.  Pulse can
use it in a later, explicitly authorized activation phase.  Its default state
is disabled independently of credentials, and all provider calls are injected
or gated by that feature flag.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
import re
from pathlib import Path
from typing import Any, Callable, Mapping

from .artifact_store import ControlledArtifactStore, ArtifactStoreError
from .contract import VideoTaskContract, VideoTaskContractError
from .document_export import export_requested_formats
from .gemini_client import GeminiClientConfig, GeminiVideoClient, GeminiVideoResult
from .markdown_contract import validate_markdown_document
from .prompt_loader import PromptDocument, PromptIntegrityError, load_master_prompt
from .pulse_interface import (
    PulseAnalysisEnvelope,
    PulseInterfaceError,
    build_pulse_envelope,
    build_pulse_envelope_without_artifact,
    validate_pulse_envelope,
)


VIDEO_YOUTUBE_ANALYZE_KIND = "video.youtube.analyze"
VIDEO2IMPLEMENTATION_ANALYSIS_TYPE = "video2implementation"
FEATURE_FLAG_ENV = "VIDEO2IMPLEMENTATION_GEMINI_ENABLED"
DEFAULT_PROMPT_VERSION = "v3"
DEFAULT_TIMEOUT_SECONDS = 600.0
DEFAULT_MODEL = "gemini-3.6-flash"
MODEL_ENV = "VIDEO2IMPLEMENTATION_GEMINI_MODEL"
ENABLED_VALUES = frozenset({"1", "true", "yes", "on"})


class Video2ImplementationAdapterError(ValueError):
    """Raised for invalid task data or an unsafe isolated integration state."""


@dataclass(frozen=True)
class Video2ImplementationFeatureConfig:
    """Configuration whose default is always disabled."""

    enabled: bool = False
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    model: str = DEFAULT_MODEL

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Video2ImplementationFeatureConfig":
        values = os.environ if environ is None else environ
        raw = str(values.get(FEATURE_FLAG_ENV, "")).strip().lower()
        try:
            timeout = float(values.get("VIDEO2IMPLEMENTATION_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT_SECONDS
        if timeout <= 0 or timeout > 3600:
            timeout = DEFAULT_TIMEOUT_SECONDS
        model = str(values.get(MODEL_ENV, DEFAULT_MODEL)).strip() or DEFAULT_MODEL
        if model != DEFAULT_MODEL:
            raise Video2ImplementationAdapterError("video2implementation_model_not_allowed")
        return cls(enabled=raw in ENABLED_VALUES, timeout_seconds=timeout, model=model)


def _analysis_id(task_id: str, video_id: str) -> str:
    digest = sha256(f"{task_id}|{video_id}|video2implementation".encode("utf-8")).hexdigest()
    return f"v2i-{digest[:48]}"


def _dedupe(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _status_from_extraction(extraction: Mapping[str, Any] | None) -> str:
    if not isinstance(extraction, Mapping):
        return "not_requested"
    source = extraction.get("source_status")
    if isinstance(source, Mapping):
        source = source.get("status")
    if source in {"extracted", "available"}:
        return "available"
    if source == "partial":
        return "partial"
    if source in {"failed", "invalid_request"}:
        return "failed"
    if extraction.get("metadata") or extraction.get("transcript"):
        return "partial"
    return "not_requested"


def _transcript_status(extraction: Mapping[str, Any] | None) -> str | None:
    value = extraction.get("transcript") if isinstance(extraction, Mapping) else None
    return value.get("status") if isinstance(value, Mapping) and isinstance(value.get("status"), str) else None


def _duration(extraction: Mapping[str, Any] | None) -> float | None:
    metadata = extraction.get("metadata") if isinstance(extraction, Mapping) else None
    value = metadata.get("duration") if isinstance(metadata, Mapping) else None
    return float(value) if isinstance(value, (int, float)) and value >= 0 else None


def _provider_metrics(result: GeminiVideoResult) -> dict[str, Any]:
    """Return bounded provider evidence without exposing response content."""
    metrics: dict[str, Any] = {
        "model": result.model,
        "provider_status": result.provider_status,
        "invocation_count": result.invocation_count,
        "elapsed_seconds": round(max(0.0, float(result.elapsed_seconds)), 3),
    }
    if isinstance(result.usage, Mapping):
        metrics["usage"] = {
            str(key): value
            for key, value in result.usage.items()
            if isinstance(key, str) and isinstance(value, (str, int, float, bool))
        }
    return metrics


def _redact_diagnostic_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    text = value[:limit]
    text = re.sub(r"(?i)(authorization|x-goog-api-key|api[_ -]?key)\s*[:=]\s*bearer\s+[^,\s]+", r"\1=[REDACTED]", text)
    text = re.sub(r"(?i)(authorization|x-goog-api-key|api[_ -]?key|bearer)\s*[:=]\s*[^,\s]+", r"\1=[REDACTED]", text)
    text = re.sub(r"(?i)\b(?:AIza[0-9A-Za-z_-]{20,}|sk-[0-9A-Za-z_-]{12,}|ak_[0-9A-Za-z_-]{12,})\b", "[REDACTED]", text)
    return text[:limit]


def _provider_diagnostics(result: GeminiVideoResult) -> dict[str, Any] | None:
    """Map client diagnostics to a bounded, stable envelope contract."""
    raw = result.diagnostics if isinstance(result.diagnostics, Mapping) else {}
    output: dict[str, Any] = {}
    http_status = raw.get("http_status")
    if isinstance(http_status, int) and not isinstance(http_status, bool) and 100 <= http_status <= 599:
        output["http_status"] = http_status
    provider_code = raw.get("provider_code")
    if isinstance(provider_code, (int, str)) and not isinstance(provider_code, bool):
        if not isinstance(provider_code, str) or provider_code.strip():
            output["provider_code"] = provider_code if isinstance(provider_code, int) else provider_code[:200]
    provider_status = raw.get("provider_status")
    if isinstance(provider_status, str) and provider_status.strip():
        output["provider_status"] = provider_status.strip()[:200]
    category = raw.get("category") or raw.get("error_category") or result.error_code
    if isinstance(category, str) and category.strip():
        output["error_category"] = category.strip()[:200]
    message = raw.get("message") or raw.get("error_message") or raw.get("detail")
    redacted_message = _redact_diagnostic_text(message, 1000)
    if redacted_message:
        output["error_message"] = redacted_message
    request_id = raw.get("request_id")
    if isinstance(request_id, str) and request_id.strip():
        output["request_id"] = request_id.strip()[:200]
    try:
        elapsed = float(result.elapsed_seconds)
    except (TypeError, ValueError):
        elapsed = None
    if elapsed is not None and elapsed >= 0:
        output["elapsed_seconds"] = round(min(elapsed, 3600.0), 3)
    return output or None


def _extraction_errors(extraction: Mapping[str, Any] | None) -> tuple[str, ...]:
    if not isinstance(extraction, Mapping):
        return ()
    raw = extraction.get("extraction_errors")
    if not isinstance(raw, (list, tuple)):
        return ()
    output: list[str] = []
    for item in raw[:20]:
        if isinstance(item, str) and item.strip():
            value = item.strip()[:1500]
            if value not in output:
                output.append(value)
    return tuple(output)


def _task_contract(request: Mapping[str, Any]) -> VideoTaskContract:
    if not isinstance(request, Mapping) or request.get("kind") != VIDEO_YOUTUBE_ANALYZE_KIND:
        raise Video2ImplementationAdapterError("video_youtube_kind_required")
    metadata = request.get("metadata") if isinstance(request.get("metadata"), Mapping) else {}
    source = dict(metadata)
    source.update(request)
    source.setdefault("task_id", request.get("id"))
    source.setdefault("requester", request.get("origin"))
    try:
        task = VideoTaskContract.from_mapping(source)
    except VideoTaskContractError as exc:
        raise Video2ImplementationAdapterError(str(exc)) from exc
    if task.analysis_type != VIDEO2IMPLEMENTATION_ANALYSIS_TYPE:
        raise Video2ImplementationAdapterError("video2implementation_analysis_type_required")
    return task


class Video2ImplementationPulseAdapter:
    """Prepare a compact Gemini result for Pulse without production side effects."""

    def __init__(
        self,
        *,
        project_root: str | Path,
        prompt_dir: str | Path | None = None,
        config: Video2ImplementationFeatureConfig | None = None,
        client: Any = None,
        artifact_store: ControlledArtifactStore | None = None,
        analysis_id_factory: Callable[[str, str], str] = _analysis_id,
    ) -> None:
        self.project_root = Path(project_root).expanduser().resolve()
        self.prompt_dir = Path(prompt_dir).expanduser().resolve() if prompt_dir else self.project_root / "prompts"
        self.config = config or Video2ImplementationFeatureConfig.from_env()
        self.client = client
        self.artifact_store = artifact_store or ControlledArtifactStore(self.project_root)
        self.analysis_id_factory = analysis_id_factory
        self._results: dict[str, PulseAnalysisEnvelope] = {}

    @staticmethod
    def supports(request: Mapping[str, Any]) -> bool:
        metadata = request.get("metadata") if isinstance(request, Mapping) and isinstance(request.get("metadata"), Mapping) else {}
        analysis_type = request.get("analysis_type") if isinstance(request, Mapping) else None
        analysis_type = analysis_type or metadata.get("analysis_type")
        return (
            isinstance(request, Mapping)
            and request.get("kind") == VIDEO_YOUTUBE_ANALYZE_KIND
            and str(analysis_type or "").strip().lower() == VIDEO2IMPLEMENTATION_ANALYSIS_TYPE
        )

    def process(
        self,
        request: Mapping[str, Any],
        *,
        extraction_result: Mapping[str, Any] | None = None,
    ) -> PulseAnalysisEnvelope | None:
        """Handle only the explicit video2implementation route.

        ``None`` means Pulse should preserve its existing summary, transcript,
        metadata, or social path.  This makes a YouTube URL alone insufficient
        to invoke Gemini.
        """
        if not self.supports(request):
            return None
        task = _task_contract(request)
        extraction_status = _status_from_extraction(extraction_result)
        transcript_status = _transcript_status(extraction_result)
        if task.task_id in self._results:
            return self._results[task.task_id]
        analysis_id = self.analysis_id_factory(task.task_id, task.video_id)
        if not isinstance(analysis_id, str) or not analysis_id:
            raise Video2ImplementationAdapterError("analysis_id_factory_invalid")

        if not self.config.enabled:
            envelope = build_pulse_envelope_without_artifact(
                task_id=task.task_id,
                video_id=task.video_id,
                canonical_url=task.video_url,
                analysis_id=analysis_id,
                prompt_version=DEFAULT_PROMPT_VERSION,
                extraction_status=extraction_status,
                audiovisual_status="not_requested",
                document_validation_status="not_requested",
                transcript_status=transcript_status,
                extraction_errors=_extraction_errors(extraction_result),
                limitations=("video2implementation_gemini_disabled",),
                summary="VIDEO2IMPLEMENTATION está preparado pero desactivado; no se llamó a Gemini.",
            )
            self._results[task.task_id] = envelope
            return envelope

        try:
            prompt = load_master_prompt(self.prompt_dir)
        except PromptIntegrityError as exc:
            envelope = self._failure(
                task, analysis_id, extraction_status, transcript_status,
                "master_prompt_unavailable", str(exc), DEFAULT_PROMPT_VERSION,
            )
            self._results[task.task_id] = envelope
            return envelope

        client = self.client
        if client is None:
            client = GeminiVideoClient(GeminiClientConfig(
                model=self.config.model,
                timeout_seconds=self.config.timeout_seconds,
                max_calls_per_task=1,
            ))
        try:
            result = client.analyze_master_markdown(
                task_id=task.task_id,
                video_url=task.video_url,
                prompt=prompt,
                duration_seconds=_duration(extraction_result),
                allow_external_call=True,
            )
        except Exception as exc:  # provider adapters expose structured errors where possible
            envelope = self._failure(
                task, analysis_id, extraction_status, transcript_status,
                "gemini_adapter_exception", type(exc).__name__, prompt.version,
            )
            self._results[task.task_id] = envelope
            return envelope
        envelope = self._envelope_from_result(
            task, analysis_id, prompt, result, extraction_status, transcript_status,
            _duration(extraction_result), _extraction_errors(extraction_result),
        )
        self._results[task.task_id] = envelope
        return envelope

    def _failure(
        self,
        task: VideoTaskContract,
        analysis_id: str,
        extraction_status: str,
        transcript_status: str | None,
        code: str,
        detail: str,
        prompt_version: str,
        extraction_errors: tuple[str, ...] = (),
        provider_diagnostics: Mapping[str, Any] | None = None,
    ) -> PulseAnalysisEnvelope:
        return build_pulse_envelope_without_artifact(
            task_id=task.task_id,
            video_id=task.video_id,
            canonical_url=task.video_url,
            analysis_id=analysis_id,
            prompt_version=prompt_version,
            extraction_status=extraction_status,
            audiovisual_status="failed",
            document_validation_status="not_requested",
            transcript_status=transcript_status,
            extraction_errors=extraction_errors,
            errors=_dedupe([code]),
            limitations=(detail[:300],),
            summary="VIDEO2IMPLEMENTATION no produjo un artefacto; el diagnóstico quedó estructurado.",
            provider_diagnostics=provider_diagnostics,
        )

    def _envelope_from_result(
        self,
        task: VideoTaskContract,
        analysis_id: str,
        prompt: PromptDocument,
        result: GeminiVideoResult,
        extraction_status: str,
        transcript_status: str | None,
        duration_seconds: float | None,
        extraction_errors: tuple[str, ...],
    ) -> PulseAnalysisEnvelope:
        errors = list(result.validation_errors)
        if result.error_code:
            errors.append(result.error_code)
        output = result.output_text
        report = None
        stored = None
        document_formats: dict[str, dict[str, Any]] = {}
        document_status = "not_requested"
        audiovisual_status = "failed"
        limitations: list[str] = []
        provider_diagnostics = _provider_diagnostics(result)
        if isinstance(output, str) and output.strip():
            report = validate_markdown_document(
                output,
                task_id=task.task_id,
                video_id=task.video_id,
                canonical_url=task.video_url,
                prompt_version=prompt.version,
                provider_status=result.provider_status,
                duration_seconds=duration_seconds,
                usage_metrics=result.usage,
            )
            document_status = report.status.lower()
            audiovisual_status = "available" if report.status == "VALID" else "partial" if report.status == "PARTIAL" else "failed"
            errors.extend(report.validation_errors)
            if report.status == "PARTIAL":
                limitations.append("document_validation_partial_evidence_preserved")
            try:
                exported = export_requested_formats(
                    self.artifact_store,
                    analysis_id,
                    output,
                    task.requested_formats,
                )
                stored = exported.get("markdown").artifact if exported.get("markdown") else None
                for extension, exported_document in exported.items():
                    if exported_document.artifact is None:
                        document_formats[extension] = {
                            "status": "not_requested" if exported_document.error == "not_requested" else "failed",
                            "error": exported_document.error,
                        }
                    else:
                        document_formats[extension] = {
                            "status": "generated",
                            "reference": exported_document.artifact.reference.as_dict(),
                        }
                export_errors = [
                    f"{extension}:{item.error}"
                    for extension, item in exported.items()
                    if item.artifact is None and item.error not in (None, "not_requested")
                ]
                errors.extend(export_errors)
                if export_errors:
                    limitations.append("one_or_more_requested_document_formats_failed")
            except ArtifactStoreError as exc:
                return self._failure(
                    task, analysis_id, extraction_status, transcript_status,
                    str(exc), "artifact storage rejected the generated document", prompt.version,
                    extraction_errors=extraction_errors,
                    provider_diagnostics=provider_diagnostics,
                )
        if result.status == "INVALID" and not output:
            audiovisual_status = "failed"
        if not stored:
            return build_pulse_envelope_without_artifact(
                task_id=task.task_id,
                video_id=task.video_id,
                canonical_url=task.video_url,
                analysis_id=analysis_id,
                prompt_version=prompt.version,
                extraction_status=extraction_status,
                audiovisual_status=audiovisual_status,
                document_validation_status=document_status,
                transcript_status=transcript_status,
                extraction_errors=extraction_errors,
                errors=_dedupe([str(value) for value in errors]),
                limitations=_dedupe(limitations),
                summary="VIDEO2IMPLEMENTATION no tiene un artefacto disponible; se conserva el diagnóstico.",
                provider_metrics=_provider_metrics(result),
                provider_diagnostics=provider_diagnostics,
                document_formats=document_formats,
            )
        envelope = build_pulse_envelope(
            project_root=self.project_root,
            task_id=task.task_id,
            video_id=task.video_id,
            canonical_url=task.video_url,
            analysis_id=analysis_id,
            prompt_version=prompt.version,
            extraction_status=extraction_status,
            audiovisual_status=audiovisual_status,
            document_validation_status=document_status,
            transcript_status=transcript_status,
            extraction_errors=extraction_errors,
            metadata={},
            errors=_dedupe([str(value) for value in errors]),
            limitations=_dedupe(limitations),
            summary="VIDEO2IMPLEMENTATION produjo Markdown; el resultado breve referencia el artefacto controlado.",
            provider_metrics=_provider_metrics(result),
            provider_diagnostics=provider_diagnostics,
            document_formats=document_formats,
        )
        validation_errors = validate_pulse_envelope(envelope.as_dict())
        if validation_errors:
            raise PulseInterfaceError("pulse_envelope_invalid:" + ",".join(validation_errors))
        return envelope


__all__ = [
    "DEFAULT_PROMPT_VERSION", "FEATURE_FLAG_ENV", "VIDEO2IMPLEMENTATION_ANALYSIS_TYPE",
    "VIDEO_YOUTUBE_ANALYZE_KIND", "Video2ImplementationAdapterError",
    "Video2ImplementationFeatureConfig", "Video2ImplementationPulseAdapter",
]
