"""Deterministic and fail-closed tests for first-run dependency bootstrap."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from bootstrap_dependencies import (  # noqa: E402
    BootstrapError, build_bundle, build_calendar, build_concept_review_packet,
    build_permission_matrix, build_runtime_config,
)
from v3_common import content_address_without, content_hash  # noqa: E402


def _write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def _inputs():
    first = date(2026, 6, 23)
    ascending = [(first + timedelta(days=index)).isoformat() for index in range(61)]
    market_days = list(reversed(ascending[:60]))
    rows = [
        {"source_sector_id": "881101.TI", "source_name": "种植业与林业",
         "source_scope": "PRIMARY", "source_layer": "INDUSTRY",
         "provider_type": "同花顺二级行业指数", "member_count_reported": 2},
        {"source_sector_id": "885311.TI", "source_name": "智能电网",
         "source_scope": "PRIMARY", "source_layer": "CONCEPT",
         "provider_type": "同花顺概念指数", "member_count_reported": 2},
        {"source_sector_id": "885338.TI", "source_name": "融资融券",
         "source_scope": "PRIMARY", "source_layer": "CONCEPT",
         "provider_type": "同花顺概念指数", "member_count_reported": 2},
        {"source_sector_id": "999001.TI", "source_name": "待复核新类型",
         "source_scope": "PRIMARY", "source_layer": "NEW_LAYER",
         "provider_type": "未知供应商类型", "member_count_reported": 2},
        {"source_sector_id": "000985.TI", "source_name": "宽基",
         "source_scope": "PRIMARY", "source_layer": "BENCHMARK",
         "provider_type": "宽基指数", "member_count_reported": 2},
        {"source_sector_id": "700750.TI", "source_name": "参考分类",
         "source_scope": "REFERENCE", "source_layer": "INDUSTRY",
         "provider_type": "同花顺行业指数", "member_count_reported": 2},
    ]
    market_payload = {
        "frame_id": "sector-close-20260821-bootstrap-test",
        "market_data_as_of": market_days[0],
        "catalog_version": "sha256:" + "c" * 64,
        "coverage_type": "FULL",
        "expected_sector_count": 6,
        "returned_sector_count": 6,
        "primary_sector_count": 5,
        "reference_sector_count": 1,
        "trading_dates": market_days,
        "sectors": rows,
    }
    market = {"schema_version": "1.5", "data_type": "sector_market_frame",
              "code": "*", "ts": "2026-08-21T15:11:00+08:00",
              "source": "wencai", "payload": market_payload,
              "meta": {"fetch_errors": [], "degraded": False}}
    weak_identity = {
        "schema_version": "1.5", "data_type": "sector_identity", "code": "*",
        "ts": "2026-08-23T02:32:00+08:00", "source": "wencai",
        "payload": {
            "requested_as_of": "2026-08-23",
            "identity_date_semantics": "CURRENT_AT_CAPTURE",
            "coverage_type": "FULL", "catalog": [
                {**row, "member_count": 2,
                 "member_codes": ["000001.SZ", "000002.SZ"],
                 "membership_hash": "sha256:" + "a" * 64,
                 "membership_coverage": "FULL"}
                for row in rows
            ],
            "stocks": [{"code": "000001.SZ", "name": "平安银行"},
                       {"code": "000002.SZ", "name": "万科A"}],
            "provisional_labels": [
                {"provisional_id": "p:1", "source_label": "未映射标签"}
            ],
        },
        "meta": {"fetch_errors": [], "degraded": False},
    }
    calendar_source = {
        "source_kind": "USER_EXPLICIT", "source_name": "unit-test-calendar",
        "source_version": "v1", "timezone": "Asia/Shanghai", "market": "CN_A",
        "trading_dates": ascending,
        "sessions": {
            market_days[0]: {"auction_start_at": f"{market_days[0]}T09:15:00+08:00"},
            ascending[-1]: {"auction_start_at": f"{ascending[-1]}T09:15:00+08:00"},
        },
    }
    runtime_source = {
        "source_kind": "USER_EXPLICIT", "source_name": "unit-test-runtime",
        "source_version": "v1", "timezone": "Asia/Shanghai",
        "official_run_window": {"start_at": "08:00:00", "end_at": "09:10:00"},
        "information_cutoff_at": "09:14:59", "auction_start_at": "09:15:00",
    }
    return market, weak_identity, calendar_source, runtime_source


def _concept_decisions(market, identity):
    packet = build_concept_review_packet(market, identity, strong_identity=True)
    return {
        "review_status": "REVIEWED",
        "reviewed_by": "unit-test-reviewer",
        "reviewed_at": "2026-08-24T00:30:00+08:00",
        "review_packet_ref": packet["packet_version"],
        "market_date": packet["market_date"],
        "catalog_version": packet["catalog_version"],
        "decisions": [
            {"source_sector_id": "885311.TI", "status": "STABLE",
             "reason": "具有独立产业链含义和可验证市场代理"},
            {"source_sector_id": "885338.TI", "status": "EXCLUDED",
             "reason": "交易资格属性，不是产业主题"},
        ],
    }


class BootstrapDependencyTests(unittest.TestCase):
    def test_weak_identity_produces_complete_proposal_not_runtime_success(self):
        market, identity, calendar, runtime = _inputs()
        bundle = build_bundle(market, identity, calendar, runtime)
        manifest, registry = bundle["manifest"], bundle["theme_registry"]
        self.assertEqual(manifest["status"], "PROPOSAL_ONLY")
        self.assertFalse(manifest["runtime_eligible"])
        self.assertTrue(manifest["blocking_reasons"])
        summary = registry["accounting_summary"]
        self.assertEqual(summary["primary_count"], 5)
        self.assertEqual(summary["accounted_primary_count"], 5)
        self.assertEqual(summary["mapped_provisional_count"], 3)
        self.assertEqual(summary["excluded_primary_count"], 2)
        self.assertEqual(registry["excluded_source_sector_ids"],
                         ["000985.TI", "885338.TI"])
        self.assertTrue(all(row["lifecycle_status"] == "PROVISIONAL"
                            for row in registry["themes"]))
        self.assertEqual(registry["provisional_label_accounting"][0]
                         ["accounting_status"], "UNTRUSTED_NOT_ADMITTED")

    def test_legacy_identity_is_diagnostic_only_with_precise_blockers(self):
        market, identity, calendar, runtime = _inputs()
        identity["schema_version"] = "1.4"
        bundle = build_bundle(market, identity, calendar, runtime)
        reasons = bundle["manifest"]["blocking_reasons"]
        self.assertIn("IDENTITY_GATEWAY_SCHEMA_VERSION_UNSUPPORTED", reasons)
        self.assertIn("IDENTITY_MARKET_DATE_MISMATCH", reasons)
        self.assertIn("CLOSE_FRAME_HASH_INTERLOCK_FAILED", reasons)
        self.assertEqual(bundle["theme_registry"]["provisional_label_accounting"][0]
                         ["accounting_status"], "UNTRUSTED_NOT_ADMITTED")

    def test_strong_gate_and_review_make_industry_and_reviewed_concept_stable(self):
        market, identity, calendar, runtime = _inputs()
        decisions = _concept_decisions(market, identity)
        with patch("bootstrap_dependencies.validate_gate", return_value={"market_date": "x"}):
            bundle = build_bundle(market, identity, calendar, runtime, decisions)
        registry = bundle["theme_registry"]
        self.assertEqual(bundle["manifest"]["status"], "READY")
        lifecycle = {row["market_proxy_source_id"]: row["lifecycle_status"]
                     for row in registry["themes"]}
        self.assertEqual(lifecycle["881101.TI"], "STABLE")
        self.assertEqual(lifecycle["885311.TI"], "STABLE")
        self.assertEqual(lifecycle["999001.TI"], "PROVISIONAL")
        self.assertNotIn("885338.TI", lifecycle)
        # Stable IDs depend only on source ID, not mutable display name.
        changed = copy.deepcopy(market)
        changed["payload"]["sectors"][0]["source_name"] = "新名称"
        changed_decisions = _concept_decisions(changed, identity)
        with patch("bootstrap_dependencies.validate_gate", return_value={"market_date": "x"}):
            changed_bundle = build_bundle(changed, identity, calendar, runtime,
                                          changed_decisions)
        before = {row["market_proxy_source_id"]: row["theme_id"]
                  for row in registry["themes"]}
        after = {row["market_proxy_source_id"]: row["theme_id"]
                 for row in changed_bundle["theme_registry"]["themes"]}
        self.assertEqual(before["881101.TI"], after["881101.TI"])

    def test_strong_gate_without_concept_decisions_stays_proposal(self):
        market, identity, calendar, runtime = _inputs()
        with patch("bootstrap_dependencies.validate_gate", return_value={"market_date": "x"}):
            bundle = build_bundle(market, identity, calendar, runtime)
        self.assertEqual(bundle["manifest"]["status"], "PROPOSAL_ONLY")
        self.assertIn("CONCEPT_DECISIONS_NOT_SUPPLIED",
                      bundle["manifest"]["blocking_reasons"])
        lifecycle = {row["market_proxy_source_id"]: row["lifecycle_status"]
                     for row in bundle["theme_registry"]["themes"]}
        self.assertEqual(lifecycle["881101.TI"], "STABLE")
        self.assertEqual(lifecycle["885311.TI"], "PROVISIONAL")

    def test_flagged_concept_cannot_be_reviewed_stable_or_omitted(self):
        market, identity, calendar, runtime = _inputs()
        decisions = _concept_decisions(market, identity)
        decisions["decisions"][1]["status"] = "STABLE"
        with patch("bootstrap_dependencies.validate_gate", return_value={"market_date": "x"}):
            with self.assertRaises(BootstrapError):
                build_bundle(market, identity, calendar, runtime, decisions)
        decisions = _concept_decisions(market, identity)
        decisions["decisions"].pop()
        with patch("bootstrap_dependencies.validate_gate", return_value={"market_date": "x"}):
            with self.assertRaises(BootstrapError):
                build_bundle(market, identity, calendar, runtime, decisions)

    def test_review_packet_has_named_member_samples_and_membership_ref(self):
        market, identity, _, _ = _inputs()
        packet = build_concept_review_packet(market, identity, strong_identity=True)
        row = next(item for item in packet["concepts"]
                   if item["source_sector_id"] == "885311.TI")
        self.assertEqual(row["member_sample"][0],
                         {"code": "000001.SZ", "name": "平安银行"})
        self.assertEqual(row["membership_ref"]["membership_hash"],
                         "sha256:" + "a" * 64)

    def test_all_reference_artifacts_are_content_addressed_and_deterministic(self):
        market, identity, calendar, runtime = _inputs()
        one = build_bundle(market, identity, calendar, runtime)
        two = build_bundle(market, identity, calendar, runtime)
        self.assertEqual(one, two)
        self.assertEqual(one["theme_registry"]["registry_version"],
                         content_address_without(one["theme_registry"], "registry_version"))
        self.assertEqual(one["coverage_permission_matrix"]["matrix_version"],
                         content_address_without(one["coverage_permission_matrix"],
                                                 "matrix_version"))
        self.assertEqual(one["runtime_config"]["config_version"],
                         content_address_without(one["runtime_config"], "config_version"))
        self.assertEqual(one["trading_calendar"]["calendar_version"],
                         content_address_without(one["trading_calendar"],
                                                 "calendar_version"))

    def test_permission_matrix_has_unique_core_rules_and_no_numeric_thresholds(self):
        matrix = build_permission_matrix("2026-08-21")
        keys = [(r["subject_scope"], r["field_or_window"], r["coverage_type"],
                 r["data_health"]) for r in matrix["entries"]]
        self.assertEqual(len(keys), len(set(keys)))
        core = [row for row in matrix["entries"]
                if row["subject_scope"] == "THEME"
                and row["field_or_window"] == "CORE_OBSERVATIONS"]
        self.assertEqual({row["data_health"] for row in core},
                         {"SUFFICIENT", "LIMITED", "INVALID"})
        self.assertNotIn("threshold", json.dumps(matrix).lower())

    def test_calendar_requires_explicit_next_day_and_exact_l60(self):
        market, _, source, _ = _inputs()
        valid = build_calendar(source, market)
        self.assertEqual(valid["provenance"]["source_kind"], "USER_EXPLICIT")
        missing_next = copy.deepcopy(source)
        missing_next["trading_dates"].pop()
        missing_next["sessions"].pop(source["trading_dates"][-1])
        with self.assertRaises(BootstrapError):
            build_calendar(missing_next, market)
        invented_gap = copy.deepcopy(source)
        invented_gap["trading_dates"][10] = "2025-01-01"
        with self.assertRaises(BootstrapError):
            build_calendar(invented_gap, market)
        authority_claim_without_provenance = copy.deepcopy(source)
        authority_claim_without_provenance["source_kind"] = "AUTHORITATIVE"
        with self.assertRaises(BootstrapError):
            build_calendar(authority_claim_without_provenance, market)
        provider = copy.deepcopy(source)
        provider.update({
            "source_kind": "STRUCTURED_PROVIDER",
            "source_uri": "akshare://tool_trade_date_hist_sina",
            "captured_at": "2026-08-24T00:40:00+08:00",
        })
        provider_calendar = build_calendar(provider, market)
        self.assertEqual(provider_calendar["provenance"]["source_kind"],
                         "STRUCTURED_PROVIDER")

    def test_runtime_rejects_implicit_or_invalid_window(self):
        _, _, _, source = _inputs()
        config = build_runtime_config(source)
        self.assertEqual(config["provenance"]["source_name"], "unit-test-runtime")
        invalid = copy.deepcopy(source)
        invalid["official_run_window"]["end_at"] = "09:16:00"
        with self.assertRaises(BootstrapError):
            build_runtime_config(invalid)
        implicit = copy.deepcopy(source)
        implicit.pop("source_kind")
        with self.assertRaises(BootstrapError):
            build_runtime_config(implicit)

    def test_cli_proposal_exit_code_and_immutable_bundle(self):
        market, identity, calendar, runtime = _inputs()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, value in (("market", market), ("identity", identity),
                                ("calendar", calendar), ("runtime", runtime)):
                _write(root / f"{name}.json", value)
            command = [sys.executable, str(SCRIPTS / "bootstrap_dependencies.py"),
                       "--market-frame", str(root / "market.json"),
                       "--identity-frame", str(root / "identity.json"),
                       "--calendar-source", str(root / "calendar.json"),
                       "--runtime-source", str(root / "runtime.json"),
                       "--output-root", str(root / "out")]
            first = subprocess.run(command, text=True, capture_output=True, check=False)
            second = subprocess.run(command, text=True, capture_output=True, check=False)
            self.assertEqual(first.returncode, 2)
            self.assertEqual(second.returncode, 2)
            self.assertIn("BOOTSTRAP PROPOSAL_ONLY", first.stdout)
            manifests = list((root / "out/proposals").rglob("bootstrap-manifest.json"))
            self.assertEqual(len(manifests), 1)
            manifest = json.loads(manifests[0].read_text())
            self.assertFalse(manifest["runtime_eligible"])
            self.assertEqual(manifest["output_refs"]["theme_registry"],
                             content_hash(json.loads(
                                 (manifests[0].parent / "theme-registry.json").read_text())))


if __name__ == "__main__":
    unittest.main()
