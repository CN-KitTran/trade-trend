#!/usr/bin/env python3
"""
trade-trend-analysis 数据层 — 全市场板块扫描采集（2026-08-16 重构；2026-08-18 迁移网关）

一次调用完成 12 条全市场查询（涨幅/跌幅/资金流入流出/成交额/5日·20日涨幅/量比异动/
3日·10日·20日主力净买入榜/回撤榜）。输出 5 个价格窗口（今/近3/近5/近10/近20 日）+
5 个资金窗口（今/3/5/10/20 日主力净买入）+ 距20日高点回撤，判断层差分读段还原走势形状。
自动翻页、合并去重、剔除宽泛指数。LLM 不再逐条跑 Bash。

用法:
  python3 scan_sectors.py --date 2026-08-14 [--format table|json]

输出:
  - stdout: 压缩表格（默认，LLM 直接可读）或完整 JSON（--format json）
  - 落盘: ~/.claude/skills/trade-trend-analysis/state/sectors.json（完整 JSON，调试/复用）

依赖（2026-08-18 阶段 6 迁移）: trade-data-gateway 统一入口（fetch_wencai → sector 域）。
  密钥池轮换/限流识别/熔断/契约校验全部由网关接管（原 hithink-sector-selector 直连
  import 与 keypool 副本依赖已删除；rows 零翻译透传 → 本文件解析/合并逻辑零改动）。
  熔断语义：sector 域连续失败 ≥2 次 → wencai::sector 冷却 5 分钟（per-domain，
  不殃及其他域；配额耗尽时提前止损，剩余查询报「熔断冷却中」入 errors）。
"""

import argparse
import json
import os
import sys
import time

# ─── 路径与依赖注入 ─────────────────────────────────────────────
_HOME = os.path.expanduser("~")
_STATE_DIR = os.path.join(_HOME, ".claude/skills/trade-trend-analysis/state")
_STATE_FILE = os.path.join(_STATE_DIR, "sectors.json")

_GATEWAY_DIR = os.path.join(_HOME, ".claude/skills/trade-data-gateway")
if os.path.isdir(_GATEWAY_DIR) and _GATEWAY_DIR not in sys.path:
    sys.path.insert(0, _GATEWAY_DIR)

try:
    from gateway import fetch_wencai  # 问财统一查询通道（sector 域，含轮换/熔断/契约）
except ImportError:
    print("FATAL: 无法 import trade-data-gateway（依赖缺失或网关已迁移）", file=sys.stderr)
    sys.exit(2)

# ─── 查询定义（从 agent prompt 迁移，limit 提高 + 自动翻页） ────────

QUERIES = [
    # (source_tag, 查询词) —— 字段覆盖矩阵：价格窗口 今/3/5/10/20 日 × 资金窗口 今/3/5/10/20 日 × 回撤
    # 窗口由问财返回的日期区间字段自动分类（_classify）；多榜交叉补全字段
    ("top_gain",  "概念板块 今日涨幅排名前40 主力资金净流入 近3日涨跌幅 近5日涨跌幅 近20日涨跌幅 距20日最高点回撤幅度"),
    ("top_loss",  "概念板块 今日跌幅排名前15 近3日涨跌幅 近5日涨跌幅 近10日涨跌幅 近20日涨跌幅"),
    ("main_in",   "概念板块 主力资金净流入排名前40 今日涨跌幅 近3日涨跌幅 近5日涨跌幅 近10日涨跌幅 近20日涨跌幅"),
    ("main_out",  "概念板块 主力资金净流出排名前25 今日涨跌幅 近3日涨跌幅 近5日涨跌幅 近10日涨跌幅 近20日涨跌幅"),
    ("turnover",  "概念板块 成交额排名前25 量比 今日涨跌幅 近5日涨跌幅"),
    ("gain_5d",   "概念板块 近5日累计涨幅排名前30 今日涨跌幅 近3日涨跌幅 近20日涨跌幅 主力资金净流入 距20日最高点回撤幅度"),
    ("vol_ratio", "概念板块 量比大于1.5 今日涨幅大于0 主力资金净流入排名前20 近5日涨跌幅 近20日涨跌幅"),
    ("gain_20d",  "概念板块 近20日累计涨幅排名前30 今日涨跌幅 近5日涨跌幅 近10日涨跌幅 距20日最高点回撤幅度"),
    ("main_20d",  "概念板块 近20日主力净买入额 排名前30 今日主力净买入额 近5日主力净买入额"),
    ("main_10d",  "概念板块 近10日主力净买入额 排名前25 近3日主力净买入额 近5日主力净买入额 今日主力净买入额"),
    ("main_3d",   "概念板块 近3日主力净买入额 排名前25 今日主力净买入额 近5日主力净买入额"),
    ("drawdown",  "概念板块 距20日最高点回撤幅度最小 近20日涨跌幅 近5日涨跌幅 今日涨跌幅 近10日涨跌幅"),
]

# 剔除宽泛指数
BROAD_INDEX_KEYWORDS = ("融资融券", "深股通", "沪股通", "国企改革", "证金持股")

PAGE_LIMIT = "40"      # 每页条数
MAX_PAGES = 2          # 每查询最多翻页数（防失控）


# ─── 字段提取（问财列名带日期后缀且不稳定，按日期模式分类） ──────────

import re
from datetime import datetime

_DATE_RE = re.compile(r"\[(\d{8})(?:-(\d{8}))?\]")


def _span_days(m):
    """区间自然日跨度；单日或解析失败返回 None。"""
    if not m or not m.group(2):
        return None
    try:
        d1 = datetime.strptime(m.group(1), "%Y%m%d")
        d2 = datetime.strptime(m.group(2), "%Y%m%d")
        return (d2 - d1).days
    except ValueError:
        return None


def _trading_days(m):
    """区间内交易日数（工作日计数近似）。

    问财的「近N日」是**交易日口径**：区间端点均为真实交易日，
    统计区间内周一~周五数量即可还原 N。此前的自然日跨度口径在
    跨周末时错位（如近3日 [0813-0817] 自然日跨度 4 > 3，被误吞入
    5日窗），导致 chg_3d/main_3d 永久缺失。

    局限：区间跨法定长假（国庆/春节）时工作日数 > 真实交易日数，
    可能向上误判一档；对正常周完全正确，极端场景可在报告中注明。
    """
    if not m or not m.group(2):
        return None
    try:
        d1 = datetime.strptime(m.group(1), "%Y%m%d")
        d2 = datetime.strptime(m.group(2), "%Y%m%d")
    except ValueError:
        return None
    if d2 < d1:
        return None
    n = 0
    d = d1
    while d <= d2:
        if d.weekday() < 5:
            n += 1
        d = datetime.fromordinal(d.toordinal() + 1)
    return n


def parse_pct(value):
    """涨跌幅/量比类：'2.95%'→2.95，'1.53'→1.53，纯数字原样。"""
    if value is None:
        return None
    s = str(value).strip().replace(",", "").replace("+", "").replace("%", "")
    if s in ("", "-", "--", "—", "None", "null", "nan"):
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def parse_amount(value):
    """金额类 → 亿单位 float。'138.8亿'→138.8 | '5.4万'→0.00054→0.00054?
    万→/1e4 亿：5.4万=0.00054亿。纯数字按「元」处理：13875555000.0→138.76。"""
    if value is None:
        return None
    s = str(value).strip().replace(",", "").replace("+", "")
    if s in ("", "-", "--", "—", "None", "null", "nan"):
        return None
    try:
        if "万亿" in s:
            return float(s.replace("万亿", "")) * 10000.0
        if "亿" in s:
            return float(s.replace("亿", ""))
        if "万" in s:
            return float(s.replace("万", "")) / 10000.0
        if "%" in s:
            return None  # 比率不是金额，拒绝
        return float(s) / 1e8  # 纯数字 = 元 → 亿
    except (ValueError, TypeError):
        return None


def _classify(key):
    """按字段名分类 → (类别, 优先级)。
    类别：chg_today/chg_3d/chg_5d/chg_10d/chg_20d（价格窗口）、
    main_flow/main_3d/main_5d/main_10d/main_20d（主力窗口）、
    drawdown/vol_ratio/amount/name/index_code。
    窗口按区间**交易日数**（工作日计数，见 _trading_days）划分：
    <=1→今日，2~3→3日窗，4~6→5日窗，7~12→10日窗，>12→20日窗。
    问财列名形态：'涨跌幅[20260814]'（当日）、'涨跌幅[20260810-20260814]'（5日区间）、
    '主力净买入额[20260810-20260814]'（5日主力）、'回撤幅度[20260814]'、'指数简称'。"""
    kk = str(key)
    m = _DATE_RE.search(kk)
    base = _DATE_RE.sub("", kk)

    # 名称
    if any(kw in kk for kw in ("指数简称", "板块简称", "简称", "名称")):
        return "name", 3
    if "代码" in kk:
        return "index_code", 0
    if "回撤" in kk or ("收盘价" in kk and "最高价" in kk):
        # 两种口径：'回撤幅度[20260814]'（正值=距高点%）与
        # '(收盘价[...]-最高价)/绝对值(最高价)'（负值，同义），统一取绝对值
        return "drawdown", 0 if m else 1

    if m:  # 带日期标注（精确，优先级最高）
        td = _trading_days(m)
        if any(kw in base for kw in ("涨跌", "涨幅", "跌幅")):
            if td is None or td <= 1:
                return "chg_today", 0
            if td <= 3:
                return "chg_3d", 0
            if td <= 6:
                return "chg_5d", 0
            if td <= 12:
                return "chg_10d", 0
            return "chg_20d", 0
        if "主力" in base and ("买入额" in base or "净" in base):
            if td is None or td <= 1:
                return "main_flow", 0
            if td <= 3:
                return "main_3d", 0
            if td <= 6:
                return "main_5d", 0
            if td <= 12:
                return "main_10d", 0
            return "main_20d", 0
        if "成交额" in base:
            return "amount", 0
        if "量比" in base:
            return "vol_ratio", 0
        return None, None

    # 无日期标注（兜底，优先级低）
    if any(kw in kk for kw in ("涨跌幅", "涨幅", "跌幅")):
        return "chg_today", 2
    if "主力" in kk and "净" in kk and "率" not in kk:
        return "main_flow", 1
    if "量比" in kk:
        return "vol_ratio", 1
    if "成交额" in kk:
        return "amount", 1
    return None, None


def extract_sector(record):
    """从一条 datas 记录提取字段（两阶段：收集全部候选 → 每类取优先级最低值）。
    板块名必须命中，否则返回 None。"""
    cand = {}  # cat -> (prio, value)
    for k, v in record.items():
        cat, prio = _classify(k)
        if cat is None:
            continue
        cur = cand.get(cat)
        if cur is None or prio < cur[0]:
            cand[cat] = (prio, v)

    name_v = cand.get("name")
    if not name_v:
        return None
    name = str(name_v[1]).strip()
    if not name or name.lower() in ("-", "—"):
        return None
    if any(kw in name for kw in BROAD_INDEX_KEYWORDS):
        return None

    out = {
        "name": name, "index_code": None,
        "chg_today": None, "chg_3d": None, "chg_5d": None, "chg_10d": None, "chg_20d": None,
        "main_flow": None, "main_3d": None, "main_5d": None, "main_10d": None, "main_20d": None,
        "drawdown": None, "vol_ratio": None, "amount": None,
    }
    for cat, (prio, v) in cand.items():
        if cat == "name":
            continue
        if cat == "index_code":
            out["index_code"] = str(v).strip()
        elif cat in ("chg_today", "chg_3d", "chg_5d", "chg_10d", "chg_20d",
                     "vol_ratio"):
            out[cat] = parse_pct(v)
        elif cat == "drawdown":
            vv = parse_pct(v)
            out[cat] = abs(vv) if vv is not None else None  # 两口径符号相反，统一绝对值
        else:  # main 系列 / amount
            out[cat] = parse_amount(v)
    return out


# ─── 请求（网关取数 + 自动翻页） ────────────────────────────────

def fetch_query(query, timeout=None):
    """执行一条查询并翻页取全，返回 (records, error)。

    取数经 trade-data-gateway fetch_wencai（sector 域）：密钥池轮换/限流识别/熔断/
    契约校验由网关完成；rows = 问财原始行零翻译（本文件解析层零改动）。
    timeout 参数保留仅为签名兼容（超时由网关统一管理，30s/次）。
    """
    records = []
    page = 1
    while page <= MAX_PAGES:
        try:
            env = fetch_wencai(query, domain="sector", page=page, limit=int(PAGE_LIMIT))
        except Exception as e:
            # 网关失败（key 全耗尽/暗盘校验/熔断冷却等）→ 整查询失败入 errors
            return records, f"查询失败: {e}"
        datas = env["payload"].get("rows") or []
        records.extend(datas)
        if not env["payload"].get("has_more", False) or not datas:
            break
        page += 1
        time.sleep(0.3)  # 翻页间隔，降低触发限流概率

    return records, None


# ─── 主流程 ───────────────────────────────────────────────────

def run_scan(date_str, timeout=None):
    queries = list(QUERIES)

    merged = {}   # key(index_code|name) -> sector dict（跨查询补齐字段）
    order = []
    errors = []

    for tag, query in queries:
        records, err = fetch_query(query, timeout)
        if err:
            errors.append(err)
            continue
        for rec in records:
            sec = extract_sector(rec)
            if not sec:
                continue
            key = sec["index_code"] or sec["name"]
            if key not in merged:
                merged[key] = sec
                merged[key]["sources"] = []
                order.append(key)
            merged[key]["sources"].append(tag)
            # 字段补齐（当前记录有值且原为 None）
            for field in ("chg_today", "chg_3d", "chg_5d", "chg_10d", "chg_20d",
                          "main_flow", "main_3d", "main_5d", "main_10d", "main_20d",
                          "drawdown", "vol_ratio", "amount"):
                if merged[key][field] is None and sec[field] is not None:
                    merged[key][field] = sec[field]

    sectors = []
    for n in order:
        s = merged[n]
        s.pop("index_code", None)  # 去重键不再需要，省 token
        sectors.append(s)
    result = {
        "scan_date": date_str,
        "query_count": len(queries),
        "sector_count": len(sectors),
        "sectors": sectors,
        "errors": errors,
    }

    # 落盘（调试/复用）：按日快照 + 最新覆盖——快照供回测（radar 侧 sector_windows 快照
    # 与 trend 侧本快照同构，回测时按日期配对；/tmp 缓存易失，此处是 durable 副本）
    os.makedirs(_STATE_DIR, exist_ok=True)
    snap_file = os.path.join(_STATE_DIR, f"sectors_{date_str.replace('-', '')}.json")
    for path in (snap_file, _STATE_FILE):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)

    return result


def fmt_table(result):
    lines = []
    lines.append(f"# 板块数据 {result['scan_date']} | 候选 {result['sector_count']} 个 | "
                 f"查询 {result['query_count']} 条 | 来源: {';'.join(e for e in result['errors']) if result['errors'] else 'OK'}")
    lines.append("板块 | 今% | 近3日% | 近5日% | 近10日% | 近20日% | 主力亿(今) | 主力亿(3日) | 主力亿(5日) | 主力亿(10日) | 主力亿(20日) | 回撤% | 量比 | 成交亿 | 来源")
    lines.append("--- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | ---")
    for s in result["sectors"]:
        def f(v, suffix=""):
            return f"{v:.2f}{suffix}" if v is not None else "-"
        src = ",".join(s["sources"])
        dd = f"{s['drawdown'] * 100:.2f}%" if s.get("drawdown") is not None else "-"
        lines.append(
            f"{s['name']} | {f(s['chg_today'], '%')} | {f(s['chg_3d'], '%')} | {f(s['chg_5d'], '%')} | "
            f"{f(s['chg_10d'], '%')} | {f(s['chg_20d'], '%')} | {f(s['main_flow'])} | {f(s['main_3d'])} | "
            f"{f(s['main_5d'])} | {f(s['main_10d'])} | {f(s['main_20d'])} | {dd} | "
            f"{f(s['vol_ratio'])} | {f(s['amount'])} | {src}"
        )
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="trade-trend-analysis 数据层：板块扫描采集")
    parser.add_argument("--date", required=True, help="数据锚定日期 YYYY-MM-DD（最近交易日）")
    parser.add_argument("--format", default="table", choices=["table", "json"])
    # 超时不再暴露：网关统一管理（TIMEOUT=30/次 + 密钥轮换 + 熔断）
    args = parser.parse_args()

    result = run_scan(args.date)
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print(fmt_table(result))
    if result["errors"]:
        print(f"\n⚠️ 部分查询失败: {len(result['errors'])} 条", file=sys.stderr)
        for e in result["errors"]:
            print(f"  - {e}", file=sys.stderr)
    sys.exit(0 if not result["errors"] else 1)


if __name__ == "__main__":
    main()
