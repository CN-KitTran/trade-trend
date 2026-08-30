# INTERNAL_GATE 首次运行 Runbook

本 Runbook 只用于首次内部闭环。它不启用 OFFICIAL、股票、午盘或上一台账承接。

## 1. 确定 D 与决策窗口

从已冻结的版本化交易日历取当前竞价前最近一个已完成交易日 `D` 和下一实际交易日 `D1`。周一盘前的正常组合是“周五 D 收盘行情 + 周五收盘后至周一 09:15 前捕获的当前身份”。身份帧必须明确使用 `DECISION_WINDOW_CURRENT_RELATION`，不能称为周五历史成分。

```bash
python3 ~/.claude/skills/trade-data-gateway/gateway.py sector-market-frame \
  --date "$D" --lookback 60
python3 ~/.claude/skills/trade-data-gateway/gateway.py sector-identity \
  --date "$D" --decision-date "$D1" --next-auction-at "${D1}T09:15:00+08:00"
```

## 2. 首次依赖 Bootstrap

使用 `scripts/bootstrap_dependencies.py`。没有概念语义审查时只会产生 `PROPOSAL_ONLY` 和 `concept-review-packet.json`；必须逐一审查所有 PRIMARY CONCEPT 后以 `--concept-decisions` 重跑。行业可结构性一对一稳定绑定；概念不能仅凭供应商类型成为稳定主题。所有源必须稳定映射、保留 PROVISIONAL 或显式排除。

Bootstrap 成功必须为 `READY`，并产出内容寻址的：

- `trading-calendar.json`
- `runtime-config.json`
- `theme-registry.json`
- `coverage-permission-matrix.json`

## 3. CLOSE_FREEZE

```bash
python3 scripts/freeze_market.py \
  --market-frame "$MARKET" --identity-frame "$IDENTITY" \
  --trading-calendar "$READY/trading-calendar.json" \
  --theme-registry "$READY/theme-registry.json" \
  --permission-matrix "$READY/coverage-permission-matrix.json" \
  --release-mode INTERNAL_GATE
```

只接受 `CLOSE_FREEZE PASS`。失败记录不能作为分析输入。

## 4. 全主题感知

先生成 DRAFT：

```bash
python3 scripts/run_preopen.py \
  --freeze-dir "$FREEZE" --theme-registry "$READY/theme-registry.json" \
  --permission-matrix "$READY/coverage-permission-matrix.json" \
  --runtime-config "$READY/runtime-config.json" --decision-date "$D1" \
  --first-run --release-mode INTERNAL_GATE
```

由于全主题卡超过普通单次上下文，技术上按稳定卡顺序等量分批，不做候选过滤：

```bash
python3 scripts/model_io.py sensing-batches \
  --sensing "$DRAFT/sensing.json" --output-dir "$WORK/sensing-batches" --batch-size 60
```

每个批次使用同一 `prompts/sensing-v1.md`，只处理该批 `batch_theme_ids`；可由 Agent 子任务并行生成 `batch-NNN.output.json`。每批必须回显 `batch_input_hash`，校验失败只能在完全相同 input 上纠正一次。随后机械闭合全部主题：

```bash
python3 scripts/model_io.py merge-sensing \
  --sensing "$DRAFT/sensing.json" --outputs-dir "$WORK/sensing-batches" \
  --batch-size 60 --output "$WORK/sensing-output.json"
```

机械合并后，必须对所有初判 `CANDIDATE` 轴执行一次单一全局只降级校准；它不设候选数量、比例或数值阈值，只判断该轴是否具有区别于宽基共同波动/一般强弱的独立结构，且现在查证可能改变正式判断：

```bash
python3 scripts/model_io.py calibration-packet \
  --sensing "$DRAFT/sensing.json" --sensing-output "$WORK/sensing-output.json" \
  --output "$WORK/calibration-packet.json"
# 单次模型调用按 prompts/sensing-calibration-v1.md 生成 calibration-output.json
python3 scripts/model_io.py apply-calibration \
  --sensing "$DRAFT/sensing.json" --sensing-output "$WORK/sensing-output.json" \
  --calibration-output "$WORK/calibration-output.json" \
  --output "$WORK/sensing-output.calibrated.json"
```

全局校准只能 `KEEP_CANDIDATE/DOWNGRADE_WATCH/DOWNGRADE_NONE`，不能升级、漏轴或自由删除父子主题。当前仍没有可审计关系簇归并，产物必须保留 `RELATION_GROUP_RECONCILIATION_NOT_IMPLEMENTED` 且最多 `PARTIAL`。带校准输出再次执行 `run_preopen.py --sensing-output ...`，取得 `READY_FOR_THEME_REVIEW` 新 run 目录。

## 5. 市场状态、定向查证、台账与预览

先检查 READY run 的 `run_window_status`。窗口前手动 `INTERNAL_GATE + EARLY_DRAFT` 可以继续本节，生成隔离的 internal ledger、同目录 `preview.md` 与内容相同的 Vault 日报；其 `information_cutoff` 必须等于真实运行开始时刻，报告必须标注“内部早稿、未生效”。Vault 存储不使它成为 SHADOW/OFFICIAL 基准，也不能在窗口开始后自动升格。`LATE_REJECTED` 或非 INTERNAL 的早稿必须停止。

先对全部已判断主题生成一次市场状态输入。模型必须读取完整输入，使用 `prompts/market-regime-v1.md` 输出 bare JSON；它不能删减原 CANDIDATE，只能把因资金迁移需要立即查证的 WATCH 轴加入补选，NONE 轴不得升级：

```bash
python3 scripts/model_io.py market-regime-packet \
  --sensing "$READY_RUN/sensing.json" \
  --output "$WORK/market-regime-packet.json"
# 单次模型调用生成 market-regime.bare.json
python3 scripts/model_io.py stamp --kind market_regime \
  --input "$WORK/market-regime.bare.json" \
  --output "$WORK/market-regime.json"
```

随后机械生成 `CANDIDATE + 合法 WATCH 补选` 的精确查证清单。外部事实只围绕这些 case 定向查证，不做泛新闻扫描：

```bash
python3 scripts/model_io.py evidence-plan \
  --sensing "$READY_RUN/sensing.json" \
  --market-regime "$WORK/market-regime.json" \
  --output "$WORK/evidence-plan.json"
```

证据采集必须逐 case 闭合 `case_coverage`。找到事实时使用 `EVIDENCE_FOUND` 并引用冻结 evidence item；补差后仍无新增直接事实时使用 `NO_NEW_DIRECT_FACT_FOUND`，同时保存实际查证范围、来源 URI、完成时间和限制。只要 evidence plan 存在 case，空 evidence/coverage、漏 case 或未知引用都失败关闭；只有 plan 本身为零 case 时空 coverage 才合法。

```bash
python3 scripts/model_io.py stamp --kind evidence \
  --input "$WORK/evidence.bare.json" --output "$WORK/evidence.json"
python3 scripts/model_io.py theme-review-packet \
  --sensing "$READY_RUN/sensing.json" \
  --market-regime "$WORK/market-regime.json" \
  --evidence "$WORK/evidence.json" \
  --output "$WORK/theme-review-packet.json"
python3 scripts/model_io.py stamp --kind theme_judgments \
  --input "$WORK/themes.bare.json" --output "$WORK/themes.json"
python3 scripts/validate_outputs.py --kind themes \
  --input "$READY_RUN/sensing.json" --output "$WORK/themes.json" \
  --market-regime "$WORK/market-regime.json"
python3 scripts/update_ledger.py --run-dir "$READY_RUN" \
  --theme-judgments "$WORK/themes.json" --evidence "$WORK/evidence.json" \
  --market-regime "$WORK/market-regime.json"
python3 scripts/render_report.py --ledger "$READY_RUN/ledger.json" \
  --output "$READY_RUN/preview.md" --vault-root "$VAULT_ROOT"
```

`VAULT_ROOT` 必须是含 `.obsidian/` 的 Vault 根；默认目标为 `investment/trend/YYYY-MM/Www/YYYY-MM-DD-板块扫描.md`。目标不存在时原子创建、内容相同时幂等、内容不同时拒绝覆盖，不能静默改写已有笔记。`update_ledger.py` 才是最终状态、市场上下文、case coverage 与引用对账门。报告必须保留内部预览、证据正文未互锁、系统风险哨兵未配置、跨批语义复核未实现等限制。股票字段一律为空；任何股票注入都会被拒绝。
