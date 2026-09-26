"""Controlled, non-overwriting storage for generated Markdown artifacts.

The model supplies document content only.  The application owns the analysis
identifier, filename, directory, and final reference.  This module performs no
network, subprocess, Telegram, database, or document-export operations.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import tempfile
from typing import Any

from .pulse_interface import (
    ANALYSIS_ID_RE,
    DocumentReference,
    PulseInterfaceError,
    document_reference_for_existing,
)


class ArtifactStoreError(PulseInterfaceError):
    """Raised when controlled artifact storage cannot safely proceed."""


@dataclass(frozen=True)
class StoredArtifact:
    reference: DocumentReference
    created: bool = True


class ControlledArtifactStore:
    """Write only application-generated names below ``<root>/artifacts``."""

    def __init__(self, project_root: str | Path) -> None:
        raw_root = Path(project_root).expanduser()
        if raw_root.is_symlink():
            raise ArtifactStoreError("controlled_root_symlink_forbidden")
        self.project_root = raw_root.resolve()
        if not self.project_root.is_dir():
            raise ArtifactStoreError("controlled_root_missing")
        self.artifacts_dir = self.project_root / "artifacts"
        if self.artifacts_dir.is_symlink():
            raise ArtifactStoreError("artifact_directory_symlink_forbidden")
        if not self.artifacts_dir.is_dir():
            raise ArtifactStoreError("artifact_directory_missing")

    @staticmethod
    def _validate_analysis_id(analysis_id: str) -> str:
        if not isinstance(analysis_id, str) or not ANALYSIS_ID_RE.fullmatch(analysis_id):
            raise ArtifactStoreError("analysis_id_invalid")
        return analysis_id

    def _target(self, analysis_id: str) -> Path:
        analysis_id = self._validate_analysis_id(analysis_id)
        target = self.artifacts_dir / f"{analysis_id}.md"
        if target.parent != self.artifacts_dir or target.name != f"{analysis_id}.md":
            raise ArtifactStoreError("artifact_path_invalid")
        return target

    def _target_for_extension(self, analysis_id: str, extension: str) -> Path:
        analysis_id = self._validate_analysis_id(analysis_id)
        extension = str(extension).strip().lower().lstrip(".")
        if extension not in {"md", "docx", "pdf"}:
            raise ArtifactStoreError("artifact_extension_not_allowed")
        target = self.artifacts_dir / f"{analysis_id}.{extension}"
        if target.parent != self.artifacts_dir or target.name != f"{analysis_id}.{extension}":
            raise ArtifactStoreError("artifact_path_invalid")
        if target.is_symlink():
            raise ArtifactStoreError("artifact_symlink_forbidden")
        return target

    def write_markdown(self, analysis_id: str, markdown: str) -> StoredArtifact:
        """Atomically create one new Markdown file and never overwrite it."""
        target = self._target(analysis_id)
        if not isinstance(markdown, str) or not markdown.strip():
            raise ArtifactStoreError("artifact_markdown_empty")
        if target.exists() or target.is_symlink():
            raise ArtifactStoreError("artifact_exists")

        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=self.artifacts_dir,
                prefix=f".{analysis_id}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temp_path = Path(stream.name)
                stream.write(markdown.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            # A hard-link create is atomic and fails instead of replacing an
            # artifact if a concurrent writer wins the race.
            os.link(temp_path, target, follow_symlinks=False)
            os.unlink(temp_path)
            temp_path = None
            return StoredArtifact(document_reference_for_existing(self.project_root, analysis_id))
        except FileExistsError as exc:
            raise ArtifactStoreError("artifact_exists") from exc
        except OSError as exc:
            raise ArtifactStoreError("artifact_write_failed") from exc
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass

    def write_bytes(self, analysis_id: str, extension: str, content: bytes) -> StoredArtifact:
        """Atomically create one controlled binary artifact without overwrite."""
        target = self._target_for_extension(analysis_id, extension)
        if not isinstance(content, (bytes, bytearray)) or not content:
            raise ArtifactStoreError("artifact_binary_empty")
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=self.artifacts_dir,
                prefix=f".{analysis_id}.",
                suffix=f".{str(extension).lower().lstrip('.')}.tmp",
                delete=False,
            ) as stream:
                temp_path = Path(stream.name)
                stream.write(bytes(content))
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temp_path, target, follow_symlinks=False)
            os.unlink(temp_path)
            temp_path = None
            data = target.read_bytes()
            relative = (Path("artifacts") / target.name).as_posix()
            reference = DocumentReference(
                analysis_id=analysis_id,
                relative_path=relative,
                sha256=sha256(data).hexdigest(),
                size_bytes=len(data),
            )
            return StoredArtifact(reference)
        except FileExistsError as exc:
            raise ArtifactStoreError("artifact_exists") from exc
        except OSError as exc:
            raise ArtifactStoreError("artifact_write_failed") from exc
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass


__all__ = ["ArtifactStoreError", "ControlledArtifactStore", "StoredArtifact"]
