"""端到端:跨午夜演唱会夜的完整协作与赛后重建。"""

import unittest

import support

from event_signal import LifecycleStatus, PermissionDenied


class EventNightCycleTest(unittest.TestCase):
    def test_full_cycle(self):
        svc, clock = support.build_service()

        # 9/15:主办方发布跨午夜场次需求信号(20:00 - 次日 02:00)
        s1 = support.publish_night_signal(
            svc, low=8000, expected=12000, high=15000, purpose="capacity_planning",
        )
        self.assertTrue(s1.window.crosses_midnight)
        self.assertEqual(s1.window.business_date.isoformat(), "2026-09-20")

        # 票务方发布受限信号,仅 hotel1 可见
        s2 = support.publish_night_signal(
            svc, publisher_id="tix", source="tix", record_id="tix-0920",
            low=6000, expected=9000, high=11000,
            visibility="restricted", allowlist=("hotel1",),
        )
        with self.assertRaises(PermissionDenied):
            svc.get_signal("hotel2", s2.signal_id)

        # 9/16:经营者依据所见信号登记增配
        clock.set("2026-09-16T12:00:00+08:00")
        svc.register_commitment(
            participant_id="hotel1", record_id="rooms-0920", revision=1,
            signal_id=s2.signal_id, signal_revision=1,
            resource="hotel_rooms", quantity=300,
            unit_cost=400.0, unit_margin=600.0,
            decided_at="2026-09-16T09:00:00+08:00",
        )
        svc.register_commitment(
            participant_id="trans1", record_id="shuttle-0920", revision=1,
            signal_id=s1.signal_id, signal_revision=1,
            resource="shuttle_seats", quantity=40,
            decided_at="2026-09-16T09:30:00+08:00",
        )

        # 9/17:票务方修订预测(更接近最终实绩);hotel1 的登记仍锚定 rev1
        clock.set("2026-09-17T09:00:00+08:00")
        support.publish_night_signal(
            svc, publisher_id="tix", source="tix", record_id="tix-0920",
            revision=2, low=7000, expected=10000, high=12000,
            occurred_at="2026-09-17T09:00:00+08:00",
            visibility="restricted", allowlist=("hotel1",),
        )

        # 权限变化:运营中心临时授权 hotel2 查看票务信号,赛后收回
        svc.grant_access(actor_id="ops", signal_id=s2.signal_id,
                         participant_id="hotel2",
                         effective_at="2026-09-18T00:00:00+08:00")
        self.assertEqual(
            svc.get_signal("hotel2", s2.signal_id,
                           as_of="2026-09-19T00:00:00+08:00").revision, 2,
        )
        svc.revoke_access(actor_id="ops", signal_id=s2.signal_id,
                          participant_id="hotel2",
                          effective_at="2026-09-21T06:00:00+08:00")
        with self.assertRaises(PermissionDenied):
            svc.get_signal("hotel2", s2.signal_id, as_of="2026-09-22T00:00:00+08:00")

        # 赛后实绩回填(含迟到的闸口修正)
        clock.set("2026-09-21T10:00:00+08:00")
        svc.submit_actual(
            participant_id="ops", record_id="turnstile-0920", revision=1,
            metric="visitor_arrivals", window_start=support.NIGHT_START,
            window_end=support.NIGHT_END, geo_id="D1", observed=13500,
            occurred_at="2026-09-21T03:00:00+08:00",
        )
        svc.submit_actual(
            participant_id="hotel1", record_id="rooms-actual", revision=1,
            metric="hotel_rooms", window_start=support.NIGHT_START,
            window_end=support.NIGHT_END, geo_id="D1", observed=210,
            occurred_at="2026-09-21T04:00:00+08:00",
        )
        svc.submit_actual(
            participant_id="trans1", record_id="shuttle-actual", revision=1,
            metric="shuttle_seats", window_start=support.NIGHT_START,
            window_end=support.NIGHT_END, geo_id="D1", observed=44,
            occurred_at="2026-09-21T04:00:00+08:00",
        )
        clock.advance(hours=5)
        svc.submit_actual(
            participant_id="ops", record_id="turnstile-0920", revision=2,
            metric="visitor_arrivals", window_start=support.NIGHT_START,
            window_end=support.NIGHT_END, geo_id="D1", observed=13600,
            occurred_at="2026-09-21T08:00:00+08:00",
        )

        # 误差回看:rev1 预期 12000 vs 实绩 13600;迟到修正前看到的是 13500
        accuracy = svc.signal_accuracy("ops", s1.signal_id)
        entry = accuracy.entries[0]
        self.assertEqual(entry.actual, 13600)
        self.assertEqual(entry.error, 1600)
        self.assertTrue(entry.within_range)
        self.assertEqual(entry.commitments_acted, 1)
        before_fix = svc.signal_accuracy(
            "ops", s1.signal_id, as_of="2026-09-21T12:00:00+08:00"
        )
        self.assertEqual(before_fix.entries[0].actual, 13500)

        # 值班重建:hotel1 因 tix 的 rev1(预期 9000)增配 300 间夜
        buildup = svc.explain_buildup("ops", "D1", support.NIGHT_START, support.NIGHT_END)
        by_participant = {e.participant_id: e for e in buildup.entries}
        self.assertEqual(by_participant["hotel1"].signal.quantity.expected, 9000)
        self.assertEqual(by_participant["hotel1"].signal.publisher_id, "tix")
        self.assertEqual(by_participant["trans1"].signal.signal_id, s1.signal_id)

        # 损失归因:多备 90 间夜 × 400 = 36000,链到 tix 的 rev1
        loss = svc.loss_attribution("ops", "D1", support.NIGHT_START, support.NIGHT_END)
        hotel = next(e for e in loss.entries if e.participant_id == "hotel1")
        self.assertEqual(hotel.over_provisioned, 90)
        self.assertEqual(hotel.loss, 36000.0)
        self.assertEqual(hotel.publisher_id, "tix")
        self.assertEqual(hotel.signal_revision, 1)

        # 聚合隐私:客流实绩只有 ops 一个贡献方,普通经营者被抑制
        suppressed = svc.actual_aggregate(
            "hotel2", "visitor_arrivals", "D1", support.NIGHT_START, support.NIGHT_END
        )
        self.assertTrue(suppressed.suppressed)
        ops_view = svc.actual_aggregate(
            "ops", "visitor_arrivals", "D1", support.NIGHT_START, support.NIGHT_END
        )
        self.assertEqual(ops_view.observed, 13600)

        # 撤回:票务方撤回信号后,历史版本与登记链仍然完整
        clock.set("2026-09-22T09:00:00+08:00")
        svc.withdraw_signal(
            publisher_id="tix", source="tix", record_id="tix-0920",
            revision=3, occurred_at="2026-09-22T09:00:00+08:00", note="复盘后归档",
        )
        history = svc.signal_history("ops", s2.signal_id)
        self.assertEqual([v.revision for v in history], [1, 2, 3])
        self.assertEqual(history[-1].status, LifecycleStatus.WITHDRAWN)
        self.assertEqual(history[0].quantity.expected, 9000)


if __name__ == "__main__":
    unittest.main()
