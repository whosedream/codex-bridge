#!/usr/bin/env python3
"""Runner, preflight checks, and background task manager for OpenAI Codex CLI."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VALID_SANDBOXES = ("read-only", "workspace-write", "danger-full-access")
VALID_REVIEW_MODES = ("uncommitted", "base", "commit", "custom")
FALLBACK_CODEX_PATHS = ("/opt/homebrew/bin/codex", "/usr/local/bin/codex")
PREFLIGHT_TIMEOUT_SECONDS = 5
DEFAULT_WAIT_SECONDS = 120
DEFAULT_POLL_WAIT_SECONDS = 300
MAX_WAIT_SECONDS = 600
MAX_CHANGED_FILES_PREVIEW = 20
MAX_FINGERPRINT_BYTES = 5 * 1024 * 1024
TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

VERIFICATION_REPORT_INSTRUCTIONS = """

For the Bridge receipt, finish with a short summary and a fenced code block
labeled bridge-verification containing a JSON object with this shape:
{"verification":[{"command":"exact command run","result":"observed outcome"}],"unverified":["check not run and why"]}
Report actual checks only. Include expected failures from test-first work and
label them as expected. Use empty lists where appropriate. If the caller
requires a different output format, honor that format.
"""


class CodexRunnerError(ValueError):
    """Raised when runner validation or preflight requirements fail."""


def resolve_codex_bin() -> str | None:
    """Resolve the Codex CLI executable path or return None if unavailable."""
    override = os.environ.get("CODEX_BIN", "").strip()
    if override:
        candidate = Path(override).expanduser()
        if os.path.sep in override or candidate.exists():
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
            return None
        return shutil.which(override)

    desktop_bin = os.environ.get("CODEX_BRIDGE_DESKTOP_BIN", "").strip()
    if desktop_bin:
        binaries = (
            path for path in Path(desktop_bin).expanduser().glob("*/codex.exe")
            if path.is_file() and os.access(path, os.X_OK)
        )
        latest = max(binaries, key=lambda path: path.parent.stat().st_mtime_ns, default=None)
        if latest is not None:
            return str(latest)

    which_path = shutil.which("codex")
    if which_path:
        return which_path

    for fallback in FALLBACK_CODEX_PATHS:
        if os.path.isfile(fallback) and os.access(fallback, os.X_OK):
            return fallback

    return None


def validate_cwd(cwd: str) -> Path:
    """Validate that cwd exists and resolves to a directory."""
    if not isinstance(cwd, str) or not cwd.strip():
        raise CodexRunnerError("cwd must be a non-empty directory path.")

    path = Path(cwd).expanduser().resolve()
    if not path.exists():
        raise CodexRunnerError(f"cwd does not exist: {cwd}")
    if not path.is_dir():
        raise CodexRunnerError(f"cwd is not a directory: {cwd}")
    return path


def validate_sandbox(sandbox: str, allow_danger: bool | None = None) -> str:
    """Validate sandbox mode and enforce opt-in for danger-full-access."""
    if sandbox not in VALID_SANDBOXES:
        raise CodexRunnerError(
            f"Invalid sandbox mode {sandbox!r}; expected one of {', '.join(VALID_SANDBOXES)}."
        )

    if sandbox == "danger-full-access":
        opted_in = (
            allow_danger
            if allow_danger is not None
            else os.environ.get("CODEX_BRIDGE_ALLOW_DANGER_SANDBOX") == "1"
        )
        if not opted_in:
            raise CodexRunnerError(
                "Sandbox mode 'danger-full-access' is disabled by default. "
                "Set CODEX_BRIDGE_ALLOW_DANGER_SANDBOX=1 to allow unsandboxed execution."
            )

    return sandbox


def _strip_toml_inline_comment(raw_value: str) -> str:
    """Strip an unquoted TOML inline comment from a scalar value."""
    in_single = False
    in_double = False
    escaped = False
    for idx, ch in enumerate(raw_value):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and in_double:
            escaped = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            return raw_value[:idx].strip()
    return raw_value.strip()


def _detect_custom_provider() -> str | None:
    """Inspect $CODEX_HOME/config.toml (or ~/.codex/config.toml) for model_provider without reading secrets."""
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    base_dir = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
    config_path = base_dir / "config.toml"
    if not config_path.is_file():
        return None

    try:
        content = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    in_top_level = True
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("["):
            in_top_level = False
            continue
        if not in_top_level or "=" not in stripped:
            continue

        key, raw_val = stripped.split("=", 1)
        if key.strip() != "model_provider":
            continue

        val = _strip_toml_inline_comment(raw_val)
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        val = val.strip()
        return val or None

    return None


def check_auth_state(codex_bin: str | None) -> dict[str, Any]:
    """Check Codex CLI authentication state without reading or leaking secret values."""
    oauth_logged_in = False
    login_status_text: str | None = None

    if codex_bin:
        try:
            proc = subprocess.run(
                [codex_bin, "login", "status"],
                capture_output=True,
                text=True,
                timeout=PREFLIGHT_TIMEOUT_SECONDS,
                check=False,
            )
            combined = "\n".join(
                part.strip() for part in (proc.stdout, proc.stderr) if part and part.strip()
            ).strip()
            if combined:
                login_status_text = combined.splitlines()[0].strip()
            oauth_logged_in = proc.returncode == 0 and "not logged in" not in combined.lower()
        except (subprocess.TimeoutExpired, OSError) as exc:
            login_status_text = f"login status check failed: {type(exc).__name__}"

    api_key_env_set = bool(os.environ.get("OPENAI_API_KEY", "").strip())
    model_provider = _detect_custom_provider()
    custom_provider_configured = bool(model_provider)
    auth_ready = bool(oauth_logged_in or api_key_env_set or custom_provider_configured)

    remediation = (
        None
        if auth_ready
        else (
            "Codex is not authenticated. Run `codex login`, set OPENAI_API_KEY, "
            "or configure `model_provider` in ~/.codex/config.toml."
        )
    )

    return {
        "oauth_logged_in": oauth_logged_in,
        "login_status_text": login_status_text,
        "api_key_env_set": api_key_env_set,
        "custom_provider_configured": custom_provider_configured,
        "model_provider": model_provider,
        "auth_ready": auth_ready,
        "remediation": remediation,
    }


def inspect_git_repo(cwd: Path) -> dict[str, Any]:
    """Inspect whether cwd is inside a Git work tree, its branch, and dirty count."""
    try:
        probe = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=PREFLIGHT_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return {"is_git_repo": False, "branch": None, "dirty": False, "dirty_count": 0}

    if probe.returncode != 0 or probe.stdout.strip().lower() != "true":
        return {"is_git_repo": False, "branch": None, "dirty": False, "dirty_count": 0}

    branch: str | None = None
    try:
        branch_proc = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=PREFLIGHT_TIMEOUT_SECONDS,
            check=False,
        )
        if branch_proc.returncode == 0:
            branch = branch_proc.stdout.strip() or None
    except (subprocess.TimeoutExpired, OSError):
        branch = None

    dirty_count = 0
    try:
        status_proc = subprocess.run(
            ["git", "-C", str(cwd), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=PREFLIGHT_TIMEOUT_SECONDS,
            check=False,
        )
        if status_proc.returncode == 0:
            dirty_count = sum(1 for line in status_proc.stdout.splitlines() if line.strip())
    except (subprocess.TimeoutExpired, OSError):
        dirty_count = 0

    return {
        "is_git_repo": True,
        "branch": branch,
        "dirty": dirty_count > 0,
        "dirty_count": dirty_count,
    }


def check_status(cwd: str | None = None) -> dict[str, Any]:
    """Run preflight checks for Codex binary, version, auth readiness, and optional Git repo."""
    validated_cwd = validate_cwd(cwd) if cwd is not None else None
    codex_bin = resolve_codex_bin()
    binary_found = codex_bin is not None

    version: str | None = None
    if codex_bin:
        try:
            ver_proc = subprocess.run(
                [codex_bin, "--version"],
                capture_output=True,
                text=True,
                timeout=PREFLIGHT_TIMEOUT_SECONDS,
                check=False,
            )
            if ver_proc.returncode == 0:
                first_line = ver_proc.stdout.strip().splitlines()
                version = first_line[0].strip() if first_line else None
        except (subprocess.TimeoutExpired, OSError):
            version = None

    auth = check_auth_state(codex_bin)
    git_info = inspect_git_repo(validated_cwd) if validated_cwd is not None else None

    return {
        "binary_found": binary_found,
        "codex_bin": codex_bin,
        "version": version,
        "oauth_logged_in": auth["oauth_logged_in"],
        "login_status_text": auth["login_status_text"],
        "api_key_env_set": auth["api_key_env_set"],
        "custom_provider_configured": auth["custom_provider_configured"],
        "model_provider": auth["model_provider"],
        "auth_ready": auth["auth_ready"],
        "remediation": auth["remediation"],
        "auth": auth,
        "cwd": str(validated_cwd) if validated_cwd is not None else None,
        "git": git_info,
        "danger_sandbox_allowed": os.environ.get("CODEX_BRIDGE_ALLOW_DANGER_SANDBOX") == "1",
    }


def _extract_agent_item_text(item: dict[str, Any]) -> str:
    text = item.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()

    content = item.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                block_text = block.get("text")
                if isinstance(block_text, str) and block_text.strip():
                    parts.append(block_text.strip())
        if parts:
            return "\n".join(parts)
    return ""


def parse_jsonl_output(stdout_text: str) -> dict[str, Any]:
    """Parse newline-delimited JSON output from Codex CLI, ignoring malformed lines."""
    session_id: str | None = None
    usage: dict[str, Any] | None = None
    agent_messages: list[str] = []
    errors: list[str] = []
    events_count = 0

    for raw_line in stdout_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if not isinstance(event, dict):
            continue

        events_count += 1
        event_type = event.get("type")

        if event_type == "thread.started" and isinstance(event.get("thread_id"), str):
            session_id = event["thread_id"]
        elif session_id is None and isinstance(event.get("thread_id"), str):
            session_id = event["thread_id"]

        if event_type == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = event["usage"]

        if event_type == "item.completed" and isinstance(event.get("item"), dict):
            item = event["item"]
            if item.get("type") == "agent_message":
                extracted = _extract_agent_item_text(item)
                if extracted:
                    agent_messages.append(extracted)

        if event_type in ("error", "turn.failed"):
            err_msg = event.get("message")
            if not isinstance(err_msg, str) and isinstance(event.get("error"), dict):
                err_msg = event["error"].get("message")
            if isinstance(err_msg, str) and err_msg.strip():
                errors.append(err_msg.strip())

    return {
        "session_id": session_id,
        "usage": usage,
        "agent_text": "\n\n".join(agent_messages).strip(),
        "errors": errors,
        "events_count": events_count,
    }


def _require_non_empty_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CodexRunnerError(f"{field_name} must be a non-empty string.")
    return value.strip()


def _normalize_optional_str(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CodexRunnerError(f"{field_name} must be a string.")
    stripped = value.strip()
    return stripped or None


def _validate_images(images: list[str] | None) -> list[str]:
    if images is None:
        return []
    if not isinstance(images, list) or not all(isinstance(img, str) and img.strip() for img in images):
        raise CodexRunnerError("images must be a list of non-empty file path strings.")
    return [img.strip() for img in images]


def _clamp_wait_seconds(wait_seconds: int | None, *, default: int = DEFAULT_WAIT_SECONDS) -> int:
    if wait_seconds is None:
        return default
    if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, int):
        raise CodexRunnerError(f"wait_seconds must be an integer, got {wait_seconds!r}.")
    return max(0, min(wait_seconds, MAX_WAIT_SECONDS))


def _split_verification_report(
    summary: str,
) -> tuple[str, list[dict[str, str]], list[str]]:
    match = re.search(r"(?:^|\n)```bridge-verification\s*\n(.*?)\n```\s*\Z", summary, re.DOTALL)
    if match is not None:
        try:
            report = json.loads(match[1])
        except json.JSONDecodeError:
            report = None
        if isinstance(report, dict):
            checks = report.get("verification")
            unverified = report.get("unverified")
            if (
                isinstance(checks, list)
                and all(
                    isinstance(check, dict)
                    and all(isinstance(check.get(key), str) and check[key].strip()
                            for key in ("command", "result"))
                    for check in checks
                )
                and isinstance(unverified, list)
                and all(isinstance(item, str) and item.strip() for item in unverified)
            ):
                verification = [
                    {"command": check["command"], "result": check["result"]} for check in checks
                ]
                return summary[:match.start()].rstrip(), verification, unverified
    return summary, [], ["Worker did not supply structured verification. See summary and details."]


def _preview_text(value: str | None, limit: int) -> str | None:
    if value is not None and len(value) > limit:
        return value[:limit].rstrip() + "\n[See details_path for the full text.]"
    return value


def _task_receipt(record: dict[str, Any], task_path: Path) -> dict[str, Any]:
    """Return a bounded terminal receipt, including for records from older versions."""
    receipt = {
        key: record[key] for key in (
            "status", "task_id", "mode", "session_id", "sandbox", "cwd", "usage",
            "elapsed_ms", "returncode", "resume_failed", "hint", "review_mode", "routed_via_exec",
        )
        if key in record
    }
    receipt["details_path"] = str(task_path.resolve())
    receipt["summary"] = _preview_text(record.get("summary"), 3000)
    receipt["error"] = _preview_text(record.get("error"), 800)

    if record.get("mode") in ("exec", "resume"):
        changed_files = record.get("changed_files", [])
        reedited = record.get("reedited_dirty_files", [])
        preview = [path for path in changed_files[:MAX_CHANGED_FILES_PREVIEW] if len(path) <= 300]
        receipt["changed_files"] = preview
        receipt["changed_files_count"] = len(changed_files)
        receipt["changed_files_truncated"] = len(preview) < len(changed_files)
        receipt["pre_existing_dirty_files_count"] = len(record.get("pre_existing_dirty_files", [])) + len(reedited)
        receipt["reedited_dirty_files_count"] = len(reedited)
        receipt["diff_stat"] = _preview_text(record.get("diff_stat"), 800)
        checks = record.get("verification", [])
        unverified = record.get("unverified", ["Verification was not recorded in this receipt."])
        receipt["verification"] = [
            {"command": _preview_text(check["command"], 250), "result": _preview_text(check["result"], 200)}
            for check in checks[:8]
        ]
        receipt["unverified"] = [_preview_text(item, 180) for item in unverified[:6]]
        if len(checks) > 8 or len(unverified) > 6:
            receipt["verification_truncated"] = True
    return receipt


def _require_codex_bin() -> str:
    codex_bin = resolve_codex_bin()
    if not codex_bin:
        raise CodexRunnerError(
            "Codex CLI binary not found. Install Codex CLI or set CODEX_BIN."
        )
    return codex_bin


def _git_output(cwd: Path, args: list[str]) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PREFLIGHT_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.rstrip()


def _read_git_head(cwd: Path) -> str | None:
    head = _git_output(cwd, ["rev-parse", "--verify", "HEAD"]).strip()
    return head or None


def _read_git_toplevel(cwd: Path) -> Path:
    top = _git_output(cwd, ["rev-parse", "--show-toplevel"]).strip()
    if top:
        try:
            return Path(top).resolve()
        except OSError:
            return Path(top)
    return cwd


def _capture_git_porcelain(cwd: Path) -> dict[str, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), "status", "--porcelain=v1", "-z", "-uall"],
            capture_output=True,
            timeout=PREFLIGHT_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return {}
    if proc.returncode != 0:
        return {}

    tokens = proc.stdout.decode("utf-8", errors="replace").split("\0")
    entries: dict[str, str] = {}
    idx = 0
    while idx < len(tokens):
        token = tokens[idx]
        if not token or len(token) < 4:
            idx += 1
            continue
        xy = token[:2]
        path = token[3:]
        if (xy[0] in ("R", "C") or xy[1] in ("R", "C")) and idx + 1 < len(tokens):
            orig_path = tokens[idx + 1]
            entries[path] = f"{xy} {orig_path} -> {path}"
            idx += 2
        else:
            entries[path] = f"{xy} {path}"
            idx += 1
    return entries


def _file_fingerprint(path: Path) -> tuple[Any, ...]:
    if path.is_symlink():
        try:
            return ("symlink", os.readlink(path))
        except OSError:
            return ("symlink", None)
    if not path.exists():
        return ("missing",)
    if path.is_dir():
        return ("dir",)
    try:
        stat_res = path.stat()
    except OSError:
        return ("unreadable",)
    if stat_res.st_size <= MAX_FINGERPRINT_BYTES:
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            return ("file", stat_res.st_size, digest)
        except OSError:
            pass
    return ("file", stat_res.st_size, stat_res.st_mtime_ns)


def _capture_git_baseline(cwd: Path) -> dict[str, Any]:
    repo_root = _read_git_toplevel(cwd)
    status_map = _capture_git_porcelain(repo_root)
    fingerprints = {rel: _file_fingerprint(repo_root / rel) for rel in status_map}
    return {
        "repo_root": str(repo_root),
        "head": _read_git_head(repo_root),
        "status": status_map,
        "fingerprints": fingerprints,
    }


def _summarize_untracked_file(repo_root: Path, rel_path: str) -> str:
    target = repo_root / rel_path
    if target.is_symlink():
        return f" {rel_path} | symlink (untracked)"
    if not target.is_file():
        return f" {rel_path} | (untracked)"
    try:
        raw = target.read_bytes()
    except OSError:
        return f" {rel_path} | (untracked)"
    if b"\x00" in raw:
        return f" {rel_path} | Bin {len(raw)} bytes (untracked)"
    return f" {rel_path} | {len(raw.splitlines())} + (untracked)"


def _compute_git_delta(
    cwd: Path,
    baseline: dict[str, Any] | None,
) -> tuple[list[str], list[str], str | None]:
    if baseline is None:
        return [], [], None

    repo_root_raw = baseline.get("repo_root")
    repo_root = (
        Path(repo_root_raw)
        if isinstance(repo_root_raw, str) and repo_root_raw
        else _read_git_toplevel(cwd)
    )
    pre_head: str | None = baseline["head"]
    pre_status: dict[str, str] = baseline["status"]
    pre_fingerprints: dict[str, tuple[Any, ...]] = baseline["fingerprints"]

    post_head = _read_git_head(repo_root)
    post_status = _capture_git_porcelain(repo_root)

    changed_set: set[str] = set()
    for path, line in post_status.items():
        if path not in pre_status or line != pre_status[path]:
            changed_set.add(path)
        elif _file_fingerprint(repo_root / path) != pre_fingerprints.get(path):
            changed_set.add(path)

    for path in pre_status:
        if path not in post_status:
            changed_set.add(path)

    head_moved = bool(post_head and pre_head != post_head)
    if pre_head and post_head and pre_head != post_head:
        committed = _git_output(repo_root, ["diff", "--name-only", pre_head, post_head])
        changed_set.update(line.strip() for line in committed.splitlines() if line.strip())
    elif pre_head is None and post_head is not None:
        initial = _git_output(repo_root, ["diff-tree", "--no-commit-id", "--name-only", "-r", post_head])
        changed_set.update(line.strip() for line in initial.splitlines() if line.strip())

    pre_existing_dirty = sorted(
        path for path in pre_status if path in post_status and path not in changed_set
    )
    changed_files = sorted(changed_set)
    if not changed_files:
        return changed_files, pre_existing_dirty, None

    stat_blocks: list[str] = []
    if head_moved and pre_head and post_head:
        committed_stat = _git_output(repo_root, ["diff", "--stat", pre_head, post_head])
        if committed_stat:
            stat_blocks.append(committed_stat)
        dirty_changed = [p for p in changed_files if p in post_status and not post_status[p].startswith("?? ")]
        if dirty_changed:
            dirty_stat = _git_output(repo_root, ["diff", "--stat", "HEAD", "--", *dirty_changed])
            if dirty_stat:
                stat_blocks.append(dirty_stat)
    elif post_head:
        tracked_stat = _git_output(repo_root, ["diff", "--stat", "HEAD", "--", *changed_files])
        if tracked_stat:
            stat_blocks.append(tracked_stat)
    else:
        wt_stat = _git_output(repo_root, ["diff", "--stat", "--", *changed_files])
        idx_stat = _git_output(repo_root, ["diff", "--cached", "--stat", "--", *changed_files])
        for block in (wt_stat, idx_stat):
            if block:
                stat_blocks.append(block)

    untracked_lines = [
        _summarize_untracked_file(repo_root, path)
        for path in changed_files
        if post_status.get(path, "").startswith("?? ")
    ]
    deleted_untracked_lines = [
        f" {path} | (deleted untracked)"
        for path in changed_files
        if path not in post_status and pre_status.get(path, "").startswith("?? ")
    ]
    extra_lines = untracked_lines + deleted_untracked_lines
    if extra_lines:
        stat_blocks.append("\n".join(extra_lines))

    diff_stat = "\n".join(block for block in stat_blocks if block).strip() or None
    return changed_files, pre_existing_dirty, diff_stat


def _append_common_exec_tail(
    cmd: list[str],
    *,
    is_git_repo: bool,
    model: str | None,
    reasoning_effort: str | None,
    images: list[str],
    prompt: str,
) -> list[str]:
    if not is_git_repo:
        cmd.append("--skip-git-repo-check")
    if model:
        cmd.extend(["-m", model])
    if reasoning_effort:
        cmd.extend(["-c", f"model_reasoning_effort={json.dumps(reasoning_effort)}"])
    for img in images:
        cmd.extend(["-i", img])
    if images or prompt.startswith("-"):
        cmd.append("--")
    cmd.append(prompt)
    return cmd


def _build_review_fallback_prompt(
    *,
    review_mode: str,
    focus_prompt: str,
    base_branch: str | None,
    commit_sha: str | None,
    title: str | None,
) -> str:
    if review_mode == "uncommitted":
        scope = (
            "the uncommitted working tree changes "
            "(staged, unstaged, and untracked files; inspect via git status and git diff)"
        )
    elif review_mode == "base":
        scope = f"the changes against base branch '{base_branch}' (inspect via git diff {base_branch}...HEAD)"
    else:
        scope = f"commit '{commit_sha}' (inspect via git show {commit_sha})"
    lines = [f"Perform a read-only code review of {scope}."]
    if title:
        lines.append(f"Review title context: {title}")
    lines.append(f"Specific review focus: {focus_prompt}")
    return "\n".join(lines)


def _is_pid_alive(pid: Any) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@dataclass
class _TaskState:
    task_id: str
    mode: str
    cwd: str
    sandbox: str
    extra: dict[str, Any]
    start_monotonic: float
    started_at: float
    tmp_dir: tempfile.TemporaryDirectory
    last_msg_path: Path
    baseline: dict[str, Any] | None
    is_resume: bool = False
    session_id: str | None = None
    progress_events: int = 0
    proc: subprocess.Popen | None = None
    stdout_lines: list[str] = field(default_factory=list)
    stderr_lines: list[str] = field(default_factory=list)
    waiter_thread: threading.Thread | None = None
    result: dict[str, Any] | None = None
    done_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)


class CodexTaskManager:
    """Executes Codex CLI commands with foreground wait, async polling, and task persistence."""

    def __init__(self, data_dir: str | Path | None = None) -> None:
        base_dir = (
            Path(data_dir).expanduser()
            if data_dir is not None
            else Path.home() / ".cache" / "codex-bridge"
        )
        self.data_dir = base_dir
        self.tasks_dir = self.data_dir / "tasks"
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self._tasks: dict[str, _TaskState] = {}
        self._registry_lock = threading.Lock()

    def run_plan(
        self,
        prompt: str,
        cwd: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
        images: list[str] | None = None,
        wait_seconds: int = DEFAULT_WAIT_SECONDS,
    ) -> dict[str, Any]:
        """Run Codex in strict read-only planning mode and capture session_id for resume."""
        clean_prompt = _require_non_empty_str(prompt, "prompt")
        cwd_path = validate_cwd(cwd)
        codex_bin = _require_codex_bin()
        clean_model = _normalize_optional_str(model, "model")
        clean_effort = _normalize_optional_str(reasoning_effort, "reasoning_effort")
        clean_images = _validate_images(images)
        clamped_wait = _clamp_wait_seconds(wait_seconds, default=DEFAULT_WAIT_SECONDS)
        git_info = inspect_git_repo(cwd_path)

        def build_cmd(last_msg_path: Path) -> list[str]:
            cmd = [
                codex_bin,
                "exec",
                "--json",
                "-s",
                "read-only",
                "-C",
                str(cwd_path),
                "-o",
                str(last_msg_path),
            ]
            return _append_common_exec_tail(
                cmd,
                is_git_repo=git_info["is_git_repo"],
                model=clean_model,
                reasoning_effort=clean_effort,
                images=clean_images,
                prompt=clean_prompt,
            )

        return self._launch_and_wait(
            mode="plan",
            cwd_path=cwd_path,
            sandbox="read-only",
            build_cmd=build_cmd,
            wait_seconds=clamped_wait,
            async_mode=False,
        )

    def run_exec(
        self,
        prompt: str,
        cwd: str,
        session_id: str | None = None,
        sandbox: str = "workspace-write",
        model: str | None = None,
        reasoning_effort: str | None = None,
        images: list[str] | None = None,
        async_mode: bool = False,
        wait_seconds: int = DEFAULT_WAIT_SECONDS,
    ) -> dict[str, Any]:
        """Run a fresh or resumed Codex execution task with sandbox and Git delta tracking."""
        clean_prompt = _require_non_empty_str(prompt, "prompt")
        cwd_path = validate_cwd(cwd)
        validated_sandbox = validate_sandbox(sandbox)
        codex_bin = _require_codex_bin()
        clean_model = _normalize_optional_str(model, "model")
        clean_effort = _normalize_optional_str(reasoning_effort, "reasoning_effort")
        clean_images = _validate_images(images)
        clamped_wait = _clamp_wait_seconds(wait_seconds, default=DEFAULT_WAIT_SECONDS)

        clean_session_id: str | None = None
        if session_id is not None:
            clean_session_id = _require_non_empty_str(session_id, "session_id")
            if clean_session_id.startswith("-"):
                raise CodexRunnerError("session_id must not start with '-'.")

        git_info = inspect_git_repo(cwd_path)
        baseline = (
            _capture_git_baseline(cwd_path)
            if validated_sandbox != "read-only" and git_info["is_git_repo"]
            else None
        )
        is_resume = clean_session_id is not None

        def build_cmd(last_msg_path: Path) -> list[str]:
            if is_resume:
                assert clean_session_id is not None
                cmd = [
                    codex_bin,
                    "exec",
                    "resume",
                    clean_session_id,
                    "--json",
                    "-o",
                    str(last_msg_path),
                    "-c",
                    f"sandbox_mode={json.dumps(validated_sandbox)}",
                ]
            else:
                cmd = [
                    codex_bin,
                    "exec",
                    "--json",
                    "-s",
                    validated_sandbox,
                    "-C",
                    str(cwd_path),
                    "-o",
                    str(last_msg_path),
                ]
            return _append_common_exec_tail(
                cmd,
                is_git_repo=git_info["is_git_repo"],
                model=clean_model,
                reasoning_effort=clean_effort,
                images=clean_images,
                prompt=clean_prompt + VERIFICATION_REPORT_INSTRUCTIONS,
            )

        return self._launch_and_wait(
            mode="resume" if is_resume else "exec",
            cwd_path=cwd_path,
            sandbox=validated_sandbox,
            build_cmd=build_cmd,
            wait_seconds=clamped_wait,
            async_mode=bool(async_mode),
            baseline=baseline,
            is_resume=is_resume,
            initial_session_id=clean_session_id,
            extra={"requested_session_id": clean_session_id},
        )

    def run_review(
        self,
        cwd: str,
        mode: str = "uncommitted",
        base_branch: str | None = None,
        commit_sha: str | None = None,
        title: str | None = None,
        prompt: str | None = None,
        model: str | None = None,
        wait_seconds: int = DEFAULT_WAIT_SECONDS,
    ) -> dict[str, Any]:
        """Run a read-only Codex code review, routing target+prompt via codex exec."""
        if mode not in VALID_REVIEW_MODES:
            raise CodexRunnerError(
                f"Invalid review mode {mode!r}; expected one of {', '.join(VALID_REVIEW_MODES)}."
            )
        cwd_path = validate_cwd(cwd)
        codex_bin = _require_codex_bin()
        clean_base = _normalize_optional_str(base_branch, "base_branch")
        clean_commit = _normalize_optional_str(commit_sha, "commit_sha")
        clean_title = _normalize_optional_str(title, "title")
        clean_prompt = _normalize_optional_str(prompt, "prompt")
        clean_model = _normalize_optional_str(model, "model")
        clamped_wait = _clamp_wait_seconds(wait_seconds, default=DEFAULT_WAIT_SECONDS)

        if clean_base and clean_commit:
            raise CodexRunnerError("Specify at most one of base_branch or commit_sha.")

        if mode == "uncommitted":
            effective_mode = "base" if clean_base else ("commit" if clean_commit else "uncommitted")
        elif mode == "base":
            if clean_commit:
                raise CodexRunnerError("commit_sha cannot be used when mode='base'.")
            if not clean_base:
                raise CodexRunnerError("base_branch is required when mode='base'.")
            effective_mode = "base"
        elif mode == "commit":
            if clean_base:
                raise CodexRunnerError("base_branch cannot be used when mode='commit'.")
            if not clean_commit:
                raise CodexRunnerError("commit_sha is required when mode='commit'.")
            effective_mode = "commit"
        else:
            if clean_base or clean_commit:
                raise CodexRunnerError("base_branch and commit_sha cannot be used when mode='custom'.")
            if not clean_prompt:
                raise CodexRunnerError("prompt is required when mode='custom'.")
            effective_mode = "custom"

        git_info = inspect_git_repo(cwd_path)
        routed_via_exec = effective_mode in ("uncommitted", "base", "commit") and clean_prompt is not None

        def build_cmd(last_msg_path: Path) -> list[str]:
            if routed_via_exec:
                assert clean_prompt is not None
                review_prompt = _build_review_fallback_prompt(
                    review_mode=effective_mode,
                    focus_prompt=clean_prompt,
                    base_branch=clean_base,
                    commit_sha=clean_commit,
                    title=clean_title,
                )
                cmd = [
                    codex_bin,
                    "exec",
                    "--json",
                    "-s",
                    "read-only",
                    "-C",
                    str(cwd_path),
                    "-o",
                    str(last_msg_path),
                ]
                if not git_info["is_git_repo"]:
                    cmd.append("--skip-git-repo-check")
                if clean_model:
                    cmd.extend(["-m", clean_model])
                if review_prompt.startswith("-"):
                    cmd.append("--")
                cmd.append(review_prompt)
                return cmd

            cmd = [
                codex_bin,
                "exec",
                "review",
                "--json",
                "-o",
                str(last_msg_path),
                "-c",
                f"sandbox_mode={json.dumps('read-only')}",
            ]
            if not git_info["is_git_repo"]:
                cmd.append("--skip-git-repo-check")
            if clean_model:
                cmd.extend(["-m", clean_model])
            if clean_title:
                cmd.extend(["--title", clean_title])

            if effective_mode == "uncommitted":
                cmd.append("--uncommitted")
            elif effective_mode == "base":
                assert clean_base is not None
                cmd.extend(["--base", clean_base])
            elif effective_mode == "commit":
                assert clean_commit is not None
                cmd.extend(["--commit", clean_commit])
            else:
                assert clean_prompt is not None
                if clean_prompt.startswith("-"):
                    cmd.append("--")
                cmd.append(clean_prompt)
            return cmd

        return self._launch_and_wait(
            mode="review",
            cwd_path=cwd_path,
            sandbox="read-only",
            build_cmd=build_cmd,
            wait_seconds=clamped_wait,
            async_mode=False,
            extra={
                "review_mode": effective_mode,
                "routed_via_exec": routed_via_exec,
            },
        )

    def poll_task(self, task_id: str, wait_seconds: int = DEFAULT_POLL_WAIT_SECONDS) -> dict[str, Any]:
        """Poll a background task by ID, waiting up to wait_seconds for completion."""
        if not isinstance(task_id, str) or not TASK_ID_PATTERN.fullmatch(task_id):
            raise CodexRunnerError(f"Invalid task_id: {task_id!r}")
        clamped_wait = _clamp_wait_seconds(wait_seconds, default=DEFAULT_POLL_WAIT_SECONDS)

        with self._registry_lock:
            task = self._tasks.get(task_id)

        if task is not None:
            if clamped_wait > 0 and not task.done_event.is_set():
                task.done_event.wait(timeout=clamped_wait)
            return self._running_snapshot(task)

        task_path = self.tasks_dir / f"{task_id}.json"
        if not task_path.is_file():
            raise CodexRunnerError(f"Unknown task_id: {task_id}")

        deadline = time.monotonic() + clamped_wait
        while True:
            record = self._read_persisted_task(task_path)
            if record.get("status") != "running":
                return _task_receipt(record, task_path)
            pid = record.get("pid")
            owner_pid = record.get("owner_pid")
            pid_dead = pid is not None and not _is_pid_alive(pid)
            owner_dead = owner_pid is None or not _is_pid_alive(owner_pid)
            if pid_dead and owner_dead:
                latest = self._read_persisted_task(task_path)
                if latest.get("status") != "running":
                    return _task_receipt(latest, task_path)
                failed_record = dict(latest)
                failed_record["status"] = "failed"
                failed_record["error"] = "Background task process is no longer running."
                self._write_task_file(task_path, failed_record)
                return _task_receipt(failed_record, task_path)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return record
            sleep_step = 0.02 if pid_dead else 0.1
            time.sleep(min(sleep_step, remaining))

    def shutdown(self, terminate_running: bool = True, timeout: float = 2.0) -> None:
        """Clean up active tasks and threads (terminating running subprocess groups if requested)."""
        with self._registry_lock:
            tasks = list(self._tasks.values())

        for task in tasks:
            if task.done_event.is_set():
                if task.waiter_thread is not None and task.waiter_thread.is_alive():
                    task.waiter_thread.join(timeout=1.0)
                continue
            proc = task.proc
            if terminate_running and proc is not None and proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except OSError:
                    pass
                if not task.done_event.wait(timeout=timeout) and proc.poll() is None:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except OSError:
                        pass
            task.done_event.wait(timeout=1.0)
            if task.waiter_thread is not None and task.waiter_thread.is_alive():
                task.waiter_thread.join(timeout=1.0)

    def _launch_and_wait(
        self,
        *,
        mode: str,
        cwd_path: Path,
        sandbox: str,
        build_cmd: Any,
        wait_seconds: int,
        async_mode: bool,
        baseline: dict[str, Any] | None = None,
        is_resume: bool = False,
        initial_session_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        tmp_dir = tempfile.TemporaryDirectory(prefix="codex_bridge_")
        last_msg_path = Path(tmp_dir.name) / "last_message.md"
        task = _TaskState(
            task_id=task_id,
            mode=mode,
            cwd=str(cwd_path),
            sandbox=sandbox,
            extra=dict(extra or {}),
            start_monotonic=time.monotonic(),
            started_at=time.time(),
            tmp_dir=tmp_dir,
            last_msg_path=last_msg_path,
            baseline=baseline,
            is_resume=is_resume,
            session_id=initial_session_id,
        )

        try:
            cmd = build_cmd(last_msg_path)
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd_path),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=True,
            )
        except Exception as exc:
            try:
                tmp_dir.cleanup()
            except OSError:
                pass
            if isinstance(exc, CodexRunnerError):
                raise
            raise CodexRunnerError(f"Failed to launch Codex CLI: {exc}") from exc

        task.proc = proc
        with self._registry_lock:
            self._tasks[task_id] = task

        with task.lock:
            initial_record = self._build_running_record(
                task,
                pid=proc.pid,
                owner_pid=os.getpid(),
            )
            self._persist_task_unlocked(task_id, initial_record)

        waiter = threading.Thread(target=self._collect_task, args=(task,), daemon=True)
        task.waiter_thread = waiter
        waiter.start()

        if async_mode:
            return {
                "status": "running",
                "task_id": task_id,
                "mode": mode,
                "session_id": initial_session_id,
                "sandbox": sandbox,
                "cwd": str(cwd_path),
                "elapsed_ms": 0,
                "progress_events": 0,
                "error": None,
            }

        if wait_seconds > 0:
            task.done_event.wait(timeout=wait_seconds)
        return self._running_snapshot(task)

    def _build_running_record(
        self,
        task: _TaskState,
        *,
        pid: int | None = None,
        owner_pid: int | None = None,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "status": "running",
            "task_id": task.task_id,
            "mode": task.mode,
            "session_id": task.session_id,
            "sandbox": task.sandbox,
            "cwd": task.cwd,
            "started_at": task.started_at,
            "elapsed_ms": int((time.monotonic() - task.start_monotonic) * 1000),
            "progress_events": task.progress_events,
            "error": None,
        }
        if pid is not None:
            record["pid"] = pid
        if owner_pid is not None:
            record["owner_pid"] = owner_pid
        return record

    def _running_snapshot(self, task: _TaskState) -> dict[str, Any]:
        with task.lock:
            if task.result is not None:
                return dict(task.result)
            snapshot = self._build_running_record(task)
            snapshot.pop("started_at", None)
            return snapshot

    def _drain_stdout(self, task: _TaskState) -> None:
        proc = task.proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                stripped = line.strip()
                parsed_event: dict[str, Any] | None = None
                if stripped:
                    try:
                        candidate = json.loads(stripped)
                        if isinstance(candidate, dict):
                            parsed_event = candidate
                    except json.JSONDecodeError:
                        parsed_event = None
                with task.lock:
                    task.stdout_lines.append(line)
                    if parsed_event is not None:
                        task.progress_events += 1
                        thread_id = parsed_event.get("thread_id")
                        if isinstance(thread_id, str) and (
                            parsed_event.get("type") == "thread.started" or task.session_id is None
                        ):
                            task.session_id = thread_id
        finally:
            try:
                proc.stdout.close()
            except OSError:
                pass

    def _drain_stderr(self, task: _TaskState) -> None:
        proc = task.proc
        if proc is None or proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                with task.lock:
                    task.stderr_lines.append(line)
        finally:
            try:
                proc.stderr.close()
            except OSError:
                pass

    def _collect_task(self, task: _TaskState) -> None:
        proc = task.proc
        assert proc is not None
        try:
            stdout_thread = threading.Thread(target=self._drain_stdout, args=(task,), daemon=True)
            stderr_thread = threading.Thread(target=self._drain_stderr, args=(task,), daemon=True)
            stdout_thread.start()
            stderr_thread.start()

            returncode = proc.wait()
            stdout_thread.join()
            stderr_thread.join()

            elapsed_ms = int((time.monotonic() - task.start_monotonic) * 1000)

            with task.lock:
                stdout_text = "".join(task.stdout_lines)
                stderr_text = "".join(task.stderr_lines)
                task.stdout_lines.clear()
                task.stderr_lines.clear()
                live_session_id = task.session_id

            parsed = parse_jsonl_output(stdout_text)
            session_id = (
                parsed["session_id"]
                or live_session_id
                or task.extra.get("requested_session_id")
            )

            last_msg_text = ""
            if task.last_msg_path.is_file():
                try:
                    last_msg_text = task.last_msg_path.read_text(
                        encoding="utf-8", errors="replace"
                    ).strip()
                except OSError:
                    last_msg_text = ""

            summary = last_msg_text or parsed["agent_text"]
            if not summary and returncode == 0:
                plain_lines: list[str] = []
                for raw in stdout_text.splitlines():
                    stripped = raw.strip()
                    if not stripped:
                        continue
                    try:
                        json.loads(stripped)
                    except json.JSONDecodeError:
                        plain_lines.append(stripped)
                summary = "\n".join(plain_lines).strip()

            error_msg: str | None = None
            if returncode != 0:
                err_parts: list[str] = list(parsed["errors"])
                stderr_clean = stderr_text.strip()
                if stderr_clean and stderr_clean not in err_parts:
                    err_parts.append(stderr_clean)
                error_msg = "\n".join(err_parts).strip() or f"Codex exited with status {returncode}"
                if not summary:
                    summary = error_msg

            changed_files, pre_dirty, diff_stat = _compute_git_delta(
                Path(task.cwd),
                task.baseline,
            )

            result: dict[str, Any] = {
                "status": "completed" if returncode == 0 else "failed",
                "task_id": task.task_id,
                "mode": task.mode,
                "session_id": session_id,
                "sandbox": task.sandbox,
                "cwd": task.cwd,
                "summary": summary,
                "usage": parsed["usage"],
                "events_count": parsed["events_count"],
                "elapsed_ms": elapsed_ms,
                "returncode": returncode,
                "error": error_msg,
                "errors": parsed["errors"],
            }
            if stdout_text or stderr_text:
                log_dir = (self.tasks_dir / task.task_id).resolve()
                log_dir.mkdir(mode=0o700)
                for stream, content in (("stdout", stdout_text), ("stderr", stderr_text)):
                    if content:
                        log_path = log_dir / f"{stream}.log"
                        log_path.write_text(content, encoding="utf-8")
                        result[f"{stream}_path"] = str(log_path)
            self._attach_mode_fields(
                task,
                result,
                changed_files=changed_files,
                pre_dirty=pre_dirty,
                diff_stat=diff_stat,
                failed=returncode != 0,
            )

            with task.lock:
                task.session_id = session_id
                task.result = self._persist_terminal_result(task.task_id, result)
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - task.start_monotonic) * 1000)
            fallback: dict[str, Any] = {
                "status": "failed",
                "task_id": task.task_id,
                "mode": task.mode,
                "session_id": task.session_id,
                "sandbox": task.sandbox,
                "cwd": task.cwd,
                "summary": f"Internal runner error: {exc}",
                "usage": None,
                "events_count": task.progress_events,
                "elapsed_ms": elapsed_ms,
                "returncode": None,
                "error": f"Internal runner error: {exc}",
                "errors": [str(exc)],
            }
            self._attach_mode_fields(
                task,
                fallback,
                changed_files=[],
                pre_dirty=[],
                diff_stat=None,
                failed=True,
            )
            with task.lock:
                task.stdout_lines.clear()
                task.stderr_lines.clear()
                task.result = self._persist_terminal_result(task.task_id, fallback)
        finally:
            for stream in (proc.stdout, proc.stderr):
                if stream is not None and not stream.closed:
                    try:
                        stream.close()
                    except OSError:
                        pass
            try:
                task.tmp_dir.cleanup()
            except OSError:
                pass
            task.done_event.set()

    def _attach_mode_fields(
        self,
        task: _TaskState,
        payload: dict[str, Any],
        *,
        changed_files: list[str],
        pre_dirty: list[str],
        diff_stat: str | None,
        failed: bool,
    ) -> None:
        if task.mode in ("exec", "resume"):
            payload["changed_files"] = changed_files
            payload["pre_existing_dirty_files"] = pre_dirty
            baseline_status = task.baseline["status"] if task.baseline is not None else {}
            payload["reedited_dirty_files"] = [path for path in changed_files if path in baseline_status]
            payload["diff_stat"] = diff_stat
            summary, verification, unverified = _split_verification_report(payload["summary"])
            payload.update(summary=summary, verification=verification, unverified=unverified)
            if task.is_resume and failed:
                payload["resume_failed"] = True
                payload["hint"] = (
                    "Codex session resume failed (the session may be missing, expired, or incompatible). "
                    "Retry as a fresh codex_run without session_id, carrying the approved plan context in the prompt."
                )
            else:
                payload["resume_failed"] = False
                payload["hint"] = None
        if task.mode == "review":
            payload["review_mode"] = task.extra.get("review_mode")
            payload["routed_via_exec"] = bool(task.extra.get("routed_via_exec", False))

    def _persist_task_unlocked(self, task_id: str, payload: dict[str, Any]) -> None:
        task_path = self.tasks_dir / f"{task_id}.json"
        self._write_task_file(task_path, payload)

    def _persist_terminal_result(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        task_path = self.tasks_dir / f"{task_id}.json"
        self._write_task_file(task_path, payload)
        return _task_receipt(payload, task_path)

    def _write_task_file(self, task_path: Path, payload: dict[str, Any]) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.tasks_dir, prefix=f".{task_path.name}.", delete=False
        ) as handle:
            tmp_path = Path(handle.name)
            try:
                json.dump(payload, handle, indent=2)
                handle.write("\n")
                handle.close()
                os.replace(tmp_path, task_path)
            finally:
                tmp_path.unlink(missing_ok=True)

    def _read_persisted_task(self, task_path: Path) -> dict[str, Any]:
        try:
            data = json.loads(task_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CodexRunnerError(f"Failed to read task state {task_path.name}: {exc}") from exc
        if not isinstance(data, dict):
            raise CodexRunnerError(f"Corrupted task state {task_path.name}")
        return data
