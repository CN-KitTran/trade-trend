"""Focused adversarial tests for LLM/output permission boundaries."""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from render_report import render  # noqa: E402
from run_preopen import _theme_cards  # noqa: E402
from update_ledger import _daily_summary, _projection, validate_all_evidence_refs  # noqa: E402
from v3_common import ContractError  # noqa: E402
from validate_outputs import validate_sensing, validate_theme_judgments  # noqa: E402


def _card():
    return {
        "theme_id": "theme-a", "display_name": "主题甲",
        "price": {"returns": {"1D": 0.01, "3D": 0.03, "5D": 0.04,
                                "10D": 0.05, "20D": 0.06}},
        "breadth": {"up_ratio_today": 0.6, "balance_path_5d": [0.1] * 5},
        "attention": {"activity_ratio_path_5d": [1.0] * 5},
        "data_health": {"price": "SUFFICIENT", "breadth": "SUFFICIENT",
                        "attention": "SUFFICIENT"},
        "evidence_catalog": ["theme-a:price", "theme-a:breadth", "theme-a:attention"],
        "permission_caps": {
            "max_sensing_opportunity_signal": "CANDIDATE",
            "max_sensing_risk_signal": "CANDIDATE",
            "formal_theme_decision_allowed": True,
            "allowed_opportunity_stages": ["FORMING"],
            "allowed_risk_levels": ["LOW", "CAUTION", "HIGH", "EXIT"],
        },
    }


def _opportunity_row():
    return {
        "theme_id": "theme-a", "decision_validation_status": "VALID",
        "state_provenance": {"mode": "CURRENT_VALIDATED", "source_run_id": None},
        "opportunity_stage": "FORMING", "risk_level": "LOW",
        "evidence_refs": ["theme-a:price"], "counterevidence_refs": [],
        "risk_evidence_refs": [], "next_validation": "下一交易日验证扩散",
        "pricing_judgment": "仍需验证", "counterevidence_assessment": "暂无反证",
        "alternative_explanations": ["宽基共同变化"],
        "opportunity_invalidation_or_reentry_condition": "相对强度失效",
        "verification_cases": [{
            "axis": "OPPORTUNITY", "conclusion": "VERIFIED",
            "evidence_for_refs": ["theme-a:price"], "evidence_against_refs": [],
            "limitations": [], "alternative_explanation": "宽基共同变化",
            "pricing_assessment": "仍需验证", "next_validation": "下一交易日",
        }],
        "report_routing": {
            "opportunity": {"tier": "BRIEF", "priority_reason": "形成中"},
            "risk": {"tier": "LEDGER_ONLY", "priority_reason": "低风险"},
        },
    }


class DecisionPermissionTests(unittest.TestCase):
    def test_peer_percentiles_use_one_unique_proxy_per_mapped_theme(self):
        def observation(sid, excess):
            return {
                "source_sector_id": sid, "source_scope": "PRIMARY",
                "metrics": {
                    "price": {"returns": {key: excess for key in
                                           ("1D", "3D", "5D", "10D", "20D")},
                              "excess_returns": {key: excess for key in
                                                 ("1D", "3D", "5D", "10D", "20D")}},
                    "breadth": {"balance_path_5d": [0.1] * 5},
                    "attention": {"activity_ratio_path_5d": [1.0] * 5},
                },
                "data_health": {"price": "SUFFICIENT", "breadth": "SUFFICIENT",
                                "attention": "SUFFICIENT"},
                "limitations": [], "provenance": [],
            }
        observations = {"source_observations": [observation("s1", 0.1),
                                                 observation("s2", -0.2)],
                        "provisional_labels": []}
        registry = {"themes": [{
            "theme_id": "theme-a", "display_name": "主题甲", "lifecycle_status": "STABLE",
            "universe_layer": "THEME", "market_proxy_source_id": "s1",
            "source_bindings": [
                {"source_kind": "SOURCE_SECTOR", "source_id": "s1", "valid_to": None},
                {"source_kind": "SOURCE_SECTOR", "source_id": "s2", "valid_to": None},
            ],
        }], "excluded_source_sector_ids": []}
        matrix = {"matrix_version": "sha256:" + "1" * 64, "entries": [{
            "subject_scope": "THEME", "field_or_window": "CORE_OBSERVATIONS",
            "data_health": "SUFFICIENT", "coverage_type": "FULL",
            "allowed_sensing_signals": ["NONE", "WATCH", "CANDIDATE"],
        }]}
        cards, excluded = _theme_cards(observations, registry, matrix)
        self.assertEqual(excluded, [])
        self.assertEqual(len(cards), 1)
        peer = cards[0]["price"]["peer_percentiles"]["5D"]
        self.assertEqual(peer["sample_count"], 1)
        self.assertEqual(peer["value"], 0.5)

    def test_persistent_candidate_requires_price_and_a_second_dimension(self):
        card = _card()
        output = {
            "correction_attempts": 0, "reconciliation": [],
            "theme_decisions": [{
                "theme_id": "theme-a",
                "opportunity": {"signal": "CANDIDATE",
                                "structure_type": "EMERGING_STRENGTH",
                                "path_pattern": "PERSISTENT",
                                "evidence_refs": ["theme-a:price"], "reason": "多日走强"},
                "risk": {"signal": "NONE", "structure_type": None,
                         "path_pattern": None, "evidence_refs": [], "reason": ""},
                "derived_decision": "OPPORTUNITY",
            }],
        }
        errors = validate_sensing([card], output)
        self.assertIn("theme-a:opportunity:PERSISTENT_MULTI_DIMENSION_REQUIRED", errors)

    def test_unvalidated_reconciliation_is_rejected(self):
        output = {"correction_attempts": 0, "theme_decisions": [],
                  "reconciliation": [{"representative": "fake"}]}
        self.assertIn("RECONCILIATION_VALIDATION_NOT_IMPLEMENTED",
                      validate_sensing([], output))

    def test_formal_stage_cannot_exceed_card_permission(self):
        sensing = {"theme_cards": [_card()],
                   "theme_decisions": [{"theme_id": "theme-a"}],
                   "review_plan": [{"theme_id": "theme-a",
                                    "review_axes": ["OPPORTUNITY"]}]}
        judgments = {"review_theme_ids": ["theme-a"],
                     "themes": [_opportunity_row()]}
        self.assertEqual(validate_theme_judgments(sensing, judgments), [])
        judgments["themes"][0]["opportunity_stage"] = "ACTIVE"
        errors = validate_theme_judgments(sensing, judgments)
        self.assertIn("theme-a:OPPORTUNITY_STAGE_PERMISSION_EXCEEDED", errors)

    def test_duplicate_formal_rows_are_rejected(self):
        sensing = {"theme_cards": [_card()],
                   "theme_decisions": [{"theme_id": "theme-a"}],
                   "review_plan": [{"theme_id": "theme-a",
                                    "review_axes": ["OPPORTUNITY"]}]}
        row = _opportunity_row()
        errors = validate_theme_judgments(
            sensing, {"review_theme_ids": ["theme-a"], "themes": [row, copy.deepcopy(row)]})
        self.assertIn("DUPLICATE_OR_INVALID_FORMAL_THEME_ROW", errors)

    def test_disabled_stock_module_cannot_hitchhike_in_theme_row(self):
        sensing = {"theme_cards": [_card()],
                   "theme_decisions": [{"theme_id": "theme-a"}],
                   "review_plan": [{"theme_id": "theme-a",
                                    "review_axes": ["OPPORTUNITY"]}]}
        row = _opportunity_row()
        row["stock_candidates"] = [{"stock_code": "000001.SZ"}]
        errors = validate_theme_judgments(
            sensing, {"review_theme_ids": ["theme-a"], "themes": [row]})
        self.assertIn("theme-a:STOCK_MODULE_NOT_ENABLED", errors)

    def test_unverified_case_cannot_create_exit(self):
        sensing = {"theme_cards": [_card()],
                   "theme_decisions": [{"theme_id": "theme-a"}],
                   "review_plan": [{"theme_id": "theme-a", "review_axes": ["RISK"]}]}
        row = _opportunity_row()
        row.update({"opportunity_stage": None, "risk_level": "EXIT",
                    "next_validation": None, "risk_relief_condition": "官方解除风险",
                    "verification_cases": [{
                        "axis": "RISK", "conclusion": "UNVERIFIED",
                        "evidence_for_refs": [], "evidence_against_refs": [],
                        "limitations": ["证据不足"],
                    }]})
        errors = validate_theme_judgments(
            sensing, {"review_theme_ids": ["theme-a"], "themes": [row]})
        self.assertIn("theme-a:HIGH_EXIT_REQUIRES_VERIFIED_RISK_CASE", errors)

    def test_every_formal_reference_field_is_checked(self):
        row = _opportunity_row()
        row["counterevidence_refs"] = ["NOPE-COUNTER"]
        row["risk_evidence_refs"] = ["NOPE-RISK"]
        row["verification_cases"][0]["evidence_for_refs"] = ["NOPE-CASE-FOR"]
        row["verification_cases"][0]["evidence_against_refs"] = ["NOPE-CASE-AGAINST"]
        errors = validate_all_evidence_refs([row], {"theme-a:price"})
        self.assertEqual(len(errors), 4)
        self.assertTrue(all("UNKNOWN_EVIDENCE_REF" in error for error in errors))


class ReportProjectionTests(unittest.TestCase):
    def _ledger(self, tier="LEDGER_ONLY"):
        return {
            "publication_status": "VALIDATED_NOT_PUBLISHED",
            "decision_date": "2026-08-24", "market_data_as_of": "2026-08-21",
            "information_cutoff": "2026-08-24T09:14:59+08:00", "effective_from": None,
            "global_data_health": "OK", "publication_completeness": "COMPLETE",
            "run_id": "run-test", "daily_summary": "机械摘要",
            "themes": [{
                "theme_id": "hidden-theme", "display_name": "不可泄漏主题",
                "state_provenance": {"mode": "CURRENT_VALIDATED"},
                "opportunity_stage": "ACTIVE", "risk_level": "LOW",
                "report_routing": {
                    "opportunity": {"tier": tier},
                    "risk": {"tier": "LEDGER_ONLY"},
                },
            }],
            "report_projection": {"sensing_watch_items": [], "failed_review_items": [],
                                  "alert_items": [], "invalidation_and_exit_items": [],
                                  "candidate_change_items": []},
            "publication_limitations": [], "global_limitations": [],
        }

    def test_ledger_only_theme_never_enters_report_body(self):
        text = render(self._ledger())
        self.assertNotIn("不可泄漏主题", text)

    def test_v052_market_context_and_theme_attribution_are_rendered(self):
        ledger = self._ledger("FOCUS")
        ledger["market_context"] = {
            "context_status": "VALIDATED",
            "market_regime": "防御/价值切换",
            "risk_appetite": "收缩",
            "capital_migration": {
                "from_summary": "高弹性成长",
                "to_summary": "资源、高股息与必需消费",
            },
            "duration": "已确认",
            "contradictions": ["部分高Beta主题仍逆势走强"],
            "confidence": {"level": "中高", "reason": "多日结构一致"},
            "evidence_refs": ["market:regime"],
            "limitations": [],
        }
        theme = ledger["themes"][0]
        theme.update({
            "market_role": "RECEIVER",
            "opportunity_driver": "REGIME",
            "risk_types": ["MARKET_TREND", "STYLE_RETREAT"],
            "regime_alignment": "ALIGNED",
            "regime_interpretation": "防御资金承接，但产业支持有限",
            "thesis": "防御切换中的承接方向",
            "why_now": "风险偏好连续收缩",
        })
        theme["report_routing"]["risk"]["tier"] = "FOCUS"
        theme["risk_level"] = "CAUTION"
        theme["risk_summary"] = "若防御交易退潮，承接可能快速减弱"
        theme["risk_relief_condition"] = "产业证据与价格结构共同改善"

        text = render(ledger)

        self.assertLess(text.index("## 市场现在在交易什么"),
                        text.index("## 今日结论"))
        self.assertIn("市场状态：防御/价值切换；风险偏好：收缩；阶段：已确认", text)
        self.assertIn("从 高弹性成长，流向 资源、高股息与必需消费", text)
        self.assertIn("市场角色：资金承接方｜主要驱动：环境驱动", text)
        self.assertIn("风险类型：市场趋势风险、风格退潮风险｜市场角色：资金承接方", text)

    def test_legacy_ledger_marks_missing_v052_context_without_inference(self):
        ledger = self._ledger("FOCUS")
        ledger["market_context"] = {
            "context_status": "INCONCLUSIVE",
            "market_posture": "INCONCLUSIVE",
            "evidence_refs": [],
            "limitations": ["NOT_CONFIGURED"],
        }

        text = render(ledger)

        self.assertIn("市场状态层尚未形成已校验结论（INCONCLUSIVE）", text)
        self.assertIn("限制：NOT_CONFIGURED", text)
        self.assertIn("市场角色：未形成已校验结论｜主要驱动：未形成已校验结论｜V0.52字段缺失", text)

    def test_unknown_route_fails_instead_of_becoming_brief(self):
        with self.assertRaises(ContractError):
            render(self._ledger("UNKNOWN"))

    def test_unrouted_sensing_watches_are_counted_not_dumped(self):
        sensing = {
            "theme_cards": [
                {"theme_id": "watch-a", "display_name": "观察甲"},
                {"theme_id": "watch-b", "display_name": "观察乙"},
            ],
            "theme_decisions": [
                {"theme_id": "watch-a", "opportunity": {"signal": "WATCH"},
                 "risk": {"signal": "NONE"}},
                {"theme_id": "watch-b", "opportunity": {"signal": "WATCH"},
                 "risk": {"signal": "WATCH"}},
            ],
        }
        projection = _projection([], sensing)
        self.assertEqual(projection["sensing_watch_items"], [])
        self.assertEqual(projection["unrouted_sensing_watch_summary"]["theme_count"], 2)
        self.assertEqual(
            projection["unrouted_sensing_watch_summary"]["axis_count"],
            {"OPPORTUNITY": 2, "RISK": 1},
        )
        ledger = self._ledger()
        ledger["themes"] = []
        ledger["report_projection"] = projection
        ledger["daily_summary"] = _daily_summary([], projection)
        text = render(ledger)
        self.assertIn("另有 2 个感知级 WATCH", text)
        self.assertNotIn("观察甲", text)
        self.assertNotIn("观察乙", text)

    def test_forming_projection_uses_registry_display_name(self):
        row = _opportunity_row()
        row["report_routing"]["opportunity"]["tier"] = "BRIEF"
        sensing = {
            "theme_cards": [{"theme_id": "theme-a", "display_name": "主题甲"}],
            "theme_decisions": [],
        }
        projection = _projection([row], sensing)
        self.assertEqual(projection["sensing_watch_items"][0]["display_name"], "主题甲")


if __name__ == "__main__":
    unittest.main()
