# V0.51 当前验收矩阵（contract fixtures + first-run CLI skeleton）

夹具和黑盒测试不调用旧版 `scan_sectors.py`。当前 V3 CLI 已存在，并完成一次真实 CLOSE_FREEZE、EARLY_DRAFT 全主题感知、115 case 正式判断、不可变台账与 Markdown 早稿渲染；它仍只代表 INTERNAL_GATE 首次运行安全纵切，未实现模块必须失败关闭，不能因 73 项测试通过而误报为完整生产闭环。

| 覆盖主题 | 夹具/测试 | V0.51 验收编号 | 当前结果语义 |
|---|---|---|---|
| 完整性门禁、缺失值 fail-closed | `complete_core_frame`, `missing_L1/L5/L20/L60`, `missing_benchmark`, `benchmark_wrong_dates` | 98, 125, 127, 128, 133, 257, 286 | 确定性通过/失败断言 |
| 错误方向数据不得降级 | `wrong_direction_data` | 43, 125, 128 | 必须 `INVALID_NO_DOWNGRADE` |
| D 收盘→D+1 真实交易日 | `d_plus_1_weekend`, `d_plus_1_holiday` | 38, 41, 50, 52, 255, 256, 259 | 周末样例通过，节假日错配拒绝 |
| L1/L5/L20/L60 + 宽基同日对齐 | 上述窗口/benchmark 夹具 | 98, 112, 114, 257, 286 | 缺任一窗口/宽基/错日均 `INVALID` |
| 新主题映射权限 | `mapping_incomplete_with_proxy`, `mapping_incomplete_without_proxy` | 130, 262, 263 | 有代理最多 `WATCH_ONLY`，无代理 `NO_DIRECTION` |
| 1D 不单独产生机会/风险 | `one_day_only` | 62, 69, 70, 86 | 最多 `WATCH`，不得 `CANDIDATE` |
| Internal/Shadow/Official 隔离 | `internal_shadow_official_isolation` | 252, 254, 296, 302 | 根目录、official 标记、状态链分离 |
| 历史不可覆盖 | `historical_immutable` | 132, 217, 249, 285, 286 | amendment 追加新路径/哈希 |
| 完整台账与简洁报告容量 | `report_capacity` | 177, 181, 186, 196, 197, 279, 280 | 台账不截断；每机会报告最多 3 只 |
| 一次纠正后仍失败的安全承接 | `correction_fails_twice` | 89, 121, 245, 269, 270, 298 | 规格夹具覆盖目标语义；当前 CLI 只实现新主题 `NO_FORMAL_STATE`，上一状态承接未实现并失败关闭 |

## 尚未覆盖（下一批）

- 完整状态转移、上一正式 sensing+ledger 强制复核与 `CARRIED_FORWARD`；当前已有越权/重复/UNVERIFIED EXIT/证据引用反例测试，但不代表连续性完成。
- 候选股票完整池对账、相关度来源、风险三入口、双截止、失败不复用旧名单。
- 午盘条件强契约与 `INTRADAY_RISK_WATCH` 生命周期。
- 并发发布锁、迟到运行、AMENDED 防前视。
- Shadow 十个连续决策日、T+5 成熟度与多维人工评价清单。
- 生产 adapter 的正式模型/股票模块仍未接入；当前已完成 CLI skeleton 的真实 subprocess 黑盒测试。

## 第二批生产 CLI 黑盒接线

| CLI/场景 | 测试 | 预期 |
|---|---|---|
| 合成 `CLOSE_FREEZE` 成功 | `test_synthetic_close_freeze_succeeds` | PASS，写入 INTERNAL_GATE immutable v1 |
| `CLOSE_FREEZE → PREOPEN` early draft | `test_synthetic_close_freeze_to_preopen_draft` | PASS，生成 EARLY_DRAFT，不产生正式判断 |
| EARLY_DRAFT 内部预览隔离 | `test_early_internal_run_can_render_explicit_unpublished_preview` | PASS，实际开始时刻为 cutoff；只写 internal ledger/preview 并显著标限 |
| 缺 L60 | `test_missing_l60_fails_closed` | FAIL + `LOOKBACK_L60_REQUIRED` |
| benchmark 错日 | `test_benchmark_wrong_date_fails_closed` | FAIL + `BENCHMARK_DATE_ALIGNMENT_FAILED` |
| identity hash 不匹配 | `test_identity_hash_mismatch_fails_closed` | FAIL + `CLOSE_FRAME_HASH_INTERLOCK_FAILED` |
| 交易日历缺失 | `test_missing_calendar_fails_closed` | FAIL + `TRADING_CALENDAR_CONTRACT_MISSING` |
| gateway benchmark dict 形状兼容 | `test_gateway_like_dict_benchmark_is_normalized_and_freeze_succeeds` | PASS（生产归一化） |
| 重复 CLOSE_FREEZE | `test_duplicate_close_freeze_is_idempotent_for_same_input` + `test_different_input_requires_explicit_amendment_and_references_original` | PASS：相同输入复用同一 v1；不同输入无 amendment 拒绝，显式 amendment 新建 v2 并引用原 manifest |
| INTERNAL/SHADOW→正式越权 | `test_internal_freeze_cannot_be_released_as_official` + preview/Vault 路径反例 | PASS：当前 OFFICIAL 整体禁用；preview 必须写 ledger 同目录，只有 INTERNAL 可将同内容受控复制到 Vault 且不改变发布状态 |

当前实际入口还包括 `bootstrap_dependencies.py` 与 `model_io.py` 的确定性分批/哈希归并及全候选轴只降级校准；manifest 为 `AVAILABLE_PARTIAL`，不存在 expectedFailure。现有 73 项趋势测试与 29 项网关测试均为正常断言；真实 Agent LLM 全主题感知与首次正式主题判断已执行，但父子/同义关系簇归并、正式每日连续性、可信证据正文互锁、股票、午盘、alert、outcomes 和发布仍列为阻断项。
