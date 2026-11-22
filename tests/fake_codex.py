#!/usr/bin/env python3
"""Configurable mock Codex CLI for codex-bridge automated tests."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def _log_invocation(argv: list[str]) -> None:
    args_file = os.environ.get("FAKE_CODEX_ARGS_FILE")
    if not args_file:
        return
    record = {
        "argv": argv,
        "cwd": os.getcwd(),
    }
    path = Path(args_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def _extract_sandbox_from_config(config_items: list[str]) -> str | None:
    for item in config_items:
        if item.startswith("sandbox_mode="):
            raw = item.split("=", 1)[1].strip()
            return raw.strip("\"'")
    return None


def _next_arg(args: list[str], idx: int, flag: str) -> tuple[str, int]:
    if idx + 1 >= len(args):
        print(f"error: flag '{flag}' requires a value", file=sys.stderr)
        sys.exit(2)
    return args[idx + 1], idx + 1


def _parse_exec_flags(args: list[str], *, allow_sandbox_and_cd: bool) -> tuple[dict, list[str]]:
    opts: dict = {
        "sandbox": None,
        "cd": None,
        "model": None,
        "config": [],
        "skip_git_repo_check": False,
        "json": False,
        "output_last_message": None,
        "images": [],
        "uncommitted": False,
        "base": None,
        "commit": None,
        "title": None,
    }
    positional: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            positional.extend(args[i + 1 :])
            break
        if arg in ("-s", "--sandbox"):
            if not allow_sandbox_and_cd:
                print(f"error: unexpected argument '{arg}' found", file=sys.stderr)
                sys.exit(2)
            opts["sandbox"], i = _next_arg(args, i, arg)
        elif arg in ("-C", "--cd"):
            if not allow_sandbox_and_cd:
                print(f"error: unexpected argument '{arg}' found", file=sys.stderr)
                sys.exit(2)
            opts["cd"], i = _next_arg(args, i, arg)
        elif arg in ("-m", "--model"):
            opts["model"], i = _next_arg(args, i, arg)
        elif arg == "-c":
            val, i = _next_arg(args, i, arg)
            opts["config"].append(val)
        elif arg == "--skip-git-repo-check":
            opts["skip_git_repo_check"] = True
        elif arg == "--json":
            opts["json"] = True
        elif arg in ("-o", "--output-last-message"):
            opts["output_last_message"], i = _next_arg(args, i, arg)
        elif arg in ("-i", "--image"):
            val, i = _next_arg(args, i, arg)
            if val.startswith("-"):
                print(
                    f"error: a value is required for '{arg} <FILE>...' but none was supplied",
                    file=sys.stderr,
                )
                sys.exit(2)
            opts["images"].append(val)
            while i + 1 < len(args) and not args[i + 1].startswith("-"):
                i += 1
                opts["images"].append(args[i])
        elif arg == "--uncommitted":
            opts["uncommitted"] = True
        elif arg == "--base":
            opts["base"], i = _next_arg(args, i, arg)
        elif arg == "--commit":
            opts["commit"], i = _next_arg(args, i, arg)
        elif arg == "--title":
            opts["title"], i = _next_arg(args, i, arg)
        elif arg.startswith("-"):
            print(f"error: unknown flag '{arg}'", file=sys.stderr)
            sys.exit(2)
        else:
            positional.append(arg)
        i += 1
    return opts, positional


def _maybe_sleep() -> None:
    raw = os.environ.get("FAKE_CODEX_SLEEP", "").strip()
    if raw:
        try:
            delay = float(raw)
        except ValueError:
            delay = 0.0
        if delay > 0:
            time.sleep(delay)


def _handle_login(args: list[str]) -> int:
    if args == ["status"]:
        status = os.environ.get("FAKE_CODEX_LOGIN_STATUS", "Logged in using ChatGPT")
        print(status)
        if "not logged in" in status.lower():
            return 1
        return 0
    print(f"Unsupported login subcommand: {args}", file=sys.stderr)
    return 2


def _emit_jsonl_start(*, session_id: str, use_json: bool) -> None:
    if not use_json:
        return
    for ev in (
        {"type": "thread.started", "thread_id": session_id},
        {"type": "turn.started"},
    ):
        print(json.dumps(ev), flush=True)


def _emit_jsonl_finish(
    *,
    summary: str,
    use_json: bool,
    output_path: str | None,
    input_tokens: int = 120,
    output_tokens: int = 45,
) -> None:
    summary = os.environ.get("FAKE_CODEX_SUMMARY", summary)
    if use_json:
        events = [
            {
                "type": "item.completed",
                "item": {
                    "id": "item_1",
                    "type": "agent_message",
                    "text": summary,
                },
            },
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                },
            },
        ]
        for ev in events:
            print(json.dumps(ev), flush=True)
    else:
        print(summary, flush=True)

    if output_path and os.environ.get("FAKE_CODEX_EMPTY_OUTPUT") != "1":
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(summary, encoding="utf-8")


def _maybe_mutate_workspace(target_dir: Path, sandbox: str | None) -> None:
    write_rel = os.environ.get("FAKE_CODEX_WRITE_FILE", "").strip()
    if not write_rel:
        return
    if sandbox not in ("workspace-write", "danger-full-access"):
        return
    target = Path(write_rel)
    if not target.is_absolute():
        target = target_dir / target
    target.parent.mkdir(parents=True, exist_ok=True)
    content = os.environ.get("FAKE_CODEX_WRITE_CONTENT", "updated by fake_codex\n")
    target.write_text(content, encoding="utf-8")


def _handle_exec(args: list[str]) -> int:
    fail_mode = os.environ.get("FAKE_CODEX_FAIL_MODE", "").strip()

    if args and args[0] == "resume":
        opts, pos = _parse_exec_flags(args[1:], allow_sandbox_and_cd=False)
        if fail_mode in ("resume", "always"):
            _maybe_sleep()
            err_msg = "Error: session not found or cannot be resumed"
            if opts["json"]:
                print(json.dumps({"type": "error", "message": err_msg}), flush=True)
            print(err_msg, file=sys.stderr)
            return 1
        session_id = pos[0] if pos else os.environ.get("FAKE_CODEX_SESSION_ID", "thr_fake_resume")
        _emit_jsonl_start(session_id=session_id, use_json=opts["json"])
        _maybe_sleep()
        prompt = " ".join(pos[1:]) if len(pos) > 1 else ""
        sandbox = _extract_sandbox_from_config(opts["config"]) or "read-only"
        _maybe_mutate_workspace(Path.cwd(), sandbox)
        summary = f"Resumed session {session_id} (sandbox={sandbox}): {prompt}".strip()
        _emit_jsonl_finish(
            summary=summary,
            use_json=opts["json"],
            output_path=opts["output_last_message"],
        )
        return 0

    if args and args[0] == "review":
        opts, pos = _parse_exec_flags(args[1:], allow_sandbox_and_cd=False)
        target_flags = sum(1 for flag in (opts["uncommitted"], opts["base"], opts["commit"]) if flag)
        if target_flags > 1:
            print("error: '--uncommitted', '--base', and '--commit' are mutually exclusive", file=sys.stderr)
            return 2
        if target_flags and pos:
            print("error: the argument '[PROMPT]' cannot be used with '--uncommitted'/'--base'/'--commit'", file=sys.stderr)
            return 2
        if fail_mode in ("review", "always"):
            _maybe_sleep()
            err_msg = "Error: review failed"
            if opts["json"]:
                print(json.dumps({"type": "error", "message": err_msg}), flush=True)
            print(err_msg, file=sys.stderr)
            return 1
        session_id = os.environ.get("FAKE_CODEX_SESSION_ID", "thr_fake_review")
        _emit_jsonl_start(session_id=session_id, use_json=opts["json"])
        _maybe_sleep()
        mode_desc = (
            "uncommitted"
            if opts["uncommitted"]
            else f"base={opts['base']}"
            if opts["base"]
            else f"commit={opts['commit']}"
            if opts["commit"]
            else f"custom={ ' '.join(pos) }"
        )
        summary = f"Review completed ({mode_desc}): no critical issues found."
        _emit_jsonl_finish(
            summary=summary,
            use_json=opts["json"],
            output_path=opts["output_last_message"],
        )
        return 0

    opts, pos = _parse_exec_flags(args, allow_sandbox_and_cd=True)
    if fail_mode in ("exec", "always"):
        _maybe_sleep()
        err_msg = "Error: codex exec failed"
        if opts["json"]:
            print(json.dumps({"type": "error", "message": err_msg}), flush=True)
        print(err_msg, file=sys.stderr)
        return 1

    session_id = os.environ.get("FAKE_CODEX_SESSION_ID", "thr_fake_001")
    _emit_jsonl_start(session_id=session_id, use_json=opts["json"])
    _maybe_sleep()
    target_dir = Path(opts["cd"]) if opts["cd"] else Path.cwd()
    sandbox = opts["sandbox"] or _extract_sandbox_from_config(opts["config"]) or "read-only"
    _maybe_mutate_workspace(target_dir, sandbox)
    prompt = " ".join(pos)
    summary = f"Executed (sandbox={sandbox}): {prompt}".strip()
    _emit_jsonl_finish(
        summary=summary,
        use_json=opts["json"],
        output_path=opts["output_last_message"],
    )
    return 0


def main(argv: list[str]) -> int:
    _log_invocation(argv)
    if not argv:
        print("codex mock CLI", file=sys.stderr)
        return 2

    if argv[0] in ("--version", "-V"):
        if os.environ.get("FAKE_CODEX_FAIL_MODE") == "version":
            print("version error", file=sys.stderr)
            return 1
        print(os.environ.get("FAKE_CODEX_VERSION", "codex-cli 0.171.0-alpha.13"))
        return 0

    if argv[0] == "login":
        return _handle_login(argv[1:])

    if argv[0] == "exec":
        return _handle_exec(argv[1:])

    print(f"Unsupported command: {argv}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
