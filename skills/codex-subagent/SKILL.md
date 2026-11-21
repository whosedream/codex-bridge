---
name: codex-subagent
description: "Dispatches on-demand engineering, read-only investigation, multi-turn follow-up, and code review tasks directly to OpenAI Codex CLI via codex-bridge_codex. Use whenever the user explicitly asks to invoke Codex, continue a prior Codex session, or run a Codex code review."
---

# Codex Subagent — On-Demand Fast Path

Execute targeted user-delegated tasks with Codex cleanly and without unnecessary multi-phase ceremony, while still translating prompts into crisp engineering briefs and verifying/translating outputs in the **user's own language**.

## MCP Reference (`codex-bridge_codex`)

The Antigravity primary agent calls `codex_status`, `codex_plan`, `codex_run`, `codex_review`, and `codex_poll` directly. Use `codex_poll(task_id, wait_seconds=30)` for background work.

## Step 1: Workspace & Readiness Check

1. Resolve `cwd` to an explicit absolute path. If `cwd` is outside the active workspace, confirm with the user first.
2. Call `codex_status(cwd="<abs_cwd>")` only if setup/auth readiness is unknown or if a Codex call fails with binary/auth errors. Translate `remediation` clearly if `binary_found` or `auth_ready` is `false`.

## Step 2: Route to the Right Codex Tool

Choose the leanest tool that matches the user's request:

### A. Read-Only Question, Exploration, or Analysis -> `codex_plan`
- Use when the user asks Codex to explain, diagnose, audit, or propose options without modifying files.
- `codex_plan` enforces `-s read-only` at the subprocess level and returns `session_id` in case the user later asks to implement.
- Args: `prompt`, `cwd`, optional `model`, `reasoning_effort`, `images`, `wait_seconds` (`0..600`, default `120`).

### B. Bounded Edit, Script, or Direct Task -> `codex_run`
- Translate the user's request into a crisp brief (Goal, `cwd` & target paths, constraints, acceptance check) and call `codex_run`:
  - `prompt` (required), `cwd` (required)
  - `sandbox`: `"workspace-write"` (default) or `"read-only"` (never `"danger-full-access"` unless `CODEX_BRIDGE_ALLOW_DANGER_SANDBOX=1` is set and the user explicitly requested it)
  - Optional: `model`, `reasoning_effort`, `images`, `async_mode` (`false` by default), `wait_seconds` (`0..600`, default `120`)
- **Safety Brake:** If a direct edit request is genuinely destructive, ambiguous in risky ways, or unexpectedly spans many files, switch to `codex_plan` first (or ask 1 crisp question) instead of mutating blindly.

### C. Multi-Turn Follow-Up -> `codex_run` with `session_id`
- When iterating on a previous `codex_plan` or `codex_run` result in the same `cwd`, pass the prior `session_id` to `codex_run`.
- **Fallback on `resume_failed: true`:** Inspect `hint` and `error`; immediately retry as a fresh `codex_run` (omitting `session_id`) carrying the relevant prior context and follow-up instructions in `prompt`.

### D. Code Review -> `codex_review`
- Always runs read-only. Match `mode` cleanly to the target:
  - `mode="uncommitted"` (default): staged, unstaged, and untracked changes.
  - `mode="base"`: pass `base_branch="<branch>"` (do not pass `commit_sha`).
  - `mode="commit"`: pass `commit_sha="<sha>"` (do not pass `base_branch`).
  - `mode="custom"`: pass `prompt="<review instructions>"` (do not pass `base_branch` or `commit_sha`).
- Optional args: `title`, `prompt` (passing `prompt` alongside `uncommitted`/`base`/`commit` is supported—the server routes focused target+prompt reviews automatically), `model`, `wait_seconds` (`0..600`, default `120`).

## Step 3: Handle Long-Running Tasks (`codex_poll`)

If `codex_plan`, `codex_run` (including `async_mode: true`), or `codex_review` returns `status == "running"` with a `task_id`:
1. Call `codex_poll(task_id="<task_id>", wait_seconds=30)`.
2. Repeat while `status == "running"` (giving the user concise progress updates if needed) until `status` is `"completed"`, `"failed"`, or `"error"`.

## Step 4: Verify & Translate Back to the User

Never dump raw JSON or JSONL logs. Present results cleanly in the **user's own language**:
- **For `codex_run`:** Inspect `changed_files`, `pre_existing_dirty_files` (do not attribute untouched dirty files to Codex), and `diff_stat`. Read key modified files or run a quick sanity check if needed, then summarize what changed, verification results, and `session_id` readiness for follow-ups.
- **For `codex_review`:** Group findings by severity (critical/bugs, behavioral/edge-case risks, minor improvements) with exact file references from `summary`, spot-checking flagged files with `read_file` when helpful.
- **For `codex_plan`:** Summarize Codex's findings or proposal clearly and ask whether to proceed with implementation if edits are warranted.
