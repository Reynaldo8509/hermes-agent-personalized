"""Stdout-only bridge entry point for the isolated Pulse VIDEO2IMPLEMENTATION adapter.

The Node bridge invokes this file with ``execFile`` and sends one JSON payload
on stdin.  The script never invokes a shell, never treats model output as a
command, and writes exactly one compact JSON envelope to stdout.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path("/home/example-user/hermes-customizations/video2implementation")
sys.path.insert(0, str(PROJECT_ROOT.parent))

from video2implementation.pulse_adapter import Video2ImplementationPulseAdapter  # noqa: E402


def main() -> int:
    try:
        payload: Any = json.load(sys.stdin)
        if not isinstance(payload, dict) or not isinstance(payload.get("request"), dict):
            raise ValueError("video2implementation_payload_invalid")
        adapter = Video2ImplementationPulseAdapter(
            project_root=PROJECT_ROOT,
            prompt_dir=PROJECT_ROOT / "prompts",
        )
        envelope = adapter.process(
            payload["request"],
            extraction_result=payload.get("extraction_result"),
        )
        if envelope is None:
            raise ValueError("video2implementation_route_not_supported")
        sys.stdout.write(json.dumps(envelope.as_dict(), ensure_ascii=False, separators=(",", ":")))
        sys.stdout.write("\n")
        return 0
    except Exception as exc:
        print(type(exc).__name__ + ": " + str(exc)[:500], file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
