from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .common import ROOT, DomainError, canonical
from .service import Service


def main():
    if os.name != "nt":
        os.umask(0o077)
    parser = argparse.ArgumentParser(description="Boss 智能招聘工作台")
    parser.add_argument("command", choices=["serve", "mcp", "demo", "probe", "backup", "restore"])
    parser.add_argument("--runtime", default=str(ROOT / "runtime"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-worker", action="store_true")
    parser.add_argument("--archive", help="恢复用备份 ZIP")
    parser.add_argument("--fixture", action="store_true", help="显式载入人工离线格式样例")
    args = parser.parse_args()
    try:
        if args.command == "restore":
            from .maintenance import restore
            if not args.archive:
                parser.error("restore 需要 --archive，并将 --runtime 指向新的空目录")
            print(restore(args.archive, args.runtime))
            return
        if args.command == "serve":
            import uvicorn
            from .api import create_app
            uvicorn.run(create_app(Path(args.runtime), not args.no_worker), host="127.0.0.1", port=args.port, access_log=False)
            return
        service = Service(Path(args.runtime))
        if args.command == "mcp":
            from .mcp_server import build_server
            build_server(service).run(transport="stdio")
        elif args.command == "demo":
            from .demo import publish_fixture, seed
            print(canonical(publish_fixture(service) if args.fixture else seed(service)))
        elif args.command == "backup":
            from .maintenance import backup
            print(backup(service))
        elif args.command == "probe":
            from .harnesses import HarnessAdapter
            from .platforms import BossAgentCliAdapter
            print(canonical({"harness": HarnessAdapter(service.config["harness"]).probe(), "boss": BossAgentCliAdapter(service).probe()}))
    except DomainError as error:
        print(canonical({"error": error.as_dict()}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
