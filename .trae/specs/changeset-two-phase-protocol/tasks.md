# 批量 Markdown 变更的两阶段协调协议（Changeset Protocol）- 实施计划

> AC 来源见同目录 `spec.md`。测试文件遵循仓库扁平命名：`core/tests/test_changeset_<行为>.py`。
> 所有 vault 内容写入只经 `core.transaction.Transaction`；运行时台账只写 `System/.dex/changesets/`。

## Task 1: 所有权契约授权 content-conversion 与台账路径登记
- **Status**: `completed`
- **Priority**: high
- **Depends On**: None
- **Completion Evidence**:
  - TR-1.1 `rule`：新增 4 个测试函数（含参数化 7 个拒绝路径/3 个放行路径），`153 passed`（test_portable_contract.py 全绿）；未分类 .md 经 fail-closed 分支拒绝（补过一次 resolution=None 漏洞）。
  - TR-1.2 `rule`：`resolve("System/.dex/changesets")` → runtime；`bash scripts/check-portable-contract.sh` 退出码 0（2135 tracked paths classified, dist in sync）；`generate-portable-contract.py` 重生成，JSON diff 仅 +runtime-changesets 一条（operation 枚举不在 JSON 中）；git 改动仅 portable_contract.py、测试、dist JSON。
- **Description**:
  - 在 `core/portable_contract.py` 的 `update_write_verdict` 允许 operation 元组中新增 `content-conversion`，并新增终局分支：仅当路径规范化后通过 `is_denied`、后缀为 `.md`、`resolve()` 所有权 ∈ {`vault`, `seed`} 时返回 allow（action 名 `convert-with-user-approval`），其余一律拒绝（含 brain/generated/runtime/未分类/非 .md/拒绝模式）。
  - 在路径规则表新增 `_r("runtime-changesets", "System/.dex/changesets", "dir", "runtime", ...)`（说明：changeset 意图、暂存与待处理事件日志，本地运行时状态，从不随发布分发）。
  - 运行 `python3 scripts/generate-portable-contract.py` 重新生成 `packages/dex-contracts/dist/portable-vault.contract.json`（不手改生成产物）。
- **Acceptance Criteria Addressed**: AC-10
- **Test Requirements**:
  - `rule` TR-1.1: 在 `core/tests/test_portable_contract.py` 新增参数化用例：vault/seed 下存在的普通 .md 对 content-conversion 放行；brain、generated、runtime、未分类路径、`.txt`、符号链接语义路径、命中 HARD_DENY 的路径全部拒绝；未知 operation 仍抛 ValueError。
  - `rule` TR-1.2: `resolve("System/.dex/changesets")` 返回 runtime 类；`bash scripts/check-portable-contract.sh` 退出码 0；生成器运行后 git diff 仅含源文件与预期生成产物差异。
- **Notes**: 参考 L950 `customization-migration` 分支与 L312-322 runtime 登记模式；该分支必须终局 return，不得落入通用 MUTATION_POLICY。

## Task 2: 变更意图模型 + 指纹与任务 ID 出现位置提取器
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 1
- **Completion Evidence**:
  - TR-2.1 `rule`：test_changeset_occurrences.py 覆盖裸锚点/`[[^...]]`/别名 wikilink/行内与围栏代码块/多文件多次；100 与 1000 边界不误配。
  - TR-2.2 `rule`：missing-file/extra-file/count-mismatch 三类差异断言（含前后计数）；wikilink 包裹转换映射不变用例通过。
  - TR-2.3 `rule`：FileIntent 往返/坏载荷拒绝、ChangeSet.summary 仅路径+id（无正文 PII）；流式 sha256 与内存 sha256 在 >1MiB 载荷下一致。17 passed。
- **Description**:
  - 新建 `core/changesets/__init__.py`、`core/changesets/model.py`：`FileIntent`（relative、old_sha256、new_sha256、size、mode）、`ChangeSet`（id、operation、created_at、intents 列表、task 出现位置映射前后快照、关联 tx_id）、状态枚举（PREPARING/PREPARED/COMMITTING/COMMITTED/ABORTED/REVERTED；INTERRUPTED 为恢复时推导态，不落盘为断言）。
  - 新建 `core/changesets/fingerprints.py`：流式 sha256（分块，不整文件驻留）；任务 ID 出现位置提取：正则 `\^task-\d{8}-\d{3,}` 须同时命中裸锚点与 `[[^...]]` wikilink 形式；输出 {task_id: {relative: count}}；提供"转换前后映射差异"函数，区分集合增减（missing/extra 文件）与每文件次数差异（count mismatch）。提取须与 work_server 锚点边界一致（`(?!\d)` 防 100/1000 误配）。
- **Acceptance Criteria Addressed**: AC-1、AC-4
- **Test Requirements**:
  - `rule` TR-2.1: `core/tests/test_changeset_occurrences.py`：同一 task id 跨 3 文件、每文件多次、wikilink 形式、代码块/行内代码边界（转换不应改动代码块内文本）的提取计数正确；裸锚点与 `[[^...]]` 计数一致。
  - `rule` TR-2.2: 差异函数对"文件集合丢失/新增"与"次数不一致"分别输出正确类别与位置清单；完全一致时返回无差异。
  - `rule` TR-2.3: `test_changeset_intents.py`：FileIntent 指纹与独立 sha256 计算一致；大文件分块哈希与一次性哈希结果相同。

## Task 3: 持久化待处理日志与暂存存储
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 2
- **Completion Evidence**:
  - TR-3.1 `rule`：状态纯由 events.jsonl 重放推导（PREPARING→PREPARED→COMMITTING→COMMITTED）；PREPARED 载荷重建 intents/occurrence maps，重复目标拒绝。
  - TR-3.2 `rule`：撕裂尾（末尾不完整 JSON）读取忽略、追加时自动截断；行内哈希损坏 fail-closed 抛 ChangeSetStorageError 且不被 prune 清理；暂存 blob 0600 且 sha/size 校验，篡改即检出；prune 仅删最旧终态（keep=3），未决/不可读目录受保护。7 passed。
- **Description**:
  - 新建 `core/changesets/storage.py`：变更组目录 `System/.dex/changesets/<cs_id>/`（0700），含 `events.jsonl`（直接复用 `core.transaction.journal.Journal`：fsync、逐行 sha、撕裂尾截断）、`staged/<NNNNNN>.bin`（0600，新字节暂存，写入后 fsync 文件与父目录）。
  - 状态从事件日志单一推导（PREPARING→PREPARED→COMMITTING→COMMITTED / ABORTED / REVERTED + 关键 payload：intents 清单、前后出现位置映射、tx_id、失败原因）；提供列举全部变更组、读取单个、无法解析目录 fail-safe 视为未决的 API。
  - 原子写意图清单：意图随 PREPARED 事件 payload 落盘（事件已哈希+fsync），不另引入第二套格式。
  - 终态清理：删除 staged 新字节；终态台账按最新 3 个留存（对齐 tx keep=3），任何非终态/不可读目录绝不删除。
  - 路径安全：目录与暂存路径经 `unsafe_existing_parent` 检查。
- **Acceptance Criteria Addressed**: AC-5、FR-13
- **Test Requirements**:
  - `rule` TR-3.1: `test_changeset_pending_log.py`：事件按序 append 后可完整重建状态；手工截断最后一行（撕裂尾）后读取得到截断前状态且可继续 append；中间行被篡改时 fail-closed。
  - `rule` TR-3.2: 暂存 blob 权限 0600、目录 0700；fsync 失败/磁盘错误路径不产生谎称已暂存。
  - `rule` TR-3.3: 构造 4 个终态组时仅保留最新 3 个台账且其 staged 已清理；PREPARED 组在任何清理调用后仍完整保留。

## Task 4: Prepare 协调器（阶段一：全量暂存 + 校验，无锁）
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 2、Task 3
- **Completion Evidence**:
  - TR-4.1 `rule`：test_changeset_coordinator.py 合法转换→PREPARED，intents/前后映射齐备，files_scanned/changed 正确（8 passed）。
  - TR-4.2 `rule`：任务不变量破坏（missing-file，含 task_id/kind/location）、brain .md contract-forbids、.txt not-markdown、prepare-commit 间 live 漂移均拒绝；vault 字节零变化、无 mutation.lock、暂存清理、PREPARE-FAILED 落盘。
  - TR-4.3 `rule`：未改动文件不进 intents；全未变化→noop 且台账无残留；有未决组时二次 prepare 拒绝（pending-changeset-exists）。
  - TR-4.4 `rule`：after-stage:0 子进程崩溃（exit 137）残留 preparing 组，rollback 后可重新 prepare/commit（test_changeset_recovery.py）。
- **Description**:
  - 新建 `core/changesets/coordinator.py` 中的 prepare 部分：输入 vault_root、相对路径列表（或文件选择谓词）、纯函数 converter(bytes/text)→新字节；流式逐文件：读当前字节→记录旧指纹与转换前出现位置映射→转换→写暂存→记录新指纹与转换后映射。
  - 全部转换完成后校验：(a) 暂存 sha==新指纹；(b) live sha==旧指纹（乐观复检）；(c) 任务 ID 出现位置不变量（FR-3，仅统计纳入变更组的文件加上其引用所在文件构成的闭包——实现上对"前/后映射中出现过的每个 task id"比较全量文件集合，因此转换器必须扫描全库相关文件，或 prepare 对全库做一次出现位置基线扫描）；(d) 路径合法性（非符号链接、.md、未拒绝、所有权 ∈ {vault,seed}），用 content-conversion verdict 预检。
  - 任一项失败：记录 PREPARE-FAILED 原因（任务 id/类别/位置），清理本次暂存，返回结构化拒绝；vault 内容零改动、不获取 mutation.lock。无实际变化的文件不进入 intents（零改动组 = 干净 no-op，返回无变更）。
  - 成功：append PREPARED（含完整意图与映射）。prepare 幂等：对同一 cs_id 重入可重建/继续（用于 PREPARING 崩溃）。
  - 提供确定性崩溃注入环境变量（如 `DEX_CHANGESET_STOP_AFTER=after-stage:<i>|after-prepared`），风格对齐 `DEX_TX_TEST_STOP_AFTER`。
- **Acceptance Criteria Addressed**: AC-1、AC-2、AC-4
- **Test Requirements**:
  - `rule` TR-4.1: `test_changeset_coordinator.py`：合法转换 prepare 后状态 PREPARED，意图/指纹/映射齐备且与独立计算一致。
  - `rule` TR-4.2: 三类失败夹具（指纹被外部改动、出现位置不变量破坏、非法路径）prepare 后内容目录 sha 清单与前置完全一致、无 mutation.lock、返回结构化原因含任务 id 与位置。
  - `rule` TR-4.3: 无变化文件被排除；全部无变化时返回 no-op 且不留未决组。
  - `rule` TR-4.4: `after-stage:<i>` 与 PREPARED 前注入崩溃后，新进程可识别残留且重新 prepare 不产生重复组/不留垃圾暂存。

## Task 5: Commit / Resume / Rollback 协调器（阶段二 + 崩溃恢复）
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 4
- **Completion Evidence**:
  - TR-5.1 `rule`：commit 后目标全部为新指纹、staged 释放、锁释放；during_commit 钩子内第二变更者获 LockBusyError（test_changeset_coordinator.py）。
  - TR-5.2 `rule`：prepare 后篡改单文件→commit 前乐观复检拒绝 live-fingerprint-drift，全部目标保持原字节；COMMIT-FAILED 路径（锁忙/PlanRejected）记 committing+failure 且可 resume。
  - TR-5.3 `rule`：test_changeset_recovery.py 子进程 137 注入 after-prepared / mid-apply:0 / after-commit-record：崩溃后无任何自动动作且（mid-apply 断言真实部分扇出落盘）；显式 resume 后 COMMITTED 且字节正确；提交前 rollback→ABORTED vault 无变化。7 passed。
  - TR-5.4 `rule`：提交后 rollback 经关联 tx snapshot 反向 content-conversion 事务还原字节→REVERTED；删除快照后 RollbackUnavailable(reason=rollback-snapshot-unavailable, git 指引) 且零写入、记录仍 committed。
  - TR-5.5 `rule`：reconcile 分支：未开始→重提；tx 无 COMMITTED→Transaction.resume 回滚后重提；tx COMMITTED 仅事件丢失→live 校验后补记 repaired；live 漂移→ChangeSetConflict；已 COMMITTED→幂等。preparing 壳不可 resume、可 rollback。
  - TR-5.6 `rubric`：5/5——提交路径只经 Transaction（含确定性 tx_id conv-&lt;cs&gt; 复用 `_begin_with_id`），锁由事务持有、协调器不重复加锁；复用 Journal/Snapshot/fsync/unsafe_existing_parent/keep=3 留存；新增代码仅意图、暂存、校验、协调。
- **Description**:
  - `commit(cs_id)`：从 PREPARED 事件重建计划，逐条构造 `PlanEntry(relative, content=暂存字节, expected_current_sha256=旧指纹, mode)`，以 `Transaction.begin(vault_root, plan, operation="content-conversion")` + `run()` 执行——库级锁、快照、temp+rename、验证全部由事务核心提供；append COMMITTING（含即将使用的确定性 tx id，用 `_begin_with_id` 接缝）于 begin 之前，成功后 append COMMITTED（含 tx_id 与 snapshot 目录引用），随后清理 staged。
  - `resume(cs_id)`：先做 reconcile——读事件、检查关联 tx journal 终态、抽 live 指纹：①PREPARED 且 live==旧指纹、暂存完整→允许重新 commit；②COMMITTING 且 tx 已回滚（无 COMMITTED 事件、live==旧指纹）→允许重新 commit；③COMMITTING 且 live==新指纹（提交实际完成、仅事件丢失）→补记 COMMITTED；④live 与旧/新都不符→拒绝并返回冲突原因，不猜；⑤已 COMMITTED→幂等成功。绝不被任何只读操作自动触发。
  - `rollback(cs_id)`：提交前=丢弃暂存+ABORTED（vault 无变化）；提交后=从关联事务 snapshot manifest+blob 读取原字节，构造反向 content-conversion PlanEntry（expected=新指纹）再跑一个事务，成功记 REVERTED；快照缺失/损坏→明确错误 + 现有 git 备份指引，零写入。
  - 冲突/失败路径：事务 begin/run 抛错（锁忙、旧指纹漂移、授权变化）时记录 FAILED 原因并原样抛出结构化结果，由读取端呈现 conflict/pending。
  - 新建 `test_changeset_recovery.py`，用 subprocess + 崩溃注入覆盖：PREPARING 中、PREPARED 后、COMMITTING 中（配合 `DEX_TX_TEST_STOP_AFTER`）、COMMITTED 记录后；每点断言恢复矩阵与文件字节。
- **Acceptance Criteria Addressed**: AC-3、AC-6、AC-7、AC-13、NFR-1
- **Test Requirements**:
  - `rule` TR-5.1: commit 成功后所有目标 sha==新指纹；commit 期间另一进程获取同一锁文件得到 LockBusyError。
  - `rule` TR-5.2: 注入单文件旧指纹漂移时事务整组回滚，所有目标 sha==旧指纹，变更组状态可被读取端判为失败/冲突。
  - `rule` TR-5.3: PREPARED 后与 COMMITTING 中两个杀进程窗口，新进程不做任何自动动作；显式 resume 后到达 COMMITTED 且字节正确；显式 rollback（提交前）后 ABORTED 且 vault 无变化。
  - `rule` TR-5.4: COMMITTED 后 rollback 在快照存在时字节级还原为 REVERTED；删除快照后 rollback 返回"无法自动回滚"且零写入。
  - `rule` TR-5.5: reconcile 五种分支各有夹具断言；无任何路径会自动提交或自动回滚。
  - `rubric` TR-5.6: 架构一致性；1-5；1=另起锁/写入路径或重复 journal/snapshot 机制；3=复用但有可避免重复；5=提交只经 Transaction、复用 Journal/lock/fsync/snapshot/留存，新代码仅含意图/暂存/校验/协调；阈值 >=4；证据：模块调用关系与代码审查。

## Task 6: 读取端一致性检查库
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 3
- **Completion Evidence**:
  - TR-6.1 `rule`：core/changesets/consistency.py 输出 {status, scanned_at, files_scanned, changesets, conflicts, warnings}；core/tests/test_changeset_consistency.py 覆盖 clean/pending（all-new interrupted 子态）/partial fan-out/live edit/set drift/count drift/损坏台账=interrupted-changeset，断言 kind/task_id/changeset_id/locations，11 passed。
  - TR-6.2 `rule`：手工部分扇出（一个文件已落地另一个未落地）与某文件丢 id 均判 conflict；mixed checkbox（work_server 同正则语义）判 mixed-task-state；信封 JSON-safe 用例断言不含绝对路径与文件正文。
  - TR-6.3 `rule`：pending_overlap/fanout_gate 对 task id 重叠与路径重叠分别命中、无关不命中，返回 allowed + reason(vault-conflict/pending-changeset-overlap)。
- **Description**:
  - 新建 `core/changesets/consistency.py`：只读。枚举变更组推导状态（未决组→pending；reconcile 发现中断→pending+interrupted 子态）；扫描 vault（限定与 Task 4 同一组用户内容目录/可注入路径谓词）构建实时任务 ID 出现映射与每个出现位置的复选框状态（open/completed，复用 work_server 复选框正则语义）。
  - 冲突检测 kind：`interrupted-changeset`、`task-occurrence-set-drift`（相对最近一个变更组前后映射，或无组时的跨文件异常）、`task-occurrence-count-drift`、`mixed-task-state`（同一 task id 跨位置完成状态不一致）、`fingerprint-drift`（live 与某未决组旧/新指纹均不符）。
  - 输出信封：`{status: clean|pending|conflict, changesets: [...], conflicts: [{kind, task_id?, changeset_id?, locations: [relatives], detail}], scanned_at, vault_root}`；提供路径/任务 id 重叠查询供守护进程使用。无 PII：只含相对路径、id、计数。
  - 对扫描异常文件（不可读、解码失败）计入冲突/告警而非静默跳过。
- **Acceptance Criteria Addressed**: AC-8
- **Test Requirements**:
  - `rule` TR-6.1: `test_changeset_consistency.py`：clean、pending（含 interrupted 子态）、conflict 三类夹具；每类冲突 kind 至少一例，断言 kind/task_id/locations。
  - `rule` TR-6.2: 手工部分扇出（A 完成/B 未完成；某文件丢 id）必报 conflict；信封 JSON 可序列化且不含绝对路径与文件正文。
  - `rule` TR-6.3: 重叠查询对"任务 id 重叠"与"路径重叠"正确命中/不命中。

## Task 7: 只读 MCP 服务器 dex-changeset-mcp 与注册
- **Status**: `completed`
- **Priority**: medium
- **Depends On**: Task 6
- **Completion Evidence**:
  - TR-7.1 `rule`：core/mcp/changeset_server.py（Server("dex-changeset-mcp")）暴露 get_changeset_status / list_pending_changesets，仅 import consistency 只读层，无任何 prepare/commit/resume/rollback 调用；stdio 全链路（initialize→tools/list→tools/call）5 passed，clean/conflict/pending 夹具输出与核心库信封一致并含 feature_status 字段。
  - TR-7.2 `rule`：preflight SERVER_MODULES/SERVER_LABELS 已登记；System/.mcp.json.example 按既有模式加入条目（provision.cjs 经该模板生成 mcpServers）；test_mcp_registration_lifecycle CORE_MCP_SERVERS 固定集合已更新；test_mcp_stdio_startup/test_preflight_paths 等共 47 passed。test_provision_parity 的失败均为预存 js-yaml node_module 缺失（干净树同样失败，与本任务无关）。
  - TR-7.3 `rule`：scripts/generate-architecture-inventory.py 重生成成功，docs/architecture/INVENTORY.md diff 仅新增 dex-changeset-mcp（2 tools）与 runtime 路径计数 15→16（System/.dex/changesets 已在 Task 1 登记），引擎计数 11→12。
- **Description**:
  - 新建 `core/mcp/changeset_server.py`（Server 名 `dex-changeset-mcp`）：工具 `get_changeset_status`（全量信封，可选 task_id/relative 过滤）、`list_pending_changesets`（仅未决组摘要）；纯只读，任何输入不得触发写入/resume/rollback；用 `core.utils.feature_status` 返回 ok/off/not_installed/broken/unknown 信封（vault 不可解析等降级为对应状态而非崩溃）。
  - 注册：`core/utils/preflight.py` 服务器映射；provisioning（`core/provision.cjs` 生成 mcpServers 的来源处）按既有服务器同模式加入；stdio 启动与 list_tools 可用。
  - 运行 `python3 scripts/generate-architecture-inventory.py` 刷新 INVENTORY（仅预期新增条目）。
- **Acceptance Criteria Addressed**: AC-12、AC-8
- **Test Requirements**:
  - `rule` TR-7.1: `test_changeset_server.py`：stdio 启动成功、工具列齐、clean/conflict 夹具下工具输出与核心库信封一致且含 feature_status 字段。
  - `rule` TR-7.2: preflight/provision 相关既有测试更新后全绿；`test_mcp_stdio_startup` 覆盖新服务器。
  - `rule` TR-7.3: INVENTORY 重新生成后漂移门通过，diff 仅含新服务器/工具条目。

## Task 8: Obsidian 同步守护进程冲突闸门
- **Status**: `completed`
- **Priority**: medium
- **Depends On**: Task 6
- **Completion Evidence**:
  - TR-8.1 `rule`：sync_daemon.DexSyncHandler 记住 vault_root；扇出前调 fanout_gate（扫描异常时 fail-closed，reason=gate-check-failed）。新增两用例：①与未决组 task id 重叠（编辑不在组目标内的镜像页）→零写入、failure_counts 记 1、log_error human_message 含 `python -m core.changesets status`、last_seen 不前进（恢复后自动重试）；②真实部分扇出（变更组一目标已落地另一未落地）即使无任务/路径重叠也拦截，reason=vault-conflict。11 passed。
  - TR-8.2 `rule`：既有 9 个守护进程用例全部回归通过（priming/防抖/canonical 直改/失败队列）。
  - 语义细化（实现层，不改 spec 读取端要求）：用户刚勾选一个镜像页产生的裸 mixed-task-state 是守护进程本来就要收敛的正常触发态，若按字面"status==conflict 即拦"会导致勾选同步永久死锁。fanout_gate 只拦截可归因于变更组的冲突（GATED_CONFLICT_KINDS=interrupted/set-drift/count-drift/fingerprint-drift）+ 未决组重叠；裸 mixed 仍在信封中暴露为 conflict（AC-8 不变），test_changeset_consistency 已分别断言两种情形，11 passed。
- **Description**:
  - 改 `core/obsidian/sync_daemon.py`：在 `sync_file_tasks` 触发 `update_task_status_everywhere` 之前调用一致性库；当 status==conflict，或存在与受影响 task_id/文件路径重叠的未决变更组时，跳过扇出写入，经 `log_error`（human_message 明确：检测到未完成的批量变更/不一致，已暂停自动同步，请运行 `python -m core.changesets status`）记录，并计入既有 failure_counts 上报。
  - 干净状态下行为保持不变（注意基线 priming 与防抖流程不回归）。
- **Acceptance Criteria Addressed**: AC-9
- **Test Requirements**:
  - `rule` TR-8.1: 扩展 `core/tests/test_sync_daemon.py`：conflict 夹具下零扇出写入（mock work_server 断言未被调用）且产生 human_message；重叠未决组同样拦截。
  - `rule` TR-8.2: clean 夹具下既有同步流程测试全部回归通过。

## Task 9: CLI 入口与 wikilink 迁移接入（含中断文案）
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 5
- **Completion Evidence**:
  - TR-9.1 `rule`：core/changesets/cli.py + __main__.py（status/resume/rollback，--json/--id/--force，退出码 0 clean/3 pending/4 conflict/1 failed/2 usage）；migrate_to_wikilinks.py 删除逐文件 write_text 循环（grep 用例断言无 write_text/write_bytes），新流程 pending 检查→确认→git 备份（prepare 前，保持备份只含用户原状）→prepare→commit；test_obsidian_wikilink_migration.py 4 passed（原夹具转换结果与纯函数逐字节一致 + 无直接写 + dry-run 无组）。
  - TR-9.2 `rule`：新增真实子进程用例：after-prepared 崩溃（exit 137）后重跑迁移不产生第二组、输出含 resume 与 rollback 全命令、live 文件零变化；照 resume 命令执行后转换落地。CLI 单测 test_changeset_cli.py 11 passed：clean/pending/conflict 退出码与 JSON 信封、resume 完成/幂等、preparing 壳拒绝、prepared rollback 零触碰、committed rollback 字节还原、快照缺失给 git 指引且零写入、未知 id 失败、无正文/无绝对路径。
  - `rubric` TR-9.3（待 Review 独立打分）：文案实现要点——每组只给一条"Your one next step"主命令（preparing→rollback，其余→resume），开头用平实语言说明文件未受损，rollback 作为次级一行替代；resume 失败时只给 rollback 一条；快照缺失只给 git 指引；JSON 路径仅含相对路径/task id/指纹。
- **Description**:
  - 新建 `core/changesets/cli.py`：`python -m core.changesets status [--json] [--id]`、`resume --id`、`rollback --id [--force]`；人类输出短步骤、状态与唯一下一步命令（AC-U1）；JSON 输出即一致性信封。
  - 改造 `core/obsidian/migrate_to_wikilinks.py`：保留索引构建与纯函数 `convert_references_in_file`、dry-run 预估、git 备份；删除 L243-256 逐文件原地写循环，改为 prepare→既有人工确认→commit；发现未决迁移组时不再新建转换，直接打印 status 与 resume/rollback 指引；dry-run 不创建变更组。VAULT_PATH 语义不变。
- **Acceptance Criteria Addressed**: AC-11、FR-12、AC-U1、NFR-4
- **Test Requirements**:
  - `rule` TR-9.1: 改造后 `test_obsidian_wikilink_migration.py` 全绿：转换结果与纯函数输出一致；代码库 grep 确认迁移脚本不再对 vault 内容直接 write_text（台账写入除外）；dry-run 后 `System/.dex/changesets/` 无新组。
  - `rule` TR-9.2: 新增"prepare 后中断再运行迁移"用例：不产生第二组，输出含明确 resume/rollback 命令；CLI status/resume/rollback 在各夹具下退出码与 JSON 字段正确。
  - `rubric` TR-9.3: 恢复可理解性；1-5；1=仅堆栈/术语；3=有说明但动作不唯一；5=平实说明数据未受损+恰好一条可复制命令；阈值 >=4；证据：对中断文案的评审记录（对照 spec NFR-4）。

## Task 10: 留存/规模性能验证与全门回归
- **Status**: `completed`
- **Priority**: medium
- **Depends On**: Task 5、Task 7、Task 8、Task 9
- **Description**:
  - 新增/扩展规模用例（参照 `test_large_vault_performance.py`）：12k 文件夹具（多数无变化、数百文件变化）下 prepare+commit 的耗时、峰值内存（不整库驻留）、暂存磁盘占用与终态释放断言。
  - 跑全门：`pytest core/tests/ core/mcp/tests/ core/migrations/tests/ -m "not fuzz"`；`scripts/check-portable-contract.sh`；架构漂移门；`npm run test:hooks`/相关 node 测试（provision.cjs 改动）；GetDiagnostics 无新增诊断。
- **Acceptance Criteria Addressed**: AC-13、AC-U3、NFR-3
- **Test Requirements**:
  - `rule` TR-10.1: 12k 夹具下全流程成功；暂存终态后磁盘释放；无任务 id 映射的错误计数（无变化文件不膨胀映射差异）。
  - `rubric` TR-10.2: 性能与资源；1-5；1=全量驻留/多轮全库扫描；3=可完成但明显冗余；5=流式、单轮基线扫描、终态即释放，耗时/占用有量化记录；阈值 >=4；证据：规模用例计时与占用记录。
  - `rule` TR-10.3: 上述全门命令退出码均为 0，输出留存为 Completion Evidence。

### Completion Evidence (Task 10)

**TR-10.1 规模全流程（[core/tests/test_changeset_scale_performance.py](file:///Users/kkcarrot/swe-project/Dex_fork1/core/tests/test_changeset_scale_performance.py)，pytest 1 passed，含夹具构建共 11.22s）**
- 12,000 个 .md（live 约 48MB），其中 400 个携带同一任务锚点并实际变化；prepare 返回 `prepared`，`files_scanned=12000`、`files_changed=400`；11,600 个无变化文件不进入意图集，任务出现映射无膨胀差异。
- commit 后样本文件为 `[[^task-20260820-001]]`、末号未变文件原样；`consistency_scan` 报 `status=clean`、`files_scanned=12000`。
- 暂存磁盘占用恰好等于新字节总和：staged = expected = **1,652,290 bytes**（每文件转换 +4 字节 × 400）；commit 终态后 staged 目录不存在（`released_after_commit=True`）。

**TR-10.2 性能与资源（量化，NFR-3）**
- `tracemalloc` 实测 prepare 峰值 **2.26 MiB**（预算 25 MiB；live 字节约 48MB → 峰值不足 live 的 5%，证明逐文件流式、无整库驻留）。
- prepare+commit 实测 **6.58s**（预算 30s）；提交后读取端单趟扫描 12k 文件 **1.06s**（预算 30s，单趟基线扫描）。
- 自评 TR-10.2 = **5/5**：流式、单趟扫描、终态即释放，且耗时/占用/磁盘均有量化断言。

**TR-10.3 全门回归（命令与退出码）**
- 特性子集回归（changeset 全套 8 个测试文件 + sync_daemon + wikilink 迁移 + portable_contract + preflight/mcp 注册门 + lens 两门，共 18 个文件）：**358 passed，EXIT=0**（`.venv/bin/python -m pytest …`）。
- `bash scripts/check-portable-contract.sh`：**EXIT=0**，"2149 tracked paths classified, release-safe, dist in sync"。
- 架构漂移门（系统 python3 为 3.9.6，跑 .sh 必因 `slots=` 语法失败——预存环境问题；按等价方式验证）：`.venv/bin/python scripts/generate-architecture-inventory.py --output "$tmp"` 后 `diff` 提交版 → **INVENTORY_DRIFT_CLEAN，EXIT=0**。
- GetDiagnostics：**无诊断 []**。
- 全量 Python 门 `.venv/bin/python -m pytest core/tests/ core/mcp/tests/ core/migrations/tests/ -m "not fuzz"`：两轮结果稳定一致（5045 passed, 70 skipped, 2 deselected，**130 failed**，964s）。对这 130 项做了干净基线对照（`git stash -u` 后在 v1.97.15 基线上重跑同 20 个文件）：
  - **114 项为预存环境失败**，基线同样失败：① 系统 python3 3.9.6 不支持 `str | Path`/`dataclass(slots=)`（test_pii_gate 14、test_architecture_inventory 1 等，脚本内部硬编码调 `python3`）；② node_modules 缺 `js-yaml`（test_provision_parity 39、onboarding 24、distribution/doctor/golden/harness/reset/smoke 中 provisioner 相关）；③ 缺 `@nangohq/providers`（entity_pages 2 等）。本任务未触碰这些子系统。
  - **16 项为本任务新增 MCP 服务器引发的派生清单失配，已全部修复并复跑通过**：lens 富目录体系（新增 `dex-changeset-mcp` 注册表条目、重生成黄金样例 `docs/examples/dex-lens-catalog-enriched-preview.json`，仅 1 处差异；测试计数 11→12 服务/156→158 工具/21→22 注册表条目/117→118 目录条目，可用性 active 86→87）——test_lens_catalog_enriched_discovery + test_dex_lens_catalog_generation 现 **81 passed**。
  - **4 项 test_smoke 失败为"未提交状态门"，非代码缺陷**：smoke 运行器快照明细取 `git ls-tree HEAD`（仅已提交），而测试发布夹具取 `git ls-files`（含暂存区）；已用插桩证明两边差异集合恰好是本任务 7 个已暂存未提交的新运行时文件（core/changesets/ 6 个 + core/mcp/changeset_server.py），文件字节/可执行位比对本身全部一致。提交后两侧集合一致即通过（CI 正常场景）。用户未要求提交，故本地保留该状态。
- Node 门（同样受预存缺模块影响，非本任务回归）：`npm run test:hooks` **EXIT=1**（日志 27 处 `Cannot find module 'js-yaml'`）；`npm run test:scripts` **EXIT=1**（js-yaml，16 处）；`npm run test:integrations` **EXIT=1**（`Missing dependency '@nangohq/providers'`；本任务未触碰 core/integrations）。

---

## Issue I-1: 健康 PREPARED 组被误报 task-occurrence-set-drift（读取端切片集合错误）
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 6
- **Discovered By**: Review R1（F1，实证复现）
- **Description**:
  - prepare 的 before/after 任务出现映射覆盖**全部目标文件**（含字节不变文件，见 coordinator.prepare 未变化分支），但 `ChangeSet.relatives` 仅由 intents（变化文件）派生。
  - [core/changesets/consistency.py](file:///Users/kkcarrot/swe-project/Dex_fork1/core/changesets/consistency.py) 基线核对用 `set(change.relatives)` 切片 live 出现表，导致"未变化但含任务锚点（常见为已 wikilink 的 `[[^task-...]]`）"的目标在健康 pending 态恒判 missing-file → 误报 task-occurrence-set-drift；该 kind 在 GATED_CONFLICT_KINDS 内，连带让守护进程在整个 prepare 窗口对全库误拦扇出。
  - 复现：两目标，Tasks.md 含裸锚点（会变），另一文件仅含已 wikilink 锚点（converter 输出不变）→ prepare 成功（1 changed of 2）→ consistency_scan 立即 status=conflict。
- **Acceptance Criteria Addressed**: AC-8、AC-9、FR-8、FR-10
- **Test Requirements**:
  - `rule` TR-I-1.1: 新增回归用例——prepare 目标中含"仅携带已 wikilink 锚点、字节不变"的文件时，commit 前 consistency_scan 为 `pending`（无 set/count drift 冲突），commit 后为 `clean`；现有 set/count drift、mixed、部分扇出、指纹漂移夹具全部继续通过。
  - `rule` TR-I-1.2: 同一健康 pending 组下，fanout_gate 对**无关任务/无关路径**返回 allowed（reason=None），对组内任务/受守护文件的重叠仍返回 pending-changeset-overlap；真实 set-drift 仍返回 vault-conflict。
- **Notes**: 正确的不变量宇宙 = before/after 出现映射内文件键的并集（prepare 实际校验的集合）；在 ChangeSet 上以只读属性提供。无数据损坏风险（commit/resume 锁下 sha 复检路径不经过该切片）。
- **Completion Evidence**:
  - `rule` TR-I-1.1 **pass**：[model.py](file:///Users/kkcarrot/swe-project/Dex_fork1/core/changesets/model.py) 新增只读属性 `ChangeSet.occurrence_relatives`（before/after 出现映射全部内文件键的并集，即 prepare 实际校验的不变量宇宙）；[consistency.py](file:///Users/kkcarrot/swe-project/Dex_fork1/core/changesets/consistency.py) 基线切片改用该集合，信封新增 `occurrence_relatives` 字段。新增回归 `test_unchanged_target_with_task_id_is_not_false_conflict`（未变化的已 wikilink 目标：prepare(files_changed=1)→scan=pending、conflicts=[]→commit 后 clean）；既有 set/count drift、mixed、部分扇出、指纹漂移、撕裂日志全部继续通过。
  - `rule` TR-I-1.2 **pass**：`pending_overlap` 路径重叠改为 relatives∪occurrence_relatives；健康 pending 组下无关任务/路径 gate=allowed/reason=None、组内任务=pending-changeset-overlap；新增 `test_occurrence_drift_in_unchanged_scope_file_still_gates`（未变化范围文件丢锚点→set-drift→无关扇出也 vault-conflict；修复前该漂移因不在 intents 集而完全漏检）。
  - 结果：test_changeset_consistency.py **13 passed**；全部特性文件回归 **363 passed**（63.31s）；GetDiagnostics []。

## Issue I-2: 迁移重跑闸门对无关裸 mixed-task-state 误触发并输出虚假文案
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 9
- **Discovered By**: Review R1（F2，实证复现）
- **Description**:
  - [core/changesets/cli.py](file:///Users/kkcarrot/swe-project/Dex_fork1/core/changesets/cli.py) `pending_guidance` 的谓词在"无任何变更组、仅裸 mixed-task-state 冲突"时也返回指引；迁移脚本据此退出并打印"上次迁移未完成/暂存已保留/照上面的命令"等与事实不符且无实际出口的文案。
  - 这与 consistency 模块自身的设计判定（裸 mixed 是守护进程将要收敛的正常 pre-sync 态，GATED_CONFLICT_KINDS 刻意不含 mixed-task-state）自相矛盾，会把从未迁移过的用户卡死。
- **Acceptance Criteria Addressed**: AC-11、AC-U1、FR-10、FR-11
- **Test Requirements**:
  - `rule` TR-I-2.1: 新增回归用例——无变更组、仅两镜像页同一任务勾选状态不一致时，`pending_guidance` 返回 None（迁移正常进入确认/prepare 流程）；既有"存在未决组时重跑只给 resume/rollback 指引、不开第二组"用例继续通过。
  - `rule` TR-I-2.2: 存在可归因于变更组的冲突（fingerprint-drift/interrupted/set/count drift，带 changeset_id）时，无未决组也仍给出指引且文案与实际状态相符；裸 mixed 永不产生"previous migration did not finish"文案（文案级 grep 断言）。
- **Completion Evidence**:
  - `rule` TR-I-2.1 **pass**：[cli.py](file:///Users/kkcarrot/swe-project/Dex_fork1/core/changesets/cli.py) `pending_guidance` 谓词收紧为"存在非终态组 **或** 带 changeset_id 且 kind∈GATED_CONFLICT_KINDS 的可归因冲突"才拦截；裸 mixed 不命中 → 返回 None。新增 `test_pending_guidance_ignores_bare_mixed_task_state` 与端到端 `test_bare_mixed_task_state_does_not_block_first_migration`（真实子进程跑迁移：returncode=0、无 "did not finish" 文案、锚点正常转换）；既有"中断后重跑不开第二组"用例继续通过。
  - `rule` TR-I-2.2 **pass**：新增 `test_pending_guidance_blocks_on_attributable_conflict_without_pending_group`（损坏事件日志→无可摘要未决组但有 interrupted 可归因冲突：仍给指引、含 cs_id、无"did not finish"、文案为"unresolved file state"）。顺手修复 advisory F4：冲突屏对 prepared 组不再输出"files exactly as you left them"矛盾安抚；fingerprint-drift 的 next command 改为 rollback（resume 对漂移必拒），interrupted-changeset 改指引 status。另修复 advisory F5：stage_blob 写入循环处理短写。
  - 结果：test_changeset_cli.py **13 passed**、test_obsidian_wikilink_migration.py **5 passed**；全部特性文件回归 **363 passed**（63.31s）；GetDiagnostics []。
