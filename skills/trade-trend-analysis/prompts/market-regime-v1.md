# 市场状态与资金迁移判断（完整市场结构）

你在全市场感知完成后、定向查证前判断市场当前在交易什么。输入以 `theme_columns + theme_rows` 紧凑矩阵覆盖全部主题，并提供已校验双轴、宽基/主题分布摘要、广度、活跃度、风格对比及多日路径；先按列名还原每行语义，不得只看候选、涨跌榜或单日表现。市场证据引用按输入的 `market_evidence_ref_rule` 生成。

输出轻量市场上下文：

- `market_regime`：用简短自然语言描述主状态，如进攻、防御/价值、资源/通胀、政策驱动、混合过渡；
- `risk_appetite`：用简短自然语言说明风险偏好扩张、收缩或中性；
- `capital_migration`：主要资金流出与承接主题 ID 及各一句解释；没有可确认迁移时使用空数组并如实说明，不得补造方向；
- `duration`：用简短自然语言说明正在形成、已经确认或正在衰退；
- `contradictions`：不符合主状态的重要反证；
- `confidence`：`HIGH`、`MEDIUM` 或 `LOW` 及理由；
- `evidence_refs` 与 `limitations`：只引用输入市场证据目录，明确数据限制。

不要使用固定阈值，不做新闻或产业事实查证，不预测指数点位。市场状态只是正式主题判断的上下文，不能作为硬过滤器；逆环境强势与独立产业机会仍可成立。

`regime_review_nominations` 只用于补回可能被逐主题感知低估、但因资金迁移而值得查证的 `WATCH` 轴：

- 只能提名原 sensing 对应轴为 `WATCH` 的主题；
- 不得提名 `NONE`，不得删除或降级原 `CANDIDATE` review plan；
- 每项必须给出 `theme_id`、`review_axes` 和基于市场状态的具体 `rationale`；
- 无合格补选时输出空数组，不设数量或比例目标。

输出 bare JSON；代码随后添加 artifact 元数据。收到校验错误时，只能基于完全相同输入纠正一次：

```json
{
  "regime_input_hash": "照抄输入",
  "sensing_ref": "照抄 sensing.artifact_hash",
  "market_regime": "简短状态",
  "risk_appetite": "简短判断",
  "capital_migration": {
    "from_theme_ids": [],
    "to_theme_ids": [],
    "from_summary": "未确认时如实说明",
    "to_summary": "未确认时如实说明"
  },
  "duration": "简短判断",
  "contradictions": [],
  "confidence": {"level": "HIGH|MEDIUM|LOW", "reason": "具体理由"},
  "evidence_refs": ["至少一个输入市场结构证据引用"],
  "limitations": [],
  "regime_review_nominations": [{
    "theme_id": "...",
    "review_axes": ["OPPORTUNITY"],
    "rationale": "该 WATCH 轴为何因当前市场状态值得立即查证"
  }],
  "correction_attempts": 0
}
```
