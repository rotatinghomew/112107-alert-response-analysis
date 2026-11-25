"""居家呼叫响应时效核对与合同处理。

承接合同对居家呼叫分别约定接听和上门的时限。本包按校时回执给出的适用
时段与误差界限确定来源时间的可能范围，只在住户和响应先后都能确认时作
相应认定；用于运营时效核对与合同处理，不从读数判断住户的诊疗需要。
"""
from .engine import Engine
from .sources import Sources, load_sources

__all__ = ["Engine", "Sources", "load_sources"]
