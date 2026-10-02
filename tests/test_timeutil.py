import unittest
from datetime import datetime, timedelta, timezone

import support  # noqa: F401  (负责把 src 放上 sys.path)

from event_signal import ValidationError, Window, format_instant, parse_instant

CN = timezone(timedelta(hours=8))


class ParseTest(unittest.TestCase):
    def test_parse_with_offset(self):
        instant = parse_instant("2026-09-20T09:00:00+08:00")
        self.assertEqual(instant.utcoffset(), timedelta(hours=8))
        self.assertEqual(format_instant(instant), "2026-09-20T09:00:00+08:00")

    def test_naive_time_rejected(self):
        with self.assertRaises(ValidationError):
            parse_instant("2026-09-20T09:00:00")

    def test_bad_string_rejected(self):
        with self.assertRaises(ValidationError):
            parse_instant("2026年9月20日")

    def test_passthrough_datetime(self):
        instant = datetime(2026, 9, 20, 9, tzinfo=CN)
        self.assertIs(parse_instant(instant), instant)


class WindowTest(unittest.TestCase):
    def test_end_must_be_after_start(self):
        with self.assertRaises(ValidationError):
            Window("2026-09-20T09:00:00+08:00", "2026-09-20T09:00:00+08:00")
        with self.assertRaises(ValidationError):
            Window("2026-09-21T02:00:00+08:00", "2026-09-20T20:00:00+08:00")

    def test_cross_midnight_session(self):
        night = Window(support.NIGHT_START, support.NIGHT_END)
        self.assertTrue(night.crosses_midnight)
        self.assertEqual(night.duration, timedelta(hours=6))
        # 跨午夜场次计入开场日
        self.assertEqual(night.business_date.isoformat(), "2026-09-20")

    def test_window_ending_at_midnight_is_not_cross_midnight(self):
        window = Window("2026-09-20T20:00:00+08:00", "2026-09-21T00:00:00+08:00")
        self.assertFalse(window.crosses_midnight)

    def test_same_day_window(self):
        window = Window("2026-09-20T10:00:00+08:00", "2026-09-20T22:00:00+08:00")
        self.assertFalse(window.crosses_midnight)

    def test_overlap_across_midnight(self):
        night = Window(support.NIGHT_START, support.NIGHT_END)
        late = Window("2026-09-20T23:00:00+08:00", "2026-09-21T01:00:00+08:00")
        self.assertTrue(night.overlaps(late))
        inter = night.intersection(late)
        self.assertEqual(inter.duration, timedelta(hours=2))
        self.assertAlmostEqual(night.fraction_covered_by(late), 2 / 6)

    def test_no_overlap_after_window(self):
        night = Window(support.NIGHT_START, support.NIGHT_END)
        after = Window("2026-09-21T03:00:00+08:00", "2026-09-21T04:00:00+08:00")
        self.assertFalse(night.overlaps(after))
        self.assertIsNone(night.intersection(after))
        self.assertEqual(night.fraction_covered_by(after), 0.0)

    def test_full_coverage(self):
        night = Window(support.NIGHT_START, support.NIGHT_END)
        self.assertEqual(night.fraction_covered_by(night), 1.0)


if __name__ == "__main__":
    unittest.main()
