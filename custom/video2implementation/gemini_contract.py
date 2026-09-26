"""Deterministic contract and semantic validation for Gemini video analysis."""
from __future__ import annotations

from dataclasses import dataclass
import json
from math import isfinite
from typing import Any, Mapping

from .contract import parse_youtube_url

VALID = "VALID"
PARTIAL = "PARTIAL"
INVALID = "INVALID"
EVIDENCE_TYPES = frozenset({"TRANSCRIPT_EVIDENCE", "VISUAL_EVIDENCE", "MODEL_INFERENCE", "UNVERIFIED"})
PROVENANCE = frozenset({"VIDEO", "INFERENCIA", "PROPUESTA", "NO VERIFICADO"})
REQUIRED_FIELDS = (
    "task_id", "video_id", "title", "original_url", "objective_general",
    "objectives_specific", "inventory", "architecture", "author_actions",
    "procedure_steps", "commands", "configurations", "dependencies",
    "technical_links", "demonstrated_results", "validations", "limitations",
    "codex_instructions", "definition_of_done", "evidence", "timestamps",
    "completeness", "errors",
)


@dataclass(frozen=True)
class ValidationReport:
    status: str
    errors: tuple[str, ...]
    value: dict[str, Any] | None = None


def _claim_item_schema() -> dict[str, Any]:
    """Explicit, non-empty shape for list items without forcing one wording."""
    return {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "name": {"type": ["string", "null"]},
            "description": {"type": ["string", "null"]},
            "text": {"type": ["string", "null"]},
            "command": {"type": ["string", "null"]},
            "url": {"type": ["string", "null"]},
            "path": {"type": ["string", "null"]},
            "key": {"type": ["string", "null"]},
            "result": {"type": ["string", "number", "boolean", "null"]},
            "step": {"type": ["integer", "null"]},
            "number": {"type": ["integer", "null"]},
            "identified": {"type": ["boolean", "null"]},
            "exact": {"type": ["boolean", "null"]},
            "provenance": {"type": ["string", "null"]},
            "evidence_type": {"type": ["string", "null"]},
            "timestamp": {"type": ["string", "null"]},
            "confidence": {"type": ["string", "number", "null"]},
            "limitations": {"type": ["array", "null"], "items": {"type": "string"}},
        },
    }


def _evidence_item_schema() -> dict[str, Any]:
    item = _claim_item_schema()
    item["required"] = ["type"]
    item["properties"] = {
        **item["properties"],
        "type": {"type": "string", "enum": sorted(EVIDENCE_TYPES)},
        "reference": {"type": ["string", "null"]},
        "claim": {"type": ["string", "null"]},
    }
    return item


def _timestamp_item_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "start_seconds": {"type": ["number", "null"]},
            "end_seconds": {"type": ["number", "null"]},
            "start": {"type": ["number", "null"]},
            "end": {"type": ["number", "null"]},
            "status": {"type": ["string", "null"]},
            "available": {"type": ["boolean", "null"]},
            "reason": {"type": ["string", "null"]},
            "evidence_type": {"type": ["string", "null"]},
        },
    }


def audiovisual_probe_schema() -> dict[str, Any]:
    """Schema for the bounded audiovisual probe, kept inside the main contract module."""
    item = _claim_item_schema()
    evidence = _evidence_item_schema()
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "video_id", "scene_description", "visual_elements", "observable_actions",
            "timestamps", "transcript_evidence", "limitations", "unverifiable",
        ],
        "properties": {
            "video_id": {"type": "string"},
            "scene_description": {"type": "string"},
            "visual_elements": {"type": "array", "items": item},
            "observable_actions": {"type": "array", "items": item},
            "timestamps": {"type": "array", "items": _timestamp_item_schema()},
            "transcript_evidence": {"type": "array", "items": evidence},
            "limitations": {"type": "array", "items": {"type": "string"}},
            "unverifiable": {"type": "array", "items": {"type": "string"}},
        },
    }


def video_analysis_schema() -> dict[str, Any]:
    """Provider-neutral JSON schema for the complete VIDEO2IMPLEMENTATION result."""
    nullable_string = {"type": ["string", "null"]}
    item = _claim_item_schema()
    evidence = _evidence_item_schema()
    timestamp = _timestamp_item_schema()
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(REQUIRED_FIELDS),
        "properties": {
            "task_id": {"type": "string"}, "video_id": {"type": "string"},
            "title": nullable_string, "original_url": {"type": "string"},
            "objective_general": nullable_string,
            "objectives_specific": {"type": "array", "items": item},
            "inventory": {
                "type": "object", "additionalProperties": True,
                "properties": {
                    "declared_count": {"type": ["integer", "null"]},
                    "items": {"type": "array", "items": item},
                    "coverage": {"type": ["number", "null"]},
                },
            },
            "architecture": {
                "type": "object", "additionalProperties": True,
                "properties": {"components": {"type": "array", "items": item}},
            },
            "author_actions": {"type": "array", "items": item},
            "procedure_steps": {"type": "array", "items": item}, "commands": {"type": "array", "items": item},
            "configurations": {"type": "array", "items": item}, "dependencies": {"type": "array", "items": item},
            "technical_links": {"type": "array", "items": item}, "demonstrated_results": {"type": "array", "items": item},
            "validations": {"type": "array", "items": item}, "limitations": {"type": "array", "items": item},
            "codex_instructions": {"type": "object", "additionalProperties": True},
            "definition_of_done": {"type": "array", "items": {"type": "string"}},
            "evidence": {"type": "array", "items": evidence}, "timestamps": {"type": "array", "items": timestamp},
            "completeness": {
                "type": "object", "additionalProperties": True,
                "required": ["status"], "properties": {"status": {"type": "string", "enum": [VALID, PARTIAL, INVALID]}},
            },
            "errors": {"type": "array", "items": item},
        },
    }


def validate_provider_schema_subset(schema: Mapping[str, Any], *, max_depth: int = 12, max_nodes: int = 512) -> tuple[str, ...]:
    """Check the final provider schema against Gemini's documented JSON-Schema subset."""
    allowed_keys = {
        "type", "properties", "required", "additionalProperties", "enum", "format",
        "items", "prefixItems", "minItems", "maxItems", "title", "description", "anyOf",
    }
    allowed_types = {"string", "number", "integer", "boolean", "object", "array", "null"}
    errors: list[str] = []
    nodes = 0

    def walk(node: Any, path: str, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > max_nodes:
            errors.append("schema_node_limit_exceeded")
            return
        if depth > max_depth:
            errors.append(f"schema_depth_exceeded:{path}")
        if not isinstance(node, Mapping):
            errors.append(f"schema_object_required:{path}")
            return
        for key in node:
            if key == "minProperties":
                errors.append(f"unsupported_schema_keyword:{path}.minProperties")
            elif key not in allowed_keys:
                errors.append(f"unsupported_schema_keyword:{path}.{key}")

        type_value = node.get("type")
        if isinstance(type_value, str):
            if type_value not in allowed_types:
                errors.append(f"schema_type_invalid:{path}.type")
        elif isinstance(type_value, list):
            if not type_value or any(item not in allowed_types for item in type_value):
                errors.append(f"schema_type_invalid:{path}.type")
        elif type_value is not None:
            errors.append(f"schema_type_invalid:{path}.type")

        properties = node.get("properties")
        if properties is not None:
            if not isinstance(properties, Mapping):
                errors.append(f"schema_properties_object_required:{path}.properties")
            else:
                for name, child in properties.items():
                    if not isinstance(name, str) or not name:
                        errors.append(f"schema_property_name_invalid:{path}.properties")
                    walk(child, f"{path}.properties.{name}", depth + 1)

        required = node.get("required")
        if required is not None:
            if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
                errors.append(f"schema_required_array_invalid:{path}.required")
            elif isinstance(properties, Mapping):
                missing = [item for item in required if item not in properties]
                errors.extend(f"schema_required_property_missing:{path}.{item}" for item in missing)

        additional = node.get("additionalProperties")
        if isinstance(additional, Mapping):
            walk(additional, f"{path}.additionalProperties", depth + 1)
        elif additional is not None and not isinstance(additional, bool):
            errors.append(f"schema_additional_properties_invalid:{path}")

        items = node.get("items")
        if items is not None:
            if isinstance(items, list):
                for index, child in enumerate(items):
                    walk(child, f"{path}.items[{index}]", depth + 1)
            else:
                walk(items, f"{path}.items", depth + 1)

        prefix = node.get("prefixItems")
        if prefix is not None:
            if not isinstance(prefix, list):
                errors.append(f"schema_prefix_items_invalid:{path}.prefixItems")
            else:
                for index, child in enumerate(prefix):
                    walk(child, f"{path}.prefixItems[{index}]", depth + 1)

        any_of = node.get("anyOf")
        if any_of is not None:
            if not isinstance(any_of, list):
                errors.append(f"schema_any_of_invalid:{path}.anyOf")
            else:
                for index, child in enumerate(any_of):
                    walk(child, f"{path}.anyOf[{index}]", depth + 1)

        for key in ("minItems", "maxItems"):
            if key in node and (not isinstance(node[key], int) or node[key] < 0):
                errors.append(f"schema_{key}_invalid:{path}.{key}")

    walk(schema, "schema", 0)
    return tuple(dict.fromkeys(errors))


def _as_object(value: Any, name: str, errors: list[str]) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        errors.append(f"{name}_object_required")
        return None
    return dict(value)


def _check_provenance(items: Any, name: str, errors: list[str]) -> None:
    if not isinstance(items, list):
        errors.append(f"{name}_array_required")
        return
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            errors.append(f"{name}[{index}]_object_required")
            continue
        if not item:
            errors.append(f"{name}[{index}]_object_empty")
            continue
        if "provenance" in item and item["provenance"] not in PROVENANCE:
            errors.append(f"{name}[{index}]_provenance_invalid")
        if item.get("exact") is True and item.get("provenance") != "VIDEO":
            errors.append(f"{name}[{index}]_exact_command_requires_video_evidence")


def _check_timestamps(value: Any, duration_seconds: float | None, errors: list[str]) -> None:
    if not isinstance(value, list):
        errors.append("timestamps_array_required")
        return
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            errors.append(f"timestamps[{index}]_object_required")
            continue
        start = item.get("start_seconds", item.get("start"))
        end = item.get("end_seconds", item.get("end", start))
        if start is None and end is None:
            if not any(key in item for key in ("status", "available", "reason", "unverified")):
                errors.append(f"timestamps[{index}]_unavailable_state_required")
            continue
        if not all(isinstance(number, (int, float)) and isfinite(float(number)) for number in (start, end)):
            errors.append(f"timestamps[{index}]_numeric_required")
            continue
        if float(start) < 0 or float(end) < float(start):
            errors.append(f"timestamps[{index}]_range_invalid")
        if duration_seconds is not None and float(end) > duration_seconds:
            errors.append(f"timestamps[{index}]_outside_video_duration")


def validate_gemini_result(
    value: str | Mapping[str, Any],
    *,
    expected_task_id: str | None = None,
    expected_video_id: str | None = None,
    duration_seconds: float | None = None,
) -> ValidationReport:
    """Classify a response as VALID, PARTIAL, or INVALID without repairing it."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return ValidationReport(INVALID, ("json_malformed",))
    else:
        parsed = dict(value) if isinstance(value, Mapping) else None
    if not isinstance(parsed, dict):
        return ValidationReport(INVALID, ("result_object_required",))

    errors: list[str] = []
    missing = [field for field in REQUIRED_FIELDS if field not in parsed]
    errors.extend(f"field_missing:{field}" for field in missing)
    if missing:
        return ValidationReport(INVALID, tuple(errors), parsed)

    if not isinstance(parsed.get("task_id"), str) or not parsed["task_id"].strip():
        errors.append("task_id_invalid")
    if expected_task_id is not None and parsed.get("task_id") != expected_task_id:
        errors.append("task_id_mismatch")
    if not isinstance(parsed.get("video_id"), str):
        errors.append("video_id_invalid")
    if expected_video_id is not None and parsed.get("video_id") != expected_video_id:
        errors.append("video_id_mismatch")
    try:
        reference = parse_youtube_url(parsed.get("original_url"))
        if reference.video_id != parsed.get("video_id"):
            errors.append("original_url_video_id_mismatch")
    except Exception as exc:
        errors.append(f"original_url_invalid:{exc}")

    inventory = _as_object(parsed.get("inventory"), "inventory", errors)
    architecture = _as_object(parsed.get("architecture"), "architecture", errors)
    codex = _as_object(parsed.get("codex_instructions"), "codex_instructions", errors)
    completeness = _as_object(parsed.get("completeness"), "completeness", errors)
    if not isinstance(parsed.get("errors"), list):
        errors.append("errors_array_required")
    if not isinstance(parsed.get("definition_of_done"), list) or not parsed.get("definition_of_done"):
        errors.append("definition_of_done_nonempty_required")
    for field in ("objectives_specific", "procedure_steps", "validations"):
        if not isinstance(parsed.get(field), list) or not parsed.get(field):
            errors.append(f"{field}_nonempty_required")
    if not isinstance(parsed.get("objective_general"), str) or not parsed["objective_general"].strip():
        errors.append("objective_general_unavailable")
    if not isinstance(parsed.get("evidence"), list):
        errors.append("evidence_array_required")
    else:
        for index, item in enumerate(parsed["evidence"]):
            if not isinstance(item, Mapping) or item.get("type") not in EVIDENCE_TYPES:
                errors.append(f"evidence[{index}]_type_invalid")

    for field in ("author_actions", "procedure_steps", "commands", "configurations", "dependencies", "technical_links", "demonstrated_results", "validations", "limitations"):
        _check_provenance(parsed.get(field), field, errors)
    _check_timestamps(parsed.get("timestamps"), duration_seconds, errors)

    missing_elements: list[Any] = []
    if inventory is not None:
        count = inventory.get("declared_count")
        items = inventory.get("items")
        if count is not None and (not isinstance(count, int) or count < 0):
            errors.append("inventory_declared_count_invalid")
        if not isinstance(items, list):
            errors.append("inventory_items_array_required")
        elif isinstance(count, int) and count > len(items):
            missing_elements = list(range(len(items) + 1, count + 1))
        if missing_elements:
            errors.append("inventory_elements_missing")

    if completeness is not None and completeness.get("status") not in {VALID, PARTIAL, INVALID}:
        errors.append("completeness_status_invalid")
    if codex is not None and not codex:
        errors.append("codex_instructions_empty")
    if architecture is not None and not architecture:
        errors.append("architecture_empty")

    if errors:
        structural = {
            error for error in errors
            if error.startswith(("field_missing", "json_", "result_", "original_url_invalid", "video_id_invalid", "video_id_mismatch", "task_id_invalid", "task_id_mismatch"))
        }
        return ValidationReport(INVALID if structural else PARTIAL, tuple(errors), parsed)
    if missing_elements:
        return ValidationReport(PARTIAL, ("inventory_elements_missing",), parsed)
    if completeness and completeness.get("status") == PARTIAL:
        return ValidationReport(PARTIAL, ("provider_reported_partial",), parsed)
    if completeness and completeness.get("status") == INVALID:
        return ValidationReport(INVALID, ("provider_reported_invalid",), parsed)
    return ValidationReport(VALID, (), parsed)
