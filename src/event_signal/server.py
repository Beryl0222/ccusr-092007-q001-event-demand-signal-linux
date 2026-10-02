"""本地开发服务器入口（纯标准库）。

用法::

    python -m event_signal.server --port 8080 --log data/signals.jsonl \
        --audit data/audit.jsonl

生产部署应把 :mod:`event_signal.wsgi` 的 ``build_app()`` 挂到 gunicorn/
uWSGI 等容器，并由鉴权网关注入 ``X-Party-Id``。
"""

from __future__ import annotations

import argparse
from wsgiref.simple_server import make_server

from .wsgi import build_app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="赛事需求信号协作后端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--log", default=None, help="信号追加日志 JSONL 路径")
    parser.add_argument("--audit", default=None, help="审计日志 JSONL 路径")
    parser.add_argument("--min-cohorts", type=int, default=3)
    parser.add_argument("--min-sample", type=int, default=10)
    args = parser.parse_args(argv)

    app = build_app(log_path=args.log, audit_path=args.audit,
                    min_cohorts=args.min_cohorts, min_sample=args.min_sample)
    server = make_server(args.host, args.port, app)
    print(f"event-signal listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
