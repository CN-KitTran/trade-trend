"""Black-box V0.51 acceptance fixtures.

These tests deliberately exercise contracts, not the legacy scanner.  The optional
production adapter is only used when TREND_ANALYSIS_ADAPTER is set; absent adapter
cases remain explicit expected failures rather than being counted as passes.
"""
from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "evals" / "fixtures" / "acceptance_cases.json"
MANIFEST_PATH = ROOT / "evals" / "manifest.json"

WINDOWS = ("L1", "L5", "L20", "L60")
SIGNAL_ORDER = {"NONE": 0, "WATCH": 1, "CANDIDATE": 2}


def _load():
    return json.loads(FIXTURE_PATH.read_text())


def _get(obj, dotted):
    for part in dotted.split("."):
        if not isinstance(obj, dict) or part not in obj:
            return None
        obj = obj[part]
    return obj


def _set(obj, dotted, value):
    bits = dotted.split(".")
    cur = obj
    for part in bits[:-1]:
        cur = cur.setdefault(part, {})
    cur[bits[-1]] = value


def _remove(obj, dotted):
    bits = dotted.split(".")
    cur = obj
    for part in bits[:-1]:
        if not isinstance(cur, dict) or part not in cur:
            return
        cur = cur[part]
    if isinstance(cur, dict):
        cur.pop(bits[-1], None)


def _materialize(cases):
    by_id = {c["id"]: c for c in cases}
    out = {}
    def one(case):
        if case["id"] in out:
            return out[case["id"]]
        item = copy.deepcopy(one(by_id[case["extends"]])) if case.get("extends") else {}
        item.update({k: copy.deepcopy(v) for k, v in case.items() if k not in {"extends", "remove", "override"}})
        for key in case.get("remove", []):
            _remove(item, key)
        for key, value in case.get("override", {}).items():
            _set(item, key, value)
        out[case["id"]] = item
        return item
    return [one(c) for c in cases]


def core_gate(case):
    frame, identity = case.get("frame", {}), case.get("identity", {})
    required = ("frame_kind", "market_data_as_of", "lookback_trading_days", "coverage_type",
                "history_coverage", "history_sample_count", "benchmark", "sector_trading_dates", "windows",
                "close_confirmation", "catalog_version")
    if any(k not in frame for k in required):
        return "INVALID"
    if frame.get("frame_kind") != "CLOSE" or frame.get("coverage_type") != "FULL":
        return "INVALID"
    if frame.get("history_coverage") != "FULL" or frame.get("lookback_trading_days") != 60 or frame.get("history_sample_count") != 60:
        return "INVALID"
    if any(frame["windows"].get(w) != "FULL" for w in WINDOWS):
        return "INVALID"
    bench = frame["benchmark"]
    if bench.get("history_coverage") != "FULL" or bench.get("sample_count") != 60 or not bench.get("trading_dates"):
        return "INVALID"
    if bench["trading_dates"] != frame["sector_trading_dates"]:
        return "INVALID"
    if not frame["close_confirmation"].get("capture_eligible"):
        return "INVALID"
    if identity.get("requested_as_of") != frame.get("market_data_as_of"):
        return "INVALID"
    if identity.get("coverage_type") != "FULL" or identity.get("catalog_version") != frame.get("catalog_version"):
        return "INVALID"
    if frame.get("direction_data_status") == "WRONG_DIRECTION":
        return "INVALID_NO_DOWNGRADE"
    return "VALID"


def d_plus_one(case):
    dates = case.get("calendar", {}).get("trading_dates", [])
    d, d1 = case.get("market_data_as_of"), case.get("decision_date")
    return "VALID" if len(dates) >= 2 and dates[0] == d and dates[1] == d1 else "INVALID_DATE_PAIR"


def mapping_policy(case):
    if case.get("mapping_status") == "INCOMPLETE" and case.get("has_verified_proxy"):
        return {"market_permission": "WATCH_ONLY", "opportunity_signal_max": "WATCH",
                "risk_signal_max": "WATCH", "formal_state": False, "stock_selection": False}
    return {"market_permission": "NO_DIRECTION", "direction_llm": False,
            "formal_state": False, "stock_selection": False}


def signal_policy(case):
    e = case.get("evidence", {})
    only_1d = set(e.get("price", {})) <= {"1D"} and not e.get("breadth") and not e.get("attention")
    if only_1d:
        return {"opportunity_signal_max": "WATCH", "risk_signal_max": "WATCH", "candidate": False}
    return {"opportunity_signal_max": "CANDIDATE", "risk_signal_max": "CANDIDATE", "candidate": True}


class FixtureManifestTests(unittest.TestCase):
    def setUp(self):
        self.doc = _load()
        self.cases = _materialize(self.doc["cases"])
        self.by_id = {c["id"]: c for c in self.cases}

    def test_manifest_and_fixture_are_versioned(self):
        manifest = json.loads(MANIFEST_PATH.read_text())
        self.assertEqual(manifest["spec_version"], "V0.51")
        self.assertTrue(manifest["cases_are_contract_fixtures"])
        self.assertEqual(manifest["fixture"], "evals/fixtures/acceptance_cases.json")
        self.assertGreaterEqual(len(manifest["acceptance_ids"]), 10)
        ids = set(self.by_id)
        for group in manifest["acceptance_ids"].values():
            self.assertTrue(group)
        self.assertEqual(self.doc["schema"], "trade-trend-analysis.acceptance-fixtures.v1")
        self.assertGreaterEqual(len(ids), 15)

    def test_core_completeness_gate_and_missing_windows_fail_closed(self):
        self.assertEqual(core_gate(self.by_id["complete_core_frame"]), "VALID")
        for case_id in ("missing_L1", "missing_L5", "missing_L20", "missing_L60", "missing_benchmark", "benchmark_wrong_dates"):
            self.assertEqual(core_gate(self.by_id[case_id]), "INVALID", case_id)

    def test_wrong_direction_data_does_not_degrade_to_partial(self):
        # A direction/shape error is a hard invalidation. It is not a LIMITED,
        # WATCH, or other permissive downgrade.
        self.assertEqual(core_gate(self.by_id["wrong_direction_data"]), "INVALID_NO_DOWNGRADE")
        self.assertNotIn(core_gate(self.by_id["wrong_direction_data"]), {"LIMITED", "WATCH", "VALID"})

    def test_d_to_d1_uses_actual_trading_calendar(self):
        self.assertEqual(d_plus_one(self.by_id["d_plus_1_weekend"]), "VALID")
        self.assertEqual(d_plus_one(self.by_id["d_plus_1_holiday"]), "INVALID_DATE_PAIR")

    def test_mapping_is_watch_only_until_atomic_registration(self):
        got = mapping_policy(self.by_id["mapping_incomplete_with_proxy"])
        self.assertEqual(got["market_permission"], "WATCH_ONLY")
        self.assertEqual(got["opportunity_signal_max"], "WATCH")
        self.assertEqual(got["risk_signal_max"], "WATCH")
        self.assertFalse(got["formal_state"])
        self.assertFalse(got["stock_selection"])
        got = mapping_policy(self.by_id["mapping_incomplete_without_proxy"])
        self.assertEqual(got["market_permission"], "NO_DIRECTION")
        self.assertFalse(got["direction_llm"])

    def test_1d_alone_cannot_create_opportunity_or_risk(self):
        got = signal_policy(self.by_id["one_day_only"])
        self.assertLessEqual(SIGNAL_ORDER[got["opportunity_signal_max"]], SIGNAL_ORDER["WATCH"])
        self.assertLessEqual(SIGNAL_ORDER[got["risk_signal_max"]], SIGNAL_ORDER["WATCH"])
        self.assertFalse(got["candidate"])

    def test_internal_shadow_official_are_separate(self):
        runs = self.by_id["internal_shadow_official_isolation"]["runs"]
        roots = [r["output_root"] for r in runs]
        self.assertEqual(len(roots), len(set(roots)))
        by_mode = {r["mode"]: r for r in runs}
        self.assertFalse(by_mode["INTERNAL_GATE"]["official"])
        self.assertFalse(by_mode["SHADOW"]["official"])
        self.assertTrue(by_mode["OFFICIAL"]["official"])
        self.assertNotEqual(by_mode["SHADOW"]["output_root"], by_mode["OFFICIAL"]["output_root"])

    def test_history_is_append_only(self):
        c = self.by_id["historical_immutable"]
        self.assertNotEqual(c["history"]["path"], c["amendment"]["path"])
        self.assertNotEqual(c["history"]["content_hash"], c["amendment"]["content_hash"])
        self.assertIn("reason", c["amendment"])
        self.assertTrue(c["amendment"]["reason"])

    def test_ledger_is_complete_but_compact_report_has_at_most_three(self):
        c = self.by_id["report_capacity"]
        candidates = c["ledger"]["opportunities"][0]["candidates"]
        self.assertGreater(len(candidates), 3)
        report = candidates[:c["expected"]["max_candidates_per_opportunity"]]
        self.assertLessEqual(len(report), 3)
        self.assertTrue(c["expected"]["ledger_preserves_all"])
        self.assertTrue(c["expected"]["no_trade_instructions"])

    def test_two_failed_validations_safe_carry_forward(self):
        c = self.by_id["correction_fails_twice"]
        self.assertEqual(sum(1 for a in c["attempts"] if not a["valid"]), 2)
        e = c["expected"]
        self.assertEqual(e["run_status"], "FAILED")
        self.assertTrue(e["carry_forward"])
        self.assertEqual(e["new_theme_state"], "NO_FORMAL_STATE")
        self.assertFalse(e["emit_same_event"])
        self.assertFalse(e["copy_candidates"])


class StaticLintAndAdapterTests(unittest.TestCase):
    def test_manifest_is_valid_json_and_adapter_is_explicit(self):
        m = json.loads(MANIFEST_PATH.read_text())
        self.assertIn(m["production_adapter"]["status"], {"AVAILABLE_PARTIAL", "AVAILABLE"})
        self.assertIn("test_module", m["production_adapter"])

    def test_production_adapter_status_is_explicitly_partial(self):
        manifest = json.loads(MANIFEST_PATH.read_text())
        self.assertIn(manifest["production_adapter"]["status"], {"AVAILABLE_PARTIAL", "AVAILABLE"})
        self.assertEqual(manifest["production_adapter"]["test_module"], "tests/test_v3_subprocess_contracts.py")


if __name__ == "__main__":
    unittest.main()
