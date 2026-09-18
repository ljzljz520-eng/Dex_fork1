# 批量 Markdown 变更的两阶段协调协议（Changeset Protocol）- 产品需求文档

## Overview

- **Summary**：为"批量转换 Markdown 文件"这一类操作建立一套协调协议：每个文件变更携带逐文件内容指纹（sha256）与任务 ID 出现位置不变量；先在暂存区完成全部转换与校验，再在库级变更锁保护下通过既有事务核心把整组文件原子替换；全过程写入持久化待处理日志，进程在任意时刻中断后都可以显式"继续完成"或"回滚"。读取端通过核心库与只读 MCP 工具显式暴露 `clean / pending / conflict` 状态，绝不静默接受部分扇出。
- **Purpose**：现有批量转换（`core/obsidian/migrate_to_wikilinks.py`）在一个循环里逐文件原地写，中途失败/断电会留下"部分文件已转换、部分未转换"的半扇出状态；任务状态扇出写入 `update_task_status_everywhere()` 同样逐文件独立写。当前没有任何读取端能发现跨文件不一致。本协议把这类操作收敛到"一扇安全门 + 显式冲突可见"。
- **Target Users**：
  - 直接用户：运行 Obsidian 迁移（及未来其他批量 Markdown 转换）的 Dex 用户，尤其是转换过程中遇到中断的非技术用户。
  - 机器消费者：Obsidian 同步守护进程、Work MCP 读取路径、Doctor/排障流程。

## Goals

- 批量转换"先全量暂存并校验、再整组生效"，任何时刻都不存在被静默接受的半扇出。
- 每个文件的变更意图都有旧/新内容指纹，且任务 ID（`^task-YYYYMMDD-XXX`，含 `[[^...]]` 形式）在转换前后的**文件集合与每文件出现次数**保持不变。
- 待处理变更组持久化、抗崩溃（fsync、原子状态文件、容忍日志撕裂尾），中断后支持显式续提交或回滚，默认不自动提交、不自动回滚。
- 读取端（核心库 + 只读 MCP 工具 + Obsidian 守护进程）显式暴露冲突：哪个任务 ID、哪种不一致、涉及哪些文件，一目了然。
- 首个完整消费者：Obsidian wikilink 批量迁移。协议本身保持通用，可供后续其他批量转换复用。

## Non-Goals

- **不**改造 `update_task_status_everywhere()` 等既有任务扇出**写入**路径（本次只保证读取端能把它们造成的不一致报为冲突；写入改造以后再做）。
- 不做跨 vault / 跨机器的分布式两阶段提交；"两阶段"指单 vault 内的 prepare→commit 协调，不引入外部协调者。
- 不做转换内容的 AI 语义判断；转换函数是纯函数输入，协议只校验指纹与任务 ID 位置不变量。
- 不新增云同步、不改变 git 备份策略（迁移现有 git 备份保留）。
- 不把未完成变更组接入 Doctor 自动修复（Doctor 接线可作为后续工作；本次仅提供状态可读与 CLI 续/回入口）。

## Background & Context

- 既有事务核心 `core/transaction/engine.py` 已提供：plan 全量授权 → `System/.dex/mutation.lock`（vault 级"一次一个变更者"锁）→ 快照 → 逐文件 temp+rename → sha256 验证 → 提交，回滚字节级精确；但其 `resume()` 对未提交事务**一律回滚、不前滚**。本协议在其上叠加一层"可续提交"的持久意图层：事务层回滚后，暂存的新字节仍在，显式 `resume` 可重新提交。
- 库级锁即 `acquire_owned_lock`（`core/transaction/lock.py`），与 CJS 迁移器共享同一锁文件；commit 阶段由 `Transaction` 持有该锁即可，协议层不重复加锁以免自锁。
- 所有权契约：`vault` 类用户内容默认 `never`、`seed` 类为 `write-if-absent`。要让用户明确发起的内容转换合法地经由事务核心写入，需要仿照 `customization-migration` seam 新增一个显式 operation（`content-conversion`），仅限非符号链接、未命中硬拒绝、所有权为 `vault`/`seed` 的 `.md` 文件。
- 运行时台账目录 `System/.dex/changesets/` 需在 `core/portable_contract.py` 登记为 `runtime`（与 `System/.dex/tx`、`ledger` 同模式），并重新生成 `packages/dex-contracts/dist/portable-vault.contract.json`。
- MCP 服务器在 `core/utils/preflight.py` 有服务器映射；新只读服务器需进入 provisioning/preflight/stdio 测试，并重新生成 `docs/architecture/INVENTORY.md`（CI 漂移门）。
- 规模先例：v1.75.2 曾处理 12k 文件 vault；暂存设计必须考虑文件数量规模与磁盘占用（提交后即清理暂存新字节）。

## Functional Requirements

- **FR-1 逐文件变更意图**：每个变更组（changeset）包含若干文件意图，每条含 vault 相对路径、旧文件 sha256（前置条件）、新内容 sha256、字节大小。意图由"读当前文件 → 纯函数转换 → 计算指纹"产生。
- **FR-2 阶段一 Prepare（无锁、只读 vault、只写运行时暂存）**：运行全部转换，把所有新字节写入私有暂存区（fsync），计算并校验：(a) 每个暂存字节的 sha256 等于其声明的新指纹；(b) 每个目标当前文件的 sha256 等于旧指纹；(c) 任务 ID 出现位置不变量（见 FR-4）；(d) 路径安全（无逃逸、非符号链接、`.md`、未被硬拒绝、所有权 ∈ {vault, seed}）。全部通过才把变更组持久化为 `PREPARED`；任何一项失败则整体拒绝、不触碰 vault 内容文件。
- **FR-3 任务 ID 出现位置不变量**：从当前 vault 内容与暂存新内容分别提取每个任务 ID 的"出现位置映射"（任务 ID → {相对路径: 出现次数}，正则须同时识别 `^task-...` 与 `[[^task-...]]`）。对每个任务 ID：出现的文件集合必须相同，且每个文件内出现次数必须相同。不一致时拒绝 prepare，并报告任务 ID、差异类型（集合增减/次数差异）与涉及文件。
- **FR-4 阶段二 Commit（库级锁 + 整组原子替换）**：将全部意图构造为 `PlanEntry`（携带 `expected_current_sha256`），以 `content-conversion` operation 经 `core.transaction.Transaction` 执行；事务核心在锁下复检旧指纹、逐文件 temp+rename、验证新指纹。成功后记录 `COMMITTED` 与关联事务 id；失败时事务已自动回滚，记录失败并把状态对读取端呈现为冲突。不得新增绕过事务核心的 vault 写入路径。
- **FR-5 持久化待处理日志**：每次状态迁移先落盘（fsync 文件与父目录）后生效；状态文件用 temp+rename 原子替换，事件日志为 append-only JSONL 并容忍撕裂尾。仅靠日志 + 暂存字节即可在新进程中重建变更组状态。
- **FR-6 恢复-继续（resume）**：提供显式恢复入口。恢复时先核对：事务日志状态、live 文件指纹、暂存字节。可续提交的条件（live 仍匹配旧指纹、暂存完整、校验仍通过）满足时重新执行 commit；条件不满足时拒绝自动续提交并给出原因。任何情况下不得静默自动提交。
- **FR-7 恢复-回滚（rollback）**：提交前回滚 = 丢弃暂存、标记 `ABORTED`，vault 无任何变化；提交后回滚 = 在关联事务快照保留期内，用快照中的原字节再发起一次 content-conversion 事务还原并标记 `REVERTED`；快照已被清理时明确告知"无法自动回滚"并给出既有 git 备份指引，绝不猜测恢复。
- **FR-8 读取端一致性检查（核心库）**：只读扫描返回结构化信封：`clean`（无未完成变更组且无扇出不一致）、`pending`（存在未完成变更组但当前 live 无不一致；含可恢复的 interrupted 子态）、`conflict`（检测到 live 不一致）。冲突类型至少包含：中断的变更组、任务 ID 出现文件集合漂移、出现次数漂移、同一任务 ID 跨位置复选框状态不一致（mixed state）、指纹漂移。每个冲突携带任务 ID（适用时）、变更组 id（适用时）与涉及的相对路径列表。
- **FR-9 只读 MCP 工具**：新增只读 MCP 服务器 `dex-changeset-mcp`，至少提供"获取变更/冲突状态"工具，返回 FR-8 信封并带 `feature_status` 健康信封（ok/off/not_installed/broken/unknown 约定）。工具不得触发任何写入、续提交或回滚。
- **FR-10 守护进程闸门**：Obsidian 同步守护进程在触发自动任务状态扇出前查询一致性；当状态为 `conflict`，或存在与受影响任务 ID/文件路径重叠的未完成变更组（`pending`/interrupted）时，跳过该自动扇出并记录带人类可读信息的错误（复用既有错误上报通道），不制造新的部分扇出。
- **FR-11 迁移接入**：`migrate_to_wikilinks.py` 改为"建索引 → prepare →（既有人工确认）→ commit"，删除逐文件原地写循环；dry-run 不产生任何变更组；对未完成变更组再次运行时，给出明确的 resume/rollback 指引而不是重开一组转换。
- **FR-12 CLI 入口**：提供 `python -m core.changesets status|resume|rollback`（支持 `--json`），中断后的用户只需照一条明确指令操作。
- **FR-13 留存与清理**：提交/回滚终态后删除暂存新字节；台账按"最新 3 个终态变更组"留存（对齐事务快照 keep=3 先例），任何无法识别为终态的目录保留待 resume，绝不删除。

## Non-Functional Requirements

- **NFR-1 崩溃安全**：复用 `core/transaction/fsync.py` 的目录 fsync 与 journal/lock 既有模式；关键崩溃窗口（prepare 前、PREPARED 后、COMMITTING 中、COMMITTED 后）均有故障注入测试，恢复结果确定。
- **NFR-2 一扇安全门**：所有 vault 内容写入只经 `core.transaction.Transaction`；运行时台账 IO 仅限 `System/.dex/changesets/`，与 journal 同级模式；路径安全检查复用 `core.path_safety`。
- **NFR-3 规模与性能**：支持万级文件扫描；流式逐文件处理、无全量字节驻留；暂存磁盘占用约等于"发生改变文件"的新字节总量，终态后释放。
- **NFR-4 可理解性**：中断后面向用户的信息必须让非技术用户知道发生了什么、数据是否安全、唯一该执行的动作是什么（短步骤、可复制命令）。
- **NFR-5 可测试性**：提供确定性的崩溃注入点（与 `DEX_TX_TEST_STOP_AFTER` 同风格），恢复路径可自动化测试。
- **NFR-6 无 PII 泄漏**：状态/日志/错误中只出现 vault 相对路径、任务 ID、指纹与计数，不输出文件正文。

## Constraints

- **Technical**：
  - Python 3，纯标准库优先（与 transaction 核心一致）；MCP 服务器复用 mcp SDK 与 `core.utils.feature_status`。
  - 新 operation 与路径分类必须改源文件并重新生成 `packages/dex-contracts/dist/portable-vault.contract.json`；新 MCP 服务器需重新生成 `docs/architecture/INVENTORY.md`；生成文件不得手改。
  - CI 门：`scripts/check-portable-contract.sh`、架构漂移门、`pytest core/tests/ core/mcp/tests/ -m "not fuzz"`、MCP stdio 启动测试须通过。
  - GitHub Actions 不涉及改动；不改动发布目录结构。
- **Business**：
  - 用户明确发起的转换才可写 vault 内容（迁移脚本现有的人工确认环节继续充当授权点）；协议自身不得在无人确认时发起转换。
- **Dependencies**：
  - `core/transaction/*`（engine、lock、fsync、journal 模式）、`core/portable_contract.py`、`core/path_safety.py`、`core/utils/feature_status.py`、`core/utils/dex_logger.py`。

## Assumptions

- 任务 ID 正则 `\^task-\d{8}-\d{3,}` 在 wikilink 化后（`[[^task-...]]`）仍可匹配，因此同一提取器可用于转换前后。
- 同一批转换中单个文件至多出现一次（按相对路径去重）；转换函数对同一输入确定性输出（重复 prepare 结果一致）。
- 迁移仍保留现有 git 备份作为用户可见的额外安全网，协议不依赖 git 存在。
- 命名决策（实现时可在计划阶段复核）：模块 `core/changesets/`；台账目录 `System/.dex/changesets/`；operation 名 `content-conversion`；MCP 服务器名 `dex-changeset-mcp`。

## Acceptance Criteria

### AC-1: 逐文件指纹意图完整且准确
- **Type**: `rule`
- **Given**: 一组待转换的 Markdown 文件，其中部分文件转换后内容改变、部分不变
- **When**: prepare 运行完成
- **Then**: 持久化意图中每个变更文件都有相对路径、旧 sha256、新 sha256、大小；暂存字节的 sha256 与声明新指纹逐一相等；当前文件 sha256 与旧指纹逐一相等
- **Pass Condition**: 用测试夹具构造转换，断言意图字段与独立计算的 sha256 全部一致
- **Evidence**: `test_changeset_intents*.py` 断言输出

### AC-2: Prepare 失败时 vault 零改动
- **Type**: `rule`
- **Given**: 暂存校验中存在任一失败（指纹不符 / 任务 ID 不变量被破坏 / 路径不合法）
- **When**: prepare 中止
- **Then**: 除 `System/.dex/changesets/` 台账外，vault 内容文件字节零变化，且不持有变更锁
- **Pass Condition**: 夹具前后对内容目录做 sha 清单对比完全一致；锁文件不存在
- **Evidence**: `test_changeset_coordinator*.py`

### AC-3: 两阶段与库级锁原子性
- **Type**: `rule`
- **Given**: 一个 PREPARED 变更组
- **When**: commit 执行
- **Then**: 写入经由 `core.transaction.Transaction`（operation=`content-conversion`）；commit 期间第二个变更者获取 `System/.dex/mutation.lock` 得到 busy；commit 成功后所有目标指纹等于新指纹，或任一失败时全部目标等于旧指纹，无中间态
- **Pass Condition**: 测试中并发/顺序尝试加锁行为正确；注入单文件失败后整组回滚且逐字节等于前置快照
- **Evidence**: `test_changeset_coordinator*.py`、故障注入测试

### AC-4: 任务 ID 出现位置不变量被强制
- **Type**: `rule`
- **Given**: 转换会丢失/新增某个任务 ID 的出现文件，或改变某文件内出现次数（含代码块、`[[^...]]` 形式边界案例）
- **When**: prepare 校验
- **Then**: prepare 拒绝，错误指出任务 ID、差异类别（集合/次数）与两侧文件清单；无内容写入。合法保持位置不变的转换通过
- **Pass Condition**: 正/反夹具用例均断言；反向用例断言零内容改动
- **Evidence**: `test_changeset_occurrences*.py`

### AC-5: 待处理日志持久且抗撕裂
- **Type**: `rule`
- **Given**: 任意已记录的状态迁移与一个被截断最后一行的事件日志
- **When**: 新进程重新加载变更组
- **Then**: 状态可从状态文件+事件日志重建；撕裂尾被截断忽略而非报错；每个迁移确实先 fsync 后生效（通过故障注入验证）
- **Pass Condition**: 日志截断用例通过；各崩溃注入点后状态判定与设计状态表一致
- **Evidence**: `test_changeset_pending_log*.py`

### AC-6: 中断后可显式继续完成
- **Type**: `rule`
- **Given**: 在 PREPARED 后、COMMITTING 中（事务已被其自身恢复回滚）两个窗口分别杀掉进程
- **When**: 新进程执行 resume（且旧指纹仍匹配、暂存完整）
- **Then**: 变更组重新提交成功，全部目标为新指纹，状态 COMMITTED；无任何自动续提交发生（必须显式调用）
- **Pass Condition**: 两个窗口的故障注入用例均须显式 resume 后才到达 COMMITTED
- **Evidence**: `test_changeset_recovery*.py`

### AC-7: 中断后可显式回滚，且不猜测
- **Type**: `rule`
- **Given**: (a) PREPARED 状态；(b) COMMITTED 且关联事务快照仍在；(c) COMMITTED 但快照已按留存策略清理
- **When**: 执行 rollback
- **Then**: (a) 暂存被丢弃、状态 ABORTED、vault 无变化；(b) 经一次 content-conversion 事务还原为原字节、状态 REVERTED；(c) 明确返回"无法自动回滚"并给出 git 备份指引，不发生任何写入
- **Pass Condition**: 三种情形夹具断言
- **Evidence**: `test_changeset_recovery*.py`

### AC-8: 读取端显式暴露冲突，拒绝静默部分扇出
- **Type**: `rule`
- **Given**: 人为构造的部分扇出（同一任务 ID 在 A 文件为完成、B 文件为未完成；或某文件丢失该 ID；或文件指纹与唯一未完成变更组的旧/新指纹都不符）
- **When**: 调用一致性检查库与只读 MCP 工具
- **Then**: 信封 `status=conflict`，每个冲突含 kind、task_id（适用时）、changeset_id（适用时）、locations；无冲突且无未完成组时为 clean；仅有未完成组时为 pending 并含 interrupted 子态。信封在库返回与 MCP 工具输出中一致
- **Pass Condition**: 三类冲突夹具 + clean/pending 夹具均断言；MCP 输出可解析且与库一致
- **Evidence**: `test_changeset_consistency*.py`、`test_changeset_server*.py`

### AC-9: 守护进程在冲突/重叠挂起时停止自动扇出
- **Type**: `rule`
- **Given**: vault 处于 conflict，或存在与本次变更任务 ID 重叠的未完成变更组
- **When**: 守护进程检测到任务状态变化并准备调用扇出写入
- **Then**: 不执行扇出写入，记录带 human_message 的错误；干净状态下既有同步行为不变
- **Pass Condition**: `test_sync_daemon.py` 新增用例：冲突时零写入且有错误记录；干净夹具行为回归通过
- **Evidence**: 守护进程单测

### AC-10: 所有权契约与登记门通过
- **Type**: `rule`
- **Given**: content-conversion operation 与 System/.dex/changesets 登记
- **When**: 对各类路径求 verdict 并运行契约相关 CI 脚本
- **Then**: 仅非符号链接、未拒绝、所有权 ∈ {vault,seed} 的 `.md` 被允许；brain/generated/runtime/未分类/拒绝模式全部拒绝；`portable-vault.contract.json` 重新生成且无手改漂移；`check-portable-contract.sh` 通过
- **Pass Condition**: 契约测试新增用例全绿；脚本退出码 0
- **Evidence**: `test_portable_contract.py` 新增用例、脚本输出

### AC-11: wikilink 迁移完整走协议
- **Type**: `rule`
- **Given**: 现有迁移测试夹具
- **When**: 以正常模式与 dry-run 模式运行迁移；并在 prepare 后中断后再次运行
- **Then**: 正常模式经 prepare→commit 完成且转换结果与原纯函数输出一致；代码中不再存在对 vault 内容文件的直接逐文件写入；dry-run 不产生变更组；中断后重跑只输出 resume/rollback 指引、不新建第二组
- **Pass Condition**: 既有 `test_obsidian_wikilink_migration.py` 改造后全绿，新增中断重跑用例通过
- **Evidence**: 迁移测试输出

### AC-12: 只读 MCP 服务器合规
- **Type**: `rule`
- **Given**: 新服务器完成注册
- **When**: 运行 MCP 注册表/stdio 启动/健康信封测试与架构清单生成
- **Then**: 服务器出现在 preflight 映射与 provisioning 中；stdio 可启动并列出工具；工具输出含 feature_status 信封；INVENTORY 重新生成后 CI 漂移门通过
- **Pass Condition**: 对应测试与生成器退出码 0
- **Evidence**: `test_mcp_servers*.py`/stdio 测试、INVENTORY diff 为空（仅预期新增条目）

### AC-13: 崩溃窗口全覆盖的回归套件
- **Type**: `rule`
- **Given**: 确定性崩溃注入点（prepare 前、PREPARED 后、COMMITTING 中、COMMITTED 记录后）
- **When**: 逐点注入并由新进程恢复
- **Then**: 每个窗口的恢复结果与状态表一致（无半态、无丢失台账、无错误清理），全量非 fuzz pytest 通过
- **Pass Condition**: 注入点用例全绿；`pytest core/tests/ core/mcp/tests/ core/migrations/tests/ -m "not fuzz"` 退出码 0
- **Evidence**: CI/本地测试输出

### AC-U1: 中断后信息对非技术用户可理解
- **Type**: `rubric`
- **Dimension**: 恢复可理解性
- **Scale**: 1-5
- **Anchors**: 1 = 仅技术栈日志，用户不知数据是否安全；3 = 有状态说明但动作不唯一或含术语；5 = 用平实语言说明"改动暂存中/已完成/已撤销、文件未受损"，并给出恰好一条可复制的续/回命令
- **Pass Threshold**: >= 4
- **Evidence**: CLI 与迁移中断文案评审（对照 NFR-4）

### AC-U2: 与既有事务/契约架构的一致性
- **Type**: `rubric`
- **Dimension**: 设计贴合度
- **Scale**: 1-5
- **Anchors**: 1 = 另起锁/日志/写入机制，重复造轮子或绕开安全门；3 = 复用部分原语但有可避免的重复；5 = 提交路径只经 Transaction，锁/fsync/路径安全/留存全部复用既有模式，新代码只承载意图、暂存、校验、协调、读取一致性
- **Pass Threshold**: >= 4
- **Evidence**: 代码审查（模块边界、调用关系）

### AC-U3: 万级文件规模下的表现
- **Type**: `rubric`
- **Dimension**: 性能与资源
- **Scale**: 1-5
- **Anchors**: 1 = 全量字节驻留内存或重复全库扫描多次；3 = 可完成但有明显冗余扫描/磁盘翻倍；5 = 流式处理、内容只存盘不常驻、暂存终态即释放，12k 文件夹具下 prepare+commit 耗时与磁盘占用在合理预算内
- **Pass Threshold**: >= 4
- **Evidence**: `test_large_vault_performance.py` 风格的规模用例计时/占用记录

## Open Questions

无阻塞性开放问题（范围已确认：通用协议 + 迁移首个消费者；库+状态记录+只读 MCP；保留 pending 显式续/回；任务 ID 不变量=文件集合+每文件次数）。命名决策见 Assumptions，可在计划阶段复核。
