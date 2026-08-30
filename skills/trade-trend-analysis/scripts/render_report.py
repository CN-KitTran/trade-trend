#!/usr/bin/env python3
"""Deterministically render the concise Markdown projection of one ledger."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from v3_common import ContractError, atomic_write_text, content_hash, read_artifact_checked


def _name(theme: dict) -> str:
    return theme.get("display_name") or theme["theme_id"]


MARKET_ROLE_LABELS = {
    "REGIME_LEADER": "环境主线",
    "RECEIVER": "资金承接方",
    "DONOR": "资金流出方",
    "INDEPENDENT": "独立产业机会",
    "COUNTER_REGIME": "逆环境强势",
    "CROWDED": "高位拥挤",
    "NEUTRAL": "普通波动",
}

OPPORTUNITY_DRIVER_LABELS = {
    "INDUSTRY": "产业驱动",
    "REGIME": "环境驱动",
    "DUAL": "产业与环境共振",
    "EVENT": "事件驱动",
    "PRICE_ONLY": "纯价格驱动",
}

RISK_TYPE_LABELS = {
    "INDUSTRY": "产业景气风险",
    "INDUSTRY_CYCLE": "产业景气风险",
    "MARKET": "市场趋势风险",
    "MARKET_TREND": "市场趋势风险",
    "STYLE": "风格退潮风险",
    "STYLE_RETREAT": "风格退潮风险",
    "CAPITAL_OUTFLOW": "资金流出风险",
    "CROWDING": "高位拥挤风险",
    "CROWDED": "高位拥挤风险",
    "EVENT_POLICY": "事件/政策风险",
    "EVENT_OR_POLICY": "事件/政策风险",
    "MIXED": "混合风险",
}

REGIME_ALIGNMENT_LABELS = {
    "ALIGNED": "顺应主环境",
    "COUNTER": "逆环境",
    "COUNTER_REGIME": "逆环境",
    "MIXED": "与主环境关系混合",
    "NEUTRAL": "与主环境关系中性",
}


def _label(value, labels: dict[str, str]) -> str:
    if value is None or value == "":
        return "未形成已校验结论"
    return labels.get(str(value), str(value))


def _item_text(item) -> str:
    if isinstance(item, dict):
        return str(item.get("summary") or item.get("reason") or
                   item.get("description") or item.get("theme_id") or "-")
    return str(item)


def _joined(value, labels: dict[str, str] | None = None) -> str:
    if value is None or value == "" or value == []:
        return "未形成已校验结论"
    values = value if isinstance(value, list) else [value]
    if labels:
        return "、".join(_label(item, labels) for item in values)
    return "、".join(_item_text(item) for item in values)


def _migration_side(migration: dict, side: str, theme_names: dict) -> str:
    summary = migration.get(f"{side}_summary")
    if summary:
        return str(summary)
    ids = migration.get(f"{side}_theme_ids") or []
    if ids:
        return "、".join(theme_names.get(theme_id, theme_id) for theme_id in ids)
    return "未形成已校验结论"


def _market_context_lines(ledger: dict, theme_names: dict) -> list[str]:
    context = ledger.get("market_context") or {}
    status = context.get("context_status")
    has_v052_fields = any(context.get(key) not in (None, "", [], {}) for key in (
        "market_regime", "risk_appetite", "capital_migration", "duration",
        "contradictions", "confidence"))
    if not has_v052_fields:
        limitations = context.get("limitations") or ["V0.52_MARKET_CONTEXT_FIELDS_MISSING"]
        return [
            f"- 市场状态层尚未形成已校验结论（{status or '未提供状态'}）。",
            "- 风险偏好、资金迁移与持续阶段均不可判定；以下仅展示已校验的板块级结论。",
            f"- 限制：{_joined(limitations)}。",
        ]

    migration = context.get("capital_migration") or {}
    confidence = context.get("confidence")
    if isinstance(confidence, dict):
        confidence_text = _joined(confidence.get("level"))
        if confidence.get("reason"):
            confidence_text += f"（{confidence['reason']}）"
    else:
        confidence_text = _joined(confidence)
    contradictions_value = context.get("contradictions")
    contradictions = ("未发现重要反证" if contradictions_value == []
                      else _joined(contradictions_value))
    return [
        (f"- 市场状态：{_joined(context.get('market_regime'))}；"
         f"风险偏好：{_joined(context.get('risk_appetite'))}；"
         f"阶段：{_joined(context.get('duration'))}。"),
        (f"- 资金迁移：从 {_migration_side(migration, 'from', theme_names)}，"
         f"流向 {_migration_side(migration, 'to', theme_names)}。"),
        f"- 关键矛盾：{contradictions}。",
        f"- 判断置信：{confidence_text}。",
    ]


def _opportunity_context(theme: dict) -> str:
    role = _label(theme.get("market_role"), MARKET_ROLE_LABELS)
    driver = _label(theme.get("opportunity_driver"), OPPORTUNITY_DRIVER_LABELS)
    parts = [f"市场角色：{role}", f"主要驱动：{driver}"]
    interpretation = theme.get("regime_interpretation")
    alignment = theme.get("regime_alignment")
    if interpretation:
        parts.append(str(interpretation))
    elif alignment:
        parts.append(_label(alignment, REGIME_ALIGNMENT_LABELS))
    elif theme.get("market_role") is None and theme.get("opportunity_driver") is None:
        parts.append("V0.52字段缺失")
    return "｜".join(parts)


def _risk_context(theme: dict) -> str:
    risk_types = _joined(theme.get("risk_types"), RISK_TYPE_LABELS)
    role = _label(theme.get("market_role"), MARKET_ROLE_LABELS)
    parts = [f"风险类型：{risk_types}", f"市场角色：{role}"]
    interpretation = theme.get("regime_interpretation")
    alignment = theme.get("regime_alignment")
    if interpretation:
        parts.append(str(interpretation))
    elif alignment:
        parts.append(_label(alignment, REGIME_ALIGNMENT_LABELS))
    elif theme.get("risk_types") is None and theme.get("market_role") is None:
        parts.append("V0.52字段缺失")
    return "｜".join(parts)


def render(ledger: dict) -> str:
    if ledger.get("publication_status") not in {"VALIDATED_NOT_PUBLISHED", "PUBLISHED", "AMENDED"}:
        raise ContractError(["LEDGER_NOT_RENDERABLE"])
    early_internal = (ledger.get("release_mode") == "INTERNAL_GATE"
                      and ledger.get("run_window_status") == "EARLY_DRAFT")
    report_label = "内部早稿" if early_internal else "盘前"
    lines = [f"# 板块机会与风险｜{ledger['decision_date']} {report_label}", ""]
    if early_internal:
        lines += ["> ⚠️ INTERNAL_GATE 早稿：按实际运行时点截断信息，仅供内部查看；不会成为正式台账或 Vault 正式报告。", ""]
    theme_names = {theme.get("theme_id"): _name(theme) for theme in ledger["themes"]}
    lines += [
             (f"> 决策日期：{ledger['decision_date']}｜市场数据截至：{ledger['market_data_as_of']} 收盘｜"
              f"信息截止：{ledger['information_cutoff']}｜生效时间：{ledger.get('effective_from') or '未生效（预览）'}｜"
              f"数据健康：{ledger['global_data_health']}｜发布完整性：{ledger['publication_completeness']}｜"
              f"台账：{ledger['run_id']}"), "", "## 市场现在在交易什么", ""]
    lines += _market_context_lines(ledger, theme_names) + ["", "## 今日结论", "",
             f"- {ledger.get('daily_summary') or '本次没有可发布的新增结论。'}", ""]
    focus_opp, brief_opp, focus_risk, brief_risk = [], [], [], []
    for theme in ledger["themes"]:
        if theme["state_provenance"]["mode"] != "CURRENT_VALIDATED":
            continue
        opp_tier = ((theme.get("report_routing") or {}).get("opportunity") or {}).get("tier")
        risk_tier = ((theme.get("report_routing") or {}).get("risk") or {}).get("tier")
        if opp_tier not in {"FOCUS", "BRIEF", "LEDGER_ONLY"}:
            raise ContractError(["OPPORTUNITY_REPORT_ROUTE_INVALID"], theme.get("theme_id"))
        if risk_tier not in {"FOCUS", "BRIEF", "LEDGER_ONLY"}:
            raise ContractError(["RISK_REPORT_ROUTE_INVALID"], theme.get("theme_id"))
        if theme.get("opportunity_stage") in {"ACTIVE", "MATURE"}:
            if opp_tier == "FOCUS":
                focus_opp.append(theme)
            elif opp_tier == "BRIEF":
                brief_opp.append(theme)
        if theme.get("risk_level") in {"CAUTION", "HIGH", "EXIT"}:
            if risk_tier == "FOCUS":
                focus_risk.append(theme)
            elif risk_tier == "BRIEF":
                brief_risk.append(theme)
    lines += ["## 重点机会", ""]
    if not focus_opp and not brief_opp:
        lines += ["- 本栏无需要在正文展示的有效机会；其余结论仅留台账。", ""]
    for theme in focus_opp:
        lines += [f"### {_name(theme)}｜{theme['opportunity_stage']} + {theme.get('risk_level') or 'LOW'}", "",
                  f"- 环境归因：{_opportunity_context(theme)}",
                  f"- 核心判断：{theme.get('thesis') or '-'}",
                  f"- 为什么是现在：{theme.get('why_now') or '-'}",
                  f"- 关键依据：{theme.get('key_evidence_summary') or '; '.join(theme.get('evidence_refs') or []) or '-'}",
                  f"- 下一验证：{theme.get('next_validation') or '-'}",
                  f"- 失效条件：{theme.get('opportunity_invalidation_or_reentry_condition') or '-'}", ""]
    if brief_opp:
        lines += ["### 其他有效机会", ""] + [
            f"- {_name(t)}｜{t['opportunity_stage']} + {t.get('risk_level') or 'LOW'}｜{t.get('thesis') or '-'}｜{t.get('next_validation') or '-'}"
            for t in brief_opp] + [""]
    lines += ["## 重点风险", ""]
    if not focus_risk and not brief_risk:
        lines += ["- 本栏无需要在正文展示的显著风险；其余结论仅留台账。", ""]
    for theme in focus_risk:
        lines += [f"### {_name(theme)}｜{theme['risk_level']}", "",
                  f"- 风险归因：{_risk_context(theme)}",
                  f"- 核心风险：{theme.get('risk_summary') or '-'}",
                  f"- 关键依据：{theme.get('risk_evidence_summary') or '; '.join(theme.get('risk_evidence_refs') or []) or '-'}",
                  f"- 风险解除条件：{theme.get('risk_relief_condition') or '-'}", ""]
    if brief_risk:
        lines += ["### 其他有效风险", ""] + [
            f"- {_name(t)}｜{t['risk_level']}｜{t.get('risk_summary') or '-'}｜{t.get('risk_relief_condition') or '-'}"
            for t in brief_risk] + [""]
    projection = ledger["report_projection"]
    if projection.get("sensing_watch_items"):
        lines += ["## 继续观察", ""] + [
            f"- {x.get('display_name') or theme_names.get(x.get('theme_id')) or x.get('theme_id')}｜{x.get('label', 'WATCH')}｜{x.get('reason') or '-'}｜{x.get('next_validation') or '-'}"
            for x in projection["sensing_watch_items"]] + [""]
    unrouted_watch_count = ((projection.get("unrouted_sensing_watch_summary") or {})
                            .get("theme_count", 0))
    if unrouted_watch_count:
        if not projection.get("sensing_watch_items"):
            lines += ["## 继续观察", ""]
        lines += [
            f"- 另有 {unrouted_watch_count} 个感知级 WATCH 完整保存在 sensing.json；"
            "因未经过候选后判断和报告路由，本预览不逐项展开。",
            "",
        ]
    combined = projection.get("failed_review_items", []) + projection.get("invalidation_and_exit_items", [])
    if combined:
        lines += ["## 撤出、失效与复核失败", ""] + [
            f"- {x.get('theme_id')}｜{x.get('opportunity_stage') or x.get('status') or 'NO_FORMAL_STATE'} / {x.get('risk_level') or '-'}｜{x.get('reason') or '本次复核失败，未形成当前正式状态'}"
            for x in combined] + [""]
    if projection.get("candidate_change_items"):
        lines += ["## 候选股票变化", ""] + [
            f"- {x.get('theme_id')}｜{x.get('stock_code')}｜REMOVE｜{(x.get('candidate_change') or {}).get('reason', '-')}"
            for x in projection["candidate_change_items"]] + [""]
    if projection.get("alert_items"):
        raise ContractError(["ALERT_LIFECYCLE_AND_RENDERING_NOT_IMPLEMENTED"])
    limitations = ledger.get("publication_limitations", []) + ledger.get("global_limitations", [])
    if limitations:
        lines += ["## 数据限制", ""] + [f"- {item}" for item in limitations] + [""]
    return "\n".join(lines).rstrip() + "\n"


def _default_vault_relative_output(ledger: dict) -> Path:
    decision_date = date.fromisoformat(ledger["decision_date"])
    return (Path("investment") / "trend" /
            f"{decision_date.year:04d}-{decision_date.month:02d}" /
            f"W{decision_date.isocalendar().week:02d}" /
            f"{decision_date.isoformat()}-板块扫描.md")


def _vault_target(vault_root: str, relative_output: str | None, ledger: dict) -> Path:
    root = Path(vault_root).resolve()
    if not (root / ".obsidian").is_dir():
        raise SystemExit("RENDER FAIL: VAULT_ROOT_NOT_OBSIDIAN_VAULT")
    relative = (Path(relative_output) if relative_output
                else _default_vault_relative_output(ledger))
    if (relative.is_absolute() or relative.suffix.lower() != ".md"
            or ".." in relative.parts or ".obsidian" in relative.parts):
        raise SystemExit("RENDER FAIL: VAULT_OUTPUT_RELATIVE_PATH_INVALID")
    target = (root / relative).resolve()
    if root not in target.parents:
        raise SystemExit("RENDER FAIL: VAULT_OUTPUT_OUTSIDE_VAULT")
    return target


def _write_idempotent(path: Path, text: str, conflict_reason: str) -> str:
    if path.exists():
        if path.read_text(encoding="utf-8") == text:
            return "IDEMPOTENT"
        raise SystemExit(f"RENDER FAIL: {conflict_reason}")
    atomic_write_text(path, text)
    return "PASS"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--vault-root",
        help=("optional Obsidian vault root; writes the exact same INTERNAL_GATE Markdown "
              "to investment/trend/YYYY-MM/Www/YYYY-MM-DD-板块扫描.md"),
    )
    parser.add_argument(
        "--vault-output-relative",
        help="optional relative .md path inside --vault-root",
    )
    parser.add_argument("--allow-unpublished-official-preview", action="store_true",
                        help="deprecated; this command is always a same-directory preview renderer")
    args = parser.parse_args()
    ledger = read_artifact_checked(args.ledger, "ledger")
    ledger_parent = Path(args.ledger).resolve().parent
    output = Path(args.output).resolve()
    if output.parent != ledger_parent or output.name != "preview.md":
        raise SystemExit("RENDER FAIL: PREVIEW_OUTPUT_MUST_BE_BESIDE_LEDGER")
    text = render(ledger)
    if args.vault_output_relative and not args.vault_root:
        raise SystemExit("RENDER FAIL: VAULT_OUTPUT_REQUIRES_VAULT_ROOT")
    vault_target = None
    if args.vault_root:
        if ledger.get("release_mode") != "INTERNAL_GATE":
            raise SystemExit("RENDER FAIL: VAULT_INTERNAL_PREVIEW_REQUIRES_INTERNAL_GATE")
        vault_target = _vault_target(args.vault_root, args.vault_output_relative, ledger)
    output = Path(args.output)
    report_status = _write_idempotent(output, text, "IMMUTABLE_REPORT_CONFLICT")
    print(f"REPORT {report_status}｜ledger={ledger['artifact_hash']}｜"
          f"report={content_hash(text)}｜{args.output}")
    if vault_target:
        vault_status = _write_idempotent(
            vault_target, text, "VAULT_REPORT_CONFLICT_REQUIRES_EXPLICIT_AMENDMENT")
        print(f"VAULT REPORT {vault_status}｜{vault_target}")


if __name__ == "__main__":
    main()
