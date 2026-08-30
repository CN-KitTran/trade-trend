# 板块感知与正式判断

## 感知输入

每张卡只含：稳定身份/唯一代理、1D/3D/5D/10D/20D 绝对与相对路径、同类位置（可用时）、广度、关注度、上一最小双轴、健康与权限。扩散/集中是前三项的联合解释，不是第四条独立证据。

## 市场状态与资金迁移

完成全部主题感知后，LLM 必须一次读取完整市场结构摘要，先判断市场正在交易什么，再进入候选查证。输入至少覆盖：统一全 A 路径、市场广度、活跃度、主要风格对比、领涨/领跌方向及其多日变化、主题关注度与相对强弱分布。

输出保持轻量：

- `market_regime`：进攻、 防御/价值、资源/通胀、政策驱动、混合过渡或其他自然语言状态；
- `risk_appetite`：扩张、收缩或中性；
- `capital_migration`：主要流出方向与承接方向；
- `duration`：正在形成、已确认或衰退；
- `contradictions`：不符合主状态的重要反证；
- `confidence`：LLM 对状态判断的置信度及理由。
- `regime_review_nominations`：因资金迁移需要补查的 WATCH 轴及理由。

不使用机械阈值，不把市场状态作为硬过滤器。nomination 只把感知 WATCH 轴送入查证，不能把 NONE 直接升级，也不能绕过正式证据门。逆环境强势和独立产业机会仍可保留，但必须解释为何能脱离主环境。

## 板块角色、驱动与风险归因

每个正式 review case 必须标注其在当前市场中的角色：`REGIME_LEADER`（环境主线）、`RECEIVER`（资金承接方）、`DONOR`（资金流出方）、`INDEPENDENT`（独立产业机会）、`COUNTER_REGIME`（逆环境强势）、`CROWDED`（高位拥挤）或 `NEUTRAL`。

机会驱动至少区分：`INDUSTRY`、`REGIME`、`DUAL`、`EVENT`、`PRICE_ONLY`。`PRICE_ONLY` 最多 WATCH；事件只有转化为多日市场结构后才可升级。

风险至少区分：产业景气、市场趋势、风格退潮、资金流出、高位拥挤、事件/政策或混合风险。基本面良好只能反驳“产业景气恶化”，不能反驳已经由价格、广度、关注度和市场状态共同确认的市场风险。反之，产业事实不强也不能单独否定已被完整市场结构确认的阶段性防御/风格机会，只能限制其持续性和剩余空间。

正式判断必须分别回答：板块为什么涨跌、主要驱动是什么、是否符合当前市场状态、产业事实支持还是限制、市场是否已定价、下一验证与失效/解除条件。

## 双轴

- 机会：`NONE/WATCH/CANDIDATE`；结构为 `EMERGING_STRENGTH/REACCELERATION/REVERSAL_ATTEMPT`。
- 风险：`NONE/WATCH/CANDIDATE`；结构为 `DETERIORATION/NARROWING/EXHAUSTION`。
- 两轴各自选择 `IMPULSE/PERSISTENT` 并分别引用事实。
- 代码机械派生 `NONE/WATCH/OPPORTUNITY/RISK/BOTH`，不得替模型改方向。

单日 IMPULSE 成为 CANDIDATE 时，同一轴必须同时引用 price、breadth、attention。累计涨幅、高位、单项资金、单股异动或一个分位不能单独触发。

## 正式状态

- 机会：`FORMING/ACTIVE/MATURE/INVALID`。
- 风险：`LOW/CAUTION/HIGH/EXIT`，与机会正交。
- 新事实尚无 A 股确认最多 `FORMING`；确定性正式证伪可独立升风险/EXIT。
- 机会下调不能自动降低风险；风险解除必须有独立解除证据。
- 判断失败：新主题 `NO_FORMAL_STATE`；续踪主题 `CARRIED_FORWARD`。两者都不得生成 `SAME` 或刷新复核期限。

## 模型与校验

校验只检查 schema、会计、引用、权限、状态纪律和单项证据禁止，不按数值显著性改结论。首次非法时将相同输入和错误列表交回纠正一次；不得补搜索、换证据或静默修复。
