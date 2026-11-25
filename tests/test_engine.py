"""业务核对引擎测试：以合成凭据验证时效认定、上限约束与调整计算。"""
import unittest
from datetime import datetime
from pathlib import Path

from careops import Engine, load_sources
from careops.engine import (
    CONTACT,
    LATE,
    ON_TIME,
    PENDING_NO_RESPONSE,
    PENDING_UNCERTAIN,
    VISIT,
)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def make_engine(as_of=None):
    return Engine(load_sources(DATA), as_of=as_of)


class CaseFormationTests(unittest.TestCase):
    def setUp(self):
        self.engine = make_engine()
        self.cases = {c.id: c for c in self.engine.cases}

    def test_review_separates_merged_calls(self):
        # T1、T2 同住户同引用 Z3 本会自动归并，Q1 凭录音证据拆案
        self.assertIn("T1", self.cases)
        self.assertIn("T2", self.cases)
        self.assertEqual(self.cases["T1"].call_ids, {"T1"})
        self.assertEqual(self.cases["T2"].call_ids, {"T2"})
        self.assertIn("Q1", [r.id for r in self.cases["T1"].reviews])

    def test_unreferenced_call_stays_single(self):
        # T3 无可核引用，不与相近呼叫归并
        self.assertEqual(self.cases["T3"].call_ids, {"T3"})

    def test_resident_confirmed_from_binding(self):
        # T1（D1）经 C4 校时后可能时段 10:03:05~10:03:15，完全落在 D1→H1 绑定内
        self.assertTrue(self.cases["T1"].resident_confirmed)
        self.assertEqual(self.cases["T1"].resident, "H1")
        self.assertEqual(self.cases["T3"].resident, "H1")
        for case in self.cases.values():
            self.assertEqual(case.station, "槐园")


class ClockAndNoticeTests(unittest.TestCase):
    def setUp(self):
        self.engine = make_engine()

    def test_replaced_calibration_superseded(self):
        # C4 已替换 C1：D1 在 2026-03-05T10:04:40 的读数按 C4 换算
        result = self.engine.clock.convert("D1", datetime(2026, 3, 5, 10, 4, 40))
        tr, cal = result
        self.assertEqual(cal.id, "C4")
        self.assertEqual((tr.lo, tr.hi), (datetime(2026, 3, 5, 10, 3, 5), datetime(2026, 3, 5, 10, 3, 15)))

    def test_calibration_as_of_cutoff(self):
        # 月单截至 2026-04-02T18:00 时 C4（2026-04-06 上报）尚未到达，仍用 C1
        engine = make_engine(as_of=datetime(2026, 4, 2, 18, 0))
        _, cal = engine.clock.convert("D1", datetime(2026, 3, 5, 10, 4, 40))
        self.assertEqual(cal.id, "C1")

    def test_notice_validity(self):
        valid = {n.id for n in self.engine.valid_notices}
        self.assertEqual(valid, {"M1"})  # M2 被 Q2 作废；M3 计划外且事后补传


class DeterminationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = make_engine()
        cls.obligations = {o.id: o for o in cls.engine.obligations}

    def test_no_response_is_pending_not_late(self):
        # 未收到有效签收不等于已知迟到
        self.assertEqual(self.obligations["T1:contact"].status, PENDING_NO_RESPONSE)
        self.assertEqual(self.obligations["T1:visit"].status, PENDING_NO_RESPONSE)
        self.assertEqual(self.obligations["T3:contact"].status, PENDING_NO_RESPONSE)
        self.assertEqual(self.obligations["T3:visit"].status, PENDING_NO_RESPONSE)
        self.assertEqual(self.obligations["T1:contact"].credit_raw, 0)

    def test_clock_margin_straddle_is_pending(self):
        # T2 接听计时 165~195 秒跨越 180 秒时限：不取最有利的时间
        o = self.obligations["T2:contact"]
        self.assertEqual(o.status, PENDING_UNCERTAIN)
        self.assertEqual(o.counted, (165.0, 195.0))
        self.assertEqual(o.credit_raw, 0)

    def test_earliest_provable_response_wins(self):
        # T2 上门有 V2（OPS，10:36:00±15）与 V7（PHONE，10:33:00，月结后补传）：
        # 以能证明的最早一次响应计时，1680 秒不越 1800 秒时限
        o = self.obligations["T2:visit"]
        self.assertEqual(o.status, ON_TIME)
        self.assertEqual(o.counted, (1680.0, 1680.0))
        self.assertEqual(o.receipts, ["V2", "V7"])

    def test_confirmed_late(self):
        self.assertEqual(self.obligations["T4:contact"].status, LATE)
        self.assertEqual(self.obligations["T4:contact"].credit_raw, 600)
        self.assertEqual(self.obligations["T5:contact"].status, LATE)
        self.assertEqual(self.obligations["T5:visit"].status, LATE)
        self.assertEqual(self.obligations["T6:contact"].status, LATE)

    def test_phone_channel_gets_no_pause(self):
        # 停服豁免只影响自动通道，电话照常计时（M3 对 T4 无抵扣）
        self.assertEqual(self.obligations["T4:contact"].pause_sec, (0.0, 0.0))
        self.assertEqual(self.obligations["T4:contact"].notices, [])

    def test_visit_not_required_for_regular(self):
        self.assertNotIn("T4:visit", self.obligations)
        self.assertNotIn("T6:visit", self.obligations)

    def test_case_cap_applies_before_station_cap(self):
        # T5 接听 1200 + 上门 3400 = 4600，先受同一呼叫上限 4000 约束
        self.assertEqual(self.obligations["T5:contact"].credit_applied, 1200)
        self.assertEqual(self.obligations["T5:visit"].credit_applied, 2800)


class RollupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rollup = make_engine().rollup("槐园", "2026-03")

    def test_counts(self):
        c = self.rollup.counts
        self.assertEqual((c["included"], c["on_time"], c["late"], c["pending"]), (10, 1, 4, 5))

    def test_rate_range(self):
        lo, hi = self.rollup.rate_range
        self.assertAlmostEqual(lo, 0.4)
        self.assertAlmostEqual(hi, 0.9)

    def test_channel_breakdown(self):
        by_channel = self.rollup.counts_by("channel")
        self.assertEqual(by_channel["phone"]["late"], 4)
        self.assertEqual(by_channel["auto"]["pending"], 4)

    def test_amounts(self):
        # 事件上限后 600+4000+600=5200，未触站点月上限 6500
        self.assertEqual(self.rollup.case_credit_total, 5200)
        self.assertEqual(self.rollup.applied_total, 5200)
        # 未决全部成立时 5200+4000+1200+4000=14400，触站点月上限 6500
        self.assertEqual(self.rollup.potential_applied_total, 6500)


class AdjustmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.adjustment = make_engine().adjustment("槐园", "2026-03")

    def test_delta_is_capped_recalculation_not_single_line_reversal(self):
        adj = self.adjustment
        self.assertEqual(adj.confirmed_cumulative, 6500)  # S-031 已确认（触顶后）
        self.assertEqual(adj.current_applied, 5200)
        # 旧月单触顶：T1;T2 上门改判 -3400 不直接退回，全月重算后差额 -1300
        self.assertEqual(adj.delta, -1300)
        self.assertNotEqual(adj.delta, -3400)

    def test_references_and_split(self):
        adj = self.adjustment
        for ref in ("Q1", "C4", "V7"):
            self.assertIn(ref, adj.references)
        line2 = next(d for d in adj.diffs if d.line == 2)
        self.assertTrue(line2.changed_attribution)  # T1;T2 拆案
        self.assertEqual(line2.credit_delta, -3400)  # 事件上限后口径
        self.assertEqual(sum(o.credit_applied for o in line2.current), 0)

    def test_no_economic_difference_changes_remain_queryable(self):
        # 第 1 行（T1;T2 接听，旧处置 disputed 0 分）：拆案后两项未决，无经济差额但可查
        line1 = next(d for d in self.adjustment.diffs if d.line == 1)
        self.assertFalse(line1.economic)
        self.assertTrue(line1.changed_attribution)
        self.assertEqual(line1.credit_delta, 0)

    def test_pending_not_counted_as_delta(self):
        adj = self.adjustment
        self.assertEqual(len(adj.pending_obligations), 5)
        self.assertEqual(adj.potential_applied, 6500)
        # 调整只含确定差额 -1300，未决不冒充确定差额
        self.assertEqual(adj.delta, -1300)

    def test_voided_notice_note(self):
        self.assertTrue(any("M2" in note and "Q2" in note for note in self.adjustment.notes))


class TodoTests(unittest.TestCase):
    def test_duty_desk_buckets(self):
        todos = make_engine().todos("槐园")
        uncontacted = {t.case.id for t in todos["uncontacted"]}
        visit_unverified = {t.case.id for t in todos["visit_unverified"]}
        self.assertEqual(uncontacted, {"T1", "T3"})  # 尚未联系
        self.assertEqual(visit_unverified, set())  # 无"已联系但缺上门核实"

    def test_todos_reflect_review_immediately(self):
        # 复核到达后待办即时调整：T1 因 Q1 拆案成为独立待办并带证据
        todos = make_engine().todos("槐园")
        t1 = next(t for t in todos["uncontacted"] if t.case.id == "T1")
        self.assertIn("Q1", t1.note)


class AsOfTests(unittest.TestCase):
    def test_evidence_filtered_by_cutoff(self):
        # 以月单截至 2026-04-02T18:00 重看：Q1/Q2/C4/V7 均未到达
        engine = make_engine(as_of=datetime(2026, 4, 2, 18, 0))
        obligations = {o.id: o for o in engine.obligations}
        # V7 未到达：T2 上门只有 V2（1845~1875 秒），确定迟到
        self.assertEqual(obligations["T2:visit"].status, LATE)
        self.assertEqual(obligations["T2:visit"].receipts, ["V2"])
        # C1 未替换：T1 可能时段跨绑定切换，住户无法确认
        self.assertEqual(obligations["T1:contact"].status, "pending_resident")
        # M2 尚未作废
        self.assertIn("M2", {n.id for n in engine.valid_notices})


if __name__ == "__main__":
    unittest.main()
