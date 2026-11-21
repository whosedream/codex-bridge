# Codex Bridge

**English** | [简体中文](README.zh-CN.md)

Antigravity delegates implementation and tests directly to the local OpenAI Codex CLI. Codex works in the selected directory and returns results to the same Antigravity conversation.

## Automatic delegation in Orchestrator

Select **Orchestrator** in Antigravity and describe the implementation and tests you want. The planner defines the work and acceptance criteria. In the same conversation, the execution phase calls `codex_run` with `model: "gpt-6.2-sol"` and `async_mode: true`, then uses `codex_poll` to collect the result. Antigravity reviews the changes and test output. The bridge uses direct MCP calls for dispatch and polling. No Gemini subagent is started.

A prompt such as “Fix the zero-duration edge case and run the focused tests” is enough. An explicit worker model or routing request overrides the default. In other Antigravity modes, ask it to hand the task to Codex. You can also request read-only planning or code review, and resume a task with its Codex `session_id`.

## Install

Install the fork from the shell that hosts Antigravity. The plugin needs Python 3.9+ and a working Codex CLI. Sign in to Codex in the environment where the worker runs, or configure a supported Codex authentication method. [Antigravity plugin installation](https://antigravity.google/docs/plugins#manual-plugin-installation) · [Codex authentication](https://learn.chatgpt.com/docs/auth)

```sh
agy plugin install https://github.com/whosedream/codex-bridge
```

For a manual install, place a clone of this fork in the plugin directory for the corresponding host.

| Host | Plugin directory | Workspace path |
| --- | --- | --- |
| Windows | `%USERPROFILE%\.gemini\config\plugins\codex-bridge` | `D:/projects/example` |
| WSL | `~/.gemini/config/plugins/codex-bridge` inside the distribution | `/home/YOUR_NAME/projects/example` |

### Windows worker

1. Verify that Windows Python and Codex start, and run `codex login status` in PowerShell.
2. In the installed copy of `mcp_config.json`, set `mcpServers.codex.command` to the full path to Windows `python.exe` when `python3` is unavailable. Keep the bundled arguments and tool settings.
3. Set `CODEX_BIN` in that server's `env` if the app cannot find the working Codex executable. For Codex Desktop, you can instead set `CODEX_BRIDGE_DESKTOP_BIN` to its versioned `bin` root. The bridge selects the latest available binary under that root. An explicit `CODEX_BIN` takes precedence.
4. If you changed the interpreter, point the installed `hooks.json` command for `codex-orchestrator` at the same Python executable and keep `scripts/orchestrator_hook.py` as its argument.

These are example fields for the installed server entry. Replace the example paths with your own.

```json
{
  "command": "C:/Tools/Python312/python.exe",
  "env": {
    "CODEX_BRIDGE_DESKTOP_BIN": "C:/Users/YOUR_NAME/AppData/Local/OpenAI/Codex/bin",
    "PYTHONUTF8": "1"
  }
}
```

### Native WSL worker

1. In the distribution running Antigravity, install Linux Python and Codex and verify `codex login status`. Sign in inside WSL if needed.
2. Install `bubblewrap` for the Codex Linux sandbox. On Ubuntu, the package is named `bubblewrap`. Verify `bwrap --version` on the worker's `PATH`. [Codex on WSL](https://learn.chatgpt.com/docs/windows/wsl)
3. Install the plugin inside WSL and keep its Linux `python3` command. If Codex or `bwrap` is outside the app's `PATH`, set `CODEX_BIN` to the Linux executable and add the necessary directories to `mcpServers.codex.env.PATH` in the installed config.
4. Open a Linux workspace such as `/home/YOUR_NAME/projects/example` and select Orchestrator.

### Both workers in a single WSL Antigravity window

Install the plugin in both environments, then edit only the installed WSL `mcp_config.json`.

1. Name the Linux server entry `codex-wsl`. Keep Linux Python, the WSL `CODEX_BIN`, a Linux task data directory, and the bundled tool settings.
2. Add a separate `codex` entry for Windows. Use a WSL interop path to Windows Python, such as `/mnt/c/Tools/Python312/python.exe`. Pass Windows paths to the Windows copy of `codex_mcp_server.py` and its separate `--data-dir`.
3. Pass the Windows Codex binary setting through `WSLENV`. Add `CODEX_BIN/w` or `CODEX_BRIDGE_DESKTOP_BIN/w` to the existing entries, plus `PYTHONUTF8/w` if set. Preserve other `WSLENV` entries. [Windows and WSL interoperability](https://learn.microsoft.com/en-us/windows/wsl/filesystems#share-environment-variables-between-windows-and-wsl-with-wslenv)
4. In Antigravity's **Customizations → Installed → Codex Bridge → MCP**, restart the affected servers. The bridge rules route Linux absolute paths to `codex-wsl` and drive-letter or UNC paths to `codex`. Status, polling, and session follow-ups stay on the selected server.

## Verify a run

Ask Orchestrator to fix a small issue and run its tests in a scratch repository. The conversation trace should show `codex_run` followed by `codex_poll`. Check the changed files and actual test output before accepting the result.

Keep machine-specific settings, API keys, Codex login caches such as `auth.json`, and session or task logs in your local installation. Leave `CODEX_BRIDGE_ALLOW_DANGER_SANDBOX` unset for the usual sandboxed setup.

## License

Based on [ballShell/codex-bridge](https://github.com/ballShell/codex-bridge). Licensed under the [MIT License](LICENSE).

---

<details>
<summary>Workspace Metadata (AgentsWorkSpace)</summary>

- **所属 Agent**：Antigravity (Gemini)
- **目录性质**：持久化（长期保留）
- **创建日期**：2026-11-21
- **任务简述**：将 OpenAI Codex CLI 封装为 Antigravity 原生插件（`codex-bridge`），实现前店后厂双模型协同。

</details>
