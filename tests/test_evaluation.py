import unittest

from support import build_service, clock
from event_signal.errors import SuppressedAggregate

WS = clock(22)
WE = clock(2, day=21)


def forecast(sid, who, est, cohort, ref, *, visibility="industry",
             sectors=("hotel",)):
    return {
        "signal_id": sid, "publisher_id": who, "purpose": "hotel_blocking",
        "metric": "hotel_demand", "region_code": "bc-stadium",
        "window_start": WS.isoformat(), "window_end": WE.isoformat(),
        "estimate": est, "unit": "room_night",
        "ci_low": est * 0.8, "ci_high": est * 1.2,
        "sample_size": 200, "cohort_key": cohort, "source_ref": ref,
        "visibility": visibility, "sectors": list(sectors),
    }


def outcome(oid, actual, cohort, *, sample=100):
    return {
        "outcome_id": oid, "metric": "hotel_demand",
        "region_code": "bc-stadium", "window_start": WS.isoformat(),
        "window_end": WE.isoformat(), "actual": actual, "unit": "room_night",
        "sample_size": sample, "cohort_key": cohort, "source": "op",
    }


class EvaluationTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service(min_cohorts=3, min_sample=10)
        s = self.svc
        s.publish_signal(forecast("sig-tk", "tk", 300, "c-tk", "r1"),
                         recorded_at=clock(9, day=1))
        s.publish_signal(forecast("sig-a", "hotel-a", 40, "c-a", "r2"),
                         recorded_at=clock(9, day=1))
        s.publish_signal(forecast("sig-b", "hotel-b", 50, "c-b", "r3"),
                         recorded_at=clock(9, day=1))

    def _decision(self, provision=320):
        return self.svc.register_decision({
            "party_id": "hotel-a", "metric": "hotel_demand",
            "region_code": "bc-stadium",
            "window_start": WS.isoformat(), "window_end": WE.isoformat(),
            "provision": provision, "unit": "room_night",
            "basis": {"sig-tk": 1, "sig-a": 1},
            "rationale": "赛前锁房 320 间",
        }, recorded_at=clock(10, day=1))

    def test_decision_chain_and_loss_attribution(self):
        svc = self.svc
        self._decision(320)
        # 迟到修订：票务 9/18 下调到 180（决定早已做出）
        svc.revise_signal("sig-tk", {"expected_revision": 1, "estimate": 180,
                                     "ci_low": 150, "ci_high": 210,
                                     "actor_id": "tk"},
                          recorded_at=clock(8, day=18))
        # 三个 cohort 实绩：200 + 20 + 25 = 245
        svc.record_outcome(outcome("o1", 200, "c-tk"), recorded_at=clock(12, day=21))
        svc.record_outcome(outcome("o2", 20, "c-a"), recorded_at=clock(12, day=21))
        svc.record_outcome(outcome("o3", 25, "c-b"), recorded_at=clock(12, day=21))

        d = svc.ledger.decisions()[0]
        attr = svc.evaluation.attribute_decision(d)
        self.assertAlmostEqual(attr.actual, 245.0)
        self.assertAlmostEqual(attr.provision, 320.0)
        self.assertAlmostEqual(attr.overage, 75.0)
        self.assertAlmostEqual(attr.shortfall, 0.0)
        # hotel over 成本系数 1.0
        self.assertAlmostEqual(attr.total_cost, 75.0)
        scores = {b.signal_id: b for b in attr.basis if b}
        tk = scores["sig-tk"]
        self.assertEqual(tk.revision_seen, 1)          # 决策所见
        self.assertAlmostEqual(tk.point, 300.0)
        self.assertAlmostEqual(tk.actual, 200.0)       # 只对自己 cohort
        self.assertTrue(tk.revised_after)
        self.assertEqual(tk.latest_revision, 2)
        self.assertAlmostEqual(tk.late_change, -120.0)  # 300 -> 180
        self.assertFalse(tk.ci_hit)                    # [240,360] 未覆盖 200

    def test_reconstruction_reports_timeline_and_cost(self):
        svc = self.svc
        self._decision(320)
        svc.revise_signal("sig-tk", {"expected_revision": 1, "estimate": 180,
                                     "actor_id": "tk"}, recorded_at=clock(8, day=18))
        svc.record_outcome(outcome("o1", 200, "c-tk"), recorded_at=clock(12, day=21))
        svc.record_outcome(outcome("o2", 20, "c-a"), recorded_at=clock(12, day=21))
        svc.record_outcome(outcome("o3", 25, "c-b"), recorded_at=clock(12, day=21))

        report = svc.reconstruct("hotel-a", {
            "metric": "hotel_demand", "region_code": "bc-stadium",
            "window_start": WS.isoformat(), "window_end": WE.isoformat()})
        kinds = [e["kind"] for e in report["timeline"]]
        self.assertIn("signal_published", kinds)
        self.assertIn("signal_revised", kinds)
        self.assertIn("decision", kinds)
        self.assertIn("outcome", kinds)
        # 时间线按系统时间排序：决定(9/10) 在迟到修订(9/18) 之前
        idx_dec = next(i for i, e in enumerate(report["timeline"])
                       if e["kind"] == "decision")
        idx_rev = next(i for i, e in enumerate(report["timeline"])
                       if e["kind"] == "signal_revised")
        self.assertLess(idx_dec, idx_rev)
        self.assertFalse(report["summary"]["suppressed"])
        self.assertAlmostEqual(report["summary"]["total_cost"], 75.0)

    def test_sparse_outcomes_suppress_error_but_keep_timeline(self):
        svc = self.svc
        self._decision(320)
        # 只有 1 个 cohort 的实绩 -> 隐私门槛不足
        svc.record_outcome(outcome("o1", 200, "c-tk"), recorded_at=clock(12, day=21))
        report = svc.reconstruct("hotel-a", {
            "metric": "hotel_demand", "region_code": "bc-stadium",
            "window_start": WS.isoformat(), "window_end": WE.isoformat()})
        self.assertTrue(report["summary"]["suppressed"])
        # 时间线不暴露该单条实绩
        self.assertNotIn("outcome", [e["kind"] for e in report["timeline"]])
        # 误差/成本被抑制：decisions 为空
        self.assertEqual(report["decisions"], [])

    def test_shortfall_cost_when_under_provisioned(self):
        svc = self.svc
        self._decision(100)   # 仅备 100
        svc.record_outcome(outcome("o1", 200, "c-tk"), recorded_at=clock(12, day=21))
        svc.record_outcome(outcome("o2", 60, "c-a"), recorded_at=clock(12, day=21))
        svc.record_outcome(outcome("o3", 40, "c-b"), recorded_at=clock(12, day=21))
        d = svc.ledger.decisions()[0]
        attr = svc.evaluation.attribute_decision(d)
        # 实际 300，备 100，缺口 200，hotel under 系数 4.0
        self.assertAlmostEqual(attr.shortfall, 200.0)
        self.assertAlmostEqual(attr.under_cost, 800.0)

    def test_other_hotel_cannot_see_foreign_decision_chain(self):
        svc = self.svc
        self._decision(320)
        report = svc.reconstruct("hotel-b", {
            "metric": "hotel_demand", "region_code": "bc-stadium",
            "window_start": WS.isoformat(), "window_end": WE.isoformat()})
        # hotel-b 看不到 hotel-a 的决定链
        self.assertEqual(report["summary"]["decision_count"], 0)


if __name__ == "__main__":
    unittest.main()
