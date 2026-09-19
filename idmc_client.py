"""
idmc_client.py — shared Informatica IDMC REST client.

LOGIN NOTE: Both logins are supported and their session ids are
interchangeable on v2 and v3 resources (the client always sends the session
under both `INFA-SESSION-ID` and `icSessionId` headers):

  - **v3 login** (`/saas/public/core/v3/login`) — the default. Returns
    `userInfo.sessionId` + the product `baseApiUrl`.
  - **v2 login** (`/ma/api/v2/user/login`) — `login_v2()`. Returns the full
    v2 user record (roles, usergroups, serverUrl, icSessionId). Use it when
    the order of operation needs v2 user detail, or call `get_v2_user_details`.

ID RULES (critical for order of operation):
  - v3 `objects` / `lookup` ids are **federated ids** — context only; they do
    NOT work on v2 endpoints.
  - ALL asset ids passed to v2 APIs must come from v2 responses
    (activityMonitor / activityLog / task / mapping ...).
  - `parentTaskFederatedId` on an activityLog entry matches a v3 object id —
    resolve it via `lookup_objects` to find the parent TASKFLOW.

Host routing (verified against the tested URLs):
  - default      -> {baseApiUrl}/api/v2/...   ALL v2 REST resources live here
                    (baseApiUrl ends in /saas, e.g. na2.../saas)
  - saas=False   -> baseApiUrl without /saas  ONLY the /active-bpel taskflow API
                    (status / resume / run / rt-by-name)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx


class IDMCError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


# Valid task type codes for /api/v2/task?type=<type>
TASK_TYPES = {"DMASK", "DRS", "DSS", "MTT", "PCS"}


class IDMCClient:
    def __init__(self, login_url, username, password, timeout=30.0):
        self._login_url = login_url.rstrip("/")
        self._username = username
        self._password = password
        self._session_id: str | None = None
        self._base_url: str | None = None
        self._http = httpx.AsyncClient(timeout=timeout)

    async def aclose(self):
        await self._http.aclose()

    async def __aenter__(self):
        await self.login()
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    # ── auth ─────────────────────────────────────────────────────────────
    async def login(self):
        """v3 login -> cache sessionId + Integration Cloud baseApiUrl."""
        url = f"{self._login_url}/saas/public/core/v3/login"
        body = {"username": self._username, "password": self._password}
        r = await self._http.post(url, json=body, headers={"Accept": "application/json"})
        if r.status_code != 200:
            raise IDMCError(f"Login failed ({r.status_code}): {r.text[:300]}")
        data = r.json()
        self._session_id = (data.get("userInfo") or {}).get("sessionId")
        products = data.get("products") or []
        base = next((p.get("baseApiUrl") for p in products
                     if p.get("name") == "Integration Cloud"), None)
        if base is None and products:
            base = products[0].get("baseApiUrl")
        self._base_url = (base or "").rstrip("/")
        if not self._session_id or not self._base_url:
            raise IDMCError(f"Login response missing sessionId/baseApiUrl: {data}")

    async def login_v2(self):
        """v2 login (`/ma/api/v2/user/login`) -> returns the full v2 user
        record (roles, usergroups, serverUrl, icSessionId...). Caches the
        icSessionId + serverUrl so subsequent calls can use this session —
        v2/v3 session ids are interchangeable on both API families."""
        url = f"{self._login_url}/ma/api/v2/user/login"
        body = {"@type": "login", "username": self._username,
                "password": self._password}
        r = await self._http.post(url, json=body,
                                  headers={"Accept": "application/json"})
        if r.status_code != 200:
            raise IDMCError(f"v2 login failed ({r.status_code}): {r.text[:300]}")
        data = r.json()
        self._session_id = data.get("icSessionId") or self._session_id
        server_url = (data.get("serverUrl") or "").rstrip("/")
        if server_url:
            self._base_url = server_url
        if not self._session_id or not self._base_url:
            raise IDMCError(f"v2 login response missing icSessionId/serverUrl: {data}")
        return data

    async def get_v2_user_details(self):
        """Fetch the caller's v2 user record (roles, usergroups, serverUrl...)
        by performing a v2 login. Read-only convenience for user context."""
        return await self.login_v2()

    async def logout(self):
        """End the session (v2 logout)."""
        return await self._request("POST", "/api/v2/user/logout")

    def _headers(self, accept: str = "application/json"):
        # Session under all three header names: v3 wants INFA-SESSION-ID,
        # v2 wants icSessionId, and the ingestion/replication family
        # (/mftsaas) wants IDS-SESSION-ID. Endpoints ignore the extras.
        return {
            "INFA-SESSION-ID": self._session_id or "",
            "icSessionId": self._session_id or "",
            "IDS-SESSION-ID": self._session_id or "",
            "Accept": accept,
            "Content-Type": "application/json",
        }

    def _url(self, path: str, *, saas: bool = True) -> str:
        """Build a full URL from base_url (which ends in /saas).
          - default     -> {baseApiUrl}/api/v2/...   (all v2 REST resources)
          - saas=False  -> baseApiUrl without /saas   (the /active-bpel taskflow API)
        """
        base = self._base_url or ""
        if not saas and base.endswith("/saas"):
            base = base[: -len("/saas")]
        return f"{base}{path}"

    def _ing_host(self) -> str:
        """Ingestion service host: pod host with '-ing' spliced into the
        first label (na2.dm-us... -> na2-ing.dm-us...). Discovered via the
        monitor UI's own XHR calls, 2026-08-27 — /dbmi lives HERE, not on
        the pod host (same pattern as the -dqprofile profiling host)."""
        base = (self._base_url or "").removesuffix("/saas")
        scheme, _, rest = base.partition("://")
        first, dot, tail = rest.partition(".")
        return f"{scheme}://{first}-ing{dot}{tail}"

    async def _request(self, method, path, *, accept="application/json",
                       saas=True, raw=False, url_override=None, **kw) -> Any:
        if not self._session_id:
            await self.login()
        url = url_override or self._url(path, saas=saas)
        r = await self._http.request(method, url, headers=self._headers(accept), **kw)
        if r.status_code == 401:                  # session expired -> retry once
            await self.login()
            r = await self._http.request(method, url, headers=self._headers(accept), **kw)
        if r.status_code >= 400:
            raise IDMCError(f"{method} {path} -> {r.status_code}: {r.text[:300]}",
                            status_code=r.status_code)
        # raw: return status + text (for endpoints that don't return JSON).
        if raw:
            return {"status_code": r.status_code, "text": r.text}
        # XML endpoints return text; JSON endpoints return parsed JSON.
        if "xml" in accept:
            return r.text
        return r.json() if r.content else {}

    # ── monitoring / activity ────────────────────────────────────────────
    async def list_running_jobs(self):
        data = await self._request("GET", "/api/v2/activity/activityMonitor?details=true")
        return data if isinstance(data, list) else data.get("entries", [])

    async def get_activity_log(self, run_id: str | None = None,
                               row_limit: int | None = 50,
                               task_id: str | None = None,
                               offset: int | None = None):
        """Completed-run history, newest first. The FULL log is huge (every
        entry carries all transformation stats), so an unbounded pull can
        time out — always cap with rowLimit unless fetching a single run_id.

        task_id filters to one task's run history (v2 taskId param) — the way
        to find PAST runs of a busy-org task without pulling the whole log,
        e.g. to grab parentTaskFederatedId while the current run is still
        in the Activity Monitor.

        offset pages deeper into history (offset + rowLimit) — required on a
        busy org where a time window holds more entries than one page."""
        path = "/api/v2/activity/activityLog" + (f"/{run_id}" if run_id else "")
        params = {}
        if not run_id:
            if row_limit:
                params["rowLimit"] = row_limit
            if task_id:
                params["taskId"] = task_id
            if offset:
                params["offset"] = offset
        return await self._request("GET", path, params=params)

    async def find_failed_runs(self, days_back: int = 7, name_filter: str = "",
                               page_size: int = 100, max_pages: int = 30):
        """Page back through the activity log collecting FAILED runs
        (state==3) newer than `days_back` days. Stops as soon as a page
        reaches entries older than the cutoff. Returns COMPACT records (see
        compact_failure) — never the raw multi-MB entries."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        needle = name_filter.lower()
        out = []
        offset = 0
        for _ in range(max_pages):
            page = await self.get_activity_log(row_limit=page_size,
                                               offset=offset)
            entries = page if isinstance(page, list) else page.get("entries", [])
            if not entries:
                break
            reached_cutoff = False
            for e in entries:
                end = parse_start_time(e.get("endTimeUtc") or e.get("endTime"))
                if end and end < cutoff:
                    reached_cutoff = True
                    continue
                if e.get("state") == 3 and (
                        not needle or needle in str(e.get("objectName", "")).lower()):
                    out.append(compact_failure(e))
            if reached_cutoff:
                break
            offset += page_size
        return out

    async def get_mapping_task_status(self, run_id: str):
        """Status/details of a MAPPING TASK (MTT) run by its runId. Run ids
        come from activityMonitor (running) or activityLog (completed) —
        these only cover mapping tasks, not taskflows."""
        return await self.get_activity_log(run_id=run_id)

    async def get_audit_log(self):
        return await self._request("GET", "/api/v2/auditlog")

    async def find_activity_log_entry(self, run_id):
        """Find a completed run in the Activity LOG by runId (returns None if
        not present yet). Activity Monitor = running; Activity Log = completed
        (final end status of mapping tasks/MTT only)."""
        log = await self.get_activity_log()
        items = log if isinstance(log, list) else log.get("entries", [])
        return next((e for e in items if str(e.get("runId")) == str(run_id)), None)

    # ── v3 object search (the "what asset is the user asking about?" layer) ─
    async def search_objects(self, q: str | None = None,
                             limit: int | None = None, skip: int | None = None):
        """Query the v3 objects catalog. `q` is a filter such as
        "type=='TASKFLOW'" or "type=='MTT' and updateTime>=2024-01-01T00:00:00.000Z".
        Returns id/path/type/updatedBy/updateTime. The returned `id` is a v3
        federated id (NOT usable on v2 endpoints) — use name/path/type to drill in."""
        params = {}
        if q is not None:
            params["q"] = q
        if limit is not None:
            params["limit"] = limit
        if skip is not None:
            params["skip"] = skip
        return await self._request("GET", "/public/core/v3/objects", params=params)

    async def lookup_objects(self, object_ids=None, objects=None):
        """v3 lookup (POST /public/core/v3/lookup) — resolve v3 federated ids
        to their path/type/name. KEY USE: an activityLog entry's
        `parentTaskFederatedId` is a v3 id; looking it up returns the parent
        TASKFLOW's path/name, and ~99% of the time the taskflow's published
        Service-URL api_name IS its name — so this is how you get from a
        mapping-task run to the taskflow you can re-run.

        object_ids: list of v3 federated id strings, OR
        objects:    pre-built list of {"id":...} / {"path":..., "type":...}."""
        body_objects = objects or [{"id": oid} for oid in (object_ids or [])]
        return await self._request("POST", "/public/core/v3/lookup",
                                   json={"objects": body_objects})

    async def get_object_references(self, object_id: str,
                                    ref_type: str = "Uses",
                                    limit: int = 50, skip: int = 0):
        """v3 asset dependencies:
        GET /public/core/v3/objects/{id}/references?refType=...

        object_id is a v3 FEDERATED id (from search_objects / lookup_objects).
        ref_type: 'Uses'   = objects this asset uses (a mapping's connections,
                             a taskflow's tasks, ...)
                  'usedBy' = objects that use this asset (everything built on
                             a connection, ...).
        Max 50 per page; page with skip.

        KEY FACT: each reference carries `appContextId` — the service-specific
        id that IS valid on v2 endpoints (task/job calls). This is the bridge
        across the v3-ids-don't-work-on-v2 rule."""
        params = {"refType": ref_type, "limit": min(limit, 50), "skip": skip}
        return await self._request(
            "GET", f"/public/core/v3/objects/{object_id}/references",
            params=params)

    async def get_security_log(self, q: str | None = None,
                               limit: int | None = None, skip: int | None = None):
        """v3 security log. `q` requires an entryTime range, e.g.
        'entryTime>="2026-06-01T00:00:00.000Z";entryTime<="2026-06-14T00:00:00.000Z"'."""
        params = {}
        if q is not None:
            params["q"] = q
        if limit is not None:
            params["limit"] = limit
        if skip is not None:
            params["skip"] = skip
        return await self._request("GET", "/public/core/v3/securityLog", params=params)

    # ── org / server ──────────────────────────────────────────────────────
    async def get_org(self):
        return await self._request("GET", "/api/v2/org")

    async def get_server_time(self):
        return await self._request("GET", "/api/v2/server/serverTime")

    # ── design assets / metadata ──────────────────────────────────────────
    async def get_tasks(self, task_type: str):
        if task_type not in TASK_TYPES:
            raise IDMCError(f"task_type must be one of {sorted(TASK_TYPES)}")
        return await self._request("GET", f"/api/v2/task?type={task_type}")

    async def get_agent_details(self):
        return await self._request("GET", "/api/v2/agent/details")

    async def get_connections(self):
        return await self._request("GET", "/api/v2/connection")

    async def get_connection(self, key, by="name"):
        """Get one connection by name (by='name') or id (by='id')."""
        seg = f"name/{key}" if by == "name" else key
        return await self._request("GET", f"/api/v2/connection/{seg}")

    # Fields the GET returns that must NOT be posted back on update
    # (system/audit/derived). Everything else is merged into the update body
    # so unsent settings are RETAINED — tested live 2026-08-06: the API is
    # replace-style (a bare partial body 403s on missing `name`, and unsent
    # optional fields reset to defaults, e.g. port 0 -> -1).
    _CONN_READONLY = {
        "@type", "id", "orgId", "createTime", "updateTime", "createdBy",
        "updatedBy", "majorUpdateTime", "connParams", "federatedId",
        "connectorStatus", "shortDescription", "baseType", "internal",
        "isRtAttrsRefreshRequired", "supportsCCIMultiGroup",
        "metadataBrowsable", "supportLabels", "vaultEnabled",
        "vaultEnabledParams", "instanceDisplayName",
    }

    async def update_connection(self, connection_id: str, updates: dict):
        """Update a connection (v2): POST /api/v2/connection/{id}.

        WRITE action with wide blast radius — every asset built on the
        connection inherits the change immediately. Caller must gate with a
        confirm step (and ideally check usedBy dependencies first).

        MERGE SEMANTICS: fetches the current connection, strips read-only
        fields, overlays `updates`, and posts the full object — because the
        API treats the update as a replace (missing required fields 403,
        missing optional fields reset to defaults).

        connParams: for connector-type connections (e.g. Snowflake) the real
        settings live in `connParams`. Passing "connParams" in `updates`
        merges those keys into the CURRENT connParams (other keys retained)
        and includes the result in the body — this is how the
        password -> private-key migration updates authenticationType /
        privateKeyFile without wiping account/warehouse/role."""
        current = await self.get_connection(connection_id, by="id")
        body = {k: v for k, v in (current or {}).items()
                if k not in self._CONN_READONLY}
        updates = dict(updates)                    # don't mutate the caller's
        cp_updates = updates.pop("connParams", None)
        body.update(updates)
        if cp_updates is not None:
            merged = dict((current or {}).get("connParams") or {})
            merged.update(cp_updates)
            body["connParams"] = merged
        body["@type"] = "connection"
        return await self._request(
            "POST", f"/api/v2/connection/{connection_id}", json=body)

    async def get_custom_functions(self):
        return await self._request("GET", "/api/v2/customFunc")

    async def get_schedules(self):
        return await self._request("GET", "/api/v2/schedule")

    async def get_runtime_environments(self):
        return await self._request("GET", "/api/v2/runtimeEnvironment")

    async def get_users(self):
        return await self._request("GET", "/api/v2/user")

    async def get_mappings(self):
        """List mappings (this endpoint returns XML, not JSON)."""
        return await self._request("GET", "/api/v2/mapping", accept="application/xml")

    async def get_mapping(self, mapping_id: str):
        """Get one mapping's definition by ID (XML). Note: requires a v2
        mapping id, NOT a v3 federated id."""
        return await self._request("GET", f"/api/v2/mapping/{mapping_id}",
                                   accept="application/xml")

    # ── connection data preview / fields ──────────────────────────────────
    # Default route (na2.../saas/api/v2/...), per the verified examples.
    # by="id" uses the connection id; by="name" uses the name path.
    def _conn_path(self, side, by, key, op, object_name):
        seg = f"name/{key}" if by == "name" else key
        return f"/api/v2/connection/{side}/{seg}/{op}/{object_name}"

    async def data_preview(self, side: str, by: str, key: str, object_name: str):
        """side: 'source'|'target'; by: 'id'|'name'."""
        return await self._request(
            "GET", self._conn_path(side, by, key, "datapreview", object_name))

    async def get_fields(self, side: str, by: str, key: str, object_name: str):
        return await self._request(
            "GET", self._conn_path(side, by, key, "field", object_name))

    # ── expression validation ─────────────────────────────────────────────
    async def validate_expression(self, expr: str, connection_id: str,
                                   object_name: str, is_source_type: bool = True):
        body = {
            "@type": "expressionValidation",
            "expr": expr,
            "connectionId": connection_id,
            "objectName": object_name,
            "isSourceType": is_source_type,
        }
        return await self._request("POST", "/api/v2/expression/validate", json=body)

    # ── taskflows (Application Integration / active-bpel) ─────────────────
    # These live at the pod host ROOT (no /saas), keyed by the taskflow runId
    # (a large numeric id, different from the v2 activityMonitor runId).
    async def get_taskflow_status(self, run_id):
        """Status of a taskflow RUN by its taskflow runId. Returns status
        (RUNNING/SUSPENDED/FAILED/...), assetName, duration, startedBy, etc."""
        return await self._request(
            "GET", f"/active-bpel/services/tf/status/{run_id}", saas=False)

    async def resume_taskflow(self, run_id):
        """Resume/retry a SUSPENDED taskflow run (resumeWithFaultRetry).
        Only works on SUSPENDED runs — a FAILED/terminated run must be
        restarted via run_taskflow_by_name. WRITE action — caller should
        gate with a confirm step."""
        return await self._request(
            "PUT",
            f"/active-bpel/management/runtime/v1/resumeWithFaultRetry/{run_id}",
            saas=False, raw=True)

    async def run_taskflow(self, taskflow_id):
        """Start a taskflow by its internal numeric ID via the management API
        (NOT a runId). Kept for the refresh-job pattern."""
        return await self._request(
            "POST", f"/active-bpel/management/runtime/v1/run/{taskflow_id}",
            saas=False, raw=True)

    async def terminate_taskflows(self, run_ids):
        """Terminate up to 200 taskflow RUNS by their taskflow runIds (the
        large numeric ids). PUT /active-bpel/services/tf/terminate with body
        {"runid": [...]}. DESTRUCTIVE — caller must confirm-gate. Like
        restart, requires the caller (team account/group) to be authorized
        on the taskflow (Allowed Users/Groups)."""
        ids = [str(r) for r in run_ids][:200]
        return await self._request(
            "PUT", "/active-bpel/services/tf/terminate",
            saas=False, raw=True, json={"runid": ids})

    async def run_taskflow_by_name(self, api_name):
        """Start a NEW taskflow run via its published Service URL
        (/active-bpel/rt/{api_name}, e.g. 'backload_visitor_activities-1').
        This is how you re-run a FAILED/terminated taskflow. Returns JSON
        containing the new run id, e.g. {"RunId": "1256355937542115328"}.
        Requires the caller to be in the taskflow's Allowed Users/Groups."""
        return await self._request("POST", f"/active-bpel/rt/{api_name}", saas=False)


    async def run_taskflow_smart(self, name, explicit_api_name=None, max_suffix=3):
        """Start a taskflow by name, auto-resolving a published version suffix.

        Why the -1/-2 suffixes exist: republishing a copy of a taskflow names
        the subsequent published versions "name-1", "name-2", etc. This tries
        the exact name first, then name-1 … name-{max_suffix}, stopping at the
        FIRST success. Safe against duplicate runs: a wrong name errors without
        starting anything, so only a real 2xx kicks off a run. A 403 (not
        authorized) stops immediately and re-raises (it's an access issue, not a
        name issue). Returns (response, used_name).

        TIP: get `name` from lookup_objects(parentTaskFederatedId) — ~99% of
        api names are just the taskflow name.
        """
        candidates = ([explicit_api_name] if explicit_api_name
                      else [name] + [f"{name}-{i}" for i in range(1, max_suffix + 1)])
        last_err = None
        for cand in candidates:
            try:
                resp = await self.run_taskflow_by_name(cand)
                return resp, cand
            except IDMCError as e:
                if e.status_code == 403 or "not authorized" in str(e).lower():
                    raise                       # auth problem — don't try other names
                last_err = e                    # wrong name / not found — try next
        raise IDMCError(
            f"No matching taskflow service name for '{name}' (tried {candidates})",
            status_code=getattr(last_err, "status_code", None))

    # ── ingestion & replication (Data Ingestion / mftsaas API) ────────────
    # Mass ingestion / replication jobs (e.g. dbmir_*) do NOT appear in the
    # v2 activityMonitor/activityLog — they live behind this separate API at
    # the pod host ROOT (no /saas). Session rides as IDS-SESSION-ID (already
    # in _headers). Response shapes marked # CONFIRM until tested live.
    async def list_ingestion_tasks(self):
        """List file/data ingestion and replication tasks (mitasks)."""
        return await self._request("GET", "/mftsaas/api/v1/mitasks",
                                   saas=False)

    async def get_ingestion_task(self, task_id: str):
        """One ingestion/replication task's details by its mitask id."""
        return await self._request("GET", f"/mftsaas/api/v1/mitasks/{task_id}",
                                   saas=False)

    async def get_ingestion_activity_log(self, task_id: str):
        """Job history for one ingestion/replication task.  # CONFIRM shape"""
        return await self._request(
            "GET", "/mftsaas/api/v1/mitasks/activityLog",
            params={"taskId": task_id}, saas=False)

    async def get_dbmi_job_status(self, job_id: int):
        """Status of a DATABASE/APPLICATION ingestion & replication job.
        Lives on the INGESTION host (see _ing_host), NOT the pod host.
        Primary: documented GET /dbmi/public/api/v2/job/status with a JSON
        body {jobId}; fallback on 404 (older pods): the monitor UI's
        /dbmi/api/v1/job/{id}/metrics/v2. Job names embed the id:
        dbmir_stage_jira_5632 -> jobId 5632. Statuses include Up and
        Running / Running with Warning / On Hold / Stopped / Failed /
        Deploying / Aborted / Completed; response carries errorMessage,
        lastAction, timings and jobConfig (taskMode UNLOAD/CDC/COMBINED,
        src/tgt connections)."""
        host = self._ing_host()
        try:
            return await self._request(
                "GET", "/dbmi/public/api/v2/job/status",
                url_override=f"{host}/dbmi/public/api/v2/job/status",
                json={"jobId": int(job_id)})
        except IDMCError as e:
            if e.status_code != 404:
                raise
            return await self._request(
                "GET", f"/dbmi/api/v1/job/{int(job_id)}/metrics/v2",
                url_override=f"{host}/dbmi/api/v1/job/{int(job_id)}/metrics/v2")

    async def get_dbmi_job_metrics(self, job_id: int,
                                   state_filter: str | list[str] | None = None,
                                   search: str = "",
                                   limit: int = 25,
                                   offset: int = 0):
        """Per-object (table-level) metrics for a DATABASE/APPLICATION
        ingestion & replication job. Lives on the INGESTION host, like
        get_dbmi_job_status. Documented POST /dbmi/public/api/v2/job/metrics
        with jobId + metricsOptions body. Returns jobInfo (same shape as the
        status call) plus metricsInfo[] — one entry per task with
        recordsRead/recordsWritten and subTasks[] carrying per-table srcName,
        tgtName, state (RUNNING/COMPLETED/FAILED/...) and, for CDC tasks,
        inserts/updates/deletes counts — plus counts.statusCounts and
        currentThroughput. 409 = job not in a valid state for stats.

        stateFilter must be an ARRAY on the wire (a bare string 500s with a
        Jackson ArrayList deserialization error — tested live 2026-09-02);
        accepts a single state string or a list and normalizes to a list."""
        host = self._ing_host()
        if isinstance(state_filter, str):
            state_filter = [state_filter]
        body = {
            "jobId": int(job_id),
            "parameters": {
                "metricsOptions": {
                    "stateFilter": state_filter,
                    "sort": ["srcTable", "asc"],
                    "search": search or "",
                    "limit": int(limit),
                    "offset": int(offset),
                }
            },
        }
        return await self._request(
            "POST", "/dbmi/public/api/v2/job/metrics",
            url_override=f"{host}/dbmi/public/api/v2/job/metrics",
            json=body)

    # ── job control (parity with stock MCP) ───────────────────────────────
    async def run_task(self, task_id: str, task_type: str = "MTT"):
        body = {"@type": "job", "taskId": task_id, "taskType": task_type}
        return await self._request("POST", "/api/v2/job", json=body)

    async def stop_job(self, task_id: str, task_type: str = "MTT"):
        body = {"@type": "job", "taskId": task_id, "taskType": task_type}
        return await self._request("POST", "/api/v2/job/stop", json=body)


# ── helpers ────────────────────────────────────────────────────────────────
def parse_start_time(value: Any):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    s = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def elapsed_hours(start, now=None):
    now = now or datetime.now(timezone.utc)
    return (now - start).total_seconds() / 3600.0


# ── error summarization (rule-based) ───────────────────────────────────────
# Pattern-matches the raw activityLog `errorMsg` into a one-line plain-English
# summary plus a speculative "why it likely failed". Deterministic, no external
# calls; extend ERROR_RULES as new failure shapes show up.
ERROR_RULES: list[tuple[str, str, str]] = [
    # (substring to match, summary, likely cause)
    # -- rules mined from real prod failures (2026-07 ledger analysis) --
    ("was stopped by user",
     "Not an error — the job was manually stopped (API or UI).",
     "Someone intentionally killed the run; the 'startedBy'-style name in "
     "the message says who. Only investigate if nobody claims it."),
    ("another instance of the task is currently running",
     "Skipped: the previous run of this task was still going.",
     "The prior run overran into this schedule slot — check why the earlier "
     "run is slow/hung; this failure clears itself once it finishes."),
    ("is not recognized",
     "Snowflake rejected a value that doesn't fit the column type "
     "(e.g. a date string into a NUMBER column).",
     "Source schema drift or a changed source query — compare the failing "
     "value in the message against the target column's type."),
    ("duplicate row detect",
     "Snowflake MERGE hit duplicate join keys (nondeterministic update).",
     "The source delivered multiple rows for the same merge key — dedupe "
     "upstream or add a tiebreaker to the merge key."),
    ("deadlock",
     "SQL Server chose this task as a deadlock victim.",
     "Transient contention with another process on the same tables — "
     "usually succeeds on rerun; recurring deadlocks need scheduling or "
     "indexing changes."),
    ("dtm process terminated unexpectedly",
     "The task's engine process (DTM) crashed on the Secure Agent.",
     "Usually agent-side resource exhaustion (memory/disk) or an agent "
     "restart mid-run — check the agent box around the failure time; "
     "recurring crashes warrant an Informatica support case."),
    ("no more data available to read",
     "The database connection dropped mid-read (PostgreSQL).",
     "Long-running query hit a server/network timeout or the DB closed the "
     "session — common on very long extracts; consider chunking or keepalive "
     "settings."),
    ("unknownhostexception",
     "DNS could not resolve a source/target hostname.",
     "Transient DNS blip on the agent, or the endpoint's hostname changed — "
     "if it recurs, check the agent box's resolver and the connection URL."),
    ("repoexception",
     "IICS internal service error while starting the task (HTTP 500).",
     "Informatica-side platform issue, usually transient — rerun; if it "
     "persists across hours, check trust.informatica.com / open a case."),
    # -- original rules --
    ("sql compilation error",
     "Snowflake rejected the SQL the connector generated (COPY INTO failed).",
     "Likely a bad/changed connection config — e.g. the warehouse/database "
     "setting on the Snowflake connection contains something invalid (such as "
     "a full 'use warehouse' statement or URL where a name belongs)."),
    ("copy_into_table",
     "The Snowflake bulk-load (COPY INTO) step failed; rows were read but not loaded.",
     "Check the Snowflake connection's warehouse/stage settings and the "
     "target table definition."),
    ("deinitdatasession",
     "The target adapter crashed while closing its data session.",
     "Usually a downstream symptom of an earlier load error — check the "
     "target-side error above it."),
    ("does not exist",
     "A referenced database object (table/relation) is missing.",
     "The table was dropped/renamed, the connection points at the wrong "
     "database/schema, or the credential can't see it (e.g. PostgreSQL "
     "'relation does not exist' also fires on schema/search_path and "
     "permission mismatches)."),
    ("not authorized", "The credential wasn't allowed to perform this action.",
     "The user/group isn't in the asset's Allowed Users/Groups, or the role "
     "is missing a privilege."),
    ("authentication", "Authentication to a source/target system failed.",
     "Expired or rotated credentials on the connection."),
    ("connection refused", "Could not reach the source/target system.",
     "Endpoint down, wrong host/port, or a network/firewall change."),
    ("timeout", "The operation timed out.",
     "Slow source/target system or an unusually large data volume."),
    ("no such file", "An expected file was missing.",
     "The upstream process didn't deliver the file, or the path/name changed."),
    ("file not found", "An expected file was missing.",
     "The upstream process didn't deliver the file, or the path/name changed."),
    ("unique constraint", "The target rejected duplicate key values.",
     "Source delivered rows that already exist — check dedup logic or a "
     "double-run."),
    ("out of memory", "The task ran out of memory on the agent.",
     "Data volume grew past what the Secure Agent box can hold — consider "
     "partitioning or a bigger box."),
]


def compact_failure(e: dict) -> dict:
    """Reduce a (potentially multi-MB) activityLog entry for a FAILED run to
    the fields that matter for alerting/forensics. Shared by the monitor's
    ledger and the find_failed_runs tool so records line up."""
    return {
        "name": e.get("objectName") or e.get("taskName") or "<unknown>",
        "run_id": str(e.get("runId") or ""),
        "task_id": str(e.get("objectId") or e.get("taskId") or ""),
        "started_utc": e.get("startTimeUtc"),
        "ended_utc": e.get("endTimeUtc"),
        "success_rows": e.get("totalSuccessRows"),
        "failed_rows": e.get("totalFailedRows"),
        "parent_task_federated_id": e.get("parentTaskFederatedId"),
        "error_summary": summarize_error(e.get("errorMsg")),
        "error_raw": (e.get("errorMsg") or "")[:2000],
    }


def summarize_error(error_msg: str | None) -> dict | None:
    """Return {'summary','likely_cause','raw_first_line'} for a failed run's
    errorMsg, or None when there's no real error. Rule-based; falls back to
    the trimmed first line labeled uncategorized."""
    if not error_msg or "no errors encountered" in error_msg.lower():
        return None
    # First meaningful line, stripped of the log-timestamp prefix noise.
    first = next((ln.strip() for ln in error_msg.splitlines() if ln.strip()), "")
    low = error_msg.lower()
    for needle, summary, cause in ERROR_RULES:
        if needle in low:
            return {"summary": summary, "likely_cause": cause,
                    "raw_first_line": first[:300]}
    return {"summary": "Task failed (uncategorized error).",
            "likely_cause": "See the raw message — pattern not yet in ERROR_RULES.",
            "raw_first_line": first[:300]}
