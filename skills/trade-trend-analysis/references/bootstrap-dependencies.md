# 首次运行依赖 Bootstrap

`scripts/bootstrap_dependencies.py` 只把显式输入转换为内容寻址依赖，不抓行情、不推断节假日，也不把映射提案冒充运行成功。

## 输入

1. 网关 `sector_market_frame` L60 完整 envelope；
2. 可选 `sector_identity` envelope；
3. 显式、结构化三方或权威日历源：必须逐项提供升序 `trading_dates`，并至少为 D 与明确的下一交易日提供带时区 `auction_start_at`；结构化三方必须如实标为 `STRUCTURED_PROVIDER`，不能冒充权威来源或用户输入，并提供 URI、抓取时间和版本；
4. 显式运行时间配置源。可参考 `config/bootstrap-runtime-input.example.json`；示例故意使用不可执行的 `EXAMPLE_NOT_APPROVED`，复制后必须明确改为 `USER_EXPLICIT` 或 `DEPLOYMENT_CONFIG` 并更新版本。
5. 可选的概念审查 decisions。工具总会先生成内容寻址的 `concept-review-packet.json`；没有完整、显式 `REVIEWED` 的 decisions 时 bundle 只能是 `PROPOSAL_ONLY`。

日历源最小结构：

```json
{
  "source_kind": "USER_EXPLICIT",
  "source_name": "operator-reviewed-calendar",
  "source_version": "2026-08-24-v1",
  "timezone": "Asia/Shanghai",
  "market": "CN_A",
  "trading_dates": ["...按升序明确列出，至少覆盖市场帧 L60、D 和 D+1..."],
  "sessions": {
    "2026-08-21": {"auction_start_at": "2026-08-21T09:15:00+08:00"},
    "2026-08-24": {"auction_start_at": "2026-08-24T09:15:00+08:00"}
  }
}
```

## 运行

```bash
python scripts/bootstrap_dependencies.py \
  --market-frame <sector-market-frame.json> \
  --identity-frame <sector-identity.json> \
  --calendar-source <explicit-calendar-source.json> \
  --runtime-source <explicit-runtime-source.json> \
  --concept-decisions <reviewed-concept-decisions.json> \
  --output-root <reference-output-root>
```

输出包括主题注册表、概念审查包、已提供时冻结后的概念 decisions、覆盖—权限矩阵、运行配置、交易日历和 `bootstrap-manifest.json`。目录由 bundle 内容哈希命名，重复相同输入幂等复用，冲突不覆盖。

## 状态语义

- `READY`（退出码 0）：市场、身份与日历已经通过 `CLOSE_FREEZE` 同一强门，且每个 PRIMARY CONCEPT 均有与当前 review packet 互锁的显式审查 decision。行业 PRIMARY 可以依据结构类型稳定映射；概念只有显式审查为 `STABLE` 后才可稳定。
- `PROPOSAL_ONLY`（退出码 2）：仍会生成完整的离线映射提案，但所有未获强身份支持的映射保持 `PROVISIONAL`，最高只能 `WATCH`。该状态不是 CLOSE_FREEZE/PREOPEN 成功。
- `FAIL`（退出码 1）：显式日历、运行配置或市场目录自身无效，因而不生成可误用的 bundle。

## 会计与分类

- 每个 PRIMARY 必须恰好进入 `MAPPED_STABLE`、`MAPPED_PROVISIONAL` 或 `EXCLUDED_PRIMARY`；闭合失败时停止。
- theme ID 仅由 `source_sector_id` 确定，名称变化不会改变 ID。
- 概念默认 `PROVISIONAL/WATCH_ONLY`，不得只凭供应商 `CONCEPT` 类型批量升级为稳定主题。审查包提供名称、ID、成员数量、最多八个 `{code,name}` 样例以及完整 membership hash/ref。
- 明显交易属性、风格/供应商指数、持股属性、地域桶、临时事件桶和单股伪主题带确定性 exclusion flag，decision 必须为 `EXCLUDED`；其他未知类型保留 `PROVISIONAL`，不静默过滤。
- REFERENCE 和 identity 中的临时标签也进入会计，但不默认获得方向判断权限。

概念 decisions 输入至少包含：`review_status=REVIEWED`、`reviewed_by`、带时区 `reviewed_at`、当前 `review_packet_ref/market_date/catalog_version`，以及对 packet 每个概念恰好一行的 `STABLE|EXCLUDED|PROVISIONAL + reason`。可使用 [prompts/concept-bootstrap-review-v1.md](../prompts/concept-bootstrap-review-v1.md) 做离线 LLM 分类，但审查结果仍必须经过脚本会计与强制排除规则。
