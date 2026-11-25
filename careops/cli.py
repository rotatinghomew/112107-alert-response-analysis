"""值班台、合同员与排班人员的查询入口。

子命令：
  todos        值班台待办：尚未联系 / 已联系但缺上门核实
  summary      站点-月份（可分通道）时效汇总与应抵扣额，用于出单
  obligations  原签月单旧归属义务明细与当前可证明口径的逐行对比
  adjustment   相对已向承接方确认累计金额、经上限约束后的调整单
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime

from .engine import (
    CHANNEL_LABELS,
    CONTACT,
    KIND_LABELS,
    STATUS_LABELS,
    Engine,
)
from .sources import load_sources, parse_time


def _fmt_range(seconds: tuple[float, float] | None) -> str:
    if seconds is None:
        return "—"
    lo, hi = seconds
    if lo == hi:
        return f"{lo:.0f}"
    return f"{lo:.0f}~{hi:.0f}"


def _fmt_pct(rate: tuple[float, float] | None) -> str:
    if rate is None:
        return "—"
    return f"{rate[0] * 100:.1f}% ~ {rate[1] * 100:.1f}%"


def _obligation_line(o) -> str:
    parts = [
        f"    {o.case.id} {KIND_LABELS[o.kind]}（{o.case.tier}/{CHANNEL_LABELS.get(o.channel, o.channel)}）",
        STATUS_LABELS[o.status],
    ]
    if o.counted is not None:
        parts.append(f"计时 {_fmt_range(o.counted)} 秒 / 时限 {o.limit_sec} 秒")
    if o.pause_sec != (0.0, 0.0):
        parts.append(f"扣暂停 {_fmt_range(o.pause_sec)} 秒")
    if o.status == "late":
        parts.append(f"抵扣 {o.credit_raw} 分（事件上限后 {o.credit_applied} 分）")
    elif o.pending and o.credit_potential:
        parts.append(f"若成立可抵 {o.credit_potential} 分")
    refs = o.evidence
    if refs:
        parts.append(f"凭据 {'/'.join(refs)}")
    return " ".join(parts)


def cmd_todos(engine: Engine, args) -> int:
    lists = engine.todos(station=args.station)
    scope = args.station or "全部站点"
    print(f"值班台待办 · {scope}" + (f"（截至 {args.as_of}）" if args.as_of else "（按当前凭据）"))
    print(f"\n尚未联系（{len(lists['uncontacted'])}）：")
    for t in lists["uncontacted"]:
        case = t.case
        rng = str(t.call_range) if t.call_range else "时段未定"
        line = f"  {case.id} 住户{case.resident or '待确认'} {case.station or '站点待确认'} {case.tier} 呼叫可能时段 {rng}"
        if t.note:
            line += f"；{t.note}"
        print(line)
    if not lists["uncontacted"]:
        print("  无")
    print(f"\n已联系但缺上门核实（{len(lists['visit_unverified'])}）：")
    for t in lists["visit_unverified"]:
        case = t.case
        rng = str(t.call_range) if t.call_range else "时段未定"
        line = f"  {case.id} 住户{case.resident or '待确认'} {case.station or '站点待确认'} {case.tier} 呼叫可能时段 {rng}"
        if t.note:
            line += f"；{t.note}"
        print(line)
    if not lists["visit_unverified"]:
        print("  无")
    return 0


def _print_rollup(engine: Engine, station: str, period: str, channel: str | None) -> None:
    rollup = engine.rollup(station, period, channel=channel)
    label = CHANNEL_LABELS.get(channel, "全部通道") if channel else "全部通道"
    c = rollup.counts
    print(f"\n== {station} · {period} · {label} ==")
    print(f"应纳入 {c['included']} 次：确定达时 {c['on_time']}，确定迟到 {c['late']}，未决 {c['pending']}")
    print(f"违约率范围：{_fmt_pct(rollup.rate_range)}")
    by_kind = rollup.counts_by("kind")
    if by_kind:
        print("分时限类别：")
        for kind, cc in by_kind.items():
            print(f"  {KIND_LABELS[kind]}：应纳入 {cc['included']}，达时 {cc['on_time']}，迟到 {cc['late']}，未决 {cc['pending']}")
    if channel is None:
        by_channel = rollup.counts_by("channel")
        if len(by_channel) > 1:
            print("分通道：")
            for ch, cc in by_channel.items():
                rate = (cc["late"] / cc["included"], (cc["late"] + cc["pending"]) / cc["included"]) if cc["included"] else None
                print(
                    f"  {CHANNEL_LABELS.get(ch, ch)}：应纳入 {cc['included']}，达时 {cc['on_time']}，"
                    f"迟到 {cc['late']}，未决 {cc['pending']}，违约率 {_fmt_pct(rate)}"
                )
    pending = [o for o in rollup.obligations if o.pending]
    if pending:
        print("未决事项（不计入确定差额）：")
        for o in pending:
            print(_obligation_line(o))
    print(
        f"可证明抵扣：事件上限后 {rollup.case_credit_total} 分，"
        f"站点月上限 {rollup.station_cap} 分，应抵扣 {rollup.applied_total} 分"
    )
    if pending:
        print(f"未决若全部成立：事件上限后至多 {rollup.potential_case_credit_total} 分，应抵扣至多 {rollup.potential_applied_total} 分")


def cmd_summary(engine: Engine, args) -> int:
    pairs = (
        [(args.station, args.period)]
        if args.station and args.period
        else [
            (s, p)
            for s, p in engine.station_periods()
            if (not args.station or s == args.station) and (not args.period or p == args.period)
        ]
    )
    if not pairs:
        print("无符合条件的记录")
        return 1
    for station, period in pairs:
        _print_rollup(engine, station, period, args.channel)
    return 0


def cmd_obligations(engine: Engine, args) -> int:
    docs = [d for d in engine.sources.signed_months if d.station == args.station and d.period == args.period]
    if not docs:
        print(f"{args.station} {args.period} 无已签月单")
        return 1
    rollup = engine.rollup(args.station, args.period)
    doc_ids = {d.document for d in docs}
    for d in docs:
        print(
            f"\n== 原签月单 {d.document} · {d.station} {d.period} =="
            f"（截至 {d.cutoff:%Y-%m-%d %H:%M} 收录，{d.issued_at:%Y-%m-%d %H:%M} 发出）"
        )
        print(f"原单口径：事件上限后 {d.case_credit_total} 分，站点月上限后确认 {d.applied_credit_total} 分")
    print(f"当前可证明：事件上限后 {rollup.case_credit_total} 分，应抵扣 {rollup.applied_total} 分")
    print("\n旧归属义务明细 vs 当前可证明口径：")
    for line in engine.sources.signed_obligations:
        if line.document not in doc_ids:
            continue
        calls = ";".join(line.calls)
        print(f"  [{line.document} 第{line.line}行] {calls} {KIND_LABELS.get(line.kind, line.kind)}"
              f" 旧处置 {line.disposition} {line.case_credit} 分")
        current = [
            o for o in rollup.obligations
            if o.kind == line.kind and o.case.call_ids & set(line.calls)
        ]
        if not current:
            print("      当前无对应义务")
        for o in current:
            print(f"  →{_obligation_line(o).strip()}")
    return 0


def cmd_adjustment(engine: Engine, args) -> int:
    adj = engine.adjustment(args.station, args.period)
    docs = "、".join(adj.documents) if adj.documents else "（无已签月单）"
    print(f"调整单 · {adj.station} {adj.period} · 相对已确认月单 {docs}")
    print(f"已向承接方确认累计：{adj.confirmed_cumulative} 分")
    print(f"当前可证明：事件上限后 {adj.current_case_credit} 分，站点月上限后 {adj.current_applied} 分")
    if adj.delta:
        print(f"应发调整：{adj.delta:+d} 分（凭据 {'/'.join(adj.references) or '—'}）")
    else:
        print("应发调整：0 分（无经济差额）")
    if adj.diffs:
        print("\n改判明细（含无经济差额的改判）：")
        for d in adj.diffs:
            calls = ";".join(d.calls)
            head = f"  [{d.document} 第{d.line}行] {calls} {KIND_LABELS.get(d.kind, d.kind)}：旧 {d.old_disposition} {d.old_credit} 分"
            changes = []
            if d.changed_attribution:
                changes.append("归属变化")
            if d.changed_status:
                changes.append("处置变化")
            tail = f"事件上限后差额 {d.credit_delta:+d} 分" if d.economic else "无经济差额"
            print(f"{head} → {'、'.join(changes) or '无变化'}，{tail}")
            for o in d.current:
                print(f"  →{_obligation_line(o).strip()}")
            if d.evidence:
                print(f"      依据 {'/'.join(d.evidence)}")
    if adj.pending_obligations:
        print("\n未决事项（不冒充确定差额，不计入本次调整）：")
        for o in adj.pending_obligations:
            print(_obligation_line(o))
        extra = adj.potential_applied - adj.current_applied
        print(f"未决若全部成立，应抵扣至多 {adj.potential_applied} 分（较当前至多再抵 {extra} 分），待证据到达后另行调整")
    if adj.notes:
        print("\n备注：")
        for note in adj.notes:
            print(f"  · {note}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="careops", description="居家呼叫响应时效核对与合同处理")
    parser.add_argument("--data", default="data", help="凭据目录（默认 ./data）")
    parser.add_argument("--as-of", default=None, help="凭据截至时点，如 2026-04-02T18:00（默认当前全部凭据）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("todos", help="值班台待办：尚未联系 / 已联系但缺上门核实")
    p.add_argument("--station", default=None)
    p.set_defaults(func=cmd_todos)

    p = sub.add_parser("summary", help="站点-月份时效汇总与应抵扣额（出单口径）")
    p.add_argument("--station", default=None)
    p.add_argument("--period", default=None, help="如 2026-03")
    p.add_argument("--channel", choices=["phone", "auto"], default=None)
    p.set_defaults(func=cmd_summary)

    p = sub.add_parser("obligations", help="原签月单旧归属义务明细 vs 当前可证明口径")
    p.add_argument("--station", required=True)
    p.add_argument("--period", required=True)
    p.set_defaults(func=cmd_obligations)

    p = sub.add_parser("adjustment", help="相对已确认累计金额的调整单")
    p.add_argument("--station", required=True)
    p.add_argument("--period", required=True)
    p.set_defaults(func=cmd_adjustment)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    as_of = parse_time(args.as_of) if args.as_of else None
    engine = Engine(load_sources(args.data), as_of=as_of)
    return args.func(engine, args)


if __name__ == "__main__":
    sys.exit(main())
