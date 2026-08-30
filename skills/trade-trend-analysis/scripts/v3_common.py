#!/usr/bin/env python3
"""Shared deterministic primitives for trade-trend-analysis V3.

No function in this module fetches market data.  Market facts enter only as
frozen trade-data-gateway envelopes supplied to the command line tools.
"""

from __future__ import annotations

import hashlib
import copy
import json
import os
import re
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

try:
    from jsonschema import Draft202012Validator
except ImportError:  # pragma: no cover - production must install the declared dependency
    Draft202012Validator = None

PRODUCER_VERSION = "trade-trend-analysis-v3.0.0-dev.2"
SCHEMA_VERSION = "v3.0-draft-1"
RELEASE_MODES = ("INTERNAL_GATE", "SHADOW", "OFFICIAL")
GATEWAY_SCHEMA_VERSION = "1.5"
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ContractError(RuntimeError):
    """A closed-gate contract failure with stable machine reason codes."""

    def __init__(self, reasons: list[str], detail: str | None = None):
        self.reasons = sorted(set(reasons))
        super().__init__(detail or "; ".join(self.reasons))


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def content_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def content_address_without(value: dict, *excluded_fields: str) -> str:
    """Hash one immutable config/reference after removing self-reference fields."""
    material = copy.deepcopy(value)
    for field in excluded_fields:
        material.pop(field, None)
    return content_hash(material)


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def load_json(path: str | Path) -> dict:
    path = Path(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(["JSON_READ_FAILED"], f"{path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError(["JSON_ROOT_NOT_OBJECT"], str(path))
    return value


def atomic_write_json(path: str | Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(value, fh, ensure_ascii=False, sort_keys=True, indent=2,
                      allow_nan=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def atomic_write_text(path: str | Path, value: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(value)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def parse_ts(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(["TIMESTAMP_INVALID"], str(value)) from exc
    if parsed.tzinfo is None:
        raise ContractError(["TIMESTAMP_TIMEZONE_MISSING"], str(value))
    return parsed


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def require_date(value: Any, reason: str) -> str:
    if not isinstance(value, str) or not ISO_DATE.match(value):
        raise ContractError([reason])
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ContractError([reason]) from exc
    return value


def schema_ref(name: str) -> dict:
    root = Path(__file__).resolve().parent.parent
    path = root / "schemas" / f"{name}.schema.json"
    if not path.exists():
        raise ContractError(["SCHEMA_REF_MISSING"], str(path))
    return {"id": name, "hash": file_hash(path)}


def artifact(kind: str, schema_name: str, created_at: str | None = None) -> dict:
    return {
        "artifact_kind": kind,
        "schema_version": SCHEMA_VERSION,
        "schema_ref": schema_ref(schema_name),
        "producer_version": PRODUCER_VERSION,
        "created_at": created_at or now_iso(),
    }


def envelope_payload(env: dict, expected_type: str) -> dict:
    required = ("schema_version", "data_type", "code", "ts", "source", "payload", "meta")
    if (any(key not in env for key in required)
            or env.get("schema_version") != GATEWAY_SCHEMA_VERSION
            or env.get("data_type") != expected_type
            or env.get("source") != "wencai"
            or not isinstance(env.get("payload"), dict) or not isinstance(env.get("meta"), dict)
            or not isinstance(env["meta"].get("fetch_errors"), list)):
        raise ContractError([f"{expected_type.upper()}_ENVELOPE_INVALID"])
    if env["meta"].get("fetch_errors"):
        raise ContractError([f"{expected_type.upper()}_ENVELOPE_FETCH_ERRORS"])
    # sector_market_frame uses meta.degraded for fully enumerated *local old-
    # history* limitations.  That is not a global directional failure; the close
    # gate independently reconciles every limited ID and still requires every
    # PRIMARY latest core row.  Identity has no equivalent local-history
    # allowance and therefore remains strictly non-degraded.
    if expected_type == "sector_identity" and env["meta"].get("degraded") is True:
        raise ContractError(["SECTOR_IDENTITY_ENVELOPE_DEGRADED"])
    return env["payload"]


def mode_root(base: str | Path, release_mode: str) -> Path:
    if release_mode not in RELEASE_MODES:
        raise ContractError(["RELEASE_MODE_INVALID"])
    base = Path(base)
    if release_mode == "INTERNAL_GATE":
        return base / "internal"
    if release_mode == "SHADOW":
        return base / "shadow"
    return base


def immutable_version_dir(parent: Path) -> Path:
    """Return the next non-existing vN path. Callers atomically mkdir it."""
    n = 1
    while (parent / f"v{n}").exists():
        n += 1
    return parent / f"v{n}"


def validate_calendar(calendar: dict, market_date: str) -> tuple[str, dict]:
    required = ("calendar_version", "timezone", "market", "trading_dates",
                "sessions")
    if any(key not in calendar for key in required):
        raise ContractError(["TRADING_CALENDAR_CONTRACT_MISSING"])
    if calendar["timezone"] != "Asia/Shanghai":
        raise ContractError(["TRADING_CALENDAR_TIMEZONE_INVALID"])
    if calendar["market"] != "CN_A":
        raise ContractError(["TRADING_CALENDAR_MARKET_INVALID"])
    if not isinstance(calendar["calendar_version"], str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", calendar["calendar_version"]):
        raise ContractError(["TRADING_CALENDAR_VERSION_NOT_IMMUTABLE"])
    if calendar["calendar_version"] != content_address_without(calendar, "calendar_version"):
        raise ContractError(["TRADING_CALENDAR_VERSION_CONTENT_MISMATCH"])
    days = calendar["trading_dates"]
    if not isinstance(days, list) or days != sorted(set(days)):
        raise ContractError(["TRADING_CALENDAR_DATES_INVALID"])
    for day in days:
        require_date(day, "TRADING_CALENDAR_DATES_INVALID")
    if market_date not in days:
        raise ContractError(["MARKET_DATE_NOT_TRADING_DAY"])
    try:
        next_day = days[days.index(market_date) + 1]
    except IndexError as exc:
        raise ContractError(["NEXT_TRADING_DAY_UNAVAILABLE"]) from exc
    sessions = calendar["sessions"]
    if not isinstance(sessions, dict):
        raise ContractError(["TRADING_SESSION_CONFIG_MISSING"])
    if market_date not in sessions or next_day not in sessions:
        raise ContractError(["TRADING_SESSION_CONFIG_MISSING"])
    current_session = sessions[market_date]
    current_auction = (current_session.get("auction_start_at")
                       if isinstance(current_session, dict) else None)
    if not current_auction or parse_ts(current_auction).date().isoformat() != market_date:
        raise ContractError(["CURRENT_TRADING_SESSION_INVALID"])
    next_session = sessions[next_day]
    auction = next_session.get("auction_start_at") if isinstance(next_session, dict) else None
    if not auction:
        raise ContractError(["NEXT_AUCTION_TIME_MISSING"])
    auction_ts = parse_ts(auction)
    if auction_ts.date().isoformat() != next_day or str(auction_ts.tzinfo) not in (
            "UTC+08:00", "Asia/Shanghai"):
        raise ContractError(["NEXT_AUCTION_TIME_INVALID"])
    return next_day, next_session


def provenance_ref(artifact_hash: str, pointer: str, observed_at: str,
                   source: str = "trade-data-gateway") -> dict:
    return {
        "source": source,
        "artifact_hash": artifact_hash,
        "json_pointer": pointer,
        "observed_at": observed_at,
    }


def normalize_benchmark_history(benchmark: dict) -> tuple[list[str], list[float]]:
    """Consume gateway 1.5 ``[{date, close}]`` and declared legacy numeric arrays.

    In both forms dates must be supplied separately and remain the single order
    authority. Dict rows are additionally cross-checked row by row. No sorting,
    filling or coercion is performed.
    """
    dates = benchmark.get("trading_dates")
    history = benchmark.get("close_history")
    if not isinstance(dates, list) or not isinstance(history, list):
        raise ContractError(["BENCHMARK_CLOSE_HISTORY_INVALID"])
    if history and all(isinstance(row, dict) for row in history):
        row_dates = [row.get("date") for row in history]
        closes = [row.get("close") for row in history]
        if row_dates != dates:
            raise ContractError(["BENCHMARK_DATE_ALIGNMENT_FAILED"])
    elif all(isinstance(value, (int, float)) and not isinstance(value, bool)
             for value in history):
        closes = list(history)
    else:
        raise ContractError(["BENCHMARK_CLOSE_HISTORY_INVALID"])
    if (len(closes) != len(dates) or any(not isinstance(value, (int, float))
                                         or isinstance(value, bool) or value <= 0
                                         for value in closes)):
        raise ContractError(["BENCHMARK_CLOSE_HISTORY_INVALID"])
    return dates, closes


def read_artifact_checked(path: str | Path, kind: str) -> dict:
    value = load_json(path)
    validate_artifact_value(value, kind, str(path))
    return value


def validate_artifact_value(value: dict, kind: str, detail: str = "") -> None:
    if value.get("artifact_kind") != kind:
        raise ContractError(["ARTIFACT_KIND_MISMATCH"], detail)
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ContractError(["UNSUPPORTED_SCHEMA_VERSION"], detail)
    declared = value.get("artifact_hash")
    unhashed = dict(value)
    unhashed.pop("artifact_hash", None)
    if declared != content_hash(unhashed):
        raise ContractError(["ARTIFACT_CONTENT_HASH_MISMATCH"], detail)
    ref = value.get("schema_ref") or {}
    expected_ref = schema_ref(ref.get("id")) if ref.get("id") else None
    if expected_ref != ref:
        raise ContractError(["ARTIFACT_SCHEMA_REF_MISMATCH"], detail)
    if Draft202012Validator is None:
        raise ContractError(["JSON_SCHEMA_VALIDATOR_UNAVAILABLE"], detail)
    schema_path = Path(__file__).resolve().parent.parent / "schemas" / f"{ref['id']}.schema.json"
    schema = load_json(schema_path)
    schema_errors = sorted(Draft202012Validator(schema).iter_errors(value),
                           key=lambda error: list(error.absolute_path))
    if schema_errors:
        first = schema_errors[0]
        pointer = "/" + "/".join(str(part) for part in first.absolute_path)
        raise ContractError(["ARTIFACT_JSON_SCHEMA_INVALID"],
                            f"{detail}{pointer}: {first.message}")
