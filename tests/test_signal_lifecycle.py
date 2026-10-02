"""信号生命周期:发布、修订、撤回、迟到数据、同源幂等与合同兼容。"""

import unittest
from pathlib import Path

import support

from event_signal import (
    ConflictError,
    LifecycleStatus,
    NotFoundError,
    PermissionDenied,
    load_record,
)

class PublishTest(unittest.TestCase):
    def test_publish_and_get(self):
        svc, _ = support.build_service()
        rev = support.publish_night_signal(svc)
        self.assertEqual(rev.signal_id, "org/concert-0920")
        self.assertEqual(rev.revision, 1)
        self.assertEqual(rev.status, LifecycleStatus.ACTIVE)
        got = svc.get_signal("hotel1", rev.signal_id)
        self.assertEqual(got.quantity.expected, 12000)
        self.assertEqual(got.window.business_date.isoformat(), "2026-09-20")

    def test_unregistered_publisher_rejected(self):
        svc, _ = support.build_service()
        with self.assertRaises(NotFoundError):
            support.publish_night_signal(svc, publisher_id="ghost", source="ghost")

    def test_unknown_geo_rejected(self):
        svc, _ = support.build_service()
        with self.assertRaises(NotFoundError):
            support.publish_night_signal(svc, geo_id="NOWHERE")

    def test_bad_quantity_range_rejected(self):
        svc, _ = support.build_service()
        with self.assertRaises(Exception):
            support.publish_night_signal(svc, low=13000, expected=12000, high=15000)


class RevisionTest(unittest.TestCase):
    def test_revision_creates_new_version_and_old_is_kept(self):
        svc, clock = support.build_service()
        rev1 = support.publish_night_signal(svc)  # recorded T0
        clock.advance(hours=1)
        support.publish_night_signal(svc, revision=2, expected=13000,
                                     occurred_at="2026-09-15T11:00:00+08:00")
        # 当前最新为 rev2
        self.assertEqual(svc.get_signal("hotel1", rev1.signal_id).quantity.expected, 13000)
        # as-of 两版本之间仍看到 rev1 —— 当时所见不被重写
        seen_then = svc.get_signal(
            "hotel1", rev1.signal_id, as_of="2026-09-15T09:30:00+08:00"
        )
        self.assertEqual(seen_then.revision, 1)
        self.assertEqual(seen_then.quantity.expected, 12000)
        history = svc.signal_history("hotel1", rev1.signal_id)
        self.assertEqual([v.revision for v in history], [1, 2])

    def test_only_publisher_can_revise(self):
        svc, _ = support.build_service()
        rev1 = support.publish_night_signal(svc)
        with self.assertRaises(PermissionDenied):
            support.publish_night_signal(
                svc, publisher_id="tix", source="org",
                record_id="concert-0920", revision=2,
            )
        # 原发布方可以修订
        support.publish_night_signal(svc, revision=2)
        self.assertEqual(svc.get_signal("ops", rev1.signal_id).revision, 2)

    def test_idempotent_retry_does_not_duplicate(self):
        svc, _ = support.build_service()
        first = support.publish_night_signal(svc)
        again = support.publish_night_signal(svc)  # 同源同内容重试
        self.assertIs(first, again)
        self.assertEqual(len(svc.store.signal_versions(first.signal_id)), 1)

    def test_same_revision_different_content_conflicts(self):
        svc, _ = support.build_service()
        support.publish_night_signal(svc)
        with self.assertRaises(ConflictError):
            support.publish_night_signal(svc, revision=1, expected=9999)

    def test_revision_gap_rejected(self):
        svc, _ = support.build_service()
        support.publish_night_signal(svc)
        with self.assertRaises(ConflictError):
            support.publish_night_signal(svc, revision=3)

    def test_late_data_becomes_new_revision(self):
        svc, clock = support.build_service()
        support.publish_night_signal(
            svc, revision=1, occurred_at="2026-09-15T10:00:00+08:00"
        )
        clock.advance(days=1)
        # 迟到数据:业务时间早于上一版,仍作为新版本追加
        late = support.publish_night_signal(
            svc, revision=2, occurred_at="2026-09-14T10:00:00+08:00", expected=12500,
        )
        self.assertEqual(late.revision, 2)
        self.assertLess(late.occurred_at, svc.store.signal_revision(late.signal_id, 1).occurred_at)
        history = svc.signal_history("ops", late.signal_id)
        self.assertEqual([v.revision for v in history], [1, 2])


class WithdrawTest(unittest.TestCase):
    def test_withdraw_appends_tombstone_and_keeps_history(self):
        svc, clock = support.build_service()
        rev1 = support.publish_night_signal(svc)
        clock.advance(hours=2)
        tomb = svc.withdraw_signal(
            publisher_id="org", source="org", record_id="concert-0920",
            revision=2, occurred_at="2026-09-15T11:00:00+08:00", note="活动取消",
        )
        self.assertEqual(tomb.status, LifecycleStatus.WITHDRAWN)
        self.assertIsNone(tomb.quantity)
        # 撤回前 as-of 仍可见 active 版本
        before = svc.get_signal("hotel1", rev1.signal_id, as_of="2026-09-15T10:00:00+08:00")
        self.assertEqual(before.status, LifecycleStatus.ACTIVE)
        history = svc.signal_history("ops", rev1.signal_id)
        self.assertEqual([v.status for v in history],
                         [LifecycleStatus.ACTIVE, LifecycleStatus.WITHDRAWN])

    def test_withdraw_unknown_signal(self):
        svc, _ = support.build_service()
        with self.assertRaises(NotFoundError):
            svc.withdraw_signal(
                publisher_id="org", source="org", record_id="nope",
                revision=2, occurred_at="2026-09-15T11:00:00+08:00",
            )

    def test_withdraw_by_non_publisher_rejected(self):
        svc, _ = support.build_service()
        support.publish_night_signal(svc)
        with self.assertRaises(PermissionDenied):
            svc.withdraw_signal(
                publisher_id="tix", source="org", record_id="concert-0920",
                revision=2, occurred_at="2026-09-15T11:00:00+08:00",
            )


class ContractCompatTest(unittest.TestCase):
    def test_fixture_envelope_flows_into_service(self):
        fixture = Path(__file__).parents[1] / "fixtures" / "demand_signal.json"
        record = load_record(fixture)
        svc, _ = support.build_service()
        rev = svc.submit_signal(
            publisher_id="org", source=record.source, record_id=record.record_id,
            revision=record.revision, occurred_at=record.occurred_at,
            purpose="general", metric="visitor_arrivals",
            window_start=support.NIGHT_START, window_end=support.NIGHT_END,
            geo_id="D1", low=1, expected=2, high=3,
        )
        # 既有标识与时间含义保持不变
        self.assertEqual(rev.source, record.source)
        self.assertEqual(rev.record_id, record.record_id)
        self.assertEqual(rev.occurred_at.isoformat(), record.occurred_at)
        self.assertEqual(rev.schema_version, record.schema_version)
        self.assertEqual(rev.domain, record.domain)
        self.assertEqual(rev.signal_id, f"{record.source}/{record.record_id}")


if __name__ == "__main__":
    unittest.main()
