# Informatica IDMC MCP Server — Design & Architecture

**Status:** in development as of 2026-07-09 (tools being refined for order of operation; monitor redesigned as a 2-hour Slack digest)
**Author:** Joe Peck
**Update Date:** 2026-07-09 (clean rewrite — all prior DEPRECATE/UPDATE annotations resolved)

> Scope note: Development project, moving toward production. The host is a
> **non-production AWS EC2 box** (stood up to test Informatica's discontinued
> AI-agent tool, otherwise unused). Because it stands alone, the same box is a
> reasonable production host — the move is just switching the login from the
> IICS **dev** org to the **prod** org. Care is needed on prod for start/stop
> abilities: **mapping tasks are readily accessible via the API, but taskflows
> must be modified in IICS (Allowed Users/Groups on the Start step) before the
> MCP can start them.** Single user (Joe) for now, which contains the risk.

---

## 1. Goals

1. **Test and refine the tools.** The tools must follow a defined **order of
   operation** (see §5) — v3 for discovery/context, v2 for run ids and actions.
2. **2-hour Slack digest monitor:** every 2 hours, post one Slack message with
   (a) what is **running**, (b) what **completed within the 2-hour window**,
   and (c) a **flag on anything running more than 2 cycles** (4 hours).
3. **Claude Desktop** remains the interactive host, reached via an
   **SSH stdio bridge** to the box (§7).
4. The always-on monitoring runs on the existing Linux box (AWS EC2; its
   Secure-Agent role is incidental) via a **systemd timer set to 2 hours**.

---

## 2. Key architectural decision: two processes, one shared library

| Need | Model | Triggered by |
|------|-------|--------------|
| Interactive control/query (richer tools) | **Pull** | Claude Desktop, only during a session |
| 2-hour Slack digest | **Push / always-on** | systemd timer, no human present |

**Why the monitor can't be an MCP tool:** Claude Desktop only launches an MCP
server for the duration of a chat session and never calls tools on its own —
there is no background loop. So the Slack digest comes entirely from
`monitor.py` on the EC2 box: the **systemd timer fires every 2 hours**, the
script pulls the data, does the calculations, formats the message, and posts
**directly to Slack** via an incoming webhook. Claude Desktop is never in the
push path.

Two processes share one client:

```
EC2 box
├── idmc_client.py   # SHARED: v3+v2 login, session caching, retries, API calls
├── mcp_server.py    # MCP server (stdio) — curated tools            [PULL]
└── monitor.py       # standalone, systemd timer every 2h            [PUSH]
        ├── polls activityMonitor → RUNNING mapping tasks (MTT)
        ├── polls activityLog     → MTTs COMPLETED in the 2h window
        └── posts ONE Slack message with both lists
                (+ ⚠ flag on jobs running > 2 cycles = 4h)

Claude Desktop (laptop)  ──ssh stdio bridge──▶  mcp_server.py
```

Activity Monitor and Activity Log only show the **running** and **final end
status** of **mapping tasks (MTT)** — taskflow state is separate (§5, Taskflow
status).

### The Slack digest content (kept deliberately simple)

**Running list** (from `activityMonitor`): task name, `startTimeUtc`, elapsed;
flagged when elapsed ≥ 4h (2 cycles of 2h).

**Recently-completed list** (from `activityLog`, `endTimeUtc` within the
window since the last digest): `objectName`, `startTimeUtc` → `endTimeUtc`,
state (SUCCESS/WARNING/FAILED), `totalSuccessRows`/`totalFailedRows`, and —
for failures — a **rule-based simplified error + speculative "why it failed"**
derived from `errorMsg` (see §6). `parentTaskFederatedId` is retained on the
entry for taskflow lookup.

---

## 2.5 What the box actually provides

An **always-on Linux machine on AWS with IAM + egress** — enough to host the
continuous monitor and the bridged MCP server. Being a non-prod, unused dev
box, there's no contention concern; run the services freely. (These tools call
the cloud REST API, so the box's Secure-Agent role buys nothing functional.)

---

## 3. Informatica API foundation

The v3 and v2 APIs are deliberately split so both can be used in a coherent
order of operation.

### Logins — both supported, sessions interchangeable

- **v3 login:** `POST {host}/saas/public/core/v3/login` with username/password
  → `userInfo.sessionId` + product `baseApiUrl`
  (e.g. `https://na2.dm-us.informaticacloud.com/saas`). Default login.
- **v2 login:** `POST https://dm-us.informaticacloud.com/ma/api/v2/user/login`
  → the full v2 user record (roles, usergroups, `serverUrl`, `icSessionId`).
  Used to get relevant data about the calling user.
- The MCP server can use the v3 and v2 session ids **interchangeably** —
  the client sends the session under both `INFA-SESSION-ID` and `icSessionId`
  headers, so whichever login the order of operation used works everywhere.

### ID rules (critical)

- v3 `objects`/`lookup` ids are **federated ids** — context only; they are
  **NOT valid on v2 endpoints**.
- **ALL asset ids used in v2 APIs must come from v2 responses**
  (activityMonitor / activityLog / task / mapping…).
- An activityLog entry's **`parentTaskFederatedId` matches a v3 object id** —
  resolving it (v3 `lookup`) identifies the **parent taskflow** of the mapping
  task that ran.

### v3 — discovery & administration

- **Object lookup (all asset types):**
  `GET /saas/public/core/v3/objects?q=type=='TASKFLOW'` — lists assets by type
  with path, updatedBy, updateTime, tags, publication status. DI types include
  DTEMPLATE (mapping), MTT (mapping task), DSS, DMASK, DRS, MAPPLET, BSERVICE,
  HSCHEMA, PCS, FWCONFIG, CUSTOMSOURCE, MI_TASK, WORKFLOW (linear taskflow),
  VISIOTEMPLATE, TASKFLOW (types not case sensitive).
- **Lookup:** `POST /saas/public/core/v3/lookup` — resolve a federated id
  (e.g. `parentTaskFederatedId`) to path/name/type.
- **Users / user groups / roles:** `GET /saas/public/core/v3/users`,
  `/userGroups`, `/roles`.

**Asset structure:** mapping (all data transformations) → **mapping task**
(job that runs the mapping, holds parameters) → **taskflow** (orchestration
layer: runs mapping tasks and other taskflows, sends notifications).

### v2 — activity, metadata, actions

- All v2 resources: `{baseApiUrl}/api/v2/...` (baseApiUrl ends in `/saas`).
- `GET /api/v2/mapping` — list mappings (returns **XML**). Mappings can't be
  tied to mapping tasks by id, but by our naming standard they share names
  minus the prefix (`mp_` mapping ↔ `mt_` mapping task) and live in the same
  folder.
- **`activityMonitor`** = running jobs only; **`activityLog`** = completed.
  Both only cover mapping-task (MTT) runs.
- **Use `startTimeUtc`, not `startTime`, for elapsed.** Plain `startTime`
  carries a misleading `Z` and is local time (hours off); `startTimeUtc` is
  true UTC. In activityMonitor the name is `taskName` (`objectName` empty);
  in activityLog the name is `objectName`.
- Sessions expire → the client re-logs-in once on a 401.

> Some endpoints live off the `/saas` prefix (the `/active-bpel` taskflow
> API); the client's `saas=False` flag drops it. If any call 404s, flip that
> flag — it's the first thing to check.

---

## 4. `idmc_client.py` (shared library)

- v3 login (default) **and** v2 login (`login_v2` / `get_v2_user_details`);
  cache session + base URL; re-login on 401.
- Thin typed method per endpoint; centralizes timeouts/retry/error handling.
- Handles **JSON and XML** responses (mapping endpoints return XML → text).
- `lookup_objects()` for federated-id → taskflow resolution.
- `summarize_error()` — rule-based error simplification (shared by monitor
  and MCP server).
- Credentials from env / AWS Secrets Manager (§8); never hard-coded.
- Reused by both `mcp_server.py` and `monitor.py`.

---

## 5. Tool catalog (curated, tiered by risk)

### Order of operation for user queries

1. **`search_objects` (v3)** — resolve *what* the user is asking about
   (type/path/updateTime/tag). **Context only** — provides no v2 ids.
2. **Activity layer (v2)** — `list_running_jobs` / `get_activity_log` provide
   the **run ids and task ids** every other v2 tool needs.
3. **`lookup_taskflow_for_run` (v3)** — `parentTaskFederatedId` → parent
   taskflow name; ~99% of published Service-URL api names are just the
   taskflow name, which feeds `run_taskflow_by_name`.
4. **Tier 2/3 tools** — confirm-gated writes, ids/names from steps 2–3.

### Tier 1 — Read-only (built)

- **Discovery (v3):** `search_objects(query)`, `lookup_taskflow_for_run(id)`,
  `get_asset_dependencies(id, refType)` — v3 `objects/{id}/references`:
  `Uses` = what an asset is made of; `usedBy` = everything built on it (e.g.
  all assets using a connection). References carry **`appContextId`, a
  service-specific id that IS valid on v2 endpoints** — the bridge across the
  v3-ids-don't-work-on-v2 rule.
- **Monitoring:**
  - `list_running_jobs` — activityMonitor list of running MTTs
  - `get_activity_log` — recently completed MTTs
  - `get_mapping_task_status(run_id)` *(renamed from `get_job_status`)* —
    MTT run detail; run ids come from activityMonitor/activityLog. FAILED
    runs are enriched with `error_summary` (simplified message + speculative
    likely cause — rule-based, see below).
  - `get_audit_log` — all org entries (connection updates, logins, etc.)
  - `find_failed_runs(days_back, name_filter, source)` — FAILED runs going
    back N days: reads the monitor's `failures.jsonl` ledger (instant), with
    API paging as fallback for pre-ledger history. Compact records only.
  - *(removed: `list_long_running_jobs` — not necessary; the monitor flags
    long-runners)*
- **Org/server:** `get_org`, `get_server_time`, `get_runtime_environments`,
  `get_users`, `get_security_log`, `get_v2_user_details`
- **Metadata:** `get_tasks(type)` (DMASK/DRS/DSS/MTT/PCS), `get_agent_details`,
  `get_connections`, `get_connectors`, `get_mappings` (XML), `get_mapping`
  (XML), `get_custom_functions`, `get_schedules`
- **Connection data:** `preview_source_data` / `preview_target_data`,
  `get_source_fields` / `get_target_fields`, `validate_expression`.
  Database connectors need the qualified `DB/SCHEMA/TABLE` object path.
  `preview_target_data` doubles as the post-change smoke test for a
  connection (verified during the 2026-08 key-pair migration: one read-only
  call proves auth with no job execution).
- **Taskflow status:** `get_taskflow_status(run_id)` — `/active-bpel`; takes
  the large taskflow runId (currently obtained via the UI), returns
  RUNNING/SUSPENDED/FAILED + assetName/duration/startedBy.

**Error simplification (rule-based):** `summarize_error()` pattern-matches the
raw `errorMsg` (Snowflake SQL compilation, COPY INTO, auth, connection,
timeout, missing file, unique constraint, OOM…) into a one-line plain-English
summary plus a speculative likely cause; unmatched errors fall back to the
trimmed first line labeled uncategorized. Extend `ERROR_RULES` as new failure
shapes appear.

### Tier 2 — Write / state-changing (confirm-gated)

- `run_mapping_task(task_id, task_type)` — task ids from the activity log
  (`objectId`) or `get_tasks`
- **Taskflows:** `run_taskflow_by_name(api_name, confirm)` (start a NEW run
  via `/active-bpel/rt/{api_name}` — how a FAILED/terminated taskflow is
  re-run; api_name resolved via `lookup_taskflow_for_run`),
  `resume_taskflow(run_id, confirm)` (resumeWithFaultRetry — only continues a
  SUSPENDED run), `run_taskflow(taskflow_id, confirm)` (numeric management
  id; refresh-job pattern), `heal_taskflow(...)` (SUSPENDED → resume /
  FAILED → new run).
  > Key facts: **resume only works on SUSPENDED runs** — FAILED runs must be
  > restarted via api_name (new runId). Published copies get `-1`/`-2`
  > suffixes; `run_taskflow_smart` tries exact name then suffixes. The caller
  > must be in the taskflow's Allowed Users/Groups (set in IICS — required
  > before prod).
- `update_connection(connection_id, updates, confirm)` — v2
  `POST /connection/{id}`. **Highest blast radius of the write tools**: every
  asset on the connection inherits the change immediately. Preview mode
  returns a current-vs-proposed diff per field; pair with
  `get_asset_dependencies(usedBy)` to see affected assets first. Credential
  fields should be rotated in the IICS UI, not through this tool.
- schedule create/update/enable-disable, `test_connection` *(future)*

### Tier 3 — Destructive (guardrails mandatory)

- `stop_running_job(task_id, task_type, confirm)` — returns a **preview**
  unless `confirm=True` (human-in-the-loop).
- `terminate_taskflows(run_ids, confirm)` — v2
  `PUT /active-bpel/services/tf/terminate`, up to 200 taskflow runIds per
  call. Preview mode resolves each run's current status/name before the
  kill. Terminated runs cannot be resumed (restart only). Requires the
  caller in each taskflow's Allowed Users/Groups — add the team account's
  group in IICS, same as for restart.

**Guardrail principle:** Tier 2/3 tools state what they'll change and require
an explicit confirm; production-affecting actions never fire autonomously.

---

## 6. The monitor (`monitor.py`) — failure-focused Slack alerts

Replaces the old email/auto-stop design entirely (email alerts, 6h thresholds,
auto-stop escalation, protected lists, and taskflow healing are **removed**;
the monitor is read-only). **Why:** Informatica's native alerting is
per-taskflow failure emails — unusable for a team channel. The monitor makes
failures *visible in Slack* and *queryable long after the fact*.

> Lesson from prod (2026-07-10): a fixed newest-N activity-log pull silently
> missed failures once prod volume exceeded one page — a full-digest format
> also buried the failures that did appear. Hence the redesign below.

**Environment targeting:** dev and prod mirror each other with no
distinguishing field, so the monitor targets an environment by **which org the
account logs into** (running against prod as of 2026-07-11).

**Single pass on a systemd timer, every 2 hours:**

1. Login; pull `activityMonitor` → running MTTs.
2. Pull `activityLog` **paginated** (`rowLimit` + `offset`, newest first)
   until the whole window since the last pass is covered — never a fixed
   newest-N (state-file bookkeeping prevents gaps/overlaps if a timer fire
   is late).
3. **Alert to Slack ONLY when something needs attention:**
   - FAILED runs in the window — one loud `:rotating_light:` message with,
     per failure: start→end (CT), rows, **simplified error + speculative
     cause** (rule-based, §5), and the **parent taskflow** (resolved via v3
     lookup of `parentTaskFederatedId`).
   - Running jobs ≥ `CYCLE_HOURS × LONG_RUN_CYCLES` (2h × 2 = **4h**).
   - Healthy passes post **nothing**.
4. **Ledger:** every failure is appended (compact JSON) to `failures.jsonl`
   next to the code — the data source for the `find_failed_runs` MCP tool,
   so "what failed last Tuesday?" never re-pages the huge activity log.
5. **Daily heartbeat:** first pass at/after `HEARTBEAT_HOUR` CT (default 8)
   posts counts since the last heartbeat (runs completed/failed, currently
   running) — so Slack silence provably means "healthy", not "monitor dead".
6. **Failsafe:** a crashed pass posts `:rotating_light: monitor pass FAILED`
   to Slack and exits non-zero. Slack-post failures themselves never abort a
   pass (log-only). All displayed times are Central; internals are UTC.
7. Persist `last_run_utc`, heartbeat counters, and ledger dedup keys.

**Quick test:** `python3 monitor.py --test` posts a test message to verify the
webhook wiring; a normal pass can be run any time with `python3 monitor.py`
(or `sudo systemctl start infra-monitor.service`, which also resets the 2h
clock).

**Channel routing:** `SLACK_WEBHOOK_URL` (primary/ops channel) gets
everything — alerts, heartbeat, failsafe. `SLACK_WEBHOOK_URL_TEAM`
(#team-data-platform) gets ONLY failure/long-run alerts, keeping the team
channel pure signal. `--test` posts to both.

**Config:** `IDMC_LOGIN_URL/USERNAME/PASSWORD`, `IDMC_ORG`, `CYCLE_HOURS` (2),
`LONG_RUN_CYCLES` (2), `HEARTBEAT_HOUR` (8, CT), `SLACK_WEBHOOK_URL`,
`SLACK_WEBHOOK_URL_TEAM`, `STATE_FILE`, `LEDGER_FILE`, optional
`IDMC_SECRET_NAME` (AWS Secrets Manager).

---

## 7. Transport: Claude Desktop ↔ server (SSH stdio bridge)

Claude Desktop's config file connects to **stdio servers only**; its
remote-HTTP connectors run from Anthropic's cloud (need public + OAuth). So
for a private box the path is an **SSH (or SSM) stdio bridge**: Claude Desktop
runs a local `ssh` command that launches `mcp_server.py` on the box and pipes
stdio. No public exposure, no token. (VPN must be up.) **Implemented and
working.**

```jsonc
"informatica": {
  "command": "ssh",
  "args": ["-T", "idmc-box",
           "/home/jbpeck/informatica-mcp/.venv/bin/python /home/jbpeck/informatica-mcp/mcp_server.py"]
}
```

**Multi-user (team) mode:** each teammate gets their own Linux account on the
box and their own `~/.informatica-mcp.env` (chmod 600) holding THEIR IICS
credentials — config loads the per-user file before the repo `.env`, so every
session runs under the caller's own IICS identity (permissions + audit
attribution). Shared read-only code lives in `/opt/informatica-mcp`. Full
onboarding: see TEAM_SETUP.md. Per-tool authorization does not exist in this
model (everyone sees the same confirm-gated tool set; IICS enforces per-user
rights) — that boundary is the Phase 5 guardrails work.

---

## 8. Security & governance

- **Least-privilege service account**, scoped to the org/APIs the tools need.
- **Secrets** (IDMC password, **Slack webhook URL**) in **AWS Secrets
  Manager** via the EC2 instance role, or a `chmod 600` `.env` for dev —
  never in code/git.
- **Credential-hosting check:** dev box, but the credential can affect prod
  jobs — scope it tightly; clear storing it here with security.
- **Destructive tools** confirmation-gated; audit every write/destructive call.
- **Prod move checklist:** switch credential to the prod org; add the MCP's
  user/group to each taskflow's Allowed Users/Groups in IICS; confirm the
  Slack webhook with SPS Slack admins.
- **Incident alignment:** if alerts drive real response, align with SPS SIP25.

---

## 9. Phased roadmap

- **Phase 1 — Slack digest monitor.** 2-hour systemd timer + webhook digest
  (this rewrite). Validate in dev: run windows, long-run flag, error
  summaries.
- **Phase 2 — Tool refinement.** Enforce the §5 order of operation in tool
  descriptions/behavior; validate `lookup_taskflow_for_run` →
  `run_taskflow_by_name` end-to-end.
- **Phase 3 — Production move.** Prod credential, taskflow Allowed
  Users/Groups in IICS, approved Slack webhook, Secrets Manager.
- **Phase 4 — Hardening.** Monitor dead-man's-switch, retire the stock
  connector, extend ERROR_RULES from real failures.
- **Phase 5 — Productionize per SPS MCP guardrails** (deferred; see
  developer.docs.spscommerce.com/guardrails → MCP): Streamable HTTP
  transport, Auth0 authorization with per-tool access control, kebab-case
  `sps-*` tool naming + explicit tool annotations, containerized deploy to
  Atlas EKS with Tech Registry Service ID, and an Engineering Enablement
  Review before launch. Monitor placement (EKS CronJob vs. EC2) decided
  then.

---

## Status / decisions

### Resolved

- ✅ Host: non-prod dev EC2 box — run services freely; same standalone box is
  the intended prod host (login swap only).
- ✅ Login: **v3 and v2 both supported**; session ids interchangeable across
  API families.
- ✅ Replacing the stock 3-tool MCP: **done** (parity + broad read set).
- ✅ Monitoring: **2-hour Slack digest** (running / completed-in-window /
  >2-cycle flag). Email + auto-stop design retired.
- ✅ Error simplification: **rule-based** summary + speculative cause.
- ✅ Transport: **SSH stdio bridge** (working).
- ✅ `startTimeUtc` (not `startTime`) confirmed for elapsed.
- ✅ All v2 endpoints tested; host routing baked into the client.

### Still open

1. **Slack webhook** — needs SPS Slack-admin approval/creation; until then
   the monitor runs log-only.
2. **Taskflow run ids** still come from the UI for `get_taskflow_status` —
   find an API path.
3. **Prod taskflow permissions** — add the MCP user to Allowed Users/Groups
   per taskflow before the prod move.
4. **Cutover:** retire the stock `job-management` connector once parity
   verified (duplicate tool-name overlap until then).

---

## Sources

- [IICS REST API reference (docs.informatica.com)](https://docs.informatica.com/integration-cloud/b2b-gateway/current-version/rest-api-reference/informatica-intelligent-cloud-services-rest-api.html)
- [v3 Login](https://docs.informatica.com/integration-cloud/b2b-gateway/current-version/rest-api-reference/platform-rest-api-version-3-resources/login.html)
- [v2 activityMonitor](https://docs.informatica.com/integration-cloud/cloud-platform/current-version/rest-api-reference/platform-rest-api-version-2-resources/activitymonitor.html)
- [TPN Developer Reference (v2.0)](https://developer.informatica.com/tpn/v2.0/reference/welcome)
