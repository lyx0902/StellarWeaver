# StellarWeaver 开发进度

本页按阶段保留当时的实现与验收结果。原始报告、日志和来源包仅作本地归档，不随 GitHub 源码公开；可公开的范围与复现入口见[验证摘要](validation/README.md)。下方代码路径是归档说明，不是下载链接。

## 2026-10-25：GitHub源码展示与忽略规则整理

根README按当前实现重写为74行，集中说明单Agent工具循环、授权/预算、会话恢复、上下文、Skills/MCP及评测边界，命令示例放在末尾，移除本机用户名和项目绝对路径。补齐IDE、本机配置、数据库、日志、构建和覆盖率产物的忽略规则；原始验证数据继续留本地，仅公开本目录的验证摘要，相关历史链接改为明确的本地归档说明。

真实临时Git规则检查覆盖33个应忽略样例、16个应保留样例，源码/测试/fixtures/模板/uv.lock/CI均保留；拟上传文件未发现当前已配置API Key原文。检查34个公开Markdown本地链接，无缺失或指向忽略文件的链接；CLI帮助、offline doctor、Ruff与格式检查通过。本轮只调整文档和.gitignore，未改运行代码、.env、原规范或原始报告，未初始化项目仓库、提交或推送。

## 2026-10-25：普通PowerShell缺Git PATH的演示启动修复

用户在普通PowerShell运行 `uv run --offline python -m stellarweaver.demo`，在 `subprocess.run(["git", "init", ...])` 得到WinError 2，Agent尚未启动。前次检查依赖Codex自动加入PATH的Git，未覆盖普通终端缺依赖场景。实际另有Git安装在 `D:\Git\cmd\git.exe`，已验证版本2.55.0.windows.2；不需要重新安装或调整API Key。

- 两个demo入口先检查当前PATH中的Git，缺失则stderr解释Git/PATH与offline关系、退出2，在创建临时工作区前结束。
- 初始化使用已解析的同一Git，并捕获检查后程序消失/不可启动或git init失败，返回明确提示。程序不猜测Codex私有缓存位置、不修改用户永久PATH。
- 演示新增demo_passed与unexpected_tool_failures，Git或其他预期成功的工具发生错误时不能仅因测试[1,0]和模型停止就冒充演示成功。
- README给出按实际Git安装位置临时设置PowerShell PATH的命令。使用仅包含 `D:\Git\cmd` 和Windows系统目录、没有Codex Git/rg的PATH真实运行原uv命令：demo_passed=true、unittest退出[1,0]、无意外工具错误，见实际输出（本地归档 `docs/validation/demo_git_path_actual.txt`）。缺rg时使用已有Python搜索回退。

以下验收修复记录保留当时版本和全量测试结果；本次启动补丁的专项检查单独记录于检查记录（本地归档 `docs/validation/demo_git_path_checks.json`）。未修改.env、原规范或uv.lock，未调用模型API、提交或发布。

## 2026-10-25：验收缺口与交付证据修复

本段与[V1逐项核对](V1_ACCEPTANCE.md)为当前状态，下方阶段记录保留当时结果。用户要求把前次代码缺口和交付改进落地；未修改原规范/.env，未提交、发布或新增模型请求。默认100k窗口及1,000,000累计输入预算保留。

本轮修复：

- Agent任务deadline涵盖resume协调、Git基线、扩展启动、模型/工具和扩展退出。先保存任务及run.started，完成清理再唯一保存outcome。chat会话初始化单独限时，每个任务重新计时，不扣用户输入等待。同步SQLite在返回后检查；CLI配置、SessionStore打开及provider最终关闭不属于Agent计时。
- 清理有15秒协作式grace；吞取消后正常返回但已超过grace也会报告cleanup_failed，不写completed。它仍需等待组件收尾，不是整个CLI的OS硬超时保证。
- diff核对启动基线、当前Git与内置修改路径并集，覆盖untracked消失、脏文件还原Git clean、原删除路径恢复、rename两端和类型变化；恢复用户基线不误报，观察不修改工作区/index。
- registry新增独立after_authorize钩子，授权后再次比对模型已见AGENTS路径/hash。根/子目录新增、修改、删除均在tool.started前停止，create/edit/command无副作用；既有before_execute不重复调用，文件hash/权限仍随后复检。
- Windows run_command先挂起创建、加入不可breakaway且kill-on-close的Job，再恢复线程；父先退出、DETACHED_PROCESS、新process group、breakaway、创建期取消、双取消、启动失败及清理未确认均有真实PID/故障回归。不再以taskkill尝试成功替代进程归零证明；无法证实则unknown。
- 搜索和内部Git完整保留unknown/取消/隔离失败；基线与diff遇到此类结果先记录changes.observation_unknown再停止，不能折成成功或Git不可用。正常无匹配、无filter及无Git降级仍分别验证。
- sdist改显式清单，wheel加排除：保留源码/测试/示例/fixture/文档/模板，排除IDE、真实环境文件、数据库、私钥及本机验证结果。合成敏感标记同时测试sdist构建链和直接wheel，检查实际制品与安装资源。
- 新评测/重评分保存原报告hash、实际catalog hash、评分版本及按内容hash归档的源码快照；拒覆盖，不导入归档Python。实际task从同一catalog原始字节解析，身份/归档间漂移拒绝，不把旧任务标成新规则。
- 原19/20报告与旧20/20回执字节保留。精确重建旧catalog，hash与原报告一致；用当前评分器分别复现旧19/20和新20/20，新增模型调用0。新v2收据明确保存本次评分实现，不能声称找回当时未归档的旧Python。

第一轮全量检查429 passed、4 skipped（210.71秒），随后继续修复交叉审阅发现的清理grace漏报、规则捕获竞态和内部搜索/Git状态传播，最终检查另列。4个skip仅为当前账户无符号链接权限3项、POSIX权限语义1项；原Windows父先退出场景已实际执行通过。独立读改测演示实际获得unittest退出码[1,0]，最终verification=passed；Skills/MCP演示completed，连接/关闭各1次。

### 本轮最终检查

| 实际执行 | 结果 |
| --- | --- |
| uv sync --locked --offline | 58包解析，editable本地重建成功，uv.lock不变 |
| Ruff check / format --check | 全通过，94文件符合格式；来源归档作为数据排除 |
| mypy src/stellarweaver | 43源码文件无错误 |
| mypy --platform linux（commands/Job两文件） | 静态模拟通过，不等于Linux实跑 |
| pytest -q -ra | **471 passed、4 skipped，206.27秒**；包含完整离线20题与独立重评分 |
| --help / version / doctor --offline | 全退出0；0.1.0/schema2；明确未测试模型连接 |
| 真实文件失败后修复演示 | completed，unittest退出码1→0，Job清理确认 |
| Skills/MCP本地演示 | completed，3请求/2工具，server连接及关闭各1 |
| 历史规则重评分 | 旧19/20、新20/20，均0新增模型调用；v2来源核验通过 |
| 保护文件hash | .env、uv.lock、原规范两份、原报告及旧回执6项未变 |

完整pytest输出（本地归档 `docs/validation/audit_fixes_final_pytest.txt`）、综合检查及构建/安装记录（本地归档 `docs/validation/audit_fixes_checks.json`）、分发文件与密钥字节扫描（本地归档 `docs/validation/audit_fixes_distribution.json`）、保护文件校验（本地归档 `docs/validation/audit_fixes_protected.json`）。新旧规则收据（本地归档 `docs/validation/audit_fixes_old_rules_v2.json`）和当前规则收据（本地归档 `docs/validation/audit_fixes_current_rules_v2.json`）附相邻.provenance来源包。分发产物位于`.validation/audit_fixes/dist-final`，历史阶段构建目录保留，不作为本轮交付。

### 剩余与下一步

本机Windows11/Python3.12.6的证据与其他平台分开。Linux/macOS运行、远端CI、重复真实20题、bash-only比较及repo-map/压缩消融尚未运行；精确tokenizer、DockerBackend和恶意候选代码的OS隔离尚未实现。Job只约束CreateProcess继承树，WMI/服务/计划任务等broker代启不在范围；POSIX进程组不保证回收主动setsid逃组后代。文件hash/指令复核仍不能排除最终检查后的任意外部编辑器竞争。

下一步先执行已配置的跨平台CI矩阵、补齐本账户无法测试的符号链接与POSIX语义，再在固定配置下做重复实测和消融；有比较数据后再判断是否需要更改上下文策略。不把当前单次20题或100k客户端参数解释为通用成功率或服务端窗口上限验证。

## 2026-10-25：默认上下文调整为100k

用户在LinkRadar跨仓库只读分析遇到32,768窗口的输入上限26,624，要求先扩大到100k。本轮仅调整客户端默认窗口和配套累计输入预算，没有修改LinkRadar文件、.env、原始规范或历史会话/评测报告，也未调用真实模型。

- ModelConfig与ModelCapabilities/FakeModel默认上下文100,000；DeepSeek适配器直接从当前配置报告该能力，模板同步。外部workspace没有stellarweaver.toml时立即使用新默认；显式workspace/--config覆盖仍保留。
- 默认输出预留4,096、安全余量2,048，输入可用93,856；估算仍为保守UTF-8 JSON字节，未替换为精确tokenizer，也不宣称已验证服务端最大窗口。
- 单任务累计输入预算200,000→1,000,000。用户给出的旧run已累计162,505输入token，扩大单次窗口时同步放宽该累计上限，避免立刻转为另一预算阻断。模型/工具次数、时间、输出与只读授权等其他限制保留。
- 当前venv为editable安装，跨工作区调用现有stellarweaver.exe的新进程即可生效，无需重装。仅修改安装目录模板不会被外部workspace自动加载，故同时修改代码默认值。
- 对用户保存的LinkRadar历史仅做内存投影验证：在100k窗口下估算输入51,148，低于93,856，保留压缩机制；没有resume执行或重写原session。
- 新增回归覆盖无HTTP的DeepSeek能力/显式旧配置覆盖、大段源码在100k下保留尾部证据且不重复读取、外部目录真实CLI加载新默认。旧M3压力测试明确固定32,768窗口，保留所有原有摘要证据强断言，避免默认变大后失去压力覆盖。
- 检查结果：381 passed, 5 skipped in 145.38s (0:02:25)；Ruff、格式检查和mypy均通过，三个100k专项与原config/context测试14项通过，见检查记录（本地归档 `docs/validation/context_100k_checks.json`）与完整测试输出（本地归档 `docs/validation/context_100k_tests.txt`）。

前次只读验收审计发现的CLI预飞行计时、diff基线删除漏报、授权后AGENTS变化检查、Windows脱离进程清理和sdist排除规则仍待后续处理；本轮参数调整不代表这些缺口已修复。历史M2/M5数据保留当时设置与原始结果，不以新默认重写旧成绩。


## 2026-10-25：M3–M5 已实施并验收

用户在M3进行中明确授权继续M4/M5，按原规范扩展，无提交、推送、发布或账户连接。以下先列当前状态；下方M0/M1/M2记录作为历史阶段证据保留，旧段落中的“尚未实施”不是当前状态。

### M3：安全恢复与上下文

- schema v2迁移、外键、事务故障回滚、OS单写者锁；assistant+planned、授权、意图、结果+tool消息+事件原子提交，UI最终结果发布晚于持久化。
- sessions list/resume/replay/export可运行。读者复制DB/WAL私有快照，原路径无sidecar写入；导出拒覆盖，workspace错配与并发writer在provider前拒绝。
- 真实os._exit故障覆盖edit/create写后未提交结果；after hash恢复只确认、不二次替换。实际命令counter=1后崩溃，连续resume均0模型且counter仍1；冲突保护用户文件。pending undo也按hash/权限只读协调。
- 模型输入投影的确定性摘要/checkpoint、完整工具组、系统/schema/输出预留预算；达到上下文压力时压缩，无法安全容纳就停。服务端超限只缩减重试一次，原始消息完整保留。
- 根与子目录AGENTS.md有界加载、来源/hash/时间、写前新增指令门闩；Python AST repo map具排名、上限、hash缓存失效、语法失败回退。
- M3全量阶段检查307 passed、5 skipped；其后追加M4/M5与恢复边界由最终检查覆盖。

### M4：扩展与改动管理

- Skills：受信项目与显式用户目录、元数据发现、项目优先及同名来源、load_skill/skill.loaded事件、正文按需加载、损坏/过大/越界错误。真实演示中技能指导后续MCP查询，非只插入文本。
- 官方mcp==1.26.0 stdio：信任+精确启动授权、协议初始化、分页发现、确定性名称映射、外部schema校验、统一dispatch/策略/journal；readonly只由用户配置确认。tools/list_changed在下一模型快照刷新。
- 本地真服务器测试覆盖调用、超时/取消/断线unknown不重放、退出PID、两server初始化取消清理、stderr及结构化输出上限/制品。默认不运行仓库可执行配置，不继承任意环境变量。
- 启动Git用户基线、三种来源diff、当前权限下单步逆序undo；hash/模式/backup冲突拒绝，保留原staged/unstaged/untracked及index bytes。create只删除匹配文件，不reset/clean/checkout。workspace OS写锁协调跨会话写入。
- 全部slash：help/exit/model/status/plan/diff/compact/undo/skill/clear。只读规划不升级原授权，clear保留历史，compact不调用模型，模型切换校验当前provider配置能力与预算。
- 一键离线演示实际成功：3次FakeModel请求、2次工具、skill1次、MCP连接/关闭各1，返回sample.py:4。扩展专项26通过，CLI/UI专项64通过；更多变更测试计入最终套件。

### M5：独立评分、真实结果与交付

- 固定20个开发任务，分类8 Bug/4小功能/4跨文件/2测试/2理解；每个manifest保存路径、策略、模型预算、oracle入口、规则及基线hash。
- run/list/score命令、独立工作区外oracle、测试变异、源码hash/事实规则、执行后越界复核；Fake参考动作只验基础设施。失败/blocked不从分母消失，完整成功需oracle+合法diff+completed。
- DeepSeek真实20任务全部completed，API报告输入319974/输出16757 token，93次模型/117次工具、219.110秒；18/18有效修改和指定验证，2次工具错误。初评19/20的Q01评分范围缺陷修复后，0模型重评分20/20，保留原始报告、回执和回归。结果仅为固定小集单次测量。
- 修复4项独立审阅发现的评分问题：exit0提前退出绕过、oracle后修改未复核、未知任务0/0成功、拒绝稀释工具错误率。Q01只要求相关resolver读取而非无关re-export。
- Windows测试中tasklist偶发返回1，改为Win32 OpenProcess/GetExitCodeProcess直接检查受管PID，保留真实存活与清理断言；对应取消/命令19 passed、1 skipped。
- 写入Windows/Linux、Python3.12/3.13的离线CI与build配置；没有提交或触发远端CI。排除固定故障fixture与生成运行目录的格式检查，src和tests照常全部检查。

### 最终本机检查

| 实际检查 | 结果 |
| --- | --- |
| uv sync --locked --offline | 58包解析/审计成功 |
| uv run ruff check . | 通过 |
| uv run ruff format --check . | 78文件符合格式（交付复核） |
| uv run mypy src/stellarweaver | 40源码文件无错误 |
| uv run pytest -q -ra | **378 passed、5 skipped，132.96秒** |
| --help / version / doctor --offline | 全部退出0；schema 2；offline明确不验模型 |
| uv build --offline | wheel和sdist构建成功 |
| 独立安装wheel smoke | 本地target安装，-I加载CLI及20题package资源成功 |
| 完整离线20题参考脚本 | 20/20通过；0权限受阻/工具错误 |
| 本地Skills/MCP演示 | completed；3 requests/2tools；server正常关闭 |

5项skip：Windows脱离进程Job Object后端尚缺1项，本账户无符号链接创建权限3项，POSIX权限语义1项。对应实现/平台限制保留，没有把skip写成pass。完整输出见检查清单（本地归档 `docs/validation/m3_m5_checks.json`）、pytest（本地归档 `docs/validation/m3_m5_pytest.txt`）。

原始规范与docs副本、.env及其余IDE配置hash未变；仅.idea/workspace.xml在工作期间被外部更新，本轮没有操作或回写它。完整保护文件核对（本地归档 `docs/validation/m3_m5_protected_files.json`）保留了两次hash。源码/依赖按授权更新，本项目仍未初始化Git，无法给出原有分支或根仓库diff。

### 可直接执行

```powershell
uv run python -m stellarweaver.extensions.demo
```

完整离线suite：`uv run python -m stellarweaver.evals run --provider fake`。真实API与独立重评分步骤和口径见[evals/README.md](../evals/README.md)、[实际结果](../evals/RESULTS.md)；恢复/撤销规则见[CHANGES.md](CHANGES.md)。

### 后续与未验证

M0–M5本轮实施范围完成；下一步从V2或补充实测继续：Windows Job Object/Docker后端、更强oracle隔离、匹配模型的tokenizer、Linux/macOS实测、远端CI、重复20题与两种消融。未运行这些项目，不填推测成功率、性能或平台通过记录。Windows ACL独占性未做专门安全审计，应用层路径/授权/锁不能替代OS隔离。


## 2026-10-25：M2 已完成（真实模型、流式、持续聊天）

本轮沿用本规范的M2范围，未进入M3恢复/上下文实现。当前最小闭环 M0–M2 可运行；SQLite记录仍不等于已实现崩溃恢复。

### 新增行为与设计融入

- provider.complete/stream共用领域契约，增加ModelStreamEvent与临时TextDelta。有界SSE聚合按index/ID组装多个工具的名称/arguments，支持Unicode分片、usage-only、文本和工具同响应。完整finish及流结束后才提交；断线、冲突、缺终态、length不会执行半个工具。
- 唯一重试层位于Agent，SDK内部重试为0。408/409/429、5xx和网络失败最多两次额外尝试，受次数/token/总时长约束，遵守完整Retry-After与指数退避/抖动。失败未知用量保守预留并标记usage_incomplete，工具副作用不自动重试。
- chat持续异步输入，复用provider、冻结策略、registry、完整消息和同一SQLite session；每个任务创建新run_id，预算按任务重置。支持/help、/status、/exit；run/continue_run防同会话并发。
- Ctrl+C取消当前模型/工具、等待清理、补齐已提交工具回合，再返回输入；第二次中断只要求清理后退出。Windows信号等待加入100ms定时唤醒，不重复取消清理。首轮尚未开始便取消也可正常提交下一轮。
- doctor --check-model --provider deepseek显式发起一次无工具、最多64输出token的检查；与--offline互斥。exec/chat增加三态--stream/--no-stream：未传参数尊重能力配置，只有显式参数才覆盖；无能力时有明确非流式fallback。
- JSONL持久事件使用连续seq；text.delta使用独立request_index/chunk_index，不占durable seq，ASCII转义兼容Windows GBK。流失败显示的草稿明确作废，不作为历史。
- 参考mini-swe-agent的显式query/execute/observation与预算边界、Pi的事件与继续上下文边界，以及前轮kitagent的职责分离；固定commit与具体融入点见[架构](ARCHITECTURE.md)。独立实现，没有复制源码或加入额外框架。

### 真实 DeepSeek 编码小任务

执行：

```powershell
uv run python -m stellarweaver.live_demo --env-file .env --output docs/validation/m2_live_report.json
```

使用deepseek-flash真实SSE响应，在新建临时Git仓库修复pagination.py的分页大小校验。模型只获准编辑该源码及执行完整匹配的unittest命令，可见测试与无关文件受hash保护；外部oracle位于工作区之外。

| 实际证据 | 结果 |
| --- | --- |
| 模型请求 / 工具调用 | 7 / 10 |
| 模型实际运行测试退出码 | **1 → 0** |
| 外部独立行为检查 | **14个断言通过，退出码0** |
| 可见测试与无关文件 | SHA-256保持不变 |
| assistant工具调用 / 最终结果 | 数量匹配，10 / 10 |
| Agent运行状态 / 检查 | completed / passed |
| task_assessment | unknown；外部oracle成功另行记录 |
| 本任务耗时 | 13.938秒 |
| API报告本任务token | 输入31568，输出1251 |
| 用量估算 / 不完整标记 | false / false |

本次只证明这个明确fixture上的流式工具任务成功，不是20任务评测、通用编码成功率或性能排名。报告含workspace、SQLite、before/after hash、diff及实际命令输出：完整报告（本地归档 `docs/validation/m2_live_report.json`）、事件（本地归档 `docs/validation/m2_live_report_events.json`）。

doctor新命令也真实通过，固定marker精确匹配、32输入+11输出tokens；该检查用量与上表编码任务分开记录。CLI证据（本地归档 `docs/validation/m2_cli.txt`）。

### 最终离线验收

| 命令/验证 | 结果 |
| --- | --- |
| uv sync --locked --offline | 39依赖审计，退出码0 |
| uv run ruff check . | 通过 |
| uv run ruff format --check . | 45文件已格式化，通过 |
| uv run mypy src/stellarweaver | 27源码文件，无类型错误 |
| uv run pytest -q -ra | **183 passed, 2 skipped，35.87秒** |
| --help / chat --help / exec --help / doctor --offline | 全部退出码0 |
| Fake流式JSONL CLI | 20条有效JSON记录，持久seq连续，最终run.completed |
| 原M1离线读改测demo | 仍成功，未引入回归 |
| 集成D | 真实父子进程取消、PID清理、同会话再输入通过 |
| 原生Windows ConPTY | 两轮任务、/status、/exit；Ctrl+C取消命令后继续任务通过 |

完整输出：测试（本地归档 `docs/validation/m2_tests.txt`）、静态/环境（本地归档 `docs/validation/m2_static.txt`）、CLI/真实doctor（本地归档 `docs/validation/m2_cli.txt`）、离线入口（本地归档 `docs/validation/m2_offline_cli.txt`）、ConPTY复验（本地归档 `docs/validation/m2_tty_final.json`）。

ConPTY复验中受管父子PID均已结束、stdout保留、第一run为cancelled、下一run为completed。工具总时长包含执行前段和远程按键注入，不作为单独取消延迟指标。含Ctrl+C的ConPTY宿主PowerShell最终返回1，而应用内运行结果与继续输入正常；该宿主退出码传播没有证明等于原生交互shell行为，保留平台限制。

两项skip与M1一致：Windows脱离子进程/父进程提前退出需要Job Object后端；本机无创建symlink权限（WinError1314）。普通附属子进程超时/取消路径已真实覆盖。

### 实际失败与修复

1. 跨assistant轮次复用call_id时，截断/跳过调用曾复用旧operation_id并覆盖历史记录；回归真实复现后改为每轮独立操作映射。
2. 退避等待受调度延迟超过deadline时曾抛TimeoutError而没有run终态；改为budget_exceeded并持久化结果。
3. doctor首次真实请求使用裸marker提示，API已响应但未精确匹配；改为明确“Reply exactly:”并再次真实通过。未将首次不匹配伪报为认证失败。
4. 原生ConPTY观察到取消反馈等待，增加信号定时唤醒与取消后继续验证；没有据总工具时长宣称某个延迟数值。
5. 刚创建尚未启动的首轮任务被取消后，continued标记曾可能使下一轮无上下文继续；按实际Agent历史判断run/continue，并加入回归。
6. 默认stream参数曾覆盖显式supports_streaming=false，独立审查后改为三态配置优先级，四种配置组合均覆盖。

### 用户文件与阶段边界

本轮仍无.git，无法提供Git分支或diff；基线hash在基线（本地归档 `docs/validation/m2_baseline.json`）。原始规范、docs规范副本与.env hash均未变。本轮未写.idea；检测到.idea/workspace.xml有外部变化，保留现状，没有尝试恢复或覆盖。其他基线IDE文件未变，检查记录（本地归档 `docs/validation/m2_protected_files.json`）。

未Git提交/推送/发布、未建立外部账户连接，没有引入Web或多Agent。API只通过用户已配置的DeepSeek密钥显式使用；真实演示不发送项目或个人文件，只使用生成fixture。

### 尚未完成/验证

- M3的迁移、会话恢复锁、unknown副作用协调、resume/replay/export、上下文压缩、分层AGENTS.md、repo map尚未实施。
- M4/M5的Skills/MCP/undo、20任务fixture与独立评测、完整CI尚未实施。
- Linux/macOS与其他Python版本未在本轮运行；Windows脱离进程回收仍为best effort，Host没有OS文件系统隔离。
- 真实DeepSeek编码任务只测一次；429/Retry-After/网络中断恢复由离线SDK传输和集成故障注入覆盖，没有在真实服务制造故障。
- 持续聊天和取消完成了Windows ConPTY验证；不同终端/远程连接、所有物理按键和宿主退出码组合未全面覆盖。
- 上下文达到客户端窗口时仍停止；没有M3压缩或无限会话保证。费用未知，不按未提供单价推算。

### M3 从哪里继续

1. 先核验本节与当前代码，复跑离线检查；重点读取session/store.py、runtime/loop.py和新增M2集成回归。
2. 定义schema迁移与单写者锁，把现有运行/授权/工具状态事务边界整理为可恢复journal；对started无结果保留unknown，不自动重跑命令。
3. 实现文件写后结果提交前故障注入与hash协调（集成E）；保持用户后续编辑和原始脏工作区。
4. 实现resume/replay/export，先完成离线回放零模型/零工具副作用（集成F），再允许明确恢复。
5. 加入完整工具回合分组的上下文预算/压缩、项目AGENTS.md作用域和基础Python repo map。M2消息/工具/取消回归必须继续通过。



## 2026-10-25 12:46：DeepSeek 最小真实连通验证通过

用户配置密钥并明确要求检查后，使用现有 OpenAICompatibleChatProvider 发起一次真实非流式请求。模型为 deepseek-flash，主机为 api.deepseek.com，要求回复固定标记 STELLARWEAVER_DEEPSEEK_OK；实际回复精确匹配，stop_reason=stop。耗时2.063秒，用量为输入32、输出11、总计43 tokens。

本次未暴露或执行任何工具，未发送仓库文件或密钥内容到模型上下文。仅证明本次认证、接口和文本响应正常，不证明真实工具调用、完整编码任务、流式或M2已经验收。结果见 连通检查JSON（本地归档 `docs/validation/deepseek_connectivity.json`）。下方M0/M1检查记录保留当时的历史状态；后续M2可从真实工具任务和流式聚合继续。


## 2026-10-25：M0 / M1 已完成

已按 docs/IMPLEMENTATION_SPEC.md 第18节完成本轮阶段，真实模型连通、流式、多轮交互继续 M2。本次无 Git 提交、发布或外部账户连接。

### 启动基线与用户文件

- 开始时目录只有 CODING_AGENT_IMPLEMENTATION_SPEC.md，没有 .git，无法给出分支/既有 Git diff。
- 检查 D:/AGENTS.md、D:/PycharmProjects/AGENTS.md 和本目录，未发现适用 AGENTS.md。
- 原始规范保留不动，复制到 docs/IMPLEMENTATION_SPEC.md。
- 原文件及副本 SHA-256 相同：`8290943837D68610D6E3B16B2665116215049517F64AEFEA8B9D803312165433`，结束时复核原文件未变。
- 新增 src/stellarweaver、pyproject.toml、uv.lock、.venv、配置/凭据模板、测试、示例及文档；没有对本目录初始化 Git。

### 本轮实际交付行为

- M0：Python src 包、uv 锁定环境、严格 TOML 配置、核心领域类型、ModelProvider、脚本/回调 FakeModel。
- CLI：--help、--version/version、doctor --offline、exec --prompt（兼容位置 task）、chat 单任务 TTY 入口、--env-file、--json、--no-color。
- 八工具：list_files、read_file、search_text、create_file、edit_file、run_command、git_status、git_diff。
- 程序级边界：严格字段/类型、固定 workspace、realpath/保护路径/忽略规则、无 TTY 不等待、冻结完整 argv/cwd 或精确路径授权。
- 文件：完整 SHA-256、有界读取、1 MiB UTF-8 编辑限制、唯一精确替换、审批后复检、排他新建、原子编辑、CRLF/权限、before/diff 制品。
- 命令：argv 无 shell、固定 cwd、受控环境、真实退出码、双流有界输出与制品配额、超时/取消清理；Git 查询过滤敏感路径并禁外部扩展执行。
- 主循环：完整工具回合、校验/拒绝/取消结果、次数/时长/token/窗口预算、三次连续相同失败停止、SQLite 消息/事件/执行意图/最终结果。
- 报告：run_status 与 task_assessment、verification_status 分开；假模型声明不作为通过证据；编辑会使旧检查失效。
- DeepSeek：已实现 deepseek-flash 非流式 function tools 适配、关闭 thinking 和 SDK 隐式重试，真实 SDK 的 MockTransport 测试通过；没有调用真实 API。
- 用户参考 kitagent 的阅读范围、固定 commit、借鉴思路写入 ARCHITECTURE.md，未复制其源码或引入其框架/平台范围。

### 可复现演示

在项目根目录执行：

```powershell
uv run python -m stellarweaver.demo
```

实际创建独立临时 Git 仓库，调用 list/read/search/edit/command/Git 工具；第一次编辑后的真实 unittest 退出码为 **1**，继续修复后的退出码为 **0**。演示返回 completed / verification passed，打印实际 workspace 和 SQLite 路径，保存备份与 diff；断言无关文件不变。执行记录见 完整演示输出（本地归档 `docs/validation/m0_m1_demo.txt`）。

仅使用读取工具的 CLI 示例：

```powershell
uv run stellarweaver exec --provider fake --script examples/offline_read.json --prompt "查看项目文件"
```

### 最终检查结果

本次实测 Windows / Python 3.12.6 / uv 0.9.7 / Git 2.53.0.windows.3。

| 实际命令 | 结果 |
| --- | --- |
| uv sync --locked --offline | 39 个锁定依赖已审计，退出码 0 |
| uv run ruff check . | 通过 |
| uv run ruff format --check . | 38 个文件已格式化，通过 |
| uv run mypy src/stellarweaver | 24 个源码文件，无类型错误 |
| uv run pytest -q -ra | **107 passed, 2 skipped，29.21 秒** |
| uv run stellarweaver --help | 正常显示可用命令，退出码 0 |
| uv run stellarweaver version | stellarweaver 0.1.0；Python 3.12.6；schema 1 |
| uv run stellarweaver doctor --offline | 本地诊断完成；Key missing；明确 model connectivity not tested |
| FakeModel CLI 读取脚本 | completed / verification not_run，退出码 0 |
| uv run python -m stellarweaver.demo | 真实失败后修复，测试退出码 [1, 0]，演示退出码 0 |

完整输出：pytest（本地归档 `docs/validation/m0_m1_tests.txt`）、静态/环境检查（本地归档 `docs/validation/m0_m1_static.txt`）、CLI（本地归档 `docs/validation/m0_m1_cli.txt`）、演示（本地归档 `docs/validation/m0_m1_demo.txt`）。Ruff 只跳过原始规范及其逐字副本的 Markdown 示例格式，避免格式器改写用户原文，新增代码照常检查。

两项跳过：

1. Windows 脱离子进程/父进程提前退出后的完整回收，需要后续 Job Object 后端；普通附属子进程超时/取消已实测通过。
2. 本机账户创建符号链接报 WinError 1314，无所需特权；符号链接逃逸测试按平台条件跳过，路径检查实现仍保留。

### 实际失败及修复记录

- Windows CRLF 导致部分测试将原始输出误按 LF 比较；修正测试表达后通过，文件工具保留 CRLF。
- 锁定 SDK 的类型签名与占位类型不一致；修正 Omit、非流式 overload、function/custom tool 分支后 mypy 通过。
- Rich 自动折行导致错误文字断言失效；改为规范化空白，仍检查凭据不出现在输出中。
- 独立审查复现“大输出制品随机 ID 导致重复失败检测失效”，改为比较稳定的失败语义。
- 独立审查复现 assistant.completed / tool.completed / tool.finished 取消时剩余工具缺结果；修复待处理调用闭合、保存已完成结果后取消，其7项预算/回合回归全通过。
- 显式 data-dir 可位于工作区内，补动态会话目录保护及 SQLite sidecar 保护。
- 本机检查汇总脚本默认 GBK 与 UTF-8 解码冲突，明确验证环境 UTF-8 后重跑；CLI JSONL 另以真实 GBK 输出流验证中文/emoji可无损往返。

### 凭据位置

已创建空 `D:/PycharmProjects/StellarWeaver/.env`，没有覆盖已有文件。填写：

```dotenv
STELLARWEAVER_API_KEY=你的Key
```

程序支持显式 `--provider deepseek --env-file .env`，默认离线 FakeModel 不使用密钥。没有真实服务验证，不宣称兼容测试等于模型连通或编码能力通过。详见 [Provider兼容性](PROVIDER_COMPATIBILITY.md)。

### 未完成与未验证

- 未进行真实 DeepSeek API 请求；没有费用、真实模型编码效果或20任务评测数据。
- M2：流式解析/组装、预算感知瞬态重试、持续多轮 chat、完整输入中断体验和真实模型小任务尚未完成。
- M3：崩溃协调、迁移、恢复锁、resume/replay/export、压缩、AGENTS.md 分层加载、repo map 尚未完成。M1 SQLite证据不等于已实现安全恢复；数据库I/O失败可能留下started未知操作，没有自动重放。
- M4/M5：Skills、MCP、undo、20任务独立oracle/评测、完整CI尚未实施。
- Linux/macOS、其他Python版本、真实TTY完整审批体验尚未在本轮验证；Windows原生不宣称为强沙箱或任意进程树可靠隔离。

## M2 从哪里继续

1. 先复跑本页检查并核对当前代码；读取 providers/openai_compatible.py、runtime/loop.py、ui/console.py 和 tests/unit/test_provider.py。
2. 用户填好 Key 并明确执行模型验证后，增加 doctor --check-model 的最小显式连通检查；再让 deepseek-flash 在受限临时仓库完成真实小任务，保存真实证据。
3. 增加非执行的流式聚合器：按 index/ID 合并多工具交错分片，完整结束后才提交 assistant，覆盖Unicode、JSON跨片段、length、断线、缺usage等。
4. 统一计算显式HTTP尝试与退避预算，最多两次额外请求；工具副作用不重试。
5. 完成chat持续输入/取消、集成D与平台化进程清理，保持每个已提交调用最终有结果；通过后再进入M3。
