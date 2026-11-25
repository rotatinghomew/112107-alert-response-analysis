"""来源时钟到本地时间的换算，以及设备绑定的归属确认。

原则：用校时回执给出的适用时段和误差界限确定来源时间的可能范围；
被替换的回执自动失效；只取当前可得的回执（可按截至日过滤）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .sources import Binding, Calibration


@dataclass(frozen=True)
class TimeRange:
    """某一来源时刻在本地时间轴上的可能范围（闭区间）。"""

    lo: datetime
    hi: datetime

    def __str__(self) -> str:  # 便于报表展示
        return f"{self.lo:%Y-%m-%d %H:%M:%S}~{self.hi:%H:%M:%S}"


class ClockBook:
    """按适用时段与误差界限换算来源时间。

    as_of 之后的回执视为尚未收到；同一来源同一时段若有多份回执，
    被其他候选回执 replaces 指向的旧回执失效，取替换链上最新的一份。
    """

    def __init__(self, calibrations: list[Calibration], as_of: date | None = None):
        self._cals = [c for c in calibrations if as_of is None or c.reported <= as_of]

    def convert(self, source: str, observed: datetime) -> tuple[TimeRange, Calibration] | None:
        """返回（本地可能范围, 所用回执）；无适用回执时返回 None。"""
        candidates = [c for c in self._cals if c.source == source and c.start <= observed < c.end]
        if not candidates:
            return None
        replaced = {c.replaces for c in candidates if c.replaces}
        current = [c for c in candidates if c.id not in replaced] or candidates
        chosen = max(current, key=lambda c: (c.reported, c.id))
        center = observed + timedelta(seconds=chosen.offset_sec)
        margin = timedelta(seconds=chosen.margin_sec)
        return TimeRange(center - margin, center + margin), chosen


class BindingBook:
    """设备绑定查询：结束点不含。"""

    def __init__(self, bindings: list[Binding]):
        self._by_device: dict[str, list[Binding]] = {}
        self._by_person: dict[str, list[Binding]] = {}
        for b in bindings:
            self._by_device.setdefault(b.device, []).append(b)
            self._by_person.setdefault(b.person, []).append(b)

    @staticmethod
    def _overlaps(binding: Binding, tr: TimeRange) -> bool:
        # 绑定覆盖 [start, end)；可能范围为闭区间（可为零宽度的精确时刻）
        return binding.start <= tr.hi and (binding.end is None or binding.end > tr.lo)

    def confirmed_person(self, device: str, tr: TimeRange) -> tuple[str | None, str | None]:
        """整个可能时段内设备唯一且连续绑定的人员与站点。

        可能时段跨绑定切换或出现未绑定空隙时，住户无法确认，返回 (None, None)。
        """
        overlaps = [b for b in self._by_device.get(device, []) if self._overlaps(b, tr)]
        persons = {b.person for b in overlaps}
        if len(persons) != 1:
            return None, None
        # 覆盖性检查：绑定区间须无空隙地覆盖整个可能时段
        cursor = tr.lo
        for b in sorted(overlaps, key=lambda x: x.start):
            if b.start > cursor:
                return None, None
            if b.end is None:
                cursor = tr.hi
                break
            cursor = max(cursor, b.end)
            if cursor >= tr.hi:
                break
        if cursor < tr.hi:
            return None, None
        bound = overlaps[0]
        return bound.person, bound.station

    def stations_for_person(self, person: str, tr: TimeRange) -> set[str]:
        """某住户在可能时段内绑定记录涉及的站点集合。"""
        return {
            b.station
            for b in self._by_person.get(person, [])
            if self._overlaps(b, tr)
        }
