#!/usr/bin/env python3
"""Build deterministic source-level observations from frozen gateway envelopes."""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path

from v3_common import (ContractError, artifact, atomic_write_json, content_hash,
                       envelope_payload, load_json, normalize_benchmark_history,
                       provenance_ref)

WINDOWS = (1, 3, 5, 10, 20)


def _return(closes_desc: list[float], window: int):
    if (len(closes_desc) <= window or any(v is None for v in closes_desc[:window + 1])
            or closes_desc[window] == 0):
        return None
    return closes_desc[0] / closes_desc[window] - 1.0


def _daily_returns(closes_desc: list[float], count: int) -> list[float] | None:
    if len(closes_desc) <= count or any(v is None for v in closes_desc[:count + 1]):
        return None
    # chronological D-count+1 ... D
    values = list(reversed(closes_desc[:count + 1]))
    return [values[i] / values[i - 1] - 1.0 for i in range(1, len(values))]


def _percentile(value, values):
    if value is None:
        return None
    valid = [v for v in values if v is not None]
    if not valid:
        return None
    return (sum(v < value for v in valid) + 0.5 * sum(v == value for v in valid)) / len(valid)


def _activity(history_desc: list[dict], trading_dates_desc: list[str]):
    by_date = {row["date"]: row for row in history_desc}
    chronological_dates = list(reversed(trading_dates_desc))
    ratios = {}
    audits = []
    for index, day in enumerate(chronological_dates):
        row = by_date.get(day) or {}
        prior_dates = chronological_dates[max(0, index - 20):index]
        prior = [(by_date.get(d) or {}).get("amount_yi") for d in prior_dates]
        valid_prior = [v for v in prior if v is not None]
        amount = row.get("amount_yi")
        baseline = statistics.median(valid_prior) if valid_prior else None
        ratio = amount / baseline if amount is not None and baseline not in (None, 0) else None
        ratios[day] = ratio
        audits.append({"date": day, "amount_yi": amount,
                       "baseline_20d": baseline, "baseline_valid_days": len(valid_prior),
                       "activity_ratio": ratio})
    latest_amount = (by_date.get(trading_dates_desc[0]) or {}).get("amount_yi")
    all_amounts = [r.get("amount_yi") for r in history_desc if r.get("amount_yi") is not None]
    return {
        "activity_ratio_path_5d": [ratios.get(d) for d in reversed(trading_dates_desc[:5])],
        "amount_percentile_60d": _percentile(latest_amount, all_amounts),
        "valid_day_counts": {
            "20D_BASELINE": audits[-1]["baseline_valid_days"] if audits else 0,
            "60D_HISTORY": len(all_amounts),
        },
        "audit": audits,
    }


def _breadth(history_desc: list[dict], trading_dates_desc: list[str]):
    by_date = {row["date"]: row for row in history_desc}
    audit = []
    for day in trading_dates_desc:
        row = by_date.get(day) or {}
        den = row.get("breadth_denominator")
        up, down = row.get("up_count"), row.get("down_count")
        non_up = row.get("non_up_count")
        if non_up is None and down is not None and row.get("flat_count") is not None:
            non_up = down + row["flat_count"]
        coverage = row.get("breadth_coverage")
        flat = row.get("flat_count")
        counts = (up, down, flat, non_up, den)
        non_negative = all(value is None or (
            isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0)
                           for value in counts)
        ratio_valid = (non_negative and den is not None and den > 0 and up is not None
                       and non_up is not None and up + non_up == den)
        # `non_up` contains both down and flat.  It is valid for an up-ratio
        # denominator check, but must never be relabelled as `down` when computing
        # the signed breadth balance required by the V3 contract.
        if coverage == "FULL":
            balance_valid = (non_negative and den is not None and den > 0
                             and up is not None and down is not None and flat is not None
                             and up + down + flat == den)
        elif coverage == "UP_DOWN_ONLY":
            balance_valid = (non_negative and den is not None and den > 0
                             and up is not None and down is not None and up + down == den)
        else:
            # In particular, UP_VS_NON_UP_ONLY deliberately has no signed
            # direction: non-up includes flats and cannot stand in for down.
            balance_valid = False
        audit.append({
            "date": day, "up_count": up, "down_count": down,
            "flat_count": flat, "non_up_count": non_up,
            "denominator": den,
            "up_ratio": up / den if ratio_valid else None,
            "down_ratio": down / den if balance_valid else None,
            "balance": (up - down) / den if balance_valid else None,
            "ratio_accounting_valid": ratio_valid,
            "balance_accounting_valid": balance_valid,
            "coverage": coverage,
        })
    valid20 = [r["balance"] for r in audit[:20] if r["balance"] is not None]
    return {
        "up_ratio_today": audit[0]["up_ratio"] if audit else None,
        "down_ratio_today": audit[0]["down_ratio"] if audit else None,
        "balance_path_5d": [r["balance"] for r in reversed(audit[:5])],
        "balance_average_20d": statistics.fmean(valid20) if valid20 else None,
        "valid_day_counts": {"5D": sum(r["balance"] is not None for r in audit[:5]),
                             "20D": len(valid20)},
        "audit": audit,
    }


def _dominance(path):
    if not path:
        return None
    denominator = sum(abs(x) for x in path)
    return abs(path[-1]) / denominator if denominator else None


def build(market_env: dict, identity_env: dict, universe: dict | None = None) -> dict:
    market = envelope_payload(market_env, "sector_market_frame")
    identity = envelope_payload(identity_env, "sector_identity")
    market_hash, identity_hash = content_hash(market_env), content_hash(identity_env)
    benchmark = market.get("benchmark") or {}
    try:
        benchmark_dates, benchmark_closes = normalize_benchmark_history(benchmark)
    except ContractError:
        raise
    if benchmark and benchmark_dates == market.get("trading_dates"):
        # Spec permits arrays newest-first only when aligned with sector history.
        bench_dates = benchmark_dates
    else:
        bench_dates = []
    if not bench_dates:
        raise ContractError(["BENCHMARK_STRONG_CONTRACT_MISSING"])

    market_by_id = {row.get("source_sector_id"): row for row in market.get("sectors", [])}
    identity_by_id = {row.get("source_sector_id"): row for row in identity.get("catalog", [])}
    observations = []
    for sid in sorted(market_by_id):
        sector = market_by_id[sid]
        ident = identity_by_id.get(sid)
        if ident is None:
            raise ContractError(["SOURCE_IDENTITY_MISSING"], sid)
        scope = sector.get("source_scope")
        history = sector.get("history") or []
        if scope != "PRIMARY":
            observations.append({
                "source_sector_id": sid, "source_scope": scope,
                "source_layer": sector.get("source_layer"),
                "source_name": sector.get("source_name"),
                "direction_permission": "REFERENCE_ONLY", "metrics": None,
                "limitations": ["REFERENCE_NOT_FORMAL_SCAN_OBJECT"],
                "provenance": [provenance_ref(market_hash,
                    f"/payload/sectors/{market['sectors'].index(sector)}",
                    market.get("capture_completed_at") or market.get("market_data_captured_at"))],
            })
            continue
        history_dates = [r.get("date") for r in history]
        trading_dates = market["trading_dates"]
        if (not history_dates or history_dates[0] != trading_dates[0]
                or len(history_dates) != len(set(history_dates))
                or any(day not in trading_dates for day in history_dates)
                or history_dates != sorted(history_dates, key=trading_dates.index)):
            raise ContractError(["SECTOR_HISTORY_DATE_ALIGNMENT_INVALID"], sid)
        if any(r.get("close") is None or not isinstance(r.get("close"), (int, float))
               or r.get("close") <= 0 for r in history):
            raise ContractError(["SECTOR_CLOSE_INVALID"], sid)
        by_date = {r["date"]: r for r in history}
        closes = [(by_date.get(day) or {}).get("close") for day in trading_dates]
        missing_dates = [day for day in trading_dates if day not in by_date]
        returns = {f"{w}D": _return(closes, w) for w in WINDOWS}
        benchmark_returns = {f"{w}D": _return(benchmark_closes, w) for w in WINDOWS}
        excess = {key: ((1 + value) / (1 + benchmark_returns[key]) - 1
                        if value is not None and benchmark_returns[key] is not None else None)
                  for key, value in returns.items()}
        sector_daily5 = _daily_returns(closes, 5)
        benchmark_daily5 = _daily_returns(benchmark_closes, 5)
        daily_excess5 = ([(1 + s) / (1 + b) - 1 for s, b in zip(sector_daily5, benchmark_daily5)]
                         if sector_daily5 and benchmark_daily5 else None)
        daily_excess3 = daily_excess5[-3:] if daily_excess5 and len(daily_excess5) >= 3 else None
        breadth = _breadth(history, trading_dates)
        attention = _activity(history, trading_dates)
        latest_history = history[0] if history else {}
        latest_breadth_invalid = (
            latest_history.get("breadth_denominator") is None
            or latest_history.get("up_count") is None
        )
        limitations = []
        if len(history) < 60 or missing_dates:
            limitations.append("PRICE_HISTORY_LT_60")
        if missing_dates:
            limitations.append("ENUMERATED_HISTORY_GAPS")
        if breadth["valid_day_counts"]["20D"] < 20:
            limitations.append("BREADTH_20D_LIMITED")
        if latest_breadth_invalid:
            limitations.append("BREADTH_LATEST_INVALID_NO_DIRECTION")
        attention_limited = (attention["valid_day_counts"]["20D_BASELINE"] < 20
                             or attention["valid_day_counts"]["60D_HISTORY"] < 60)
        if attention["valid_day_counts"]["20D_BASELINE"] < 20:
            limitations.append("ATTENTION_BASELINE_LIMITED")
        if attention["valid_day_counts"]["60D_HISTORY"] < 60:
            limitations.append("ATTENTION_60D_HISTORY_LIMITED")
        breadth_metrics = {k: v for k, v in breadth.items() if k != "audit"}
        breadth_metrics.update({
            "current_valid_member_count": ident.get("member_count"),
            "membership_change": None,
            "coverage": {
                "5D": ("FULL" if breadth["valid_day_counts"]["5D"] == 5 else "LIMITED"),
                "20D": ("FULL" if breadth["valid_day_counts"]["20D"] == 20 else "LIMITED"),
            },
        })
        attention_metrics = {k: v for k, v in attention.items() if k != "audit"}
        attention_metrics["coverage"] = {
            "20D_BASELINE": ("FULL" if attention["valid_day_counts"]["20D_BASELINE"] == 20
                             else "LIMITED"),
            "60D_HISTORY": ("FULL" if attention["valid_day_counts"]["60D_HISTORY"] == 60
                            else "LIMITED"),
        }
        observations.append({
            "source_sector_id": sid, "source_scope": scope,
            "source_layer": sector.get("source_layer"),
            "source_name": sector.get("source_name"),
            "direction_permission": ("NO_DIRECTION_SOURCE_OBSERVATION"
                                     if latest_breadth_invalid
                                     else "ELIGIBLE_SOURCE_OBSERVATION"),
            "identity": {
                "member_count": ident.get("member_count"),
                "membership_hash": ident.get("membership_hash"),
                "market_proxy": "PRIMARY_INDEX",
            },
            "metrics": {
                "price": {"returns": returns, "excess_returns": excess,
                          "daily_excess_path_5d": daily_excess5,
                          "last_day_dominance": {"3D": _dominance(daily_excess3),
                                                 "5D": _dominance(daily_excess5)}},
                "breadth": breadth_metrics,
                "attention": attention_metrics,
            },
            "data_health": {
                "price": "LIMITED" if missing_dates else "SUFFICIENT",
                "breadth": ("INVALID" if latest_breadth_invalid else
                            ("LIMITED" if breadth["valid_day_counts"]["20D"] < 20
                             else "SUFFICIENT")),
                "attention": ("LIMITED" if attention_limited else "SUFFICIENT"),
            },
            "audit": {"history": history, "missing_trading_dates": missing_dates,
                      "breadth": breadth["audit"],
                      "attention": attention["audit"]},
            "limitations": limitations,
            "provenance": [
                provenance_ref(market_hash,
                    f"/payload/sectors/{market['sectors'].index(sector)}",
                    market.get("capture_completed_at") or market.get("market_data_captured_at")),
                provenance_ref(market_hash, "/payload/benchmark",
                    market.get("capture_completed_at") or market.get("market_data_captured_at")),
                provenance_ref(identity_hash,
                    f"/payload/catalog/{identity['catalog'].index(ident)}",
                    identity.get("capture_completed_at") or identity.get("identity_observed_at")),
            ],
        })

    # Peer percentiles are intentionally not calculated over raw source rows.
    # Multiple overlapping supplier labels can map to one stable theme and would
    # otherwise double-count that theme.  run_preopen computes the descriptive
    # percentile after registry mapping, over one unique proxy per theme.
    primary_count = sum(row.get("source_scope") == "PRIMARY" for row in market.get("sectors", []))
    observed_primary = sum(row["source_scope"] == "PRIMARY" for row in observations)
    if observed_primary != primary_count:
        raise ContractError(["PRIMARY_SOURCE_ACCOUNTING_FAILED"])
    output = {
        **artifact("observations", "observations"),
        "market_data_as_of": market["market_data_as_of"],
        "market_frame_hash": market_hash,
        "identity_frame_hash": identity_hash,
        "source_universe_version": identity.get("universe_version"),
        "catalog_version": market.get("catalog_version"),
        "window_order": [f"{w}D" for w in WINDOWS],
        "path_order": ["D-4", "D-3", "D-2", "D-1", "D"],
        "source_count": len(market.get("sectors", [])),
        "primary_source_count": primary_count,
        "accounted_source_count": len(observations),
        "source_observations": observations,
        "provisional_labels": identity.get("provisional_labels", []),
        "universe_ref": content_hash(universe) if universe else None,
    }
    output["artifact_hash"] = content_hash(output)
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--market-frame", required=True)
    parser.add_argument("--identity-frame", required=True)
    parser.add_argument("--universe")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        result = build(load_json(args.market_frame), load_json(args.identity_frame),
                       load_json(args.universe) if args.universe else None)
        atomic_write_json(args.output, result)
    except ContractError as exc:
        print(json.dumps({"status": "FAIL", "reason_codes": exc.reasons,
                          "detail": str(exc)}, ensure_ascii=False))
        raise SystemExit(2)
    print(f"OBSERVATIONS PASS｜PRIMARY={result['primary_source_count']}｜"
          f"accounted={result['accounted_source_count']}")


if __name__ == "__main__":
    main()
