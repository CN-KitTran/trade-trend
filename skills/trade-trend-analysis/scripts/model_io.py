#!/usr/bin/env python3
"""Compact deterministic model I/O for the INTERNAL_GATE orchestrator.

This module never decides a market direction.  It only removes verbose audit
material from an already frozen sensing artifact and stamps model-authored bare
JSON with the repository's artifact metadata/content hash.
"""

from __future__ import annotations

import argparse
import copy
import json
import statistics
from pathlib import Path

from v3_common import (ContractError, artifact, atomic_write_json, content_hash,
                       file_hash, load_json, read_artifact_checked)
from validate_outputs import (derive, effective_review_plan, validate_market_regime,
                              validate_sensing)


CALIBRATION_ACTIONS = {"KEEP_CANDIDATE", "DOWNGRADE_WATCH", "DOWNGRADE_NONE"}
CALIBRATION_ROOT_KEYS = {
    "calibration_input_hash", "sensing_input_hash", "sensing_artifact_hash",
    "sensing_output_hash", "prompt_ref", "correction_attempts", "cases",
}
CALIBRATION_CASE_KEYS = {
    "case_id", "case_input_hash", "theme_id", "axis", "action",
    "why_not_common_market_movement", "why_immediate_verification_matters",
}


def compact_card(card: dict) -> dict:
    """Keep every theme and every decision-relevant fact, drop audit verbosity."""
    price = copy.deepcopy(card.get("price") or {})
    breadth = copy.deepcopy(card.get("breadth") or {})
    attention = copy.deepcopy(card.get("attention") or {})
    for value in (breadth, attention):
        value.pop("audit", None)
    caps = card.get("permission_caps") or {}
    return {
        "theme_id": card.get("theme_id"),
        "display_name": card.get("display_name"),
        "universe_layer": card.get("universe_layer"),
        "price": price,
        "breadth": breadth,
        "attention": attention,
        "prior_state": card.get("prior_state"),
        "data_health": card.get("data_health"),
        "permission_caps": {
            "max_sensing_opportunity_signal": caps.get(
                "max_sensing_opportunity_signal"),
            "max_sensing_risk_signal": caps.get("max_sensing_risk_signal"),
            "formal_theme_decision_allowed": caps.get(
                "formal_theme_decision_allowed"),
            "allowed_opportunity_stages": caps.get("allowed_opportunity_stages") or [],
            "allowed_risk_levels": caps.get("allowed_risk_levels") or [],
            "reason_codes": caps.get("reason_codes") or [],
        },
        "evidence_catalog": card.get("evidence_catalog") or [],
    }


def _rounded(value):
    return round(value, 6) if isinstance(value, float) else value


def compact_regime_theme(card: dict, decision: dict) -> dict:
    """Keep all themes but only the facts needed for cross-market context.

    The formal review packet retains the full compact card.  Repeating full
    permission, audit, prose and evidence arrays for 651 themes made the
    market-state call several hundred thousand tokens and defeated the purpose
    of a light context layer.
    """
    price, breadth, attention = (card.get("price") or {}, card.get("breadth") or {},
                                 card.get("attention") or {})
    percentiles = {
        window: _rounded((row or {}).get("value"))
        for window, row in (price.get("peer_percentiles") or {}).items()
    }
    axis = {}
    for name in ("opportunity", "risk"):
        source = decision.get(name) or {}
        axis[name] = {key: source.get(key) for key in (
            "signal", "structure_type", "path_pattern")}
    return {
        "theme_id": card.get("theme_id"),
        "display_name": card.get("display_name"),
        "universe_layer": card.get("universe_layer"),
        "returns": {key: _rounded(value) for key, value in
                    (price.get("returns") or {}).items()},
        "excess_returns": {key: _rounded(value) for key, value in
                           (price.get("excess_returns") or {}).items()},
        "peer_percentiles": percentiles,
        "last_day_dominance": {key: _rounded(value) for key, value in
                               (price.get("last_day_dominance") or {}).items()},
        "breadth": {
            "up_ratio_today": _rounded(breadth.get("up_ratio_today")),
            "down_ratio_today": _rounded(breadth.get("down_ratio_today")),
            "balance_path_5d": [_rounded(value) for value in
                                (breadth.get("balance_path_5d") or [])],
            "balance_average_20d": _rounded(breadth.get("balance_average_20d")),
        },
        "attention": {
            "activity_ratio_path_5d": [_rounded(value) for value in
                                       (attention.get("activity_ratio_path_5d") or [])],
            "amount_percentile_60d": _rounded(attention.get("amount_percentile_60d")),
        },
        "sensing": {**axis, "derived_decision": decision.get("derived_decision")},
        "available_market_evidence": ["price", "breadth", "attention"],
    }


def _market_structure_summary(themes: list[dict]) -> dict:
    windows = ("1D", "3D", "5D", "10D", "20D")
    summary = {"theme_count": len(themes), "windows": {}}
    for window in windows:
        absolute = [row["returns"].get(window) for row in themes]
        excess = [row["excess_returns"].get(window) for row in themes]
        absolute = [value for value in absolute if isinstance(value, (int, float))]
        excess = [value for value in excess if isinstance(value, (int, float))]
        benchmark_candidates = [a - e for a, e in zip(
            [row["returns"].get(window) for row in themes],
            [row["excess_returns"].get(window) for row in themes])
            if isinstance(a, (int, float)) and isinstance(e, (int, float))]
        summary["windows"][window] = {
            "benchmark_return": _rounded(statistics.median(benchmark_candidates))
            if benchmark_candidates else None,
            "theme_median_return": _rounded(statistics.median(absolute))
            if absolute else None,
            "theme_median_excess": _rounded(statistics.median(excess))
            if excess else None,
            "positive_theme_count": sum(value > 0 for value in absolute),
            "positive_excess_theme_count": sum(value > 0 for value in excess),
            "valid_theme_count": len(absolute),
        }
    summary["sensing_distribution"] = {
        axis: {signal: sum((row["sensing"].get(axis) or {}).get("signal") == signal
                           for row in themes)
               for signal in ("NONE", "WATCH", "CANDIDATE")}
        for axis in ("opportunity", "risk")
    }
    return summary


REGIME_THEME_COLUMNS = [
    "theme_id", "display_name", "universe_layer",
    "return_1d", "return_3d", "return_5d", "return_10d", "return_20d",
    "excess_1d", "excess_3d", "excess_5d", "excess_10d", "excess_20d",
    "peer_percentile_5d", "peer_percentile_20d", "last_day_dominance_5d",
    "up_ratio_today", "breadth_balance_5d_first", "breadth_balance_5d_last",
    "breadth_balance_average_20d", "attention_ratio_5d_first",
    "attention_ratio_5d_last", "attention_percentile_60d",
    "opportunity_signal", "opportunity_structure", "opportunity_pattern",
    "risk_signal", "risk_structure", "risk_pattern", "derived_decision",
]


def _regime_theme_row(theme: dict) -> list:
    returns, excess = theme["returns"], theme["excess_returns"]
    peer, dominance = theme["peer_percentiles"], theme["last_day_dominance"]
    breadth, attention = theme["breadth"], theme["attention"]
    breadth_path = breadth.get("balance_path_5d") or []
    attention_path = attention.get("activity_ratio_path_5d") or []
    opportunity = theme["sensing"].get("opportunity") or {}
    risk = theme["sensing"].get("risk") or {}
    return [
        theme["theme_id"], theme["display_name"], theme["universe_layer"],
        returns.get("1D"), returns.get("3D"), returns.get("5D"),
        returns.get("10D"), returns.get("20D"),
        excess.get("1D"), excess.get("3D"), excess.get("5D"),
        excess.get("10D"), excess.get("20D"), peer.get("5D"), peer.get("20D"),
        dominance.get("5D"), breadth.get("up_ratio_today"),
        breadth_path[0] if breadth_path else None,
        breadth_path[-1] if breadth_path else None,
        breadth.get("balance_average_20d"),
        attention_path[0] if attention_path else None,
        attention_path[-1] if attention_path else None,
        attention.get("amount_percentile_60d"),
        opportunity.get("signal"), opportunity.get("structure_type"),
        opportunity.get("path_pattern"), risk.get("signal"),
        risk.get("structure_type"), risk.get("path_pattern"),
        theme["sensing"].get("derived_decision"),
    ]


def build_sensing_packet(sensing_path: Path) -> dict:
    sensing = read_artifact_checked(sensing_path, "sensing")
    cards = sensing.get("theme_cards")
    if not isinstance(cards, list):
        raise ContractError(["THEME_CARDS_NOT_ARRAY"])
    compact = [compact_card(card) for card in cards]
    if [row.get("theme_id") for row in compact] != [
            row.get("theme_id") for row in cards]:
        raise ContractError(["COMPACT_PACKET_THEME_ACCOUNTING_FAILED"])
    prompt_path = Path(__file__).resolve().parent.parent / "prompts" / "sensing-v1.md"
    return {
        "packet_kind": "sensing_model_input",
        "decision_date": sensing.get("decision_date"),
        "market_data_as_of": sensing.get("market_data_as_of"),
        "sensing_input_hash": sensing.get("sensing_input_hash"),
        "prompt_ref": file_hash(prompt_path),
        "theme_count": len(compact),
        "themes": compact,
        "output_contract": {
            "required": ["sensing_input_hash", "correction_attempts",
                         "theme_decisions", "reconciliation"],
            "one_decision_per_theme": True,
            "reconciliation": [],
        },
    }


def build_market_regime_packet(sensing_path: Path) -> dict:
    """Build the one-shot full-market context input after sensing is complete."""
    sensing = read_artifact_checked(sensing_path, "sensing")
    if (sensing.get("validation") or {}).get("status") != "PASS":
        raise ContractError(["SENSING_NOT_VALIDATED"])
    cards = sensing.get("theme_cards") or []
    decisions = {row.get("theme_id"): row for row in sensing.get("theme_decisions") or []}
    if set(decisions) != {card.get("theme_id") for card in cards}:
        raise ContractError(["THEME_ACCOUNTING_MISMATCH"])
    themes = [compact_regime_theme(card, decisions[card["theme_id"]])
              for card in cards]
    prompt_path = Path(__file__).resolve().parent.parent / "prompts" / "market-regime-v1.md"
    packet = {
        "packet_kind": "market_regime_model_input",
        "decision_date": sensing.get("decision_date"),
        "market_data_as_of": sensing.get("market_data_as_of"),
        "information_cutoff": sensing.get("information_cutoff"),
        "sensing_ref": sensing.get("artifact_hash"),
        "prompt_ref": file_hash(prompt_path),
        "theme_count": len(themes),
        "market_structure_summary": _market_structure_summary(themes),
        "theme_columns": REGIME_THEME_COLUMNS,
        "theme_rows": [_regime_theme_row(theme) for theme in themes],
        "market_evidence_ref_rule": "{theme_id}:{price|breadth|attention}",
        "output_contract": {
            "sensing_ref": sensing.get("artifact_hash"),
            "correction_attempts": "0 or 1",
            "regime_review_nominations": (
                "WATCH axes only; no NONE upgrades; do not remove CANDIDATE axes"),
        },
    }
    packet["regime_input_hash"] = content_hash(packet)
    packet["output_contract"]["regime_input_hash"] = packet["regime_input_hash"]
    return packet


def validate_market_regime_for_sensing(sensing_path: Path,
                                       regime_path: Path) -> tuple[dict, dict]:
    sensing = read_artifact_checked(sensing_path, "sensing")
    regime = read_artifact_checked(regime_path, "market_regime")
    packet = build_market_regime_packet(sensing_path)
    errors = validate_market_regime(sensing, regime)
    if regime.get("regime_input_hash") != packet.get("regime_input_hash"):
        errors.append("MARKET_REGIME_INPUT_HASH_MISMATCH")
    if errors:
        raise ContractError(errors)
    return sensing, regime


def build_evidence_plan(sensing_path: Path, regime_path: Path) -> dict:
    """Emit the exact post-regime cases to verify; collectors must not guess."""
    sensing, regime = validate_market_regime_for_sensing(sensing_path, regime_path)
    plan, errors = effective_review_plan(sensing, regime)
    if errors:
        raise ContractError(errors)
    cards = {card.get("theme_id"): card for card in sensing.get("theme_cards", [])}
    decisions = {row.get("theme_id"): row for row in sensing.get("theme_decisions", [])}
    candidate_axes = {
        (row.get("theme_id"), axis)
        for row in sensing.get("review_plan") or []
        for axis in (row.get("review_axes") or [])
    }
    nomination_reason = {
        (row["theme_id"], axis): row["rationale"]
        for row in regime.get("regime_review_nominations") or []
        for axis in row["review_axes"]
    }
    cases = []
    for tid in [card.get("theme_id") for card in sensing.get("theme_cards", [])]:
        for axis in ("OPPORTUNITY", "RISK"):
            if axis not in plan.get(tid, set()):
                continue
            case = {
                "case_id": f"{tid}::{axis}",
                "theme_id": tid,
                "display_name": cards[tid].get("display_name"),
                "axis": axis,
                "origin": ("SENSING_CANDIDATE" if (tid, axis) in candidate_axes
                           else "REGIME_WATCH_NOMINATION"),
                "nomination_rationale": nomination_reason.get((tid, axis)),
                "market_evidence_refs": cards[tid].get("evidence_catalog") or [],
                "theme_card": compact_card(cards[tid]),
                "sensing_axis_decision": copy.deepcopy(
                    (decisions[tid].get(axis.lower()) or {})),
            }
            cases.append(case)
    result = {
        "packet_kind": "evidence_collection_plan",
        "decision_date": sensing.get("decision_date"),
        "market_data_as_of": sensing.get("market_data_as_of"),
        "information_cutoff": sensing.get("information_cutoff"),
        "sensing_ref": sensing.get("artifact_hash"),
        "market_regime_ref": regime.get("artifact_hash"),
        "regime_input_hash": regime.get("regime_input_hash"),
        "case_count": len(cases),
        "cases": cases,
        "evidence_output_contract": {
            "information_cutoff": sensing.get("information_cutoff"),
            "one_case_coverage_per_case_id": True,
            "allowed_status": ["EVIDENCE_FOUND", "NO_NEW_DIRECT_FACT_FOUND"],
        },
    }
    result["evidence_plan_hash"] = content_hash(result)
    result["evidence_output_contract"]["evidence_plan_hash"] = result[
        "evidence_plan_hash"]
    return result


def build_sensing_batches(sensing_path: Path, batch_size: int) -> list[dict]:
    """Partition without filtering; global peer facts remain inside every card."""
    if batch_size < 1 or batch_size > 100:
        raise ContractError(["SENSING_BATCH_SIZE_INVALID"])
    packet = build_sensing_packet(sensing_path)
    themes = packet.pop("themes")
    packet.pop("output_contract", None)
    global_theme_count = packet.pop("theme_count")
    count = (len(themes) + batch_size - 1) // batch_size
    packets = []
    for index in range(count):
        rows = themes[index * batch_size:(index + 1) * batch_size]
        value = {
            **packet,
            "packet_kind": "sensing_model_input_batch",
            "global_theme_count": global_theme_count,
            "batch_index": index + 1,
            "batch_count": count,
            "batch_theme_count": len(rows),
            "batch_theme_ids": [row["theme_id"] for row in rows],
            "themes": rows,
            "batch_output_contract": {
                "batch_index": index + 1,
                "sensing_input_hash": packet["sensing_input_hash"],
                "correction_attempts": "0 or 1",
                "theme_decisions": "exactly one per batch_theme_id; do not output other themes",
                "reconciliation": [],
            },
        }
        value["batch_input_hash"] = content_hash({
            "sensing_input_hash": value["sensing_input_hash"],
            "prompt_ref": value["prompt_ref"],
            "batch_index": value["batch_index"],
            "batch_count": value["batch_count"],
            "batch_theme_ids": value["batch_theme_ids"],
            "themes": value["themes"],
        })
        value["batch_output_contract"]["batch_input_hash"] = value["batch_input_hash"]
        packets.append(value)
    if sum(item["batch_theme_count"] for item in packets) != global_theme_count:
        raise ContractError(["SENSING_BATCH_ACCOUNTING_FAILED"])
    return packets


def merge_sensing_batches(sensing_path: Path, outputs_dir: Path,
                          batch_size: int) -> dict:
    sensing = read_artifact_checked(sensing_path, "sensing")
    cards = sensing.get("theme_cards") or []
    packets = build_sensing_batches(sensing_path, batch_size)
    decisions = {}
    correction_attempts = 0
    expected_files = {f"batch-{packet['batch_index']:03d}.output.json" for packet in packets}
    actual_files = {path.name for path in outputs_dir.glob("batch-*.output.json")}
    if actual_files != expected_files:
        raise ContractError(["SENSING_BATCH_OUTPUT_SET_MISMATCH"],
                            f"missing={sorted(expected_files-actual_files)} extra={sorted(actual_files-expected_files)}")
    card_by_id = {card.get("theme_id"): card for card in cards}
    for packet in packets:
        input_path = outputs_dir / f"batch-{packet['batch_index']:03d}.input.json"
        if not input_path.exists() or load_json(input_path) != packet:
            raise ContractError(["SENSING_BATCH_INPUT_TAMPERED_OR_MISSING"],
                                str(input_path))
        path = outputs_dir / f"batch-{packet['batch_index']:03d}.output.json"
        output = load_json(path)
        if output.get("batch_index") != packet["batch_index"]:
            raise ContractError(["SENSING_BATCH_INDEX_MISMATCH"], str(path))
        if output.get("sensing_input_hash") != sensing.get("sensing_input_hash"):
            raise ContractError(["SENSING_INPUT_HASH_MISMATCH"], str(path))
        if output.get("batch_input_hash") != packet.get("batch_input_hash"):
            raise ContractError(["SENSING_BATCH_INPUT_HASH_MISMATCH"], str(path))
        if output.get("reconciliation", []) != []:
            raise ContractError(["RECONCILIATION_VALIDATION_NOT_IMPLEMENTED"], str(path))
        subset = [card_by_id[theme_id] for theme_id in packet["batch_theme_ids"]]
        errors = validate_sensing(subset, output)
        if errors:
            raise ContractError(errors, str(path))
        for row in output["theme_decisions"]:
            tid = row["theme_id"]
            if tid in decisions:
                raise ContractError(["DUPLICATE_THEME_DECISION"], tid)
            decisions[tid] = row
        correction_attempts = max(correction_attempts,
                                  int(output.get("correction_attempts", 0)))
    ordered_ids = [card.get("theme_id") for card in cards]
    if set(decisions) != set(ordered_ids):
        raise ContractError(["THEME_ACCOUNTING_MISMATCH"])
    return {
        "sensing_input_hash": sensing.get("sensing_input_hash"),
        "correction_attempts": correction_attempts,
        "theme_decisions": [decisions[tid] for tid in ordered_ids],
        "reconciliation": [],
        "technical_batching": {
            "method": "DETERMINISTIC_ALL_THEME_PARTITION",
            "batch_count": len(packets),
            "batch_size": batch_size,
            "cross_batch_reconciliation": "NOT_IMPLEMENTED",
        },
    }


def _calibration_inputs(sensing_path: Path, sensing_output_path: Path) -> tuple[dict, dict]:
    sensing = read_artifact_checked(sensing_path, "sensing")
    if (sensing.get("theme_decisions") not in (None, [])
            or (sensing.get("validation") or {}).get("status") != "NOT_RUN"):
        raise ContractError(["CALIBRATION_DRAFT_SENSING_REQUIRED"])
    supplied = load_json(sensing_output_path)
    errors = validate_sensing(sensing.get("theme_cards") or [], supplied)
    if supplied.get("sensing_input_hash") != sensing.get("sensing_input_hash"):
        errors.append("SENSING_INPUT_HASH_MISMATCH")
    batching = supplied.get("technical_batching") or {}
    if batching.get("method") != "DETERMINISTIC_ALL_THEME_PARTITION":
        errors.append("CALIBRATION_MERGED_SENSING_OUTPUT_REQUIRED")
    if batching.get("global_candidate_calibration") == "GLOBAL_DOWNGRADE_CALIBRATION_APPLIED":
        errors.append("CALIBRATION_ALREADY_APPLIED")
    if errors:
        raise ContractError(errors)
    return sensing, supplied


def _signal_counts(rows: list[dict]) -> dict:
    result = {"theme_count": len(rows)}
    for axis in ("opportunity", "risk"):
        result[axis] = {signal: sum(
            (row["decision"].get(axis) or {}).get("signal") == signal for row in rows)
            for signal in ("CANDIDATE", "WATCH", "NONE")}
    return result


def _calibration_card(card: dict) -> dict:
    """Keep axis-calibration facts without repeating formal-stage contracts."""
    compact = compact_card(card)
    caps = compact.get("permission_caps") or {}
    compact["permission_caps"] = {
        "max_sensing_opportunity_signal": caps.get(
            "max_sensing_opportunity_signal"),
        "max_sensing_risk_signal": caps.get("max_sensing_risk_signal"),
        "reason_codes": caps.get("reason_codes") or [],
    }
    compact.pop("evidence_catalog", None)
    return compact


def build_calibration_packet(sensing_path: Path, sensing_output_path: Path) -> dict:
    """Build one global, downgrade-only case for every initially candidate axis."""
    sensing, supplied = _calibration_inputs(sensing_path, sensing_output_path)
    cards = sensing.get("theme_cards") or []
    decisions = {row["theme_id"]: row for row in supplied["theme_decisions"]}
    joined = [{"card": card, "decision": decisions[card["theme_id"]]} for card in cards]
    all_market = _signal_counts(joined)
    layers = []
    for layer in sorted({row["card"].get("universe_layer") for row in joined}):
        layer_rows = [row for row in joined if row["card"].get("universe_layer") == layer]
        layers.append({"universe_layer": layer, **_signal_counts(layer_rows)})
    prompt_path = Path(__file__).resolve().parent.parent / "prompts" / "sensing-calibration-v1.md"
    prompt_ref = file_hash(prompt_path)
    sensing_output_hash = content_hash(supplied)
    cases = []
    for row in joined:
        card, decision = row["card"], row["decision"]
        for axis_name, axis_key in (("OPPORTUNITY", "opportunity"), ("RISK", "risk")):
            if decision[axis_key]["signal"] != "CANDIDATE":
                continue
            case = {
                "case_id": f"{card['theme_id']}::{axis_name}",
                "theme_id": card["theme_id"], "axis": axis_name,
                "theme_card": _calibration_card(card),
                "initial_axis_decision": copy.deepcopy(decision[axis_key]),
                "other_axis_signal": decision[
                    "risk" if axis_key == "opportunity" else "opportunity"]["signal"],
            }
            case["case_input_hash"] = content_hash({
                "prompt_ref": prompt_ref, "sensing_output_hash": sensing_output_hash, **case})
            cases.append(case)
    packet = {
        "packet_kind": "sensing_global_candidate_calibration_input",
        "decision_date": sensing.get("decision_date"),
        "market_data_as_of": sensing.get("market_data_as_of"),
        "sensing_input_hash": sensing.get("sensing_input_hash"),
        "sensing_artifact_hash": sensing.get("artifact_hash"),
        "sensing_output_hash": sensing_output_hash, "prompt_ref": prompt_ref,
        "all_market_signal_distribution": all_market,
        "layer_signal_distributions": layers, "case_count": len(cases), "cases": cases,
        "output_contract": {
            "correction_attempts": "0 or 1", "one_result_per_case_axis": True,
            "allowed_actions": sorted(CALIBRATION_ACTIONS),
            "required_explanations": ["why_not_common_market_movement",
                                      "why_immediate_verification_matters"],
        },
    }
    packet["calibration_input_hash"] = content_hash(packet)
    return packet


def apply_calibration(sensing_path: Path, sensing_output_path: Path,
                      calibration_output_path: Path) -> dict:
    """Apply validated KEEP-or-downgrade decisions; never upgrades an axis."""
    sensing, supplied = _calibration_inputs(sensing_path, sensing_output_path)
    packet = build_calibration_packet(sensing_path, sensing_output_path)
    authored = load_json(calibration_output_path)
    errors = []
    if set(authored) != CALIBRATION_ROOT_KEYS:
        errors.append("CALIBRATION_OUTPUT_ROOT_SHAPE_INVALID")
    for key in ("calibration_input_hash", "sensing_input_hash", "sensing_artifact_hash",
                "sensing_output_hash", "prompt_ref"):
        if authored.get(key) != packet.get(key):
            errors.append(f"CALIBRATION_{key.upper()}_MISMATCH")
    attempts = authored.get("correction_attempts")
    if type(attempts) is not int or attempts not in (0, 1):
        errors.append("CALIBRATION_CORRECTION_ATTEMPTS_INVALID")
    rows = authored.get("cases")
    if not isinstance(rows, list):
        errors.append("CALIBRATION_CASES_NOT_ARRAY")
        rows = []
    expected = packet["cases"]
    if len(rows) != len(expected):
        errors.append("CALIBRATION_CASE_ACCOUNTING_MISMATCH")
    for actual, wanted in zip(rows, expected):
        if not isinstance(actual, dict) or set(actual) != CALIBRATION_CASE_KEYS:
            errors.append("CALIBRATION_CASE_SHAPE_INVALID")
            continue
        for key in ("case_id", "case_input_hash", "theme_id", "axis"):
            if actual.get(key) != wanted.get(key):
                errors.append(f"CALIBRATION_CASE_{key.upper()}_MISMATCH")
        if actual.get("action") not in CALIBRATION_ACTIONS:
            errors.append("CALIBRATION_ACTION_INVALID")
        for key in ("why_not_common_market_movement", "why_immediate_verification_matters"):
            if not isinstance(actual.get(key), str) or not actual[key].strip():
                errors.append("CALIBRATION_EXPLANATION_MISSING")
    if errors:
        raise ContractError(errors)
    result = copy.deepcopy(supplied)
    decisions = {row["theme_id"]: row for row in result["theme_decisions"]}
    audit = []
    for actual in rows:
        axis_key = actual["axis"].lower()
        current = decisions[actual["theme_id"]][axis_key]
        initial = copy.deepcopy(current)
        if actual["action"] == "DOWNGRADE_WATCH":
            current["signal"] = "WATCH"
            current["reason"] = ("全局校准降级：" + actual["why_not_common_market_movement"].strip()
                                 + "；" + actual["why_immediate_verification_matters"].strip())
        elif actual["action"] == "DOWNGRADE_NONE":
            decisions[actual["theme_id"]][axis_key] = {
                "signal": "NONE", "structure_type": None, "path_pattern": None,
                "evidence_refs": [], "reason": ""}
        calibrated = copy.deepcopy(decisions[actual["theme_id"]][axis_key])
        audit.append({**actual, "initial_axis_decision": initial,
                      "calibrated_axis_decision": calibrated})
    for decision in result["theme_decisions"]:
        decision["derived_decision"] = derive(
            decision["opportunity"]["signal"], decision["risk"]["signal"])
    result["correction_attempts"] = max(result.get("correction_attempts", 0), attempts)
    batching = result["technical_batching"]
    batching["global_candidate_calibration"] = "GLOBAL_DOWNGRADE_CALIBRATION_APPLIED"
    batching["relation_group_reconciliation"] = "RELATION_GROUP_RECONCILIATION_NOT_IMPLEMENTED"
    result["candidate_calibration"] = {
        "status": "GLOBAL_DOWNGRADE_CALIBRATION_APPLIED",
        "calibration_input_hash": packet["calibration_input_hash"],
        "calibration_output_hash": content_hash(authored),
        "sensing_output_hash": packet["sensing_output_hash"],
        "prompt_ref": packet["prompt_ref"], "case_count": len(audit), "audit": audit}
    final_errors = validate_sensing(sensing.get("theme_cards") or [], result)
    if final_errors:
        raise ContractError(final_errors)
    return result


def build_review_packet(sensing_path: Path, regime_path: Path,
                        evidence_path: Path) -> dict:
    sensing, regime = validate_market_regime_for_sensing(sensing_path, regime_path)
    cards = {card.get("theme_id"): compact_card(card)
             for card in sensing.get("theme_cards", [])}
    decisions = {row.get("theme_id"): row for row in sensing.get("theme_decisions", [])}
    plans, plan_errors = effective_review_plan(sensing, regime)
    if plan_errors:
        raise ContractError(plan_errors)
    evidence = read_artifact_checked(evidence_path, "evidence")
    if evidence.get("information_cutoff") != sensing.get("information_cutoff"):
        raise ContractError(["EVIDENCE_CUTOFF_MISMATCH"])
    # Reject an invalid or incomplete evidence package before spending a
    # formal LLM call; update_ledger repeats the same final gate after judgment.
    from update_ledger import (_validate_evidence,
                               validate_evidence_case_coverage)
    evidence_ids = _validate_evidence(evidence, sensing["information_cutoff"])
    evidence_plan = build_evidence_plan(sensing_path, regime_path)
    coverage_errors = validate_evidence_case_coverage(
        evidence, evidence_plan, evidence_ids, sensing["information_cutoff"])
    if coverage_errors:
        raise ContractError(coverage_errors)
    coverage_by_case = {row["case_id"]: row for row in evidence["case_coverage"]}
    cases = []
    for tid in [card.get("theme_id") for card in sensing.get("theme_cards", [])]:
        axes = [axis for axis in ("OPPORTUNITY", "RISK")
                if axis in plans.get(tid, set())]
        if not axes:
            continue
        if tid not in cards or tid not in decisions:
            raise ContractError(["REVIEW_PLAN_UNKNOWN_THEME"], str(tid))
        cases.append({
            "theme_id": tid, "review_axes": axes,
            "theme_card": cards[tid], "sensing_decision": decisions[tid],
            "case_coverage": [coverage_by_case[f"{tid}::{axis}"] for axis in axes],
        })
    prompt_path = Path(__file__).resolve().parent.parent / "prompts" / "theme-judgment-v1.md"
    return {
        "packet_kind": "theme_review_model_input",
        "decision_date": sensing.get("decision_date"),
        "market_data_as_of": sensing.get("market_data_as_of"),
        "information_cutoff": sensing.get("information_cutoff"),
        "prompt_ref": file_hash(prompt_path),
        "market_regime_ref": regime.get("artifact_hash"),
        "regime_input_hash": regime.get("regime_input_hash"),
        "market_context": regime,
        "evidence_plan_hash": evidence_plan["evidence_plan_hash"],
        "review_theme_ids": [row["theme_id"] for row in cases],
        "review_cases": cases,
        "external_evidence": evidence.get("evidence_items") or [],
        "output_contract": {
            "correction_attempts": "0 or 1",
            "review_theme_ids": [row["theme_id"] for row in cases],
            "market_regime_ref": regime.get("artifact_hash"),
            "regime_input_hash": regime.get("regime_input_hash"),
            "themes": "exactly one row per review theme; use the JSON skeleton in the prompt",
        },
    }


def stamp(kind: str, source: dict) -> dict:
    schema_name = {"evidence": "evidence", "theme_judgments": "theme-judgments",
                   "market_regime": "market-regime"}[kind]
    forbidden = {"artifact_kind", "schema_version", "schema_ref", "producer_version",
                 "created_at", "artifact_hash"}
    bare = {key: value for key, value in source.items() if key not in forbidden}
    result = {**artifact(kind, schema_name), **bare}
    result["artifact_hash"] = content_hash(result)
    return result


def write_once(path: Path, value: dict) -> None:
    if path.exists():
        if load_json(path) == value:
            return
        raise ContractError(["MODEL_IO_OUTPUT_CONFLICT"], str(path))
    atomic_write_json(path, value)


def main():
    parser = argparse.ArgumentParser(description="V3 compact model I/O helper")
    sub = parser.add_subparsers(dest="command", required=True)
    packet = sub.add_parser("sensing-packet")
    packet.add_argument("--sensing", required=True)
    packet.add_argument("--output", required=True)
    batches = sub.add_parser("sensing-batches")
    batches.add_argument("--sensing", required=True)
    batches.add_argument("--output-dir", required=True)
    batches.add_argument("--batch-size", type=int, default=60)
    merge = sub.add_parser("merge-sensing")
    merge.add_argument("--sensing", required=True)
    merge.add_argument("--outputs-dir", required=True)
    merge.add_argument("--batch-size", type=int, default=60)
    merge.add_argument("--output", required=True)
    calibration_packet = sub.add_parser("calibration-packet")
    calibration_packet.add_argument("--sensing", required=True)
    calibration_packet.add_argument("--sensing-output", required=True)
    calibration_packet.add_argument("--output", required=True)
    apply_parser = sub.add_parser("apply-calibration")
    apply_parser.add_argument("--sensing", required=True)
    apply_parser.add_argument("--sensing-output", required=True)
    apply_parser.add_argument("--calibration-output", required=True)
    apply_parser.add_argument("--output", required=True)
    regime_packet = sub.add_parser("market-regime-packet")
    regime_packet.add_argument("--sensing", required=True)
    regime_packet.add_argument("--output", required=True)
    evidence_plan = sub.add_parser("evidence-plan")
    evidence_plan.add_argument("--sensing", required=True)
    evidence_plan.add_argument("--market-regime", required=True)
    evidence_plan.add_argument("--output", required=True)
    review = sub.add_parser("theme-review-packet")
    review.add_argument("--sensing", required=True)
    review.add_argument("--market-regime", required=True)
    review.add_argument("--evidence", required=True)
    review.add_argument("--output", required=True)
    stamp_parser = sub.add_parser("stamp")
    stamp_parser.add_argument("--kind", required=True,
                              choices=("evidence", "theme_judgments", "market_regime"))
    stamp_parser.add_argument("--input", required=True, help="bare model JSON")
    stamp_parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        if args.command == "sensing-packet":
            value = build_sensing_packet(Path(args.sensing))
            write_once(Path(args.output), value)
        elif args.command == "sensing-batches":
            output_dir = Path(args.output_dir)
            if output_dir.exists() and any(output_dir.iterdir()):
                raise ContractError(["MODEL_IO_OUTPUT_CONFLICT"], str(output_dir))
            output_dir.mkdir(parents=True, exist_ok=True)
            values = build_sensing_batches(Path(args.sensing), args.batch_size)
            for value in values:
                write_once(output_dir / f"batch-{value['batch_index']:03d}.input.json", value)
            print(f"MODEL_IO PASS｜batches={len(values)}｜{output_dir}")
            return
        elif args.command == "merge-sensing":
            value = merge_sensing_batches(Path(args.sensing), Path(args.outputs_dir),
                                           args.batch_size)
            write_once(Path(args.output), value)
        elif args.command == "calibration-packet":
            value = build_calibration_packet(
                Path(args.sensing), Path(args.sensing_output))
            write_once(Path(args.output), value)
        elif args.command == "apply-calibration":
            value = apply_calibration(
                Path(args.sensing), Path(args.sensing_output),
                Path(args.calibration_output))
            write_once(Path(args.output), value)
        elif args.command == "market-regime-packet":
            value = build_market_regime_packet(Path(args.sensing))
            write_once(Path(args.output), value)
        elif args.command == "evidence-plan":
            value = build_evidence_plan(
                Path(args.sensing), Path(args.market_regime))
            write_once(Path(args.output), value)
        elif args.command == "theme-review-packet":
            value = build_review_packet(
                Path(args.sensing), Path(args.market_regime), Path(args.evidence))
            write_once(Path(args.output), value)
        else:
            value = stamp(args.kind, load_json(args.input))
            write_once(Path(args.output), value)
    except ContractError as exc:
        print(json.dumps({"status": "FAIL", "reason_codes": exc.reasons,
                          "detail": str(exc)}, ensure_ascii=False))
        raise SystemExit(2)
    print(f"MODEL_IO PASS｜{args.output}")


if __name__ == "__main__":
    main()
