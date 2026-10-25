# 公开验证摘要

StellarWeaver 0.1.0，SQLite 会话 schema 2。以下是 2026-10-25 在 Windows 11 / Python 3.12.6 上已完成检查的摘要；它区分不同补丁与历史评测，不代表跨平台验证。

| 范围 | 已记录结果 |
| --- | --- |
| 验收缺口修复后的全量离线检查 | **471 passed、4 skipped**；跳过为符号链接权限 3 项、POSIX 权限语义 1 项 |
| 全量检查时的静态与分发验证 | Ruff、格式检查、mypy、wheel/sdist 构建及隔离加载演示通过 |
| 后续 demo 启动补丁的专项检查 | **21 passed**，覆盖缺 Git 的明确提示及相关 CLI、主循环、打包回归；没有将它记为重新执行全量测试 |
| 普通终端修复演示 | 真实临时 Git 仓库中 unittest 退出码 **1 → 0**，演示通过，无意外工具错误 |
| 历史真实模型评测 | DeepSeek `deepseek-flash`，固定 20 个小任务各运行 1 次；Agent 均 completed，初始独立评分 **19/20** |
| 历史规则修订与重评分 | 修正 Q01 多余读取要求后 **20/20**；当前评分器加精确重建的旧规则复现 **19/20**，修正规则复现 **20/20**；重评分均为 **0 次新增模型调用** |

471/4 的全量结果发生在后续 demo 启动补丁之前。此次补丁只记录 21 项专项检查，不把历史全量成绩当作补丁后的新测量。真实模型成绩也是历史单次测量，不作为当前代码再次调用模型后的成绩；统计口径与限制见[评测结果](../../evals/RESULTS.md)。

原始 JSON、命令日志、会话工作区及评分来源包保留在开发者本地。它们包含本机路径与运行状态，不随 GitHub 源码公开；本目录只公开这份摘要。公开摘要不包含完整工具证据和候选工作区，**不能作为 `score` 的输入，也不能独立复核或重评分历史运行**。公开规则及夹具用于建立新的可复现运行，见[评测说明](../../evals/README.md)和[历史规则档案](../../evals/provenance/README.md)。

从源码建立环境后，可执行：

```powershell
uv sync --locked
uv run pytest -q -ra
uv run ruff check .
uv run ruff format --check .
uv run mypy src/stellarweaver
uv run stellarweaver doctor --offline
uv run python -m stellarweaver.demo
uv run python -m stellarweaver.evals run --provider fake
```

这些入口不请求模型 API；首次安装依赖可能需要联网。演示和评测需要 Git 位于 PATH；离线参考动作验证真实文件、工具和评分链路，不衡量模型自主编码能力。命令会建立新的临时工作区和报告，不复用历史成绩。针对真实 provider 的运行需另行配置密钥并显式选择，方法见评测说明。

Linux/macOS 实机、远端 CI、重复真实评测及组件消融尚未完成。Windows Job Object 的受管进程测试不等于操作系统文件权限隔离；当前 Host 后端仍使用本机用户权限。当前范围见[V1 验收核对](../V1_ACCEPTANCE.md)。
