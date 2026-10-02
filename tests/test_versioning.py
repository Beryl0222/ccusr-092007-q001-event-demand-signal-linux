import tempfile
import unittest
from pathlib import Path

from support import build_service, clock
from event_signal.errors import (
    ConflictError,
    DuplicateSubmission,
    StaleRevision,
)
from event_signal.models import Decision, SignalVersion


def signal_payload(sid="sig-1", publisher="tk", est=500, ref="ref-1",
                   visibility="industry", sectors=("hotel",), **extra):
    p = {
        "signal_id": sid, "publisher_id": publisher, "purpose": "hotel_blocking",
        "metric": "hotel_demand", "region_code": "bc-stadium",
        "window_start": clock(22).isoformat(), "window_end": clock(1, 30, day=21).isoformat(),
        "estimate": est, "unit": "room_night", "ci_low": est * 0.8,
        "ci_high": est * 1.2, "confidence": 0.9, "sample_size": 200,
        "cohort_key": "batch-x", "source_ref": ref,
        "visibility": visibility, "sectors": list(sectors),
    }
    p.update(extra)
    return p


class VersionChainTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()

    def test_publish_then_revise_creates_new_immutable_version(self):
        v1 = self.svc.publish_signal(signal_payload(), recorded_at=clock(9, day=1))
        self.assertEqual(v1.revision, 1)
        v2 = self.svc.revise_signal(
            "sig-1", {"expected_revision": 1, "estimate": 420,
                      "ci_low": 350, "ci_high": 490, "actor_id": "tk"},
            recorded_at=clock(9, day=2))
        self.assertEqual((v2.revision, v2.supersedes, v2.estimate), (2, 1, 420))
        # 历史版本原样保留，不被改写
        self.assertEqual(self.svc.ledger.version("sig-1", 1).estimate, 500)

    def test_stale_revision_rejected(self):
        self.svc.publish_signal(signal_payload(), recorded_at=clock(9, day=1))
        self.svc.revise_signal("sig-1", {"expected_revision": 1, "estimate": 420},
                               recorded_at=clock(9, day=2))
        with self.assertRaises(StaleRevision):
            self.svc.revise_signal("sig-1", {"expected_revision": 1, "estimate": 1},
                                   recorded_at=clock(9, day=3))

    def test_withdraw_is_a_tombstone_not_a_delete(self):
        self.svc.publish_signal(signal_payload())
        tomb = self.svc.withdraw_signal(
            "sig-1", {"expected_revision": 1, "actor_id": "tk"})
        self.assertEqual((tomb.status, tomb.revision), ("withdrawn", 2))
        self.assertEqual(len(self.svc.ledger.versions("sig-1")), 2)
        with self.assertRaises(ConflictError):
            self.svc.revise_signal("sig-1", {"estimate": 1})

    def test_duplicate_source_ref_is_not_double_counted(self):
        self.svc.publish_signal(signal_payload(est=500), recorded_at=clock(9, day=1))
        with self.assertRaises(DuplicateSubmission) as ctx:
            self.svc.publish_signal(signal_payload(sid="sig-2", est=500))
        self.assertEqual(ctx.exception.existing_id, "sig-1")

    def test_as_of_shows_only_what_had_arrived(self):
        self.svc.publish_signal(signal_payload(est=500), recorded_at=clock(9, day=1))
        self.svc.revise_signal("sig-1", {"expected_revision": 1, "estimate": 420},
                               recorded_at=clock(9, day=10))
        self.assertEqual(self.svc.ledger.version_as_of("sig-1", clock(12, day=1)).estimate, 500)
        self.assertEqual(self.svc.ledger.version_as_of("sig-1", clock(12, day=10)).estimate, 420)

    def test_late_data_after_decision_forms_new_version(self):
        self.svc.publish_signal(signal_payload(est=500), recorded_at=clock(9, day=1))
        d = self.svc.register_decision({
            "party_id": "hotel-a", "metric": "hotel_demand",
            "region_code": "bc-stadium",
            "window_start": clock(22).isoformat(),
            "window_end": clock(1, 30, day=21).isoformat(),
            "provision": 500, "unit": "room_night", "basis": {"sig-1": 1},
            "rationale": "按 r1 锁房",
        }, recorded_at=clock(10, day=1))
        # 迟到数据：业务窗已近，系统 9/19 才收到修订
        late = self.svc.revise_signal(
            "sig-1", {"expected_revision": 1, "estimate": 300, "actor_id": "tk"},
            recorded_at=clock(8, day=19))
        self.assertEqual(late.revision, 2)
        # 决定登记时所见仍是 r1
        self.assertEqual(self.svc.ledger.version("sig-1", d.basis["sig-1"]).estimate, 500)

    def test_persistence_replays(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "log.jsonl")
            svc1 = build_service(log_path=path)
            svc1.publish_signal(signal_payload(), recorded_at=clock(9, day=1))
            svc1.revise_signal("sig-1", {"expected_revision": 1, "estimate": 420},
                               recorded_at=clock(9, day=2))
            svc2 = build_service(log_path=path)
            self.assertEqual(svc2.ledger.latest("sig-1").estimate, 420)
            self.assertEqual(svc2.ledger.version("sig-1", 1).estimate, 500)

    def test_decision_and_outcome_round_trip_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "log.jsonl")
            svc1 = build_service(log_path=path)
            svc1.publish_signal(signal_payload(), recorded_at=clock(9, day=1))
            svc1.register_decision({
                "party_id": "hotel-a", "metric": "hotel_demand",
                "region_code": "bc-stadium",
                "window_start": clock(22).isoformat(),
                "window_end": clock(1, 30, day=21).isoformat(),
                "provision": 500, "unit": "room_night", "basis": {"sig-1": 1},
                "rationale": "锁房",
            }, recorded_at=clock(10, day=1))
            svc1.record_outcome({
                "metric": "hotel_demand", "region_code": "bc-stadium",
                "window_start": clock(22).isoformat(),
                "window_end": clock(1, 30, day=21).isoformat(),
                "actual": 300, "unit": "room_night", "sample_size": 50,
                "cohort_key": "batch-x", "source": "op",
            }, recorded_at=clock(12, day=21))

            svc2 = build_service(log_path=path)
            self.assertEqual(len(svc2.ledger.decisions()), 1)
            d = svc2.ledger.latest_decision(  # 重放后可按 id 取回
                svc1.ledger.decisions()[0].decision_id)
            self.assertEqual(d.provision, 500)
            self.assertEqual(d.basis, {"sig-1": 1})
            outcomes = svc2.ledger.outcomes()
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(outcomes[0].actual, 300)
            self.assertEqual(outcomes[0].cohort_key, "batch-x")


if __name__ == "__main__":
    unittest.main()
