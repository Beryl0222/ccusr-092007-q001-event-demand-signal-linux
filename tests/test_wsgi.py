import io
import json
import unittest

from support import clock, iso
from event_signal.wsgi import build_app


def call(app, method, path, payload=None, party=""):
    body = json.dumps(payload).encode() if payload is not None else b""
    if "?" in path:
        path_info, query_string = path.split("?", 1)
    else:
        path_info, query_string = path, ""
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path_info,
        "QUERY_STRING": query_string,
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body),
    }
    if party:
        environ["HTTP_X_PARTY_ID"] = party
    captured = {}

    def start_response(status, headers):
        captured["status"] = int(status.split()[0])

    raw = app(environ, start_response)
    return captured["status"], json.loads(b"".join(raw).decode())


class WsgiFlowTest(unittest.TestCase):
    def setUp(self):
        self.app = build_app()
        s = self.app.service
        for p in (
            {"party_id": "org", "name": "主办方", "role": "organizer"},
            {"party_id": "tk", "name": "票务", "role": "ticketer"},
            {"party_id": "hotel-a", "name": "酒店甲", "role": "hotel",
             "sectors": ["hotel"]},
            {"party_id": "hotel-b", "name": "酒店乙", "role": "hotel",
             "sectors": ["hotel"]},
            {"party_id": "hotel-c", "name": "酒店丙", "role": "hotel",
             "sectors": ["hotel"]},
            {"party_id": "op", "name": "值班长", "role": "operator",
             "can_bypass_threshold": True},
        ):
            call(self.app, "POST", "/v1/parties", p)
        for r in (
            {"code": "city-1", "name": "城市", "level": "city"},
            {"code": "bc", "name": "商圈", "level": "business_circle",
             "parent": "city-1"},
        ):
            call(self.app, "POST", "/v1/regions", r)

    def _sig(self, sid, who, est, cohort, ref, **extra):
        p = {
            "signal_id": sid, "publisher_id": who, "purpose": "hotel",
            "metric": "hotel_demand", "region_code": "bc",
            "window_start": iso(clock(22)), "window_end": iso(clock(1, 30, day=21)),
            "estimate": est, "unit": "room_night", "sample_size": 50,
            "cohort_key": cohort, "source_ref": ref,
            "visibility": "industry", "sectors": ["hotel"],
            "recorded_at": iso(clock(9, day=1)),
        }
        p.update(extra)
        return p

    def test_full_flow_publish_decide_revise_outcome_reconstruct(self):
        app = self.app
        st, _ = call(app, "POST", "/v1/signals", self._sig("s1", "tk", 300, "c1", "r1"), "tk")
        self.assertEqual(st, 200)
        call(app, "POST", "/v1/signals", self._sig("s2", "hotel-a", 40, "c2", "r2"), "hotel-a")
        call(app, "POST", "/v1/signals", self._sig("s3", "hotel-b", 50, "c3", "r3"), "hotel-b")

        # 重复提交返回 409 幂等冲突
        st, body = call(app, "POST", "/v1/signals",
                        self._sig("s1x", "tk", 300, "c1", "r1"), "tk")
        self.assertEqual(st, 409)
        self.assertEqual(body["error"], "duplicate_submission")
        self.assertEqual(body["details"]["existing_id"], "s1")

        # 聚合
        q = {"metric": "hotel_demand", "region_code": "bc",
             "window_start": iso(clock(22)), "window_end": iso(clock(1, 30, day=21)),
             "as_of": iso(clock(9, day=1))}
        st, body = call(app, "POST", "/v1/aggregations", q, "hotel-a")
        self.assertEqual(st, 200)
        self.assertAlmostEqual(body["point"], 390.0)

        # 登记决定
        st, dec = call(app, "POST", "/v1/decisions", {
            "party_id": "hotel-a", "metric": "hotel_demand", "region_code": "bc",
            "window_start": iso(clock(22)), "window_end": iso(clock(1, 30, day=21)),
            "provision": 320, "unit": "room_night",
            "basis": {"s1": 1, "s2": 1}, "rationale": "锁房"}, "hotel-a")
        self.assertEqual(st, 200)

        # 非发布方修订被拒
        st, body = call(app, "POST", "/v1/signals/s1/revisions",
                        {"expected_revision": 1, "estimate": 180}, "hotel-a")
        self.assertEqual(st, 403)
        # 发布方修订成功
        st, body = call(app, "POST", "/v1/signals/s1/revisions",
                        {"expected_revision": 1, "estimate": 180,
                         "ci_low": 150, "ci_high": 210}, "tk")
        self.assertEqual((st, body["revision"]), (200, 2))

        # GET 版本历史（as_of 只能看到当时已送达版本）；查询串需 URL 编码
        from urllib.parse import quote
        st, hist = call(app, "GET",
                        f"/v1/signals/s1?as_of={quote(iso(clock(9, day=1)))}",
                        party="tk")
        self.assertEqual(st, 200)
        self.assertEqual(len(hist), 1)

        # 实绩回填 ×3 cohort 后重建
        for oid, actual, coh in (("o1", 200, "c1"), ("o2", 20, "c2"), ("o3", 25, "c3")):
            st, _ = call(app, "POST", "/v1/outcomes", {
                "metric": "hotel_demand", "region_code": "bc",
                "window_start": iso(clock(22)), "window_end": iso(clock(1, 30, day=21)),
                "actual": actual, "unit": "room_night", "sample_size": 100,
                "cohort_key": coh, "source": "op"}, "op")
            self.assertEqual(st, 200)
        st, rep = call(app, "POST", "/v1/reconstructions", q, "hotel-a")
        self.assertEqual(st, 200)
        self.assertAlmostEqual(rep["summary"]["total_cost"], 75.0)
        kinds = {e["kind"] for e in rep["timeline"]}
        self.assertEqual(kinds, {"signal_published", "signal_revised",
                                 "decision", "outcome"})

    def test_validation_error_on_bad_window(self):
        st, body = call(self.app, "POST", "/v1/signals", self._sig(
            "s9", "tk", 1, "c9", "r9",
            window_start=iso(clock(2)), window_end=iso(clock(1))), "tk")
        self.assertEqual(st, 422)
        self.assertEqual(body["error"], "validation_error")

    def test_unknown_route(self):
        st, _ = call(self.app, "POST", "/v1/nope", {}, "tk")
        self.assertEqual(st, 404)


if __name__ == "__main__":
    unittest.main()
