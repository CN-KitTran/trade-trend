"""Black-box subprocess smoke/contract tests for the V3 production skeleton.

All input and output data lives in TemporaryDirectory.  These tests do not import
production functions; each CLI is exercised as a separate process.  Expected
failures are intentional and document production contract gaps rather than
weakening assertions.
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
PYTHON = sys.executable
SH_TZ = timezone(timedelta(hours=8))


def _dates(count=60, latest=date(2026, 8, 21)):
    out = []
    cur = latest
    while len(out) < count:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur -= timedelta(days=1)
    return out


def _hash(value):
    # Keep this independent from production imports: artifact hashes are canonical JSON.
    import hashlib
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _artifact(kind, schema_name, **fields):
    schema_path = ROOT / "schemas" / f"{schema_name}.schema.json"
    import hashlib
    value = {
        "artifact_kind": kind,
        "schema_version": "v3.0-draft-1",
        "schema_ref": {"id": schema_name,
                       "hash": "sha256:" + hashlib.sha256(schema_path.read_bytes()).hexdigest()},
        "producer_version": "trade-trend-analysis-v3.0.0-dev.1",
        "created_at": "2026-08-24T07:01:00+08:00",
        **fields,
    }
    value["artifact_hash"] = _hash(value)
    return value


def _envelope(data_type, payload, ts="2026-08-21T15:11:00+08:00"):
    return {"schema_version": "1.5", "data_type": data_type,
            "code": "*", "ts": ts, "source": "wencai", "payload": payload,
            "meta": {"fetch_errors": []}}


def _fixtures():
    dates = _dates()
    market_date = dates[0]
    sectors = []
    for sid, name in (("885001.TI", "主题甲"), ("885002.TI", "主题乙")):
        history = []
        for i, day in enumerate(dates):
            history.append({
                "date": day, "close": 100.0 - i * 0.1,
                "return_pct": 0.1, "amount_yi": 10.0 + i * 0.01,
                "up_count": 6, "down_count": 3, "flat_count": 1,
                "non_up_count": 4, "breadth_denominator": 10,
                "breadth_coverage": "FULL", "activity_coverage": "FULL",
            })
        sectors.append({
            "source_sector_id": sid, "source_name": name,
            "source_layer": "CONCEPT", "source_scope": "PRIMARY",
            "member_count_reported": 10, "history": history,
            "history_coverage": "FULL", "breadth_history_coverage": "FULL",
            "activity_history_coverage": "FULL",
        })
    catalog = [{
        "source_sector_id": row["source_sector_id"], "source_name": row["source_name"],
        "source_layer": "CONCEPT", "source_scope": "PRIMARY",
        "member_count_reported": 10,
    } for row in sectors]
    for index, item in enumerate(catalog):
        codes = [f"{index + 1}{number:05d}.SZ" for number in range(10)]
        item.update({"member_count": len(codes), "member_codes": codes,
                     "membership_hash": _hash(codes), "membership_coverage": "FULL"})
    # freeze_market.py currently consumes numeric close_history.  A separate test
    # below records the gateway-like dict form as a production incompatibility.
    benchmark = {
        "benchmark_id": "stable-full-a-benchmark",
        "definition_version": "sha256:" + "b" * 64,
        "provider_index_code": "000985",
        "provider_index_name": "中证全指",
        "history_coverage": "FULL", "trading_dates": list(dates),
        "close_history": [{"date": day, "close": 5000.0 - i}
                          for i, day in enumerate(dates)],
    }
    market_payload = {
        "frame_id": "sector-close-20260821-test", "frame_kind": "CLOSE",
        "requested_as_of": market_date, "market_data_as_of": market_date,
        "market_data_captured_at": "2026-08-21T15:11:00+08:00",
        "lookback_trading_days": 60, "coverage_type": "FULL",
        "core_contract_tier": "V3_CORE_L60", "close_freeze_eligible": True,
        "catalog_version": "sha256:" + "c" * 64,
        "trading_dates": dates, "benchmark": benchmark,
        "close_confirmation": {
            "exchange_close_at": "2026-08-21T15:00:00+08:00",
            "earliest_freeze_at": "2026-08-21T15:10:00+08:00",
            "capture_eligible": True,
            "supplier_latest_market_data_as_of": market_date,
            "timing_config_version": "gateway-close-timing-v1",
            "confirmation_method": "SUPPLIER_LATEST_DATE_AND_FREEZE_TIME",
        },
        "expected_sector_count": 2, "returned_sector_count": 2,
        "history_coverage": "FULL", "history_limited_sector_ids": [],
        "breadth_history_coverage": "FULL", "breadth_limited_sector_ids": [],
        "activity_history_coverage": "FULL", "activity_limited_sector_ids": [],
        "primary_sector_count": 2, "reference_sector_count": 0,
        "sectors": sectors,
        "query_version": "sector-market-frame-v3",
    }
    identity_payload = {
        "requested_as_of": market_date,
        "identity_date_semantics": "CLOSE_FREEZE_CURRENT_RELATION",
        "historical_reconstruction": False,
        "verified_close_frame_id": market_payload["frame_id"],
        "verified_close_frame_hash": "PLACEHOLDER",
        "coverage_type": "FULL", "catalog_version": market_payload["catalog_version"],
        "universe_version": "sha256:" + "d" * 64,
        "catalog_count": 2, "primary_sector_count": 2, "reference_sector_count": 0,
        "provisional_label_count": 0, "primary_membership_coverage": "FULL",
        "catalog": catalog, "provisional_labels": [],
        "query_version": "sector-identity-v2",
    }
    market_env = _envelope("sector_market_frame", market_payload)
    identity_env = _envelope("sector_identity", identity_payload)
    identity_payload["verified_close_frame_hash"] = _hash(market_env)
    identity_env = _envelope("sector_identity", identity_payload)
    calendar = {
        "calendar_version": "PENDING", "timezone": "Asia/Shanghai",
        "market": "CN_A", "trading_dates": list(reversed(dates)) + ["2026-08-24"],
        "sessions": {
            market_date: {"auction_start_at": "2026-08-21T09:15:00+08:00"},
            "2026-08-24": {"auction_start_at": "2026-08-24T09:15:00+08:00"},
        },
    }
    calendar["calendar_version"] = _hash({
        key: value for key, value in calendar.items() if key != "calendar_version"})
    registry = {
        "registry_version": "PENDING", "snapshot_hash": "PENDING",
        "effective_as_of": market_date,
        "themes": [{
            "theme_id": "theme-a", "display_name": "主题甲",
            "lifecycle_status": "STABLE", "market_proxy_source_id": "885001.TI",
            "source_bindings": [{"source_kind": "SOURCE_SECTOR", "source_id": "885001.TI", "valid_to": None}],
        }, {
            "theme_id": "theme-b", "display_name": "主题乙",
            "lifecycle_status": "STABLE", "market_proxy_source_id": "885002.TI",
            "source_bindings": [{"source_kind": "SOURCE_SECTOR", "source_id": "885002.TI", "valid_to": None}],
        }],
        "excluded_source_sector_ids": [],
    }
    registry["snapshot_hash"] = _hash({
        key: value for key, value in registry.items()
        if key not in {"registry_version", "snapshot_hash", "previous_registry_version"}})
    registry["registry_version"] = _hash({
        key: value for key, value in registry.items() if key != "registry_version"})
    matrix = {"matrix_version": "PENDING", "entries": [
        {"subject_scope": "THEME", "field_or_window": "CORE_OBSERVATIONS", "data_health": "SUFFICIENT", "coverage_type": "FULL", "allowed_sensing_signals": ["NONE", "WATCH", "CANDIDATE"]},
        {"subject_scope": "THEME", "field_or_window": "CORE_OBSERVATIONS", "data_health": "LIMITED", "coverage_type": "FULL", "allowed_sensing_signals": ["NONE", "WATCH"]},
    ]}
    matrix["matrix_version"] = _hash({
        key: value for key, value in matrix.items() if key != "matrix_version"})
    runtime = {
        "config_version": "PENDING", "timezone": "Asia/Shanghai",
        "official_run_window": {"start_at": "08:00:00", "end_at": "09:10:00"},
        "information_cutoff_at": "09:14:59", "auction_start_at": "09:15:00",
    }
    runtime["config_version"] = _hash({
        key: value for key, value in runtime.items() if key != "config_version"})
    return market_env, identity_env, calendar, registry, matrix, runtime


class V3SubprocessContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "inputs").mkdir()
        self.market, self.identity, self.calendar, self.registry, self.matrix, self.runtime = _fixtures()
        self.market_path = self.root / "inputs/market.json"
        self.identity_path = self.root / "inputs/identity.json"
        self.calendar_path = self.root / "inputs/calendar.json"
        self.registry_path = self.root / "inputs/registry.json"
        self.matrix_path = self.root / "inputs/matrix.json"
        self.runtime_path = self.root / "inputs/runtime.json"
        _write(self.market_path, self.market)
        _write(self.identity_path, self.identity)
        _write(self.calendar_path, self.calendar)
        _write(self.registry_path, self.registry)
        _write(self.matrix_path, self.matrix)
        _write(self.runtime_path, self.runtime)

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, script, *args):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SCRIPTS) + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.run([PYTHON, str(SCRIPTS / script), *map(str, args)],
                              cwd=str(SCRIPTS), env=env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def market_regime(self, sensing_path: Path, stem: str):
        """Build a valid neutral V0.52 context without nominating review cases."""
        packet_path = self.root / f"inputs/{stem}-market-regime-packet.json"
        result = self.run_script(
            "model_io.py", "market-regime-packet", "--sensing", sensing_path,
            "--output", packet_path)
        self.assertEqual(result.returncode, 0, result.stdout)
        packet = json.loads(packet_path.read_text())
        sensing = json.loads(sensing_path.read_text())
        refs = [
            ref for card in sensing["theme_cards"]
            for ref in card.get("evidence_catalog", [])
        ]
        self.assertTrue(refs, "market-regime fixture requires one frozen market ref")
        regime = _artifact(
            "market_regime", "market-regime",
            regime_input_hash=packet["regime_input_hash"],
            sensing_ref=packet["sensing_ref"],
            market_regime="无明确主线",
            risk_appetite="中性",
            capital_migration={
                "from_theme_ids": [], "to_theme_ids": [],
                "from_summary": "无明确流出方向",
                "to_summary": "无明确承接方向",
            },
            duration="尚未形成",
            contradictions=[],
            confidence={"level": "LOW", "reason": "全部主题均无候选"},
            evidence_refs=[refs[0]],
            limitations=["ZERO_REVIEW_CASES"],
            regime_review_nominations=[],
            correction_attempts=0,
        )
        regime_path = self.root / f"inputs/{stem}-market-regime.json"
        _write(regime_path, regime)
        plan_path = self.root / f"inputs/{stem}-evidence-plan.json"
        result = self.run_script(
            "model_io.py", "evidence-plan", "--sensing", sensing_path,
            "--market-regime", regime_path, "--output", plan_path)
        self.assertEqual(result.returncode, 0, result.stdout)
        plan = json.loads(plan_path.read_text())
        self.assertEqual(plan["case_count"], 0)
        return regime_path, regime, plan

    def freeze(self, release="INTERNAL_GATE", amendment_reason=None):
        args = [
            "freeze_market.py", "--market-frame", self.market_path,
            "--identity-frame", self.identity_path,
            "--trading-calendar", self.calendar_path,
            "--theme-registry", self.registry_path,
            "--permission-matrix", self.matrix_path,
            "--release-mode", release, "--output-root", self.root / "data",
        ]
        if amendment_reason:
            args.extend(["--amendment-reason", amendment_reason])
        return self.run_script(*args)

    def test_synthetic_close_freeze_succeeds(self):
        result = self.freeze()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("CLOSE_FREEZE PASS", result.stdout)
        self.freeze_dir = self.root / "data/internal/market/2026-08/2026-08-21/close/v1"
        self.assertTrue((self.freeze_dir / "manifest.json").exists())
        self.assertTrue((self.freeze_dir / "observations.json").exists())

    def test_synthetic_close_freeze_to_preopen_draft(self):
        """Successful synthetic CLOSE_FREEZE -> early PREOPEN DRAFT smoke."""
        result = self.freeze()
        self.assertEqual(result.returncode, 0, result.stdout)
        freeze_dir = self.root / "data/internal/market/2026-08/2026-08-21/close/v1"
        result = self.run_script(
            "run_preopen.py", "--freeze-dir", freeze_dir,
            "--theme-registry", self.registry_path,
            "--permission-matrix", self.matrix_path,
            "--runtime-config", self.runtime_path,
            "--decision-date", "2026-08-24", "--now", "2026-08-24T07:00:00+08:00",
            "--first-run",
            "--release-mode", "INTERNAL_GATE", "--output-root", self.root / "data")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("PREOPEN DRAFT", result.stdout)

    def test_early_internal_run_can_render_explicit_unpublished_preview(self):
        result = self.freeze()
        self.assertEqual(result.returncode, 0, result.stdout)
        freeze_dir = self.root / "data/internal/market/2026-08/2026-08-21/close/v1"
        draft = self.run_script(
            "run_preopen.py", "--freeze-dir", freeze_dir,
            "--theme-registry", self.registry_path,
            "--permission-matrix", self.matrix_path,
            "--runtime-config", self.runtime_path,
            "--decision-date", "2026-08-24", "--now", "2026-08-24T07:00:00+08:00",
            "--first-run", "--release-mode", "INTERNAL_GATE",
            "--output-root", self.root / "data")
        self.assertEqual(draft.returncode, 0, draft.stdout)
        draft_sensing = json.loads((
            self.root / "data/internal/runs/2026-08/2026-08-24/preopen/v1/sensing.json"
        ).read_text())
        supplied = {
            "sensing_input_hash": draft_sensing["sensing_input_hash"],
            "correction_attempts": 0,
            "theme_decisions": [{
                "theme_id": card["theme_id"],
                "opportunity": {"signal": "NONE", "structure_type": None,
                                "path_pattern": None, "evidence_refs": [], "reason": "无候选"},
                "risk": {"signal": "NONE", "structure_type": None,
                         "path_pattern": None, "evidence_refs": [], "reason": "无候选"},
                "derived_decision": "NONE",
            } for card in draft_sensing["theme_cards"]],
            "reconciliation": [],
        }
        supplied_path = self.root / "inputs/early-sensing-output.json"
        _write(supplied_path, supplied)
        ready = self.run_script(
            "run_preopen.py", "--freeze-dir", freeze_dir,
            "--theme-registry", self.registry_path,
            "--permission-matrix", self.matrix_path,
            "--runtime-config", self.runtime_path,
            "--decision-date", "2026-08-24", "--now", "2026-08-24T07:00:00+08:00",
            "--sensing-output", supplied_path, "--first-run",
            "--release-mode", "INTERNAL_GATE", "--output-root", self.root / "data")
        self.assertEqual(ready.returncode, 0, ready.stdout)
        run_dir = self.root / "data/internal/runs/2026-08/2026-08-24/preopen/v2"
        sensing = json.loads((run_dir / "sensing.json").read_text())
        self.assertEqual(sensing["information_cutoff"], "2026-08-24T07:00:00+08:00")
        regime_path, regime, evidence_plan = self.market_regime(
            run_dir / "sensing.json", "early")
        evidence_path = self.root / "inputs/early-evidence.json"
        judgments_path = self.root / "inputs/early-judgments.json"
        _write(evidence_path, _artifact(
            "evidence", "evidence", information_cutoff=sensing["information_cutoff"],
            evidence_plan_hash=evidence_plan["evidence_plan_hash"],
            evidence_items=[], case_coverage=[]))
        _write(judgments_path, _artifact(
            "theme_judgments", "theme-judgments", review_theme_ids=[], themes=[],
            market_regime_ref=regime["artifact_hash"],
            regime_input_hash=regime["regime_input_hash"]))
        ledger = self.run_script(
            "update_ledger.py", "--run-dir", run_dir,
            "--theme-judgments", judgments_path, "--evidence", evidence_path,
            "--market-regime", regime_path)
        self.assertEqual(ledger.returncode, 0, ledger.stdout)
        ledger_value = json.loads((run_dir / "ledger.json").read_text())
        self.assertEqual(ledger_value["run_window_status"], "EARLY_DRAFT")
        self.assertIn("EARLY_INTERNAL_PREVIEW_AS_OF_RUN_START",
                      ledger_value["publication_limitations"])
        preview = self.run_script(
            "render_report.py", "--ledger", run_dir / "ledger.json",
            "--output", run_dir / "preview.md")
        self.assertEqual(preview.returncode, 0, preview.stdout)
        text = (run_dir / "preview.md").read_text()
        self.assertIn("内部早稿", text)
        self.assertIn("不会成为正式台账或 Vault 正式报告", text)
        vault_root = self.root / "vault"
        (vault_root / ".obsidian").mkdir(parents=True)
        published = self.run_script(
            "render_report.py", "--ledger", run_dir / "ledger.json",
            "--output", run_dir / "preview.md", "--vault-root", vault_root)
        self.assertEqual(published.returncode, 0, published.stdout)
        vault_report = (vault_root / "investment/trend/2026-08/W35/"
                        "2026-08-24-板块扫描.md")
        self.assertEqual(vault_report.read_text(), text)
        vault_report.write_text("existing user note", encoding="utf-8")
        conflict = self.run_script(
            "render_report.py", "--ledger", run_dir / "ledger.json",
            "--output", run_dir / "preview.md", "--vault-root", vault_root)
        self.assertNotEqual(conflict.returncode, 0)
        self.assertIn("VAULT_REPORT_CONFLICT_REQUIRES_EXPLICIT_AMENDMENT",
                      conflict.stdout)
        self.assertEqual(vault_report.read_text(), "existing user note")

    def test_validated_empty_market_pipeline_to_immutable_ledger_and_report(self):
        result = self.freeze()
        self.assertEqual(result.returncode, 0, result.stdout)
        freeze_dir = self.root / "data/internal/market/2026-08/2026-08-21/close/v1"
        draft = self.run_script(
            "run_preopen.py", "--freeze-dir", freeze_dir,
            "--theme-registry", self.registry_path,
            "--permission-matrix", self.matrix_path,
            "--runtime-config", self.runtime_path,
            "--decision-date", "2026-08-24", "--now", "2026-08-24T08:30:00+08:00",
            "--first-run",
            "--release-mode", "INTERNAL_GATE", "--output-root", self.root / "data")
        self.assertEqual(draft.returncode, 0, draft.stdout)
        draft_dir = self.root / "data/internal/runs/2026-08/2026-08-24/preopen/v1"
        draft_sensing = json.loads((draft_dir / "sensing.json").read_text())
        supplied = {
            "sensing_input_hash": draft_sensing["sensing_input_hash"],
            "correction_attempts": 0,
            "theme_decisions": [{
                "theme_id": card["theme_id"],
                "opportunity": {"signal": "NONE", "structure_type": None,
                                "path_pattern": None, "evidence_refs": [], "reason": "无候选"},
                "risk": {"signal": "NONE", "structure_type": None,
                         "path_pattern": None, "evidence_refs": [], "reason": "无候选"},
                "derived_decision": "NONE",
            } for card in draft_sensing["theme_cards"]],
            "reconciliation": [],
        }
        supplied_path = self.root / "inputs/sensing-output.json"
        _write(supplied_path, supplied)
        ready = self.run_script(
            "run_preopen.py", "--freeze-dir", freeze_dir,
            "--theme-registry", self.registry_path,
            "--permission-matrix", self.matrix_path,
            "--runtime-config", self.runtime_path,
            "--decision-date", "2026-08-24", "--now", "2026-08-24T08:30:00+08:00",
            "--sensing-output", supplied_path,
            "--first-run",
            "--release-mode", "INTERNAL_GATE", "--output-root", self.root / "data")
        self.assertEqual(ready.returncode, 0, ready.stdout)
        self.assertIn("READY_FOR_THEME_REVIEW", ready.stdout)
        run_dir = self.root / "data/internal/runs/2026-08/2026-08-24/preopen/v2"
        sensing = json.loads((run_dir / "sensing.json").read_text())
        regime_path, regime, evidence_plan = self.market_regime(
            run_dir / "sensing.json", "eligible")
        evidence = _artifact(
            "evidence", "evidence", information_cutoff=sensing["information_cutoff"],
            evidence_plan_hash=evidence_plan["evidence_plan_hash"],
            evidence_items=[], case_coverage=[])
        judgments = _artifact(
            "theme_judgments", "theme-judgments", review_theme_ids=[], themes=[],
            market_regime_ref=regime["artifact_hash"],
            regime_input_hash=regime["regime_input_hash"])
        evidence_path = self.root / "inputs/evidence.json"
        judgments_path = self.root / "inputs/judgments.json"
        _write(evidence_path, evidence)
        _write(judgments_path, judgments)
        ledger = self.run_script(
            "update_ledger.py", "--run-dir", run_dir,
            "--theme-judgments", judgments_path, "--evidence", evidence_path,
            "--market-regime", regime_path)
        self.assertEqual(ledger.returncode, 0, ledger.stdout)
        self.assertIn("LEDGER PASS", ledger.stdout)
        ledger_retry = self.run_script(
            "update_ledger.py", "--run-dir", run_dir,
            "--theme-judgments", judgments_path, "--evidence", evidence_path,
            "--market-regime", regime_path)
        self.assertEqual(ledger_retry.returncode, 0, ledger_retry.stdout)
        self.assertIn("LEDGER IDEMPOTENT", ledger_retry.stdout)
        report_path = run_dir / "preview.md"
        report = self.run_script(
            "render_report.py", "--ledger", run_dir / "ledger.json", "--output", report_path)
        self.assertEqual(report.returncode, 0, report.stdout)
        self.assertTrue(report_path.exists())
        report_retry = self.run_script(
            "render_report.py", "--ledger", run_dir / "ledger.json", "--output", report_path)
        self.assertEqual(report_retry.returncode, 0, report_retry.stdout)
        self.assertIn("REPORT IDEMPOTENT", report_retry.stdout)
        escaped_report = self.run_script(
            "render_report.py", "--ledger", run_dir / "ledger.json",
            "--output", self.root / "outputs/formal-looking-report.md")
        self.assertNotEqual(escaped_report.returncode, 0, escaped_report.stdout)
        self.assertIn("PREVIEW_OUTPUT_MUST_BE_BESIDE_LEDGER", escaped_report.stdout)
        previous_attempt = self.run_script(
            "update_ledger.py", "--run-dir", run_dir,
            "--theme-judgments", judgments_path, "--evidence", evidence_path,
            "--market-regime", regime_path,
            "--previous-ledger", run_dir / "ledger.json")
        self.assertNotEqual(previous_attempt.returncode, 0, previous_attempt.stdout)
        self.assertIn("PREVIOUS_RUN_CONTINUITY_NOT_IMPLEMENTED", previous_attempt.stdout)

        # A model cannot inject a market conclusion or a fake hard-fact alert
        # through fields that do not yet have deterministic lifecycle validators.
        another_ready = self.run_script(
            "run_preopen.py", "--freeze-dir", freeze_dir,
            "--theme-registry", self.registry_path,
            "--permission-matrix", self.matrix_path,
            "--runtime-config", self.runtime_path,
            "--decision-date", "2026-08-24", "--now", "2026-08-24T08:30:00+08:00",
            "--sensing-output", supplied_path, "--first-run",
            "--release-mode", "INTERNAL_GATE", "--output-root", self.root / "data")
        self.assertEqual(another_ready.returncode, 0, another_ready.stdout)
        injected_run = self.root / "data/internal/runs/2026-08/2026-08-24/preopen/v3"
        injected_regime_path, injected_regime, _ = self.market_regime(
            injected_run / "sensing.json", "injected")
        injected = _artifact(
            "theme_judgments", "theme-judgments", review_theme_ids=[], themes=[],
            market_regime_ref=injected_regime["artifact_hash"],
            regime_input_hash=injected_regime["regime_input_hash"],
            daily_summary="伪造机会结论",
            alert_items=[{"theme_id": "fake", "factual_change": "伪事实"}])
        injected_path = self.root / "inputs/injected-judgments.json"
        _write(injected_path, injected)
        rejected = self.run_script(
            "update_ledger.py", "--run-dir", injected_run,
            "--theme-judgments", injected_path, "--evidence", evidence_path,
            "--market-regime", injected_regime_path)
        self.assertNotEqual(rejected.returncode, 0, rejected.stdout)
        self.assertIn("UNVALIDATED_LEDGER_PROJECTION_FIELD_FORBIDDEN", rejected.stdout)

    def test_missing_l60_fails_closed(self):
        self.market["payload"]["lookback_trading_days"] = 20
        self.market["payload"]["trading_dates"] = self.market["payload"]["trading_dates"][:20]
        self.market["payload"]["benchmark"]["trading_dates"] = self.market["payload"]["trading_dates"]
        self.market["payload"]["benchmark"]["close_history"] = self.market["payload"]["benchmark"]["close_history"][:20]
        _write(self.market_path, self.market)
        result = self.freeze()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LOOKBACK_L60_REQUIRED", result.stdout)

    def test_non_core_l60_marker_fails_closed(self):
        self.market["payload"]["close_freeze_eligible"] = False
        self.identity["payload"]["verified_close_frame_hash"] = _hash(self.market)
        _write(self.market_path, self.market)
        _write(self.identity_path, self.identity)
        result = self.freeze()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("V3_CORE_CLOSE_FREEZE_NOT_ELIGIBLE", result.stdout)

    def test_wrong_gateway_schema_version_fails_closed(self):
        self.market["schema_version"] = "gateway-test-v1"
        self.identity["payload"]["verified_close_frame_hash"] = _hash(self.market)
        _write(self.market_path, self.market)
        _write(self.identity_path, self.identity)
        result = self.freeze()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("SECTOR_MARKET_FRAME_ENVELOPE_INVALID", result.stdout)

    def test_market_identity_scope_swap_fails_closed(self):
        row = self.identity["payload"]["catalog"][0]
        row["source_scope"] = "REFERENCE"
        row["membership_coverage"] = "NOT_REQUIRED_REFERENCE"
        row["member_count"] = None
        row["membership_hash"] = None
        self.identity["payload"]["primary_sector_count"] = 1
        self.identity["payload"]["reference_sector_count"] = 1
        _write(self.identity_path, self.identity)
        result = self.freeze()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("MARKET_IDENTITY_SOURCE_CLASSIFICATION_MISMATCH", result.stdout)

    def test_latest_breadth_accounting_error_fails_closed(self):
        self.market["payload"]["sectors"][0]["history"][0]["non_up_count"] = 99
        self.identity["payload"]["verified_close_frame_hash"] = _hash(self.market)
        _write(self.market_path, self.market)
        _write(self.identity_path, self.identity)
        result = self.freeze()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("PRIMARY_LATEST_CORE_FIELD_INVALID", result.stdout)

    def test_latest_up_vs_non_up_only_passes_without_inventing_balance(self):
        latest = self.market["payload"]["sectors"][0]["history"][0]
        latest.update({"down_count": None, "flat_count": None,
                       "non_up_count": 4, "breadth_denominator": 10,
                       "breadth_coverage": "UP_VS_NON_UP_ONLY"})
        self.identity["payload"]["verified_close_frame_hash"] = _hash(self.market)
        _write(self.market_path, self.market)
        _write(self.identity_path, self.identity)
        result = self.freeze()
        self.assertEqual(result.returncode, 0, result.stdout)
        observations = json.loads((
            self.root / "data/internal/market/2026-08/2026-08-21/close/v1/observations.json"
        ).read_text())
        row = next(item for item in observations["source_observations"]
                   if item["source_sector_id"] == "885001.TI")
        self.assertEqual(row["metrics"]["breadth"]["up_ratio_today"], 0.6)
        self.assertIsNone(row["metrics"]["breadth"]["down_ratio_today"])
        self.assertIsNone(row["metrics"]["breadth"]["balance_path_5d"][-1])
        self.assertEqual(row["data_health"]["breadth"], "LIMITED")

    def test_short_60d_attention_history_is_explicitly_limited(self):
        limited_ids = []
        for sector in self.market["payload"]["sectors"]:
            limited_ids.append(sector["source_sector_id"])
            sector["activity_history_coverage"] = "LIMITED"
            for row in sector["history"][21:]:
                row["amount_yi"] = None
                row["activity_coverage"] = "MISSING"
        self.market["payload"]["activity_history_coverage"] = "LIMITED"
        self.market["payload"]["activity_limited_sector_ids"] = limited_ids
        self.market["meta"]["degraded"] = True
        self.identity["payload"]["verified_close_frame_hash"] = _hash(self.market)
        _write(self.market_path, self.market)
        _write(self.identity_path, self.identity)
        result = self.freeze()
        self.assertEqual(result.returncode, 0, result.stdout)
        observations = json.loads((
            self.root / "data/internal/market/2026-08/2026-08-21/close/v1/observations.json"
        ).read_text())
        primary = [row for row in observations["source_observations"]
                   if row["source_scope"] == "PRIMARY"]
        self.assertTrue(primary)
        for row in primary:
            self.assertEqual(row["data_health"]["attention"], "LIMITED")
            self.assertEqual(row["metrics"]["attention"]["valid_day_counts"]["60D_HISTORY"], 21)
            self.assertIn("ATTENTION_60D_HISTORY_LIMITED", row["limitations"])

    def test_unmapped_provisional_label_is_accounted_as_no_direction(self):
        self.identity["payload"]["provisional_labels"] = [{
            "provisional_id": "p-new", "source_label": "新主题标签",
            "label_origin": "STOCK_CONCEPT", "member_count": 2,
            "member_codes": ["000001.SZ", "000002.SZ"],
            "membership_hash": _hash(["000001.SZ", "000002.SZ"]),
            "membership_coverage": "FULL_STOCK_RELATION_SCAN",
            "market_proxy_status": "MISSING",
        }]
        self.identity["payload"]["provisional_label_count"] = 1
        _write(self.identity_path, self.identity)
        result = self.freeze()
        self.assertEqual(result.returncode, 0, result.stdout)
        freeze_dir = self.root / "data/internal/market/2026-08/2026-08-21/close/v1"
        result = self.run_script(
            "run_preopen.py", "--freeze-dir", freeze_dir,
            "--theme-registry", self.registry_path,
            "--permission-matrix", self.matrix_path,
            "--runtime-config", self.runtime_path,
            "--decision-date", "2026-08-24", "--now", "2026-08-24T07:00:00+08:00",
            "--first-run", "--release-mode", "INTERNAL_GATE",
            "--output-root", self.root / "data")
        self.assertEqual(result.returncode, 0, result.stdout)
        sensing = json.loads((
            self.root / "data/internal/runs/2026-08/2026-08-24/preopen/v1/sensing.json"
        ).read_text())
        rows = [row for row in sensing["limited_or_excluded"]
                if row.get("provisional_id") == "p-new"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "NO_DIRECTION")

    def test_wrong_benchmark_identity_fails_closed(self):
        self.market["payload"]["benchmark"]["provider_index_code"] = "000300"
        self.identity["payload"]["verified_close_frame_hash"] = _hash(self.market)
        _write(self.market_path, self.market)
        _write(self.identity_path, self.identity)
        result = self.freeze()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BENCHMARK_STRONG_CONTRACT_MISSING", result.stdout)

    def test_l60_dates_must_match_versioned_calendar(self):
        self.calendar["trading_dates"].pop(10)
        self.calendar["calendar_version"] = _hash({
            key: value for key, value in self.calendar.items() if key != "calendar_version"})
        _write(self.calendar_path, self.calendar)
        result = self.freeze()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MARKET_DATES_CALENDAR_MISMATCH", result.stdout)

    def test_benchmark_wrong_date_fails_closed(self):
        self.market["payload"]["benchmark"]["trading_dates"][0] = "2026-08-20"
        # Keep identity's frame reference valid so the intended benchmark error,
        # rather than the upstream hash error, is the asserted failure.
        self.identity["payload"]["verified_close_frame_hash"] = _hash(self.market)
        _write(self.market_path, self.market)
        _write(self.identity_path, self.identity)
        result = self.freeze()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BENCHMARK_DATE_ALIGNMENT_FAILED", result.stdout)

    def test_identity_hash_mismatch_fails_closed(self):
        self.identity["payload"]["verified_close_frame_hash"] = "sha256:" + "0" * 64
        _write(self.identity_path, self.identity)
        result = self.freeze()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CLOSE_FRAME_HASH_INTERLOCK_FAILED", result.stdout)

    def test_decision_window_identity_allows_friday_to_monday_preopen(self):
        payload = self.identity["payload"]
        payload.update({
            "market_data_as_of": "2026-08-21",
            "decision_date": "2026-08-24",
            "identity_date_semantics": "DECISION_WINDOW_CURRENT_RELATION",
            "identity_observed_at": "2026-08-23T23:44:00+08:00",
            "next_auction_at": "2026-08-24T09:15:00+08:00",
            "decision_window_cutoff_at": "2026-08-24T09:15:00+08:00",
            "decision_window_config_version": "sha256:" + "e" * 64,
        })
        _write(self.identity_path, self.identity)
        result = self.freeze()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("CLOSE_FREEZE PASS", result.stdout)

    def test_decision_window_identity_at_auction_fails_closed(self):
        payload = self.identity["payload"]
        payload.update({
            "market_data_as_of": "2026-08-21",
            "decision_date": "2026-08-24",
            "identity_date_semantics": "DECISION_WINDOW_CURRENT_RELATION",
            "identity_observed_at": "2026-08-24T09:15:00+08:00",
            "next_auction_at": "2026-08-24T09:15:00+08:00",
            "decision_window_cutoff_at": "2026-08-24T09:15:00+08:00",
            "decision_window_config_version": "sha256:" + "e" * 64,
        })
        _write(self.identity_path, self.identity)
        result = self.freeze()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("IDENTITY_OUTSIDE_DECISION_WINDOW", result.stdout)

    def test_missing_calendar_fails_closed(self):
        self.calendar.pop("trading_dates")
        _write(self.calendar_path, self.calendar)
        result = self.freeze()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TRADING_CALENDAR_CONTRACT_MISSING", result.stdout)

    def test_duplicate_close_freeze_is_idempotent_for_same_input(self):
        first = self.freeze()
        self.assertEqual(first.returncode, 0, first.stdout)
        second = self.freeze()
        self.assertEqual(second.returncode, 0, second.stdout)
        self.assertIn("CLOSE_FREEZE IDEMPOTENT", second.stdout)
        self.assertTrue((self.root / "data/internal/market/2026-08/2026-08-21/close/v1").exists())
        self.assertFalse((self.root / "data/internal/market/2026-08/2026-08-21/close/v2").exists())

    def test_different_input_requires_explicit_amendment_and_references_original(self):
        first = self.freeze()
        self.assertEqual(first.returncode, 0, first.stdout)
        old_dir = self.root / "data/internal/market/2026-08/2026-08-21/close/v1"
        old_manifest = json.loads((old_dir / "manifest.json").read_text())
        # Deterministic input repair: change one close, then refresh the identity
        # frame hash so the expected failure is amendment conflict, not hash mismatch.
        self.market["payload"]["sectors"][0]["history"][0]["close"] += 1.0
        self.identity["payload"]["verified_close_frame_hash"] = _hash(self.market)
        _write(self.market_path, self.market)
        _write(self.identity_path, self.identity)
        conflict = self.freeze()
        self.assertNotEqual(conflict.returncode, 0, conflict.stdout)
        self.assertIn("CLOSE_FREEZE_INPUT_CONFLICT_REQUIRES_AMENDMENT", conflict.stdout)
        amendment = self.freeze(amendment_reason="deterministic_data_repair")
        self.assertEqual(amendment.returncode, 0, amendment.stdout)
        new_dir = self.root / "data/internal/market/2026-08/2026-08-21/close/v2"
        self.assertTrue(new_dir.exists())
        new_manifest = json.loads((new_dir / "manifest.json").read_text())
        self.assertEqual(new_manifest["amends_freeze_ref"], old_manifest["artifact_hash"])
        self.assertEqual(new_manifest["amendment_reason"], "deterministic_data_repair")

    def test_internal_freeze_cannot_be_released_as_official(self):
        result = self.freeze("INTERNAL_GATE")
        self.assertEqual(result.returncode, 0, result.stdout)
        freeze_dir = self.root / "data/internal/market/2026-08/2026-08-21/close/v1"
        result = self.run_script(
            "run_preopen.py", "--freeze-dir", freeze_dir,
            "--theme-registry", self.registry_path,
            "--permission-matrix", self.matrix_path,
            "--runtime-config", self.runtime_path,
            "--decision-date", "2026-08-24", "--now", "2026-08-24T08:30:00+08:00",
            "--first-run",
            "--release-mode", "OFFICIAL", "--output-root", self.root / "data")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("OFFICIAL_EXECUTION_AND_PUBLICATION_NOT_IMPLEMENTED", result.stdout)

    def test_build_observations_cli_is_reproducible(self):
        output = self.root / "outputs/observations.json"
        result = self.run_script("build_observations.py", "--market-frame", self.market_path,
                                "--identity-frame", self.identity_path, "--output", output)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(output.exists())
        artifact = json.loads(output.read_text())
        self.assertEqual(artifact["artifact_kind"], "observations")
        self.assertEqual(artifact["market_data_as_of"], "2026-08-21")

    def test_validate_outputs_cli_rejects_missing_model_decisions(self):
        source = self.root / "inputs/sensing.json"
        output = self.root / "inputs/model-output.json"
        _write(source, {"theme_cards": [{"theme_id": "theme-a"}]})
        _write(output, {})
        result = self.run_script("validate_outputs.py", "--kind", "sensing",
                                "--input", source, "--output", output)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("THEME_DECISIONS_NOT_ARRAY", result.stdout)

    def test_update_ledger_cli_rejects_close_freeze_as_preopen_run(self):
        result = self.freeze()
        self.assertEqual(result.returncode, 0, result.stdout)
        freeze_dir = self.root / "data/internal/market/2026-08/2026-08-21/close/v1"
        judgments = self.root / "inputs/judgments.json"
        evidence = self.root / "inputs/evidence.json"
        regime = self.root / "inputs/market-regime.json"
        _write(judgments, {})
        _write(evidence, {})
        _write(regime, _artifact(
            "market_regime", "market-regime",
            regime_input_hash="sha256:" + "1" * 64,
            sensing_ref="sha256:" + "2" * 64,
            market_regime="无明确主线", risk_appetite="中性",
            capital_migration={
                "from_theme_ids": [], "to_theme_ids": [],
                "from_summary": "无明确流出方向", "to_summary": "无明确承接方向"},
            duration="尚未形成", contradictions=[],
            confidence={"level": "LOW", "reason": "无盘前感知输入"},
            evidence_refs=["market:unavailable"], limitations=["INVALID_RUN_DIR_FIXTURE"],
            regime_review_nominations=[], correction_attempts=0))
        result = self.run_script("update_ledger.py", "--run-dir", freeze_dir,
                                "--theme-judgments", judgments, "--evidence", evidence,
                                "--market-regime", regime)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ARTIFACT_KIND_MISMATCH", result.stdout)

    def test_render_report_cli_rejects_close_freeze_manifest_as_ledger(self):
        result = self.freeze()
        self.assertEqual(result.returncode, 0, result.stdout)
        manifest = self.root / "data/internal/market/2026-08/2026-08-21/close/v1/manifest.json"
        result = self.run_script("render_report.py", "--ledger", manifest,
                                "--output", self.root / "outputs/report.md")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ARTIFACT_KIND_MISMATCH", result.stdout)

    def test_gateway_like_dict_benchmark_is_normalized_and_freeze_succeeds(self):
        self.market["payload"]["benchmark"]["close_history"] = [
            {"date": d, "close": 5000.0 - i}
            for i, d in enumerate(self.market["payload"]["trading_dates"])
        ]
        self.identity["payload"]["verified_close_frame_hash"] = _hash(self.market)
        _write(self.market_path, self.market)
        _write(self.identity_path, self.identity)
        result = self.freeze()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("CLOSE_FREEZE PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
