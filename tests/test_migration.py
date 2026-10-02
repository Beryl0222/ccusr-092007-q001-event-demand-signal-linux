import unittest
from pathlib import Path

from support import build_service, clock
from event_signal.contracts import load_record
from event_signal.errors import ValidationError
from event_signal.migration import migrate_legacy
from event_signal.models import STATUS_ACTIVE

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "demand_signal.json"


class MigrationTest(unittest.TestCase):
    def test_legacy_sample_keeps_identifiers_and_semantics(self):
        record = load_record(FIXTURE)
        self.assertEqual(record.record_id, "sample-001")
        self.assertEqual(record.domain, "event_signal")
        self.assertEqual(record.source, "业务样例")

        ver = migrate_legacy(record, fields={
            "purpose": "hotel_blocking",
            "metric": "hotel_demand",
            "region_code": "bc-stadium",
            "window_end": "2026-09-20T12:00:00+08:00",
            "estimate": 300, "unit": "room_night",
        })
        # 标识与时间语义保持
        self.assertEqual(ver.signal_id, "sample-001")
        self.assertEqual(ver.publisher_id, "业务样例")
        self.assertEqual(ver.window_start.isoformat(), record.occurred_at)
        self.assertEqual(ver.revision, 1)
        self.assertEqual(ver.status, STATUS_ACTIVE)
        self.assertIn("v1", ver.note)
        self.assertEqual(ver.source_ref, "v1:sample-001")

    def test_migrated_record_enters_aggregation(self):
        svc = build_service()
        record = load_record(FIXTURE)
        ver = migrate_legacy(record, fields={
            "publisher_id": "tk",
            "purpose": "hotel_blocking",
            "metric": "hotel_demand",
            "region_code": "bc-stadium",
            "window_end": "2026-09-20T12:00:00+08:00",
            "estimate": 300, "unit": "room_night",
            "cohort_key": "legacy-1", "sample_size": 50,
            "visibility": "industry", "sectors": ["hotel"],
        })
        svc.ledger.publish(ver, recorded_at=clock(9, day=1))
        # 再补两个独立 cohort，迁来的信号正常进入聚合
        for sid, who, cohort in (("m2", "hotel-a", "c-a"), ("m3", "hotel-b", "c-b")):
            svc.publish_signal({
                "signal_id": sid, "publisher_id": who, "purpose": "hotel_blocking",
                "metric": "hotel_demand", "region_code": "bc-stadium",
                "window_start": "2026-09-20T09:00:00+08:00",
                "window_end": "2026-09-20T12:00:00+08:00",
                "estimate": 20, "unit": "room_night", "sample_size": 30,
                "cohort_key": cohort, "source_ref": f"ref-{sid}",
                "visibility": "industry", "sectors": ["hotel"],
            }, recorded_at=clock(9, day=1))
        res = svc.query_aggregate("hotel-a", {
            "metric": "hotel_demand", "region_code": "bc-stadium",
            "window_start": "2026-09-20T09:00:00+08:00",
            "window_end": "2026-09-20T12:00:00+08:00",
            "as_of": clock(10, day=1).isoformat()})
        self.assertEqual(res["cohort_count"], 3)
        self.assertAlmostEqual(res["point"], 340.0)

    def test_migration_requires_v2_fields(self):
        record = load_record(FIXTURE)
        with self.assertRaises(ValidationError):
            migrate_legacy(record, fields={"metric": "hotel_demand"})
