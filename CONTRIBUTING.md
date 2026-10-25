# 本地开发

使用 Python 3.12+ 和 uv，先运行 `uv sync --locked`。

提交前自行执行：

```text
uv run ruff check .
uv run ruff format --check .
uv run mypy src/stellarweaver
uv run pytest -q
uv run stellarweaver doctor --offline
```

默认测试必须离线，文件和命令行为用真实临时仓库验证。真实 API 测试应显式选择，缺 Key 时跳过并说明。不要将模型“完成”声明替代工具证据。修改领域或工具协议时检查主循环与 provider 的整轮消息契约。

本项目不自动提交、发布或连接外部账户。保存既有用户文件与工作区基线，遵守 docs/IMPLEMENTATION_SPEC.md 的阶段范围。

20题与独立评分说明见 evals/README.md。`uv run python -m stellarweaver.evals run --provider fake`运行真实工具参考脚本，不能称模型能力测量。`score REPORT`会执行独立检查；`replay`完全不执行历史工具。修改评分规则需有复现与回归，保留已有原始结果和规则修订原因。

当前不配置 GitHub Actions，请手动运行上述检查；本机通过不能代替其他平台验证。故障夹具evals/fixtures与生成运行目录有意排除Ruff，避免格式化改变固定坏实现；src与tests仍全量检查。

分发时运行 `uv build --offline` 并检查 sdist 与直接 wheel。显式清单保留源码、测试、文档、示例、评测夹具和 `.env.example`，排除真实环境文件、IDE/工具私有目录、数据库/私钥和本机验证结果。构建成功本身不能证明内容安全，`tests/integration/test_packaging.py` 使用合成敏感标记检查两种构建路径。

规则修改后用 `python -m stellarweaver.evals score REPORT --output NEW_RECEIPT_JSON` 保存独立收据及来源包；不覆盖旧报告。`--catalog` 只接受自己信任的规则，oracle 在 Host 执行。来源校验只读取归档 Python 字节，不自动运行旧源码。
