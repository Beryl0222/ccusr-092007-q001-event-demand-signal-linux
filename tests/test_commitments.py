"""供给登记:绑定所见版本、权限与撤回约束、修订与幂等。"""

import unittest

import support

from event_signal import (
    ConflictError,
    LifecycleStatus,
    PermissionDenied,
    ValidationError,
)


def _commit(svc, **overrides):
    params = dict(
        participant_id="hotel1", record_id="rooms-0920", revision=1,
        signal_id=None, resource="hotel_rooms", quantity=300,
        unit_cost=400.0, unit_margin=600.0,
        decided_at="2026-09-15T12:00:00+08:00",
    )
    params.update(overrides)
    return svc.register_commitment(**params)


class CommitmentTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock = support.build_service()
        self.signal = support.publish_night_signal(self.svc)  # recorded T0 09:00
        self.clock.set("2026-09-15T18:00:00+08:00")

    def test_register_pins_seen_revision(self):
        commitment = _commit(self.svc, signal_id=self.signal.signal_id)
        self.assertEqual(commitment.signal_revision, 1)
        self.assertEqual(commitment.status, LifecycleStatus.ACTIVE)
        self.assertFalse(commitment.stale)

    def test_commitment_requires_view_permission(self):
        svc, clock = support.build_service()
        restricted = support.publish_night_signal(
            svc, publisher_id="tix", source="tix", record_id="tix-0920",
            visibility="restricted",
        )
        clock.set("2026-09-15T18:00:00+08:00")
        with self.assertRaises(PermissionDenied):
            _commit(svc, signal_id=restricted.signal_id)

    def test_decided_at_before_revision_recorded_rejected(self):
        with self.assertRaises(ValidationError):
            _commit(self.svc, signal_id=self.signal.signal_id,
                    decided_at="2026-09-15T08:00:00+08:00")

    def test_explicit_future_revision_rejected(self):
        self.clock.advance(hours=3)
        support.publish_night_signal(self.svc, revision=2, expected=13000,
                                     occurred_at="2026-09-15T13:00:00+08:00")
        with self.assertRaises(ValidationError):
            _commit(self.svc, signal_id=self.signal.signal_id, signal_revision=2,
                    decided_at="2026-09-15T10:00:00+08:00")

    def test_commitment_after_withdrawal_rejected(self):
        self.clock.set("2026-09-16T09:00:00+08:00")
        self.svc.withdraw_signal(
            publisher_id="org", source="org", record_id="concert-0920",
            revision=2, occurred_at="2026-09-16T09:00:00+08:00",
        )
        self.clock.set("2026-09-18T09:00:00+08:00")
        with self.assertRaises(ConflictError):
            _commit(self.svc, signal_id=self.signal.signal_id,
                    decided_at="2026-09-17T09:00:00+08:00")
        # 撤回之前作出的决定仍然有效
        earlier = _commit(self.svc, signal_id=self.signal.signal_id,
                          record_id="rooms-early", decided_at="2026-09-15T12:00:00+08:00")
        self.assertEqual(earlier.status, LifecycleStatus.ACTIVE)

    def test_stale_flag_when_newer_revision_existed(self):
        self.clock.advance(hours=3)  # 21:00
        support.publish_night_signal(self.svc, revision=2, expected=13000,
                                     occurred_at="2026-09-15T21:00:00+08:00")
        self.clock.advance(hours=3)  # 次日 00:00
        stale_commit = _commit(self.svc, signal_id=self.signal.signal_id,
                               signal_revision=1, decided_at="2026-09-15T22:00:00+08:00")
        self.assertTrue(stale_commit.stale)
        fresh = _commit(self.svc, signal_id=self.signal.signal_id, record_id="rooms-2",
                        decided_at="2026-09-15T22:00:00+08:00")
        self.assertEqual(fresh.signal_revision, 2)
        self.assertFalse(fresh.stale)

    def test_amend_creates_new_version(self):
        first = _commit(self.svc, signal_id=self.signal.signal_id)
        self.clock.advance(hours=5)
        amended = _commit(self.svc, signal_id=self.signal.signal_id, revision=2,
                          quantity=250, decided_at="2026-09-15T13:00:00+08:00")
        self.assertEqual(amended.quantity, 250)
        versions = self.svc.store.commitment_versions(first.commitment_id)
        self.assertEqual([v.quantity for v in versions], [300, 250])
        # as-of 两次记录之间仍是 300(rev1 记录于 18:00,rev2 记录于 23:00)
        then = self.svc.store.commitment_at(
            first.commitment_id, as_of=support.dt("2026-09-15T20:00:00+08:00")
        )
        self.assertEqual(then.quantity, 300)

    def test_idempotent_retry(self):
        first = _commit(self.svc, signal_id=self.signal.signal_id)
        again = _commit(self.svc, signal_id=self.signal.signal_id)
        self.assertIs(first, again)
        self.assertEqual(len(self.svc.store.commitment_versions(first.commitment_id)), 1)

    def test_invalid_quantity_and_future_decision(self):
        with self.assertRaises(ValidationError):
            _commit(self.svc, signal_id=self.signal.signal_id, quantity=0)
        with self.assertRaises(ValidationError):
            _commit(self.svc, signal_id=self.signal.signal_id,
                    decided_at="2026-09-20T00:00:00+08:00")  # 晚于当前时钟

    def test_withdraw_commitment_keeps_history(self):
        first = _commit(self.svc, signal_id=self.signal.signal_id)
        self.clock.advance(hours=2)
        tomb = self.svc.withdraw_commitment(
            participant_id="hotel1", record_id="rooms-0920", revision=2,
            decided_at="2026-09-15T13:00:00+08:00",
        )
        self.assertEqual(tomb.status, LifecycleStatus.WITHDRAWN)
        versions = self.svc.store.commitment_versions(first.commitment_id)
        self.assertEqual([v.status for v in versions],
                         [LifecycleStatus.ACTIVE, LifecycleStatus.WITHDRAWN])


if __name__ == "__main__":
    unittest.main()
