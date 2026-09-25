"""服务入口。

用法::

    python3 -m service_09252_009 [--host 127.0.0.1] [--port 8080] [--db PATH]

数据库路径默认取环境变量 ``RESOLUTION_DB``，否则使用当前工作目录下的
``./data/resolution.db``（运行数据不写入源码目录）。``--db :memory:`` 可用于
临时演示。
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .api import make_server
from .service import ResolutionService
from .storage import SQLiteStore


def build_service(db_path: str) -> tuple[ResolutionService, SQLiteStore]:
    if db_path != ":memory:":
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(db_path)
    return ResolutionService(store), store


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="联合教研议题决议服务")
    parser.add_argument("--host", default=os.environ.get("RESOLUTION_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("RESOLUTION_PORT", "8080")))
    parser.add_argument(
        "--db",
        default=os.environ.get("RESOLUTION_DB", "data/resolution.db"),
        help="SQLite 文件路径（:memory: 为内存库）")
    args = parser.parse_args(argv)

    service, store = build_service(args.db)
    httpd = make_server(service, args.host, args.port)
    try:
        print(f"决议服务已启动: http://{args.host}:{args.port}  db={args.db}")
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        store.close()


if __name__ == "__main__":
    main()
