#!/usr/bin/env python3
"""Check the public showcase README assets and tracked runtime artifacts."""
from __future__ import annotations
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
REQUIRED_ASSETS = (
    "docs/assets/hermes-agent-banner.png",
    "docs/assets/ai-architecture-map.png",
)
FORBIDDEN_EXTENSIONS = (".db", ".sqlite", ".sqlite3", ".log")
FORBIDDEN_DIRS = {
    "node_modules", "__pycache__", ".next", ".venv", "venv",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
}
errors: list[str] = []
if not README.is_file() or README.stat().st_size == 0:
    errors.append("README.md is missing or empty")
else:
    text = README.read_text(encoding="utf-8")
    for target in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text):
        target = target.split()[0].strip("<>")
        if target.startswith(("https://", "http://", "data:")):
            continue
        local = target.split("#", 1)[0]
        if local and not (ROOT / local).is_file():
            errors.append(f"README image does not exist: {local}")
for asset in REQUIRED_ASSETS:
    path = ROOT / asset
    if not path.is_file() or path.stat().st_size == 0:
        errors.append(f"required showcase image is missing or empty: {asset}")
result = subprocess.run(
    ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
)
for raw in result.stdout.split(b"\0"):
    if not raw:
        continue
    name = raw.decode("utf-8", errors="replace")
    path = Path(name)
    basename = path.name.lower()
    allowed_example = basename.endswith((".example", ".template", ".sample", ".dist"))
    is_environment_file = basename == ".env" or basename.endswith(".env") or basename.startswith(".env.")
    if is_environment_file and not allowed_example:
        errors.append(f"tracked environment file: {name}")
    if basename.endswith(FORBIDDEN_EXTENSIONS):
        errors.append(f"tracked runtime data/log file: {name}")
    if any(part.lower() in FORBIDDEN_DIRS for part in path.parts):
        errors.append(f"tracked cache/build directory: {name}")
if errors:
    print("Public repository check failed:", file=sys.stderr)
    for error in errors:
        print(f"- {error}", file=sys.stderr)
    raise SystemExit(1)
print("Public repository check passed: README assets resolve; no tracked env, database, log, or cache/build artifacts found.")
