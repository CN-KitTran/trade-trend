#!/usr/bin/env python3
import sys
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from model_io import (build_sensing_batches, compact_card,
                      merge_sensing_batches, stamp)  # noqa: E402
from v3_common import (ContractError, artifact, content_hash,
                       validate_artifact_value)  # noqa: E402


class ModelIoTests(unittest.TestCase):
    def test_compact_card_keeps_direction_facts_and_drops_audits(self):
        card = {
            "theme_id": "theme-a", "display_name": "主题甲",
            "universe_layer": "THEME", "price": {"returns": {"5D": 0.123456789}},
            "breadth": {"balance_path_5d": [0.1], "audit": [{"raw": True}]},
            "attention": {"activity_ratio_path_5d": [1.2], "audit": [{"raw": True}]},
            "data_health": {"price": "SUFFICIENT"},
            "permission_caps": {
                "max_sensing_opportunity_signal": "CANDIDATE",
                "max_sensing_risk_signal": "CANDIDATE",
                "formal_theme_decision_allowed": True,
                "allowed_opportunity_stages": ["FORMING"],
                "allowed_risk_levels": ["LOW", "CAUTION"],
            },
            "evidence_catalog": ["theme-a:price"],
        }
        value = compact_card(card)
        self.assertEqual(value["price"]["returns"]["5D"], 0.123456789)
        self.assertNotIn("audit", value["breadth"])
        self.assertNotIn("audit", value["attention"])
        self.assertEqual(value["theme_id"], "theme-a")

    def test_stamp_creates_valid_evidence_artifact(self):
        value = stamp("evidence", {
            "information_cutoff": "2026-08-24T09:14:59+08:00",
            "evidence_plan_hash": "sha256:" + "1" * 64,
            "evidence_items": [],
            "case_coverage": [],
        })
        validate_artifact_value(value, "evidence")

    def test_sensing_batches_bind_inputs_and_merge_all_themes(self):
        cards = []
        for tid in ("theme-a", "theme-b"):
            cards.append({
                "theme_id": tid, "display_name": tid,
                "permission_caps": {
                    "max_sensing_opportunity_signal": "NONE",
                    "max_sensing_risk_signal": "NONE",
                },
                "evidence_catalog": [],
            })
        value = {
            **artifact("sensing", "sensing"),
            "session": "PREOPEN", "release_mode": "INTERNAL_GATE",
            "decision_date": "2026-08-24", "market_data_as_of": "2026-08-21",
            "market_data_captured_at": "2026-08-23T22:12:12+08:00",
            "source_universe_version": "sha256:" + "1" * 64,
            "information_cutoff": "2026-08-24T09:14:59+08:00",
            "run_window_status": "EARLY_DRAFT", "observations_ref": "x",
            "theme_registry_ref": "y", "coverage_permission_matrix_ref": "z",
            "sensing_input_hash": "sha256:" + "2" * 64,
            "theme_cards": cards, "eligible_theme_count": 2,
            "judged_theme_count": 0, "validation": {"status": "NOT_RUN"},
            "theme_decisions": [],
        }
        value["artifact_hash"] = content_hash(value)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sensing = root / "sensing.json"
            sensing.write_text(json.dumps(value), encoding="utf-8")
            packets = build_sensing_batches(sensing, 1)
            for packet in packets:
                index = packet["batch_index"]
                (root / f"batch-{index:03d}.input.json").write_text(
                    json.dumps(packet), encoding="utf-8")
                tid = packet["batch_theme_ids"][0]
                output = {
                    "batch_index": index,
                    "batch_input_hash": packet["batch_input_hash"],
                    "sensing_input_hash": value["sensing_input_hash"],
                    "correction_attempts": 0,
                    "reconciliation": [],
                    "theme_decisions": [{
                        "theme_id": tid,
                        "opportunity": {"signal": "NONE", "structure_type": None,
                                        "path_pattern": None, "evidence_refs": [],
                                        "reason": ""},
                        "risk": {"signal": "NONE", "structure_type": None,
                                 "path_pattern": None, "evidence_refs": [],
                                 "reason": ""},
                        "derived_decision": "NONE",
                    }],
                }
                (root / f"batch-{index:03d}.output.json").write_text(
                    json.dumps(output), encoding="utf-8")
            merged = merge_sensing_batches(sensing, root, 1)
            self.assertEqual([row["theme_id"] for row in merged["theme_decisions"]],
                             ["theme-a", "theme-b"])
            packet_path = root / "batch-001.input.json"
            tampered = json.loads(packet_path.read_text())
            tampered["themes"][0]["display_name"] = "tampered"
            packet_path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaises(ContractError) as raised:
                merge_sensing_batches(sensing, root, 1)
            self.assertIn("SENSING_BATCH_INPUT_TAMPERED_OR_MISSING",
                          raised.exception.reasons)


if __name__ == "__main__":
    unittest.main()
