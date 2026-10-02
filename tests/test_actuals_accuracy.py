"""实绩回填与误差计算:迟到修正、k 匿名抑制、分版本误差。"""

import unittest

import support

from event_signal import PermissionDenied, ValidationError


def _actual(svc, **overrides):
    params = dict(
        participant_id="ops", record_id="turnstile-0920", revision=1,
        metric="visitor_arrivals", window_start=support.NIGHT_START,
        window_end=support.NIGHT_END, geo_id="D1", observed=13500,
        occurred_at="2026-09-21T03:00:00+08:00",
    )
    params.update(overrides)
    return svc.submit_actual(**params)


class ActualsTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock = support.build_service()
        self.signal = support.publish_night_signal(self.svc)
        self.clock.set("2026-09-21T10:00:00+08:00")

    def test_negative_observed_rejected(self):
        with self.assertRaises(ValidationError):
            _actual(self.svc, observed=-1)

    def test_late_correction_is_new_version(self):
        _actual(self.svc, observed=13500)
        self.clock.advance(hours=5)
        _actual(self.svc, revision=2, observed=13600,
                occurred_at="2026-09-21T08:00:00+08:00")
        now = self.svc.actual_aggregate(
            "ops", "visitor_arrivals", "D1", support.NIGHT_START, support.NIGHT_END
        )
        self.assertEqual(now.observed, 13600)
        # as-of 修正前仍是迟到的旧值 —— 历史不被重写
        before = self.svc.actual_aggregate(
            "ops", "visitor_arrivals", "D1", support.NIGHT_START, support.NIGHT_END,
            as_of="2026-09-21T12:00:00+08:00",
        )
        self.assertEqual(before.observed, 13500)

    def test_idempotent_retry(self):
        first = _actual(self.svc)
        again = _actual(self.svc)
        self.assertIs(first, again)
        self.assertEqual(len(self.svc.store.actual_versions(first.series_id)), 1)

    def test_k_anonymity_suppression(self):
        _actual(self.svc)  # 仅 1 个贡献方
        agg = self.svc.actual_aggregate(
            "hotel1", "visitor_arrivals", "D1", support.NIGHT_START, support.NIGHT_END
        )
        self.assertTrue(agg.suppressed)
        self.assertIsNone(agg.observed)
        # 运营中心不受抑制
        ops_view = self.svc.actual_aggregate(
            "ops", "visitor_arrivals", "D1", support.NIGHT_START, support.NIGHT_END
        )
        self.assertEqual(ops_view.observed, 13500)
        # 补足 3 个贡献方后解除抑制
        _actual(self.svc, participant_id="hotel1", record_id="h1-count",
                observed=4000, metric="visitor_arrivals")
        _actual(self.svc, participant_id="dine1", record_id="d1-count",
                observed=2000, metric="visitor_arrivals")
        agg = self.svc.actual_aggregate(
            "hotel2", "visitor_arrivals", "D1", support.NIGHT_START, support.NIGHT_END
        )
        self.assertFalse(agg.suppressed)
        self.assertEqual(agg.observed, 19500)
        self.assertEqual(agg.contributors, 3)

    def test_proration_across_windows(self):
        # 实绩窗口 20:00-04:00(8h)覆盖信号窗口 20:00-02:00(6h):按 6/8 分摊
        _actual(self.svc, window_end="2026-09-21T04:00:00+08:00", observed=16000)
        agg = self.svc.actual_aggregate(
            "ops", "visitor_arrivals", "D1", support.NIGHT_START, support.NIGHT_END
        )
        self.assertEqual(agg.observed, 12000)
        self.assertTrue(agg.prorated)

    def test_geo_rollup_in_aggregate(self):
        _actual(self.svc, geo_id="V1", observed=9000)
        _actual(self.svc, participant_id="hotel1", record_id="h1",
                geo_id="B1", observed=3000)
        _actual(self.svc, participant_id="dine1", record_id="d1",
                geo_id="D2", observed=999)  # 其他商圈,不计入
        agg = self.svc.actual_aggregate(
            "ops", "visitor_arrivals", "D1", support.NIGHT_START, support.NIGHT_END
        )
        self.assertEqual(agg.observed, 12000)


class AccuracyTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock = support.build_service()
        self.signal = support.publish_night_signal(
            self.svc, low=8000, expected=12000, high=15000,
        )
        self.clock.set("2026-09-21T10:00:00+08:00")
        _actual(self.svc, observed=13500)

    def test_error_math(self):
        report = self.svc.signal_accuracy("ops", self.signal.signal_id)
        entry = report.entries[0]
        self.assertEqual(entry.actual, 13500)
        self.assertEqual(entry.error, 1500)  # 实绩 - 预期
        self.assertTrue(entry.within_range)
        self.assertAlmostEqual(entry.abs_pct_error, 1500 / 13500)
        self.assertFalse(report.suppressed)

    def test_out_of_range_flagged(self):
        svc, clock = support.build_service()
        signal = support.publish_night_signal(svc, low=8000, expected=12000, high=13000)
        clock.set("2026-09-21T10:00:00+08:00")
        _actual(svc, observed=13500)
        entry = svc.signal_accuracy("ops", signal.signal_id).entries[0]
        self.assertFalse(entry.within_range)

    def test_per_revision_entries_and_acted_on(self):
        # hotel1 基于 rev1 登记;随后信号修订为 rev2
        self.svc.register_commitment(
            participant_id="hotel1", record_id="rooms-0920", revision=1,
            signal_id=self.signal.signal_id, signal_revision=1,
            resource="hotel_rooms", quantity=300,
            decided_at="2026-09-15T12:00:00+08:00",
        )
        self.clock.set("2026-09-16T09:00:00+08:00")
        support.publish_night_signal(self.svc, revision=2, expected=13000,
                                     occurred_at="2026-09-16T09:00:00+08:00")
        self.clock.set("2026-09-21T10:00:00+08:00")
        report = self.svc.signal_accuracy("ops", self.signal.signal_id)
        self.assertEqual(len(report.entries), 2)
        rev1, rev2 = report.entries
        self.assertEqual(rev1.commitments_acted, 1)   # 有登记依据 rev1 行动
        self.assertEqual(rev1.error, 1500)
        self.assertEqual(rev2.commitments_acted, 0)
        self.assertEqual(rev2.error, 500)             # 修订后更接近实绩

    def test_publisher_sees_suppressed_until_k_contributors(self):
        # 实绩只有 ops 一个贡献方:发布方视角被抑制,运营中心可见
        report = self.svc.signal_accuracy("org", self.signal.signal_id)
        self.assertTrue(report.suppressed)
        self.assertIsNone(report.entries[0].actual)
        _actual(self.svc, participant_id="hotel1", record_id="h1", observed=4000)
        _actual(self.svc, participant_id="dine1", record_id="d1", observed=2000)
        report = self.svc.signal_accuracy("org", self.signal.signal_id)
        self.assertFalse(report.suppressed)
        self.assertEqual(report.entries[0].actual, 19500)

    def test_unrelated_publisher_denied(self):
        with self.assertRaises(PermissionDenied):
            self.svc.signal_accuracy("tix", self.signal.signal_id)

    def test_withdrawn_revision_has_no_forecast(self):
        self.clock.set("2026-09-16T09:00:00+08:00")
        self.svc.withdraw_signal(
            publisher_id="org", source="org", record_id="concert-0920",
            revision=2, occurred_at="2026-09-16T09:00:00+08:00",
        )
        self.clock.set("2026-09-21T10:00:00+08:00")
        report = self.svc.signal_accuracy("ops", self.signal.signal_id)
        self.assertIsNone(report.entries[-1].forecast)
        self.assertIsNone(report.entries[-1].actual)


if __name__ == "__main__":
    unittest.main()
