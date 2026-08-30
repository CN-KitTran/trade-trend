#!/usr/bin/env python3
"""CLOSE_FREEZE gate: validate, account, derive and immutably freeze D data.

The command deliberately accepts only complete trade-data-gateway envelopes.
It does not know supplier APIs and has no Top-N or prior-state fallback.
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path

from build_observations import build as build_observations
from v3_common import (ContractError, artifact, atomic_write_json, content_hash,
                       content_address_without, envelope_payload, file_hash,
                       immutable_version_dir, load_json,
                       mode_root, normalize_benchmark_history, now_iso, parse_ts,
                       validate_calendar)


def _non_negative_number(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and value >= 0)


def _breadth_accounting_valid(row: dict) -> bool:
    den = row.get("breadth_denominator")
    up, down = row.get("up_count"), row.get("down_count")
    flat, non_up = row.get("flat_count"), row.get("non_up_count")
    coverage = row.get("breadth_coverage")
    values = (den, up, down, flat, non_up)
    if any(value is not None and not _non_negative_number(value) for value in values):
        return False
    if den is None or den <= 0 or up is None:
        return False
    if coverage == "FULL":
        return (down is not None and flat is not None and non_up is not None
                and up + down + flat == den and down + flat == non_up)
    if coverage == "UP_DOWN_ONLY":
        return down is not None and flat is None and up + down == den
    if coverage == "UP_VS_NON_UP_ONLY":
        return down is None and flat is None and non_up is not None and up + non_up == den
    return False


def validate_gate(market_env: dict, identity_env: dict, calendar: dict) -> dict:
    reasons = []
    try:
        market = envelope_payload(market_env, "sector_market_frame")
        identity = envelope_payload(identity_env, "sector_identity")
    except ContractError as exc:
        raise exc
    market_hash = content_hash(market_env)
    identity_hash = content_hash(identity_env)

    required_market = ("frame_id", "frame_kind", "market_data_as_of",
                       "market_data_captured_at", "lookback_trading_days",
                       "core_contract_tier", "close_freeze_eligible",
                       "benchmark", "close_confirmation", "coverage_type",
                       "history_coverage", "history_limited_sector_ids",
                       "breadth_history_coverage", "breadth_limited_sector_ids",
                       "latest_core_coverage", "latest_core_limited_sector_ids",
                       "activity_history_coverage", "activity_limited_sector_ids",
                       "catalog_version", "trading_dates", "sectors",
                       "expected_sector_count", "returned_sector_count",
                       "primary_sector_count", "reference_sector_count")
    required_identity = ("requested_as_of", "identity_date_semantics",
                         "verified_close_frame_id", "verified_close_frame_hash",
                         "coverage_type", "catalog_version", "universe_version",
                         "catalog", "provisional_labels", "catalog_count",
                         "primary_sector_count", "reference_sector_count",
                         "provisional_label_count", "primary_membership_coverage")
    if any(k not in market for k in required_market):
        reasons.append("MARKET_FRAME_STRONG_CONTRACT_MISSING")
    if any(k not in identity for k in required_identity):
        reasons.append("IDENTITY_FRAME_STRONG_CONTRACT_MISSING")
    if reasons:
        raise ContractError(reasons)

    market_date = market["market_data_as_of"]
    next_day, next_session = validate_calendar(calendar, market_date)
    if market.get("frame_kind") != "CLOSE":
        reasons.append("MARKET_FRAME_KIND_NOT_CLOSE")
    if market.get("requested_as_of", market_date) != market_date:
        reasons.append("MARKET_FRAME_DATE_MISMATCH")
    if identity.get("requested_as_of") != market_date:
        reasons.append("IDENTITY_MARKET_DATE_MISMATCH")
    identity_semantics = identity.get("identity_date_semantics")
    if identity_semantics not in {
            "CLOSE_FREEZE_CURRENT_RELATION", "DECISION_WINDOW_CURRENT_RELATION"}:
        reasons.append("IDENTITY_DATE_SEMANTICS_INVALID")
    if identity.get("historical_reconstruction") not in (None, False):
        reasons.append("HISTORICAL_IDENTITY_RECONSTRUCTION_FORBIDDEN")
    if market.get("lookback_trading_days") != 60:
        reasons.append("LOOKBACK_L60_REQUIRED")
    if (market.get("core_contract_tier") != "V3_CORE_L60"
            or market.get("close_freeze_eligible") is not True):
        reasons.append("V3_CORE_CLOSE_FREEZE_NOT_ELIGIBLE")
    if market.get("coverage_type") != "FULL" or identity.get("coverage_type") != "FULL":
        reasons.append("CORE_COVERAGE_NOT_FULL")
    localized_history_limit = any(market.get(key) == "LIMITED" for key in (
        "history_coverage", "breadth_history_coverage", "activity_history_coverage"))
    if bool((market_env.get("meta") or {}).get("degraded")) != localized_history_limit:
        reasons.append("MARKET_META_LOCAL_LIMITATION_STATUS_MISMATCH")
    if market.get("catalog_version") != identity.get("catalog_version"):
        reasons.append("CATALOG_VERSION_MISMATCH")
    if identity.get("verified_close_frame_id") != market.get("frame_id"):
        reasons.append("CLOSE_FRAME_ID_INTERLOCK_FAILED")
    if identity.get("verified_close_frame_hash") != market_hash:
        reasons.append("CLOSE_FRAME_HASH_INTERLOCK_FAILED")

    # Market facts are D-close facts.  Identity is allowed to be either frozen
    # after that close on D, or captured during the closed decision window up to
    # (but never including) D+1 auction.  The latter is the normal Friday-close
    # -> Monday-preopen path and is not represented as historical reconstruction.
    if identity_semantics == "DECISION_WINDOW_CURRENT_RELATION":
        if identity.get("market_data_as_of") != market_date:
            reasons.append("IDENTITY_DECISION_WINDOW_MARKET_DATE_MISMATCH")
        if identity.get("decision_date") != next_day:
            reasons.append("IDENTITY_DECISION_DATE_MISMATCH")
        calendar_auction = next_session.get("auction_start_at")
        if (identity.get("next_auction_at") != calendar_auction
                or identity.get("decision_window_cutoff_at") != calendar_auction):
            reasons.append("IDENTITY_DECISION_WINDOW_CALENDAR_MISMATCH")
        observed_at = identity.get("identity_observed_at")
        confirmation_for_window = market.get("close_confirmation") or {}
        earliest = confirmation_for_window.get("earliest_freeze_at")
        try:
            if (not observed_at or not earliest or not calendar_auction
                    or parse_ts(observed_at) < parse_ts(earliest)
                    or parse_ts(observed_at) >= parse_ts(calendar_auction)):
                reasons.append("IDENTITY_OUTSIDE_DECISION_WINDOW")
        except (ContractError, TypeError, ValueError):
            reasons.append("IDENTITY_DECISION_WINDOW_TIMESTAMP_INVALID")

    dates = market.get("trading_dates")
    if not isinstance(dates, list) or len(dates) != 60 or dates != sorted(set(dates), reverse=True):
        reasons.append("TRADING_DATES_L60_INVALID")
    elif dates[0] != market_date:
        reasons.append("TRADING_DATES_LATEST_MISMATCH")
    else:
        calendar_days = calendar.get("trading_dates") or []
        market_index = None
        try:
            market_index = calendar_days.index(market_date)
        except (ValueError, TypeError):
            pass
        if market_index is None or market_index < 59:
            expected_l60 = []
        else:
            expected_l60 = list(reversed(calendar_days[market_index - 59:market_index + 1]))
        if dates != expected_l60:
            reasons.append("MARKET_DATES_CALENDAR_MISMATCH")

    confirmation = market.get("close_confirmation") or {}
    required_confirmation = ("exchange_close_at", "earliest_freeze_at", "capture_eligible",
                             "supplier_latest_market_data_as_of", "timing_config_version",
                             "confirmation_method")
    if any(k not in confirmation for k in required_confirmation):
        reasons.append("CLOSE_CONFIRMATION_MISSING")
    elif confirmation.get("capture_eligible") is not True:
        reasons.append("CLOSE_CAPTURE_NOT_ELIGIBLE")
    else:
        captured = parse_ts(market["market_data_captured_at"])
        if captured < parse_ts(confirmation["earliest_freeze_at"]):
            reasons.append("CAPTURE_BEFORE_EARLIEST_FREEZE")
        if parse_ts(confirmation["exchange_close_at"]) >= parse_ts(confirmation["earliest_freeze_at"]):
            reasons.append("EARLIEST_FREEZE_CONFIG_INVALID")
        if confirmation.get("supplier_latest_market_data_as_of") != market_date:
            reasons.append("SUPPLIER_LATEST_DATE_MISMATCH")
        if (parse_ts(confirmation["exchange_close_at"]).date().isoformat() != market_date
                or parse_ts(confirmation["earliest_freeze_at"]).date().isoformat() != market_date):
            reasons.append("CLOSE_CONFIRMATION_DATE_MISMATCH")

    benchmark = market.get("benchmark") or {}
    provider_code = str(benchmark.get("provider_index_code") or "").upper().split(".")[0]
    if (benchmark.get("benchmark_id") != "stable-full-a-benchmark" or
            provider_code != "000985" or
            not str(benchmark.get("definition_version", "")).startswith("sha256:") or
            benchmark.get("history_coverage") != "FULL"):
        reasons.append("BENCHMARK_STRONG_CONTRACT_MISSING")
    if benchmark.get("trading_dates") != dates:
        reasons.append("BENCHMARK_DATE_ALIGNMENT_FAILED")
    try:
        benchmark_dates, closes = normalize_benchmark_history(benchmark)
        if len(closes) != 60:
            reasons.append("BENCHMARK_CLOSE_HISTORY_INVALID")
        if benchmark_dates != dates:
            reasons.append("BENCHMARK_DATE_ALIGNMENT_FAILED")
    except ContractError as exc:
        reasons.extend(exc.reasons)
    if not all(isinstance(row, dict) for row in benchmark.get("close_history", [])):
        reasons.append("BENCHMARK_GATEWAY_1_5_ROW_SHAPE_REQUIRED")

    sectors = market.get("sectors") or []
    catalog = identity.get("catalog") or []
    sector_ids = [row.get("source_sector_id") for row in sectors]
    catalog_ids = [row.get("source_sector_id") for row in catalog]
    expected = market.get("expected_sector_count")
    returned = market.get("returned_sector_count")
    if (expected != returned or returned != len(sectors) or len(sector_ids) != len(set(sector_ids))
            or None in sector_ids):
        reasons.append("MARKET_SOURCE_ACCOUNTING_FAILED")
    if len(catalog_ids) != len(set(catalog_ids)) or set(catalog_ids) != set(sector_ids):
        reasons.append("IDENTITY_SOURCE_ACCOUNTING_FAILED")
    market_by_id = {row.get("source_sector_id"): row for row in sectors}
    identity_by_id = {row.get("source_sector_id"): row for row in catalog}
    allowed_scopes = {"PRIMARY", "REFERENCE"}
    if any(row.get("source_scope") not in allowed_scopes for row in sectors + catalog):
        reasons.append("SOURCE_SCOPE_ENUM_INVALID")
    for sid in set(sector_ids) & set(catalog_ids):
        market_row, identity_row = market_by_id[sid], identity_by_id[sid]
        if (market_row.get("source_scope") != identity_row.get("source_scope")
                or market_row.get("source_layer") != identity_row.get("source_layer")):
            reasons.append("MARKET_IDENTITY_SOURCE_CLASSIFICATION_MISMATCH")
            break
    primary_ids = {row.get("source_sector_id") for row in sectors
                   if row.get("source_scope") == "PRIMARY"}
    reference_ids = {row.get("source_sector_id") for row in sectors
                     if row.get("source_scope") == "REFERENCE"}
    declared_latest_core_limited = sorted(
        market.get("latest_core_limited_sector_ids") or [])
    computed_latest_core_limited = []
    for row in sectors:
        if row.get("source_sector_id") not in primary_ids:
            continue
        latest = (row.get("history") or [{}])[0]
        if latest.get("up_count") is None or latest.get("breadth_denominator") is None:
            if latest.get("breadth_coverage") != "MISSING_UP":
                reasons.append("PRIMARY_LATEST_CORE_LIMITATION_TYPE_INVALID")
            computed_latest_core_limited.append(row.get("source_sector_id"))
    computed_latest_core_limited.sort()
    if (computed_latest_core_limited != declared_latest_core_limited
            or len(computed_latest_core_limited) > 1
            or market.get("latest_core_coverage")
            != ("LIMITED" if computed_latest_core_limited else "FULL")):
        reasons.append("PRIMARY_LATEST_CORE_LIMITATION_ACCOUNTING_FAILED")
    for coverage_key, ids_key, row_key in (
            ("history_coverage", "history_limited_sector_ids", "history_coverage"),
            ("breadth_history_coverage", "breadth_limited_sector_ids", "breadth_history_coverage"),
            ("activity_history_coverage", "activity_limited_sector_ids", "activity_history_coverage")):
        if market.get(coverage_key) not in {"FULL", "LIMITED"}:
            reasons.append("CORE_COVERAGE_DECLARATION_INVALID")
            continue
        computed_limited = sorted(row.get("source_sector_id") for row in sectors
                                  if row.get(row_key) != "FULL")
        declared_limited = sorted(market.get(ids_key) or [])
        if computed_limited != declared_limited:
            reasons.append("LOCAL_LIMITATION_ACCOUNTING_FAILED")
        if (market.get(coverage_key) == "FULL") != (not computed_limited):
            reasons.append("LOCAL_LIMITATION_COVERAGE_STATUS_MISMATCH")
    # Recompute the supplier's limitation declarations from the frozen rows;
    # declarations cannot grant a wider permission than the actual history.
    expected_dates = market.get("trading_dates") or []
    actual_history_limited = []
    actual_breadth_limited = []
    actual_activity_limited = []
    for row in sectors:
        history = row.get("history") or []
        history_dates = [item.get("date") for item in history if isinstance(item, dict)]
        if history_dates != [day for day in expected_dates if day in set(history_dates)]:
            reasons.append("SECTOR_HISTORY_ORDER_OR_DATE_INVALID")
        missing = [day for day in expected_dates if day not in set(history_dates)]
        if sorted(row.get("missing_trading_dates") or []) != sorted(missing):
            reasons.append("SECTOR_MISSING_DATE_DECLARATION_MISMATCH")
        sid = row.get("source_sector_id")
        if missing:
            actual_history_limited.append(sid)
        if any(item.get("breadth_denominator") is None for item in history):
            actual_breadth_limited.append(sid)
        if any(item.get("amount_yi") is None for item in history):
            actual_activity_limited.append(sid)
    for actual, declared, reason in (
            (actual_history_limited, market.get("history_limited_sector_ids") or [],
             "ACTUAL_HISTORY_LIMITATION_DECLARATION_MISMATCH"),
            (actual_breadth_limited, market.get("breadth_limited_sector_ids") or [],
             "ACTUAL_BREADTH_LIMITATION_DECLARATION_MISMATCH"),
            (actual_activity_limited, market.get("activity_limited_sector_ids") or [],
             "ACTUAL_ACTIVITY_LIMITATION_DECLARATION_MISMATCH")):
        if sorted(actual) != sorted(declared):
            reasons.append(reason)
    for row in sectors:
        if row.get("source_sector_id") not in primary_ids:
            continue
        history = row.get("history") or []
        latest = history[0] if history else {}
        isolated_latest_core = row.get("source_sector_id") in computed_latest_core_limited
        if (latest.get("date") != market_date
                or not isinstance(latest.get("close"), (int, float))
                or isinstance(latest.get("close"), bool) or latest.get("close") <= 0
                or not _non_negative_number(latest.get("amount_yi"))
                or (not isolated_latest_core and not _breadth_accounting_valid(latest))):
            reasons.append("PRIMARY_LATEST_CORE_FIELD_INVALID")
            break
        identity_row = identity_by_id.get(row.get("source_sector_id")) or {}
        member_codes = identity_row.get("member_codes")
        if (not isinstance(member_codes, list)
                or len(member_codes) != len(set(member_codes))
                or identity_row.get("member_count") != len(member_codes)
                or identity_row.get("membership_hash") != content_hash(sorted(member_codes))
                or identity_row.get("membership_coverage") != "FULL"):
            reasons.append("PRIMARY_IDENTITY_MEMBERSHIP_INVALID")
            break
    declared_primary = market.get("primary_sector_count")
    if declared_primary != len(primary_ids):
        reasons.append("PRIMARY_COUNT_MISMATCH")
    if market.get("reference_sector_count") != len(reference_ids):
        reasons.append("REFERENCE_COUNT_MISMATCH")
    identity_primary = {row.get("source_sector_id") for row in catalog
                        if row.get("source_scope") == "PRIMARY"}
    identity_reference = {row.get("source_sector_id") for row in catalog
                          if row.get("source_scope") == "REFERENCE"}
    if (identity.get("catalog_count") != len(catalog)
            or identity.get("primary_sector_count") != len(identity_primary)
            or identity.get("reference_sector_count") != len(identity_reference)
            or identity.get("primary_membership_coverage") != "FULL"):
        reasons.append("IDENTITY_COUNT_OR_MEMBERSHIP_ACCOUNTING_FAILED")
    provisional = identity.get("provisional_labels") or []
    provisional_ids = [row.get("provisional_id") for row in provisional
                       if isinstance(row, dict)]
    if (len(provisional_ids) != len(provisional)
            or len(provisional_ids) != len(set(provisional_ids))
            or identity.get("provisional_label_count") != len(provisional)):
        reasons.append("PROVISIONAL_LABEL_ACCOUNTING_FAILED")
    for row in provisional:
        codes = row.get("member_codes") if isinstance(row, dict) else None
        if (not isinstance(codes, list) or len(codes) != len(set(codes))
                or row.get("member_count") != len(codes)
                or row.get("membership_hash") != content_hash(sorted(codes))):
            reasons.append("PROVISIONAL_MEMBERSHIP_INVALID")
            break
    if reasons:
        raise ContractError(reasons)
    return {
        "market_date": market_date,
        "next_decision_date": next_day,
        "next_session": next_session,
        "market_hash": market_hash,
        "identity_hash": identity_hash,
        "primary_ids": sorted(primary_ids),
    }


def _universe(market_env: dict, identity_env: dict) -> dict:
    market = market_env["payload"]
    identity = identity_env["payload"]
    rows = []
    for row in identity["catalog"]:
        rows.append({
            "source_sector_id": row["source_sector_id"],
            "source_name": row.get("source_name"),
            "source_layer": row.get("source_layer"),
            "source_scope": row.get("source_scope"),
            "accounting_status": ("PRIMARY_OBSERVATION" if row.get("source_scope") == "PRIMARY"
                                  else "REFERENCE_DIAGNOSTIC"),
        })
    for row in identity.get("provisional_labels", []):
        rows.append({
            "source_sector_id": row.get("provisional_id"),
            "source_name": row.get("source_label"),
            "source_layer": "PROVISIONAL",
            "source_scope": "PROVISIONAL",
            "accounting_status": "NO_DIRECTION",
            "reason_codes": (["MARKET_PROXY_MISSING"]
                             if row.get("market_proxy_status") != "VERIFIED" else []),
        })
    result = {
        **artifact("universe", "universe"),
        "universe_id": f"source-universe-{market['market_data_as_of']}",
        "analysis_universe_version": identity["universe_version"],
        "as_of": market["market_data_as_of"],
        "catalog_version": market["catalog_version"],
        "coverage_type": "FULL",
        "source_accounting": rows,
    }
    result["artifact_hash"] = content_hash(result)
    return result


def write_failure(root: Path, release_mode: str, market_date: str, reasons: list[str],
                  inputs: dict, occurred_at: str) -> Path:
    failure_dir = mode_root(root, release_mode) / "failures" / "close-freeze" / market_date
    failure_dir.mkdir(parents=True, exist_ok=True)
    stem = occurred_at.replace(":", "").replace("+", "_")
    path = failure_dir / f"{stem}.json"
    suffix = 1
    while path.exists():
        path = failure_dir / f"{stem}-{suffix}.json"
        suffix += 1
    data = {
        **artifact("close_freeze_failure", "failure", occurred_at),
        "session": "CLOSE_FREEZE", "release_mode": release_mode,
        "market_data_as_of": market_date, "publication_status": "FAILED",
        "reason_codes": sorted(set(reasons)), "input_hashes": inputs,
    }
    data["artifact_hash"] = content_hash(data)
    atomic_write_json(path, data)
    return path


def main():
    parser = argparse.ArgumentParser(description="V3 CLOSE_FREEZE strong-contract gate")
    parser.add_argument("--market-frame", required=True)
    parser.add_argument("--identity-frame", required=True)
    parser.add_argument("--trading-calendar", required=True)
    parser.add_argument("--theme-registry", required=True)
    parser.add_argument("--permission-matrix", required=True)
    parser.add_argument("--release-mode", choices=("INTERNAL_GATE", "SHADOW", "OFFICIAL"),
                        default="INTERNAL_GATE")
    parser.add_argument("--output-root", default=str(Path(__file__).resolve().parent.parent / "data"))
    parser.add_argument("--amendment-reason",
                        help="required for a different-input revision of the same D/mode")
    args = parser.parse_args()
    market_env, identity_env = load_json(args.market_frame), load_json(args.identity_frame)
    calendar = load_json(args.trading_calendar)
    registry, matrix = load_json(args.theme_registry), load_json(args.permission_matrix)
    created = now_iso()
    market_date = (market_env.get("payload") or {}).get("market_data_as_of", "unknown-date")
    input_hashes = {"sector_market_frame": content_hash(market_env),
                    "sector_identity": content_hash(identity_env),
                    "trading_calendar": content_hash(calendar),
                    "theme_registry": content_hash(registry),
                    "coverage_permission_matrix": content_hash(matrix)}
    try:
        if (not str(registry.get("registry_version", "")).startswith("sha256:")
                or not str(registry.get("snapshot_hash", "")).startswith("sha256:")
                or not registry.get("effective_as_of") or not isinstance(registry.get("themes"), list)
                or not isinstance(registry.get("excluded_source_sector_ids"), list)):
            raise ContractError(["THEME_REGISTRY_STRONG_CONTRACT_MISSING"])
        if registry["snapshot_hash"] != content_address_without(
                registry, "registry_version", "snapshot_hash", "previous_registry_version"):
            raise ContractError(["THEME_REGISTRY_SNAPSHOT_HASH_MISMATCH"])
        if registry["registry_version"] != content_address_without(
                registry, "registry_version"):
            raise ContractError(["THEME_REGISTRY_VERSION_CONTENT_MISMATCH"])
        if (not str(matrix.get("matrix_version", "")).startswith("sha256:")
                or not isinstance(matrix.get("entries"), list) or not matrix["entries"]):
            raise ContractError(["PERMISSION_MATRIX_STRONG_CONTRACT_MISSING"])
        if matrix["matrix_version"] != content_address_without(matrix, "matrix_version"):
            raise ContractError(["PERMISSION_MATRIX_VERSION_CONTENT_MISMATCH"])
        gate = validate_gate(market_env, identity_env, calendar)
        if registry["effective_as_of"] > gate["market_date"]:
            raise ContractError(["THEME_REGISTRY_FROM_FUTURE"])
        root = mode_root(args.output_root, args.release_mode)
        parent = root / "market" / gate["market_date"][:7] / gate["market_date"] / "close"
        parent.mkdir(parents=True, exist_ok=True)
        existing = sorted((p for p in parent.glob("v*") if p.is_dir()),
                          key=lambda p: int(p.name[1:]) if p.name[1:].isdigit() else 10**9)
        for version in existing:
            manifest_path = version / "manifest.json"
            if not manifest_path.exists():
                continue
            old = load_json(manifest_path)
            if (old.get("close_freeze_status") == "PASS"
                    and old.get("release_mode") == args.release_mode
                    and old.get("input_hashes") == input_hashes):
                print(f"CLOSE_FREEZE IDEMPOTENT｜D={gate['market_date']}｜复用 {version}")
                return
        if existing and not args.amendment_reason:
            raise ContractError(["CLOSE_FREEZE_INPUT_CONFLICT_REQUIRES_AMENDMENT"])
        universe = _universe(market_env, identity_env)
        observations = build_observations(market_env, identity_env, universe)
        target = immutable_version_dir(parent)
        staging = Path(tempfile.mkdtemp(prefix=".freeze-", dir=parent))
        try:
            atomic_write_json(staging / "sector_market_frame.json", market_env)
            atomic_write_json(staging / "sector_identity.json", identity_env)
            atomic_write_json(staging / "trading_calendar.json", calendar)
            atomic_write_json(staging / "theme_registry_ref.json", {
                "registry_version": registry["registry_version"],
                "snapshot_hash": registry["snapshot_hash"],
                "content_hash": content_hash(registry)})
            atomic_write_json(staging / "coverage_permission_matrix_ref.json", {
                "matrix_version": matrix["matrix_version"],
                "content_hash": content_hash(matrix)})
            atomic_write_json(staging / "universe.json", universe)
            atomic_write_json(staging / "observations.json", observations)
            manifest = {
                **artifact("close_freeze_manifest", "close-freeze-manifest", created),
                "session": "CLOSE_FREEZE", "release_mode": args.release_mode,
                "close_freeze_status": "PASS", "market_data_as_of": gate["market_date"],
                "market_data_captured_at": market_env["payload"]["market_data_captured_at"],
                "next_decision_date": gate["next_decision_date"],
                "next_auction_start_at": gate["next_session"]["auction_start_at"],
                "trading_calendar_ref": content_hash(calendar),
                "theme_registry_ref": content_hash(registry),
                "coverage_permission_matrix_ref": content_hash(matrix),
                "market_frame_ref": gate["market_hash"],
                "identity_frame_ref": gate["identity_hash"],
                "identity_date_semantics": identity_env["payload"].get(
                    "identity_date_semantics"),
                "identity_observed_at": identity_env["payload"].get(
                    "identity_observed_at"),
                "observations_ref": content_hash(observations),
                "input_hashes": input_hashes, "reason_codes": [],
                "query_versions": {
                    "market": market_env["payload"].get("query_version"),
                    "identity": identity_env["payload"].get("query_version"),
                },
                "amends_freeze_ref": (load_json(existing[-1] / "manifest.json").get("artifact_hash")
                                       if existing else None),
                "amendment_reason": args.amendment_reason,
            }
            manifest["artifact_hash"] = content_hash(manifest)
            atomic_write_json(staging / "manifest.json", manifest)
            staging.replace(target)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    except ContractError as exc:
        failure = write_failure(Path(args.output_root), args.release_mode, market_date,
                                exc.reasons, input_hashes, created)
        print(json.dumps({"close_freeze_status": "FAIL", "reason_codes": exc.reasons,
                          "failure_ref": str(failure)}, ensure_ascii=False))
        raise SystemExit(2)
    print(f"CLOSE_FREEZE PASS｜D={gate['market_date']}｜L60完整｜宽基对齐｜"
          f"身份互锁通过｜{target}")


if __name__ == "__main__":
    main()
