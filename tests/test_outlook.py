"""需求聚合:可见性过滤、地域汇总、跨午夜按比例分摊、同源重叠告警。"""

import unittest

import support


def _totals(report):
    return {t.metric: t for t in report.totals}


class OutlookTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock = support.build_service()
        # 主办方:网络级,跨午夜场次
        self.s1 = support.publish_night_signal(
            self.svc, low=8000, expected=12000, high=15000,
        )
        # 票务方:受限,仅 hotel1 在白名单
        self.s2 = support.publish_night_signal(
            self.svc, publisher_id="tix", source="tix", record_id="tix-0920",
            low=6000, expected=9000, high=11000,
            visibility="restricted", allowlist=("hotel1",),
        )

    def outlook(self, viewer, start=support.NIGHT_START, end=support.NIGHT_END, geo="D1"):
        return self.svc.demand_outlook(viewer, geo, start, end)

    def test_visibility_filters_lines(self):
        hotel1_view = self.outlook("hotel1")
        self.assertEqual(len(hotel1_view.lines), 2)
        self.assertEqual(_totals(hotel1_view)["visitor_arrivals"].expected, 21000)
        self.assertEqual(_totals(hotel1_view)["visitor_arrivals"].publishers, 2)
        hotel2_view = self.outlook("hotel2")
        self.assertEqual(len(hotel2_view.lines), 1)
        self.assertEqual(_totals(hotel2_view)["visitor_arrivals"].expected, 12000)

    def test_proration_for_partial_window(self):
        # 只查询 20:00-23:00(6 小时窗口的一半)
        report = self.outlook("hotel2", start="2026-09-20T20:00:00+08:00",
                              end="2026-09-20T23:00:00+08:00")
        total = _totals(report)["visitor_arrivals"]
        self.assertEqual(total.expected, 6000)
        self.assertTrue(all(line.prorated for line in report.lines))

    def test_rollup_and_context(self):
        # 场馆级信号计入商圈合计;城市级信号只作背景
        support.publish_night_signal(
            self.svc, record_id="concert-venue", geo_id="V1",
            low=500, expected=1000, high=1500,
        )
        support.publish_night_signal(
            self.svc, record_id="city-festival", geo_id="CITY",
            low=40000, expected=50000, high=60000,
        )
        support.publish_night_signal(
            self.svc, record_id="other-district", geo_id="D2",
            low=1, expected=2, high=3,
        )
        report = self.outlook("hotel2")
        total = _totals(report)["visitor_arrivals"]
        self.assertEqual(total.expected, 13000)  # 12000 + 1000,不含 D2 与 CITY
        self.assertEqual(len(report.context), 1)
        self.assertEqual(report.context[0].signal_id, "org/city-festival")
        self.assertNotIn("org/other-district", {l.signal_id for l in report.lines})

    def test_same_source_overlap_warns(self):
        # 同一来源两条 record 时间窗重叠:可能重复计入
        support.publish_night_signal(
            self.svc, record_id="concert-0920-b", expected=4000,
            low=3000, high=5000,
        )
        report = self.outlook("hotel2")
        self.assertEqual(len(report.warnings), 1)
        warning = report.warnings[0]
        self.assertEqual(warning.source, "org")
        self.assertEqual(set(warning.record_ids), {"concert-0920", "concert-0920-b"})
        self.assertIn("重复计入", warning.message)

    def test_withdrawn_signal_excluded(self):
        self.clock.advance(hours=2)
        self.svc.withdraw_signal(
            publisher_id="tix", source="tix", record_id="tix-0920",
            revision=2, occurred_at="2026-09-15T11:00:00+08:00",
        )
        report = self.outlook("hotel1")
        self.assertEqual(_totals(report)["visitor_arrivals"].expected, 12000)
        # as-of 撤回前仍包含
        before = self.svc.demand_outlook(
            "hotel1", "D1", support.NIGHT_START, support.NIGHT_END,
            as_of="2026-09-15T10:00:00+08:00",
        )
        self.assertEqual(_totals(before)["visitor_arrivals"].expected, 21000)

    def test_unregistered_viewer_denied(self):
        from event_signal import PermissionDenied

        with self.assertRaises(PermissionDenied):
            self.outlook("outsider")


if __name__ == "__main__":
    unittest.main()
