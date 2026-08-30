# 候选后主题判断（冻结证据包）

只处理输入 `review_theme_ids` 中的 case，且每个主题恰好输出一次。不得删除 case、补搜索、引用证据包外事实或改变数据权限。输入必须含经过哈希互锁的 `market_context`；缺失、失配或无法解释时显式 FAILED，不得伪造 `NEUTRAL` 等默认值。

分别给出 `opportunity_stage`（null/FORMING/ACTIVE/MATURE/INVALID）与 `risk_level`（null/LOW/CAUTION/HIGH/EXIT），保持两轴正交。每个机会必须处理反对证据与合理替代解释；每个 CAUTION/HIGH/EXIT 必须有独立风险证据和解除条件。正向隔夜事实尚无 A 股确认时最多 FORMING。

每个正式 case 必须输出 `market_role`、`opportunity_driver`、`risk_types`、`regime_alignment` 与 `regime_interpretation`：

- `market_role`：`REGIME_LEADER`、`RECEIVER`、`DONOR`、`INDEPENDENT`、`COUNTER_REGIME`、`CROWDED` 或 `NEUTRAL`；
- `opportunity_driver`：有机会判断时为 `INDUSTRY`、`REGIME`、`DUAL`、`EVENT` 或 `PRICE_ONLY`，不适用时为 null；`PRICE_ONLY` 不能形成正式机会；
- `risk_types`：从 `INDUSTRY`、`MARKET_TREND`、`STYLE_RETREAT`、`CAPITAL_OUTFLOW`、`CROWDING`、`EVENT_POLICY`、`MIXED` 中选择；优先列出具体类型，只有无法再拆解时使用 `MIXED`；无已识别风险时为空数组；
- `regime_alignment`：`ALIGNED`、`COUNTER`、`MIXED` 或 `NEUTRAL`；
- `regime_interpretation`：说明板块为何符合、逆于或独立于主状态。

基本面良好只能反驳产业景气恶化，不能据此否定价格、广度、关注度与资金迁移共同确认的市场风险；产业事实不强也不能单独否定已经确认的环境驱动机会，只能限制持续性和空间。市场状态不是硬过滤器。

输出 thesis、why_now、why_changed_today、pricing_judgment、evidence/counterevidence/risk refs、next_validation、机会失效/重入条件、风险解除条件和逐轴 report routing。引用只能来自冻结 market evidence catalog、`market_context` 或 `evidence.json`。每个 review case 必须能在 `evidence.json.case_coverage` 找到对应查证记录；`NO_NEW_DIRECT_FACT_FOUND` 表示已查但未发现新增直接事实，不等于未查，也不能留空证据包。

失败或无法合法判断时显式返回 FAILED；新主题为 NO_FORMAL_STATE，续踪主题为 CARRIED_FORWARD。不得生成 SAME、刷新期限或复制股票。校验纠正最多一次且输入不变。

输出 bare JSON；代码随后添加 artifact 元数据。每个主题以如下完整形状为起点，按 `review_axes` 填写对应 case。股票模块关闭，两个股票字段必须为空数组：

```json
{
  "correction_attempts": 0,
  "review_theme_ids": ["..."],
  "market_regime_ref": "照抄 market_context.artifact_hash",
  "regime_input_hash": "照抄输入",
  "themes": [{
    "theme_id": "...",
    "decision_validation_status": "VALID",
    "state_provenance": {"mode": "CURRENT_VALIDATED", "source_run_id": null},
    "opportunity_stage": null,
    "risk_level": "LOW",
    "market_role": "NEUTRAL",
    "opportunity_driver": null,
    "risk_types": [],
    "regime_alignment": "NEUTRAL",
    "regime_interpretation": "说明与当前市场状态的关系，不能使用无依据默认值",
    "thesis": "",
    "why_now": "",
    "why_changed_today": "",
    "pricing_judgment": "",
    "evidence_refs": [],
    "counterevidence_refs": [],
    "risk_evidence_refs": [],
    "key_evidence_summary": "",
    "risk_summary": "",
    "risk_evidence_summary": "",
    "next_validation": null,
    "counterevidence_assessment": "",
    "alternative_explanations": [],
    "opportunity_invalidation_or_reentry_condition": null,
    "risk_relief_condition": null,
    "verification_cases": [],
    "report_routing": {
      "opportunity": {"tier": "LEDGER_ONLY", "priority_reason": ""},
      "risk": {"tier": "LEDGER_ONLY", "priority_reason": ""}
    },
    "stock_candidates": [],
    "removed_previous_candidates": []
  }]
}
```

`verification_cases` 的单轴形状：

```json
{"axis":"OPPORTUNITY","conclusion":"VERIFIED|PARTIAL|UNVERIFIED|CONTRADICTED","evidence_for_refs":[],"evidence_against_refs":[],"limitations":[],"alternative_explanation":"","pricing_assessment":"","next_validation":""}
```

风险 case 使用 `axis=RISK`；不需要机会专属的后三个文本字段。`FORMING/ACTIVE/MATURE` 必须有机会 case，`CAUTION/HIGH/EXIT` 必须有风险 case。
