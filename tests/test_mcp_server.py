from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_SCRIPT = REPO_ROOT / "scripts" / "codex_mcp_server.py"
RUNNER_SCRIPT = REPO_ROOT / "scripts" / "codex_runner.py"
FAKE_CODEX = str(REPO_ROOT / "tests" / "fake_codex.py")


class _McpClient:
    """Line-delimited JSON-RPC 2.0 client over subprocess stdin/stdout."""

    def __init__(self, data_dir: Path, extra_env: dict[str, str] | None = None, unset_env: tuple[str, ...] = ()) -> None:
        env = os.environ.copy()
        env["CODEX_BIN"] = FAKE_CODEX
        for key in unset_env:
            env.pop(key, None)
        if extra_env:
            env.update(extra_env)

        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER_SCRIPT), "--data-dir", str(data_dir)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=env,
        )
        self._next_id = 1

    def send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        assert self.proc.stdin is not None
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        assert self.proc.stdin is not None
        assert self.proc.stdout is not None
        req_id = self._next_id
        self._next_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

        line = self.proc.stdout.readline()
        if not line:
            stderr_text = self.proc.stderr.read() if self.proc.stderr else ""
            raise AssertionError(f"MCP server closed stdout unexpectedly. stderr={stderr_text!r}")
        resp = json.loads(line)
        if resp.get("id") != req_id:
            raise AssertionError(f"Expected response id {req_id}, got {resp!r}")
        return resp

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        resp = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        if "error" in resp:
            raise AssertionError(f"Unexpected JSON-RPC error from tools/call({name}): {resp['error']!r}")
        result = resp["result"]
        content = result["content"]
        if not isinstance(content, list) or len(content) != 1 or content[0].get("type") != "text":
            raise AssertionError(f"Unexpected MCP tool content shape: {result!r}")
        parsed_payload = json.loads(content[0]["text"])
        return result, parsed_payload

    def close(self, timeout: float = 5.0) -> tuple[int, str]:
        if self.proc.stdin and not self.proc.stdin.closed:
            self.proc.stdin.close()
        rc = self.proc.wait(timeout=timeout)
        stderr_text = self.proc.stderr.read() if self.proc.stderr else ""
        if self.proc.stdout and not self.proc.stdout.closed:
            self.proc.stdout.close()
        if self.proc.stderr and not self.proc.stderr.closed:
            self.proc.stderr.close()
        return rc, stderr_text


class TestMcpServer(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.data_dir = self.tmpdir / "bridge_data"
        self.workdir = self.tmpdir / "workspace"
        self.workdir.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_scripts_are_executable(self) -> None:
        for path in (SERVER_SCRIPT, RUNNER_SCRIPT):
            self.assertTrue(path.is_file(), f"Missing script: {path}")
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertTrue(mode & stat.S_IXUSR, f"Expected owner execute bit on {path}, got {oct(mode)}")

    def test_initialize_and_tools_list(self) -> None:
        client = _McpClient(
            self.data_dir,
            unset_env=("CODEX_BRIDGE_ALLOW_DANGER_SANDBOX",),
        )
        try:
            init_resp = client.request(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test-client", "version": "1.0.0"},
                },
            )
            result = init_resp["result"]
            self.assertEqual(result["protocolVersion"], "2024-11-05")
            self.assertEqual(result["capabilities"], {"tools": {}})
            self.assertEqual(result["serverInfo"], {"name": "codex-bridge", "version": "0.4.0"})
            instructions = result.get("instructions", "")
            self.assertIn("codex_plan", instructions)
            self.assertIn("codex_run", instructions)
            self.assertIn("primary agent dispatches directly", instructions)

            client.send_notification("notifications/initialized")

            ping_resp = client.request("ping")
            self.assertEqual(ping_resp["result"], {})

            tools_resp = client.request("tools/list")
            tools = tools_resp["result"]["tools"]
            by_name = {tool["name"]: tool for tool in tools}
            self.assertEqual(
                set(by_name.keys()),
                {"codex_status", "codex_plan", "codex_run", "codex_poll", "codex_review"},
            )

            self.assertEqual(by_name["codex_plan"]["inputSchema"]["required"], ["prompt", "cwd"])
            self.assertEqual(by_name["codex_run"]["inputSchema"]["required"], ["prompt", "cwd"])
            self.assertEqual(by_name["codex_poll"]["inputSchema"]["required"], ["task_id"])
            self.assertEqual(by_name["codex_review"]["inputSchema"]["required"], ["cwd"])
            self.assertEqual(
                by_name["codex_run"]["inputSchema"]["properties"]["sandbox"]["enum"],
                ["workspace-write", "read-only"],
            )
        finally:
            rc, _ = client.close()
        self.assertEqual(rc, 0)

    def test_initialize_2025_protocol_and_danger_sandbox_schema(self) -> None:
        client = _McpClient(
            self.data_dir,
            extra_env={"CODEX_BRIDGE_ALLOW_DANGER_SANDBOX": "1"},
        )
        try:
            init_resp = client.request(
                "initialize",
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test-client", "version": "1.0.0"},
                },
            )
            self.assertEqual(init_resp["result"]["protocolVersion"], "2025-03-26")

            tools_resp = client.request("tools/list")
            tools = {t["name"]: t for t in tools_resp["result"]["tools"]}
            self.assertEqual(
                tools["codex_run"]["inputSchema"]["properties"]["sandbox"]["enum"],
                ["workspace-write", "read-only", "danger-full-access"],
            )
        finally:
            rc, _ = client.close()
        self.assertEqual(rc, 0)

    def test_initialize_falls_back_to_latest_supported_protocol_version(self) -> None:
        client = _McpClient(self.data_dir)
        try:
            unknown_resp = client.request(
                "initialize",
                {
                    "protocolVersion": "2099-01-01",
                    "capabilities": {},
                    "clientInfo": {"name": "future-client", "version": "1.0.0"},
                },
            )
            self.assertEqual(unknown_resp["result"]["protocolVersion"], "2025-03-26")

            omitted_resp = client.request(
                "initialize",
                {
                    "capabilities": {},
                    "clientInfo": {"name": "legacy-client", "version": "1.0.0"},
                },
            )
            self.assertEqual(omitted_resp["result"]["protocolVersion"], "2025-03-26")
        finally:
            rc, _ = client.close()
        self.assertEqual(rc, 0)

    def test_tools_call_plan_then_resume_run_and_review(self) -> None:
        args_file = self.tmpdir / "fake_codex_args.jsonl"
        client = _McpClient(
            self.data_dir,
            extra_env={
                "FAKE_CODEX_ARGS_FILE": str(args_file),
                "FAKE_CODEX_SESSION_ID": "thr_plan_to_exec",
            },
            unset_env=("CODEX_BRIDGE_ALLOW_DANGER_SANDBOX",),
        )
        try:
            client.request("initialize", {"protocolVersion": "2024-11-05"})
            client.send_notification("notifications/initialized")

            status_res, status_payload = client.call_tool("codex_status", {"cwd": str(self.workdir)})
            self.assertFalse(status_res["isError"])
            self.assertTrue(status_payload["binary_found"])
            self.assertTrue(status_payload["auth_ready"])
            self.assertEqual(status_payload["cwd"], str(self.workdir.resolve()))

            plan_res, plan_payload = client.call_tool(
                "codex_plan",
                {"prompt": "Design the fix", "cwd": str(self.workdir)},
            )
            self.assertFalse(plan_res["isError"])
            self.assertEqual(plan_payload["status"], "completed")
            self.assertEqual(plan_payload["mode"], "plan")
            self.assertEqual(plan_payload["sandbox"], "read-only")
            self.assertEqual(plan_payload["session_id"], "thr_plan_to_exec")

            run_res, run_payload = client.call_tool(
                "codex_run",
                {
                    "prompt": "Apply the approved plan",
                    "cwd": str(self.workdir),
                    "session_id": plan_payload["session_id"],
                    "sandbox": "workspace-write",
                },
            )
            self.assertFalse(run_res["isError"])
            self.assertEqual(run_payload["status"], "completed")
            self.assertEqual(run_payload["mode"], "resume")
            self.assertEqual(run_payload["session_id"], "thr_plan_to_exec")
            self.assertFalse(run_payload["resume_failed"])

            review_res, review_payload = client.call_tool(
                "codex_review",
                {"cwd": str(self.workdir), "mode": "uncommitted"},
            )
            self.assertFalse(review_res["isError"])
            self.assertEqual(review_payload["status"], "completed")
            self.assertEqual(review_payload["mode"], "review")
            self.assertEqual(review_payload["review_mode"], "uncommitted")
        finally:
            rc, _ = client.close()
        self.assertEqual(rc, 0)

    def test_tools_call_async_and_poll(self) -> None:
        client = _McpClient(
            self.data_dir,
            extra_env={"FAKE_CODEX_SLEEP": "0.25"},
        )
        try:
            client.request("initialize", {"protocolVersion": "2024-11-05"})
            client.send_notification("notifications/initialized")

            run_res, run_payload = client.call_tool(
                "codex_run",
                {
                    "prompt": "Slow background task",
                    "cwd": str(self.workdir),
                    "async_mode": True,
                },
            )
            self.assertFalse(run_res["isError"])
            self.assertEqual(run_payload["status"], "running")
            task_id = run_payload["task_id"]

            poll_res, poll_payload = client.call_tool(
                "codex_poll",
                {"task_id": task_id, "wait_seconds": 5},
            )
            self.assertFalse(poll_res["isError"])
            self.assertEqual(poll_payload["status"], "completed")
            self.assertEqual(poll_payload["task_id"], task_id)
        finally:
            rc, _ = client.close()
        self.assertEqual(rc, 0)

    def test_tools_call_errors_return_structured_isError_true(self) -> None:
        client = _McpClient(
            self.data_dir,
            unset_env=("CODEX_BRIDGE_ALLOW_DANGER_SANDBOX",),
        )
        try:
            client.request("initialize", {"protocolVersion": "2024-11-05"})
            client.send_notification("notifications/initialized")

            # Missing cwd
            err_res, err_payload = client.call_tool("codex_plan", {"prompt": "Plan something"})
            self.assertTrue(err_res["isError"])
            self.assertIn("cwd", err_payload["error"])

            # Nonexistent cwd
            err_res2, err_payload2 = client.call_tool(
                "codex_run",
                {"prompt": "Do work", "cwd": str(self.tmpdir / "missing_dir")},
            )
            self.assertTrue(err_res2["isError"])
            self.assertIn("does not exist", err_payload2["error"])

            # Disallowed danger-full-access
            err_res3, err_payload3 = client.call_tool(
                "codex_run",
                {
                    "prompt": "Do work",
                    "cwd": str(self.workdir),
                    "sandbox": "danger-full-access",
                },
            )
            self.assertTrue(err_res3["isError"])
            self.assertIn("danger-full-access", err_payload3["error"])

            # Invalid argument types (boolean for wait_seconds, string for async_mode)
            err_res4, err_payload4 = client.call_tool(
                "codex_run",
                {
                    "prompt": "Do work",
                    "cwd": str(self.workdir),
                    "async_mode": "false",
                },
            )
            self.assertTrue(err_res4["isError"])
            self.assertIn("async_mode", err_payload4["error"])

            # Unknown extra parameter
            err_res5, err_payload5 = client.call_tool(
                "codex_status",
                {"unknown_param": 123},
            )
            self.assertTrue(err_res5["isError"])
            self.assertIn("unknown_param", err_payload5["error"])
        finally:
            rc, _ = client.close()
        self.assertEqual(rc, 0)

    def test_failed_resume_preserves_structured_payload_with_isError_true(self) -> None:
        client = _McpClient(
            self.data_dir,
            extra_env={"FAKE_CODEX_FAIL_MODE": "resume"},
        )
        try:
            client.request("initialize", {"protocolVersion": "2024-11-05"})
            client.send_notification("notifications/initialized")

            res, payload = client.call_tool(
                "codex_run",
                {
                    "prompt": "Continue plan",
                    "cwd": str(self.workdir),
                    "session_id": "thr_expired_123",
                },
            )
            self.assertTrue(res["isError"])
            self.assertEqual(payload["status"], "failed")
            self.assertTrue(payload["resume_failed"])
            self.assertTrue(payload["hint"])
            self.assertTrue(payload["error"])
        finally:
            rc, _ = client.close()
        self.assertEqual(rc, 0)

    def test_stdin_eof_shuts_down_running_tasks_and_exits_zero(self) -> None:
        client = _McpClient(
            self.data_dir,
            extra_env={"FAKE_CODEX_SLEEP": "15"},
        )
        try:
            client.request("initialize", {"protocolVersion": "2024-11-05"})
            client.send_notification("notifications/initialized")

            run_res, run_payload = client.call_tool(
                "codex_run",
                {
                    "prompt": "Long running task",
                    "cwd": str(self.workdir),
                    "async_mode": True,
                },
            )
            self.assertFalse(run_res["isError"])
            self.assertEqual(run_payload["status"], "running")
        finally:
            started = time.monotonic()
            rc, _ = client.close(timeout=5.0)
            elapsed = time.monotonic() - started
        self.assertEqual(rc, 0)
        self.assertLess(elapsed, 5.0)

    def test_jsonrpc_protocol_errors_do_not_crash_server(self) -> None:
        client = _McpClient(self.data_dir)
        try:
            assert client.proc.stdin is not None
            assert client.proc.stdout is not None

            # Malformed JSON line -> -32700 Parse error
            client.proc.stdin.write("{not valid json\n")
            client.proc.stdin.flush()
            parse_err = json.loads(client.proc.stdout.readline())
            self.assertEqual(parse_err["error"]["code"], -32700)
            self.assertIsNone(parse_err["id"])

            # Unknown method -> -32601 Method not found
            unknown_method = client.request("nonexistent/method")
            self.assertEqual(unknown_method["error"]["code"], -32601)

            # Unknown tool -> -32602 Invalid params
            unknown_tool = client.request("tools/call", {"name": "no_such_tool", "arguments": {}})
            self.assertEqual(unknown_tool["error"]["code"], -32602)

            # Server remains responsive
            ping_resp = client.request("ping")
            self.assertEqual(ping_resp["result"], {})
        finally:
            rc, _ = client.close()
        self.assertEqual(rc, 0)

    def test_sigterm_during_active_sync_tool_call_terminates_child_process(self) -> None:
        client = _McpClient(
            self.data_dir,
            extra_env={"FAKE_CODEX_SLEEP": "15"},
        )
        child_pid: int | None = None
        try:
            client.request("initialize", {"protocolVersion": "2024-11-05"})
            client.send_notification("notifications/initialized")

            assert client.proc.stdin is not None
            sync_req = {
                "jsonrpc": "2.0",
                "id": 99,
                "method": "tools/call",
                "params": {
                    "name": "codex_run",
                    "arguments": {
                        "prompt": "Blocked synchronous task",
                        "cwd": str(self.workdir),
                        "async_mode": False,
                    },
                },
            }
            client.proc.stdin.write(json.dumps(sync_req) + "\n")
            client.proc.stdin.flush()

            tasks_dir = self.data_dir / "tasks"
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                for path in tasks_dir.glob("task_*.json"):
                    try:
                        record = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    pid = record.get("pid")
                    if isinstance(pid, int) and pid > 0:
                        child_pid = pid
                        break
                if child_pid is not None:
                    break
                time.sleep(0.02)

            self.assertIsNotNone(child_pid, "Expected detached child codex PID to be persisted")
            assert child_pid is not None

            started = time.monotonic()
            client.proc.send_signal(signal.SIGTERM)
            rc = client.proc.wait(timeout=5.0)
            elapsed = time.monotonic() - started

            self.assertEqual(rc, 0)
            self.assertLess(elapsed, 5.0)

            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)
        finally:
            if client.proc.poll() is None:
                client.proc.kill()
                client.proc.wait(timeout=5.0)
            if child_pid is not None:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except OSError:
                    pass
            if client.proc.stdin and not client.proc.stdin.closed:
                client.proc.stdin.close()
            if client.proc.stdout and not client.proc.stdout.closed:
                client.proc.stdout.close()
            if client.proc.stderr and not client.proc.stderr.closed:
                client.proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
