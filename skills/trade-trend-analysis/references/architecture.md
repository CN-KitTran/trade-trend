# V3 运行架构

设计版本：V0.53。此文件与同目录判断、证据和报告规则共同构成当前规格，不再依赖缺失的外部规格文件。

## 产物链

```text
gateway envelopes
  -> CLOSE_FREEZE manifest + raw envelopes + universe + observations
  -> PREOPEN 全市场 sensing input/output
  -> market regime + capital migration context
  -> regime-aware review cases
  -> frozen evidence + integrated theme judgments
  -> ledger + report_projection
  -> deterministic preview/report
```

市场状态层必须在全市场感知完成后、定向查证和正式判断前运行。它读取完整主题卡、统一宽基、广度、活跃度及风格对比，不能按涨跌榜、故事或预设数量先过滤主题。它只提供上下文和资金迁移解释，不能一票否决逆环境独立机会。

市场状态层不做新闻追踪或复杂宏观建模，由 LLM 在无固定阈值下给出：市场状态、风险偏好、主要流出方、主要承接方、持续阶段、矛盾证据和置信度。它还可把已被全市场感知标为 WATCH、但因资金迁移需要立即查证的轴加入 `regime_review_nominations`；不能从 NONE 直接升级，也不能直接形成正式机会或风险。正式 review plan 是原 CANDIDATE 轴与合法 nomination 的确定性并集，正式主题判断再据此标注板块角色、机会驱动和风险类型。

根 JSON 产物均含 `artifact_kind/schema_version/schema_ref/producer_version/created_at`，并以 canonical JSON SHA-256 追踪。reader 对未知 schema 失败，不原地迁移历史。

同一 D、同一 release mode、完全相同 input hashes 的重复 CLOSE_FREEZE 幂等复用原 vN；任何不同输入默认拒绝。只有显式提供 amendment 原因才可新增版本并引用前版，绝不静默覆盖。PREOPEN 只能消费与自身相同 release mode 的冻结，禁止 INTERNAL/SHADOW 产物流入 OFFICIAL。

## 三类依赖

1. 核心数据：L60 板块帧、统一宽基、收盘认证，以及与其 envelope hash 互锁的同日收盘身份或 D 收盘至 D+1 竞价前决策窗口当前关系身份；缺任一项全局 `INVALID`。决策窗口身份不是 D 日历史成分。
2. 运行设施：版本化交易日历、D+1、竞价前截止、正式窗口、原子发布锁；缺失不能正式发布。
3. 条件模块：股票帧只关闭相应股票模块；午盘帧只关闭午盘。不能反向替代板块帧。

V0.53 将“完整目录”与“每个源都有当日方向权限”拆开：若最新日仅 1 个 PRIMARY 源出现已明确登记的 `MISSING_UP`，保留原始空值，以它为固定代理的主题强制 `NO_DIRECTION`，不进入主题卡、横截面分位、市场状态、候选、查证或报告结论；其余主题继续。不补 0、不换代理、不沿用昨日。同类缺失超过 1 个，或任何日期、目录、宽基、价格、成交核心缺口，仍全局失败关闭。报告必须显示局部数据限制并标记 `PARTIAL`。

## 隔离

| 模式 | 工程根 | 可写正式报告 |
|---|---|---|
| INTERNAL_GATE | `data/internal/` | 否 |
| SHADOW | `data/shadow/` | 否 |
| OFFICIAL | `data/` | 只有切换确认、窗口和锁均通过后 |

当前 `run_preopen.py` 直接拒绝 `OFFICIAL`。INTERNAL 先写 ledger 同目录 `preview.md`，再由同一 renderer 把完全相同且显著标限的 Markdown 写入 Vault 日报路径；这只是便于在 Obsidian 阅读，不改变 `VALIDATED_NOT_PUBLISHED`，也不得冒充正式/SHADOW 发布。SHADOW 仍只写自身目录。

## 当前纵切范围

已实现：网关强契约消费门、不可变 close freeze、源级计算/provenance、主题源会计、主题级唯一代理分位、感知与正式判断权限校验、所有正式证据引用字段校验、首次运行台账和同目录简洁预览。2026-08-21 L60 市场帧与 2026-08-24 竞价前决策窗口 identity 已真实互锁，内容寻址交易日历/运行配置、SOURCE_FIRST 注册表和权限矩阵已闭环，真实 `CLOSE_FREEZE PASS` 并生成 651 张 PREOPEN 主题卡；316 个初判候选轴已完成全局只降级校准并通过哈希/会计校验。

已实现 Agent 驱动的全主题确定性分批、逐批哈希互锁、全 ID 机械归并，以及覆盖所有初判候选轴的一次全局只降级校准；校准不设配额/数值阈值，不得升级非候选。可审计父子/同义关系簇归并仍未实现，因此该路径必须带 `RELATION_GROUP_RECONCILIATION_NOT_IMPLEMENTED` 并最多 `PARTIAL`。

V0.52 已实现市场状态与资金迁移 artifact、WATCH-only 补选、确定性 evidence plan、逐 case 查证覆盖门、板块角色/驱动/风险类型校验和报告投影。市场状态与 sensing、正式主题判断和 ledger 均以哈希互锁；NONE 轴不能借环境层升级，空 coverage 只有在 evidence plan 本身为零 case 时才合法。

显式未实现仍包括：自动证据查询及正文快照互锁、上一正式 sensing+ledger 的强制复核/状态转移/安全承接、alert 生命周期、股票、午盘、outcomes、正式发布锁/发布器/调度。当前查证由 Agent 按确定性 evidence plan 执行，代码能证明逐 case 有查证记录和引用闭合，不能单凭 URI/手工事实证明网页正文真实。`--previous-ledger` 与 `OFFICIAL` 当前均失败关闭；缺失模块不得用假字段或旁路代替。

## 当前只允许的验证路径

1. 使用真实或合成的 CLOSE 市场帧与合法身份模式执行 `CLOSE_FREEZE`；决策窗口身份必须在实际 D+1 竞价前捕获并完整互锁。
2. INTERNAL/SHADOW 盘前骨架必须显式传 `--first-run`；这只是 bootstrap 验证，不是每天重新假装首次运行的入口。
3. 窗口前手动 `INTERNAL_GATE + EARLY_DRAFT` 在双轴/权限/证据/case 校验全部通过后，可以生成隔离的 `VALIDATED_NOT_PUBLISHED` ledger、同目录 `preview.md` 和内容相同的 Vault 日报；其信息截止为真实运行开始时刻，必须标注未生效且不能进入正式/SHADOW 状态链。非 INTERNAL 早稿仍拒绝入账。
4. `render_report.py` 必须写 ledger 相邻的 `preview.md`；只有 INTERNAL_GATE 可额外传 `--vault-root` 写入受控 Vault 相对路径，且同名不同内容失败关闭。

一旦存在上一正式运行，当前纵切不得继续出新台账，直到连续性模块能够从上一 `sensing.json + ledger.json` 机械生成 mandatory review、逐轴变化和失败安全承接。
