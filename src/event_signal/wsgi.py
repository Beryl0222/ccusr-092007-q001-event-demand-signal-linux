"""HTTP/JSON 接口（纯标准库 WSGI）。

路由：

* ``POST /v1/parties``、``/v1/regions``                 目录登记
* ``POST /v1/grants``、``/v1/grants/revoke``            授权变化
* ``POST /v1/sectors/suspend``、``/sectors/restore``    行业资格暂停/恢复
* ``POST /v1/signals``                                  发布（幂等键 source_ref）
* ``POST /v1/signals/{id}/revisions``                   修订（新版本）
* ``POST /v1/signals/{id}/withdrawal``                  撤回（墓碑版本）
* ``GET  /v1/signals/{id}?as_of=...``                   版本历史 / as-of 还原
* ``POST /v1/decisions``                                登记供给决定
* ``POST /v1/outcomes``                                 实绩回填
* ``POST /v1/aggregations``                             隐私安全聚合查询
* ``POST /v1/reconstructions``                          商圈决策链重建

身份用请求头 ``X-Party-Id`` 标识（真实部署应替换为鉴权网关注入，
协议层只负责按该身份执行可见性规则）。
"""

from __future__ import annotations

import json
from typing import Any, Callable
from urllib.parse import parse_qs

from .audit import AuditLog
from .errors import SignalError
from .geography import RegionTree
from .metrics import MetricRegistry
from .parties import PartyDirectory
from .service import DemandSignalService
from .store import AppendLog, SignalLedger


class WsgiApp:
    def __init__(self, service: DemandSignalService) -> None:
        self.service = service

    # ---- WSGI 入口 -------------------------------------------------------

    def __call__(self, environ: dict, start_response: Callable) -> list[bytes]:
        method = environ["REQUEST_METHOD"]
        path = environ.get("PATH_INFO", "/")
        try:
            if method == "GET" and path.startswith("/v1/signals/"):
                signal_id = path.rsplit("/", 1)[-1]
                qs = parse_qs(environ.get("QUERY_STRING", ""))
                payload: dict[str, Any] = {}
                if qs.get("as_of"):
                    payload["as_of"] = qs["as_of"][0]
                viewer = environ.get("HTTP_X_PARTY_ID", "")
                from .timeutils import parse_iso
                as_of = parse_iso(payload["as_of"]) if payload.get("as_of") else None
                body = self.service.signal_history(viewer, signal_id, as_of=as_of)
                return self._json(start_response, 200, body)

            if method != "POST":
                return self._error(start_response, 405, "method_not_allowed",
                                   f"不支持 {method}")
            data = self._read_json(environ)
            viewer = environ.get("HTTP_X_PARTY_ID", "")
            body = self._route(path, data, viewer)
            return self._json(start_response, 200, body)
        except SignalError as exc:
            return self._error(start_response, exc.http_status, exc.code,
                               exc.message, exc.details)
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            return self._error(start_response, 400, "bad_request", str(exc))

    def _route(self, path: str, data: dict[str, Any], viewer: str) -> Any:
        s = self.service
        recorded_at = self._maybe_dt(data.pop("recorded_at", None))
        if path == "/v1/parties":
            return s.register_party(data).party_id and {"party_id": data["party_id"]}
        if path == "/v1/regions":
            r = s.add_region(data)
            return {"code": r.code, "level": r.level, "parent": r.parent}
        if path == "/v1/grants":
            s.grant(data)
            return {"status": "granted"}
        if path == "/v1/grants/revoke":
            s.revoke_group(data)
            return {"status": "revoked"}
        if path == "/v1/sectors/suspend":
            s.suspend_sector(data)
            return {"status": "suspended"}
        if path == "/v1/sectors/restore":
            s.restore_sector(data)
            return {"status": "restored"}
        if path == "/v1/signals":
            ver = s.publish_signal(data, recorded_at=recorded_at)
            return {"signal_id": ver.signal_id, "revision": ver.revision,
                    "recorded_at": ver.recorded_at.isoformat()}
        if path.startswith("/v1/signals/") and path.endswith("/revisions"):
            signal_id = path.split("/")[3]
            data.setdefault("actor_id", viewer)
            ver = s.revise_signal(signal_id, data, recorded_at=recorded_at)
            return {"signal_id": signal_id, "revision": ver.revision}
        if path.startswith("/v1/signals/") and path.endswith("/withdrawal"):
            signal_id = path.split("/")[3]
            data.setdefault("actor_id", viewer)
            ver = s.withdraw_signal(signal_id, data, recorded_at=recorded_at)
            return {"signal_id": signal_id, "revision": ver.revision,
                    "status": ver.status}
        if path == "/v1/decisions":
            d = s.register_decision(data, recorded_at=recorded_at)
            return {"decision_id": d.decision_id, "revision": d.revision}
        if path == "/v1/outcomes":
            o = s.record_outcome(data, recorded_at=recorded_at)
            return {"outcome_id": o.outcome_id}
        if path == "/v1/aggregations":
            return s.query_aggregate(viewer, data)
        if path == "/v1/reconstructions":
            return s.reconstruct(viewer, data)
        from .errors import NotFound
        raise NotFound(f"未知路由: {path}")

    @staticmethod
    def _maybe_dt(value: Any):
        if not value:
            return None
        from .timeutils import parse_iso
        return parse_iso(value)

    # ---- 编解码 ----------------------------------------------------------

    @staticmethod
    def _read_json(environ: dict) -> dict[str, Any]:
        length = int(environ.get("CONTENT_LENGTH") or 0)
        if length <= 0:
            return {}
        raw = environ["wsgi.input"].read(length)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return payload

    @staticmethod
    def _json(start_response: Callable, status: int, body: Any) -> list[bytes]:
        raw = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        start_response(f"{status} OK", [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(raw))),
        ])
        return [raw]

    @staticmethod
    def _error(start_response: Callable, status: int, code: str,
               message: str, details: dict | None = None) -> list[bytes]:
        payload: dict[str, Any] = {"error": code, "message": message}
        if details:
            payload["details"] = details
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        reason = {400: "Bad Request", 403: "Forbidden", 404: "Not Found",
                  405: "Method Not Allowed", 409: "Conflict",
                  422: "Unprocessable Entity"}.get(status, "Error")
        start_response(f"{status} {reason}", [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(raw))),
        ])
        return [raw]


def build_app(*, log_path: str | None = None, audit_path: str | None = None,
              min_cohorts: int = 3, min_sample: int = 10) -> WsgiApp:
    """装配整套服务；``log_path=None`` 时为纯内存（测试/演示）。"""

    ledger = SignalLedger(AppendLog(log_path))
    service = DemandSignalService(
        ledger=ledger,
        parties=PartyDirectory(),
        regions=RegionTree(),
        metrics=MetricRegistry(),
        audit=AuditLog(audit_path),
        min_cohorts=min_cohorts,
        min_sample=min_sample,
    )
    return WsgiApp(service)
