"""Deterministic validation and adaptation for Markdown audiovisual output.

The validator normalizes only an explicit, reviewable alias table. It never
rewrites the provider document, upgrades declared provenance to verified
evidence, or executes commands found in the document.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import unicodedata
from typing import Any, Mapping
from urllib.parse import urlsplit


VALID = "VALID"
PARTIAL = "PARTIAL"
INVALID = "INVALID"

# Aliases are whole-heading equivalents, not word fragments. This prevents a
# heading such as "Objetivo financiero" from satisfying the technical
# objective section merely because it contains the word "objetivo".
SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "objective": (
        "objective", "objetivo", "objetivo general", "objetivos especificos",
        "objetivo de implementacion", "implementation objective",
    ),
    "architecture": (
        "architecture", "arquitectura", "arquitectura o flujo general",
        "architecture or general flow",
    ),
    "inventory": (
        "inventory", "inventario", "inventario completo de elementos del video",
    ),
    "tools_dependencies": (
        "tools and dependencies", "tools dependencies", "herramientas y dependencias",
        "software servicios y dependencias",
    ),
    "author_actions": (
        "author actions", "acciones del autor", "que hace el autor",
    ),
    "procedure": (
        "procedure", "procedimiento", "procedimiento completo paso a paso",
        "complete step by step procedure",
    ),
    "commands_configuration": (
        "commands configuration", "commands configurations and code",
        "comandos configuraciones", "comandos y configuraciones",
        "comandos configuraciones y codigo",
    ),
    "technical_links": (
        "technical links", "enlaces tecnicos", "enlaces y recursos relevantes",
        "links and relevant resources",
    ),
    "results_validation": (
        "results and validation", "resultados y validacion", "resultado final mostrado",
        "validacion", "validaciones realizadas",
    ),
    "limitations": (
        "limitations", "limitaciones", "limitaciones riesgos y advertencias",
    ),
    "evidence": (
        "evidence", "evidencia", "evidencia visual y transcript",
        "resultado final mostrado", "validaciones realizadas",
    ),
    "definition_of_done": ("definition of done",),
    "codex_instructions": (
        "codex instructions", "instrucciones para codex",
        "si eres una ia lee esta parte para implementacion",
    ),
}

# Kept as a compatibility export for callers that used the previous regex
# table. Recognition itself is now exact-after-normalization.
REQUIRED_SECTION_PATTERNS = {
    name: re.compile("|".join(re.escape(alias) for alias in aliases), re.IGNORECASE)
    for name, aliases in SECTION_ALIASES.items()
}

HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
TIMESTAMP_RE = re.compile(r"(?<!\d)(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?!\d)")
URL_RE = re.compile(r"https?://[^\s<>\]\)\"']+", re.IGNORECASE)
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
CODE_BLOCK_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
COMMAND_LINE_RE = re.compile(
    r"^(?:\$\s*)?(?:sudo\s+\S+|kubectl\s+\S+|git\s+\S+|python(?:3)?\s+\S+|"
    r"npm\s+\S+|curl\s+\S+|systemctl\s+\S+|docker\s+"
    r"(?:build|run|info|ps|images|pull|rmi|compose)\b).+",
    re.IGNORECASE,
)
DECLARED_PROVENANCE_RE = re.compile(r"\[(VIDEO|TRANSCRIPT|INFERENCIA|MODEL_INFERENCE|UNVERIFIED)\]", re.IGNORECASE)


def normalize_heading(value: str) -> str:
    """Normalize a heading for exact alias matching without changing Markdown."""
    text = re.sub(r"^\s*(?:ia[- ]?\d+|\d+(?:\.\d+)*)\s*[-.)]?\s*", "", value.strip(), flags=re.IGNORECASE)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^a-zA-Z0-9]+", " ", text).strip().lower()
    return re.sub(r"\s+", " ", text)


def canonical_section_for_heading(heading: str) -> str | None:
    normalized = normalize_heading(heading)
    for section, aliases in SECTION_ALIASES.items():
        if normalized in aliases:
            return section
    return None


@dataclass(frozen=True)
class MarkdownValidation:
    task_id: str
    video_id: str
    canonical_url: str
    prompt_version: str
    document_markdown: str
    detected_sections: tuple[str, ...]
    missing_sections: tuple[str, ...]
    evidence_references: tuple[dict[str, Any], ...]
    detected_commands: tuple[dict[str, Any], ...]
    validation_errors: tuple[str, ...]
    completeness: dict[str, Any]
    usage_metrics: Mapping[str, Any] | None
    status: str
    recognized_sections: tuple[str, ...] = ()
    present_but_unrecognized: tuple[str, ...] = ()
    unrecognized_headings: tuple[str, ...] = ()
    empty_sections: tuple[str, ...] = ()
    inventory_items: tuple[str, ...] = ()
    provenance_counts: Mapping[str, int] = field(default_factory=dict)
    technical_content: Mapping[str, Any] = field(default_factory=dict)

    @property
    def errors(self) -> tuple[str, ...]:
        """Compatibility alias used by earlier validation callers."""
        return self.validation_errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "video_id": self.video_id,
            "canonical_url": self.canonical_url,
            "prompt_version": self.prompt_version,
            "document_markdown": self.document_markdown,
            "detected_sections": list(self.detected_sections),
            "recognized_sections": list(self.recognized_sections),
            "present_but_unrecognized": list(self.present_but_unrecognized),
            "unrecognized_headings": list(self.unrecognized_headings),
            "missing_sections": list(self.missing_sections),
            "empty_sections": list(self.empty_sections),
            "inventory_items": list(self.inventory_items),
            "provenance_counts": dict(self.provenance_counts),
            "evidence_references": list(self.evidence_references),
            "detected_commands": list(self.detected_commands),
            "technical_content": dict(self.technical_content),
            "validation_errors": list(self.validation_errors),
            "completeness": dict(self.completeness),
            "usage_metrics": dict(self.usage_metrics or {}),
            "status": self.status,
        }


def _timestamp_seconds(match: re.Match[str]) -> float:
    hours, minutes, seconds = match.groups()
    return float((int(hours) if hours else 0) * 3600 + int(minutes) * 60 + int(seconds))


def _valid_url(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def _section_content(markdown: str, headings: list[tuple[str, int, int]]) -> dict[str, str]:
    lines = markdown.splitlines()
    content: dict[str, str] = {}
    for index, (heading, start, level) in enumerate(headings):
        end = len(lines)
        for _, next_start, next_level in headings[index + 1:]:
            if next_level <= level:
                end = next_start
                break
        content[heading] = "\n".join(lines[start + 1:end]).strip()
    return content


def _context_provenance(lines: list[str], index: int) -> str:
    context = "\n".join(lines[max(0, index - 6): index + 1])
    match = DECLARED_PROVENANCE_RE.search(context)
    return match.group(1).upper() if match else "UNVERIFIED"


def _provenance_counts(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for match in DECLARED_PROVENANCE_RE.finditer(text):
        key = match.group(1).upper()
        counts[key] = counts.get(key, 0) + 1
    return counts


def validate_markdown_document(
    markdown: str,
    *,
    task_id: str,
    video_id: str,
    canonical_url: str,
    prompt_version: str,
    provider_status: str | None = None,
    duration_seconds: float | None = None,
    usage_metrics: Mapping[str, Any] | None = None,
) -> MarkdownValidation:
    """Preserve Markdown and classify it without inventing technical facts."""
    text = markdown if isinstance(markdown, str) else ""
    errors: list[str] = []
    if not text.strip():
        return MarkdownValidation(
            task_id, video_id, canonical_url, prompt_version, text, (), (), (), (),
            ("markdown_empty",), {"status": INVALID, "section_coverage": 0.0},
            usage_metrics, INVALID,
        )

    lines = text.splitlines()
    heading_matches = list(HEADING_RE.finditer(text))
    headings = [
        (
            match.group(1).strip(),
            text[:match.start()].count("\n"),
            len(match.group(0)) - len(match.group(0).lstrip("#")),
        )
        for match in heading_matches
    ]
    detected = tuple(item[0] for item in headings)
    contents = _section_content(text, headings)
    empty_sections = tuple(heading for heading, content in contents.items() if not content)
    errors.extend(f"empty_heading:{heading}" for heading in empty_sections)

    recognized: list[str] = []
    unrecognized: list[str] = []
    for heading in detected:
        section = canonical_section_for_heading(heading)
        if section is None:
            unrecognized.append(heading)
        elif section not in recognized:
            recognized.append(section)
    missing = tuple(section for section in SECTION_ALIASES if section not in recognized)
    present_but_unrecognized: list[str] = []
    content_signals = {
        "evidence": r"\*\*\s*(?:evidence|evidencia)\s*:\s*\*\*|\b(?:procedencia|fuente principal|transcript evidence|visual evidence)\b",
    }
    for section, signal in content_signals.items():
        if section in missing and re.search(signal, text, re.IGNORECASE):
            present_but_unrecognized.append(section)
    missing = tuple(section for section in missing if section not in present_but_unrecognized)
    if missing:
        errors.append("sections_missing:" + ",".join(missing))
    if provider_status == "incomplete":
        errors.append("provider_incomplete")

    links = URL_RE.findall(text)
    for link in links:
        if not _valid_url(link.rstrip(".,;")):
            errors.append("invalid_link_detected")
    for match in MARKDOWN_LINK_RE.finditer(text):
        if not _valid_url(match.group(1).strip()):
            errors.append("invalid_link_detected")

    references: list[dict[str, Any]] = []
    for match in TIMESTAMP_RE.finditer(text):
        seconds = _timestamp_seconds(match)
        line_index = text[:match.start()].count("\n")
        item: dict[str, Any] = {
            "text": match.group(0),
            "seconds": seconds,
            "evidence_type": "UNVERIFIED",
            "declared_provenance": _context_provenance(lines, line_index),
        }
        if duration_seconds is not None and seconds > duration_seconds:
            errors.append(f"timestamp_outside_duration:{match.group(0)}")
            item["status"] = "outside_duration"
        else:
            item["status"] = "unverified_without_source_metadata"
        references.append(item)

    commands: list[dict[str, Any]] = []
    command_texts: set[str] = set()
    for block in CODE_BLOCK_RE.finditer(text):
        block_start = text[:block.start(1)].count("\n")
        for offset, line in enumerate(block.group(1).splitlines()):
            candidate = line.strip()
            if COMMAND_LINE_RE.match(candidate):
                commands.append({
                    "text": candidate,
                    "evidence_type": "UNVERIFIED",
                    "declared_provenance": _context_provenance(lines, block_start + offset),
                    "exact": False,
                })
                command_texts.add(candidate)
    for line_index, line in enumerate(lines):
        candidate = line.strip()
        if candidate not in command_texts and COMMAND_LINE_RE.match(candidate):
            commands.append({
                "text": candidate,
                "evidence_type": "UNVERIFIED",
                "declared_provenance": _context_provenance(lines, line_index),
                "exact": False,
            })
            command_texts.add(candidate)
    if commands:
        errors.append("commands_detected_without_deterministic_source")

    if re.search(r"(?:execute|run|ejecuta|ejecutar)\s+(?:this|the|este|el)\s+(?:command|comando)", text, re.IGNORECASE):
        errors.append("execution_instruction_kept_documentary_only")

    inventory_items = tuple(
        heading for heading in detected
        if re.match(r"^(?:elemento|element|item)\s+\d+\b", normalize_heading(heading), re.IGNORECASE)
    )
    technical_content = {
        "links_detected": len(links),
        "commands_detected": len(commands),
        "timestamps_detected": len(references),
        "inventory_items_detected": len(inventory_items),
        "definition_of_done_present": "definition_of_done" in recognized,
        "codex_instructions_present": "codex_instructions" in recognized,
    }
    coverage = round((len(SECTION_ALIASES) - len(missing)) / len(SECTION_ALIASES), 3)
    status = VALID if not errors else PARTIAL
    completeness = {
        "status": status,
        "section_coverage": coverage,
        "inventory_coverage": None,
        "factual_accuracy": "not_determinable_from_markdown_alone",
    }
    return MarkdownValidation(
        task_id, video_id, canonical_url, prompt_version, text, detected, missing,
        tuple(references), tuple(commands), tuple(dict.fromkeys(errors)), completeness,
        usage_metrics, status, tuple(recognized), tuple(present_but_unrecognized), tuple(unrecognized),
        empty_sections, inventory_items, _provenance_counts(text), technical_content,
    )


__all__ = [
    "INVALID", "PARTIAL", "VALID", "MarkdownValidation", "REQUIRED_SECTION_PATTERNS",
    "SECTION_ALIASES", "canonical_section_for_heading", "normalize_heading",
    "validate_markdown_document",
]
