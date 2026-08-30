#!/usr/bin/env python3
"""Deterministic V3 validators. Never repairs or invents model decisions."""

from __future__ import annotations

import argparse
import json
import re

from v3_common import ContractError, load_json, read_artifact_checked

SIGNALS = {"NONE": 0, "WATCH": 1, "CANDIDATE": 2}
OPP_STRUCTURES = {"EMERGING_STRENGTH", "REACCELERATION", "REVERSAL_ATTEMPT"}
RISK_STRUCTURES = {"DETERIORATION", "NARROWING", "EXHAUSTION"}
PATTERNS = {"IMPULSE", "PERSISTENT"}
DERIVED = {("NONE", "NONE"): "NONE", ("WATCH", "NONE"): "WATCH",
           ("NONE", "WATCH"): "WATCH", ("WATCH", "WATCH"): "WATCH"}
CALIBRATION_ACTIONS = {"KEEP_CANDIDATE", "DOWNGRADE_WATCH", "DOWNGRADE_NONE"}
MARKET_ROLES = {
    "REGIME_LEADER", "RECEIVER", "DONOR", "INDEPENDENT",
    "COUNTER_REGIME", "CROWDED", "NEUTRAL",
}
OPPORTUNITY_DRIVERS = {"INDUSTRY", "REGIME", "DUAL", "EVENT", "PRICE_ONLY"}
RISK_TYPES = {
    "INDUSTRY", "MARKET_TREND", "STYLE_RETREAT", "CAPITAL_OUTFLOW",
    "CROWDING", "EVENT_POLICY", "MIXED",
}
REGIME_ALIGNMENTS = {"ALIGNED", "COUNTER", "MIXED", "NEUTRAL"}
CONFIDENCE_LEVELS = {"HIGH", "MEDIUM", "LOW"}


def _valid_sha(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None


def validate_market_regime(sensing: dict, regime: dict) -> list[str]:
    """Validate a model-authored market context against the frozen sensing set.

    This deliberately validates accounting and provenance, not whether the
    model's market interpretation is economically correct.
    """
    errors = []
    if regime.get("sensing_ref") != sensing.get("artifact_hash"):
        errors.append("MARKET_REGIME_SENSING_REF_MISMATCH")
    if not _valid_sha(regime.get("regime_input_hash")):
        errors.append("MARKET_REGIME_INPUT_HASH_INVALID")
    for field in ("market_regime", "risk_appetite", "duration"):
        if not isinstance(regime.get(field), str) or not regime[field].strip():
            errors.append(f"MARKET_REGIME_{field.upper()}_MISSING")
    confidence = regime.get("confidence")
    if not isinstance(confidence, dict):
        errors.append("MARKET_REGIME_CONFIDENCE_INVALID")
    else:
        if confidence.get("level") not in CONFIDENCE_LEVELS:
            errors.append("MARKET_REGIME_CONFIDENCE_LEVEL_INVALID")
        if not isinstance(confidence.get("reason"), str) or not confidence["reason"].strip():
            errors.append("MARKET_REGIME_CONFIDENCE_REASON_MISSING")
    theme_ids = {card.get("theme_id") for card in sensing.get("theme_cards", [])}
    migration = regime.get("capital_migration")
    if not isinstance(migration, dict):
        errors.append("MARKET_REGIME_CAPITAL_MIGRATION_INVALID")
    else:
        migration_sets = {}
        for key in ("from_theme_ids", "to_theme_ids"):
            values = migration.get(key)
            if (not isinstance(values, list) or len(values) != len(set(values))
                    or any(value not in theme_ids for value in values)):
                errors.append(f"MARKET_REGIME_{key.upper()}_INVALID")
                values = []
            migration_sets[key] = set(values)
        if migration_sets.get("from_theme_ids", set()).intersection(
                migration_sets.get("to_theme_ids", set())):
            errors.append("MARKET_REGIME_CAPITAL_MIGRATION_OVERLAP")
        for key in ("from_summary", "to_summary"):
            if not isinstance(migration.get(key), str) or not migration[key].strip():
                errors.append(f"MARKET_REGIME_{key.upper()}_MISSING")
    for field in ("contradictions", "limitations"):
        values = regime.get(field)
        if (not isinstance(values, list)
                or any(not isinstance(value, str) or not value.strip() for value in values)):
            errors.append(f"MARKET_REGIME_{field.upper()}_INVALID")
    allowed_market_refs = {
        ref for card in sensing.get("theme_cards", [])
        for ref in (card.get("evidence_catalog") or [])
    }
    refs = regime.get("evidence_refs")
    if (not isinstance(refs, list) or not refs
            or len(refs) != len(set(refs))
            or any(ref not in allowed_market_refs for ref in refs)):
        errors.append("MARKET_REGIME_EVIDENCE_REFS_INVALID")

    decisions = {row.get("theme_id"): row for row in sensing.get("theme_decisions", [])}
    nominations = regime.get("regime_review_nominations")
    if not isinstance(nominations, list):
        errors.append("REGIME_REVIEW_NOMINATIONS_NOT_ARRAY")
        nominations = []
    seen = set()
    for row in nominations:
        if not isinstance(row, dict):
            errors.append("REGIME_REVIEW_NOMINATION_INVALID")
            continue
        tid, axes = row.get("theme_id"), row.get("review_axes")
        if (tid not in decisions or not isinstance(axes, list) or not axes
                or len(axes) != len(set(axes))
                or any(axis not in {"OPPORTUNITY", "RISK"} for axis in axes)
                or not isinstance(row.get("rationale"), str) or not row["rationale"].strip()):
            errors.append("REGIME_REVIEW_NOMINATION_INVALID")
            continue
        for axis in axes:
            key = (tid, axis)
            if key in seen:
                errors.append("REGIME_REVIEW_NOMINATION_DUPLICATE_AXIS")
            seen.add(key)
            signal = ((decisions[tid].get(axis.lower()) or {}).get("signal"))
            if signal != "WATCH":
                errors.append(f"{tid}:{axis}:REGIME_NOMINATION_REQUIRES_WATCH")
    return sorted(set(errors))


def effective_review_plan(sensing: dict, regime: dict | None = None) -> tuple[dict[str, set[str]], list[str]]:
    """Return candidate axes plus valid regime nominations, with no upgrades."""
    errors = []
    plan = {}
    for row in sensing.get("review_plan") or []:
        if not isinstance(row, dict) or not isinstance(row.get("theme_id"), str):
            errors.append("REVIEW_PLAN_INVALID")
            continue
        tid, axes = row["theme_id"], row.get("review_axes")
        if (tid in plan or not isinstance(axes, list) or not axes
                or len(axes) != len(set(axes))
                or any(axis not in {"OPPORTUNITY", "RISK"} for axis in axes)):
            errors.append("REVIEW_PLAN_INVALID")
            continue
        plan[tid] = set(axes)
    if regime is None:
        return plan, sorted(set(errors))
    errors.extend(validate_market_regime(sensing, regime))
    for row in regime.get("regime_review_nominations") or []:
        if not isinstance(row, dict) or not isinstance(row.get("theme_id"), str):
            continue
        tid = row["theme_id"]
        axes = row.get("review_axes")
        if not isinstance(axes, list):
            continue
        plan.setdefault(tid, set()).update(
            axis for axis in axes if axis in {"OPPORTUNITY", "RISK"})
    return plan, sorted(set(errors))


def _validate_candidate_calibration(output: dict, decisions: list[dict]) -> list[str]:
    errors = []
    batching = output.get("technical_batching")
    applied = (isinstance(batching, dict)
               and batching.get("global_candidate_calibration")
               == "GLOBAL_DOWNGRADE_CALIBRATION_APPLIED")
    calibration = output.get("candidate_calibration")
    if not applied:
        return (["CANDIDATE_CALIBRATION_METADATA_WITHOUT_APPLIED_FLAG"]
                if calibration is not None else [])
    if not isinstance(calibration, dict) or set(calibration) != {
            "status", "calibration_input_hash", "calibration_output_hash",
            "sensing_output_hash", "prompt_ref", "case_count", "audit"}:
        return ["CANDIDATE_CALIBRATION_METADATA_INVALID"]
    if calibration.get("status") != "GLOBAL_DOWNGRADE_CALIBRATION_APPLIED":
        errors.append("CANDIDATE_CALIBRATION_STATUS_INVALID")
    for key in ("calibration_input_hash", "calibration_output_hash",
                "sensing_output_hash", "prompt_ref"):
        if not _valid_sha(calibration.get(key)):
            errors.append(f"CANDIDATE_CALIBRATION_{key.upper()}_INVALID")
    rows = calibration.get("audit")
    if not isinstance(rows, list):
        return errors + ["CANDIDATE_CALIBRATION_AUDIT_NOT_ARRAY"]
    if type(calibration.get("case_count")) is not int or calibration["case_count"] != len(rows):
        errors.append("CANDIDATE_CALIBRATION_AUDIT_ACCOUNTING_MISMATCH")
    final_by_id = {row.get("theme_id"): row for row in decisions if isinstance(row, dict)}
    seen = set()
    expected_keys = {
        "case_id", "case_input_hash", "theme_id", "axis", "action",
        "why_not_common_market_movement", "why_immediate_verification_matters",
        "initial_axis_decision", "calibrated_axis_decision",
    }
    for row in rows:
        if not isinstance(row, dict) or set(row) != expected_keys:
            errors.append("CANDIDATE_CALIBRATION_AUDIT_ROW_SHAPE_INVALID")
            continue
        tid, axis, action = row.get("theme_id"), row.get("axis"), row.get("action")
        key = (tid, axis)
        if key in seen:
            errors.append("CANDIDATE_CALIBRATION_AUDIT_DUPLICATE_AXIS")
        seen.add(key)
        if axis not in {"OPPORTUNITY", "RISK"} or row.get("case_id") != f"{tid}::{axis}":
            errors.append("CANDIDATE_CALIBRATION_AUDIT_CASE_ID_INVALID")
            continue
        if not _valid_sha(row.get("case_input_hash")):
            errors.append("CANDIDATE_CALIBRATION_CASE_INPUT_HASH_INVALID")
        if action not in CALIBRATION_ACTIONS:
            errors.append("CANDIDATE_CALIBRATION_ACTION_INVALID")
            continue
        for text_key in ("why_not_common_market_movement",
                         "why_immediate_verification_matters"):
            if not isinstance(row.get(text_key), str) or not row[text_key].strip():
                errors.append("CANDIDATE_CALIBRATION_EXPLANATION_MISSING")
        initial, calibrated = row.get("initial_axis_decision"), row.get("calibrated_axis_decision")
        if not isinstance(initial, dict) or initial.get("signal") != "CANDIDATE":
            errors.append("CANDIDATE_CALIBRATION_INITIAL_AXIS_NOT_CANDIDATE")
            continue
        expected_signal = {"KEEP_CANDIDATE": "CANDIDATE",
                           "DOWNGRADE_WATCH": "WATCH",
                           "DOWNGRADE_NONE": "NONE"}[action]
        if not isinstance(calibrated, dict) or calibrated.get("signal") != expected_signal:
            errors.append("CANDIDATE_CALIBRATION_ACTION_RESULT_MISMATCH")
            continue
        final = final_by_id.get(tid)
        final_axis = (final or {}).get(axis.lower())
        if final_axis != calibrated:
            errors.append("CANDIDATE_CALIBRATION_AUDIT_FINAL_MISMATCH")
    return errors


def derive(opportunity: str, risk: str) -> str:
    if opportunity == "CANDIDATE" and risk == "CANDIDATE":
        return "BOTH"
    if opportunity == "CANDIDATE":
        return "OPPORTUNITY"
    if risk == "CANDIDATE":
        return "RISK"
    return DERIVED[(opportunity, risk)]


def validate_sensing(cards: list[dict], output: dict) -> list[str]:
    errors = []
    batching = output.get("technical_batching")
    if batching is not None and (not isinstance(batching, dict)
            or batching.get("method") != "DETERMINISTIC_ALL_THEME_PARTITION"
            or not isinstance(batching.get("batch_count"), int)
            or batching.get("batch_count", 0) < 1
            or not isinstance(batching.get("batch_size"), int)
            or batching.get("batch_size", 0) < 1
            or batching.get("cross_batch_reconciliation") != "NOT_IMPLEMENTED"):
        errors.append("TECHNICAL_BATCHING_METADATA_INVALID")
    if output.get("correction_attempts", 0) not in (0, 1):
        errors.append("CORRECTION_ATTEMPTS_EXCEEDED")
    card_by_id = {c.get("theme_id"): c for c in cards}
    decisions = output.get("theme_decisions")
    if not isinstance(decisions, list):
        return ["THEME_DECISIONS_NOT_ARRAY"]
    ids = [d.get("theme_id") for d in decisions]
    if len(ids) != len(set(ids)):
        errors.append("DUPLICATE_THEME_DECISION")
    if set(ids) != set(card_by_id):
        errors.append("THEME_ACCOUNTING_MISMATCH")
    errors.extend(_validate_candidate_calibration(output, decisions))
    reconciliation = output.get("reconciliation", [])
    if not isinstance(reconciliation, list):
        errors.append("RECONCILIATION_NOT_ARRAY")
    elif reconciliation:
        # Representative/support reconciliation needs a separate deterministic
        # relationship validator.  Until it exists, accepting free-form model
        # rows would create unaudited theme suppression metadata.
        errors.append("RECONCILIATION_VALIDATION_NOT_IMPLEMENTED")
    for item in decisions:
        tid = item.get("theme_id")
        card = card_by_id.get(tid)
        if not card:
            continue
        allowed_refs = set(card.get("evidence_catalog", []))
        for axis, structures, cap_key in (
                ("opportunity", OPP_STRUCTURES, "max_sensing_opportunity_signal"),
                ("risk", RISK_STRUCTURES, "max_sensing_risk_signal")):
            value = item.get(axis)
            if not isinstance(value, dict) or value.get("signal") not in SIGNALS:
                errors.append(f"{tid}:{axis}:SIGNAL_INVALID")
                continue
            signal = value["signal"]
            cap = card.get("permission_caps", {}).get(cap_key, "NONE")
            if cap not in SIGNALS or SIGNALS[signal] > SIGNALS[cap]:
                errors.append(f"{tid}:{axis}:PERMISSION_CAP_EXCEEDED")
            refs = value.get("evidence_refs") or []
            if any(ref not in allowed_refs for ref in refs):
                errors.append(f"{tid}:{axis}:UNKNOWN_EVIDENCE_REF")
            if signal == "NONE":
                if value.get("structure_type") is not None or value.get("path_pattern") is not None:
                    errors.append(f"{tid}:{axis}:NONE_HAS_STRUCTURE")
            else:
                if value.get("structure_type") not in structures:
                    errors.append(f"{tid}:{axis}:STRUCTURE_INVALID")
                if value.get("path_pattern") not in PATTERNS:
                    errors.append(f"{tid}:{axis}:PATH_PATTERN_INVALID")
                if not refs or not value.get("reason"):
                    errors.append(f"{tid}:{axis}:EVIDENCE_OR_REASON_MISSING")
                if value.get("path_pattern") == "IMPULSE" and signal == "CANDIDATE":
                    categories = {ref.split(":")[-1] for ref in refs}
                    if not {"price", "breadth", "attention"}.issubset(categories):
                        errors.append(f"{tid}:{axis}:IMPULSE_THREE_OBSERVATIONS_REQUIRED")
                    price = card.get("price") or {}
                    breadth = card.get("breadth") or {}
                    attention = card.get("attention") or {}
                    if ((price.get("returns") or {}).get("1D") is None
                            or breadth.get("up_ratio_today") is None
                            or not any(v is not None for v in
                                       (attention.get("activity_ratio_path_5d") or [])[-1:])):
                        errors.append(f"{tid}:{axis}:IMPULSE_REFERENCED_FACT_MISSING")
                if value.get("path_pattern") == "PERSISTENT" and signal == "CANDIDATE":
                    price = card.get("price") or {}
                    multi_day = any((price.get("returns") or {}).get(key) is not None
                                    for key in ("3D", "5D", "10D", "20D"))
                    if not multi_day:
                        errors.append(f"{tid}:{axis}:PERSISTENT_MULTI_DAY_FACT_REQUIRED")
                    categories = {ref.split(":")[-1] for ref in refs}
                    if "price" not in categories or not ({"breadth", "attention"} & categories):
                        errors.append(f"{tid}:{axis}:PERSISTENT_MULTI_DIMENSION_REQUIRED")
                    breadth_valid = any(v is not None for v in
                                        ((card.get("breadth") or {}).get("balance_path_5d") or []))
                    attention_valid = any(v is not None for v in
                                          ((card.get("attention") or {}).get(
                                              "activity_ratio_path_5d") or []))
                    if (("breadth" in categories and not breadth_valid)
                            or ("attention" in categories and not attention_valid)):
                        errors.append(f"{tid}:{axis}:PERSISTENT_REFERENCED_FACT_MISSING")
        opp = (item.get("opportunity") or {}).get("signal")
        risk = (item.get("risk") or {}).get("signal")
        if opp in SIGNALS and risk in SIGNALS and item.get("derived_decision") != derive(opp, risk):
            errors.append(f"{tid}:DERIVED_DECISION_INVALID")
    return sorted(set(errors))


def validate_theme_judgments(sensing: dict, judgments: dict,
                             market_regime: dict | None = None) -> list[str]:
    """Validate minimal formal judgments; never infer their investment meaning."""
    errors = []
    if judgments.get("correction_attempts", 0) not in (0, 1):
        errors.append("CORRECTION_ATTEMPTS_EXCEEDED")
    if market_regime is not None:
        if judgments.get("market_regime_ref") != market_regime.get("artifact_hash"):
            errors.append("THEME_JUDGMENT_MARKET_REGIME_REF_MISMATCH")
        if judgments.get("regime_input_hash") != market_regime.get("regime_input_hash"):
            errors.append("THEME_JUDGMENT_REGIME_INPUT_HASH_MISMATCH")
    allowed_themes = {d["theme_id"]: d for d in sensing.get("theme_decisions", [])}
    cards = {card.get("theme_id"): card for card in sensing.get("theme_cards", [])}
    plan_rows = sensing.get("review_plan")
    if not isinstance(plan_rows, list):
        return ["DETERMINISTIC_REVIEW_PLAN_MISSING"]
    review_plan, plan_errors = effective_review_plan(sensing, market_regime)
    errors.extend(plan_errors)
    rows = judgments.get("themes")
    if not isinstance(rows, list):
        return ["FORMAL_THEMES_NOT_ARRAY"]
    declared_review_ids = judgments.get("review_theme_ids")
    if not isinstance(declared_review_ids, list) or len(declared_review_ids) != len(
            set(declared_review_ids)):
        errors.append("REVIEW_THEME_IDS_INVALID")
        declared_review_ids = []
    review_ids = set(declared_review_ids)
    row_ids = [r.get("theme_id") for r in rows if isinstance(r, dict)]
    if len(row_ids) != len(rows) or len(row_ids) != len(set(row_ids)):
        errors.append("DUPLICATE_OR_INVALID_FORMAL_THEME_ROW")
    if review_ids != set(review_plan):
        errors.append("DETERMINISTIC_REVIEW_PLAN_MISMATCH")
    if set(row_ids) != review_ids:
        errors.append("FORMAL_REVIEW_ACCOUNTING_MISMATCH")
    for row in rows:
        if not isinstance(row, dict):
            continue
        tid = row.get("theme_id")
        if tid not in allowed_themes or tid not in cards:
            errors.append(f"{tid}:UNKNOWN_THEME")
            continue
        status = row.get("decision_validation_status")
        mode = (row.get("state_provenance") or {}).get("mode")
        if status not in {"VALID", "CORRECTED", "FAILED"}:
            errors.append(f"{tid}:VALIDATION_STATUS_INVALID")
        if status == "FAILED" and mode not in {"CARRIED_FORWARD", "NO_FORMAL_STATE"}:
            errors.append(f"{tid}:FAILED_STATE_PROVENANCE_INVALID")
        if status in {"VALID", "CORRECTED"} and mode != "CURRENT_VALIDATED":
            errors.append(f"{tid}:CURRENT_STATE_PROVENANCE_REQUIRED")
        if mode == "CARRIED_FORWARD":
            errors.append(f"{tid}:CARRIED_FORWARD_CONTINUITY_NOT_IMPLEMENTED")
            if row.get("change_events") or row.get("stock_candidates"):
                errors.append(f"{tid}:CARRIED_FORWARD_MUTATION_FORBIDDEN")
        # The stock contract is not enabled in this implementation slice.  No
        # model-authored stock row or removal event may hitchhike through a
        # valid theme judgment into the ledger/report projection.
        if row.get("stock_candidates") not in (None, []):
            errors.append(f"{tid}:STOCK_MODULE_NOT_ENABLED")
        if row.get("removed_previous_candidates") not in (None, []):
            errors.append(f"{tid}:STOCK_MODULE_NOT_ENABLED")
        if mode == "NO_FORMAL_STATE" and (row.get("opportunity_stage") is not None
                                           or row.get("risk_level") is not None):
            errors.append(f"{tid}:NO_FORMAL_STATE_HAS_FORMAL_AXES")
        if row.get("opportunity_stage") not in {None, "FORMING", "ACTIVE", "MATURE", "INVALID"}:
            errors.append(f"{tid}:OPPORTUNITY_STAGE_INVALID")
        if row.get("risk_level") not in {None, "LOW", "CAUTION", "HIGH", "EXIT"}:
            errors.append(f"{tid}:RISK_LEVEL_INVALID")
        if market_regime is not None:
            if row.get("market_role") not in MARKET_ROLES:
                errors.append(f"{tid}:MARKET_ROLE_INVALID")
            driver = row.get("opportunity_driver")
            if driver is not None and driver not in OPPORTUNITY_DRIVERS:
                errors.append(f"{tid}:OPPORTUNITY_DRIVER_INVALID")
            risk_types = row.get("risk_types")
            if (not isinstance(risk_types, list)
                    or len(risk_types) != len(set(risk_types))
                    or any(value not in RISK_TYPES for value in risk_types)):
                errors.append(f"{tid}:RISK_TYPES_INVALID")
                risk_types = []
            if row.get("regime_alignment") not in REGIME_ALIGNMENTS:
                errors.append(f"{tid}:REGIME_ALIGNMENT_INVALID")
            if (not isinstance(row.get("regime_interpretation"), str)
                    or not row["regime_interpretation"].strip()):
                errors.append(f"{tid}:REGIME_INTERPRETATION_REQUIRED")
            if (row.get("opportunity_stage") in {"FORMING", "ACTIVE", "MATURE"}
                    and driver is None):
                errors.append(f"{tid}:OPPORTUNITY_DRIVER_REQUIRED")
            if driver == "PRICE_ONLY" and row.get("opportunity_stage") in {
                    "FORMING", "ACTIVE", "MATURE"}:
                errors.append(f"{tid}:PRICE_ONLY_FORMAL_OPPORTUNITY_FORBIDDEN")
            if (row.get("risk_level") in {"CAUTION", "HIGH", "EXIT"}
                    and not risk_types):
                errors.append(f"{tid}:RISK_TYPES_REQUIRED")
        card = cards[tid]
        caps = card.get("permission_caps") or {}
        if mode == "CURRENT_VALIDATED":
            if caps.get("formal_theme_decision_allowed") is not True:
                errors.append(f"{tid}:FORMAL_THEME_DECISION_NOT_ALLOWED")
            stage = row.get("opportunity_stage")
            risk_level = row.get("risk_level")
            if stage is not None and stage not in set(caps.get("allowed_opportunity_stages") or []):
                errors.append(f"{tid}:OPPORTUNITY_STAGE_PERMISSION_EXCEEDED")
            if risk_level is not None and risk_level not in set(caps.get("allowed_risk_levels") or []):
                errors.append(f"{tid}:RISK_LEVEL_PERMISSION_EXCEEDED")
        refs = row.get("evidence_refs") or []
        for ref_field in ("evidence_refs", "counterevidence_refs", "risk_evidence_refs"):
            if not isinstance(row.get(ref_field, []), list):
                errors.append(f"{tid}:{ref_field.upper()}_NOT_ARRAY")
        risk_refs = row.get("risk_evidence_refs") or []
        if mode == "CURRENT_VALIDATED" and not (refs or risk_refs):
            errors.append(f"{tid}:FORMAL_EVIDENCE_REQUIRED")
        if row.get("opportunity_stage") is not None and not row.get("next_validation"):
            errors.append(f"{tid}:NEXT_VALIDATION_REQUIRED")
        if row.get("opportunity_stage") in {"FORMING", "ACTIVE", "MATURE"}:
            if (not row.get("pricing_judgment") or not row.get("counterevidence_assessment")
                    or not row.get("alternative_explanations")
                    or not row.get("opportunity_invalidation_or_reentry_condition")):
                errors.append(f"{tid}:OPPORTUNITY_CASE_DISCIPLINE_INCOMPLETE")
        if row.get("risk_level") in {"CAUTION", "HIGH", "EXIT"} and not row.get("risk_relief_condition"):
            errors.append(f"{tid}:RISK_RELIEF_REQUIRED")
        cases = row.get("verification_cases") or []
        if not isinstance(cases, list):
            errors.append(f"{tid}:VERIFICATION_CASES_NOT_ARRAY")
            cases = []
        case_axes = [case.get("axis") for case in cases if isinstance(case, dict)]
        if len(case_axes) != len(cases) or len(case_axes) != len(set(case_axes)):
            errors.append(f"{tid}:DUPLICATE_OR_INVALID_REVIEW_AXIS_CASE")
        if mode == "CURRENT_VALIDATED" and set(case_axes) != review_plan.get(tid, set()):
            errors.append(f"{tid}:REVIEW_AXIS_CASE_ACCOUNTING_MISMATCH")
        case_by_axis = {}
        for case in cases:
            if not isinstance(case, dict) or case.get("axis") not in {"OPPORTUNITY", "RISK"}:
                continue
            axis = case["axis"]
            case_by_axis[axis] = case
            if case.get("conclusion") not in {"VERIFIED", "PARTIAL", "UNVERIFIED", "CONTRADICTED"}:
                errors.append(f"{tid}:{axis}:CASE_CONCLUSION_INVALID")
            for field in ("evidence_for_refs", "evidence_against_refs", "limitations"):
                if not isinstance(case.get(field), list):
                    errors.append(f"{tid}:{axis}:{field.upper()}_NOT_ARRAY")
            supporting = case.get("evidence_for_refs") or []
            opposing = case.get("evidence_against_refs") or []
            limitations = case.get("limitations") or []
            if case.get("conclusion") in {"VERIFIED", "PARTIAL"} and not supporting:
                errors.append(f"{tid}:{axis}:SUPPORTED_CASE_REQUIRES_EVIDENCE")
            if case.get("conclusion") == "CONTRADICTED" and not opposing:
                errors.append(f"{tid}:{axis}:CONTRADICTED_CASE_REQUIRES_EVIDENCE")
            if case.get("conclusion") == "UNVERIFIED" and not limitations:
                errors.append(f"{tid}:{axis}:UNVERIFIED_CASE_REQUIRES_LIMITATION")
            if axis == "OPPORTUNITY" and (not case.get("alternative_explanation")
                                           or not case.get("pricing_assessment")
                                           or not case.get("next_validation")):
                errors.append(f"{tid}:OPPORTUNITY_CASE_DISCIPLINE_INCOMPLETE")
        opp_case = case_by_axis.get("OPPORTUNITY")
        risk_case = case_by_axis.get("RISK")
        if mode == "CURRENT_VALIDATED" and row.get("opportunity_stage") in {
                "FORMING", "ACTIVE", "MATURE"}:
            conclusion = (opp_case or {}).get("conclusion")
            if conclusion == "PARTIAL" and row.get("opportunity_stage") != "FORMING":
                errors.append(f"{tid}:PARTIAL_OPPORTUNITY_EXCEEDS_FORMING")
            elif conclusion != "VERIFIED" and not (
                    conclusion == "PARTIAL" and row.get("opportunity_stage") == "FORMING"):
                errors.append(f"{tid}:OPPORTUNITY_CASE_PERMISSION_EXCEEDED")
        if mode == "CURRENT_VALIDATED" and row.get("opportunity_stage") == "INVALID":
            if (opp_case or {}).get("conclusion") not in {"VERIFIED", "CONTRADICTED"}:
                errors.append(f"{tid}:INVALID_WITHOUT_DECISIVE_CASE")
        if mode == "CURRENT_VALIDATED" and row.get("risk_level") in {"HIGH", "EXIT"}:
            if (risk_case or {}).get("conclusion") != "VERIFIED":
                errors.append(f"{tid}:HIGH_EXIT_REQUIRES_VERIFIED_RISK_CASE")
        if mode == "CURRENT_VALIDATED" and row.get("risk_level") == "CAUTION":
            if (risk_case or {}).get("conclusion") not in {"VERIFIED", "PARTIAL"}:
                errors.append(f"{tid}:CAUTION_REQUIRES_SUPPORTED_RISK_CASE")
        routing = row.get("report_routing")
        if mode == "CURRENT_VALIDATED":
            if not isinstance(routing, dict):
                errors.append(f"{tid}:REPORT_ROUTING_REQUIRED")
            else:
                for axis in ("opportunity", "risk"):
                    route = routing.get(axis)
                    if not isinstance(route, dict) or route.get("tier") not in {
                            "FOCUS", "BRIEF", "LEDGER_ONLY"}:
                        errors.append(f"{tid}:{axis}:REPORT_ROUTING_INVALID")
                risk_tier = ((routing.get("risk") or {}).get("tier"))
                if row.get("risk_level") in {"HIGH", "EXIT"} and risk_tier == "LEDGER_ONLY":
                    errors.append(f"{tid}:HIGH_EXIT_CANNOT_BE_LEDGER_ONLY")
                opp_tier = ((routing.get("opportunity") or {}).get("tier"))
                if row.get("opportunity_stage") == "FORMING" and opp_tier == "FOCUS":
                    errors.append(f"{tid}:FORMING_CANNOT_BE_FOCUS")
    return sorted(set(errors))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", required=True, choices=("sensing", "themes"))
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--market-regime")
    args = parser.parse_args()
    source, output = load_json(args.input), load_json(args.output)
    if args.kind == "sensing":
        errors = validate_sensing(source.get("theme_cards", []), output)
    else:
        if not args.market_regime:
            errors = ["MARKET_REGIME_REQUIRED"]
        else:
            regime = read_artifact_checked(args.market_regime, "market_regime")
            errors = validate_theme_judgments(source, output, regime)
    print(json.dumps({"status": "PASS" if not errors else "FAIL", "errors": errors},
                     ensure_ascii=False, indent=2))
    raise SystemExit(0 if not errors else 2)


if __name__ == "__main__":
    main()
