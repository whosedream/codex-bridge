---
name: codex-architect
description: "Orchestrates Mode A two-phase architecture design and implementation with OpenAI Codex CLI via codex-bridge_codex. Use when handling heavy backend features, system architecture, complex multi-file refactors, or tricky debugging that benefit from read-only planning, user sign-off, and resumed execution."
---

# Codex Architect — Mode A Two-Phase Workflow

Guide heavy engineering tasks from human intent to verified implementation using **Phase 1 read-only planning** (`codex_plan`), **user sign-off**, and **Phase 2 resumed execution** (`codex_run`). Always converse, ask questions, and deliver summaries in the **user's own language**.

## MCP Reference (`codex-bridge_codex`)

The Antigravity primary agent calls `codex_status`, `codex_plan`, `codex_run`, `codex_review`, and `codex_poll` directly. When a tool returns `running`, keep its `task_id`. Wait for a completion notification if the client provides one for that task. Otherwise call `codex_poll(task_id="<task_id>")` for the 300-second default, set `wait_seconds=600` for longer tasks, or use a shorter value, including `0` for immediate status. Keep waiting on the same ID after `running`. Report completion, failure, or a decision the user needs to make.

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
2. **If `status == "running"`:** Follow the notification or long-poll procedure above using this task ID.
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
   - `prompt`: include user adjustments. For code changes, ask Codex to run a focused regression red on the original code, make the smallest change, run it green, and run affected regression tests. Ask for actual commands and outcomes.
   - `cwd`: same absolute workspace path
   - `session_id`: `session_id` captured from `codex_plan`
   - `sandbox`: `"workspace-write"` (never `"danger-full-access"` unless `CODEX_BRIDGE_ALLOW_DANGER_SANDBOX=1` is active and explicitly requested)
   - Dispatch background work once with `async_mode=true`. Optional: `model`, `reasoning_effort`, `images`, `wait_seconds` (`0..600`, default `120` for a synchronous call)
2. **If `status == "running"`:** Follow the notification or long-poll procedure above using this task ID.
3. **Resume Failure Fallback (`resume_failed: true`):**
   - If `codex_run` returns `status == "failed"` and `resume_failed: true` (inspect `hint` and `error`), immediately retry with a **fresh** `codex_run` **omitting `session_id`**, embedding the full approved plan summary, user adjustments, constraints, and verification criteria directly in `prompt`.

## Step 7: Inspect, Sanity-Check, and Summarize

1. Inspect the completed `codex_run` payload:
   - `changed_files` and `diff_stat`: bounded descriptions of the changes attributed to the worker
   - `verification` and `unverified`: observed commands and outcomes, and checks still open
   - `details_path`: local full lists, attribution of historical changes, and logs. Read it as needed.
2. The host independently runs the agreed acceptance suite once against the final code. Include authorized socket, GPU, or network checks that need the host. If the full suite covers focused checks, run the full suite alone. Only rerun relevant checks to diagnose a failure or after code changes.
3. For code changes, call `codex_review` for the task diff in a fresh read-only session. Provide the exact task scope, and collect the findings before reporting.
4. Report concisely in the user's language:
   - **What changed** (files and behavioral summary)
   - **Why** (key design decisions)
   - **Verification** (report worker tests, host acceptance, and review findings separately)
   - **Open items / next steps** (only if any remain)
