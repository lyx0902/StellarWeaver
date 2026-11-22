# 统一评测协议

本页定义 M1 的 SWE-bench Lite 结果契约。原有数据准备、Agent 执行、SQLite Journal 和独立官方评分入口继续使用。标准化报告从既有制品读取，无需重新请求模型或启动评分容器。独立 SWE-bench Lite 批量与试跑配置固定为 100,000 token 窗口，交互式 CLI 与 Web 默认采用 200,000 token。窗口变化的评测对照需在实验设置中单独登记。 [固定对照设置](EVAL_PLAN.md#对照设计)

## 离线命令

```powershell
uv run --locked --offline python -m stellarweaver.evals.swebench_contract report .validation/swebench-phase2-official-sample
uv run --locked --offline python -m stellarweaver.evals.swebench_contract report .validation/swebench-phase2-official-sample --output .validation/sample-contract.json
uv run --locked --offline python -m stellarweaver.evals.swebench_contract compare .validation/BASELINE .validation/VARIANT --vary source_sha256
```

报告默认写到标准输出。`--output` 使用一个位于源实验目录之外的新文件，已有目标会拒绝覆盖。先停止其他进程对同一实验写入，再生成报告。读取过程会核对文件哈希和已发现的尝试、评分目录，变化时拒绝输出。标准化 JSON 使用稳定键序且无生成时间，同一组输入字节可复现同一个输出哈希。

## 数据与身份

| 记录 | 绑定信息 |
| --- | --- |
| Manifest | 契约版本、suite、数据集修订和校验标识、split、有序题目、Harness 提交及 Python 源码文件哈希、预算、评分版本、配置哈希。历史记录沿用其原有的源码哈希范围。实验 ID 由原始 manifest 哈希导出，字节相同的副本具有同一实验 ID。 |
| Task / Attempt | 每道题保留 Git 基线和历次尝试。新尝试各自保存 `attempt.json`，`solve.json` 指向当前尝试。记录运行态、候选态、补丁哈希、实际记录到的模型设置、工具 schema、策略和配置哈希、调用次数、耗时及 token 计量来源。 |
| Score | 评分 ID、题号与尝试关联、补丁哈希、评分器版本、镜像摘要、评分输入哈希和结论。每次独立评分产生新的评分目录，`evaluation.json` 指向当前评分。新评分收据还保存原始报告哈希。 |
| Evidence | 源实验内的相对路径、SHA-256 和字节数，关联冻结输入、尝试收据、SQLite Journal、候选、评分报告或日志。报告仅索引公开题面和评分制品的路径和哈希。 |

原始论文或官方评分资料中用于独立验证的隐藏测试、参考补丁与评分脚本仍留在原评分制品中。标准报告不复制这些字段。仓库源码、工具输出和导出的实验记录均可能含项目特有信息，公开分享前按实际证据范围审阅。

已完成的缓存先验证冻结输入，再核对候选哈希、尝试身份、评分器版本和镜像。正常复用保留原评分 ID。`evaluate --rescore` 走既有官方评分器并生成新 ID；已有历史实验应以只读报告为入口。更换候选使用独立实验，新旧补丁分别关联各自的评分。旧版本已经冻结的执行指纹仍由批量入口校验。

老制品可能缺少实际模型修订、配置/权限哈希、带原因的尝试收据或缓存用量。此时相关字段为空，旧评分可通过唯一匹配的补丁哈希关联尝试。同一哈希匹配多次旧尝试时，该评分不用于当前尝试的通过率。FakeModel 用保存脚本的哈希标识轨迹配置。

## 分母、计量与配对

- 计划分母等于 manifest 中注册的独立题目数。每道题只计入一次，旧尝试和重评分仍留在明细。
- `by_run_status`、`by_candidate_status`、`by_evaluation_status` 分别计数。未开始、未产补丁、空补丁、预算用尽、环境失败和未评分保留自己的状态。`resolved_rate` 用当前可关联官方评分的通过数除以计划题数。`graded` 只计官方已判定的通过和未通过题。
- `usage_source` 区分服务端返回、估算、不完整与不可用。缺失的缓存 token 保留为空。当前格式保留 `security_assessment=not_evaluated`，为独立安全评测留出字段。
- 批量入口将无人值守策略及其版本写入 `run-settings.json`。策略拦截和环境准备失败分别记录在运行归因中，评分失败仍属于独立评分状态。对照组应采用同一硬边界与冻结策略。 [策略分流](SECURITY.md#运行模式与分流)
- `compare` 按题号配对，先核对数据、split、基线、冻结输入、预算和策略等控制条件，再核对模型身份、隔离环境和评分器。只有双方同时有可关联且可比较的官方判定才进入四格配对计数；未配对题号和排除原因另列。
- `--vary` 仅用于显式注册源码版本、配置哈希、提示哈希或工具 schema 哈希的变化。`source_sha256` 覆盖源码文件哈希与 Harness 提交。策略哈希、可见测试环境、模型身份和官方评分绑定仍须满足比较条件。缺少可信模型修订的历史 DeepSeek 实验可独立报告。

对照结果用于检查已登记任务中的差异。正式测试的预算、模型身份、数据冻结与统计检验按[实验计划](EVAL_PLAN.md)另行登记。

本地固定夹具独立导出边界、正常任务和工具效率统计，方法见[本地工具边界评测](SECURITY.md#本地工具边界评测)及[修复过程夹具](EVAL.md#修复过程的离线夹具)。
