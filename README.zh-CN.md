# Codex Bridge

[English](README.md) | **简体中文**

Antigravity 把代码实现和测试直接派给本机的 OpenAI Codex CLI。Codex 在指定工作目录执行，结果回到同一个 Antigravity 对话。

## Orchestrator 自动派工

在 Antigravity 里选 **Orchestrator**，直接描述要改的代码。主代理负责拆任务、确定验收套件，并在同一对话直接调用 `codex_run`，传入 `model: "gpt-6.2-sol"` 和 `async_mode: true`。Worker 先在原实现上用针对性测试跑红，再做最小修改，跑绿新增用例和受影响的回归。主代理调用 `codex_review`，用新的只读 Codex 会话审查本次 diff。

宿主对最终代码独立跑一次约定的验收套件，包含需要宿主环境且已获授权的 socket、GPU 或联网检查。完整套件包含专项用例时只跑完整套件。只有诊断失败或代码变化后才重跑相关检查。分别汇报 Worker 测试、宿主验收和审查结论。

后台任务派发一次后沿用 `task_id`。客户端能通知该任务完成时等通知。否则调用 `codex_poll`，省略 `wait_seconds` 默认最多等 300 秒，较长任务可设 600 秒。仍可短等或用 0 立即查状态。任务提前结束时立即返回。收到 `running` 就继续用同一个 ID 等待。

完成回执包含摘要、变更文件总数、最多 20 个路径的预览、简短的 diff 统计和 Worker 的验证命令与结果。回执还记录失败或未验证项、耗时和 CLI 提供的 token 用量。历史脏文件只显示数量。按需从 `details_path` 指向的本地 JSON 查看完整清单、验证详情和 Codex 日志路径。

例如，直接说「修复零时长边界并运行定向测试」即可。你也可以显式指定其他 worker 模型或执行方式。在其他 Antigravity 模式里，可以说「把这个任务交给 Codex」。插件还支持只读规划、代码审查和用 `session_id` 续跑。

## 安装

从运行 Antigravity 的同一环境安装这个 fork。插件需要 Python 3.9+ 和可用的 Codex CLI。请在 worker 所在环境登录 Codex，或配置它支持的认证方式。[Antigravity 插件安装](https://antigravity.google/docs/plugins#manual-plugin-installation) · [Codex 认证](https://learn.chatgpt.com/docs/auth)

```sh
agy plugin install https://github.com/whosedream/codex-bridge
```

手动安装时，把这个 fork 克隆到相应的插件目录。

| 运行环境 | 插件目录 | 工作区路径示例 |
| --- | --- | --- |
| Windows | `%USERPROFILE%\.gemini\config\plugins\codex-bridge` | `D:/projects/example` |
| WSL | 发行版内的 `~/.gemini/config/plugins/codex-bridge` | `/home/YOUR_NAME/projects/example` |

### Windows worker

1. 在 PowerShell 确认 Windows Python 和 Codex 可启动，再检查 `codex login status`。
2. 如果 `python3` 不可用，在**已安装副本**的 `mcp_config.json` 里把 `mcpServers.codex.command` 改成 Windows `python.exe` 的完整路径。保留原来的参数和工具设置。
3. 如果应用找不到可用的 Codex 可执行文件，在该服务的 `env` 里设置 `CODEX_BIN`。使用 Codex Desktop 时，也可以把 `CODEX_BRIDGE_DESKTOP_BIN` 指向桌面版的 `bin` 根目录。桥接会从版本子目录选择最新可用的二进制。显式指定的 `CODEX_BIN` 优先。
4. 如果换了 Python，把已安装的 `hooks.json` 里 `codex-orchestrator` 的命令也改用同一个解释器，保留 `scripts/orchestrator_hook.py` 参数。

下面是服务配置片段。请按本机实际路径替换示例值。

```json
{
  "command": "C:/Tools/Python312/python.exe",
  "env": {
    "CODEX_BRIDGE_DESKTOP_BIN": "C:/Users/YOUR_NAME/AppData/Local/OpenAI/Codex/bin",
    "PYTHONUTF8": "1"
  }
}
```

### WSL 原生 worker

1. 在运行 Antigravity 的 WSL 发行版里准备 Linux Python 和 Codex，并检查 `codex login status`。需要登录时，在 WSL 内完成。
2. 为 Codex 的 Linux 沙盒准备 `bubblewrap`。Ubuntu 的包名是 `bubblewrap`。在 worker 使用的 `PATH` 上确认 `bwrap --version` 可执行。[Codex 的 WSL 说明](https://learn.chatgpt.com/docs/windows/wsl)
3. 在 WSL 内安装插件，沿用 Linux `python3`。如果应用找不到 Codex 或 `bwrap`，在已安装副本中用 `CODEX_BIN` 指定 Linux 可执行文件，并在 `mcpServers.codex.env.PATH` 补齐相应目录。
4. 打开 Linux 工作区，例如 `/home/YOUR_NAME/projects/example`，然后选择 Orchestrator。

### 同一个 WSL Antigravity 窗口同时接两种 worker

在 Windows 和 WSL 各安装一份插件，只编辑 WSL 侧已安装的 `mcp_config.json`。

1. 将 Linux 服务命名为 `codex-wsl`，保留 Linux Python、WSL 里的 `CODEX_BIN`、Linux 任务数据目录和已有工具设置。
2. 添加一个供 Windows worker 使用的 `codex` 服务。启动命令用 Windows Python 的 WSL 路径，例如 `/mnt/c/Tools/Python312/python.exe`。脚本参数和独立的 `--data-dir` 使用 Windows 路径。
3. 按需向已有的 `WSLENV` 追加 `CODEX_BIN/w` 或 `CODEX_BRIDGE_DESKTOP_BIN/w`，如设置了 `PYTHONUTF8`，再追加 `PYTHONUTF8/w`。保留原有条目。[Windows 和 WSL 互操作](https://learn.microsoft.com/en-us/windows/wsl/filesystems#share-environment-variables-between-windows-and-wsl-with-wslenv)
4. 在 Antigravity 的 **Customizations → Installed → Codex Bridge → MCP** 重启受影响的服务。桥接规则按路径派工，Linux 绝对路径走 `codex-wsl`，Windows 盘符或 UNC 路径走 `codex`。就绪检查、轮询和续跑都走同一个服务。

## 验证一次派工

选一个临时仓库，让 Orchestrator 修个小问题并跑测试。对话轨迹中应看到 `codex_run`、轮询和独立的 `codex_review`。核对实现回执里的红测和绿测、不同的审查会话 ID，以及审查覆盖的改动范围。

机器专用配置、API 密钥、`auth.json` 等登录缓存，以及任务和会话日志都留在本机。日常使用保留默认沙盒，`CODEX_BRIDGE_ALLOW_DANGER_SANDBOX` 留空即可。

## 开源协议

基于 [ballShell/codex-bridge](https://github.com/ballShell/codex-bridge)，采用 [MIT License](LICENSE)。
