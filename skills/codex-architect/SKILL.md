---
name: codex-architect
description: "Orchestrates Mode A two-phase architecture design and implementation with OpenAI Codex CLI via codex-bridge_codex. Use when handling heavy backend features, system architecture, complex multi-file refactors, or tricky debugging that benefit from read-only planning, user sign-off, and resumed execution."
---

# Codex Architect — Mode A Two-Phase Workflow

Guide heavy engineering tasks from human intent to verified implementation using **Phase 1 read-only planning** (`codex_plan`), **user sign-off**, and **Phase 2 resumed execution** (`codex_run`). Always converse, ask questions, and deliver summaries in the **user's own language**.

## MCP Reference (`codex-bridge_codex`)

The Antigravity primary agent calls `codex_status`, `codex_plan`, `codex_run`, `codex_review`, and `codex_poll` directly. Use `codex_poll(task_id, wait_seconds=30)` for background work.

## Step 1: Preflight & Workspace Guard

1. Confirm the target `cwd` as an absolute path. If it lies outside the currently active workspace, ask the user before proceeding.
2. Call `codex_status(cwd="<abs_cwd>")` if Codex readiness or Git state is unverified (or after any setup/auth failure).
   - Verify `binary_found` and `auth_ready`. If either is `false`, translate `remediation` into plain guidance in the user's language and stop.
   - Note `git` (`is_git_repo`, `branch`, `dirty`, `dirty_count`) and `danger_sandbox_allowed` so you understand baseline repo state.

## Step 2: Concise Socratic Discovery

Before dispatching Codex, ask focused plain-language questions (in one concise turn) for any unresolved ambiguity:
- Core behavior, boundaries, and explicit non-goals
- API shapes, schemas, backwards-compatibility, or performance constraints
- Edge cases, error-handling expectations, and how success should be tested

Skip redundant questions if the user's specification is already concrete.

## Step 3: Build the Engineering Brief

Synthesize the user's answers and any paths you already inspected into a structured prompt for Codex:

```markdown
## Goal & Non-Goals
- Goal: <clear engineering objective>
- Non-Goals: <explicit boundaries not to touch>

## Workspace & Key Context
- Target cwd: <abs_cwd>
- Key files / symbols: <paths and relevant entry points>

## Constraints & Edge Cases
- <stack conventions, compatibility rules, error handling>

## Acceptance & Verification Criteria
- <specific unit/integration tests or commands to verify behavior>
```

For Phase 1, instruct Codex to stay read-only and output: proposed architecture/design, exact files to create or modify, trade-offs/risks, and concrete verification steps.

## Step 4: Phase 1 — Read-Only Plan (`codex_plan`)

1. Call `codex_plan` with:
   - `prompt` (required): structured engineering brief
   - `cwd` (required): validated absolute path
   - Optional: `model`, `reasoning_effort` (`low` | `medium` | `high` | `xhigh`), `images`, `wait_seconds` (`0..600`, default `120`)
   *(Subprocess safety: `codex_plan` always enforces `-s read-only`.)*
2. **If `status == "running"`:** Poll with `codex_poll` (`task_id`, optional `wait_seconds`, default `30`) until `status` is `"completed"`, `"failed"`, or `"error"`. Give the user a short status update if multiple polls are needed.
3. **If `status == "failed"` or `"error"`:** Inspect `error` / `errors`, run `codex_status` if auth/binary-related, and report the blocker clearly without dumping raw JSON.
4. **Capture `session_id`:** Save `session_id` from the completed `codex_plan` payload for Phase 2 continuity.

## Step 5: Human Plan Review & Sign-Off Gate

Translate `summary` into clear, structured language for the user (never dump raw logs):
- **Approach:** What Codex proposes and why
- **Scope:** Files and modules to be created or modified
- **Trade-offs & Risks:** Assumptions, alternatives, or open questions
- **Verification Plan:** How changes will be tested

**Stop and ask for explicit user approval** (and any adjustments) before calling `codex_run` to edit files.

## Step 6: Phase 2 — Resumed Execution (`codex_run`)

1. Once approved, call `codex_run` with:
   - `prompt`: confirm plan approval, include any user adjustments, and tell Codex to implement and run verification checks
   - `cwd`: same absolute workspace path
   - `session_id`: `session_id` captured from `codex_plan`
   - `sandbox`: `"workspace-write"` (never `"danger-full-access"` unless `CODEX_BRIDGE_ALLOW_DANGER_SANDBOX=1` is active and explicitly requested)
   - Optional: `model`, `reasoning_effort`, `images`, `async_mode` (`false` by default), `wait_seconds` (`0..600`, default `120`)
2. **If `status == "running"`:** Poll `codex_poll` with `task_id` until `status` is `"completed"`, `"failed"`, or `"error"`.
3. **Resume Failure Fallback (`resume_failed: true`):**
   - If `codex_run` returns `status == "failed"` and `resume_failed: true` (inspect `hint` and `error`), immediately retry with a **fresh** `codex_run` **omitting `session_id`**, embedding the full approved plan summary, user adjustments, constraints, and verification criteria directly in `prompt`.

## Step 7: Inspect, Sanity-Check, and Summarize

1. Inspect the completed `codex_run` payload:
   - `changed_files`: exact files created, modified, or deleted by Codex
   - `pre_existing_dirty_files`: dirty files that existed before Codex ran (do not attribute these to Codex)
   - `diff_stat`: concise per-file line delta
2. Use `read_file` to inspect key changed files, and run a quick sanity check or test suite if Codex could not verify everything or if final verification evidence is missing.
3. Optionally run `codex_review(cwd="<abs_cwd>", mode="uncommitted")` on high-risk changes before reporting.
4. Report concisely in the user's language:
   - **What changed** (files and behavioral summary)
   - **Why** (key design decisions)
   - **Verification** (observed test/command results and manual file checks)
   - **Open items / next steps** (only if any remain)
