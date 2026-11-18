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

保留用户的既有文件与工作区基线。开发范围和实施阶段见[路线图](docs/ROADMAP.md)，原始需求见 [CLI 实现规范](docs/archive/IMPLEMENTATION_SPEC.md)。提交、发布和外部账户操作由维护者授权。

20 题评测和独立评分见[评测手册](evals/README.md)。`run --provider fake` 运行参考动作并检验执行链路。`score REPORT` 执行独立检查，`replay` 读取历史事件。评分规则变更需附复现和回归，保留原始结果与修订原因。

改动 Web 模块时，运行 `uv sync --locked --extra web`，按[验证说明](docs/VALIDATION.md)准备测试数据库和浏览器夹具。数据库 schema 变更使用 Alembic revision，连接、迁移和恢复流程见[数据库文档](docs/DATABASE.md)。

当前不配置 GitHub Actions，请手动运行上述检查；本机通过不能代替其他平台验证。故障夹具evals/fixtures与生成运行目录有意排除Ruff，避免格式化改变固定坏实现；src与tests仍全量检查。

分发时运行 `uv build --offline` 并检查 sdist 与直接 wheel。显式清单保留源码、测试、文档、示例、评测夹具和 `.env.example`，排除真实环境文件、IDE/工具私有目录、数据库/私钥和本机验证结果。构建成功本身不能证明内容安全，`tests/integration/test_packaging.py` 使用合成敏感标记检查两种构建路径。

规则修改后用 `python -m stellarweaver.evals score REPORT --output NEW_RECEIPT_JSON` 保存独立收据及来源包；不覆盖旧报告。`--catalog` 只接受自己信任的规则，oracle 在 Host 执行。来源校验只读取归档 Python 字节，不自动运行旧源码。
