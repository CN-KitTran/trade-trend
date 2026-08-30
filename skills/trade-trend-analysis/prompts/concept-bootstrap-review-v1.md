# 概念源首次审查 V1

你在离线审查 `concept-review-packet.json`。这不是市场方向判断，不判断涨跌，也不使用新闻。

逐项阅读 `source_sector_id/source_name/member_count/member_sample/membership_hash/screening_flags`，对每个概念恰好输出一行：

- `STABLE`：具有持续、可解释的产业链、技术、产品、供需或政策产业机制；供应商源指数可作为该主题唯一市场代理；
- `EXCLUDED`：交易属性、风格/供应商指数、持股/重仓属性、纯地域桶、短期事件桶、单股或空壳伪主题；
- `PROVISIONAL`：无法仅凭当前名称与成员样例可靠确定，保留观察但最多 WATCH。

`required_status=EXCLUDED` 的行必须排除。不要因为名字热门、近期上涨或成员数量多而升级。不要合并不同 source ID；首次 Bootstrap 保持一源一 theme ID。

输出 JSON：

```json
{
  "review_status": "REVIEWED",
  "reviewed_by": "<reviewer/model-manifest>",
  "reviewed_at": "<timezone-aware timestamp>",
  "review_packet_ref": "<packet_version>",
  "market_date": "<packet market_date>",
  "catalog_version": "<packet catalog_version>",
  "decisions": [
    {
      "source_sector_id": "...",
      "status": "STABLE|EXCLUDED|PROVISIONAL",
      "reason": "一句可审计理由",
      "reason_codes": []
    }
  ]
}
```
