"""可见级别与权限变化:授权/收回按 as-of 时刻求值。"""

import unittest

import support

from event_signal import PermissionDenied


class VisibilityTest(unittest.TestCase):
    def test_private_visible_only_to_publisher_and_ops(self):
        svc, _ = support.build_service()
        rev = support.publish_night_signal(svc, visibility="private")
        with self.assertRaises(PermissionDenied):
            svc.get_signal("hotel1", rev.signal_id)
        self.assertEqual(svc.get_signal("org", rev.signal_id).revision, 1)
        self.assertEqual(svc.get_signal("ops", rev.signal_id).revision, 1)

    def test_network_visible_to_registered_participants(self):
        svc, _ = support.build_service()
        rev = support.publish_night_signal(svc, visibility="network")
        for viewer in ("hotel1", "trans1", "dine1", "tix"):
            self.assertEqual(svc.get_signal(viewer, rev.signal_id).revision, 1)
        with self.assertRaises(PermissionDenied):
            svc.get_signal("outsider", rev.signal_id)

    def test_restricted_allowlist(self):
        svc, _ = support.build_service()
        rev = support.publish_night_signal(
            svc, publisher_id="tix", source="tix", record_id="tix-0920",
            visibility="restricted", allowlist=("hotel1",),
        )
        self.assertEqual(svc.get_signal("hotel1", rev.signal_id).revision, 1)
        with self.assertRaises(PermissionDenied):
            svc.get_signal("hotel2", rev.signal_id)

    def test_visibility_tightened_by_new_revision(self):
        svc, clock = support.build_service()
        rev = support.publish_night_signal(svc, visibility="network")  # T0
        clock.advance(hours=2)
        support.publish_night_signal(svc, revision=2, visibility="private",
                                     occurred_at="2026-09-15T12:00:00+08:00")
        # 当前:hotel1 已不可见
        with self.assertRaises(PermissionDenied):
            svc.get_signal("hotel1", rev.signal_id)
        # as-of 收紧之前:rev1 仍按当时的 network 级别可见
        seen = svc.get_signal("hotel1", rev.signal_id, as_of="2026-09-15T10:00:00+08:00")
        self.assertEqual(seen.revision, 1)


class GrantTest(unittest.TestCase):
    def _restricted_signal(self, svc):
        return support.publish_night_signal(
            svc, publisher_id="tix", source="tix", record_id="tix-0920",
            visibility="restricted",
        )

    def test_grant_then_revoke_evaluated_as_of(self):
        svc, _ = support.build_service()
        rev = self._restricted_signal(svc)  # recorded T0 = 09-15 09:00
        with self.assertRaises(PermissionDenied):
            svc.get_signal("hotel1", rev.signal_id)
        svc.grant_access(actor_id="ops", signal_id=rev.signal_id,
                         participant_id="hotel1",
                         effective_at="2026-09-16T00:00:00+08:00")
        # 授权生效前仍不可见
        with self.assertRaises(PermissionDenied):
            svc.get_signal("hotel1", rev.signal_id, as_of="2026-09-15T12:00:00+08:00")
        # 生效后可见
        self.assertEqual(
            svc.get_signal("hotel1", rev.signal_id, as_of="2026-09-17T00:00:00+08:00").revision,
            1,
        )
        # 收回后再次不可见;但历史 as-of 仍按当时权限可见
        svc.revoke_access(actor_id="ops", signal_id=rev.signal_id,
                          participant_id="hotel1",
                          effective_at="2026-09-18T00:00:00+08:00")
        with self.assertRaises(PermissionDenied):
            svc.get_signal("hotel1", rev.signal_id, as_of="2026-09-19T00:00:00+08:00")
        self.assertEqual(
            svc.get_signal("hotel1", rev.signal_id, as_of="2026-09-17T00:00:00+08:00").revision,
            1,
        )

    def test_publisher_can_grant_own_signal(self):
        svc, _ = support.build_service()
        rev = self._restricted_signal(svc)
        svc.grant_access(actor_id="tix", signal_id=rev.signal_id,
                         participant_id="hotel2",
                         effective_at="2026-09-16T00:00:00+08:00")
        self.assertEqual(
            svc.get_signal("hotel2", rev.signal_id, as_of="2026-09-16T12:00:00+08:00").revision,
            1,
        )

    def test_unrelated_participant_cannot_grant(self):
        svc, _ = support.build_service()
        rev = self._restricted_signal(svc)
        with self.assertRaises(PermissionDenied):
            svc.grant_access(actor_id="hotel1", signal_id=rev.signal_id,
                             participant_id="hotel2",
                             effective_at="2026-09-16T00:00:00+08:00")


if __name__ == "__main__":
    unittest.main()
