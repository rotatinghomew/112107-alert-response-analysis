"""业务核对引擎：归案、时效认定、停服抵扣、上限约束、月单汇总与调整。

核对原则（与承接合同口径一致）：

* 用校时回执的适用时段与误差界限确定来源时间的可能范围；只有住户和响应
  先后都能确认才作认定，不取最有利的时间——计时区间整体越限才认定迟到，
  整体不越限才认定达时，跨越时限或未收到有效签收均为未决。
* 自动归并要求同住户和可核的引用（ref）；人工复核可凭证据拆案或并案；
  分案后签收只用于它实际回应的一案。
* 停服豁免只影响合同指定的自动通道，电话照常计时；事后补传不能把计划外
  停服变成豁免；扣除的暂停区间须有对应呼叫与有效停服凭证，重叠只扣一次。
* 一个呼叫有多份有效同类签收时，以能证明的最早一次响应计时；未收到有效
  签收不等于已知迟到。
* 只将能证明的违约计入应抵扣额，先受同一呼叫（事件）上限约束，再受站点
  月上限约束。未决事项不计入确定差额。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .sources import Call, Review, SlaRow, Sources
from .timeline import BindingBook, ClockBook, TimeRange

CONTACT = "contact"
VISIT = "visit"
KIND_ORDER = {CONTACT: 0, VISIT: 1}
KIND_LABELS = {CONTACT: "接听", VISIT: "上门"}

ON_TIME = "on_time"
LATE = "late"
PENDING_NO_RESPONSE = "pending_no_response"
PENDING_UNCERTAIN = "pending_uncertain"
PENDING_ORDER = "pending_order"
PENDING_RESIDENT = "pending_resident"
PENDING_TIME = "pending_time"

PENDING_STATUSES = {
    PENDING_NO_RESPONSE,
    PENDING_UNCERTAIN,
    PENDING_ORDER,
    PENDING_RESIDENT,
    PENDING_TIME,
}

STATUS_LABELS = {
    ON_TIME: "确定达时",
    LATE: "确定迟到",
    PENDING_NO_RESPONSE: "未决·未收到有效签收",
    PENDING_UNCERTAIN: "未决·时钟误差内无法判定",
    PENDING_ORDER: "未决·响应先后无法确认",
    PENDING_RESIDENT: "未决·住户无法确认",
    PENDING_TIME: "未决·来源时钟无适用校时",
}

# 旧台账处置口径映射到本引擎口径，用于新旧对比
OLD_DISPOSITION_MAP = {
    "late": LATE,
    "on_time": ON_TIME,
    "disputed": "pending",
    "unverified": "pending",
}

CHANNEL_LABELS = {"phone": "电话", "auto": "自动", "other": "其他"}


def channel_of(source: str, devices: set[str]) -> str:
    """呼叫来源所属通道：电话 / 自动（设备）/ 其他。"""
    if source == "PHONE":
        return "phone"
    if source in devices:
        return "auto"
    return "other"


def _union_intervals(intervals: list[tuple[datetime, datetime, str]]) -> list[tuple[datetime, datetime, str]]:
    """合并重叠或相接的暂停区间，重叠只扣一次。返回 (起, 止, 来源编号)。"""
    merged: list[list] = []
    for start, end, ref in sorted(intervals, key=lambda x: (x[0], x[1])):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
            merged[-1][2] = f"{merged[-1][2]},{ref}"
        else:
            merged.append([start, end, ref])
    return [(s, e, r) for s, e, r in merged]


def _overlap_seconds(intervals: list[tuple[datetime, datetime, str]], lo: datetime, hi: datetime) -> float:
    return sum(
        max(0.0, (min(end, hi) - max(start, lo)).total_seconds())
        for start, end, _ in intervals
    )


@dataclass
class Case:
    """一个处置事件：同一响应义务下的一或多个呼叫。"""

    calls: list[Call]
    resident: str | None
    resident_confirmed: bool
    station: str | None
    reviews: list[Review] = field(default_factory=list)

    @property
    def id(self) -> str:
        return ";".join(sorted(c.id for c in self.calls))

    @property
    def call_ids(self) -> set[str]:
        return {c.id for c in self.calls}

    @property
    def first_call(self) -> Call:
        return min(self.calls, key=lambda c: (c.observed, c.id))

    @property
    def tier(self) -> str:
        return self.first_call.tier


@dataclass
class Obligation:
    """一案一类（接听/上门）的时效义务核对结果。"""

    case: Case
    kind: str
    status: str
    limit_sec: int
    channel: str
    call_range: TimeRange | None = None
    response_range: TimeRange | None = None
    counted: tuple[float, float] | None = None  # 扣除暂停后的计时区间（秒）
    pause_sec: tuple[float, float] = (0.0, 0.0)  # 可扣暂停区间（秒）
    credit_raw: int = 0  # 确定迟到的原始抵扣（分）
    credit_potential: int = 0  # 未决若成立可抵扣（分）
    credit_applied: int = 0  # 事件上限分摊后（分）
    receipts: list[str] = field(default_factory=list)
    calibrations: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)

    @property
    def pending(self) -> bool:
        return self.status in PENDING_STATUSES

    @property
    def id(self) -> str:
        return f"{self.case.id}:{self.kind}"

    @property
    def evidence(self) -> list[str]:
        refs = list(self.receipts) + list(self.calibrations) + list(self.notices)
        refs += [r.id for r in self.case.reviews]
        return sorted(set(refs))


@dataclass
class Rollup:
    """站点-月份汇总：应纳入、确定达时/迟到、未决与抵扣金额。"""

    station: str
    period: str
    obligations: list[Obligation]
    station_cap: int
    case_caps: dict[str, int]  # 事件 id -> 同一呼叫上限（分）

    @staticmethod
    def _counts(obligations: list[Obligation]) -> dict[str, int]:
        return {
            "included": len(obligations),
            "on_time": sum(1 for o in obligations if o.status == ON_TIME),
            "late": sum(1 for o in obligations if o.status == LATE),
            "pending": sum(1 for o in obligations if o.pending),
        }

    @property
    def counts(self) -> dict[str, int]:
        return self._counts(self.obligations)

    def counts_by(self, key: str) -> dict[str, dict[str, int]]:
        groups: dict[str, list[Obligation]] = {}
        for o in self.obligations:
            label = o.kind if key == "kind" else o.channel
            groups.setdefault(label, []).append(o)
        return {label: self._counts(obs) for label, obs in sorted(groups.items())}

    @property
    def rate_range(self) -> tuple[float, float] | None:
        """违约率范围：确定迟到/应纳入 ~ （确定迟到+未决）/应纳入。"""
        c = self.counts
        if not c["included"]:
            return None
        return (c["late"] / c["included"], (c["late"] + c["pending"]) / c["included"])

    @property
    def case_credit_total(self) -> int:
        """能证明的违约在事件上限后的合计（分）。"""
        return sum(o.credit_applied for o in self.obligations)

    @property
    def applied_total(self) -> int:
        """再受站点月上限约束后的应抵扣额（分）。"""
        return min(self.case_credit_total, self.station_cap)

    @property
    def potential_case_credit_total(self) -> int:
        """未决事项若全部改判迟到，事件上限后的最大合计（分）。"""
        by_case: dict[str, list[Obligation]] = {}
        for o in self.obligations:
            by_case.setdefault(o.case.id, []).append(o)
        total = 0
        for case_id, obs in by_case.items():
            raw = sum(o.credit_raw + o.credit_potential for o in obs)
            total += min(raw, self.case_caps[case_id])
        return total

    @property
    def potential_applied_total(self) -> int:
        return min(self.potential_case_credit_total, self.station_cap)


@dataclass
class ObligationDiff:
    """旧归属义务明细与当前可证明口径的对比。"""

    document: str
    line: int
    calls: tuple[str, ...]
    kind: str
    old_disposition: str
    old_credit: int
    current: list[Obligation]
    changed_attribution: bool
    changed_status: bool
    credit_delta: int  # 事件上限后口径的差额（分）

    @property
    def economic(self) -> bool:
        return self.credit_delta != 0

    @property
    def evidence(self) -> list[str]:
        refs: set[str] = set()
        for o in self.current:
            refs.update(o.evidence)
        return sorted(refs)


@dataclass
class Adjustment:
    """相对已向承接方确认累计金额的调整（经上限约束，未决不计入）。"""

    station: str
    period: str
    documents: list[str]
    confirmed_cumulative: int  # 已发月单向承接方确认的累计应抵扣（分）
    current_case_credit: int
    current_applied: int
    delta: int  # 应发调整（分），负数为退回
    diffs: list[ObligationDiff]
    pending_obligations: list[Obligation]
    potential_applied: int  # 未决全部成立时的应抵扣上限（分）
    notes: list[str]

    @property
    def references(self) -> list[str]:
        refs: set[str] = set()
        for d in self.diffs:
            refs.update(d.evidence)
        return sorted(refs)


@dataclass
class TodoItem:
    case: Case
    missing: str  # contact=尚未联系 / visit=已联系但缺上门核实
    call_range: TimeRange | None
    note: str


class Engine:
    """以当前可得凭据（或指定截至时点）核对某数据目录下的全部记录。"""

    def __init__(self, sources: Sources, as_of: datetime | None = None):
        self.sources = sources
        self.as_of = as_of
        as_of_day = as_of.date() if as_of else None
        self.clock = ClockBook(sources.calibrations, as_of_day)
        self.bindings = BindingBook(sources.bindings)
        self.devices = {b.device for b in sources.bindings}
        # 证据可得性：呼叫按到达平台时刻、签收与校时按上报日、复核按记录日过滤
        self.calls = [c for c in sources.calls if as_of is None or c.arrived <= as_of]
        self.receipts = [r for r in sources.receipts if as_of_day is None or r.reported <= as_of_day]
        self.reviews = sorted(
            (r for r in sources.reviews if as_of_day is None or r.recorded <= as_of_day),
            key=lambda r: (r.recorded, r.id),
        )
        self.voided_notices = {r.origin for r in self.reviews if r.operation == "void_notice"}
        self.valid_notices = [n for n in sources.outages if self._notice_valid(n)]
        caps = {row.station_month_cap for row in sources.sla.values()}
        # 站点月上限为合同参数；各档位不一致时取最小（偏保守，不取最有利）
        self.station_month_cap = min(caps)
        self._call_ranges: dict[str, TimeRange | None] = {}
        self._call_calibrations: dict[str, str] = {}
        self.cases = self._build_cases()
        self.obligations = self._evaluate()

    # ------------------------------------------------------------------ 归案

    def _build_cases(self) -> list[Case]:
        residents: dict[str, str | None] = {}
        confirmed: dict[str, bool] = {}
        stations: dict[str, str | None] = {}
        for call in self.calls:
            converted = self.clock.convert(call.source, call.observed)
            self._call_ranges[call.id] = converted[0] if converted else None
            self._call_calibrations[call.id] = converted[1].id if converted else ""
            if call.person:
                residents[call.id] = call.person
                confirmed[call.id] = True
            elif converted is not None:
                person, _ = self.bindings.confirmed_person(call.source, converted[0])
                residents[call.id] = person
                confirmed[call.id] = person is not None
            else:
                residents[call.id] = None
                confirmed[call.id] = False
            stations[call.id] = self._station_for(call, residents[call.id], self._call_ranges[call.id])

        # 自动归并：同住户且引用可核（同一 ref 被多条记录引用）
        groups: list[set[str]] = []
        by_ref: dict[tuple[str, str], set[str]] = {}
        for call in self.calls:
            if call.ref and confirmed[call.id]:
                by_ref.setdefault((residents[call.id], call.ref), set()).add(call.id)
        merged_into: dict[str, set[str]] = {}

        def group_of(cid: str) -> set[str]:
            for g in groups:
                if cid in g:
                    return g
            return merged_into.setdefault(cid, {cid})

        for ids in by_ref.values():
            if len(ids) > 1:
                target: set[str] = set()
                for cid in ids:
                    g = group_of(cid)
                    if g in groups:
                        groups.remove(g)
                    target |= g
                groups.append(target)

        # 人工复核：按记录先后应用，可凭证据拆案或并案
        involved: dict[str, list[Review]] = {}
        for review in self.reviews:
            if review.operation == "separate":
                # 拆案：origin 自立一案，与其余呼叫（含 related）分开
                g = group_of(review.origin)
                if review.origin in g and len(g) > 1:
                    g.discard(review.origin)
                    groups.append({review.origin})
                for cid in (review.origin, *review.related):
                    involved.setdefault(cid, []).append(review)
            elif review.operation == "merge":
                targets = [t for t in (review.origin, *review.related)]
                merged: set[str] = set()
                for t in targets:
                    g = group_of(t)
                    if g in groups:
                        groups.remove(g)
                    merged |= g
                groups.append(merged)
                for cid in targets:
                    involved.setdefault(cid, []).append(review)

        # 未参与任何归并的呼叫各自成案
        grouped = set().union(*groups) if groups else set()
        for call in self.calls:
            if call.id not in grouped:
                groups.append({call.id})

        call_by_id = {c.id: c for c in self.calls}
        cases: list[Case] = []
        for ids in groups:
            calls = sorted((call_by_id[i] for i in ids), key=lambda c: (c.observed, c.id))
            res_values = {residents[c.id] for c in calls if confirmed[c.id]}
            resident = res_values.pop() if len(res_values) == 1 else None
            station_values = {stations[c.id] for c in calls if stations[c.id]}
            station = station_values.pop() if len(station_values) == 1 else None
            reviews: list[Review] = []
            for c in calls:
                for r in involved.get(c.id, []):
                    if r not in reviews:
                        reviews.append(r)
            cases.append(
                Case(
                    calls=calls,
                    resident=resident,
                    resident_confirmed=resident is not None and all(confirmed[c.id] for c in calls),
                    station=station,
                    reviews=sorted(reviews, key=lambda r: (r.recorded, r.id)),
                )
            )
        return sorted(cases, key=lambda cs: (cs.first_call.observed, cs.id))

    def _station_for(self, call: Call, resident: str | None, tr: TimeRange | None) -> str | None:
        if call.source in self.devices:
            if tr is None:
                return None
            _, station = self.bindings.confirmed_person(call.source, tr)
            return station
        if resident and tr is not None:
            stations = self.bindings.stations_for_person(resident, tr)
            if len(stations) == 1:
                return stations.pop()
        return None

    # ------------------------------------------------------------------ 停服

    def _notice_valid(self, notice) -> bool:
        if notice.id in self.voided_notices:
            return False  # 人工复核作废
        if notice.status != "planned":
            return False  # 计划外停服不免除
        if notice.notified_at > notice.start:
            return False  # 事后补传不能变成豁免
        return True

    def _pauses_for(self, case: Case) -> list[tuple[datetime, datetime, str]]:
        """本案可扣的暂停区间：须有对应呼叫与有效停服凭证，且只影响对应通道。"""
        intervals: list[tuple[datetime, datetime, str]] = []
        for call in case.calls:
            channel = channel_of(call.source, self.devices)
            for notice in self.valid_notices:
                if notice.device != call.source:
                    continue
                if notice.scope == "auto" and channel != "auto":
                    continue  # 电话照常计时
                if notice.scope == "phone" and channel != "phone":
                    continue
                intervals.append((notice.start, notice.end, notice.id))
        return _union_intervals(intervals)

    # ------------------------------------------------------------------ 认定

    def _case_call_range(self, case: Case) -> TimeRange | None:
        """本案呼叫时刻的可能范围：取各成员呼叫中可证明的最早一次。"""
        ranges = [self._call_ranges[c.id] for c in case.calls]
        if any(r is None for r in ranges):
            return None
        return TimeRange(min(r.lo for r in ranges), min(r.hi for r in ranges))

    def _evaluate(self) -> list[Obligation]:
        obligations: list[Obligation] = []
        for case in self.cases:
            sla = self.sources.sla.get(case.tier)
            if sla is None:
                raise ValueError(f"合同未载明类别: {case.tier!r}")
            kinds = [CONTACT] + ([VISIT] if sla.visit_required else [])
            for kind in kinds:
                obligations.append(self._evaluate_one(case, kind, sla))
        # 事件上限：先受同一呼叫上限约束（接听在先、上门在后分摊）
        for case in self.cases:
            sla = self.sources.sla[case.tier]
            remaining = sla.case_cap
            late = [o for o in obligations if o.case is case and o.status == LATE]
            for o in sorted(late, key=lambda x: KIND_ORDER[x.kind]):
                o.credit_applied = min(o.credit_raw, remaining)
                remaining -= o.credit_applied
        return obligations

    def _evaluate_one(self, case: Case, kind: str, sla: SlaRow) -> Obligation:
        limit = sla.contact_limit if kind == CONTACT else sla.visit_limit
        credit = sla.contact_credit if kind == CONTACT else sla.visit_credit
        channel = channel_of(case.first_call.source, self.devices)
        call_range = self._case_call_range(case)
        calibrations = {self._call_calibrations[c.id] for c in case.calls if self._call_calibrations[c.id]}
        base = Obligation(case=case, kind=kind, status="", limit_sec=limit, channel=channel,
                          call_range=call_range)
        if not case.resident_confirmed:
            base.status = PENDING_RESIDENT
            base.credit_potential = credit
            base.calibrations = sorted(calibrations)
            return base
        if call_range is None:
            base.status = PENDING_TIME
            base.credit_potential = credit
            base.calibrations = sorted(calibrations)
            return base

        # 分案后签收只用于它实际回应的一案：按签收原始归属引用归集
        receipts = [r for r in self.receipts if r.call_id in case.call_ids and r.kind == kind]
        response_lo: datetime | None = None
        response_hi: datetime | None = None
        used: list[str] = []
        for receipt in receipts:
            converted = self.clock.convert(receipt.time_source, receipt.observed)
            if converted is None:
                continue  # 时间来源无适用校时，该签收不可用于计时
            tr, cal = converted
            used.append(receipt.id)
            calibrations.add(cal.id)
            # 多份有效同类签收：以能证明的最早一次响应计时
            response_lo = tr.lo if response_lo is None else min(response_lo, tr.lo)
            response_hi = tr.hi if response_hi is None else min(response_hi, tr.hi)
        base.receipts = sorted(used)
        base.calibrations = sorted(calibrations)
        if response_lo is None or response_hi is None:
            base.status = PENDING_NO_RESPONSE  # 未收到有效签收不等于已知迟到
            base.credit_potential = credit
            return base
        base.response_range = TimeRange(response_lo, response_hi)

        pauses = self._pauses_for(case)
        base.notices = sorted({ref for _, _, ref in pauses})
        p_max = _overlap_seconds(pauses, call_range.lo, response_hi)
        p_min = _overlap_seconds(pauses, call_range.hi, response_lo) if call_range.hi < response_lo else 0.0
        base.pause_sec = (p_min, p_max)

        e_lo = (response_lo - call_range.hi).total_seconds()
        e_hi = (response_hi - call_range.lo).total_seconds()
        counted = (e_lo - p_max, e_hi - p_min)
        base.counted = counted
        if e_lo < 0:
            base.status = PENDING_ORDER  # 响应先后无法确认，不作认定
        elif counted[0] > limit:
            base.status = LATE  # 计时区间整体越限：确定迟到
        elif counted[1] <= limit:
            base.status = ON_TIME  # 整体不越限：确定达时
        else:
            base.status = PENDING_UNCERTAIN  # 跨越时限：不取最有利的时间
        if base.status == LATE:
            base.credit_raw = credit
        elif base.pending:
            base.credit_potential = credit
        return base

    # ------------------------------------------------------------------ 汇总

    @staticmethod
    def _period_of(case: Case, call_range: TimeRange | None) -> str | None:
        """呼叫可能时段整体落在同一自然月时才归入该月。"""
        if call_range is None:
            return None
        if (call_range.lo.year, call_range.lo.month) != (call_range.hi.year, call_range.hi.month):
            return None
        return f"{call_range.lo.year:04d}-{call_range.lo.month:02d}"

    def _case_caps(self) -> dict[str, int]:
        return {case.id: self.sources.sla[case.tier].case_cap for case in self.cases}

    def rollup(self, station: str, period: str, channel: str | None = None) -> Rollup:
        selected = []
        for o in self.obligations:
            if o.case.station != station:
                continue
            if self._period_of(o.case, o.call_range) != period:
                continue
            if channel and o.channel != channel:
                continue
            selected.append(o)
        return Rollup(
            station=station,
            period=period,
            obligations=selected,
            station_cap=self.station_month_cap,
            case_caps=self._case_caps(),
        )

    def station_periods(self) -> list[tuple[str, str]]:
        pairs = {
            (o.case.station, self._period_of(o.case, o.call_range))
            for o in self.obligations
            if o.case.station and self._period_of(o.case, o.call_range)
        }
        return sorted(pairs)

    # ------------------------------------------------------------------ 待办

    def todos(self, station: str | None = None) -> dict[str, list[TodoItem]]:
        """值班台待办：尚未联系 / 已联系但缺上门核实（按当前凭据即时调整）。"""
        uncontacted: list[TodoItem] = []
        visit_unverified: list[TodoItem] = []
        for case in self.cases:
            if station and case.station != station:
                continue
            kinds = {r.kind for r in self.receipts if r.call_id in case.call_ids}
            sla = self.sources.sla[case.tier]
            call_range = self._case_call_range(case)
            note = "；".join(f"{r.id} {r.evidence}" for r in case.reviews)
            if CONTACT not in kinds:
                uncontacted.append(TodoItem(case=case, missing=CONTACT, call_range=call_range, note=note))
            elif sla.visit_required and VISIT not in kinds:
                visit_unverified.append(TodoItem(case=case, missing=VISIT, call_range=call_range, note=note))
        key = lambda t: (t.call_range.lo if t.call_range else datetime.max, t.case.id)
        return {"uncontacted": sorted(uncontacted, key=key), "visit_unverified": sorted(visit_unverified, key=key)}

    # ------------------------------------------------------------------ 调整

    def obligation_diffs(self, station: str, period: str) -> list[ObligationDiff]:
        """旧归属义务明细与当前可证明口径的逐行对比（含无经济差额的改判）。"""
        rollup = self.rollup(station, period)
        diffs: list[ObligationDiff] = []
        docs = [d for d in self.sources.signed_months if d.station == station and d.period == period]
        doc_ids = {d.document for d in docs}
        for line in self.sources.signed_obligations:
            if line.document not in doc_ids:
                continue
            current = [
                o for o in rollup.obligations
                if o.kind == line.kind and o.case.call_ids & set(line.calls)
            ]
            old_status = OLD_DISPOSITION_MAP.get(line.disposition, line.disposition)
            same_attribution = len(current) == 1 and current[0].case.call_ids == set(line.calls)
            same_status = (
                len(current) == 1
                and (current[0].status if not current[0].pending else "pending") == old_status
            )
            new_credit = sum(o.credit_applied for o in current)
            delta = new_credit - line.case_credit
            if same_attribution and same_status and delta == 0:
                continue  # 归属、处置与金额均未变
            diffs.append(ObligationDiff(
                document=line.document,
                line=line.line,
                calls=line.calls,
                kind=line.kind,
                old_disposition=line.disposition,
                old_credit=line.case_credit,
                current=current,
                changed_attribution=not same_attribution,
                changed_status=not same_status,
                credit_delta=delta,
            ))
        return diffs

    def adjustment(self, station: str, period: str) -> Adjustment:
        """相对已向承接方确认累计金额、经上限约束后的调整计算。

        旧月单触顶时，单个义务改判的金额不直接退回：须重算全月，
        只就相对已确认累计仍有变化的部分发出调整；未决不计入差额。
        """
        rollup = self.rollup(station, period)
        docs = [d for d in self.sources.signed_months if d.station == station and d.period == period]
        confirmed = sum(d.applied_credit_total for d in docs)
        diffs = self.obligation_diffs(station, period)
        notes: list[str] = []
        for d in docs:
            if d.case_credit_total > self.station_month_cap:
                notes.append(
                    f"{d.document} 事件上限后 {d.case_credit_total} 分已触站点月上限 "
                    f"{self.station_month_cap} 分，单个义务改判金额不直接退回，按全月重算差额"
                )
        for review in self.reviews:
            if review.operation == "void_notice":
                used = any(review.origin in o.notices for o in self.obligations)
                if not used:
                    notes.append(f"{review.id}：{review.origin} 停服凭证作废（{review.evidence}），未影响已确认金额")
        pending = [o for o in rollup.obligations if o.pending]
        return Adjustment(
            station=station,
            period=period,
            documents=[d.document for d in docs],
            confirmed_cumulative=confirmed,
            current_case_credit=rollup.case_credit_total,
            current_applied=rollup.applied_total,
            delta=rollup.applied_total - confirmed,
            diffs=diffs,
            pending_obligations=pending,
            potential_applied=rollup.potential_applied_total,
            notes=notes,
        )
