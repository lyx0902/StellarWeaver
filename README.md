# StellarWeaver

一个用 Python 实现的命令行 Coding Agent：读取代码、调用工具、修改文件、运行测试，再根据真实反馈继续修复。核心是自行实现的单 Agent 循环，提供交互式 `chat` 和非交互 `exec`。

## 已实现

- **模型与工具循环**：支持 DeepSeek / OpenAI 兼容的 Chat Completions 和流式输出，完整组装工具参数后再执行；提供可脚本化 FakeModel，支持离线演示与测试。
- **八个内置工具**：文件枚举、读取、搜索、新建、精确编辑、命令执行，以及 Git 状态和差异查询。
- **授权与执行控制**：只读、询问、工作区编辑三种策略；严格参数校验、SHA-256 冲突检查、请求/工具/时长预算，以及超时和取消后的受管进程清理。无 TTY 且授权不足时直接停止。
- **持久化与恢复**：SQLite 保存消息、授权、工具结果和制品；支持会话列表、恢复、只读回放与导出。结果未知的命令不会自动重跑。
- **上下文与改动管理**：默认 100k 窗口配置、保留完整工具回合的历史压缩、分目录 `AGENTS.md` 和 Python repo map；`/diff` 区分原有与新增变化，`/undo` 只回退可证明匹配的内置编辑。
- **Skills 与 MCP**：技能按需发现和加载；通过官方 MCP SDK 连接本地 stdio 服务器，统一进行授权、参数校验、调用和关闭。

## 验证与边界

默认测试无需外网或付费 Key，使用真实临时 Git 仓库、文件和进程验证编辑、失败后修复、恢复与冲突处理。另有 20 个固定开发任务及独立评分入口，覆盖 Bug、小功能、跨文件修改、测试补充和代码理解。

DeepSeek `deepseek-flash` 已完成一次 20 题实测：初评 19/20，修正一项评分范围后，零新增模型调用重评分为 20/20。它只代表该小型任务集的一次结果；FakeModel 仅验证执行基础设施。详见[评测结果](evals/RESULTS.md)和[公开验证摘要](docs/validation/README.md)。

目前主要在 Windows / Python 3.12.6 验证，其他平台实跑、远端 CI 和重复/消融评测尚未完成。命令和 MCP 使用宿主机权限，不提供 OS 文件系统沙箱；100k 是客户端配置，当前采用保守字节估算。

## 安装与配置

需要 **Python 3.12+、uv 和当前终端 PATH 可见的 Git**；`rg` 可选，缺失时使用 Python 搜索回退。`--offline` 不会附带 Git，也要求 Python 依赖已经缓存。

默认 provider 为 `fake`。使用 DeepSeek 时，参照 [.env.example](.env.example) 在仓库根目录创建 `.env`，填写 `STELLARWEAVER_API_KEY`；默认模型为 `deepseek-flash`。模型、预算和策略配置参见 [stellarweaver.example.toml](stellarweaver.example.toml)，本机配置可保存为 `stellarweaver.toml`。

`.env`、本机配置、IDE 文件、会话数据库和原始运行产物已列入 [.gitignore](.gitignore)；提交源码时保留模板与 `uv.lock`。

架构与完整用法：[架构](docs/ARCHITECTURE.md) · [改动与恢复](docs/CHANGES.md) · [评测说明](evals/README.md) · [开发指南](CONTRIBUTING.md) · [项目进度](docs/PROGRESS.md)。

## 命令示例

安装与离线检查：

```powershell
uv sync --locked
git --version
uv run stellarweaver doctor --offline
```

离线演示真实“测试失败 → 修复 → 通过”，以及 Skills / 本地 MCP：

```powershell
uv run --offline python -m stellarweaver.demo
uv run --offline python -m stellarweaver.extensions.demo
```

配置 Key 后进入交互式编码，可使用 `/status`、`/diff`、`/undo`、`/compact` 等命令：

```powershell
uv run stellarweaver chat --workspace . --provider deepseek --env-file .env --stream
```

只读分析项目，输出 JSONL 事件：

```powershell
uv run stellarweaver exec --workspace . --provider deepseek --env-file .env --policy read-only --json --prompt "梳理项目入口、模块职责和核心调用链，给出文件路径与行号。"
```

查看、回放或继续会话，将 `SESSION_ID` 替换为实际 ID：

```powershell
uv run stellarweaver sessions list
uv run stellarweaver replay SESSION_ID --json
uv run stellarweaver resume SESSION_ID --provider deepseek --env-file .env --prompt "重新读取当前状态，继续验证。"
```

运行离线测试与 20 题参考动作评测：

```powershell
uv run pytest -q
uv run python -m stellarweaver.evals run --provider fake
```
