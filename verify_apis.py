"""
verify_apis.py — smoke-test every read-only IDMC endpoint once.

Calls each read tool against the LIVE tenant and prints OK / FAIL / SKIP per
endpoint, so you can confirm the whole surface after deploying. Read-only only —
it never runs or stops a job.

Run (uses the same .env as the monitor/server):
    python3 verify_apis.py
    # also exercise the arg-dependent endpoints:
    python3 verify_apis.py --mapping-id <v2_mapping_id> \
        --conn <connection_id_or_name> --object <objectName> --by id

Endpoints that need a specific ID/object (mapping-by-id, data preview, fields,
expression validation) are SKIPPED unless you pass the matching args.
Exit code is non-zero if any endpoint FAILS.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from config import Config
from idmc_client import IDMCClient


def _size(res) -> str:
    if isinstance(res, (list, dict, str)):
        return f"{type(res).__name__}, len={len(res)}"
    return type(res).__name__


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mapping-id", help="v2 mapping id for get_mapping")
    ap.add_argument("--conn", help="connection id or name for preview/fields/expression")
    ap.add_argument("--object", help="object (table/file) name for preview/fields")
    ap.add_argument("--by", default="id", choices=["id", "name"])
    ap.add_argument("--taskflow-run", help="taskflow runId for get_taskflow_status")
    args = ap.parse_args()

    cfg = Config.load()
    results: list[tuple[str, str, str]] = []

    async def check(name, coro):
        try:
            res = await coro
            results.append((name, "OK", _size(res)))
        except Exception as e:                       # noqa: BLE001 — report, don't raise
            results.append((name, "FAIL", str(e)[:140]))

    def skip(name, why):
        results.append((name, "SKIP", why))

    async with IDMCClient(cfg.login_url, cfg.username, cfg.password) as c:
        # No-arg read endpoints
        await check("get_org", c.get_org())
        await check("get_server_time", c.get_server_time())
        await check("get_audit_log", c.get_audit_log())
        await check("get_activity_log", c.get_activity_log())
        await check("list_running_jobs", c.list_running_jobs())
        await check("get_agent_details", c.get_agent_details())
        await check("get_connections", c.get_connections())
        if args.conn and args.by == "name":
            await check("get_connection", c.get_connection(args.conn, by="name"))
        await check("get_connectors", c.get_connectors())
        await check("get_custom_functions", c.get_custom_functions())
        await check("get_schedules", c.get_schedules())
        await check("get_runtime_environments", c.get_runtime_environments())
        await check("get_users", c.get_users())
        await check("get_security_log", c.get_security_log())          # last 24h
        await check("search_objects[MTT]", c.search_objects(q="type=='MTT'", limit=5))
        await check("get_mappings", c.get_mappings())
        for t in ("DMASK", "DRS", "DSS", "MTT", "PCS"):
            await check(f"get_tasks[{t}]", c.get_tasks(t))

        # Arg-dependent endpoints
        if args.mapping_id:
            await check("get_mapping", c.get_mapping(args.mapping_id))
        else:
            skip("get_mapping", "pass --mapping-id <v2 mapping id>")

        if args.taskflow_run:
            await check("get_taskflow_status", c.get_taskflow_status(args.taskflow_run))
        else:
            skip("get_taskflow_status", "pass --taskflow-run <taskflow runId>")

        if args.conn and args.object:
            await check("preview_source_data",
                        c.data_preview("source", args.by, args.conn, args.object))
            await check("get_source_fields",
                        c.get_fields("source", args.by, args.conn, args.object))
            await check("validate_expression",
                        c.validate_expression("systimestamp()", args.conn,
                                               args.object, True))
        else:
            skip("preview/fields/expression", "pass --conn and --object")

    # Report
    width = max(len(n) for n, _, _ in results)
    print()
    for name, status, note in results:
        print(f"{name:<{width}}  {status:<4}  {note}")
    ok = sum(1 for r in results if r[1] == "OK")
    fail = sum(1 for r in results if r[1] == "FAIL")
    skipped = sum(1 for r in results if r[1] == "SKIP")
    print(f"\n{ok} OK, {fail} FAIL, {skipped} skipped")
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    asyncio.run(main())
