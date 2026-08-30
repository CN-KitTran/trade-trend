# 全市场候选降级校准（输入不可变）

你只复核初判为 `CANDIDATE` 的主题—轴是否真的值得**立即进入定向查证**。这不是第二次盲扫，也不是固定配额筛选。

`CANDIDATE` 必须同时满足：结构相对全市场及同层普通共同波动具有可辨识的独特性；价格、相对强弱、广度、关注度和路径形成多维一致或有明确可解释的分歧；现在进行定向查证有现实可能改变正式机会阶段或风险等级。普通宽基共同上涨/下跌、一般强弱、仅排名靠前或只有单日变化，应降为 `WATCH`；没有独立复核价值时降为 `NONE`。

不要设固定候选数量、比例或任何数值阈值。候选可以全部保留，也可以全部降级。不得升级任何非候选，不得删除主题，不得做父子主题自由归并。

对 packet 中每个 case 恰好输出一次并保持原顺序。只允许 `KEEP_CANDIDATE`、`DOWNGRADE_WATCH`、`DOWNGRADE_NONE`。

逐轴填写 `why_not_common_market_movement` 与 `why_immediate_verification_matters`。保留候选时必须正面说明为何不是普通共同波动、为何必须现在查证；降级时明确说明哪一条件无法成立，不能用空泛措辞冒充正面理由。

```json
{
  "calibration_input_hash": "照抄输入",
  "sensing_input_hash": "照抄输入",
  "sensing_artifact_hash": "照抄输入",
  "sensing_output_hash": "照抄输入",
  "prompt_ref": "照抄输入",
  "correction_attempts": 0,
  "cases": [{
    "case_id": "照抄输入",
    "case_input_hash": "照抄输入",
    "theme_id": "照抄输入",
    "axis": "OPPORTUNITY|RISK",
    "action": "KEEP_CANDIDATE|DOWNGRADE_WATCH|DOWNGRADE_NONE",
    "why_not_common_market_movement": "一句轴级判断",
    "why_immediate_verification_matters": "一句轴级判断"
  }]
}
```
