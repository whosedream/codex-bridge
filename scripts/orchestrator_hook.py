#!/usr/bin/env python3
"""Set the implementation route when Antigravity selects Orchestrator."""
import json
import sys

context = json.load(sys.stdin)
model = context.get("modelName", "").casefold()
if "orchestrator" in model and context.get("invocationNum") == 0:
    message = (
        "This turn is using Antigravity Orchestrator. In the plan, route code implementation "
        "and test execution to Codex Bridge. Preserve that route when handing off to the "
        "executor: call codex_run directly with model=gpt-6.2-sol and async_mode=true, then "
        "poll codex_poll for the result. Keep planning and acceptance in Antigravity. "
        "Honor any explicit user override."
    )
    result = {"injectSteps": [{"systemMessage": {"systemMessage": message}}]}
else:
    result = {}
print(json.dumps(result))
