"""Isolated Gemini Interactions client for VIDEO2IMPLEMENTATION.

The client is deliberately separate from Hermes' general Gemini adapter.  It
accepts only a validated public YouTube URL, performs one provider call at
most per task, exposes bounded diagnostics, and never executes model output.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import time
from typing import Any, Mapping

import httpx

from .contract import parse_youtube_url
from .gemini_contract import audiovisual_probe_schema, validate_gemini_result
from .gemini_preparation import GeminiPreparationConfig, classify_provider_error, prepare_gemini_request
from .markdown_contract import validate_markdown_document
from .prompt_loader import PromptDocument


# Compatibility alias for callers that imported the probe schema from this
# module during Phase 3B. The canonical contract now lives in gemini_contract.
PROBE_SCHEMA: dict[str, Any] = audiovisual_probe_schema()


DEFAULT_PROBE_INSTRUCTION = (
    "Analyze this public video visually. Return JSON only with exactly these fields: "
    "video_id, scene_description, visual_elements, observable_actions, timestamps, "
    "transcript_evidence, limitations, unverifiable. "
    "Use visual observations only for visual_elements and observable_actions. "
    "Use transcript_evidence only for spoken or subtitle content. "
    "Do not invent timestamps; use an empty array when a timestamp cannot be verified. "
    "Do not provide a full transcript, implementation guide, commands, or tool calls."
)


@dataclass(frozen=True)
class GeminiClientConfig:
    model: str = "gemini-3.8-flash"
    endpoint: str = "https://generativelanguage.googleapis.com/v1beta/interactions"
    timeout_seconds: float = 600.0
    max_calls_per_task: int = 1
    max_response_bytes: int = 512_000
    max_input_chars: int = 300_000
    max_output_tokens: int = 32_768


@dataclass(frozen=True)
class GeminiVideoResult:
    task_id: str
    video_id: str
    canonical_url: str
    status: str
    output_text: str | None
    parsed_output: dict[str, Any] | None
    validation_errors: tuple[str, ...]
    model: str
    provider_status: str | None
    usage: dict[str, Any] | None
    elapsed_seconds: float
    invocation_count: int
    error_code: str | None = None
    diagnostics: Mapping[str, Any] | None = None


class GeminiVideoClientError(RuntimeError):
    """Bounded, non-retrying error returned by the isolated client."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def _env_api_key() -> str:
    # The value is read only from the already configured Hermes environment and
    # is never included in a request URL, diagnostics, or result object.
    return (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()


def _extract_text(payload: Mapping[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str):
        return direct
    blocks: list[str] = []
    for container in (payload.get("outputs"), payload.get("output")):
        if isinstance(container, list):
            for item in container:
                if isinstance(item, Mapping) and isinstance(item.get("text"), str):
                    blocks.append(item["text"])
    steps = payload.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, Mapping):
                continue
            content = step.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, Mapping) and isinstance(item.get("text"), str):
                        blocks.append(item["text"])
    return "\n".join(blocks).strip()


def _usage(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    value = payload.get("usage")
    return dict(value) if isinstance(value, Mapping) else None


def _json_from_output(text: str) -> dict[str, Any]:
    if not text.strip():
        raise GeminiVideoClientError("gemini_empty_response")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GeminiVideoClientError("gemini_malformed_json", str(exc)) from exc
    if not isinstance(value, dict):
        raise GeminiVideoClientError("gemini_result_object_required")
    return value


def validate_visual_probe(value: Mapping[str, Any], expected_video_id: str) -> tuple[str, tuple[str, ...]]:
    """Validate the small one-shot probe without repairing provider output."""
    errors: list[str] = []
    required = set(PROBE_SCHEMA["required"])
    errors.extend(f"field_missing:{field}" for field in sorted(required - set(value)))
    if value.get("video_id") != expected_video_id:
        errors.append("video_id_mismatch")
    if not isinstance(value.get("scene_description"), str) or not value.get("scene_description", "").strip():
        errors.append("scene_description_required")
    for field in ("visual_elements", "observable_actions", "timestamps", "transcript_evidence", "limitations", "unverifiable"):
        if not isinstance(value.get(field), list):
            errors.append(f"{field}_array_required")
    for field in ("visual_elements", "observable_actions", "transcript_evidence"):
        for index, item in enumerate(value.get(field, [])):
            if not isinstance(item, Mapping) or not item:
                errors.append(f"{field}[{index}]_object_empty")
    for index, item in enumerate(value.get("timestamps", [])):
        if not isinstance(item, Mapping):
            errors.append(f"timestamps[{index}]_object_required")
            continue
        start = item.get("start_seconds", item.get("start"))
        end = item.get("end_seconds", item.get("end", start))
        if start is None and end is None:
            errors.append(f"timestamps[{index}]_time_unverified")
        elif not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or start < 0 or end < start:
            errors.append(f"timestamps[{index}]_range_invalid")
    if errors:
        return "PARTIAL", tuple(errors)
    if not any(isinstance(item, Mapping) and item for item in value.get("visual_elements", [])) and not any(isinstance(item, Mapping) and item for item in value.get("observable_actions", [])):
        return "PARTIAL", ("visual_evidence_absent",)
    return "VALID", ()


def _provider_error(status_code: int, body: str) -> str:
    lowered = body.lower()
    if status_code in (401, 403) or "api key" in lowered or "unauthorized" in lowered:
        return classify_provider_error("auth")
    if status_code == 429 or "quota" in lowered or "resource_exhausted" in lowered:
        return classify_provider_error("quota")
    if status_code in {408, 504}:
        return "gemini_timeout"
    if status_code in {413} or "context" in lowered and ("limit" in lowered or "exceed" in lowered):
        return "gemini_context_limit"
    if status_code == 400 and any(term in lowered for term in ("schema", "invalid_argument", "json schema")):
        return "gemini_schema_rejected"
    if status_code == 400 and any(term in lowered for term in ("malformed", "invalid request", "bad request")):
        return "gemini_request_malformed"
    if status_code in {500, 502, 503, 504}:
        return "gemini_provider_temporary_error"
    if status_code == 404 or "model" in lowered and "not found" in lowered:
        return classify_provider_error("model")
    if "not found" in lowered or "unavailable" in lowered or "private" in lowered or "inaccessible" in lowered:
        return "gemini_video_inaccessible"
    return "gemini_provider_error"


def _redact_error_text(value: str, limit: int = 1000) -> str:
    text = (value or "")[:limit]
    text = re.sub(r"(?i)(authorization|x-goog-api-key|api[_ -]?key)\s*[:=]\s*bearer\s+[^,\s]+", r"\1=[REDACTED]", text)
    text = re.sub(r"(?i)(authorization|x-goog-api-key|api[_ -]?key|bearer)\s*[:=]\s*[^,\s]+", r"\1=[REDACTED]", text)
    text = re.sub(r"(?i)\b(?:AIza[0-9A-Za-z_-]{20,}|sk-[0-9A-Za-z_-]{12,}|ak_[0-9A-Za-z_-]{12,})\b", "[REDACTED]", text)
    return text


def _provider_diagnostics(status_code: int, body: str, headers: Any = None) -> dict[str, Any]:
    """Keep bounded provider evidence without retaining the raw response or secrets."""
    diagnostics: dict[str, Any] = {"http_status": status_code}
    parsed: Mapping[str, Any] | None = None
    try:
        candidate = json.loads(body)
        if isinstance(candidate, Mapping):
            parsed = candidate
    except (TypeError, ValueError):
        pass
    error = parsed.get("error") if isinstance(parsed, Mapping) else None
    if not isinstance(error, Mapping):
        error = parsed if isinstance(parsed, Mapping) else {}
    for source, target in (("code", "provider_code"), ("status", "provider_status"), ("message", "message")):
        value = error.get(source)
        if isinstance(value, (str, int)) and value not in ("", None):
            diagnostics[target] = _redact_error_text(str(value)) if source == "message" else value
    details = error.get("details") if isinstance(error, Mapping) else None
    if isinstance(details, list):
        fields: list[dict[str, str]] = []
        for detail in details:
            if not isinstance(detail, Mapping):
                continue
            violations = detail.get("fieldViolations")
            if not isinstance(violations, list):
                continue
            for violation in violations:
                if isinstance(violation, Mapping):
                    item: dict[str, str] = {}
                    if isinstance(violation.get("field"), str):
                        item["field"] = violation["field"][:200]
                    if isinstance(violation.get("description"), str):
                        item["description"] = _redact_error_text(violation["description"], 400)
                    if item:
                        fields.append(item)
        if fields:
            diagnostics["field_violations"] = fields[:10]
    if isinstance(headers, Mapping):
        for name in ("x-request-id", "x-goog-request-id", "request-id"):
            value = headers.get(name) or headers.get(name.title())
            if isinstance(value, str) and value:
                diagnostics["request_id"] = value[:200]
                break
    if "message" not in diagnostics and body:
        diagnostics["message"] = _redact_error_text(body)
    return diagnostics


class GeminiVideoClient:
    """One-shot client for a public YouTube URL through Gemini Interactions."""

    def __init__(self, config: GeminiClientConfig = GeminiClientConfig(), *, api_key: str | None = None, http_client: Any = None) -> None:
        if config.max_calls_per_task != 1:
            raise GeminiVideoClientError("max_calls_per_task_must_be_one")
        if config.max_response_bytes < 1 or config.max_output_tokens < 1:
            raise GeminiVideoClientError("gemini_limits_invalid")
        self.config = config
        self._api_key = (api_key if api_key is not None else _env_api_key()).strip()
        self._http = http_client
        self.invocation_count = 0

    def build_probe_request(self, video_url: str, instruction: str = DEFAULT_PROBE_INSTRUCTION) -> dict[str, Any]:
        reference = parse_youtube_url(video_url)
        if not isinstance(instruction, str) or not instruction.strip():
            raise GeminiVideoClientError("probe_instruction_required")
        return {
            "model": self.config.model,
            "input": [
                {"type": "text", "text": instruction.strip()},
                {"type": "video", "uri": reference.normalized_url},
            ],
            "response_format": {"type": "text", "mime_type": "application/json", "schema": PROBE_SCHEMA},
            "generation_config": {"max_output_tokens": self.config.max_output_tokens},
            "stream": False,
            "store": False,
            "background": False,
            "tools": [],
        }

    def build_master_prompt_request(
        self,
        *,
        task_id: str,
        video_url: str,
        prompt: PromptDocument,
        metadata: Mapping[str, Any] | None = None,
        transcript: Mapping[str, Any] | None = None,
    ):
        """Prepare the complete master-prompt request without contacting Gemini."""
        preparation = GeminiPreparationConfig(
            model=self.config.model,
            endpoint=self.config.endpoint,
            timeout_seconds=int(self.config.timeout_seconds),
            max_calls_per_task=self.config.max_calls_per_task,
            max_input_chars=self.config.max_input_chars,
            max_output_tokens=self.config.max_output_tokens,
        )
        return prepare_gemini_request(
            task_id=task_id,
            video_url=video_url,
            prompt=prompt,
            metadata=metadata,
            transcript=transcript,
            config=preparation,
        )

    def build_master_markdown_request(
        self,
        *,
        task_id: str,
        video_url: str,
        prompt: PromptDocument,
    ) -> dict[str, Any]:
        """Prepare a text/Markdown request without embedding the JSON schema.

        The master prompt remains byte-for-byte unchanged. This route is
        intentionally independent from the structured JSON route because the
        provider rejected the large response schema used by the earlier probe.
        """
        reference = parse_youtube_url(video_url)
        if not isinstance(task_id, str) or not task_id.strip():
            raise GeminiVideoClientError("task_id_required")
        if not isinstance(prompt, PromptDocument) or not prompt.text.strip():
            raise GeminiVideoClientError("master_prompt_required")
        if len(prompt.text) > self.config.max_input_chars:
            raise GeminiVideoClientError("prompt_input_too_large")
        return {
            "model": self.config.model,
            "input": [
                {"type": "text", "text": prompt.text},
                {"type": "video", "uri": reference.normalized_url},
            ],
            "response_format": {"type": "text", "mime_type": "text/plain"},
            "generation_config": {"max_output_tokens": self.config.max_output_tokens},
            "stream": False,
            "store": False,
            "background": False,
            "tools": [],
        }

    def _execute_request(
        self,
        *,
        task_id: str,
        video_url: str,
        request: Mapping[str, Any],
        validator: Any,
        parser: Any = _json_from_output,
    ) -> GeminiVideoResult:
        reference = parse_youtube_url(video_url)
        if not isinstance(task_id, str) or not task_id.strip():
            raise GeminiVideoClientError("task_id_required")
        if self.invocation_count >= self.config.max_calls_per_task:
            raise GeminiVideoClientError("gemini_call_limit_exceeded")
        if not self._api_key:
            raise GeminiVideoClientError("gemini_authentication_not_configured")
        self.invocation_count += 1
        started = time.monotonic()
        try:
            client = self._http or httpx.Client(timeout=self.config.timeout_seconds)
            response = client.post(
                self.config.endpoint,
                json=request,
                headers={"Content-Type": "application/json", "Accept": "application/json", "x-goog-api-key": self._api_key},
                timeout=self.config.timeout_seconds,
            )
            elapsed = time.monotonic() - started
            body_bytes = response.content
            if len(body_bytes) > self.config.max_response_bytes:
                return GeminiVideoResult(task_id, reference.video_id, reference.normalized_url, "INVALID", None, None, ("response_too_large",), self.config.model, None, None, elapsed, self.invocation_count, "gemini_response_too_large")
            body_text = body_bytes.decode("utf-8", errors="replace")
            if response.status_code >= 400:
                code = _provider_error(response.status_code, body_text)
                diagnostics = _provider_diagnostics(response.status_code, body_text, getattr(response, "headers", None))
                diagnostics["category"] = code
                return GeminiVideoResult(task_id, reference.video_id, reference.normalized_url, "INVALID", None, None, (code,), self.config.model, None, None, elapsed, self.invocation_count, code, diagnostics)
            try:
                payload = response.json()
            except (ValueError, json.JSONDecodeError) as exc:
                return GeminiVideoResult(task_id, reference.video_id, reference.normalized_url, "INVALID", None, None, ("gemini_invalid_json_response",), self.config.model, None, None, elapsed, self.invocation_count, "gemini_invalid_json_response", {"detail": str(exc)})
            provider_status = payload.get("status") if isinstance(payload, Mapping) else None
            if provider_status in {"failed", "cancelled"}:
                code = "gemini_processing_failed" if provider_status == "failed" else "gemini_cancelled"
                return GeminiVideoResult(task_id, reference.video_id, reference.normalized_url, "INVALID", None, None, (code,), self.config.model, provider_status, _usage(payload), elapsed, self.invocation_count, code)
            text = _extract_text(payload)
            try:
                parsed = parser(text)
            except GeminiVideoClientError as exc:
                return GeminiVideoResult(task_id, reference.video_id, reference.normalized_url, "INVALID", text or None, None, (exc.code,), self.config.model, provider_status, _usage(payload), elapsed, self.invocation_count, exc.code)
            status, errors = validator(parsed)
            if provider_status == "incomplete":
                if "provider_incomplete" not in errors:
                    errors = tuple(errors) + ("provider_incomplete",)
                if status == "VALID":
                    status = "PARTIAL"
            if isinstance(parsed, dict) and "usage_metrics" in parsed:
                parsed["usage_metrics"] = _usage(payload) or {}
                if provider_status == "incomplete" and "provider_incomplete" not in parsed.get("validation_errors", []):
                    parsed["validation_errors"] = list(parsed.get("validation_errors", [])) + ["provider_incomplete"]
                    parsed["status"] = "PARTIAL"
            return GeminiVideoResult(task_id, reference.video_id, reference.normalized_url, status, text, parsed, errors, self.config.model, provider_status, _usage(payload), elapsed, self.invocation_count)
        except httpx.TimeoutException:
            elapsed = time.monotonic() - started
            return GeminiVideoResult(task_id, reference.video_id, reference.normalized_url, "INVALID", None, None, ("gemini_timeout",), self.config.model, None, None, elapsed, self.invocation_count, "gemini_timeout")
        except httpx.HTTPError as exc:
            elapsed = time.monotonic() - started
            return GeminiVideoResult(task_id, reference.video_id, reference.normalized_url, "INVALID", None, None, ("gemini_transport_error",), self.config.model, None, None, elapsed, self.invocation_count, "gemini_transport_error", {"detail": str(exc)})
        finally:
            if self._http is None and "client" in locals():
                client.close()

    def analyze_video(self, *, task_id: str, video_url: str, instruction: str = DEFAULT_PROBE_INSTRUCTION) -> GeminiVideoResult:
        request = self.build_probe_request(video_url, instruction)
        return self._execute_request(
            task_id=task_id,
            video_url=video_url,
            request=request,
            validator=lambda value: validate_visual_probe(value, parse_youtube_url(video_url).video_id),
        )

    def analyze_master_prompt(
        self,
        *,
        task_id: str,
        video_url: str,
        prompt: PromptDocument,
        metadata: Mapping[str, Any] | None = None,
        transcript: Mapping[str, Any] | None = None,
        duration_seconds: float | None = None,
        allow_external_call: bool = False,
    ) -> GeminiVideoResult:
        """Execute the full master-prompt path only with an explicit opt-in."""
        if not allow_external_call:
            raise GeminiVideoClientError("external_call_not_explicitly_enabled")
        prepared = self.build_master_prompt_request(
            task_id=task_id, video_url=video_url, prompt=prompt,
            metadata=metadata, transcript=transcript,
        )
        reference = parse_youtube_url(video_url)

        def validate(value: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
            report = validate_gemini_result(
                value,
                expected_task_id=task_id,
                expected_video_id=reference.video_id,
                duration_seconds=duration_seconds,
            )
            return report.status, report.errors

        return self._execute_request(
            task_id=task_id,
            video_url=prepared.canonical_url,
            request=prepared.request,
            validator=validate,
        )

    def analyze_master_markdown(
        self,
        *,
        task_id: str,
        video_url: str,
        prompt: PromptDocument,
        duration_seconds: float | None = None,
        allow_external_call: bool = False,
    ) -> GeminiVideoResult:
        """Execute the independent Markdown output path with one provider call."""
        if not allow_external_call:
            raise GeminiVideoClientError("external_call_not_explicitly_enabled")
        reference = parse_youtube_url(video_url)
        request = self.build_master_markdown_request(
            task_id=task_id,
            video_url=video_url,
            prompt=prompt,
        )

        def parse_markdown(text: str) -> dict[str, Any]:
            report = validate_markdown_document(
                text,
                task_id=task_id,
                video_id=reference.video_id,
                canonical_url=reference.normalized_url,
                prompt_version=prompt.version,
                duration_seconds=duration_seconds,
            )
            return report.as_dict()

        def validate(value: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
            status = value.get("status")
            errors = value.get("validation_errors", ())
            if status not in {"VALID", "PARTIAL", "INVALID"}:
                return "INVALID", ("markdown_adapter_status_invalid",)
            if not isinstance(errors, (list, tuple)):
                return "INVALID", ("markdown_adapter_errors_invalid",)
            return status, tuple(str(error) for error in errors)

        return self._execute_request(
            task_id=task_id,
            video_url=request["input"][-1]["uri"],
            request=request,
            validator=validate,
            parser=parse_markdown,
        )


__all__ = [
    "DEFAULT_PROBE_INSTRUCTION", "GeminiClientConfig", "GeminiVideoClient",
    "GeminiVideoClientError", "GeminiVideoResult", "PROBE_SCHEMA", "validate_visual_probe",
]
