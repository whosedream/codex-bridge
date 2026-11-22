#!/usr/bin/env python3
"""Set the implementation route when Antigravity selects Orchestrator."""
import json
import sys

from codex_runner import DEFAULT_POLL_WAIT_SECONDS, MAX_WAIT_SECONDS

context = json.load(sys.stdin)
# The host can report the underlying model name even in Orchestrator mode.
if context.get("invocationNum") == 0:
    message = (
        "Apply these defaults only when this turn uses Antigravity Orchestrator. "
        "In the plan, route code implementation "
        "and development tests to Codex Bridge. Call codex_run directly once with "
        "model=gpt-6.2-sol and async_mode=true. Require a failing regression before the code "
        "change, a minimal fix, a passing rerun, and affected regression checks. "
        "Wait for a client completion notification for the task if available. Otherwise use "
        f"codex_poll with the same task_id and omit wait_seconds for the {DEFAULT_POLL_WAIT_SECONDS}-second default. "
        f"Use {MAX_WAIT_SECONDS} for longer waits. After a running result, keep waiting on that ID without redispatch "
        "or status-only updates. Report completion, failure, or a decision the user must make. "
        "Call codex_review for the task's diff in a fresh read-only session. The Antigravity "
        "host independently runs the agreed acceptance suite once against the final code, "
        "including authorized socket, GPU, or network checks that need the host. "
        "Run the full suite alone when it covers the focused checks. Only rerun relevant "
        "checks for failure diagnosis or after code changes. Report worker tests, host acceptance, "
        "and review findings separately. "
        "Honor any explicit user override."
    )
    result = {"injectSteps": [{"systemMessage": {"systemMessage": message}}]}
else:
    result = {}
print(json.dumps(result))
