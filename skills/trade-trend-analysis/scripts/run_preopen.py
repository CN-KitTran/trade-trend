#!/usr/bin/env python3
"""Prepare and validate a PREOPEN sensing run from an immutable close freeze.

The script does not call an LLM. An orchestrator may supply a frozen sensing
output. Missing model output is an explicit DRAFT, never a market conclusion.
"""

from __future__ import annotations

import argparse
import copy
import json
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from v3_common import (ContractError, artifact, atomic_write_json, atomic_write_text,
                       content_address_without, content_hash, immutable_version_dir,
                       load_json, mode_root, now_iso, parse_ts, read_artifact_checked)
from validate_outputs import derive, validate_sensing

SIGNAL_ORDER = {"NONE": 0, "WATCH": 1, "CANDIDATE": 2}
WINDOWS = ("1D", "3D", "5D", "10D", "20D")


def _percentile(value, values):
    valid = [item for item in values if item is not None]
    if value is None or not valid:
        return None
    return (sum(item < value for item in valid)
            + 0.5 * sum(item == value for item in valid)) / len(valid)


def _at(day: str, clock: str) -> datetime:
    parsed_time = time.fromisoformat(clock)
    return datetime.combine(datetime.fromisoformat(day).date(), parsed_time,
                            tzinfo=ZoneInfo("Asia/Shanghai"))


def _window(decision_date: str, now: datetime, config: dict,
            calendar_auction_start: str) -> tuple[str, dict]:
    required = ("config_version", "timezone", "official_run_window",
                "information_cutoff_at", "auction_start_at")
    if any(k not in config for k in required) or config.get("timezone") != "Asia/Shanghai":
        raise ContractError(["RUNTIME_CONFIG_CONTRACT_MISSING"])
    version = config.get("config_version")
    if not isinstance(version, str) or not version.startswith("sha256:") or len(version) != 71:
        raise ContractError(["RUNTIME_CONFIG_VERSION_NOT_IMMUTABLE"])
    if version != content_address_without(config, "config_version"):
        raise ContractError(["RUNTIME_CONFIG_VERSION_CONTENT_MISMATCH"])
    win = config["official_run_window"]
    start, end = _at(decision_date, win["start_at"]), _at(decision_date, win["end_at"])
    cutoff, auction = _at(decision_date, config["information_cutoff_at"]), _at(
        decision_date, config["auction_start_at"])
    if auction != parse_ts(calendar_auction_start).astimezone(ZoneInfo("Asia/Shanghai")):
        raise ContractError(["RUNTIME_AUCTION_CALENDAR_MISMATCH"])
    if not start < end < auction or cutoff >= auction:
        raise ContractError(["OFFICIAL_WINDOW_CONFIG_INVALID"])
    status = "EARLY_DRAFT" if now < start else ("ELIGIBLE" if now <= end else "LATE_REJECTED")
    # The information cutoff is what was actually knowable when this run
    # started, never a configured future clock.  The configured cutoff remains
    # the latest possible bound for an eligible scheduled run.
    effective_cutoff = min(now, cutoff)
    return status, {"start_at": start.isoformat(), "end_at": end.isoformat(),
                    "information_cutoff": effective_cutoff.isoformat(),
                    "configured_latest_information_cutoff": cutoff.isoformat(),
                    "auction_start_at": auction.isoformat()}


def _load_freeze(path: Path) -> tuple[dict, dict]:
    manifest = read_artifact_checked(path / "manifest.json", "close_freeze_manifest")
    observations = read_artifact_checked(path / "observations.json", "observations")
    if manifest.get("close_freeze_status") != "PASS":
        raise ContractError(["CLOSE_FREEZE_NOT_PASS"])
    checks = {
        "sector_market_frame.json": manifest["market_frame_ref"],
        "sector_identity.json": manifest["identity_frame_ref"],
        "observations.json": manifest["observations_ref"],
    }
    for name, expected in checks.items():
        actual = content_hash(load_json(path / name))
        if actual != expected:
            raise ContractError(["CLOSE_FREEZE_HASH_MISMATCH"], name)
    return manifest, observations


def _theme_cards(observations: dict, registry: dict, matrix: dict) -> tuple[list[dict], list[dict]]:
    source_obs = {r["source_sector_id"]: r for r in observations["source_observations"]
                  if r.get("source_scope") == "PRIMARY"}
    themes = registry.get("themes")
    exclusions = set(registry.get("excluded_source_sector_ids") or [])
    provisional_rows = observations.get("provisional_labels") or []
    if not isinstance(provisional_rows, list):
        raise ContractError(["PROVISIONAL_LABEL_ACCOUNTING_INVALID"])
    provisional_by_id = {row.get("provisional_id"): row for row in provisional_rows
                         if isinstance(row, dict) and row.get("provisional_id")}
    if len(provisional_by_id) != len(provisional_rows):
        raise ContractError(["PROVISIONAL_LABEL_ACCOUNTING_INVALID"])
    if not isinstance(themes, list):
        raise ContractError(["THEME_REGISTRY_CONTRACT_MISSING"])
    mapped = {}
    mapped_provisional = {}
    cards = []
    limited_or_excluded = []
    seen_theme_ids = set()
    for theme in themes:
        tid = theme.get("theme_id")
        if not isinstance(tid, str) or not tid or tid in seen_theme_ids:
            raise ContractError(["THEME_REGISTRY_THEME_ID_INVALID_OR_DUPLICATE"])
        seen_theme_ids.add(tid)
        active_bindings = [b for b in theme.get("source_bindings", [])
                           if isinstance(b, dict) and b.get("valid_to") is None]
        bindings = [b.get("source_id") for b in active_bindings
                    if b.get("source_kind") == "SOURCE_SECTOR"]
        provisional_bindings = [b.get("source_id") for b in active_bindings
                                if b.get("source_kind") == "PROVISIONAL_LABEL"]
        if any(not isinstance(sid, str) or not sid for sid in bindings):
            raise ContractError(["THEME_REGISTRY_SOURCE_BINDING_INVALID"], tid)
        if any(not isinstance(pid, str) or not pid for pid in provisional_bindings):
            raise ContractError(["THEME_REGISTRY_PROVISIONAL_BINDING_INVALID"], tid)
        lifecycle = theme.get("lifecycle_status")
        if lifecycle not in {"STABLE", "PROVISIONAL", "RETIRED"}:
            raise ContractError(["THEME_REGISTRY_LIFECYCLE_INVALID"], tid)
        all_binding_keys = [(b.get("source_kind"), b.get("source_id"))
                            for b in active_bindings]
        if len(all_binding_keys) != len(set(all_binding_keys)):
            raise ContractError(["THEME_REGISTRY_DUPLICATE_ACTIVE_BINDING"], tid)
        if any(sid not in source_obs for sid in bindings):
            raise ContractError(["THEME_REGISTRY_UNKNOWN_SOURCE_BINDING"], tid)
        if (set(bindings) | set(provisional_bindings)) & exclusions:
            raise ContractError(["THEME_REGISTRY_MAPPED_AND_EXCLUDED_CONFLICT"], tid)
        if lifecycle == "RETIRED" and (bindings or provisional_bindings):
            raise ContractError(["RETIRED_THEME_HAS_ACTIVE_BINDINGS"], tid)
        for sid in bindings:
            if sid in mapped and mapped[sid] != tid:
                raise ContractError(["SOURCE_MAPPED_TO_MULTIPLE_THEMES"], sid)
            mapped[sid] = tid
        for pid in provisional_bindings:
            if pid not in provisional_by_id:
                raise ContractError(["UNKNOWN_PROVISIONAL_LABEL_BINDING"], pid)
            if pid in mapped_provisional and mapped_provisional[pid] != tid:
                raise ContractError(["PROVISIONAL_LABEL_MAPPED_TO_MULTIPLE_THEMES"], pid)
            mapped_provisional[pid] = tid
        proxy = theme.get("market_proxy_source_id")
        obs = source_obs.get(proxy)
        if lifecycle == "RETIRED":
            continue
        if proxy is not None and proxy not in bindings:
            raise ContractError(["THEME_PROXY_NOT_IN_ACTIVE_BINDINGS"], tid)
        if obs is None:
            limited_or_excluded.append({
                "theme_id": tid, "source_sector_ids": sorted(bindings),
                "provisional_label_ids": sorted(provisional_bindings),
                "status": "NO_DIRECTION",
                "reason_codes": ["MARKET_PROXY_MISSING_OR_INVALID"],
            })
            continue
        dimension_health = obs.get("data_health") or {}
        if any(value == "INVALID" for value in dimension_health.values()):
            limited_or_excluded.append({
                "theme_id": tid, "source_sector_ids": sorted(bindings),
                "provisional_label_ids": sorted(provisional_bindings),
                "status": "NO_DIRECTION", "reason_codes": ["CORE_OBSERVATION_INVALID"],
            })
            continue
        limited = any(value == "LIMITED" for value in dimension_health.values())
        provisional = lifecycle == "PROVISIONAL"
        health = "LIMITED" if limited else "SUFFICIENT"
        matches = [entry for entry in matrix.get("entries", [])
                   if entry.get("subject_scope") == "THEME"
                   and entry.get("field_or_window") == "CORE_OBSERVATIONS"
                   and entry.get("data_health") == health
                   and entry.get("coverage_type") == "FULL"]
        if len(matches) != 1:
            raise ContractError(["PERMISSION_MATRIX_THEME_RULE_AMBIGUOUS_OR_MISSING"],
                                f"theme={tid} health={health}")
        rule = matches[0]
        allowed = rule.get("allowed_sensing_signals") or []
        if any(signal not in SIGNAL_ORDER for signal in allowed) or not allowed:
            raise ContractError(["PERMISSION_MATRIX_SIGNAL_RULE_INVALID"], tid)
        cap = max(allowed, key=SIGNAL_ORDER.get)
        if provisional and SIGNAL_ORDER[cap] > SIGNAL_ORDER["WATCH"]:
            cap = "WATCH"
        price = copy.deepcopy(obs["metrics"]["price"])
        # Cross-sectional position is a theme-card fact.  It is deliberately
        # populated only after stable mapping so overlapping supplier labels do
        # not receive extra weight in the peer pool.
        price.pop("peer_percentiles", None)
        breadth = copy.deepcopy(obs["metrics"]["breadth"])
        attention = copy.deepcopy(obs["metrics"]["attention"])
        cards.append({
            "theme_id": tid, "display_name": theme.get("display_name"),
            "universe_layer": theme.get("universe_layer", "THEME"),
            "source_sector_ids": sorted(bindings), "market_proxy_source_id": proxy,
            "provisional_label_ids": sorted(provisional_bindings),
            "price": price, "breadth": breadth, "attention": attention,
            "prior_state": theme.get("prior_state", "FIRST_OBSERVATION"),
            "data_health": {"price": dimension_health.get("price", "INVALID"),
                            "breadth": dimension_health.get("breadth", "INVALID"),
                            "attention": dimension_health.get("attention", "INVALID"),
                            "limitations": obs.get("limitations", [])},
            "permission_caps": {
                "max_sensing_opportunity_signal": cap,
                "max_sensing_risk_signal": cap,
                "formal_theme_decision_allowed": (not provisional and bool(
                    (rule.get("allowed_opportunity_stages") or [])
                    or (rule.get("allowed_risk_levels") or []))),
                "allowed_opportunity_stages": rule.get("allowed_opportunity_stages") or [],
                "allowed_risk_levels": rule.get("allowed_risk_levels") or [],
                "stock_selection_allowed": False,
                "hard_fact_alert_allowed": False,
                "reason_codes": (["PROVISIONAL_WATCH_ONLY"] if provisional else []),
                "matrix_version": matrix.get("matrix_version"),
            },
            "evidence_catalog": [f"{tid}:price", f"{tid}:breadth", f"{tid}:attention"],
            "source_observation_refs": obs.get("provenance", []),
        })
    for layer in ("INDUSTRY", "THEME"):
        pool = [card for card in cards
                if ("INDUSTRY" if card.get("universe_layer") == "INDUSTRY"
                    else "THEME") == layer]
        for window in WINDOWS:
            values = [(card.get("price") or {}).get("excess_returns", {}).get(window)
                      for card in pool]
            sample_count = sum(value is not None for value in values)
            for card, value in zip(pool, values):
                card["price"].setdefault("peer_percentiles", {})[window] = {
                    "value": _percentile(value, values),
                    "sample_count": sample_count,
                    "peer_layer": layer,
                }
    unaccounted = set(source_obs) - set(mapped) - exclusions
    invalid_exclusions = exclusions - (set(source_obs) | set(provisional_by_id))
    if unaccounted or invalid_exclusions:
        raise ContractError(["THEME_REGISTRY_SOURCE_ACCOUNTING_FAILED"],
                            f"unaccounted={sorted(unaccounted)[:5]} invalid_exclusions={sorted(invalid_exclusions)[:5]}")
    for excluded_id in sorted(exclusions):
        key = "provisional_id" if excluded_id in provisional_by_id else "source_sector_id"
        limited_or_excluded.append({key: excluded_id, "theme_id": None,
                                    "status": "EXCLUDED",
                                    "reason_codes": ["REGISTRY_EXCLUSION"]})
    for pid in sorted(set(provisional_by_id) - set(mapped_provisional) - exclusions):
        row = provisional_by_id[pid]
        limited_or_excluded.append({
            "provisional_id": pid, "display_name": row.get("source_label"),
            "theme_id": None, "status": "NO_DIRECTION",
            "reason_codes": ["PROVISIONAL_MARKET_PROXY_MISSING"],
        })
    return sorted(cards, key=lambda c: (c["universe_layer"], c["theme_id"])), limited_or_excluded


def main():
    parser = argparse.ArgumentParser(description="V3 PREOPEN sensing skeleton")
    parser.add_argument("--freeze-dir", required=True)
    parser.add_argument("--theme-registry", required=True)
    parser.add_argument("--permission-matrix", required=True)
    parser.add_argument("--runtime-config", required=True)
    parser.add_argument("--decision-date", required=True)
    parser.add_argument("--now", help="timezone-aware ISO timestamp; default current time")
    baseline = parser.add_mutually_exclusive_group(required=True)
    baseline.add_argument("--first-run", action="store_true",
                          help="explicit bootstrap only; daily continuity is not implemented yet")
    baseline.add_argument("--previous-ledger",
                          help="reserved for the previous published PREOPEN ledger")
    parser.add_argument("--previous-sensing",
                        help="reserved; must match --previous-ledger once continuity is implemented")
    parser.add_argument("--sensing-output", help="frozen LLM output JSON")
    parser.add_argument("--release-mode", choices=("INTERNAL_GATE", "SHADOW", "OFFICIAL"),
                        default="INTERNAL_GATE")
    parser.add_argument("--output-root", default=str(Path(__file__).resolve().parent.parent / "data"))
    args = parser.parse_args()

    if args.release_mode == "OFFICIAL":
        raise SystemExit("PREOPEN FAIL: OFFICIAL_EXECUTION_AND_PUBLICATION_NOT_IMPLEMENTED")
    if args.previous_ledger or args.previous_sensing:
        raise SystemExit("PREOPEN FAIL: PREVIOUS_RUN_CONTINUITY_NOT_IMPLEMENTED")
    if not args.first_run:
        raise SystemExit("PREOPEN FAIL: EXPLICIT_FIRST_RUN_REQUIRED")

    started = args.now or now_iso()
    now = parse_ts(started).astimezone(ZoneInfo("Asia/Shanghai"))
    freeze, observations = _load_freeze(Path(args.freeze_dir))
    if freeze.get("release_mode") != args.release_mode:
        raise SystemExit("PREOPEN FAIL: RELEASE_MODE_CHAIN_MISMATCH")
    if freeze.get("next_decision_date") != args.decision_date:
        raise SystemExit("PREOPEN FAIL: DECISION_DATE_NOT_NEXT_TRADING_DAY")
    registry, matrix, config = (load_json(args.theme_registry),
                                load_json(args.permission_matrix), load_json(args.runtime_config))
    if (not str(registry.get("registry_version", "")).startswith("sha256:")
            or not str(registry.get("snapshot_hash", "")).startswith("sha256:")
            or not registry.get("effective_as_of")):
        raise SystemExit("PREOPEN FAIL: THEME_REGISTRY_VERSION_INVALID")
    if (registry["snapshot_hash"] != content_address_without(
            registry, "registry_version", "snapshot_hash", "previous_registry_version")
            or registry["registry_version"] != content_address_without(
                registry, "registry_version")):
        raise SystemExit("PREOPEN FAIL: THEME_REGISTRY_CONTENT_ADDRESS_INVALID")
    if not str(matrix.get("matrix_version", "")).startswith("sha256:") or not matrix.get("entries"):
        raise SystemExit("PREOPEN FAIL: PERMISSION_MATRIX_VERSION_INVALID")
    if matrix["matrix_version"] != content_address_without(matrix, "matrix_version"):
        raise SystemExit("PREOPEN FAIL: PERMISSION_MATRIX_CONTENT_ADDRESS_INVALID")
    if content_hash(registry) != freeze.get("theme_registry_ref"):
        raise SystemExit("PREOPEN FAIL: THEME_REGISTRY_FREEZE_MISMATCH")
    if content_hash(matrix) != freeze.get("coverage_permission_matrix_ref"):
        raise SystemExit("PREOPEN FAIL: PERMISSION_MATRIX_FREEZE_MISMATCH")
    status, window = _window(args.decision_date, now, config,
                             freeze.get("next_auction_start_at", ""))
    if args.release_mode == "OFFICIAL" and status != "ELIGIBLE":
        raise SystemExit(f"PREOPEN FAIL: OFFICIAL_RUN_{status}")
    cards, excluded = _theme_cards(observations, registry, matrix)
    isolated_core_count = sum(
        1 for row in excluded
        if row.get("status") == "NO_DIRECTION"
        and "CORE_OBSERVATION_INVALID" in (row.get("reason_codes") or [])
    )
    global_limitations = (
        [f"LATEST_CORE_BREADTH_ISOLATED:{isolated_core_count}"]
        if isolated_core_count else [])
    sensing_input_hash = content_hash({"cards": cards, "observations": observations["artifact_hash"],
                                       "registry": registry["registry_version"],
                                       "matrix": matrix["matrix_version"]})
    sensing = {
        **artifact("sensing", "sensing", started),
        "session": "PREOPEN", "release_mode": args.release_mode,
        "decision_date": args.decision_date,
        "market_data_as_of": freeze["market_data_as_of"],
        "market_data_captured_at": freeze["market_data_captured_at"],
        "source_universe_version": observations["source_universe_version"],
        "information_cutoff": window["information_cutoff"],
        "run_window_status": status, "observations_ref": observations["artifact_hash"],
        "theme_registry_ref": registry["registry_version"],
        "coverage_permission_matrix_ref": matrix["matrix_version"],
        "sensing_input_hash": sensing_input_hash,
        "theme_cards": cards, "limited_or_excluded": excluded,
        "source_count": observations["source_count"],
        "accounted_count": observations["accounted_source_count"],
        "provisional_label_count": len(observations.get("provisional_labels") or []),
        "accounted_provisional_label_count": len(observations.get("provisional_labels") or []),
        "eligible_theme_count": len(cards), "judged_theme_count": 0,
        "global_data_health": ("LIMITED" if isolated_core_count else "OK"),
        "global_limitations": global_limitations,
        "validation": {"status": "NOT_RUN", "errors": []},
        "theme_decisions": [], "reconciliation": [], "review_plan": [],
    }
    publication_status = "DRAFT"
    if args.sensing_output:
        supplied = load_json(args.sensing_output)
        errors = validate_sensing(cards, supplied)
        if supplied.get("sensing_input_hash") != sensing_input_hash:
            errors.append("SENSING_INPUT_HASH_MISMATCH")
        sensing["validation"] = {"status": "PASS" if not errors else "FAIL",
                                 "errors": errors, "correction_attempts": supplied.get("correction_attempts", 0)}
        if errors:
            publication_status = "FAILED"
        else:
            sensing["theme_decisions"] = supplied["theme_decisions"]
            sensing["reconciliation"] = supplied.get("reconciliation", [])
            batching = supplied.get("technical_batching")
            if batching:
                sensing["technical_batching"] = batching
                calibration = supplied.get("candidate_calibration")
                if (batching.get("global_candidate_calibration")
                        == "GLOBAL_DOWNGRADE_CALIBRATION_APPLIED" and calibration):
                    sensing["candidate_calibration"] = calibration
                    sensing["global_limitations"].append(
                        "RELATION_GROUP_RECONCILIATION_NOT_IMPLEMENTED")
                else:
                    sensing["global_limitations"].append(
                        "TECHNICAL_BATCHED_SENSING_NO_CROSS_BATCH_RECONCILIATION")
            sensing["judged_theme_count"] = len(supplied["theme_decisions"])
            sensing["review_plan"] = []
            for decision in supplied["theme_decisions"]:
                axes = []
                if (decision.get("opportunity") or {}).get("signal") == "CANDIDATE":
                    axes.append("OPPORTUNITY")
                if (decision.get("risk") or {}).get("signal") == "CANDIDATE":
                    axes.append("RISK")
                if axes:
                    sensing["review_plan"].append({"theme_id": decision["theme_id"],
                                                   "review_axes": axes})
            publication_status = "READY_FOR_THEME_REVIEW"
    sensing["artifact_hash"] = content_hash(sensing)

    root = mode_root(args.output_root, args.release_mode)
    parent = root / "runs" / args.decision_date[:7] / args.decision_date / "preopen"
    parent.mkdir(parents=True, exist_ok=True)
    run_dir = immutable_version_dir(parent)
    run_dir.mkdir()
    atomic_write_json(run_dir / "sensing.json", sensing)
    manifest = {
        **artifact("preopen_manifest", "preopen-manifest", started),
        "run_id": f"{args.decision_date}-preopen-{run_dir.name}", "session": "PREOPEN",
        "release_mode": args.release_mode, "decision_date": args.decision_date,
        "market_data_as_of": freeze["market_data_as_of"],
        "market_data_captured_at": freeze["market_data_captured_at"],
        "source_universe_version": observations["source_universe_version"],
        "market_freeze_ref": freeze["artifact_hash"], "run_started_at": started,
        "validation_completed_at": now_iso(), "published_at": None,
        "official_run_window": window, "run_window_status": status,
        "publication_status": publication_status, "publication_completeness": None,
        "publication_lock_id": None, "amends_run_id": None,
        "amendment_reason_code": None, "sensing_ref": sensing["artifact_hash"],
        "input_hashes": {"registry": content_hash(registry), "matrix": content_hash(matrix),
                         "runtime_config": content_hash(config)},
    }
    manifest["artifact_hash"] = content_hash(manifest)
    atomic_write_json(run_dir / "manifest.json", manifest)
    diagnosis = (f"SENSING {sensing['validation']['status']}｜合格主题 {len(cards)}｜"
                 f"已判断 {sensing['judged_theme_count']}｜状态 {publication_status}\n")
    atomic_write_text(run_dir / "diagnostic.txt", diagnosis)
    print(f"PREOPEN {publication_status}｜{status}｜themes={len(cards)}｜{run_dir}")
    raise SystemExit(2 if publication_status == "FAILED" else 0)


if __name__ == "__main__":
    main()
