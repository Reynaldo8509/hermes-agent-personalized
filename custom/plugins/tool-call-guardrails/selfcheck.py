#!/usr/bin/env python3
"""Cheap, offline smoke test for tool-call guardrails."""
import importlib.util
from pathlib import Path

path = Path(__file__).with_name("__init__.py")
spec = importlib.util.spec_from_file_location("guard", path)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)
assert module._pre_tool_call("terminal", {"command": "integration_status twitter"}, "smoke")["action"] == "block"
assert module._pre_tool_call("composio_read", {"toolkit": "twitter"}, "smoke")["action"] == "block"
for _ in range(3):
    assert module._pre_tool_call("composio_discover", {"toolkit": "twitter", "intent": "recent"}, "discover") is None
assert module._pre_tool_call("composio_discover", {"toolkit": "twitter", "intent": "recent"}, "discover")["action"] == "block"
module.mark_failure("twitter")
assert module.cooldown_active("twitter")
print("tool-call-guardrails selfcheck: OK")
