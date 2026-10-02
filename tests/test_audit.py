"""审计重建:为何增配、损失归因到哪条决策链。"""

import unittest

import support

from event_signal import PermissionDenied


def _setup_network(svc):
    """主办方网络级信号 + 票务方受限信号(授权 hotel1)。"""
    s1 = support.publish_night_signal(
        svc, low=8000, expected=12000, high=15000,
    )
    s2 = support.publish_night_signal(
        svc, publisher_id="tix", source="tix", record_id="tix-0920",
        low=6000, expected=9000, high=11000,
        visibility="restricted", allowlist=("hotel1",),
    )
    return s1, s2


class BuildupTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock = support.build_service()
        self.s1, self.s2 = _setup_network(self.svc)
        self.clock.set("2026-09-16T10:00:00+08:00")
        self.svc.register_commitment(
            participant_id="hotel1", record_id="rooms-0920", revision=1,
            signal_id=self.s2.signal_id, signal_revision=1,
            resource="hotel_rooms", quantity=300,
            unit_cost=400.0, unit_margin=600.0,
            decided_at="2026-09-16T09:00:00+08:00",
        )
        self.clock.advance(hours=1)
        self.svc.register_commitment(
            participant_id="trans1", record_id="shuttle-0920", revision=1,
            signal_id=self.s1.signal_id, signal_revision=1,
            resource="shuttle_seats", quantity=40,
            decided_at="2026-09-16T09:30:00+08:00",
        )

    def test_explain_buildup_reconstructs_seen_revision(self):
        # 事后票务方修订信号:重建时仍应呈现 hotel1 当时所见的 rev1
        self.clock.set("2026-09-17T09:00:00+08:00")
        support.publish_night_signal(
            self.svc, publisher_id="tix", source="tix", record_id="tix-0920",
            revision=2, expected=10000, low=7000, high=12000,
            occurred_at="2026-09-17T09:00:00+08:00",
        )
        report = self.svc.explain_buildup(
            "ops", "D1", support.NIGHT_START, support.NIGHT_END
        )
        self.assertEqual(len(report.entries), 2)
        by_participant = {e.participant_id: e for e in report.entries}
        hotel = by_participant["hotel1"]
        self.assertEqual(hotel.signal.revision, 1)
        self.assertEqual(hotel.signal.quantity.expected, 9000)  # 所见即 rev1
        self.assertEqual(hotel.signal.publisher_id, "tix")
        self.assertEqual(hotel.quantity, 300)
        totals = {t.resource: t for t in report.totals_by_resource}
        self.assertEqual(totals["hotel_rooms"].quantity, 300)
        self.assertEqual(totals["shuttle_seats"].quantity, 40)

    def test_buildup_requires_ops_role(self):
        with self.assertRaises(PermissionDenied):
            self.svc.explain_buildup("hotel1", "D1", support.NIGHT_START, support.NIGHT_END)

    def test_buildup_as_of_excludes_later_commitments(self):
        # hotel1 的登记记录于 10:00,trans1 的记录于 11:00
        report = self.svc.explain_buildup(
            "ops", "D1", support.NIGHT_START, support.NIGHT_END,
            as_of="2026-09-16T10:30:00+08:00",
        )
        self.assertEqual([e.participant_id for e in report.entries], ["hotel1"])


class LossTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock = support.build_service()
        self.s1, self.s2 = _setup_network(self.svc)
        self.clock.set("2026-09-16T10:00:00+08:00")
        self.svc.register_commitment(
            participant_id="hotel1", record_id="rooms-0920", revision=1,
            signal_id=self.s2.signal_id, signal_revision=1,
            resource="hotel_rooms", quantity=300,
            unit_cost=400.0, unit_margin=600.0,
            decided_at="2026-09-16T09:00:00+08:00",
        )
        self.clock.advance(hours=1)
        self.svc.register_commitment(
            participant_id="trans1", record_id="shuttle-0920", revision=1,
            signal_id=self.s1.signal_id, signal_revision=1,
            resource="shuttle_seats", quantity=40,
            decided_at="2026-09-16T09:30:00+08:00",
        )
        self.clock.set("2026-09-21T10:00:00+08:00")
        # hotel1 实际只售出 210 间夜 → 多备 90;trans1 实际承运 44 → 缺口 4
        self.svc.submit_actual(
            participant_id="hotel1", record_id="rooms-actual", revision=1,
            metric="hotel_rooms", window_start=support.NIGHT_START,
            window_end=support.NIGHT_END, geo_id="D1", observed=210,
            occurred_at="2026-09-21T03:00:00+08:00",
        )
        self.svc.submit_actual(
            participant_id="trans1", record_id="shuttle-actual", revision=1,
            metric="shuttle_seats", window_start=support.NIGHT_START,
            window_end=support.NIGHT_END, geo_id="D1", observed=44,
            occurred_at="2026-09-21T03:00:00+08:00",
        )

    def test_loss_attributed_to_decision_chain(self):
        report = self.svc.loss_attribution(
            "ops", "D1", support.NIGHT_START, support.NIGHT_END
        )
        by_participant = {e.participant_id: e for e in report.entries}
        hotel = by_participant["hotel1"]
        self.assertEqual(hotel.provisioned, 300)
        self.assertEqual(hotel.actual, 210)
        self.assertEqual(hotel.over_provisioned, 90)
        self.assertEqual(hotel.loss, 90 * 400.0)
        # 决策链:损失 ← 登记 ← 所见信号版本 ← 发布方
        self.assertEqual(hotel.signal_id, self.s2.signal_id)
        self.assertEqual(hotel.signal_revision, 1)
        self.assertEqual(hotel.publisher_id, "tix")
        # trans1 未报价:缺口可算但损失不可定价
        trans = by_participant["trans1"]
        self.assertEqual(trans.unmet, 4)
        self.assertTrue(trans.unpriced)
        self.assertIsNone(trans.loss)
        self.assertEqual(report.total_loss, 90 * 400.0)
        self.assertEqual(dict(report.by_publisher)["tix"], 90 * 400.0)

    def test_loss_requires_ops_role(self):
        with self.assertRaises(PermissionDenied):
            self.svc.loss_attribution("tix", "D1", support.NIGHT_START, support.NIGHT_END)

    def test_missing_actual_flagged(self):
        self.svc.register_commitment(
            participant_id="dine1", record_id="meals-0920", revision=1,
            signal_id=self.s1.signal_id, signal_revision=1,
            resource="meal_sets", quantity=500, unit_cost=30.0,
            decided_at="2026-09-16T10:00:00+08:00",
        )
        report = self.svc.loss_attribution(
            "ops", "D1", support.NIGHT_START, support.NIGHT_END
        )
        dine = next(e for e in report.entries if e.participant_id == "dine1")
        self.assertIsNone(dine.actual)
        self.assertIsNone(dine.loss)


if __name__ == "__main__":
    unittest.main()
