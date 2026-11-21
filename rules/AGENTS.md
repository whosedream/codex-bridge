# Direct Codex Workers

In Antigravity Orchestrator mode, delegate code implementation, test changes, and test execution to Codex by default, even when the user only describes the work. The Antigravity planner scopes the task and defines acceptance. Include the Codex dispatch in the plan when handing off to the Orchestrator executor. The executor calls `codex_run` directly in the same conversation with `model: "gpt-6.2-sol"` and `async_mode: true`, then polls `codex_poll`. Do not substitute native file-edit tools, local test execution, or `invoke_subagent` for this work. Ask Codex to run the tests and report their actual results. Antigravity reviews the changes and test evidence, and sends any needed follow-up to the same Codex session.

Keep read-only exploration and planning in Antigravity unless the user asks to delegate them. Follow an explicit request for a different worker model or execution route.

When the user delegates a task to Codex, the Antigravity primary agent calls the Codex Bridge MCP tools from the current conversation. Call `codex_run` directly for execution. Keep the user's requested model, scope, constraints, and verification in the brief. When no model is requested, omit `model` to use the Codex configuration.

- In a WSL installation with both endpoints, use `codex-bridge_codex-wsl` for Linux absolute paths such as `/home/…`; send Windows drive-letter and UNC paths to `codex-bridge_codex`. Use the same endpoint for readiness, dispatch, follow-up, and polling.
- Use an absolute `cwd` for the workspace the user authorized. Use `workspace-write` for approved edits and `read-only` for inspection or non-editing execution.
- For background work, call `codex_run` with `async_mode: true`. Poll `codex_poll` with the returned `task_id` until it finishes. Collect the outcome in this conversation.
- Call `codex_plan` directly if the user wants a read-only proposal. Call `codex_review` directly for code review. Pass the same workspace and the prior `session_id` to `codex_run` to continue a Codex conversation.
- Keep parallel writes in separate worktrees or run them sequentially. Codex Bridge runs in the supplied directory.
- After a run, inspect the worker's report and verify its result. For edits, check `changed_files`, `pre_existing_dirty_files`, and `diff_stat`. Report failures and give the user the result in their language.
