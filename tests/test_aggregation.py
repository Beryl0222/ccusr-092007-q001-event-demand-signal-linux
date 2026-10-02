import unittest

from support import build_service, clock
from event_signal.errors import PermissionDenied, SuppressedAggregate

WS = clock(22)
WE = clock(2, day=21)


def base(sid, publisher, est, cohort, ref, *, visibility="industry",
         sectors=("hotel",), sample=100, start=WS, end=WE, ci=True):
    p = {
        "signal_id": sid, "publisher_id": publisher, "purpose": "hotel_blocking",
        "metric": "hotel_demand", "region_code": "bc-stadium",
        "window_start": start.isoformat(), "window_end": end.isoformat(),
        "estimate": est, "unit": "room_night", "sample_size": sample,
        "cohort_key": cohort, "source_ref": ref,
        "visibility": visibility, "sectors": list(sectors),
    }
    if ci:
        p["ci_low"], p["ci_high"] = est * 0.8, est * 1.2
    return p


class AggregationTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service(min_cohorts=3, min_sample=10)
        self.now = clock(9, day=1)

    def _publish_three_cohorts(self):
        s = self.svc
        # 票务批次 cohort A：300，跨 22:00-02:00
        s.publish_signal(base("sig-tk", "tk", 300, "batch-tk", "r1"),
                         recorded_at=self.now)
        # 两家酒店各自的私有预订队列，分别独立 cohort
        s.publish_signal(base("sig-a", "hotel-a", 40, "batch-a", "r2",
                              visibility="private"), recorded_at=self.now)
        s.publish_signal(base("sig-b", "hotel-b", 50, "batch-b", "r3",
                              visibility="industry"), recorded_at=self.now)

    def test_aggregation_across_three_cohorts(self):
        self._publish_three_cohorts()
        # hotel-a 的私有信号只有本人可见；hotel-b 行业可见。
        # 对 hotel-b：可见 tk(300)+b(50)，仅 2 cohort -> 抑制
        with self.assertRaises(SuppressedAggregate):
            self.svc.query_aggregate("hotel-b", {
                "metric": "hotel_demand", "region_code": "bc-stadium",
                "window_start": WS.isoformat(), "window_end": WE.isoformat(),
                "as_of": self.now.isoformat()})

    def test_publisher_sees_own_private_plus_industry(self):
        self._publish_three_cohorts()
        res = self.svc.query_aggregate("hotel-a", {
            "metric": "hotel_demand", "region_code": "bc-stadium",
            "window_start": WS.isoformat(), "window_end": WE.isoformat(),
            "as_of": self.now.isoformat()})
        # tk 300 + 自己私有 40 + hotel-b 50 = 390，3 个 cohort
        self.assertAlmostEqual(res["point"], 390.0)
        self.assertEqual(res["cohort_count"], 3)

    def test_same_cohort_duplicate_is_deduplicated_by_time_arbitration(self):
        s = self.svc
        # 主办方转发票务同批数据（同 cohort），窗更大 21:00-03:00，est=600
        s.publish_signal(base("sig-tk", "tk", 300, "dup", "r1",
                              start=clock(22), end=clock(2, day=21)),
                         recorded_at=self.now)
        s.publish_signal(base("sig-org", "org", 600, "dup", "r2",
                              visibility="open", sectors=(),
                              start=clock(21), end=clock(3, day=21)),
                         recorded_at=self.now)
        # 再补两个独立 cohort 跨过阈值
        s.publish_signal(base("sig-a", "hotel-a", 10, "ca", "r3",
                              visibility="industry"), recorded_at=self.now)
        s.publish_signal(base("sig-b", "hotel-b", 10, "cb", "r4"),
                         recorded_at=self.now)
        res = s.query_aggregate("hotel-a", {
            "metric": "hotel_demand", "region_code": "bc-stadium",
            "window_start": WS.isoformat(), "window_end": WE.isoformat(),
            "as_of": self.now.isoformat()})
        # organizer 优先级更高，其窗 21-03 完全覆盖查询 22-02：
        # 600 * 4h/6h = 400；tk 被全覆盖计 0；+10+10 = 420
        self.assertAlmostEqual(res["point"], 420.0)
        self.assertEqual(res["cohort_count"], 3)

    def test_partial_window_proportional_split(self):
        s = self.svc
        # 查询只取信号窗的后半段 00:00-02:00
        s.publish_signal(base("sig-tk", "tk", 300, "dup", "r1"),
                         recorded_at=self.now)
        s.publish_signal(base("sig-org", "org", 600, "dup", "r2",
                              visibility="open", sectors=(),
                              start=clock(21), end=clock(3, day=21)),
                         recorded_at=self.now)
        s.publish_signal(base("sig-a", "hotel-a", 12, "ca", "r3",
                              visibility="industry"), recorded_at=self.now)
        s.publish_signal(base("sig-b", "hotel-b", 12, "cb", "r4"),
                         recorded_at=self.now)
        res = s.query_aggregate("hotel-a", {
            "metric": "hotel_demand", "region_code": "bc-stadium",
            "window_start": clock(0, day=21).isoformat(),
            "window_end": clock(2, day=21).isoformat(),
            "as_of": self.now.isoformat()})
        # org 600 均摊 6h，查询占 2h -> 200；tk 同 cohort 被覆盖 -> 0
        self.assertAlmostEqual(res["point"], 200 + 12 * (2 / 4) * 2)

    def test_hourly_buckets_split_cross_midnight(self):
        self._publish_three_cohorts()
        res = self.svc.query_aggregate("hotel-a", {
            "metric": "hotel_demand", "region_code": "bc-stadium",
            "window_start": WS.isoformat(), "window_end": WE.isoformat(),
            "as_of": self.now.isoformat(), "hourly": True})
        self.assertEqual(len(res["buckets"]), 4)
        self.assertAlmostEqual(sum(b["point"] for b in res["buckets"]),
                               res["point"])

    def test_unit_normalization(self):
        s = self.svc
        # attendance（person 系）验证 kperson -> person 归一
        s.publish_signal({
            "signal_id": "at1", "publisher_id": "org", "purpose": "crowd",
            "metric": "attendance", "region_code": "bc-stadium",
            "window_start": WS.isoformat(), "window_end": WE.isoformat(),
            "estimate": 0.3, "unit": "kperson", "sample_size": 100,
            "cohort_key": "dup", "visibility": "open", "sectors": [],
            "source_ref": "r9"}, recorded_at=self.now)
        s.publish_signal(base("sig-a", "hotel-a", 12, "ca", "r3",
                              visibility="industry"), recorded_at=self.now)
        s.publish_signal(base("sig-b", "hotel-b", 12, "cb", "r4"),
                         recorded_at=self.now)
        # attendance 查询：同 dup cohort 0.3kperson 与一条 person 口径
        s.publish_signal({
            "signal_id": "at2", "publisher_id": "tk", "purpose": "crowd",
            "metric": "attendance", "region_code": "bc-stadium",
            "window_start": WS.isoformat(), "window_end": WE.isoformat(),
            "estimate": 200, "unit": "person", "sample_size": 100,
            "cohort_key": "other", "visibility": "open", "sectors": [],
            "source_ref": "r10"}, recorded_at=self.now)
        s.publish_signal({
            "signal_id": "at3", "publisher_id": "org", "purpose": "crowd",
            "metric": "attendance", "region_code": "bc-stadium",
            "window_start": WS.isoformat(), "window_end": WE.isoformat(),
            "estimate": 50, "unit": "person", "sample_size": 100,
            "cohort_key": "third", "visibility": "open", "sectors": [],
            "source_ref": "r11"}, recorded_at=self.now)
        res = s.query_aggregate("hotel-a", {
            "metric": "attendance", "region_code": "bc-stadium",
            "window_start": WS.isoformat(), "window_end": WE.isoformat(),
            "as_of": self.now.isoformat()})
        # 0.3 kperson = 300 person（dup cohort），+200 +50
        self.assertAlmostEqual(res["point"], 550.0)
        self.assertEqual(res["unit"], "person")

    def test_region_rollup_never_drills_down(self):
        self._publish_three_cohorts()
        # venue 级查询不能使用商圈级信号
        with self.assertRaises(SuppressedAggregate):
            self.svc.query_aggregate("hotel-a", {
                "metric": "hotel_demand", "region_code": "v-stadium",
                "window_start": WS.isoformat(), "window_end": WE.isoformat(),
                "as_of": self.now.isoformat()})
        # 城市级可以把商圈信号向上汇总
        res = self.svc.query_aggregate("hotel-a", {
            "metric": "hotel_demand", "region_code": "city-1",
            "window_start": WS.isoformat(), "window_end": WE.isoformat(),
            "as_of": self.now.isoformat()})
        self.assertAlmostEqual(res["point"], 390.0)


class VisibilityTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()
        self.now = clock(9, day=1)

    def test_private_signal_hidden_from_other_parties_but_operator(self):
        self.svc.publish_signal(base("p1", "hotel-a", 30, "ca", "r1",
                                     visibility="private"),
                                recorded_at=self.now)
        self.svc.publish_signal(base("p2", "hotel-b", 30, "cb", "r2",
                                     visibility="private"),
                                recorded_at=self.now)
        self.svc.publish_signal(base("p3", "hotel-c", 30, "cc", "r3",
                                     visibility="private"),
                                recorded_at=self.now)
        # 其他酒店：三个 private 全不可见 -> 无信号 NotFound 由聚合转抑制
        from event_signal.errors import SuppressedAggregate
        with self.assertRaises(SuppressedAggregate):
            self.svc.query_aggregate("hotel-b", {
                "metric": "hotel_demand", "region_code": "bc-stadium",
                "window_start": WS.isoformat(), "window_end": WE.isoformat(),
                "as_of": self.now.isoformat()})
        # 运营中心也看不到 private 明文聚合（只能让它们进入阈值内的行业聚合）
        with self.assertRaises(SuppressedAggregate):
            self.svc.query_aggregate("op", {
                "metric": "hotel_demand", "region_code": "bc-stadium",
                "window_start": WS.isoformat(), "window_end": WE.isoformat(),
                "as_of": self.now.isoformat()})

    def test_party_whitelist(self):
        self.svc.publish_signal(base("w1", "org", 30, "cw", "r1",
                                     visibility="open", sectors=()),
                                recorded_at=self.now)
        self.svc.publish_signal({
            **base("w2", "tk", 40, "c2", "r2", visibility="parties",
                   sectors=()),
            "audience": ["hotel-a"]}, recorded_at=self.now)
        self.svc.publish_signal(base("w3", "hotel-c", 50, "c3", "r3"),
                                recorded_at=self.now)
        # hotel-a 在白名单：open + parties(w2) + industry(w3) = 3 cohort
        res = self.svc.query_aggregate("hotel-a", {
            "metric": "hotel_demand", "region_code": "bc-stadium",
            "window_start": WS.isoformat(), "window_end": WE.isoformat(),
            "as_of": self.now.isoformat()})
        self.assertEqual(res["cohort_count"], 3)
        # hotel-b 不在白名单：看不到 w2，只有 open+industry = 2 -> 抑制
        with self.assertRaises(SuppressedAggregate):
            self.svc.query_aggregate("hotel-b", {
                "metric": "hotel_demand", "region_code": "bc-stadium",
                "window_start": WS.isoformat(), "window_end": WE.isoformat(),
                "as_of": self.now.isoformat()})

    def test_suspended_sector_access_replays_point_in_time(self):
        svc = self.svc
        svc.publish_signal(base("i1", "tk", 30, "c1", "r1"),
                           recorded_at=self.now)
        svc.publish_signal(base("i2", "hotel-b", 40, "c2", "r2"),
                           recorded_at=self.now)
        svc.publish_signal(base("i3", "hotel-c", 50, "c3", "r3"),
                           recorded_at=self.now)
        # 暂停前 hotel-a 能聚合到 3 cohort
        res = svc.query_aggregate("hotel-a", {
            "metric": "hotel_demand", "region_code": "bc-stadium",
            "window_start": WS.isoformat(), "window_end": WE.isoformat(),
            "as_of": self.now.isoformat()})
        self.assertEqual(res["cohort_count"], 3)
        # 9/5 起 hotel-a 被移出酒店行业组
        svc.suspend_sector({"grantee_id": "hotel-a", "sector": "hotel",
                            "at": clock(9, day=5).isoformat()})
        with self.assertRaises(SuppressedAggregate):
            svc.query_aggregate("hotel-a", {
                "metric": "hotel_demand", "region_code": "bc-stadium",
                "window_start": WS.isoformat(), "window_end": WE.isoformat(),
                "as_of": clock(9, day=6).isoformat()})
        # 但以 9/4 的身份重放历史，仍然可见（审计可还原）
        hist = svc.query_aggregate("hotel-a", {
            "metric": "hotel_demand", "region_code": "bc-stadium",
            "window_start": WS.isoformat(), "window_end": WE.isoformat(),
            "as_of": clock(9, day=4).isoformat()})
        self.assertEqual(hist["cohort_count"], 3)


if __name__ == "__main__":
    unittest.main()
