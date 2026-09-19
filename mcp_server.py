"""
mcp_server.py — Informatica IDMC MCP server (stdio).

Replaces the stock 3-tool MCP with parity tools plus a broad set of v2
read-only tools. Runs over stdio so Claude Desktop launches it via an SSH/SSM
bridge (see README) with no public exposure.

ORDER OF OPERATION for a typical user query:
  1. search_objects (v3)      — resolve WHAT the user is asking about
                                (type/path/updateTime/tag). CONTEXT ONLY —
                                v3 federated ids do NOT work on v2 endpoints.
  2. list_running_jobs /      — v2 activity layer: running MTTs and their
     get_activity_log           final end status. ALL run/task ids for v2
                                tools come from HERE.
  3. lookup_taskflow_for_run  — an activityLog entry's parentTaskFederatedId
     (v3 lookup)                resolves to the parent TASKFLOW; ~99% of
                                Service-URL api names are just the taskflow
                                name, enabling run_taskflow_by_name.
  4. Tier 2/3 write tools     — confirm-gated; ids/names from steps 2–3.

Tool groups:
  Monitoring:  list_running_jobs, get_mapping_task_status, get_activity_log,
               get_audit_log
  Discovery:   search_objects, lookup_taskflow_for_run (v3)
  Org/server:  get_org, get_server_time, get_v2_user_details
  Metadata:    get_tasks, get_agent_details, get_connections, get_connectors,
               get_mappings, get_mapping, get_custom_functions, get_schedules
  Connection:  preview_source_data / preview_target_data, get_source_fields /
               get_target_fields, validate_expression
  Parity:      run_mapping_task                  (was in stock MCP)
  Destructive: stop_running_job                  (gated behind confirm=True)
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from mcp.server.fastmcp import FastMCP

from config import Config
from idmc_client import (IDMCClient, IDMCError, elapsed_hours,
                         parse_start_time, summarize_error)

mcp = FastMCP("informatica-idmc")
_cfg = Config.load()


async def _client() -> IDMCClient:
    c = IDMCClient(_cfg.login_url, _cfg.username, _cfg.password)
    await c.login()
    return c


def _summarize(job: dict) -> dict:
    # activityMonitor fields: name in taskName, type in `type`, true UTC start
    # in startTimeUtc (plain startTime is local with a misleading Z).
    name = job.get("taskName") or job.get("objectName") or "<unknown>"
    start = parse_start_time(job.get("startTimeUtc") or job.get("startTime"))
    return {
        "name": name,
        "run_id": str(job.get("runId") or ""),
        "task_id": str(job.get("taskId") or ""),
        "type": job.get("type"),
        "started_utc": start.isoformat() if start else None,
        "elapsed_hours": round(elapsed_hours(start), 2) if start else None,
        "state": job.get("executionState"),
        "success_target_rows": job.get("successTargetRows"),
        "error": job.get("errorMsg"),
    }


# ── asset discovery (v3) — usually the FIRST step for a user query ──────────
@mcp.tool()
async def search_objects(query: str = "", limit: int = 100) -> object:
    """Find IDMC assets by a v3 filter — the entry point for "what is the user
    asking about?". Returns each asset's id, path, type, updatedBy, updateTime.

    query examples:
      "type=='TASKFLOW'"
      "type=='MTT' and updateTime>=2024-01-01T00:00:00.000Z"
      "location=='Default/Sales'"
      "tag=='UpsellOpps'"
    Asset types include DTEMPLATE (mapping), MTT (mapping task), DSS, DMASK,
    DRS, PCS, WORKFLOW (linear taskflow), TASKFLOW, MAPPLET, and more.

    NOTE: the returned `id` is a v3 federated id and does NOT work on v2
    endpoints — use the name/path/type to drill into v2 tools. It DOES match
    the `parentTaskFederatedId` field on activityLog entries (see
    lookup_taskflow_for_run).
    """
    c = await _client()
    try:
        return await c.search_objects(q=query or None, limit=limit)
    finally:
        await c.aclose()


@mcp.tool()
async def lookup_taskflow_for_run(parent_task_federated_id: str) -> object:
    """Resolve an activityLog entry's `parentTaskFederatedId` (a v3 federated
    id) to the parent TASKFLOW's path/name/type via the v3 lookup API.

    Why this matters: this ties a mapping-task run to the taskflow that
    orchestrated it, and ~99% of the time the taskflow's published
    Service-URL api_name is just its name — so the result feeds directly
    into run_taskflow_by_name to re-run a failed taskflow.
    """
    c = await _client()
    try:
        return await c.lookup_objects(object_ids=[parent_task_federated_id])
    finally:
        await c.aclose()


@mcp.tool()
async def get_asset_dependencies(asset_id: str, ref_type: str = "Uses",
                                 limit: int = 50, skip: int = 0) -> object:
    """Find what an asset is MADE OF or what is BUILT ON it (v3 references).

    asset_id is a v3 FEDERATED id — get it from search_objects (e.g.
    "type=='Connection'" or a path filter) or lookup_taskflow_for_run.

    ref_type='Uses'   -> objects this asset uses. A mapping/task returns its
                         connections; a taskflow returns its tasks.
    ref_type='usedBy' -> objects that use this asset. THE way to answer
                         "which assets make use of this connection?" — pass
                         the connection's federated id.

    Each reference includes `appContextId`: the service-specific id that IS
    valid in v2 API calls (e.g. as a task id) — unlike the v3 federated `id`.
    Max 50 per page (API cap); use skip to page through larger sets.
    """
    c = await _client()
    try:
        return await c.get_object_references(asset_id, ref_type=ref_type,
                                             limit=limit, skip=skip)
    finally:
        await c.aclose()


@mcp.tool()
async def get_v2_user_details() -> object:
    """Get the calling user's v2 record (roles, usergroups, serverUrl, org)
    via the v2 login endpoint. Read-only; useful for 'what can I do?' context.
    (v2 and v3 session ids are interchangeable across both API families.)"""
    c = await _client()
    try:
        return await c.get_v2_user_details()
    finally:
        await c.aclose()


# ── monitoring / activity ──────────────────────────────────────────────────
@mcp.tool()
async def list_running_jobs() -> list[dict]:
    """List all currently running IDMC jobs with name, run id, and elapsed time."""
    c = await _client()
    try:
        return [_summarize(j) for j in await c.list_running_jobs()]
    finally:
        await c.aclose()


@mcp.tool()
async def get_mapping_task_status(run_id: str) -> object:
    """Get the status/details of a MAPPING TASK (MTT) run by its run id.
    Run ids come from list_running_jobs (activityMonitor) or get_activity_log
    (activityLog) — these only cover mapping tasks, not taskflows. For a
    FAILED run the response is enriched with `error_summary` (simplified
    message + speculative likely cause)."""
    c = await _client()
    try:
        data = await c.get_mapping_task_status(run_id)
        entries = data if isinstance(data, list) else [data]
        for e in entries:
            if isinstance(e, dict) and e.get("errorMsg"):
                s = summarize_error(e.get("errorMsg"))
                if s:
                    e["error_summary"] = s
        return data
    finally:
        await c.aclose()


@mcp.tool()
async def get_activity_log(run_id: str = "", row_limit: int = 20,
                           task_id: str = "") -> object:
    """Get completed-run history (activityLog), newest first. Optionally a
    single run id. row_limit caps the number of entries — the full log is
    very large and an unbounded pull times out, so keep this modest and
    raise it only when you need to look further back.

    task_id (from list_running_jobs or a log entry's objectId) filters to one
    task's run history — the efficient way to find a specific task's past
    runs (and their parentTaskFederatedId) on a busy org."""
    c = await _client()
    try:
        return await c.get_activity_log(run_id=run_id or None,
                                        row_limit=row_limit,
                                        task_id=task_id or None)
    finally:
        await c.aclose()


@mcp.tool()
async def get_audit_log() -> object:
    """Get the organization's audit log (security/admin events)."""
    c = await _client()
    try:
        return await c.get_audit_log()
    finally:
        await c.aclose()


def _read_ledger(cutoff: datetime, name_filter: str) -> list[dict]:
    """Read the monitor's failure ledger (failures.jsonl), newest data the
    monitor has seen. Returns [] if the ledger doesn't exist yet."""
    out: list[dict] = []
    needle = name_filter.lower()
    try:
        with open(_cfg.ledger_file) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                end = parse_start_time(rec.get("ended_utc"))
                if end and end < cutoff:
                    continue
                if needle and needle not in str(rec.get("name", "")).lower():
                    continue
                out.append(rec)
    except FileNotFoundError:
        pass
    return out


@mcp.tool()
async def find_failed_runs(days_back: int = 7, name_filter: str = "",
                           source: str = "auto", limit: int = 50) -> object:
    """Find FAILED mapping-task runs going back `days_back` days — the
    "what failed last Tuesday?" tool. Returns COMPACT records (name, run/task
    ids, times, simplified error + likely cause, parent taskflow), never the
    raw multi-MB log entries.

    name_filter: case-insensitive substring on the task name.
    source: 'ledger' = the monitor's failures.jsonl on the box (instant,
            covers everything the monitor has seen since deployment);
            'api'    = page the live activity log (slower; also finds
            failures that predate the ledger, subject to IDMC retention);
            'auto'   = ledger first, plus API fill-in when the requested
            window predates the ledger's coverage (default).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    results: list[dict] = []
    seen: set[str] = set()

    if source in ("auto", "ledger"):
        for rec in _read_ledger(cutoff, name_filter):
            key = f"{rec.get('task_id')}:{rec.get('run_id')}"
            if key not in seen:
                seen.add(key)
                results.append(rec)

    ledger_exists = os.path.exists(_cfg.ledger_file)
    need_api = source == "api" or (source == "auto" and not ledger_exists)
    if need_api:
        c = await _client()
        try:
            for rec in await c.find_failed_runs(days_back=days_back,
                                                name_filter=name_filter):
                key = f"{rec.get('task_id')}:{rec.get('run_id')}"
                if key not in seen:
                    seen.add(key)
                    results.append(rec)
        finally:
            await c.aclose()

    results.sort(key=lambda r: r.get("ended_utc") or "", reverse=True)
    return {"count": len(results), "source_used":
            ("api" if need_api and source != "ledger" else "ledger"),
            "failures": results[:limit]}


# ── org / server ────────────────────────────────────────────────────────────
@mcp.tool()
async def get_org() -> object:
    """Get details about the current IDMC organization."""
    c = await _client()
    try:
        return await c.get_org()
    finally:
        await c.aclose()


@mcp.tool()
async def get_server_time() -> object:
    """Get the IDMC server time (useful for elapsed-time calculations)."""
    c = await _client()
    try:
        return await c.get_server_time()
    finally:
        await c.aclose()


# ── metadata ──────────────────────────────────────────────────────────────
@mcp.tool()
async def get_tasks(task_type: str) -> object:
    """List tasks of a given type. task_type one of: DMASK, DRS, DSS, MTT, PCS."""
    c = await _client()
    try:
        return await c.get_tasks(task_type)
    finally:
        await c.aclose()


@mcp.tool()
async def get_agent_details() -> object:
    """Get details of the org's Secure Agents."""
    c = await _client()
    try:
        return await c.get_agent_details()
    finally:
        await c.aclose()


@mcp.tool()
async def get_connections() -> object:
    """List existing connections in the org."""
    c = await _client()
    try:
        return await c.get_connections()
    finally:
        await c.aclose()


@mcp.tool()
async def get_connection(connection: str, by: str = "name") -> object:
    """Get one connection's details by name (by='name') or id (by='id')."""
    c = await _client()
    try:
        return await c.get_connection(connection, by=by)
    finally:
        await c.aclose()


@mcp.tool()
async def update_connection(connection_id: str, updates: dict,
                            confirm: bool = False) -> dict:
    """Update a connection's settings (v2 POST /connection/{id}).

    HIGH BLAST RADIUS: every asset built on this connection inherits the
    change immediately — check get_asset_dependencies(ref_type='usedBy')
    first to see what you're about to affect. A bad value here is exactly
    how whole nightly loads break (e.g. an invalid warehouse setting).

    connection_id: the v2 connection id (from get_connections /
    get_connection — NOT a v3 federated id).
    updates: only the fields to change, e.g. {"runtimeEnvironmentId": "...",
    "database": "..."}. Avoid sending credential fields through this tool —
    rotate passwords in the IICS UI instead.

    WRITE action: without confirm=True this returns a PREVIEW showing each
    field's current value vs. the proposed value — nothing is changed.
    """
    c = await _client()
    try:
        if not confirm:
            current = await c.get_connection(connection_id, by="id")
            diff = {}
            for k, v in updates.items():
                if k == "connParams" and isinstance(v, dict):
                    cur_cp = (current or {}).get("connParams") or {}
                    diff["connParams"] = {
                        sub: {"current": cur_cp.get(sub), "proposed": sv}
                        for sub, sv in v.items()}
                else:
                    diff[k] = {"current": (current or {}).get(k),
                               "proposed": v}
            return {"action": "preview", "connection_id": connection_id,
                    "connection_name": (current or {}).get("name"),
                    "would_change": diff,
                    "note": ("Call again with confirm=true to apply. "
                             "Consider get_asset_dependencies(usedBy) on "
                             "this connection first — every listed asset "
                             "is affected.")}
        result = await c.update_connection(connection_id, updates)
        return {"action": "updated", "connection_id": connection_id,
                "at": datetime.now(timezone.utc).isoformat(),
                "result": result}
    finally:
        await c.aclose()


_SECRET_KEY_HINTS = ("password", "pwd", "secret", "token", "passphrase")


def _mask_secrets(obj):
    """Recursively mask values whose key looks credential-like, so secrets
    from plan files or API responses never flow back into chat."""
    if isinstance(obj, dict):
        return {k: ("***set***" if v and any(h in k.lower()
                                             for h in _SECRET_KEY_HINTS)
                    else _mask_secrets(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask_secrets(x) for x in obj]
    return obj


@mcp.tool()
async def update_connections_from_file(
        file_name: str = "connection_key_migration.json",
        confirm: bool = False, force: bool = False) -> dict:
    """Apply connection updates from a plan file ON THE BOX — the way to
    change credentials/auth settings WITHOUT them passing through chat
    (e.g. the Snowflake password -> private-key migration).

    The file lives next to the server code (~/informatica-mcp/<file_name>)
    and has the shape: {"connections": [{"connection_id", "expect_name",
    "updates": {...}}, ...]}. See connection_key_migration.json.

    Safety: each entry is applied only if the connection's ACTUAL name
    matches expect_name exactly. All credential-looking values are masked
    in this tool's output. Placeholder entries (FILL_ME...) are skipped.

    KEY GUARDS (learned from real mismatches, 2026-08-10): an entry is
    REFUSED when (a) the proposed privateKeyFile's filename doesn't contain
    the connection's Snowflake user — a key authenticates ONE user, so a
    mismatch breaks the connection; or (b) the connection is already on
    KeyPair with a DIFFERENT key file — it was likely migrated deliberately.
    Pass force=true only when you know better than the guard.

    ROLE (optional entry key "role": "<ROLE_NAME>"): appends
    'role=<ROLE_NAME>' to the connection's Additional JDBC URL Parameters
    (connParams.additionalparam), reading the CURRENT value live — needed
    because KeyPair mode hides the connector's role field. Idempotent; a
    conflicting existing role= is refused unless force=true. Entries may be
    role-only (no "updates") for passes over already-migrated connections.

    WRITE action: without confirm=True returns a masked PREVIEW diff per
    connection; with confirm=True applies via the same merge semantics as
    update_connection.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), file_name)
    try:
        with open(path) as f:
            plan = json.load(f)
    except FileNotFoundError:
        return {"error": f"Plan file not found on the box: {path}"}
    except json.JSONDecodeError as e:
        return {"error": f"Plan file is not valid JSON: {e}"}

    results = []
    c = await _client()
    try:
        for entry in plan.get("connections", []):
            conn_id = str(entry.get("connection_id", ""))
            expect = str(entry.get("expect_name", ""))
            role = str(entry.get("role", "")).strip()
            updates = entry.get("updates") or {}
            if "FILL_ME" in conn_id or "FILL_ME" in expect \
                    or (not updates and not role):
                results.append({"connection_id": conn_id,
                                "status": "skipped (placeholder/empty)"})
                continue
            current = await c.get_connection(conn_id, by="id")
            actual = (current or {}).get("name")
            if actual != expect:
                results.append({"connection_id": conn_id,
                                "status": "REFUSED — name mismatch",
                                "expected": expect, "actual": actual})
                continue
            cur_cp = (current or {}).get("connParams") or {}
            new_cp = updates.get("connParams") or {}
            sf_user = str(cur_cp.get("user") or "")
            key_file = str(new_cp.get("privateKeyFile") or "")
            guard = None
            # Key files are WINDOWS paths (Secure Agent box) while this
            # server runs on Linux — split on both separators by hand.
            # Exact stem match required: substring checks pass false friends
            # (user CORPSYS_INFORMATICA vs key DEV_CORPSYS_INFORMATICA.p8).
            key_name = key_file.replace("\\", "/").rsplit("/", 1)[-1]
            key_stem = key_name.rsplit(".", 1)[0].upper()
            if key_file and sf_user and key_stem != sf_user.upper():
                guard = (f"key/user mismatch — connection user is {sf_user} "
                         f"but key file is {key_name}")
            elif (key_file and cur_cp.get("authentication") == "KeyPair"
                  and key_file != cur_cp.get("privateKeyFile")):
                old_key = str(cur_cp.get("privateKeyFile") or "")
                guard = ("already on KeyPair with a different key "
                         f"({old_key.replace(chr(92), '/').rsplit('/', 1)[-1]}) "
                         "— likely migrated deliberately")
            if guard and not force:
                results.append({"connection": actual,
                                "connection_id": conn_id,
                                "snowflake_user": sf_user,
                                "status": f"REFUSED — {guard} "
                                          "(pass force=true to override)"})
                continue
            # ROLE APPEND: KeyPair mode hides the connector's `role` field
            # (per the CCM metadata), so the role must ride in the JDBC URL
            # params. Entry key "role" appends 'role=<name>' to the CURRENT
            # additionalparam read LIVE from the connection — never a
            # hand-copied string. Idempotent: same role already present =
            # no change; a DIFFERENT role present is refused unless
            # force=true (then replaced).
            if role:
                cur_jdbc = str(cur_cp.get("additionalparam") or "")
                parts = [p for p in cur_jdbc.split("&") if p]
                existing = next((p.split("=", 1)[1] for p in parts
                                 if p.lower().startswith("role=")), None)
                if existing != role:
                    if existing is not None and not force:
                        results.append({
                            "connection": actual, "connection_id": conn_id,
                            "snowflake_user": sf_user,
                            "status": (f"REFUSED — JDBC params already carry "
                                       f"role={existing}; pass force=true "
                                       f"to replace with {role}")})
                        continue
                    parts = [p for p in parts
                             if not p.lower().startswith("role=")]
                    parts.append(f"role={role}")
                    updates = dict(updates)
                    cp2 = dict(updates.get("connParams") or {})
                    cp2["additionalparam"] = "&".join(parts)
                    updates["connParams"] = cp2
            if not confirm:
                diff = {}
                for k, v in updates.items():
                    if k == "connParams" and isinstance(v, dict):
                        diff["connParams"] = {
                            sub: {"current": cur_cp.get(sub), "proposed": sv}
                            for sub, sv in v.items()}
                    else:
                        diff[k] = {"current": (current or {}).get(k),
                                   "proposed": v}
                results.append({"connection": actual, "connection_id": conn_id,
                                "snowflake_user": sf_user,
                                "status": "preview",
                                "would_change": _mask_secrets(diff)})
            else:
                await c.update_connection(conn_id, updates)
                results.append({"connection": actual, "connection_id": conn_id,
                                "status": "updated",
                                "at": datetime.now(timezone.utc).isoformat()})
    finally:
        await c.aclose()
    return {"action": "applied" if confirm else "preview",
            "plan_file": file_name, "results": results,
            "note": ("" if confirm else
                     "Call again with confirm=true to apply all previewed "
                     "entries.")}


@mcp.tool()
async def get_connectors() -> object:
    """List available connectors (connector types)."""
    c = await _client()
    try:
        return await c.get_connectors()
    finally:
        await c.aclose()


@mcp.tool()
async def get_mappings() -> str:
    """List all mappings (returns XML: name, ID, created date, etc.)."""
    c = await _client()
    try:
        return await c.get_mappings()
    finally:
        await c.aclose()


@mcp.tool()
async def get_mapping(mapping_id: str) -> str:
    """Get one mapping's definition by ID (XML). Needs a v2 mapping id."""
    c = await _client()
    try:
        return await c.get_mapping(mapping_id)
    finally:
        await c.aclose()


@mcp.tool()
async def get_custom_functions() -> object:
    """List custom functions defined in the org."""
    c = await _client()
    try:
        return await c.get_custom_functions()
    finally:
        await c.aclose()


@mcp.tool()
async def get_schedules() -> object:
    """List custom schedules defined in the org."""
    c = await _client()
    try:
        return await c.get_schedules()
    finally:
        await c.aclose()


@mcp.tool()
async def get_runtime_environments() -> object:
    """List runtime environments (Secure Agent groups) and their agents."""
    c = await _client()
    try:
        return await c.get_runtime_environments()
    finally:
        await c.aclose()


@mcp.tool()
async def get_users() -> object:
    """List org users and their roles."""
    c = await _client()
    try:
        return await c.get_users()
    finally:
        await c.aclose()


@mcp.tool()
async def get_security_log(query: str = "") -> object:
    """Query the v3 security log. `query` requires an entryTime range, e.g.
    'entryTime>="2026-06-01T00:00:00.000Z";entryTime<="2026-06-14T00:00:00.000Z"'
    (max 14-day range; defaults to last 24h)."""
    c = await _client()
    try:
        return await c.get_security_log(q=query or None)
    finally:
        await c.aclose()


# ── connection data preview / fields / expression ──────────────────────────
@mcp.tool()
async def preview_source_data(connection: str, object_name: str,
                              by: str = "id") -> object:
    """Preview data from a SOURCE object. connection = id (by='id') or
    connection name (by='name'); object_name is the table/file.

    For DATABASE connectors (e.g. Snowflake), object_name must be the
    QUALIFIED path with slashes: 'DB/SCHEMA/TABLE' (e.g.
    'DEV_STAGING/AVIGILON_ALTA/ORGS_GROUP') — a bare table name 403s with
    'Invalid Object Path Format'."""
    c = await _client()
    try:
        return await c.data_preview("source", by, connection, object_name)
    finally:
        await c.aclose()


@mcp.tool()
async def preview_target_data(connection: str, object_name: str,
                              by: str = "id") -> object:
    """Preview data from a TARGET object (see preview_source_data for args;
    database connectors need the qualified 'DB/SCHEMA/TABLE' path).

    TIP: this is the ideal post-change smoke test for a connection (e.g.
    after a key-pair migration) — one read-only call opens a real session
    through the connection with no job execution and no source-API
    dependencies."""
    c = await _client()
    try:
        return await c.data_preview("target", by, connection, object_name)
    finally:
        await c.aclose()


@mcp.tool()
async def get_source_fields(connection: str, object_name: str,
                            by: str = "id") -> object:
    """Get field names for a SOURCE object."""
    c = await _client()
    try:
        return await c.get_fields("source", by, connection, object_name)
    finally:
        await c.aclose()


@mcp.tool()
async def get_target_fields(connection: str, object_name: str,
                            by: str = "id") -> object:
    """Get field names for a TARGET object."""
    c = await _client()
    try:
        return await c.get_fields("target", by, connection, object_name)
    finally:
        await c.aclose()


@mcp.tool()
async def validate_expression(expr: str, connection_id: str, object_name: str,
                              is_source_type: bool = True) -> object:
    """Validate an IDMC expression (e.g. 'systimestamp()') against an object."""
    c = await _client()
    try:
        return await c.validate_expression(expr, connection_id, object_name,
                                           is_source_type)
    finally:
        await c.aclose()


# ── ingestion & replication (mftsaas) ───────────────────────────────────────
@mcp.tool()
async def list_ingestion_tasks() -> object:
    """List Data Ingestion and Replication tasks (file/database ingestion,
    e.g. dbmir_* replication jobs). These jobs do NOT appear in
    list_running_jobs / get_activity_log — this separate API is the only
    programmatic view of them. Read-only."""
    c = await _client()
    try:
        return await c.list_ingestion_tasks()
    finally:
        await c.aclose()


@mcp.tool()
async def get_ingestion_task_log(task_id: str) -> object:
    """Job history/status for ONE FILE ingestion/replication task by its
    mitask id (from list_ingestion_tasks). Read-only."""
    c = await _client()
    try:
        return await c.get_ingestion_activity_log(task_id)
    finally:
        await c.aclose()


@mcp.tool()
async def get_dbmi_job_status(job_id: int) -> object:
    """Status of a DATABASE/APPLICATION ingestion & replication job (DBMI) —
    the dbmir_* replication jobs invisible to list_running_jobs. The job
    name embeds the id: dbmir_stage_jira_5632 -> job_id 5632.

    Returns status (Up and Running / Running with Warning / On Hold /
    Stopped / Failed / Deploying / Aborted / Completed...), errorMessage,
    lastAction, start/end times, and jobConfig (taskMode UNLOAD/CDC/COMBINED,
    source/target connection ids, deploy version). Read-only."""
    c = await _client()
    try:
        return await c.get_dbmi_job_status(job_id)
    finally:
        await c.aclose()


@mcp.tool()
async def get_dbmi_job_metrics(job_id: int,
                               state_filter: str | list[str] | None = None,
                               search: str = "", limit: int = 25,
                               offset: int = 0) -> object:
    """Per-object (table-level) metrics for a DATABASE/APPLICATION ingestion
    & replication job (DBMI) — the drill-down get_dbmi_job_status lacks.
    The job name embeds the id: dbmir_stage_jira_5632 -> job_id 5632.

    Returns jobInfo (job-level status) plus metricsInfo[] with per-task
    recordsRead/recordsWritten and subTasks[] — each source table's srcName,
    tgtName, state (RUNNING / COMPLETED / FAILED / ...) and, for CDC jobs,
    inserts/updates/deletes counts — plus counts.statusCounts (quick way to
    spot how many tables are failing when the job shows 'warning') and
    currentThroughput.

    state_filter narrows subtasks by state — pass one state string (e.g.
    'ERROR') or a list of states; the client sends it as the array the API
    requires. search filters by object name; limit/offset paginate.
    Read-only. A 409 means the job is not in a valid state for stats
    collection."""
    c = await _client()
    try:
        return await c.get_dbmi_job_metrics(job_id, state_filter=state_filter,
                                            search=search, limit=limit,
                                            offset=offset)
    finally:
        await c.aclose()


# ── taskflows (status + resume/run) ─────────────────────────────────────────
@mcp.tool()
async def get_taskflow_status(run_id: str) -> dict:
    """Get the status of a TASKFLOW run by its taskflow runId — the large numeric
    id (e.g. 1256290565593927680), NOT the activityMonitor runId. Returns
    status (RUNNING/SUSPENDED/FAILED/...), assetName, duration, startedBy, etc.
    Use this to check whether a taskflow is still running or stuck."""
    c = await _client()
    try:
        return await c.get_taskflow_status(run_id)
    finally:
        await c.aclose()


@mcp.tool()
async def resume_taskflow(run_id: str, confirm: bool = False) -> dict:
    """Resume/retry a SUSPENDED or FAILED taskflow RUN by its runId
    (resumeWithFaultRetry) — i.e. re-run a taskflow that isn't running.

    WRITE action: returns a PREVIEW unless confirm=True. Check status first.
    """
    if not confirm:
        return {"action": "preview", "would_resume_run_id": run_id,
                "note": "Call again with confirm=true to resume this taskflow run."}
    c = await _client()
    try:
        return await c.resume_taskflow(run_id)
    finally:
        await c.aclose()


@mcp.tool()
async def run_taskflow_by_name(api_name: str, confirm: bool = False) -> dict:
    """Start a NEW run of a taskflow by its published Service-URL API name
    (e.g. 'backload_visitor_activities-1'). This is how you **re-run a FAILED or
    terminated taskflow** (resume only works on SUSPENDED runs). Returns the new
    run id.

    The caller's identity must be in the taskflow's Allowed Users/Groups.
    WRITE action: returns a PREVIEW unless confirm=True.
    """
    if not confirm:
        return {"action": "preview", "would_run_api_name": api_name,
                "note": "Call again with confirm=true to start a new run."}
    c = await _client()
    try:
        resp = await c.run_taskflow_by_name(api_name)
        run_id = (resp or {}).get("RunId") or (resp or {}).get("runId")
        return {"action": "started", "api_name": api_name,
                "new_run_id": run_id, "raw": resp}
    finally:
        await c.aclose()


@mcp.tool()
async def run_taskflow(taskflow_id: str, confirm: bool = False) -> dict:
    """Start a taskflow by its internal numeric ID via the management API (not a
    runId). Most users want run_taskflow_by_name instead; this is for the
    refresh-job pattern. WRITE action: PREVIEW unless confirm=True.
    """
    if not confirm:
        return {"action": "preview", "would_run_taskflow_id": taskflow_id,
                "note": "Call again with confirm=true to run this taskflow."}
    c = await _client()
    try:
        return await c.run_taskflow(taskflow_id)
    finally:
        await c.aclose()


@mcp.tool()
async def heal_taskflow(run_id: str, api_name: str = "",
                        refresh_taskflow_id: str = "", confirm: bool = False) -> dict:
    """Check a taskflow run by its runId and heal it in one step:
      - RUNNING   -> no action
      - SUSPENDED -> optionally run a refresh taskflow first, then RESUME
      - FAILED    -> START A NEW RUN (resume can't restart a terminated run),
                     returning the new run id

    For FAILED, the api name defaults to the taskflow's own name (from the
    status' assetName) — you usually don't need to pass `api_name`. If the
    published Service-URL name differs (e.g. a version suffix like '-1'), pass
    `api_name` explicitly. If the restart is blocked because you're not
    authorized, this returns guidance to add your group to the taskflow's
    Allowed Groups.

    WRITE action: PREVIEW (status + plan) unless confirm=True.
    """
    c = await _client()
    try:
        status = await c.get_taskflow_status(run_id)
        state = (status or {}).get("status")
        asset_name = (status or {}).get("assetName") or ""
        effective_api = api_name or asset_name      # default api name = taskflow name

        if state == "SUSPENDED":
            plan = (f"run refresh {refresh_taskflow_id}, then resume"
                    if refresh_taskflow_id else "resume")
        elif state == "FAILED":
            plan = (f"start a new run via '{effective_api}'" if effective_api
                    else "FAILED but no taskflow name available — pass api_name")
        elif state == "RUNNING":
            plan = "no action (already running)"
        else:
            plan = f"no action defined for status {state}"

        if (not confirm or state not in ("SUSPENDED", "FAILED")
                or plan.startswith(("no action", "FAILED but"))):
            return {"action": "preview" if not confirm else "none",
                    "status": state, "plan": plan, "run_id": run_id,
                    "taskflow": asset_name}

        if state == "SUSPENDED":
            steps = []
            if refresh_taskflow_id:
                steps.append({"refresh_run": await c.run_taskflow(refresh_taskflow_id)})
            steps.append({"resume": await c.resume_taskflow(run_id)})
            return {"action": "resumed", "status": state, "run_id": run_id,
                    "taskflow": asset_name, "steps": steps}

        # FAILED -> start a new run, auto-resolving the name (exact, then -1/-2/-3)
        try:
            resp, used = await c.run_taskflow_smart(
                asset_name, explicit_api_name=api_name or None)
        except IDMCError as e:
            if e.status_code == 403 or "not authorized" in str(e).lower():
                return {"action": "blocked", "reason": "not authorized",
                        "taskflow": asset_name,
                        "guidance": ("Add the group you're in to this taskflow's "
                                     "Allowed Groups (Start step properties) and "
                                     "re-publish, then retry."),
                        "error": str(e)}
            return {"action": "error", "reason": "could not resolve a service name",
                    "taskflow": asset_name,
                    "guidance": ("Confirm the exact Service-URL name via "
                                 "Actions > Properties Detail > Copy Service URL "
                                 "and pass it as api_name."),
                    "error": str(e)}
        new_run_id = (resp or {}).get("RunId") or (resp or {}).get("runId")
        return {"action": "restarted", "status": state, "taskflow": asset_name,
                "used_api_name": used, "old_run_id": run_id,
                "new_run_id": new_run_id, "raw": resp}
    finally:
        await c.aclose()


@mcp.tool()
async def terminate_taskflows(run_ids: list[str],
                              confirm: bool = False) -> dict:
    """Terminate (kill) up to 200 taskflow RUNS by their taskflow runIds —
    the large numeric ids (from get_taskflow_status or the Monitor UI), NOT
    activityMonitor MTT run ids.

    DESTRUCTIVE and irreversible: running work is cut off mid-flight; a
    terminated run cannot be resumed, only restarted via
    run_taskflow_by_name. The caller's account/group must be in each
    taskflow's Allowed Users/Groups (same requirement as restart) or the
    call 403s.

    PREVIEW (confirm=False) looks up each run's current status and taskflow
    name so you can see exactly what would be killed before confirming.
    """
    if not confirm:
        preview = []
        c = await _client()
        try:
            for rid in run_ids[:200]:
                try:
                    st = await c.get_taskflow_status(str(rid))
                    preview.append({"run_id": str(rid),
                                    "taskflow": (st or {}).get("assetName"),
                                    "status": (st or {}).get("status")})
                except Exception as e:              # noqa: BLE001
                    preview.append({"run_id": str(rid),
                                    "status_lookup_failed": str(e)[:150]})
        finally:
            await c.aclose()
        return {"action": "preview", "would_terminate": preview,
                "note": ("Call again with confirm=true to TERMINATE all of "
                         "the above. Terminated runs cannot be resumed.")}
    c = await _client()
    try:
        result = await c.terminate_taskflows([str(r) for r in run_ids])
        return {"action": "terminated", "run_ids": [str(r) for r in run_ids],
                "at": datetime.now(timezone.utc).isoformat(),
                "result": result}
    finally:
        await c.aclose()


# ── parity tool ────────────────────────────────────────────────────────────
@mcp.tool()
async def run_mapping_task(task_id: str, task_type: str = "MTT") -> dict:
    """Start an IDMC task (default a mapping task, MTT). Returns the job info.
    task_id is a v2 id — get it from the activity log (`objectId`) or
    get_tasks; a v3 federated id will NOT work here."""
    c = await _client()
    try:
        return await c.run_task(task_id, task_type)
    finally:
        await c.aclose()


# ── destructive tool (confirm-gated) ────────────────────────────────────────
@mcp.tool()
async def stop_running_job(task_id: str, task_type: str = "MTT",
                           confirm: bool = False) -> dict:
    """Stop a running IDMC job. DESTRUCTIVE and irreversible.

    Returns a PREVIEW unless confirm=True — the human-in-the-loop guardrail.
    """
    if not confirm:
        return {
            "action": "preview",
            "would_stop": {"task_id": task_id, "task_type": task_type},
            "note": "Call again with confirm=true to actually stop this job.",
        }
    c = await _client()
    try:
        result = await c.stop_job(task_id, task_type)
        return {"action": "stopped", "task_id": task_id,
                "at": datetime.now(timezone.utc).isoformat(), "result": result}
    finally:
        await c.aclose()


if __name__ == "__main__":
    mcp.run()   # stdio transport — launched by Claude Desktop via the bridge
