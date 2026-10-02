import unittest
from datetime import timedelta

from support import TZ, clock
from event_signal.timeutils import (
    hourly_buckets,
    overlap_seconds,
    parse_iso,
    resolve_clock_window,
    spans_midnight,
    to_iso,
)


class TimeWindowTest(unittest.TestCase):
    def test_parse_requires_offset(self):
        with self.assertRaises(ValueError):
            parse_iso("2026-09-20T09:00:00")
        dt = parse_iso("2026-09-20T09:00:00+08:00")
        self.assertEqual(dt.utcoffset(), timedelta(hours=8))
        self.assertEqual(to_iso(parse_iso("2026-09-20T01:00:00Z")),
                         "2026-09-20T01:00:00+00:00")

    def test_clock_window_wraps_past_midnight(self):
        day = clock(0)
        start, end = resolve_clock_window(day, "22:00", "01:30")
        self.assertEqual(start, clock(22))
        self.assertEqual(end, clock(1, 30, day=21))
        self.assertTrue(spans_midnight(start, end))
        self.assertEqual((end - start), timedelta(hours=3, minutes=30))

    def test_clock_window_same_day(self):
        day = clock(0)
        start, end = resolve_clock_window(day, "09:00", "12:00")
        self.assertEqual((start, end), (clock(9), clock(12)))
        self.assertFalse(spans_midnight(start, end))

    def test_hourly_buckets_align_across_midnight(self):
        buckets = hourly_buckets(clock(22, 30), clock(1, day=21))
        self.assertEqual(len(buckets), 3)
        # 首桶截到下一整点，尾桶不足一小时
        self.assertEqual(buckets[0][0], clock(22, 30))
        self.assertEqual(buckets[0][1], clock(23))
        self.assertEqual(buckets[-1][1], clock(1, day=21))

    def test_overlap(self):
        self.assertEqual(
            overlap_seconds(clock(22), clock(2, day=21),
                            clock(23), clock(1, day=21)),
            2 * 3600)
        self.assertEqual(
            overlap_seconds(clock(22), clock(23), clock(23, 30), clock(0, day=21)),
            0)


if __name__ == "__main__":
    unittest.main()
