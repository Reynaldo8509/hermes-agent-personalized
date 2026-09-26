"""Load and integrity-check the versioned VIDEO2IMPLEMENTATION master prompt.

The prompt is trusted configuration supplied by REY, not executable input. This
module never evaluates Markdown, expands shell syntax, or sends the prompt to a
provider. A caller must explicitly request loading the fixed master-prompt file.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re

MASTER_PROMPT_FILENAME = "VIDEO2IMPLEMENTATION_Gemini_Prompt_Maestro_v3.md"
REQUIRED_MARKERS = (
    "# VIDEO2IMPLEMENTATION",
    "## 2. FUENTES Y JERARQUÍA DE EVIDENCIA",
    "## 3. CLASIFICACIÓN ESTRICTA DE PROCEDENCIA",
    "## 4. CONTROL DE COBERTURA",
    "# SI ERES UNA IA, LEE ESTA PARTE PARA IMPLEMENTACIÓN",
    "## IA-12. DEFINITION OF DONE",
    "## 12. CONTROL DE CALIDAD FINAL",
)


class PromptIntegrityError(ValueError):
    """Raised when the configured master prompt is absent or incomplete."""


@dataclass(frozen=True)
class PromptDocument:
    path: str
    version: str
    sha256: str
    text: str
    headings: tuple[str, ...]


def _headings(text: str) -> tuple[str, ...]:
    return tuple(
        line.strip()
        for line in text.splitlines()
        if re.match(r"^#{1,4}\s+\S", line.strip())
    )


def load_prompt(path: str | Path) -> PromptDocument:
    """Read the exact UTF-8 prompt and verify its structural markers."""
    prompt_path = Path(path).expanduser().resolve()
    if not prompt_path.is_file():
        raise PromptIntegrityError("master_prompt_missing")
    try:
        text = prompt_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PromptIntegrityError("master_prompt_unreadable") from exc
    if not text.strip():
        raise PromptIntegrityError("master_prompt_empty")
    missing = [marker for marker in REQUIRED_MARKERS if marker not in text]
    if missing:
        raise PromptIntegrityError("master_prompt_structure_incomplete:" + ",".join(missing))
    match = re.search(r"_v(\d+)\.md$", prompt_path.name, re.IGNORECASE)
    version = f"v{match.group(1)}" if match else "unversioned"
    return PromptDocument(
        path=str(prompt_path),
        version=version,
        sha256=sha256(text.encode("utf-8")).hexdigest(),
        text=text,
        headings=_headings(text),
    )


def load_master_prompt(prompt_dir: str | Path) -> PromptDocument:
    """Load only the controlled master-prompt filename from a trusted directory."""
    root = Path(prompt_dir).expanduser().resolve()
    return load_prompt(root / MASTER_PROMPT_FILENAME)
