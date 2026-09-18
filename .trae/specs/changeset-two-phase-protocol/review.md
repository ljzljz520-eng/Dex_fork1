# 批量 Markdown 变更的两阶段协调协议（Changeset Protocol）- Independent Review

- [x] CP-R1: 两阶段协议核心正确（逐文件指纹意图 / prepare 失败零触碰 / commit 经 Transaction 与库级锁原子性 / 任务 ID 出现位置不变量）
  - **Type**: `rule`
  - **Covers**: AC-1、AC-2、AC-3、AC-4、FR-1、FR-2、FR-3、FR-4
  - **Evidence**: R1 pass——评审者读码确认 prepare 失败分支 vault 零触碰；commit 经 `Transaction._begin_with_id(operation="content-conversion")`，锁由事务持有；特性子集 324 passed。

- [x] CP-R2: 持久化待处理日志与崩溃恢复确定（日志撕裂容忍；prepare 前 / PREPARED 后 / COMMITTING 中 / COMMITTED 后四窗口；显式 resume；rollback 三情形含快照缺失不猜测）
  - **Type**: `rule`
  - **Covers**: AC-5、AC-6、AC-7、AC-13、FR-5、FR-6、FR-7、NFR-1、NFR-5
  - **Evidence**: R1 pass——复用 Journal sha 链/撕裂截断/fsync；resume 五分支无静默自动提交；真实子进程 exit 137 四窗口测试通过；快照缺失零写入有测试。

- [x] CP-R3: 读取端显式暴露冲突且守护进程停止部分扇出（信封 clean/pending/conflict 与五类 conflict；库与 MCP 输出一致；守护进程 conflict/重叠时零扇出写入并留 human_message；信封无绝对路径与正文）
  - **Type**: `rule`
  - **Covers**: AC-8、AC-9、FR-8、FR-9、FR-10、NFR-6
  - **Evidence**: **R1 fail → F1**：健康 PREPARED 组因基线切片只含 intents（变化文件）而把"未变化但含任务 ID 的目标文件"误报 missing-file / task-occurrence-set-drift；该冲突在 GATED_CONFLICT_KINDS 内，连带误锁守护进程扇出。已实证复现。→ Issue I-1。**R2 pass（翻案）**：切片宇宙改为 occurrence_relatives（before/after 映射文件键并集）；评审者独立构造场景验证健康 pending 无冲突、未变化范围文件真实漂移仍检出并 gate、库↔MCP 信封一致且无正文/绝对路径。

- [x] CP-R4: 所有权契约、路径登记与只读 MCP 合规（content-conversion 终局分支仅放行非符号链接/未拒绝/所有权∈{vault,seed} 的 .md；dist 重生成；check-portable-contract 通过；服务器注册/preflight/stdio/feature_status；INVENTORY 无漂移；服务器无任何写入/续/回能力）
  - **Type**: `rule`
  - **Covers**: AC-10、AC-12、NFR-2
  - **Evidence**: R1 pass——check-portable-contract EXIT=0（2153 paths）；INVENTORY 等价校验无差异；changeset_server.py 无 prepare/commit/resume/rollback import，物理只读。

- [x] CP-R5: wikilink 迁移与 CLI 完整接入（prepare→人工确认→commit；无逐文件直接写；dry-run 无组；中断重跑只给指引不开第二组；git 备份先于 prepare；CLI status/resume/rollback 退出码与 --json；终态清理与 keep=3 留存）
  - **Type**: `rule`
  - **Covers**: AC-11、FR-11、FR-12、FR-13
  - **Evidence**: R1 pass（附 F2 缺陷，单列 Issue I-2 关联本 CP/AC-11）：协议主通道、无直写、备份顺序、dry-run、中断重跑、CLI 退出码均验证；但迁移重跑闸门对"无变更组的裸 mixed-task-state"误触发并输出虚假"上次迁移未完成"文案。→ Issue I-2，修复后 R2 复检。

- [x] CP-R6: 规模与全门回归（12k 流式规模用例；特性子集全绿；契约门/架构门/GetDiagnostics；全量门失败全部归因于预存环境或已修复项，无未解释回归）
  - **Type**: `rule`
  - **Covers**: AC-13、NFR-3
  - **Evidence**: R1 pass——评审者独立复跑特性子集 324 passed；独立核实 130 全量失败归因（114 预存基线同样失败；12 lens 已由本改动修复并复跑通过；4 smoke 为未提交状态门，差集机制经读码+复现确认提交后自愈）；契约门/INVENTORY/GetDiagnostics 干净。

- [ ] CP-U1: 中断后信息对非技术用户可理解
  - **Type**: `rubric`
  - **Covers**: AC-U1、NFR-4、TR-9.3
  - **Scale**: 1-5
  - **Anchors**: 1 = 仅技术栈日志，用户不知数据是否安全；3 = 有状态说明但动作不唯一或含术语；5 = 平实语言说明"改动暂存中/已完成/已撤销、文件未受损"，并给出恰好一条可复制的续/回命令
  - **Pass Threshold**: >= 4
  - **Evidence**: R1 评分 **4/5（过阈值）**：评审者实跑三态文案，pending/conflict 均有平实安抚与可复制命令；扣分点：主命令+rollback 替代行并存、冲突行暴露 kind 技术串、conflict 屏顶部安抚文案与真实漂移矛盾（advisory F4）、F2 虚假文案（已立 Issue I-2，修复后 R2 重新评分）。**R2 评分 4/5（维持，过阈值）**：F2/F4 修复后实跑四态文案，虚假"上次迁移未完成"已消除、漂移组改指 rollback，可理解性提升；未给 5 的原因：每组仍主+替两条命令、冲突行保留 kind 技术串，且新发现 F7/F8/F9 三处异常态文案/恢复出口瑕疵（均 advisory，不阻塞，建议纳入 dex-cards 后续打磨）。

- [x] CP-U2: 与既有事务/契约架构一致
  - **Type**: `rubric`
  - **Covers**: AC-U2、TR-5.6
  - **Scale**: 1-5
  - **Anchors**: 1 = 另起锁/日志/写入机制，重复造轮子或绕开安全门；3 = 复用部分原语但有可避免的重复；5 = 提交路径只经 Transaction，锁/fsync/路径安全/留存全部复用既有模式，新代码只承载意图、暂存、校验、协调、读取一致性
  - **Pass Threshold**: >= 4
  - **Evidence**: R1 评分 **5/5**：提交路径只经 Transaction；锁/fsync/Journal/Snapshot/路径安全/keep=3 全复用；确定性 `conv-<cs_id>` tx_id 接缝干净。

- [x] CP-U3: 万级文件规模下的表现
  - **Type**: `rubric`
  - **Covers**: AC-U3、TR-10.2
  - **Scale**: 1-5
  - **Anchors**: 1 = 全量字节驻留内存或重复全库扫描多次；3 = 可完成但有明显冗余扫描/磁盘翻倍；5 = 流式处理、内容只存盘不常驻、暂存终态即释放，12k 文件夹具下 prepare+commit 耗时与磁盘占用在合理预算内
  - **Pass Threshold**: >= 4
  - **Evidence**: R1 评分 **4/5（过阈值）**：峰值约 2.26MiB、prepare+commit 6.58s、暂存精确并终态释放；扣分：25MiB 预算偏松、tracemalloc 未包 commit、fanout_gate 每次全库 rglob 无缓存（advisory）。

## Review History

### Review R1（全新上下文独立评审，只读）
- **Result**: `fail`
- **Evidence**:
  - 评审者独立运行：特性子集 15 文件 `.venv/bin/python -m pytest …` → 324 passed in 62.91s；`check-portable-contract.sh` EXIT=0（2153 paths）；INVENTORY 等价 diff 为空；独立复现并归类 smoke 4 失败（git ls-tree HEAD vs git ls-files 机制差，提交后自愈）；GetDiagnostics []；新代码仅 stdlib + core.* + mcp SDK。
  - 两条 actionable 均以独立构造的最小用例实证复现：
    - **F1（medium，→ Issue I-1，CP-R3）**：健康 PREPARED 组被误报 task-occurrence-set-drift。根因 [consistency.py:235](file:///Users/kkcarrot/swe-project/Dex_fork1/core/changesets/consistency.py#L235) 用 intents 派生集（仅变化文件）切片 live 出现表，而 prepare 的 before/after 出现映射覆盖全部目标（含未变化文件，[coordinator.py:237-242](file:///Users/kkcarrot/swe-project/Dex_fork1/core/changesets/coordinator.py#L237-L242)）；未变化但含任务锚点的目标在健康 pending 态恒为 missing-file，并因属于 GATED_CONFLICT_KINDS 连带误锁守护进程扇出。无数据损坏（commit/resume 锁下 sha 复检不受影响）。
    - **F2（medium，→ Issue I-2，CP-R5/AC-11/AC-U1）**：`pending_guidance` 在"无变更组、仅裸 mixed-task-state"时仍拦截迁移重跑，输出"上次迁移未完成/暂存已保留"等与事实不符且无出口的文案；与本仓库自己对裸 mixed 态"正常 pre-sync 态、不拦截"的设计判定矛盾。
  - Advisory（不阻塞，留档）：F3 spec FR-10/AC-9 未明文记录"裸 mixed 不拦"细化；F4 conflict 屏顶部 prepared 安抚文案与真实漂移矛盾、fingerprint-drift 一律推荐 resume 需两步；F5 [storage.py](file:///Users/kkcarrot/swe-project/Dex_fork1/core/changesets/storage.py) 单次 os.write 未循环短写；F6 人工确认在 prepare 之前（沿用旧脚本顺序）与 FR-11 字面顺序不符但满足实质授权。
- **Route**: 返回 Implement；I-1、I-2 已在 tasks.md 物化为 pending Issue，修复并补回归后由全新评审者执行 R2。

### Review R2（全新上下文独立评审，只读；Issue I-1/I-2 修复后复审）
- **Result**: `pass`（无 actionable；CP-R1..R6 全 pass；CP-U1 4/5、CP-U2 5/5、CP-U3 4/5，均过阈值）
- **Evidence**:
  - 评审者独立复跑特性 18 文件 → **363 passed in 64.03s**；契约门 EXIT=0（2153 paths）；INVENTORY 等价 diff 干净；GetDiagnostics []；抽查 smoke 状态门机制确认 4 失败为未提交状态门。
  - **F1（I-1）修复核验通过**：独立库级探针验证 occurrence_relatives 恰为 before/after 文件键并集（空/单边/去重排序边界），健康 pending 无冲突且无关扇出 allowed、组内任务仍 overlap 拦截；未变化范围文件真实漂移仍 set-drift 并 vault-conflict 拦截无关扇出（修复前漏检）；路径守卫为旧集超集只会更严，无死锁/数据风险；信封新字段仅相对路径。
  - **F2（I-2）修复核验通过**：裸 mixed 无组时 pending_guidance=None，真实子进程首迁移完成（无"did not finish"、转换落地）；损坏事件日志（无可摘要未决组、仅可归因 interrupted 冲突）仍拦截且文案属实含 cs_id；四态 status 文案实读，漂移组 resume 安全拒绝后 rollback 成功。
  - 回归断言强度：5 个新增回归（2 一致性 + 2 CLI + 1 迁移端到端）均断言真实行为与文案，评审者独立场景全部复现一致。
- **Advisory（不阻塞，留档；建议 F7/F9 纳入 dex-cards 后续打磨，不在本公开仓库开 issue）**：
  - **F7（中低）**：不可读（非撕裂性损坏）事件日志为用户侧恢复死路——interrupted 行 next command 自指 status，resume/rollback 未捕获 ChangeSetStorageError 而抛含绝对路径的原始 traceback，prepare 又永久拒绝。超出崩溃模型（崩溃只能产生可自愈撕裂尾），无数据安全影响。
  - **F8（低）**：preparing 崩溃壳文案矛盾（"interrupted partway" 紧接 "Nothing was applied"）、rollback 命令重复两遍；主/替同一条命令。安全与命令正确。
  - **F9（低中）**：F4 修复在迁移重跑屏不完整——pending_guidance 调 `_render_group_block(summary)` 漏传 `under_conflict`（[cli.py:268-269](file:///Users/kkcarrot/swe-project/Dex_fork1/core/changesets/cli.py#L268-L269)），漂移组仍显示"files exactly as you left them"并主推必败的 resume；resume 零写入安全失败后才给 rollback。
  - **F10（信息）**：显式 resume 的锁下复检只含 intents 指纹+暂存完整性，不含 occurrence 基线；未变化范围文件锚点被删时 resume 仍提交（该文件不在事务计划内、提交不覆盖用户改动，无损坏）；与 FR-6 字面表述有差距，建议记录设计决定或补基线。
  - R1 的 F3、F6 继续留档（spec 措辞类）。
- **Route**: 评审通过，收尾。本任务全部 10 个 Task + Issue I-1/I-2 完成；改动未提交（等待用户决定）。
