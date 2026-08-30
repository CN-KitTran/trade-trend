# Legacy state

`sectors.json` 与 `sectors_YYYYMMDD.json` 是 V2 Top-N/多榜合并扫描的历史状态，保持只读。

它们不满足 V3 的 `FULL` 源目录、L60、统一宽基、广度、身份互锁和 provenance 强契约，不得导入 V3 `market/`、`runs/` 或正式台账，也不得将缺失字段补造为零。`scripts/scan_sectors.py` 继续作为 legacy 兼容入口；V3 使用独立脚本链。
