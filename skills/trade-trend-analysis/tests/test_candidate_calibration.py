#!/usr/bin/env python3
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from model_io import apply_calibration, build_calibration_packet  # noqa: E402
from validate_outputs import validate_sensing  # noqa: E402
from v3_common import ContractError, artifact, content_hash  # noqa: E402


def axis(signal, structure=None, ref="theme-a"):
    return ({"signal": "NONE", "structure_type": None, "path_pattern": None,
             "evidence_refs": [], "reason": ""} if signal == "NONE" else
            {"signal": signal, "structure_type": structure,
             "path_pattern": "PERSISTENT",
             "evidence_refs": [f"{ref}:price", f"{ref}:breadth"],
             "reason": "multi-day evidence"})


class CandidateCalibrationTests(unittest.TestCase):
    def _inputs(self, root):
        cards = []
        for tid, layer in (("theme-a", "INDUSTRY"), ("theme-b", "THEME")):
            cards.append({
                "theme_id": tid, "display_name": tid, "universe_layer": layer,
                "price": {"returns": {"5D": 0.1}},
                "breadth": {"balance_path_5d": [0.2]},
                "attention": {"activity_ratio_path_5d": [1.1]},
                "permission_caps": {
                    "max_sensing_opportunity_signal": "CANDIDATE",
                    "max_sensing_risk_signal": "CANDIDATE"},
                "evidence_catalog": [f"{tid}:price", f"{tid}:breadth"],
            })
        sensing = {
            **artifact("sensing", "sensing"), "session": "PREOPEN",
            "release_mode": "INTERNAL_GATE", "decision_date": "2026-08-24",
            "market_data_as_of": "2026-08-21", "market_data_captured_at": "x",
            "source_universe_version": "u", "information_cutoff": "x",
            "run_window_status": "EARLY_DRAFT", "observations_ref": "o",
            "theme_registry_ref": "r", "coverage_permission_matrix_ref": "m",
            "sensing_input_hash": "sha256:" + "2" * 64, "theme_cards": cards,
            "eligible_theme_count": 2, "judged_theme_count": 0,
            "validation": {"status": "NOT_RUN", "errors": []},
            "theme_decisions": [],
        }
        sensing["artifact_hash"] = content_hash(sensing)
        decisions = [
            {"theme_id": "theme-a", "opportunity": axis("CANDIDATE", "EMERGING_STRENGTH"),
             "risk": axis("WATCH", "NARROWING"), "derived_decision": "OPPORTUNITY"},
            {"theme_id": "theme-b", "opportunity": axis("NONE"),
             "risk": axis("CANDIDATE", "DETERIORATION", "theme-b"),
             "derived_decision": "RISK"},
        ]
        merged = {
            "sensing_input_hash": sensing["sensing_input_hash"], "correction_attempts": 0,
            "theme_decisions": decisions, "reconciliation": [],
            "technical_batching": {"method": "DETERMINISTIC_ALL_THEME_PARTITION",
                "batch_count": 2, "batch_size": 1,
                "cross_batch_reconciliation": "NOT_IMPLEMENTED"},
        }
        sp, mp = root / "sensing.json", root / "merged.json"
        sp.write_text(json.dumps(sensing)); mp.write_text(json.dumps(merged))
        return sp, mp, merged

    def test_packet_accounts_every_candidate_axis_and_distributions(self):
        with tempfile.TemporaryDirectory() as tmp:
            sp, mp, _ = self._inputs(Path(tmp))
            packet = build_calibration_packet(sp, mp)
            self.assertEqual([(c["theme_id"], c["axis"]) for c in packet["cases"]],
                             [("theme-a", "OPPORTUNITY"), ("theme-b", "RISK")])
            self.assertEqual(packet["case_count"], 2)
            self.assertEqual(packet["all_market_signal_distribution"]["opportunity"],
                             {"CANDIDATE": 1, "WATCH": 0, "NONE": 1})
            self.assertEqual(len(packet["layer_signal_distributions"]), 2)

    def test_apply_only_keeps_or_downgrades_and_rejects_hash_tamper(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); sp, mp, merged = self._inputs(root)
            packet = build_calibration_packet(sp, mp)
            authored = {key: packet[key] for key in (
                "calibration_input_hash", "sensing_input_hash", "sensing_artifact_hash",
                "sensing_output_hash", "prompt_ref")}
            authored["correction_attempts"] = 0
            authored["cases"] = []
            for index, case in enumerate(packet["cases"]):
                authored["cases"].append({
                    "case_id": case["case_id"], "case_input_hash": case["case_input_hash"],
                    "theme_id": case["theme_id"], "axis": case["axis"],
                    "action": "DOWNGRADE_WATCH" if index == 0 else "DOWNGRADE_NONE",
                    "why_not_common_market_movement": "cannot distinguish ordinary movement",
                    "why_immediate_verification_matters": "does not require immediate review",
                })
            cp = root / "calibration.json"; cp.write_text(json.dumps(authored))
            result = apply_calibration(sp, mp, cp)
            self.assertEqual(result["theme_decisions"][0]["opportunity"]["signal"], "WATCH")
            self.assertEqual(result["theme_decisions"][1]["risk"]["signal"], "NONE")
            self.assertEqual(result["theme_decisions"][0]["risk"],
                             merged["theme_decisions"][0]["risk"])
            self.assertEqual(result["technical_batching"]["global_candidate_calibration"],
                             "GLOBAL_DOWNGRADE_CALIBRATION_APPLIED")
            self.assertEqual(len(result["candidate_calibration"]["audit"]), 2)
            tampered = copy.deepcopy(authored); tampered["cases"][0]["case_input_hash"] = "bad"
            cp.write_text(json.dumps(tampered))
            with self.assertRaises(ContractError):
                apply_calibration(sp, mp, cp)

    def test_applied_flag_cannot_bypass_calibration_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); sp, mp, merged = self._inputs(root)
            sensing = json.loads(sp.read_text())
            forged = copy.deepcopy(merged)
            forged["technical_batching"]["global_candidate_calibration"] = (
                "GLOBAL_DOWNGRADE_CALIBRATION_APPLIED")
            forged["technical_batching"]["relation_group_reconciliation"] = (
                "RELATION_GROUP_RECONCILIATION_NOT_IMPLEMENTED")
            forged["candidate_calibration"] = {}
            errors = validate_sensing(sensing["theme_cards"], forged)
            self.assertIn("CANDIDATE_CALIBRATION_METADATA_INVALID", errors)


if __name__ == "__main__":
    unittest.main()
