"""原始凭据文件的读取与结构定义。

本模块只解析字段、保持原值，不做任何业务判断；空值保持为 None，不自动当零。
所有普通时间字段按本地时间（东八区）解析，支持分、秒精度及纯日期。
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


def parse_time(text: str) -> datetime:
    """解析本地时间：``YYYY-MM-DDTHH:MM[:SS]``，纯日期视为当日零时。"""
    text = text.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"无法解析时间: {text!r}")


def parse_day(text: str) -> date:
    return datetime.strptime(text.strip(), "%Y-%m-%d").date()


def _opt(text: str | None) -> str | None:
    if text is None:
        return None
    text = text.strip()
    return text or None


@dataclass(frozen=True)
class Binding:
    """设备绑定：from 起、until 止（结束点不含），权威本地时间。"""

    device: str
    person: str
    station: str
    start: datetime
    end: datetime | None


@dataclass(frozen=True)
class Calibration:
    """校时回执：offset_sec 加在来源时间上得本地中心时刻，margin_sec 为两侧界限。

    区间针对来源时钟；replaces 指向被本回执替换的旧回执编号。
    """

    id: str
    source: str
    start: datetime
    end: datetime
    offset_sec: int
    margin_sec: int
    reported: date
    replaces: str | None


@dataclass(frozen=True)
class Call:
    """呼叫：observed_at 为来源时钟读数，arrived_at 为到达平台时刻。

    person 为空的记录必须从设备绑定确认住户；ref 为源端转接凭证。
    """

    id: str
    source: str
    person: str | None
    observed: datetime
    arrived: datetime
    ref: str | None
    tier: str


@dataclass(frozen=True)
class Receipt:
    """现场签收：含原始归属引用（call_id）与时间来源（time_source）。"""

    id: str
    call_id: str
    time_source: str
    kind: str  # contact=接听 / visit=上门
    observed: datetime
    reported: date


@dataclass(frozen=True)
class OutageNotice:
    """停服安排：scope 为受影响通道（auto=自动通道），device 为对象设备。"""

    id: str
    scope: str
    device: str
    start: datetime
    end: datetime
    notified_at: datetime
    status: str  # planned / unplanned


@dataclass(frozen=True)
class Review:
    """人工核对回函：separate 拆案 / merge 并案 / void_notice 作废停服凭证。"""

    id: str
    recorded: date
    operation: str
    origin: str
    related: tuple[str, ...]
    evidence: str


@dataclass(frozen=True)
class SlaRow:
    """合同档位：时限单位为秒，抵扣单位为分。"""

    tier: str
    contact_limit: int
    visit_limit: int
    visit_required: bool
    contact_credit: int
    visit_credit: int
    case_cap: int
    station_month_cap: int


@dataclass(frozen=True)
class SignedMonth:
    """已发月单：cutoff 为当时收录凭据的截至时间，金额为分。"""

    document: str
    station: str
    period: str
    cutoff: datetime
    issued_at: datetime
    case_credit_total: int
    applied_credit_total: int


@dataclass(frozen=True)
class SignedObligation:
    """已发月单的义务明细（旧归属，不一定正确）。

    case_credit 为旧台账已计入事件上限、尚未扣减站点月上限的金额。
    """

    document: str
    line: int
    calls: tuple[str, ...]
    kind: str
    disposition: str
    case_credit: int


@dataclass
class Sources:
    bindings: list[Binding]
    calibrations: list[Calibration]
    calls: list[Call]
    receipts: list[Receipt]
    outages: list[OutageNotice]
    reviews: list[Review]
    sla: dict[str, SlaRow]
    signed_months: list[SignedMonth]
    signed_obligations: list[SignedObligation]


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def load_sources(data_dir: str | Path) -> Sources:
    root = Path(data_dir)

    bindings = [
        Binding(
            device=r["device"],
            person=r["person"],
            station=r["station"],
            start=parse_time(r["from"]),
            end=parse_time(r["until"]) if _opt(r["until"]) else None,
        )
        for r in _rows(root / "bindings.csv")
    ]
    calibrations = [
        Calibration(
            id=r["calibration"],
            source=r["source"],
            start=parse_time(r["from"]),
            end=parse_time(r["until"]),
            offset_sec=int(r["offset_sec"]),
            margin_sec=int(r["margin_sec"]),
            reported=parse_day(r["reported_at"]),
            replaces=_opt(r["replaces"]),
        )
        for r in _rows(root / "clock_checks.csv")
    ]
    calls = [
        Call(
            id=r["call"],
            source=r["source"],
            person=_opt(r["person"]),
            observed=parse_time(r["observed_at"]),
            arrived=parse_time(r["arrived_at"]),
            ref=_opt(r["ref"]),
            tier=r["tier"],
        )
        for r in _rows(root / "calls.csv")
    ]
    receipts = [
        Receipt(
            id=r["receipt"],
            call_id=r["call"],
            time_source=r["time_source"],
            kind=r["kind"],
            observed=parse_time(r["observed_at"]),
            reported=parse_day(r["reported_at"]),
        )
        for r in _rows(root / "receipts.csv")
    ]
    outages = [
        OutageNotice(
            id=r["notice"],
            scope=r["scope"],
            device=r["device"],
            start=parse_time(r["from"]),
            end=parse_time(r["until"]),
            notified_at=parse_time(r["notified_at"]),
            status=r["status"],
        )
        for r in _rows(root / "outage_notices.csv")
    ]
    reviews = [
        Review(
            id=r["review"],
            recorded=parse_day(r["recorded_at"]),
            operation=r["operation"],
            origin=r["origin"],
            related=tuple(x for x in r["related"].split(";") if x.strip()) if _opt(r["related"]) else (),
            evidence=r["evidence"],
        )
        for r in _rows(root / "manual_review.csv")
    ]
    sla = {
        r["tier"]: SlaRow(
            tier=r["tier"],
            contact_limit=int(r["contact_limit"]),
            visit_limit=int(r["visit_limit"]),
            visit_required=r["visit_required"].strip().lower() == "yes",
            contact_credit=int(r["contact_credit"]),
            visit_credit=int(r["visit_credit"]),
            case_cap=int(r["case_cap"]),
            station_month_cap=int(r["station_month_cap"]),
        )
        for r in _rows(root / "sla.csv")
    }
    signed_months = [
        SignedMonth(
            document=r["document"],
            station=r["station"],
            period=r["period"],
            cutoff=parse_time(r["cutoff"]),
            issued_at=parse_time(r["issued_at"]),
            case_credit_total=int(r["case_credit_total"]),
            applied_credit_total=int(r["applied_credit_total"]),
        )
        for r in _rows(root / "signed_months.csv")
    ]
    signed_obligations = [
        SignedObligation(
            document=r["document"],
            line=int(r["line"]),
            calls=tuple(x for x in r["calls"].split(";") if x.strip()),
            kind=r["kind"],
            disposition=r["disposition"],
            case_credit=int(r["case_credit"]),
        )
        for r in _rows(root / "signed_obligations.csv")
    ]
    return Sources(
        bindings=bindings,
        calibrations=calibrations,
        calls=calls,
        receipts=receipts,
        outages=outages,
        reviews=reviews,
        sla=sla,
        signed_months=signed_months,
        signed_obligations=signed_obligations,
    )
