#!/usr/bin/env python3
"""Zero-dependency stdio MCP server bridging Antigravity to OpenAI Codex CLI."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Any, Callable, TextIO

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from codex_runner import (  # noqa: E402
    DEFAULT_POLL_WAIT_SECONDS,
    DEFAULT_WAIT_SECONDS,
    MAX_WAIT_SECONDS,
    VALID_REVIEW_MODES,
    CodexRunnerError,
    CodexTaskManager,
    check_status,
)

SERVER_INFO = {"name": "codex-bridge", "version": "0.4.0"}
SUPPORTED_PROTOCOL_VERSIONS = ("2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL_VERSION = "2025-03-26"

SERVER_INSTRUCTIONS = (
    "The Antigravity primary agent dispatches directly to OpenAI Codex CLI with codex_run. "
    "In Orchestrator mode this is the default for code implementation and test execution; "
    "use model=gpt-6.2-sol unless the user specifies another model. "
    "When both Codex Bridge endpoints are installed in WSL, route POSIX absolute paths to "
    "codex-bridge_codex-wsl and Windows paths to codex-bridge_codex. Keep each task on its endpoint. "
    "Pass async_mode=true for background work and poll codex_poll with the returned task_id "
    "until the task finishes. Use codex_plan for a read-only proposal and codex_review for a "
    "read-only code review. Pass session_id to codex_run to continue in the same workspace. "
    "Return the verified result to the user in the primary conversation."
)

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603


class _JsonRpcError(Exception):
    """Internal exception carrying a JSON-RPC 2.0 error code and message."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _sandbox_enum() -> list[str]:
    if os.environ.get("CODEX_BRIDGE_ALLOW_DANGER_SANDBOX") == "1":
        return ["workspace-write", "read-only", "danger-full-access"]
    return ["workspace-write", "read-only"]


def _build_tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "codex_status",
            "description": (
                "Check Codex CLI binary availability, version, authentication readiness, "
                "danger-sandbox opt-in state, and optional Git repository status."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "cwd": {
                        "type": "string",
                        "description": "Optional workspace directory to validate and inspect for Git state.",
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "codex_plan",
            "description": (
                "Run Codex in strict read-only planning mode (Mode A step 1) and return "
                "a structured plan summary plus session_id for subsequent codex_run resume."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Planning task prompt for Codex.",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Absolute path to the target workspace directory.",
                    },
                    "model": {
                        "type": "string",
                        "description": "Optional Codex model override.",
                    },
                    "reasoning_effort": {
                        "type": "string",
                        "description": "Optional model_reasoning_effort override (e.g., low, medium, high, xhigh).",
                    },
                    "images": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of image file paths to attach.",
                    },
                    "wait_seconds": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": MAX_WAIT_SECONDS,
                        "default": DEFAULT_WAIT_SECONDS,
                        "description": "Maximum seconds to wait synchronously before returning a running handle.",
                    },
                },
                "required": ["prompt", "cwd"],
                "additionalProperties": False,
            },
        },
        {
            "name": "codex_run",
            "description": (
                "Dispatch a fresh or resumed Codex worker from the primary conversation. Pass session_id to "
                "resume an approved codex_plan session (Mode A step 2). Returns summary, "
                "session_id, changed_files, diff_stat, and resume_failed/hint on resume errors."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Execution instructions for Codex.",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Absolute path to the target workspace directory.",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Optional Codex session/thread ID to resume.",
                    },
                    "sandbox": {
                        "type": "string",
                        "enum": _sandbox_enum(),
                        "default": "workspace-write",
                        "description": "Codex execution sandbox mode.",
                    },
                    "model": {
                        "type": "string",
                        "description": "Optional Codex model override.",
                    },
                    "reasoning_effort": {
                        "type": "string",
                        "description": "Optional model_reasoning_effort override.",
                    },
                    "images": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of image file paths to attach.",
                    },
                    "async_mode": {
                        "type": "boolean",
                        "default": False,
                        "description": "If true, return immediately with a task_id for codex_poll.",
                    },
                    "wait_seconds": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": MAX_WAIT_SECONDS,
                        "default": DEFAULT_WAIT_SECONDS,
                        "description": "Maximum seconds to wait synchronously when async_mode is false.",
                    },
                },
                "required": ["prompt", "cwd"],
                "additionalProperties": False,
            },
        },
        {
            "name": "codex_poll",
            "description": (
                "Poll a background or timed-out Codex task by task_id, optionally waiting up "
                "to wait_seconds for completion."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Task identifier returned by codex_plan, codex_run, or codex_review.",
                    },
                    "wait_seconds": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": MAX_WAIT_SECONDS,
                        "default": DEFAULT_POLL_WAIT_SECONDS,
                        "description": "Maximum seconds to wait for completion before returning current status.",
                    },
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "codex_review",
            "description": (
                "Run a read-only Codex code review against uncommitted changes, a base branch, "
                "a specific commit, or a custom review prompt."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "cwd": {
                        "type": "string",
                        "description": "Absolute path to the target workspace directory.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": list(VALID_REVIEW_MODES),
                        "default": "uncommitted",
                        "description": "Review target mode.",
                    },
                    "base_branch": {
                        "type": "string",
                        "description": "Base branch name (required when mode='base').",
                    },
                    "commit_sha": {
                        "type": "string",
                        "description": "Commit SHA (required when mode='commit').",
                    },
                    "title": {
                        "type": "string",
                        "description": "Optional review title context.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Review focus prompt (required when mode='custom').",
                    },
                    "model": {
                        "type": "string",
                        "description": "Optional Codex model override.",
                    },
                    "wait_seconds": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": MAX_WAIT_SECONDS,
                        "default": DEFAULT_WAIT_SECONDS,
                        "description": "Maximum seconds to wait synchronously for review completion.",
                    },
                },
                "required": ["cwd"],
                "additionalProperties": False,
            },
        },
    ]


def _validate_arg_keys(args: dict[str, Any], allowed: set[str], required: tuple[str, ...]) -> None:
    unknown = sorted(set(args.keys()) - allowed)
    if unknown:
        raise CodexRunnerError(f"Unexpected argument(s): {', '.join(unknown)}")
    for key in required:
        if key not in args or args[key] is None:
            raise CodexRunnerError(f"Missing required argument: {key}")


def _opt_str(args: dict[str, Any], key: str) -> str | None:
    val = args.get(key)
    if val is None:
        return None
    if not isinstance(val, str):
        raise CodexRunnerError(f"{key} must be a string.")
    return val


def _req_str(args: dict[str, Any], key: str) -> str:
    val = args.get(key)
    if not isinstance(val, str) or not val.strip():
        raise CodexRunnerError(f"{key} must be a non-empty string.")
    return val


def _opt_bool(args: dict[str, Any], key: str, default: bool) -> bool:
    val = args.get(key)
    if val is None:
        return default
    if not isinstance(val, bool):
        raise CodexRunnerError(f"{key} must be a boolean.")
    return val


def _opt_int(args: dict[str, Any], key: str, default: int) -> int:
    val = args.get(key)
    if val is None:
        return default
    if isinstance(val, bool) or not isinstance(val, int):
        raise CodexRunnerError(f"{key} must be an integer.")
    return val


def _opt_str_list(args: dict[str, Any], key: str) -> list[str] | None:
    val = args.get(key)
    if val is None:
        return None
    if not isinstance(val, list) or not all(isinstance(item, str) for item in val):
        raise CodexRunnerError(f"{key} must be a list of strings.")
    return val


def _format_tool_result(payload: dict[str, Any], *, is_error: bool) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False, indent=2),
            }
        ],
        "isError": is_error,
    }


class McpServer:
    """Line-delimited JSON-RPC 2.0 MCP server over stdio."""

    def __init__(
        self,
        data_dir: str | Path | None = None,
        manager: CodexTaskManager | None = None,
    ) -> None:
        self.manager = manager if manager is not None else CodexTaskManager(data_dir=data_dir)
        self._write_lock = threading.Lock()
        self._tool_handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "codex_status": self._call_status,
            "codex_plan": self._call_plan,
            "codex_run": self._call_run,
            "codex_poll": self._call_poll,
            "codex_review": self._call_review,
        }

    def _call_status(self, args: dict[str, Any]) -> dict[str, Any]:
        _validate_arg_keys(args, {"cwd"}, ())
        return check_status(cwd=_opt_str(args, "cwd"))

    def _call_plan(self, args: dict[str, Any]) -> dict[str, Any]:
        _validate_arg_keys(
            args,
            {"prompt", "cwd", "model", "reasoning_effort", "images", "wait_seconds"},
            ("prompt", "cwd"),
        )
        return self.manager.run_plan(
            prompt=_req_str(args, "prompt"),
            cwd=_req_str(args, "cwd"),
            model=_opt_str(args, "model"),
            reasoning_effort=_opt_str(args, "reasoning_effort"),
            images=_opt_str_list(args, "images"),
            wait_seconds=_opt_int(args, "wait_seconds", DEFAULT_WAIT_SECONDS),
        )

    def _call_run(self, args: dict[str, Any]) -> dict[str, Any]:
        _validate_arg_keys(
            args,
            {
                "prompt",
                "cwd",
                "session_id",
                "sandbox",
                "model",
                "reasoning_effort",
                "images",
                "async_mode",
                "wait_seconds",
            },
            ("prompt", "cwd"),
        )
        sandbox = _opt_str(args, "sandbox")
        return self.manager.run_exec(
            prompt=_req_str(args, "prompt"),
            cwd=_req_str(args, "cwd"),
            session_id=_opt_str(args, "session_id"),
            sandbox=sandbox if sandbox is not None else "workspace-write",
            model=_opt_str(args, "model"),
            reasoning_effort=_opt_str(args, "reasoning_effort"),
            images=_opt_str_list(args, "images"),
            async_mode=_opt_bool(args, "async_mode", False),
            wait_seconds=_opt_int(args, "wait_seconds", DEFAULT_WAIT_SECONDS),
        )

    def _call_poll(self, args: dict[str, Any]) -> dict[str, Any]:
        _validate_arg_keys(args, {"task_id", "wait_seconds"}, ("task_id",))
        return self.manager.poll_task(
            task_id=_req_str(args, "task_id"),
            wait_seconds=_opt_int(args, "wait_seconds", DEFAULT_POLL_WAIT_SECONDS),
        )

    def _call_review(self, args: dict[str, Any]) -> dict[str, Any]:
        _validate_arg_keys(
            args,
            {"cwd", "mode", "base_branch", "commit_sha", "title", "prompt", "model", "wait_seconds"},
            ("cwd",),
        )
        mode = _opt_str(args, "mode")
        return self.manager.run_review(
            cwd=_req_str(args, "cwd"),
            mode=mode if mode is not None else "uncommitted",
            base_branch=_opt_str(args, "base_branch"),
            commit_sha=_opt_str(args, "commit_sha"),
            title=_opt_str(args, "title"),
            prompt=_opt_str(args, "prompt"),
            model=_opt_str(args, "model"),
            wait_seconds=_opt_int(args, "wait_seconds", DEFAULT_WAIT_SECONDS),
        )

    def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        protocol_version = (
            requested
            if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS
            else DEFAULT_PROTOCOL_VERSION
        )
        return {
            "protocolVersion": protocol_version,
            "capabilities": {"tools": {}},
            "serverInfo": dict(SERVER_INFO),
            "instructions": SERVER_INSTRUCTIONS,
        }

    def _handle_tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        tool_name = params.get("name")
        if not isinstance(tool_name, str) or not tool_name:
            raise _JsonRpcError(JSONRPC_INVALID_PARAMS, "tools/call requires a string tool name.")

        handler = self._tool_handlers.get(tool_name)
        if handler is None:
            raise _JsonRpcError(JSONRPC_INVALID_PARAMS, f"Unknown tool: {tool_name}")

        raw_args = params.get("arguments")
        if raw_args is None:
            args: dict[str, Any] = {}
        elif isinstance(raw_args, dict):
            args = raw_args
        else:
            return _format_tool_result(
                {"status": "error", "error": "Tool arguments must be an object."},
                is_error=True,
            )

        try:
            payload = handler(args)
        except CodexRunnerError as exc:
            return _format_tool_result(
                {"status": "error", "error": str(exc)},
                is_error=True,
            )

        is_error = payload.get("status") == "failed"
        return _format_tool_result(payload, is_error=is_error)

    def handle_request(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        """Process one JSON-RPC 2.0 request/notification dict and return a response dict or None."""
        if not isinstance(msg, dict):
            return self._error_response(None, JSONRPC_INVALID_REQUEST, "Request must be a JSON object.")

        has_id = "id" in msg
        msg_id = msg.get("id")
        method = msg.get("method")

        if not isinstance(method, str) or not method:
            if not has_id:
                return None
            return self._error_response(msg_id, JSONRPC_INVALID_REQUEST, "Missing or invalid method.")

        if not has_id or method == "notifications/initialized":
            return None

        raw_params = msg.get("params")
        if raw_params is None:
            params: dict[str, Any] = {}
        elif isinstance(raw_params, dict):
            params = raw_params
        else:
            return self._error_response(msg_id, JSONRPC_INVALID_PARAMS, "params must be a JSON object.")

        try:
            if method == "initialize":
                result = self._handle_initialize(params)
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": _build_tool_definitions()}
            elif method == "tools/call":
                result = self._handle_tools_call(params)
            else:
                return self._error_response(
                    msg_id,
                    JSONRPC_METHOD_NOT_FOUND,
                    f"Method not found: {method}",
                )
        except _JsonRpcError as exc:
            return self._error_response(msg_id, exc.code, exc.message)
        except Exception as exc:
            print(f"[codex-bridge] internal error handling {method}: {exc}", file=sys.stderr)
            return self._error_response(msg_id, JSONRPC_INTERNAL_ERROR, f"Internal error: {exc}")

        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _error_response(msg_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": code, "message": message},
        }

    def _write_json(self, stdout: TextIO, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False)
        with self._write_lock:
            stdout.write(encoded + "\n")
            stdout.flush()

    def serve_stdio(
        self,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
    ) -> None:
        """Serve newline-delimited JSON-RPC 2.0 requests on stdio until EOF."""
        in_stream = stdin if stdin is not None else sys.stdin
        out_stream = stdout if stdout is not None else sys.stdout
        try:
            for raw_line in in_stream:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError as exc:
                    self._write_json(
                        out_stream,
                        self._error_response(None, JSONRPC_PARSE_ERROR, f"Parse error: {exc}"),
                    )
                    continue

                if not isinstance(msg, dict):
                    self._write_json(
                        out_stream,
                        self._error_response(
                            None,
                            JSONRPC_INVALID_REQUEST,
                            "JSON-RPC message must be an object.",
                        ),
                    )
                    continue

                response = self.handle_request(msg)
                if response is not None:
                    self._write_json(out_stream, response)
        finally:
            self.manager.shutdown(terminate_running=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Zero-dependency stdio MCP server for OpenAI Codex CLI.",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Directory for persisted task state (defaults to ~/.cache/codex-bridge).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    server = McpServer(data_dir=args.data_dir)
    install_sigterm = (
        hasattr(signal, "SIGTERM")
        and threading.current_thread() is threading.main_thread()
    )
    prev_sigterm = signal.getsignal(signal.SIGTERM) if install_sigterm else None

    if install_sigterm:
        def _handle_sigterm(_signum: int, _frame: Any) -> None:
            # Ignore duplicate SIGTERM while serve_stdio()'s finally block terminates children.
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, _handle_sigterm)

    try:
        server.serve_stdio()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        if install_sigterm and prev_sigterm is not None:
            signal.signal(signal.SIGTERM, prev_sigterm)
    return 0


if __name__ == "__main__":
    sys.exit(main())
