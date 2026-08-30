#!/usr/bin/env python3
"""Build auditable first-run reference dependencies for V3.

This tool deliberately does not fetch data and never invents a trading day.  It
turns explicit inputs into content-addressed reference artifacts.  A market
catalog can always be used to produce an offline SOURCE_FIRST_BOOTSTRAP mapping
proposal; the bundle is runtime-eligible only when the supplied market,
identity, and explicit calendar pass the same strong gate as CLOSE_FREEZE.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from freeze_market import validate_gate
from v3_common import (ContractError, atomic_write_json, canonical_bytes,
                       content_address_without, content_hash, envelope_payload,
                       load_json, parse_ts, require_date)


BOOTSTRAP_VERSION = "source-first-bootstrap-v1"
MATRIX_POLICY_VERSION = "coverage-permission-bootstrap-v1"
CLASSIFICATION_POLICY_VERSION = "source-classification-v1"

RECOGNIZED_PRIMARY_TYPES = {
    ("INDUSTRY", "同花顺二级行业指数"),
    ("INDUSTRY", "同花顺三级行业指数"),
}

# Only exact structural types are excluded.  Names are never keyword-filtered:
# an unfamiliar PRIMARY remains PROVISIONAL rather than disappearing.
OBVIOUSLY_INELIGIBLE_PRIMARY_LAYERS = {
    "BENCHMARK", "BROAD_MARKET", "REFERENCE", "STOCK", "OUTCOME",
}
OBVIOUSLY_INELIGIBLE_PROVIDER_TYPES = {
    "宽基指数", "规模指数", "风格指数",
}

CONCEPT_EXCLUSION_PATTERNS = {
    "TRADING_ATTRIBUTE": re.compile(
        r"^(融资融券|沪股通|深股通|ST板块|摘帽|举牌|新股与次新股|注册制次新股|科创次新股)$|"
        r"(一季报预增|中报预增|年报预增|高送转|破净股|低价股)"),
    "HOLDING_ATTRIBUTE": re.compile(
        r"(证金持股|国家大基金持股|社保.*持股|基金重仓|保险重仓|券商重仓|QFII|"
        r"参股银行|参股券商|参股保险)"),
    "STYLE_OR_PROVIDER_INDEX": re.compile(
        r"^(中字头股票|高股息精选|超级品牌|同花顺漂亮100|同花顺中特估100|"
        r"同花顺出海50|同花顺果指数|同花顺新质50|中国AI 50)$"),
    "GEOGRAPHIC_BUCKET": re.compile(
        r"(自贸区|自由贸易港|新区|京津冀一体化|长三角一体化|粤港澳大湾区|"
        r"共同富裕示范区|新疆振兴|西部大开发|海峡两岸|一带一路)$"),
    "TEMPORARY_EVENT_BUCKET": re.compile(r"(俄乌冲突概念)$"),
}


class BootstrapError(RuntimeError):
    def __init__(self, reasons: list[str], detail: str | None = None):
        self.reasons = sorted(set(reasons))
        super().__init__(detail or "; ".join(self.reasons))


def _immutable_write(path: Path, value: dict) -> None:
    """Write once; an identical rerun is idempotent, a conflict is rejected."""
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BootstrapError(["BOOTSTRAP_OUTPUT_UNREADABLE"], str(path)) from exc
        if canonical_bytes(old) != canonical_bytes(value):
            raise BootstrapError(["BOOTSTRAP_OUTPUT_CONFLICT"], str(path))
        return
    atomic_write_json(path, value)


def _market_catalog(market_env: dict) -> tuple[dict, list[dict]]:
    try:
        payload = envelope_payload(market_env, "sector_market_frame")
    except ContractError as exc:
        raise BootstrapError(exc.reasons) from exc
    required = ("frame_id", "market_data_as_of", "catalog_version",
                "coverage_type", "expected_sector_count", "returned_sector_count",
                "primary_sector_count", "reference_sector_count", "sectors")
    if any(key not in payload for key in required):
        raise BootstrapError(["MARKET_CATALOG_CONTRACT_MISSING"])
    require_date(payload.get("market_data_as_of"), "MARKET_DATE_INVALID")
    rows = payload.get("sectors")
    if payload.get("coverage_type") != "FULL" or not isinstance(rows, list):
        raise BootstrapError(["MARKET_CATALOG_NOT_FULL"])
    ids = [row.get("source_sector_id") for row in rows if isinstance(row, dict)]
    if (len(ids) != len(rows) or any(not isinstance(sid, str) or not sid for sid in ids)
            or len(ids) != len(set(ids))
            or payload.get("expected_sector_count") != len(rows)
            or payload.get("returned_sector_count") != len(rows)):
        raise BootstrapError(["MARKET_CATALOG_ACCOUNTING_INVALID"])
    scopes = [row.get("source_scope") for row in rows]
    if any(scope not in {"PRIMARY", "REFERENCE"} for scope in scopes):
        raise BootstrapError(["MARKET_CATALOG_SCOPE_INVALID"])
    if (payload.get("primary_sector_count") != scopes.count("PRIMARY")
            or payload.get("reference_sector_count") != scopes.count("REFERENCE")):
        raise BootstrapError(["MARKET_CATALOG_COUNT_MISMATCH"])
    return payload, sorted(copy.deepcopy(rows), key=lambda row: row["source_sector_id"])


def _identity_payload_if_readable(identity_env: dict | None) -> tuple[dict | None, list[str]]:
    if identity_env is None:
        return None, ["IDENTITY_NOT_SUPPLIED"]
    try:
        return envelope_payload(identity_env, "sector_identity"), []
    except ContractError as exc:
        # A legacy/weak payload may still be enumerated for an explicitly
        # untrusted offline accounting proposal.  It is never used to grant
        # STABLE lifecycle or runtime eligibility.
        raw = identity_env.get("payload")
        if isinstance(raw, dict):
            return raw, list(exc.reasons) + ["IDENTITY_PAYLOAD_DIAGNOSTIC_ONLY"]
        return None, list(exc.reasons)


def _theme_id(source_sector_id: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", source_sector_id.lower()).strip("-")
    digest = hashlib.sha256(source_sector_id.encode("utf-8")).hexdigest()[:10]
    return f"theme-source-{slug or 'id'}-{digest}"


def _concept_exclusion_flags(row: dict) -> list[str]:
    flags = []
    name = str(row.get("source_name") or "")
    member_count = row.get("member_count_reported")
    if isinstance(member_count, int) and not isinstance(member_count, bool) and member_count <= 1:
        flags.append("SINGLE_STOCK_OR_EMPTY_PSEUDO_THEME")
    for code, pattern in CONCEPT_EXCLUSION_PATTERNS.items():
        if pattern.search(name):
            flags.append(code)
    return sorted(set(flags))


def build_concept_review_packet(market_env: dict, identity_env: dict | None,
                                *, strong_identity: bool) -> dict:
    market, catalog = _market_catalog(market_env)
    identity, _ = _identity_payload_if_readable(identity_env)
    identity_by_id = {}
    stock_names = {}
    if strong_identity and identity is not None:
        identity_by_id = {row.get("source_sector_id"): row
                          for row in identity.get("catalog", [])
                          if isinstance(row, dict)}
        stock_names = {row.get("code"): row.get("name")
                       for row in identity.get("stocks", [])
                       if isinstance(row, dict) and isinstance(row.get("code"), str)}
    concepts = []
    for row in catalog:
        if row.get("source_scope") != "PRIMARY" or row.get("source_layer") != "CONCEPT":
            continue
        ident = identity_by_id.get(row["source_sector_id"], {})
        members = ident.get("member_codes")
        member_sample = ([{"code": code, "name": stock_names.get(code)}
                          for code in sorted(members)[:8]]
                         if isinstance(members, list) else [])
        flags = _concept_exclusion_flags(row)
        concepts.append({
            "source_sector_id": row["source_sector_id"],
            "source_name": row.get("source_name"),
            "provider_type": row.get("provider_type"),
            "member_count": (ident.get("member_count")
                             if strong_identity else row.get("member_count_reported")),
            "member_sample": member_sample,
            "membership_hash": (ident.get("membership_hash")
                                if strong_identity else None),
            "membership_ref": ({
                "identity_frame_ref": content_hash(identity_env),
                "source_sector_id": row["source_sector_id"],
                "membership_hash": ident.get("membership_hash"),
            } if strong_identity else None),
            "membership_evidence_status": ("STRONG_IDENTITY" if strong_identity
                                           else "UNAVAILABLE_OR_UNTRUSTED"),
            "screening_flags": flags,
            "required_status": "EXCLUDED" if flags else None,
            "default_without_review": "EXCLUDED" if flags else "PROVISIONAL",
        })
    packet = {
        "packet_version": "PENDING",
        "packet_kind": "CONCEPT_REVIEW_PACKET",
        "review_policy_version": CLASSIFICATION_POLICY_VERSION,
        "market_date": market["market_data_as_of"],
        "catalog_version": market["catalog_version"],
        "source_market_frame_ref": content_hash(market_env),
        "source_identity_frame_ref": (content_hash(identity_env)
                                      if identity_env is not None else None),
        "concept_count": len(concepts),
        "concepts": concepts,
        "decision_contract": {
            "allowed_statuses": ["STABLE", "EXCLUDED", "PROVISIONAL"],
            "exactly_one_decision_per_concept": True,
            "nonempty_reason_required": True,
            "flagged_rows_must_be_excluded": True,
        },
    }
    packet["packet_version"] = content_address_without(packet, "packet_version")
    return packet


def freeze_concept_decisions(source: dict, packet: dict) -> dict:
    if source.get("review_status") != "REVIEWED":
        raise BootstrapError(["CONCEPT_DECISIONS_NOT_EXPLICITLY_REVIEWED"])
    if not isinstance(source.get("reviewed_by"), str) or not source["reviewed_by"]:
        raise BootstrapError(["CONCEPT_DECISIONS_REVIEWER_MISSING"])
    try:
        parse_ts(source.get("reviewed_at"))
    except ContractError as exc:
        raise BootstrapError(["CONCEPT_DECISIONS_REVIEW_TIME_INVALID"]) from exc
    if source.get("review_packet_ref") != packet["packet_version"]:
        raise BootstrapError(["CONCEPT_DECISIONS_PACKET_MISMATCH"])
    if (source.get("market_date") != packet["market_date"]
            or source.get("catalog_version") != packet["catalog_version"]):
        raise BootstrapError(["CONCEPT_DECISIONS_CATALOG_MISMATCH"])
    rows = source.get("decisions")
    if not isinstance(rows, list):
        raise BootstrapError(["CONCEPT_DECISIONS_MISSING"])
    packet_by_id = {row["source_sector_id"]: row for row in packet["concepts"]}
    decision_by_id = {}
    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            raise BootstrapError(["CONCEPT_DECISION_ROW_INVALID"])
        sid = row.get("source_sector_id")
        status = row.get("status")
        reason = row.get("reason")
        if (not isinstance(sid, str) or not sid or sid in decision_by_id
                or status not in {"STABLE", "EXCLUDED", "PROVISIONAL"}
                or not isinstance(reason, str) or not reason.strip()):
            raise BootstrapError(["CONCEPT_DECISION_ROW_INVALID"], str(sid))
        if sid not in packet_by_id:
            raise BootstrapError(["CONCEPT_DECISION_UNKNOWN_SOURCE"], sid)
        required = packet_by_id[sid].get("required_status")
        if required is not None and status != required:
            raise BootstrapError(["INELIGIBLE_CONCEPT_MUST_BE_EXCLUDED"], sid)
        normalized_row = {
            "source_sector_id": sid,
            "status": status,
            "reason": reason.strip(),
            "reason_codes": sorted(set(row.get("reason_codes") or [])),
        }
        decision_by_id[sid] = normalized_row
        normalized.append(normalized_row)
    missing = sorted(set(packet_by_id) - set(decision_by_id))
    if missing:
        raise BootstrapError(["CONCEPT_DECISIONS_INCOMPLETE"], ",".join(missing[:5]))
    frozen = {
        "decision_version": "PENDING",
        "decision_kind": "SOURCE_FIRST_CONCEPT_DECISIONS_V1",
        "review_status": "REVIEWED",
        "reviewed_by": source["reviewed_by"],
        "reviewed_at": source["reviewed_at"],
        "review_packet_ref": packet["packet_version"],
        "market_date": packet["market_date"],
        "catalog_version": packet["catalog_version"],
        "decision_count": len(normalized),
        "decisions": sorted(normalized, key=lambda item: item["source_sector_id"]),
        "source_input_ref": content_hash(source),
    }
    frozen["decision_version"] = content_address_without(frozen, "decision_version")
    declared = source.get("decision_version")
    if declared not in (None, "PENDING", frozen["decision_version"]):
        raise BootstrapError(["CONCEPT_DECISIONS_CONTENT_ADDRESS_MISMATCH"])
    return frozen


def _classify_primary(row: dict, strong_identity: bool) -> tuple[str, list[str]]:
    layer = row.get("source_layer")
    provider_type = row.get("provider_type")
    if (layer in OBVIOUSLY_INELIGIBLE_PRIMARY_LAYERS
            or provider_type in OBVIOUSLY_INELIGIBLE_PROVIDER_TYPES):
        return "EXCLUDED", ["EXACT_STRUCTURAL_TYPE_NOT_SECTOR_THEME"]
    if not strong_identity:
        return "PROVISIONAL", ["IDENTITY_NOT_STRONGLY_INTERLOCKED"]
    if (layer, provider_type) in RECOGNIZED_PRIMARY_TYPES and row.get("source_name"):
        return "STABLE", ["RECOGNIZED_PRIMARY_SOURCE_TYPE"]
    return "PROVISIONAL", ["UNRECOGNIZED_SOURCE_TYPE_REQUIRES_REVIEW"]


def build_registry(market_env: dict, identity_env: dict | None,
                   *, strong_identity: bool, strong_gate_reasons: list[str],
                   concept_packet: dict, concept_decisions: dict | None) -> dict:
    market, catalog = _market_catalog(market_env)
    identity, identity_read_errors = _identity_payload_if_readable(identity_env)
    themes: list[dict] = []
    excluded: list[str] = []
    accounting: list[dict] = []
    mapped_primary: set[str] = set()
    concept_decision_by_id = {
        row["source_sector_id"]: row for row in (concept_decisions or {}).get("decisions", [])
    }
    packet_by_id = {row["source_sector_id"]: row for row in concept_packet["concepts"]}

    for row in catalog:
        sid = row["source_sector_id"]
        scope = row.get("source_scope")
        base = {
            "source_sector_id": sid,
            "source_name": row.get("source_name"),
            "source_scope": scope,
            "source_layer": row.get("source_layer"),
            "provider_type": row.get("provider_type"),
        }
        if scope == "REFERENCE":
            accounting.append({**base, "accounting_status": "REFERENCE_DIAGNOSTIC",
                               "theme_id": None,
                               "reason_codes": ["REFERENCE_SCOPE_NOT_DIRECTIONAL"]})
            continue
        if row.get("source_layer") == "CONCEPT":
            decision = concept_decision_by_id.get(sid)
            flags = (packet_by_id.get(sid) or {}).get("screening_flags") or []
            if flags:
                lifecycle, reasons = "EXCLUDED", ["DETERMINISTIC_CONCEPT_EXCLUSION"] + flags
            elif decision is None:
                lifecycle, reasons = "PROVISIONAL", ["CONCEPT_REVIEW_REQUIRED"]
            elif decision["status"] == "EXCLUDED":
                lifecycle, reasons = "EXCLUDED", ["REVIEWED_CONCEPT_EXCLUSION"] + decision["reason_codes"]
            elif decision["status"] == "PROVISIONAL":
                lifecycle, reasons = "PROVISIONAL", ["REVIEWED_CONCEPT_REMAINS_PROVISIONAL"] + decision["reason_codes"]
            elif strong_identity:
                lifecycle, reasons = "STABLE", ["EXPLICITLY_REVIEWED_CONCEPT"] + decision["reason_codes"]
            else:
                lifecycle, reasons = "PROVISIONAL", ["IDENTITY_NOT_STRONGLY_INTERLOCKED"]
        else:
            lifecycle, reasons = _classify_primary(row, strong_identity)
        if lifecycle == "EXCLUDED":
            excluded.append(sid)
            accounting.append({**base, "accounting_status": "EXCLUDED_PRIMARY",
                               "theme_id": None, "reason_codes": reasons})
            continue
        tid = _theme_id(sid)
        mapped_primary.add(sid)
        themes.append({
            "theme_id": tid,
            "display_name": row.get("source_name") or sid,
            "universe_layer": ("INDUSTRY" if row.get("source_layer") == "INDUSTRY"
                               else "THEME"),
            "lifecycle_status": lifecycle,
            "market_proxy_source_id": sid,
            "prior_state": "FIRST_OBSERVATION",
            "source_bindings": [{
                "source_kind": "SOURCE_SECTOR",
                "source_id": sid,
                "valid_from": market["market_data_as_of"],
                "valid_to": None,
            }],
            "bootstrap_reason_codes": reasons,
        })
        accounting.append({**base,
                           "accounting_status": f"MAPPED_{lifecycle}",
                           "theme_id": tid, "reason_codes": reasons})

    primary_ids = {row["source_sector_id"] for row in catalog
                   if row.get("source_scope") == "PRIMARY"}
    if mapped_primary | set(excluded) != primary_ids or mapped_primary & set(excluded):
        raise BootstrapError(["PRIMARY_SOURCE_ACCOUNTING_NOT_CLOSED"])

    provisional_accounting: list[dict] = []
    if identity is not None:
        provisional = identity.get("provisional_labels")
        if not isinstance(provisional, list):
            provisional = []
            identity_read_errors.append("IDENTITY_PROVISIONAL_LABELS_INVALID")
        seen: set[str] = set()
        for row in sorted((item for item in provisional if isinstance(item, dict)),
                          key=lambda item: str(item.get("provisional_id"))):
            pid = row.get("provisional_id")
            if not isinstance(pid, str) or not pid or pid in seen:
                identity_read_errors.append("IDENTITY_PROVISIONAL_ACCOUNTING_INVALID")
                continue
            seen.add(pid)
            provisional_accounting.append({
                "provisional_id": pid,
                "source_label": row.get("source_label"),
                "accounting_status": ("NO_DIRECTION_UNMAPPED" if strong_identity
                                      else "UNTRUSTED_NOT_ADMITTED"),
                "reason_codes": (["MARKET_PROXY_MAPPING_REQUIRED"] if strong_identity else
                                 ["IDENTITY_NOT_STRONGLY_INTERLOCKED"]),
            })

    registry_ready = strong_identity and concept_decisions is not None
    approval = "RUNTIME_ELIGIBLE" if registry_ready else "PROPOSAL_ONLY"
    registry: dict[str, Any] = {
        "registry_version": "PENDING",
        "snapshot_hash": "PENDING",
        "effective_as_of": market["market_data_as_of"],
        "bootstrap_method": BOOTSTRAP_VERSION,
        "classification_policy_version": CLASSIFICATION_POLICY_VERSION,
        "approval_status": approval,
        "source_market_frame_ref": content_hash(market_env),
        "source_identity_frame_ref": (content_hash(identity_env)
                                      if identity_env is not None else None),
        "concept_review_packet_ref": concept_packet["packet_version"],
        "concept_decisions_ref": ((concept_decisions or {}).get("decision_version")),
        "themes": sorted(themes, key=lambda item: item["theme_id"]),
        "excluded_source_sector_ids": sorted(excluded),
        "source_accounting": accounting,
        "provisional_label_accounting": provisional_accounting,
        "accounting_summary": {
            "catalog_count": len(catalog),
            "primary_count": len(primary_ids),
            "reference_count": len(catalog) - len(primary_ids),
            "mapped_stable_count": sum(t["lifecycle_status"] == "STABLE" for t in themes),
            "mapped_provisional_count": sum(t["lifecycle_status"] == "PROVISIONAL"
                                             for t in themes),
            "excluded_primary_count": len(excluded),
            "accounted_primary_count": len(mapped_primary) + len(excluded),
            "provisional_label_count": len(provisional_accounting),
        },
        "runtime_blocking_reasons": sorted(set(
            ([] if registry_ready else strong_gate_reasons + identity_read_errors))),
    }
    registry["snapshot_hash"] = content_address_without(
        registry, "registry_version", "snapshot_hash", "previous_registry_version")
    registry["registry_version"] = content_address_without(registry, "registry_version")
    return registry


def _entry(scope: str, field: str, coverage: str, health: str, effect: str,
           signals: list[str], stages: list[str], risks: list[str], reason: str,
           stock_allowed: bool = False) -> dict:
    return {
        "subject_scope": scope,
        "field_or_window": field,
        "coverage_type": coverage,
        "data_health": health,
        "permission_effect": effect,
        "allowed_sensing_signals": signals,
        "allowed_opportunity_stages": stages,
        "allowed_risk_levels": risks,
        "stock_selection_allowed": stock_allowed,
        "reason_code": reason,
    }


def build_permission_matrix(effective_as_of: str) -> dict:
    require_date(effective_as_of, "MATRIX_EFFECTIVE_DATE_INVALID")
    none: list[str] = []
    signals = ["NONE", "WATCH", "CANDIDATE"]
    stages = ["FORMING", "ACTIVE", "MATURE", "INVALID"]
    risks = ["LOW", "CAUTION", "HIGH", "EXIT"]
    entries = [
        _entry("GLOBAL", "SOURCE_CATALOG", "FULL", "SUFFICIENT", "KEEP",
               signals, stages, risks, "FULL_SOURCE_CATALOG_REQUIRED"),
        _entry("GLOBAL", "SOURCE_CATALOG", "TOP_N", "INVALID", "CLOSE",
               none, none, none, "TOP_N_CATALOG_FORBIDDEN"),
        _entry("GLOBAL", "SOURCE_CATALOG", "MISSING", "INVALID", "CLOSE",
               none, none, none, "SOURCE_CATALOG_MISSING"),
        _entry("GLOBAL", "SOURCE_CATALOG", "FAILED", "INVALID", "CLOSE",
               none, none, none, "SOURCE_CATALOG_FAILED"),
        _entry("THEME", "CORE_OBSERVATIONS", "FULL", "SUFFICIENT", "KEEP",
               signals, stages, risks, "CORE_OBSERVATIONS_SUFFICIENT"),
        # A localized old-history limitation does not erase reliable current
        # multi-dimensional structure, so sensing may still select CANDIDATE.
        # Positive formal status is capped at FORMING; verified risk may still
        # escalate because risk evidence has its own independent case gate.
        _entry("THEME", "CORE_OBSERVATIONS", "FULL", "LIMITED", "CAP",
               signals, ["FORMING", "INVALID"], risks,
               "LOCAL_HISTORY_LIMIT_CAPS_POSITIVE_STAGE"),
        _entry("THEME", "CORE_OBSERVATIONS", "FULL", "INVALID", "CLOSE",
               none, none, none, "CORE_OBSERVATIONS_INVALID"),
    ]
    for field in ("PRICE_AND_BENCHMARK", "BREADTH", "ATTENTION"):
        entries.extend([
            _entry("THEME", field, "FULL", "SUFFICIENT", "KEEP",
                   signals, stages, risks, f"{field}_SUFFICIENT"),
            _entry("THEME", field, "FULL", "LIMITED", "CAP",
                   ["NONE", "WATCH"], ["FORMING", "INVALID"], risks,
                   f"{field}_LIMITED"),
            _entry("THEME", field, "FULL", "INVALID", "CLOSE",
                   none, none, none, f"{field}_INVALID"),
        ])
    entries.extend([
        _entry("THEME", "IDENTITY_AND_PROXY", "FULL", "SUFFICIENT", "KEEP",
               signals, stages, risks, "IDENTITY_AND_PROXY_VERIFIED"),
        _entry("THEME", "IDENTITY_AND_PROXY", "FULL", "LIMITED", "CAP",
               ["NONE", "WATCH"], none, none, "PROVISIONAL_MAPPING_WATCH_ONLY"),
        _entry("THEME", "IDENTITY_AND_PROXY", "MISSING", "INVALID", "CLOSE",
               none, none, none, "MARKET_PROXY_MISSING_NO_DIRECTION"),
        _entry("STOCK", "STOCK_MARKET_FRAME", "MISSING", "INVALID", "CLOSE",
               none, none, none, "STOCK_MODULE_DISABLED_IN_BOOTSTRAP"),
        _entry("OUTCOME", "EVALUATION_WINDOW", "MISSING", "INVALID", "CLOSE",
               none, none, none, "OUTCOME_MODULE_DISABLED_IN_BOOTSTRAP"),
    ])
    keys = [(e["subject_scope"], e["field_or_window"], e["coverage_type"],
             e["data_health"]) for e in entries]
    if len(keys) != len(set(keys)):
        raise BootstrapError(["PERMISSION_MATRIX_RULE_DUPLICATE"])
    matrix = {
        "matrix_version": "PENDING",
        "policy_version": MATRIX_POLICY_VERSION,
        "effective_as_of": effective_as_of,
        "entries": entries,
    }
    matrix["matrix_version"] = content_address_without(matrix, "matrix_version")
    return matrix


def _clock(value: Any, reason: str) -> time:
    if not isinstance(value, str):
        raise BootstrapError([reason])
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise BootstrapError([reason]) from exc
    if parsed.tzinfo is not None:
        raise BootstrapError([reason])
    return parsed


def build_runtime_config(source: dict) -> dict:
    if source.get("source_kind") not in {"USER_EXPLICIT", "DEPLOYMENT_CONFIG"}:
        raise BootstrapError(["RUNTIME_SOURCE_KIND_NOT_EXPLICIT"])
    if not isinstance(source.get("source_name"), str) or not source["source_name"]:
        raise BootstrapError(["RUNTIME_SOURCE_NAME_MISSING"])
    if not isinstance(source.get("source_version"), str) or not source["source_version"]:
        raise BootstrapError(["RUNTIME_SOURCE_VERSION_MISSING"])
    if source.get("timezone") != "Asia/Shanghai":
        raise BootstrapError(["RUNTIME_TIMEZONE_INVALID"])
    window = source.get("official_run_window")
    if not isinstance(window, dict):
        raise BootstrapError(["RUNTIME_WINDOW_MISSING"])
    start = _clock(window.get("start_at"), "RUNTIME_START_INVALID")
    end = _clock(window.get("end_at"), "RUNTIME_END_INVALID")
    cutoff = _clock(source.get("information_cutoff_at"), "RUNTIME_CUTOFF_INVALID")
    auction = _clock(source.get("auction_start_at"), "RUNTIME_AUCTION_INVALID")
    if not start < end < auction or cutoff >= auction:
        raise BootstrapError(["RUNTIME_WINDOW_ORDER_INVALID"])
    config = {
        "config_version": "PENDING",
        "timezone": "Asia/Shanghai",
        "official_run_window": {
            "start_at": window["start_at"], "end_at": window["end_at"],
        },
        "information_cutoff_at": source["information_cutoff_at"],
        "auction_start_at": source["auction_start_at"],
        "provenance": {
            "source_kind": source["source_kind"],
            "source_name": source["source_name"],
            "source_version": source["source_version"],
            "input_ref": content_hash(source),
        },
    }
    config["config_version"] = content_address_without(config, "config_version")
    return config


def build_calendar(source: dict, market_env: dict) -> dict:
    market, _ = _market_catalog(market_env)
    if source.get("source_kind") not in {
            "USER_EXPLICIT", "AUTHORITATIVE", "STRUCTURED_PROVIDER"}:
        raise BootstrapError(["CALENDAR_SOURCE_KIND_NOT_EXPLICIT"])
    if not isinstance(source.get("source_name"), str) or not source["source_name"]:
        raise BootstrapError(["CALENDAR_SOURCE_NAME_MISSING"])
    if not isinstance(source.get("source_version"), str) or not source["source_version"]:
        raise BootstrapError(["CALENDAR_SOURCE_VERSION_MISSING"])
    if source.get("source_kind") in {"AUTHORITATIVE", "STRUCTURED_PROVIDER"}:
        if not isinstance(source.get("source_uri"), str) or not source["source_uri"]:
            raise BootstrapError(["PROVIDER_CALENDAR_URI_MISSING"])
        try:
            parse_ts(source.get("captured_at"))
        except ContractError as exc:
            raise BootstrapError(["PROVIDER_CALENDAR_CAPTURE_TIME_INVALID"]) from exc
    if source.get("market") != "CN_A" or source.get("timezone") != "Asia/Shanghai":
        raise BootstrapError(["CALENDAR_MARKET_OR_TIMEZONE_INVALID"])
    days = source.get("trading_dates")
    if not isinstance(days, list) or len(days) < 2 or days != sorted(set(days)):
        raise BootstrapError(["CALENDAR_TRADING_DATES_NOT_EXPLICIT_ORDERED_UNIQUE"])
    for day in days:
        require_date(day, "CALENDAR_TRADING_DATE_INVALID")
    market_days = market.get("trading_dates")
    if not isinstance(market_days, list) or len(market_days) != 60:
        raise BootstrapError(["MARKET_L60_DATES_REQUIRED_FOR_CALENDAR"])
    d = market["market_data_as_of"]
    if d not in days:
        raise BootstrapError(["MARKET_DATE_MISSING_FROM_CALENDAR"])
    idx = days.index(d)
    if idx < 59 or list(reversed(days[idx - 59:idx + 1])) != market_days:
        raise BootstrapError(["CALENDAR_DOES_NOT_EXPLICITLY_COVER_MARKET_L60"])
    if idx + 1 >= len(days):
        raise BootstrapError(["NEXT_TRADING_DAY_NOT_EXPLICIT"])
    next_day = days[idx + 1]
    sessions = source.get("sessions")
    if not isinstance(sessions, dict):
        raise BootstrapError(["CALENDAR_SESSIONS_NOT_EXPLICIT"])
    normalized_sessions: dict[str, dict] = {}
    for day, row in sorted(sessions.items()):
        if day not in days or not isinstance(row, dict):
            raise BootstrapError(["CALENDAR_SESSION_UNKNOWN_DATE"])
        auction = row.get("auction_start_at")
        try:
            ts = parse_ts(auction)
        except ContractError as exc:
            raise BootstrapError(["CALENDAR_AUCTION_TIMESTAMP_INVALID"], day) from exc
        local = ts.astimezone(ZoneInfo("Asia/Shanghai"))
        if local.date().isoformat() != day or local.utcoffset().total_seconds() != 8 * 3600:
            raise BootstrapError(["CALENDAR_AUCTION_TIMESTAMP_INVALID"], day)
        normalized_sessions[day] = {"auction_start_at": local.isoformat()}
    if d not in normalized_sessions or next_day not in normalized_sessions:
        raise BootstrapError(["CURRENT_AND_NEXT_SESSION_REQUIRED"])
    provenance = {
        "source_kind": source["source_kind"],
        "source_name": source["source_name"],
        "source_version": source["source_version"],
        "input_ref": content_hash(source),
    }
    for optional in ("source_uri", "captured_at"):
        if optional in source:
            provenance[optional] = source[optional]
    calendar = {
        "calendar_version": "PENDING",
        "timezone": "Asia/Shanghai",
        "market": "CN_A",
        "trading_dates": list(days),
        "sessions": normalized_sessions,
        "provenance": provenance,
    }
    calendar["calendar_version"] = content_address_without(calendar, "calendar_version")
    return calendar


def _identity_diagnostics(market: dict, identity: dict | None) -> list[str]:
    if identity is None:
        return ["IDENTITY_NOT_SUPPLIED"]
    market_payload = market.get("payload") if isinstance(market, dict) else None
    identity_payload = identity.get("payload") if isinstance(identity, dict) else None
    if not isinstance(market_payload, dict) or not isinstance(identity_payload, dict):
        return ["IDENTITY_PAYLOAD_INVALID"]
    reasons = []
    if identity.get("schema_version") != "1.5":
        reasons.append("IDENTITY_GATEWAY_SCHEMA_VERSION_UNSUPPORTED")
    if identity.get("data_type") != "sector_identity":
        reasons.append("IDENTITY_DATA_TYPE_INVALID")
    if identity_payload.get("requested_as_of") != market_payload.get("market_data_as_of"):
        reasons.append("IDENTITY_MARKET_DATE_MISMATCH")
    if identity_payload.get("identity_date_semantics") not in {
            "CLOSE_FREEZE_CURRENT_RELATION", "DECISION_WINDOW_CURRENT_RELATION"}:
        reasons.append("IDENTITY_DATE_SEMANTICS_INVALID")
    if identity_payload.get("catalog_version") != market_payload.get("catalog_version"):
        reasons.append("CATALOG_VERSION_MISMATCH")
    if identity_payload.get("verified_close_frame_id") != market_payload.get("frame_id"):
        reasons.append("CLOSE_FRAME_ID_INTERLOCK_FAILED")
    if identity_payload.get("verified_close_frame_hash") != content_hash(market):
        reasons.append("CLOSE_FRAME_HASH_INTERLOCK_FAILED")
    return reasons


def _strong_gate(market: dict, identity: dict | None,
                 calendar: dict) -> tuple[bool, list[str]]:
    if identity is None:
        return False, ["IDENTITY_NOT_SUPPLIED"]
    try:
        validate_gate(market, identity, calendar)
    except ContractError as exc:
        return False, sorted(set(list(exc.reasons) +
                                 _identity_diagnostics(market, identity)))
    return True, []


def build_bundle(market_env: dict, identity_env: dict | None,
                 calendar_source: dict, runtime_source: dict,
                 concept_decision_source: dict | None = None) -> dict[str, dict]:
    market, _ = _market_catalog(market_env)
    calendar = build_calendar(calendar_source, market_env)
    runtime = build_runtime_config(runtime_source)
    strong_core, reasons = _strong_gate(market_env, identity_env, calendar)
    concept_packet = build_concept_review_packet(
        market_env, identity_env, strong_identity=strong_core)
    concept_decisions = None
    if concept_decision_source is not None:
        concept_decisions = freeze_concept_decisions(concept_decision_source,
                                                     concept_packet)
    else:
        reasons = sorted(set(reasons + ["CONCEPT_DECISIONS_NOT_SUPPLIED"]))
    ready = strong_core and concept_decisions is not None
    registry = build_registry(market_env, identity_env,
                              strong_identity=strong_core,
                              strong_gate_reasons=reasons,
                              concept_packet=concept_packet,
                              concept_decisions=concept_decisions)
    matrix = build_permission_matrix(market["market_data_as_of"])
    status = "READY" if ready else "PROPOSAL_ONLY"
    refs = {
        "theme_registry": content_hash(registry),
        "coverage_permission_matrix": content_hash(matrix),
        "runtime_config": content_hash(runtime),
        "trading_calendar": content_hash(calendar),
        "concept_review_packet": content_hash(concept_packet),
        "concept_decisions": (content_hash(concept_decisions)
                              if concept_decisions is not None else None),
    }
    manifest = {
        "bootstrap_version": BOOTSTRAP_VERSION,
        "bundle_version": "PENDING",
        "status": status,
        "runtime_eligible": ready,
        "market_date": market["market_data_as_of"],
        "input_refs": {
            "sector_market_frame": content_hash(market_env),
            "sector_identity": content_hash(identity_env) if identity_env is not None else None,
            "calendar_source": content_hash(calendar_source),
            "runtime_source": content_hash(runtime_source),
            "concept_decision_source": (content_hash(concept_decision_source)
                                        if concept_decision_source is not None else None),
        },
        "output_refs": refs,
        "source_accounting": registry["accounting_summary"],
        "blocking_reasons": sorted(set(reasons)),
        "safety_note": ("May enter CLOSE_FREEZE dependency gate" if ready else
                        "Offline mapping proposal only; must not be presented as a successful run"),
    }
    manifest["bundle_version"] = content_address_without(manifest, "bundle_version")
    result = {"manifest": manifest, "theme_registry": registry,
              "coverage_permission_matrix": matrix, "runtime_config": runtime,
              "trading_calendar": calendar,
              "concept_review_packet": concept_packet}
    if concept_decisions is not None:
        result["concept_decisions"] = concept_decisions
    return result


def write_bundle(root: Path, bundle: dict[str, dict]) -> Path:
    manifest = bundle["manifest"]
    bucket = "ready" if manifest["runtime_eligible"] else "proposals"
    version = manifest["bundle_version"].split(":", 1)[1]
    target = root / bucket / manifest["market_date"] / version
    target.mkdir(parents=True, exist_ok=True)
    names = {
        "manifest": "bootstrap-manifest.json",
        "theme_registry": "theme-registry.json",
        "coverage_permission_matrix": "coverage-permission-matrix.json",
        "runtime_config": "runtime-config.json",
        "trading_calendar": "trading-calendar.json",
        "concept_review_packet": "concept-review-packet.json",
        "concept_decisions": "concept-decisions.json",
    }
    for key, filename in names.items():
        if key in bundle:
            _immutable_write(target / filename, bundle[key])
    return target


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build content-addressed first-run reference dependencies")
    parser.add_argument("--market-frame", required=True)
    parser.add_argument("--identity-frame",
                        help="optional; absent/weak identity yields PROPOSAL_ONLY")
    parser.add_argument("--calendar-source", required=True,
                        help="explicit/authoritative dates and sessions; never inferred")
    parser.add_argument("--runtime-source", required=True,
                        help="explicit runtime policy input")
    parser.add_argument("--concept-decisions",
                        help="reviewed one-row-per-PRIMARY-CONCEPT decisions")
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args()
    try:
        market = load_json(args.market_frame)
        identity = load_json(args.identity_frame) if args.identity_frame else None
        calendar_source = load_json(args.calendar_source)
        runtime_source = load_json(args.runtime_source)
        concept_decisions = (load_json(args.concept_decisions)
                             if args.concept_decisions else None)
        bundle = build_bundle(market, identity, calendar_source, runtime_source,
                              concept_decisions)
        target = write_bundle(Path(args.output_root), bundle)
    except (BootstrapError, ContractError) as exc:
        reasons = getattr(exc, "reasons", ["BOOTSTRAP_FAILED"])
        print("BOOTSTRAP FAIL｜" + ",".join(sorted(set(reasons))), file=sys.stderr)
        return 1
    status = bundle["manifest"]["status"]
    print(f"BOOTSTRAP {status}｜D={bundle['manifest']['market_date']}｜{target}")
    if status != "READY":
        print("BLOCKING｜" + ",".join(bundle["manifest"]["blocking_reasons"]),
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
