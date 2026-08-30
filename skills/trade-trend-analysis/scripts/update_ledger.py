#!/usr/bin/env python3
"""Create an immutable deterministic ledger from validated frozen judgments."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
from pathlib import Path

from v3_common import (ContractError, artifact, atomic_write_json, content_hash,
                       load_json, now_iso, parse_ts, read_artifact_checked,
                       validate_artifact_value)
from validate_outputs import validate_sensing, validate_theme_judgments


def _validate_evidence(evidence: dict, cutoff: str) -> set[str]:
    validate_artifact_value(evidence, "evidence", "evidence input")
    items = evidence.get("evidence_items")
    if not isinstance(items, list):
        raise ContractError(["EVIDENCE_ITEMS_NOT_ARRAY"])
    ids = []
    for item in items:
        required = ("evidence_id", "source_uri", "source_tier", "published_at",
                    "captured_at", "content_hash", "fact", "trust_boundary")
        if any(k not in item for k in required):
            raise ContractError(["EVIDENCE_PROVENANCE_INCOMPLETE"])
        if parse_ts(item["published_at"]) > parse_ts(cutoff):
            raise ContractError(["EVIDENCE_AFTER_INFORMATION_CUTOFF"])
        # ``information_cutoff`` governs when the fact became public, not when
        # this run fetched it.  Targeted verification necessarily completes
        # after the sensing cutoff; rejecting that clock would make a real
        # evidence pass impossible.  The earlier vertical slice compounded
        # that problem by having no case-coverage gate.
        if parse_ts(item["captured_at"]) < parse_ts(item["published_at"]):
            raise ContractError(["EVIDENCE_CAPTURED_BEFORE_PUBLICATION"])
        if not isinstance(item["content_hash"], str) or not re.fullmatch(
                r"sha256:[0-9a-f]{64}", item["content_hash"]):
            raise ContractError(["EVIDENCE_CONTENT_HASH_INVALID"])
        if item["trust_boundary"] != "EXTERNAL_UNTRUSTED_CONTENT_ISOLATED":
            raise ContractError(["EVIDENCE_TRUST_BOUNDARY_MISSING"])
        ids.append(item["evidence_id"])
    if len(ids) != len(set(ids)):
        raise ContractError(["DUPLICATE_EVIDENCE_ID"])
    return set(ids)


def validate_evidence_case_coverage(evidence: dict, evidence_plan: dict,
                                    evidence_ids: set[str], cutoff: str) -> list[str]:
    """Require one auditable verification result for every planned case axis."""
    errors = []
    if evidence.get("evidence_plan_hash") != evidence_plan.get("evidence_plan_hash"):
        errors.append("EVIDENCE_PLAN_HASH_MISMATCH")
    expected = {row.get("case_id"): row for row in evidence_plan.get("cases") or []}
    if len(expected) != evidence_plan.get("case_count"):
        errors.append("EVIDENCE_PLAN_CASE_ACCOUNTING_INVALID")
    coverage = evidence.get("case_coverage")
    if not isinstance(coverage, list):
        return sorted(set(errors + ["EVIDENCE_CASE_COVERAGE_NOT_ARRAY"]))
    # An empty package is legitimate only when the deterministic plan itself
    # has zero cases.  The 2026-08-24 failure had many planned cases and is
    # therefore still rejected below rather than being treated as "no facts".
    if not coverage:
        if expected:
            errors.append("EVIDENCE_CASE_COVERAGE_EMPTY")
        if evidence_ids:
            errors.append("EVIDENCE_ITEMS_NOT_MAPPED_TO_CASE")
        return sorted(set(errors))
    actual_ids = [row.get("case_id") for row in coverage if isinstance(row, dict)]
    if len(actual_ids) != len(coverage) or len(actual_ids) != len(set(actual_ids)):
        errors.append("EVIDENCE_CASE_COVERAGE_DUPLICATE_OR_INVALID")
    if set(actual_ids) != set(expected):
        errors.append("EVIDENCE_CASE_COVERAGE_ACCOUNTING_MISMATCH")
    used_refs = set()
    item_by_id = {item.get("evidence_id"): item
                  for item in evidence.get("evidence_items") or []}
    for row in coverage:
        if not isinstance(row, dict):
            continue
        case_id = row.get("case_id")
        wanted = expected.get(case_id)
        if wanted is None:
            continue
        if (row.get("theme_id") != wanted.get("theme_id")
                or row.get("axis") != wanted.get("axis")
                or case_id != f"{row.get('theme_id')}::{row.get('axis')}"):
            errors.append(f"{case_id}:EVIDENCE_CASE_IDENTITY_MISMATCH")
        status = row.get("status")
        refs = row.get("evidence_refs")
        if not isinstance(refs, list) or len(refs) != len(set(refs)):
            errors.append(f"{case_id}:EVIDENCE_CASE_REFS_INVALID")
            refs = []
        if any(ref not in evidence_ids for ref in refs):
            errors.append(f"{case_id}:EVIDENCE_CASE_UNKNOWN_REF")
        used_refs.update(ref for ref in refs if ref in evidence_ids)
        for field in ("search_scope", "source_uris"):
            values = row.get(field)
            if (not isinstance(values, list) or not values
                    or any(not isinstance(value, str) or not value.strip() for value in values)):
                errors.append(f"{case_id}:{field.upper()}_REQUIRED")
        completed_at = row.get("search_completed_at")
        try:
            completed_ts = parse_ts(completed_at)
            if any(completed_ts < parse_ts(item_by_id[ref]["captured_at"])
                   for ref in refs if ref in item_by_id):
                errors.append(f"{case_id}:SEARCH_COMPLETED_BEFORE_EVIDENCE_CAPTURE")
        except ContractError:
            errors.append(f"{case_id}:SEARCH_COMPLETED_AT_INVALID")
        limitations = row.get("limitations")
        if (not isinstance(limitations, list)
                or any(not isinstance(value, str) or not value.strip()
                       for value in limitations)):
            errors.append(f"{case_id}:LIMITATIONS_INVALID")
            limitations = []
        if status == "EVIDENCE_FOUND":
            if not refs:
                errors.append(f"{case_id}:EVIDENCE_FOUND_REQUIRES_REF")
        elif status == "NO_NEW_DIRECT_FACT_FOUND":
            if refs:
                errors.append(f"{case_id}:NO_NEW_DIRECT_FACT_REFS_FORBIDDEN")
            if not limitations:
                errors.append(f"{case_id}:NO_NEW_DIRECT_FACT_LIMITATION_REQUIRED")
        else:
            errors.append(f"{case_id}:EVIDENCE_CASE_STATUS_INVALID")
    if evidence_ids - used_refs:
        errors.append("EVIDENCE_ITEMS_NOT_MAPPED_TO_CASE")
    return sorted(set(errors))


def _projection(themes: list[dict], sensing: dict) -> dict:
    watch, failed, invalidated, changes = [], [], [], []
    formal_ids = {row["theme_id"] for row in themes}
    card_names = {card.get("theme_id"): card.get("display_name")
                  for card in sensing.get("theme_cards", [])}
    for row in themes:
        tid = row["theme_id"]
        mode = row["state_provenance"]["mode"]
        if row["decision_validation_status"] == "FAILED":
            failed.append({"theme_id": tid, "source_run_id": row["state_provenance"].get("source_run_id"),
                           "status": "NO_FORMAL_STATE",
                           "reason": (row.get("review_failure") or {}).get(
                               "reason", "本次复核失败，未形成当前正式状态")})
        if (row.get("opportunity_stage") == "FORMING"
                and (((row.get("report_routing") or {}).get("opportunity") or {}).get("tier")
                     == "BRIEF")):
            watch.append({"theme_id": tid, "display_name": card_names.get(tid),
                          "label": "FORMING",
                          "reason": row.get("why_now"), "next_validation": row.get("next_validation")})
        if row.get("opportunity_stage") == "INVALID" or row.get("risk_level") == "EXIT":
            invalidated.append({"theme_id": tid, "opportunity_stage": row.get("opportunity_stage"),
                                "risk_level": row.get("risk_level"),
                                "reason": row.get("state_change_reason"),
                                "condition": row.get("opportunity_invalidation_or_reentry_condition")})
        for removed in row.get("removed_previous_candidates") or []:
            changes.append({"theme_id": tid, **removed})
    sensing_watch_theme_ids = set()
    sensing_watch_axis_count = {"OPPORTUNITY": 0, "RISK": 0}
    for decision in sensing.get("theme_decisions", []):
        tid = decision.get("theme_id")
        if tid in formal_ids:
            continue
        for axis in ("opportunity", "risk"):
            item = decision.get(axis) or {}
            if item.get("signal") == "WATCH":
                sensing_watch_theme_ids.add(tid)
                sensing_watch_axis_count[axis.upper()] += 1
        # Ordinary sensing WATCH rows remain fully auditable in sensing.json,
        # but they have no validated report route.  Listing every such row in
        # Markdown would turn a concise decision report into a second market
        # dump, so only explicitly BRIEF FORMING rows enter watch items.
    return {"sensing_watch_items": watch, "failed_review_items": failed,
            "unrouted_sensing_watch_summary": {
                "theme_count": len(sensing_watch_theme_ids),
                "axis_count": sensing_watch_axis_count,
                "storage": "SENSING_JSON_ONLY",
            },
            "alert_items": [],
            "invalidation_and_exit_items": invalidated,
            "candidate_change_items": changes}


def _daily_summary(themes: list[dict], projection: dict) -> str:
    current = [row for row in themes
               if (row.get("state_provenance") or {}).get("mode") == "CURRENT_VALIDATED"]
    opportunities = sum(row.get("opportunity_stage") in {"ACTIVE", "MATURE"}
                        for row in current)
    risks = sum(row.get("risk_level") in {"CAUTION", "HIGH", "EXIT"}
                for row in current)
    forming = sum(row.get("opportunity_stage") == "FORMING" for row in current)
    routed_forming = len(projection["sensing_watch_items"])
    sensing_watches = ((projection.get("unrouted_sensing_watch_summary") or {})
                       .get("theme_count", 0))
    failed = len(projection["failed_review_items"])
    return (f"已校验有效机会 {opportunities} 个、显著风险 {risks} 个；"
            f"形成中 {forming} 个（正文观察 {routed_forming} 个），"
            f"感知级观察 {sensing_watches} 个已留存但不逐项展示，"
            f"复核失败 {failed} 个。")


def _all_row_refs(row: dict):
    for field in ("evidence_refs", "counterevidence_refs", "risk_evidence_refs"):
        for ref in row.get(field) or []:
            yield field, ref
    for case in row.get("verification_cases") or []:
        if not isinstance(case, dict):
            continue
        for field in ("evidence_for_refs", "evidence_against_refs"):
            for ref in case.get(field) or []:
                yield f"verification_cases.{field}", ref


def validate_all_evidence_refs(themes: list[dict], allowed_refs: set[str]) -> list[str]:
    errors = []
    for row in themes:
        tid = row.get("theme_id")
        for field, ref in _all_row_refs(row):
            if not isinstance(ref, str) or ref not in allowed_refs:
                errors.append(f"{tid}:{field}:UNKNOWN_EVIDENCE_REF")
    return sorted(set(errors))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--theme-judgments", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--market-regime", required=True)
    parser.add_argument("--previous-ledger")
    args = parser.parse_args()
    if args.previous_ledger:
        raise SystemExit("LEDGER FAIL: PREVIOUS_RUN_CONTINUITY_NOT_IMPLEMENTED")
    run_dir = Path(args.run_dir)
    manifest = read_artifact_checked(run_dir / "manifest.json", "preopen_manifest")
    sensing = read_artifact_checked(run_dir / "sensing.json", "sensing")
    judgments, evidence = load_json(args.theme_judgments), load_json(args.evidence)
    validate_artifact_value(judgments, "theme_judgments", "theme judgments input")
    from model_io import (build_evidence_plan,
                          validate_market_regime_for_sensing)
    _, market_regime = validate_market_regime_for_sensing(
        run_dir / "sensing.json", Path(args.market_regime))
    if manifest.get("release_mode") == "OFFICIAL":
        raise SystemExit("LEDGER FAIL: OFFICIAL_EXECUTION_AND_PUBLICATION_NOT_IMPLEMENTED")
    # A manually requested INTERNAL_GATE run may produce an explicitly
    # unpublished Markdown preview before the scheduled window.  It never
    # becomes an OFFICIAL/SHADOW baseline and stays isolated in data/internal.
    # Late runs and every non-internal early run remain fail-closed.
    early_internal_preview = (
        manifest.get("release_mode") == "INTERNAL_GATE"
        and manifest.get("run_window_status") == "EARLY_DRAFT"
    )
    if manifest.get("run_window_status") != "ELIGIBLE" and not early_internal_preview:
        raise SystemExit(
            "LEDGER FAIL: RUN_WINDOW_NOT_ELIGIBLE_FOR_LEDGER_"
            + str(manifest.get("run_window_status") or "MISSING")
        )
    forbidden = ("daily_summary", "sensing_watch_items", "alert_items",
                 "routing_resolution", "market_context", "system_risk_sentinel",
                 "publication_limitations")
    if any(judgments.get(field) not in (None, "", [], {}) for field in forbidden):
        raise SystemExit("LEDGER FAIL: UNVALIDATED_LEDGER_PROJECTION_FIELD_FORBIDDEN")
    if manifest.get("publication_status") != "READY_FOR_THEME_REVIEW":
        raise SystemExit("LEDGER FAIL: SENSING_NOT_READY")
    if (manifest.get("sensing_ref") != sensing.get("artifact_hash")
            or manifest.get("release_mode") != sensing.get("release_mode")
            or manifest.get("decision_date") != sensing.get("decision_date")):
        raise SystemExit("LEDGER FAIL: PREOPEN_ARTIFACT_INTERLOCK_FAILED")
    sensing_errors = validate_sensing(sensing["theme_cards"], sensing)
    judgment_errors = validate_theme_judgments(sensing, judgments, market_regime)
    if sensing_errors or judgment_errors:
        print(json.dumps({"status": "FAIL", "errors": sensing_errors + judgment_errors},
                         ensure_ascii=False))
        raise SystemExit(2)
    if evidence.get("information_cutoff") != sensing["information_cutoff"]:
        raise SystemExit("LEDGER FAIL: EVIDENCE_CUTOFF_MISMATCH")
    evidence_ids = _validate_evidence(evidence, sensing["information_cutoff"])
    evidence_plan = build_evidence_plan(run_dir / "sensing.json", Path(args.market_regime))
    coverage_errors = validate_evidence_case_coverage(
        evidence, evidence_plan, evidence_ids, sensing["information_cutoff"])
    if coverage_errors:
        raise SystemExit("LEDGER FAIL: " + ";".join(coverage_errors))
    if (judgments.get("market_regime_ref") != market_regime.get("artifact_hash")
            or judgments.get("regime_input_hash") != market_regime.get("regime_input_hash")):
        raise SystemExit("LEDGER FAIL: THEME_JUDGMENT_MARKET_REGIME_INTERLOCK_FAILED")
    market_refs = {ref for card in sensing["theme_cards"] for ref in card["evidence_catalog"]}
    ref_errors = validate_all_evidence_refs(
        judgments["themes"], evidence_ids | market_refs | {market_regime["artifact_hash"]})
    if ref_errors:
        raise SystemExit("LEDGER FAIL: " + ";".join(ref_errors))
    display_names = {card.get("theme_id"): card.get("display_name")
                     for card in sensing.get("theme_cards", [])}
    themes = []
    for source_row in judgments["themes"]:
        row = copy.deepcopy(source_row)
        # Identity/display fields are deterministic registry data, not model
        # authority.  Ignore an omitted or hallucinated model display name.
        row["display_name"] = display_names.get(row["theme_id"])
        themes.append(row)
    themes.sort(key=lambda r: r["theme_id"])
    partial_global_limitations = {
        "TECHNICAL_BATCHED_SENSING_NO_CROSS_BATCH_RECONCILIATION",
        "RELATION_GROUP_RECONCILIATION_NOT_IMPLEMENTED",
    }
    has_isolated_latest_core = any(
        str(item).startswith("LATEST_CORE_BREADTH_ISOLATED:")
        for item in sensing.get("global_limitations") or [])
    completeness = (
        "PARTIAL"
        if (any(r["decision_validation_status"] == "FAILED" for r in themes)
            or has_isolated_latest_core
            or partial_global_limitations.intersection(sensing.get("global_limitations") or []))
        else "COMPLETE"
    )
    created = now_iso()
    projection = _projection(themes, sensing)
    ledger = {
        **artifact("ledger", "ledger", created),
        "run_id": manifest["run_id"], "session": "PREOPEN",
        "release_mode": manifest["release_mode"],
        "decision_date": manifest["decision_date"],
        "market_data_as_of": manifest["market_data_as_of"],
        "market_data_captured_at": manifest["market_data_captured_at"],
        "information_cutoff": sensing["information_cutoff"],
        "effective_from": None,
        "planned_effective_from": manifest["official_run_window"]["auction_start_at"],
        "run_started_at": manifest["run_started_at"],
        "validation_completed_at": created, "published_at": None,
        "run_window_status": manifest["run_window_status"],
        "publication_status": "VALIDATED_NOT_PUBLISHED",
        "publication_completeness": completeness,
        "publication_limitations": ([
            "INTERNAL_PREVIEW_ONLY",
            "PREVIOUS_RUN_CONTINUITY_NOT_IMPLEMENTED",
            "ALERT_LIFECYCLE_NOT_IMPLEMENTED",
            "EVIDENCE_BODY_SNAPSHOT_NOT_INTERLOCKED",
            "SYSTEM_RISK_SENTINEL_NOT_CONFIGURED",
        ] + (["EARLY_INTERNAL_PREVIEW_AS_OF_RUN_START"]
             if early_internal_preview else [])),
        "global_data_health": (
            "OK" if sensing["global_data_health"] == "OK" else "PARTIAL"
        ),
        "global_limitations": sensing["global_limitations"],
        "market_freeze_ref": manifest["market_freeze_ref"],
        "source_universe_version": manifest["source_universe_version"],
        "theme_registry_ref": sensing["theme_registry_ref"],
        "previous_official_run_id": None,
        "previous_decision_date": None,
        "daily_summary": _daily_summary(themes, projection),
        "routing_resolution": {
            "authority": "UNIFIED_THEME_OUTPUT",
            "routing_run_ref": content_hash({"themes": themes}),
            "fallback_reason": None},
        "market_context": {
            "context_status": "VALIDATED",
            "market_regime_ref": market_regime["artifact_hash"],
            "regime_input_hash": market_regime["regime_input_hash"],
            "market_posture": market_regime["market_regime"],
            "market_regime": market_regime["market_regime"],
            "risk_appetite": market_regime["risk_appetite"],
            "capital_migration": copy.deepcopy(market_regime["capital_migration"]),
            "duration": market_regime["duration"],
            "contradictions": copy.deepcopy(market_regime["contradictions"]),
            "confidence": copy.deepcopy(market_regime["confidence"]),
            "evidence_refs": copy.deepcopy(market_regime["evidence_refs"]),
            "limitations": copy.deepcopy(market_regime["limitations"]),
            "regime_review_nominations": copy.deepcopy(
                market_regime["regime_review_nominations"]),
        },
        "system_risk_sentinel": {
            "sentinel_status": "NOT_CONFIGURED",
            "intake_event_refs": [], "evidence_refs": [],
            "affected_theme_ids": [], "transmission_hypotheses": [],
            "forced_review_theme_ids": [], "creates_new_opportunity": False,
            "direct_state_change_allowed": False,
            "limitations": ["NO_CONFIGURED_UPSTREAM_EVENT_SOURCE"],
        },
        "report_projection": projection,
        "themes": themes,
        "evidence_ref": content_hash(evidence),
    }
    ledger["artifact_hash"] = content_hash(ledger)
    input_hashes = {"evidence": content_hash(evidence),
                    "market_regime": content_hash(market_regime),
                    "theme_judgments": content_hash(judgments)}
    immutable_outputs = (run_dir / "evidence.json", run_dir / "themes.json",
                         run_dir / "ledger.json", run_dir / "validation-manifest.json")
    if any(path.exists() for path in immutable_outputs):
        if not all(path.exists() for path in immutable_outputs):
            raise SystemExit("LEDGER FAIL: IMMUTABLE_OUTPUT_SET_INCOMPLETE")
        prior_validation = read_artifact_checked(
            run_dir / "validation-manifest.json", "ledger_validation_manifest")
        if (prior_validation.get("input_hashes") == input_hashes
                and prior_validation.get("sensing_ref") == manifest["sensing_ref"]):
            prior_evidence = read_artifact_checked(run_dir / "evidence.json", "evidence")
            prior_themes = read_artifact_checked(run_dir / "themes.json", "theme_judgments")
            prior_ledger = read_artifact_checked(run_dir / "ledger.json", "ledger")
            if (content_hash(prior_evidence) != content_hash(evidence)
                    or content_hash(prior_themes) != content_hash(judgments)
                    or prior_validation.get("ledger_ref") != prior_ledger.get("artifact_hash")):
                raise SystemExit("LEDGER FAIL: IMMUTABLE_OUTPUT_INTERLOCK_FAILED")
            print(f"LEDGER IDEMPOTENT｜{run_dir / 'ledger.json'}")
            return
        raise SystemExit("LEDGER FAIL: IMMUTABLE_OUTPUT_CONFLICT")
    validation_manifest = {
        **artifact("ledger_validation_manifest", "ledger-validation-manifest", created),
        "run_id": manifest["run_id"], "session": "PREOPEN",
        "release_mode": manifest["release_mode"], "decision_date": manifest["decision_date"],
        "market_data_as_of": manifest["market_data_as_of"],
        "market_freeze_ref": manifest["market_freeze_ref"],
        "run_started_at": manifest["run_started_at"], "validation_completed_at": created,
        "published_at": None, "official_run_window": manifest["official_run_window"],
        "run_window_status": manifest["run_window_status"],
        "publication_status": "VALIDATED_NOT_PUBLISHED",
        "publication_completeness": completeness, "publication_lock_id": None,
        "amends_run_id": None, "amendment_reason_code": None,
        "sensing_ref": manifest["sensing_ref"], "ledger_ref": ledger["artifact_hash"],
        "market_regime_ref": market_regime["artifact_hash"],
        "input_hashes": input_hashes,
    }
    validation_manifest["artifact_hash"] = content_hash(validation_manifest)
    lock_path = run_dir / ".ledger-write.lock"
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise SystemExit("LEDGER FAIL: CONCURRENT_OR_STALE_WRITE_LOCK")
    try:
        os.write(lock_fd, f"pid={os.getpid()}\n".encode())
        os.fsync(lock_fd)
        if any(path.exists() for path in immutable_outputs):
            raise SystemExit("LEDGER FAIL: IMMUTABLE_OUTPUT_RACE_CONFLICT")
        atomic_write_json(run_dir / "evidence.json", evidence)
        atomic_write_json(run_dir / "themes.json", judgments)
        atomic_write_json(run_dir / "ledger.json", ledger)
        atomic_write_json(run_dir / "validation-manifest.json", validation_manifest)
    finally:
        os.close(lock_fd)
        # Normal completion releases the single-writer gate.  A hard-killed
        # process intentionally leaves a stale lock and partial-set failure for
        # explicit operator repair rather than guessing which files are valid.
        lock_path.unlink(missing_ok=True)
    print(f"LEDGER PASS｜themes={len(themes)}｜{completeness}｜{run_dir / 'ledger.json'}")


if __name__ == "__main__":
    main()
