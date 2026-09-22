[README.md](https://github.com/user-attachments/files/32423086/README.md)
# Informatica IDMC MCP Server

An MCP (Model Context Protocol) server and companion monitoring daemon that put
Informatica Intelligent Cloud Services (IDMC/IICS) under natural-language
control — and keep watching the platform when nobody is asking.

Two processes, one shared REST client:

| | What it is | Trigger | Where it runs |
|---|---|---|---|
| **`mcp_server.py`** | 42-tool MCP server over stdio | A human asking a question in Claude Desktop | EC2 box, launched per session over an SSH stdio bridge |
| **`monitor.py`** | Failure-focused Slack monitor | `systemd` timer, every 2 hours | EC2 box, always on |

Both import `idmc_client.py`. One login implementation, one retry policy, one
error-summarization rule set — used by the interactive path and the unattended
path alike.

> **Status:** in active development, mid-migration to production. The **host** is
> a standalone non-prod EC2 box (it stands alone, so it is also the intended prod
> host — the move is a login swap). The **credential** points at the prod IICS
> org, so read tools see production and write tools would change it. Still open
> before Phase 3 closes: per-taskflow Allowed Users/Groups in IICS, an
> SPS-approved Slack webhook (until then the monitor runs log-only), and secrets
> in AWS Secrets Manager. Multi-user via a shared team credential (see
> [`TEAM_SETUP.md`](TEAM_SETUP.md)). Full rationale and decision log:
> [`informatica-mcp-design.md`](informatica-mcp-design.md).

---

## Table of contents

- [Quick start](#quick-start)
- [Architecture](#architecture)
- [Tool reference](#tool-reference)
- [Order of operation](#order-of-operation)
- [Depth of analysis](#depth-of-analysis-what-you-can-actually-ask)
- [The monitor](#the-monitor)
- [Why an EC2 Linux box](#why-an-ec2-linux-box)
- [The monitor as another worker](#the-monitor-as-another-worker)
- [Design principles](#design-principles)
- [Security and governance](#security-and-governance)
- [Repository layout](#repository-layout)
- [Testing and verification](#testing-and-verification)
- [Roadmap and open items](#roadmap)
- [What this project demonstrates](#what-this-project-demonstrates)

---

## Quick start

### If you are a teammate who just wants the tools in Claude Desktop

You need SPS VPN, a Linux account on the box, and Claude Desktop. You do **not**
need your own IICS credential — a shared team credential is configured centrally.
Full instructions: **[`TEAM_SETUP.md`](TEAM_SETUP.md)**.

The short version, in `~/Library/Application Support/Claude/claude_desktop_config.json`:

```jsonc
"mcpServers": {
  "informatica": {
    "command": "ssh",
    "args": ["-T", "idmc-box",
             "/opt/informatica-mcp/.venv/bin/python /opt/informatica-mcp/mcp_server.py"]
  }
}
```

Fully quit Claude Desktop (Cmd+Q) and reopen. Smoke test: ask it to run
`list_running_jobs`.

### If you are setting up a box from scratch

```bash
git clone git@github.com:jpeckjr/informatica-mcp.git
cd informatica-mcp
./setup.sh                 # creates .venv, installs deps, then stops
# create .env with IDMC_LOGIN_URL / IDMC_USERNAME / IDMC_PASSWORD; chmod 600
./setup.sh                 # re-run: unit tests + one live monitor pass
```

> **Known gap:** `setup.sh` copies `.env.example` to `.env` on a first run, but
> `.env.example` is not currently in the repo, so the first run aborts under
> `set -e`. Create `.env` by hand (see the config table below) or add the
> template. Tracked in [open items](#roadmap).

Then install the monitor as a systemd timer. **Edit the unit first** — it
hardcodes `/home/jbpeck/informatica-mcp` as `WorkingDirectory` and interpreter
path:

```bash
sudo cp deploy/infra-monitor.{service,timer} /etc/systemd/system/
sudoedit /etc/systemd/system/infra-monitor.service   # fix User= and the paths
sudo systemctl daemon-reload
sudo systemctl enable --now infra-monitor.timer
systemctl list-timers infra-monitor.timer      # confirm next fire
python3 monitor.py --test                      # verify Slack webhook wiring
```

### Configuration

All configuration is environment variables, loaded by `config.py` in strict
priority order — **first value seen wins**:

1. Real environment variables (systemd `Environment=`, shell exports)
2. `~/.informatica-mcp.env` — the *invoking user's* own credentials (chmod 600)
3. `.env` next to the module — the shared/service fallback

| Variable | Default | Purpose |
|---|---|---|
| `IDMC_LOGIN_URL` | *(required)* | IICS login host, e.g. `https://dm-us.informaticacloud.com` |
| `IDMC_USERNAME` | *(required)* | IICS account |
| `IDMC_PASSWORD` | *(required)* | IICS password (or supply via `IDMC_SECRET_NAME`) |
| `IDMC_ORG` | `prod` | Label only — the *actual* target org is whichever org the credential logs into |
| `IDMC_SECRET_NAME` | — | AWS Secrets Manager secret; pulls `idmc_password`, `slack_webhook_url`, `slack_webhook_url_team` via the EC2 instance role |
| `AWS_REGION` | `us-east-2` | Region for Secrets Manager |
| `CYCLE_HOURS` | `2` | Monitor pass cadence (must match the systemd timer) |
| `LONG_RUN_CYCLES` | `2` | A running job is flagged after `CYCLE_HOURS × LONG_RUN_CYCLES` (= 4h) |
| `HEARTBEAT_HOUR` | `8` | Central-time hour for the daily proof-of-life post |
| `SLACK_WEBHOOK_URL` | — | Primary/ops channel — gets everything. Unset ⇒ log-only mode |
| `SLACK_WEBHOOK_URL_TEAM` | — | Team channel — gets **only** failure/long-run alerts |
| `STATE_FILE` | `./monitor_state.json` | Window bookkeeping between passes |
| `LEDGER_FILE` | *(next to the module)* | `failures.jsonl` — the failure ledger |

`config.py` validates the three required variables at load and fails loudly
with the names of anything missing. One variable sits outside it: `LOG_LEVEL`
(default `INFO`) is read directly by `monitor.py`, so it must come from the real
environment and does not participate in the layered `.env` loading above.

---

## Architecture

```
                    EC2 box (private, VPN only)
                    ─────────────────────────────────────────────
                    idmc_client.py        SHARED LIBRARY
                      • v3 + v2 login, interchangeable sessions
                      • host routing (/saas vs /active-bpel)
                      • 401 → single silent re-login
                      • JSON and XML response handling
                      • ERROR_RULES / summarize_error()
                              ▲                    ▲
                              │                    │
              ┌───────────────┘                    └────────────────┐
              │                                                     │
       mcp_server.py                                          monitor.py
       42 stdio tools            [PULL]                 single pass    [PUSH]
       launched per chat session                        systemd timer, every 2h
              ▲                                                     │
              │ ssh -T (stdio pipe)                                 │ webhook
              │                                                     ▼
       Claude Desktop (laptop)                               Slack channels
                                                        (ops = everything,
                                                         team = alerts only)
                                                                    │
                                        failures.jsonl  ◀───────────┘
                                        (ledger, read back by find_failed_runs)
```

Diagrams: [`demo/informatica_mcp_architecture_v2.svg`](demo/informatica_mcp_architecture_v2.svg),
[`demo/informatica-mcp-order-of-operation.svg`](demo/informatica-mcp-order-of-operation.svg),
[`demo/agent-vs-mcp.svg`](demo/agent-vs-mcp.svg).

### The API foundation

IDMC exposes two REST API families, and this project deliberately uses both:

- **v3** (`/saas/public/core/v3/...`) — discovery and administration. Object
  search by type/path/tag, federated-id lookup, object references (lineage),
  users, roles, security log.
- **v2** (`{baseApiUrl}/api/v2/...`) — activity, metadata and actions.
  `activityMonitor` (running), `activityLog` (completed), connections,
  mappings, task start/stop.

The client logs in with v3 by default and sends the session id under **both**
`INFA-SESSION-ID` and `icSessionId` headers, so a single session works across
both families.

**The ID rule that governs everything** (and the single most common source of
403/404 confusion when working with these APIs):

> v3 `objects`/`lookup` ids are **federated ids** and are *not* valid on v2
> endpoints. Every asset id passed to a v2 API must have come out of a v2
> response.

There are exactly two documented bridges across that boundary, and both are
exposed as tools:

- An activityLog entry's `parentTaskFederatedId` **is** a v3 object id →
  `lookup_taskflow_for_run` resolves it to the parent taskflow.
- v3 object references carry an `appContextId` that **is** valid on v2 →
  `get_asset_dependencies` surfaces it.

---

## Tool reference

42 tools, tiered by blast radius. Tier 2/3 tools return a **preview** and refuse
to act without `confirm=True` — with one exception, noted below, that is a known
defect rather than a design choice.

### Tier 1 — read-only

**Discovery (v3)**

| Tool | What it does |
|---|---|
| `search_objects(query, limit)` | Find assets by v3 filter (type, path, updateTime, tag). The entry point for "what is the user asking about?" — context only, yields no v2 ids |
| `lookup_taskflow_for_run(parent_task_federated_id)` | Resolve an activityLog entry's federated id to its **parent taskflow** path/name/type |
| `get_asset_dependencies(asset_id, ref_type, limit, skip)` | `Uses` = what an asset is made of; `usedBy` = everything built on it (e.g. every asset on a connection). References carry the v2-valid `appContextId` |

**Monitoring and history**

| Tool | What it does |
|---|---|
| `list_running_jobs()` | activityMonitor — currently running mapping tasks with name, run id, elapsed |
| `get_activity_log(run_id, row_limit, task_id)` | Completed-run history, newest first |
| `get_mapping_task_status(run_id)` | One MTT run in detail; FAILED runs are enriched with `error_summary` |
| `find_failed_runs(days_back, name_filter, source, limit)` | "What failed last Tuesday?" — reads the monitor's `failures.jsonl` ledger instantly, with live-API paging as fallback |
| `get_audit_log()` | Org audit entries (connection updates, logins, admin actions) |
| `get_security_log(query)` | v3 security log; requires an `entryTime` range |

**Org, server, metadata**

`get_org`, `get_server_time`, `get_v2_user_details`, `get_users`,
`get_runtime_environments`, `get_agent_details`, `get_tasks(task_type)`
(DMASK/DRS/DSS/MTT/PCS), `get_connections`, `get_connection(connection, by)`,
`get_connectors`, `get_mappings` (XML), `get_mapping(mapping_id)` (XML),
`get_custom_functions`, `get_schedules`.

**Connection data — inspect without executing anything**

| Tool | What it does |
|---|---|
| `preview_source_data(connection, object_name, by)` | Read sample rows from a source object |
| `preview_target_data(connection, object_name, by)` | Read sample rows from a target object — doubles as the **post-change smoke test** for a connection (one read-only call proves auth, no job execution) |
| `get_source_fields` / `get_target_fields` | Field names for an object |
| `validate_expression(expr, connection_id, object_name, is_source_type)` | Validate an IDMC expression against a real object |

Database connectors need the qualified `DB/SCHEMA/TABLE` object path.

**Ingestion and replication** — these jobs are invisible to `activityMonitor`,
which is why they get their own tools:

| Tool | What it does |
|---|---|
| `list_ingestion_tasks()` | File and database ingestion/replication tasks (`dbmir_*`) |
| `get_ingestion_task_log(task_id)` | Job history for one **file** ingestion task |
| `get_dbmi_job_status(job_id)` | Status of a **database/application** replication job |
| `get_dbmi_job_metrics(job_id, state_filter, ...)` | Per-object (table-level) drill-down that `get_dbmi_job_status` lacks |

**Taskflow status**

`get_taskflow_status(run_id)` — takes the large numeric taskflow runId (not the
activityMonitor runId) and returns RUNNING/SUSPENDED/FAILED plus asset name,
duration and `startedBy`.

### Tier 2 — write, confirm-gated

| Tool | Notes |
|---|---|
| `run_mapping_task(task_id, task_type)` | Task ids come from the activity log (`objectId`) or `get_tasks`. **⚠ Not currently confirm-gated** — it starts the task immediately. Open defect; see [open items](#roadmap) |
| `run_taskflow_by_name(api_name, confirm)` | Start a **new** run via the published Service URL — this is how a FAILED taskflow is re-run |
| `resume_taskflow(run_id, confirm)` | `resumeWithFaultRetry`. **Only works on SUSPENDED runs** — FAILED runs need a new run via `run_taskflow_by_name`. (The tool's own docstring still says "SUSPENDED or FAILED"; that text is stale — see [open items](#roadmap)) |
| `run_taskflow(taskflow_id, confirm)` | Start by internal numeric management id (refresh-job pattern) |
| `heal_taskflow(run_id, api_name, refresh_taskflow_id, confirm)` | One step: RUNNING → no action, SUSPENDED → resume, FAILED → new run |
| `update_connection(connection_id, updates, confirm)` | **Highest blast radius of the write tools** — every asset on the connection inherits the change immediately. Preview returns a current-vs-proposed diff per field |
| `update_connections_from_file(file_name, confirm, force)` | Apply a plan file **on the box**, so credentials never pass through chat |

`update_connections_from_file` is worth calling out. It exists because the
Snowflake password → key-pair migration needed to change credential material
across dozens of connections, and pasting key paths and passphrases into a chat
window is not an acceptable way to do that. The plan file sits next to the
server code; the tool applies each entry only if the connection's **actual name
matches `expect_name` exactly**, masks every credential-looking value in its own
output via a recursive `_mask_secrets`, skips `FILL_ME` placeholders, and
refuses an entry when:

- the proposed `privateKeyFile` filename doesn't contain the connection's
  Snowflake user (a key authenticates exactly one user — a mismatch silently
  breaks the connection), or
- the connection is already on KeyPair auth with a *different* key file (it was
  probably migrated deliberately).

Both guards were written after real mismatches, not anticipated in the abstract.
`force=true` overrides them.

### Tier 3 — destructive, guardrails mandatory

| Tool | Notes |
|---|---|
| `stop_running_job(task_id, task_type, confirm)` | Returns a preview unless `confirm=True` |
| `terminate_taskflows(run_ids, confirm)` | Up to 200 runIds per call. Preview resolves each run's current status and name before the kill. **Terminated runs cannot be resumed** — only restarted |

---

## Order of operation

The tools are not a flat bag of endpoints. They are sequenced, and both the
module docstring and the individual tool descriptions state the sequence so the
model follows it:

1. **`search_objects` (v3)** — resolve *what* the user is talking about.
   Context only; produces no ids usable on v2.
2. **Activity layer (v2)** — `list_running_jobs` / `get_activity_log`. This is
   where every run id and task id that other v2 tools need actually comes from.
3. **`lookup_taskflow_for_run` (v3)** — `parentTaskFederatedId` → parent
   taskflow name. Roughly 99% of published Service-URL API names are just the
   taskflow name, which feeds `run_taskflow_by_name`.
4. **Tier 2/3 tools** — confirm-gated, with ids and names from steps 2–3.

This matters because the alternative — letting a model guess which id goes to
which endpoint — produces a stream of 403s and 404s that look like permission
problems and aren't. Encoding the sequence into the tool surface turns an
undocumented API quirk into something the model gets right on the first attempt.

---

## Depth of analysis: what you can actually ask

The point of a 42-tool surface rather than a 3-tool one is that a single
question can be answered end-to-end, with the model chaining tools instead of
handing back a partial answer and a suggestion to check the UI.

**Failure triage, root cause included**

> *"What failed overnight and why?"*

`find_failed_runs` reads the ledger (instant, no API paging) → each failure
already carries a rule-based `error_summary` with a plain-English summary, a
speculative likely cause, and the raw first line → `lookup_taskflow_for_run`
names the parent taskflow → `run_taskflow_by_name` restarts it after you
confirm. One question, from symptom to restart.

**Blast-radius analysis before a change**

> *"If I change this Snowflake connection, what breaks?"*

`get_connection` → `get_asset_dependencies(usedBy)` enumerates every asset built
on it → `update_connection` in preview mode returns a field-level
current-vs-proposed diff → after the change, `preview_target_data` proves auth
still works with a single read and no job execution.

**Lineage in both directions**

`Uses` walks downward (what is this asset made of); `usedBy` walks upward
(everything that depends on it). Combined with `search_objects` by type and
`get_mapping` XML, you can answer structural questions about the org that the
UI makes you click through one asset at a time.

**Cross-surface visibility**

Mapping tasks, taskflows, and database/file ingestion jobs live in three
different places in IDMC with three different id schemes and three different
status APIs. `list_running_jobs`, `get_taskflow_status`, and
`get_dbmi_job_status` / `get_dbmi_job_metrics` cover all three, so "what is
running right now" has one answer rather than three partial ones.

**Historical questions the platform can't answer**

IDMC's activity log has finite retention and paginates awkwardly. The monitor's
`failures.jsonl` ledger is an append-only local record of every failure the
monitor ever saw, in compact form. `find_failed_runs` reads it first and only
falls back to the live API for pre-ledger history — so *"is this the third time
this week?"* is answerable, which is the question that separates a recurring
defect from a transient one.

**Error summarization**

`ERROR_RULES` in `idmc_client.py` is an ordered substring→(summary, likely
cause) table, mined from real production failures rather than invented. It
covers Snowflake SQL compilation and `COPY INTO` failures, MERGE duplicate-key
errors, type-coercion rejections, SQL Server deadlock victims, DTM process
crashes on the Secure Agent, dropped PostgreSQL reads, DNS resolution failures,
IICS internal 500s, overlapping-run skips, and manual stops. Anything unmatched
falls back to the trimmed first line, labeled uncategorized — it never guesses.
It is deterministic and makes no external calls, so the monitor's output is
reproducible and free.

Note the first rule: *"was stopped by user"* is classified as **not an error**.
A meaningful share of "failures" in any orchestration platform are people
pressing stop, and a triage tool that can't say so wastes everyone's morning.

---

## The monitor

`monitor.py` is a single-pass, read-only script. It runs, does one window of
work, posts if warranted, writes its state, and exits. `systemd` handles the
schedule.

**Why it exists at all:** Informatica's native alerting is per-taskflow failure
email. That is fine for one inbox and useless for a team channel — and it has no
memory, so it cannot answer *"has this happened before?"*.

**Why it can't be an MCP tool:** Claude Desktop launches an MCP server only for
the duration of a chat session, and never calls tools on its own. There is no
background loop in the MCP model. Anything that must happen when nobody is
looking has to live outside it. This is the architectural fork the whole project
turns on, and it is why there are two processes rather than one.

### What a pass does

1. Log in; pull `activityMonitor` for running mapping tasks.
2. Pull `activityLog` **paginated** (`rowLimit` + `offset`, newest first, 100 per
   page, capped at 20 pages) back to the window start.
3. Post to Slack **only when something needs attention**:
   - FAILED runs in the window — one `:rotating_light:` message with, per
     failure: start→end in Central time, rows ok/failed, the simplified error
     and speculative cause, the raw first line, and the resolved parent taskflow.
   - Running jobs at or past `CYCLE_HOURS × LONG_RUN_CYCLES` (4h) — a
     `:warning:` long-run list.
   - A healthy pass posts **nothing**.
4. Append every new failure to `failures.jsonl` (compact JSON, deduped on the
   last 500 `task:run` keys), which is what `find_failed_runs` reads.
5. Post a daily heartbeat on the first pass at or after `HEARTBEAT_HOUR` Central
   with counts since the last heartbeat.
6. Persist `last_run_utc`, heartbeat counters, and dedup keys — atomically, via
   write-to-`.tmp`-then-`os.replace`.

All displayed times are Central; all internal math is UTC.

### Three details that are the whole design

**The window is anchored to the last successful pass, not to the clock.**
`window_start = state["last_run_utc"] or (now - cycle_hours)`. If a timer fire is
late, or the box reboots, or a pass crashes, the next pass covers the gap. No
missed failures, no duplicate alerts. The systemd timer carries `Persistent=true`
for the same reason — a missed fire runs on boot.

This replaced a fixed newest-N activity-log pull that **silently missed
failures** once production volume exceeded one page. That incident is the reason
for both the pagination and the state file, and it is the single most valuable
thing in the repository's history: a monitor that quietly under-reports is worse
than no monitor, because it manufactures false confidence.

**Silence has to mean something.** Two mechanisms make it provable:

- A **daily heartbeat** — proof of life, so "no Slack messages" means "healthy",
  not "the monitor died three weeks ago".
- A **failsafe** — `main()` wraps the pass, and a crash posts
  `:rotating_light: IDMC monitor pass FAILED` with the exception and the
  `journalctl` command to run, then exits non-zero so systemd records it too.
  A Slack post that itself fails is logged and never aborts the pass.

**Channel routing keeps signal pure.** `SLACK_WEBHOOK_URL` (ops/personal) gets
everything — alerts, heartbeat, failsafe. `SLACK_WEBHOOK_URL_TEAM` gets *only*
failure and long-run alerts, so the team channel never accumulates
housekeeping that trains people to ignore it. `--test` posts to both, so a newly
provisioned team webhook can be verified before it goes live.

### Operating it

```bash
python3 monitor.py                              # one pass now
python3 monitor.py --test                       # verify webhook wiring (both channels)
sudo systemctl start infra-monitor.service      # one pass, also resets the 2h clock
journalctl -u infra-monitor.service -n 100      # what happened
```

The systemd unit is deliberately boring and constrained: `Type=oneshot`,
unprivileged `User=`, `MemoryMax=256M`, `CPUQuota=25%`, `NoNewPrivileges=true`.
A monitoring process should not be able to destabilize the host it monitors from.

---

## Why an EC2 Linux box

The host is an AWS EC2 Linux instance that already existed (stood up to trial
Informatica's since-discontinued AI-agent tool, otherwise idle). Putting this
project on it was a deliberate choice, not a convenience:

**Something has to be always on.** A 2-hour monitor cannot live on a laptop.
Laptops sleep, close, travel, and lose VPN. The moment the alerting is only as
reliable as someone's lid being open, it isn't alerting.

**MCP stdio needs a host the client can reach.** Claude Desktop's config
launches **stdio servers only**; its remote-HTTP connectors run from Anthropic's
cloud and would require a public endpoint plus OAuth. A private box reached by
`ssh -T` gives stdio transport with **no public exposure and no token to leak** —
the SSH session *is* the transport. The trade-off is an explicit one: VPN must be
up.

**It makes the box a shared surface instead of a personal one.** Each teammate
gets a Linux account and their own SSH key; the shared read-only code lives in
`/opt/informatica-mcp`; the team credential is one root-owned, `chmod 640`,
`mcpteam`-group file. Onboarding is one `usermod`. Offboarding is deleting an
account. Credential rotation is editing one file. None of that is possible if
the server runs on each person's laptop.

**Identity and audit come for free.** Because people SSH in as themselves, the
box's SSH logs record who ran what session, even though the IICS credential is
shared. Config is also written to load `~/.informatica-mcp.env` *before* the
shared `.env`, so any user who wants to run under their own IICS identity — and
get real per-user attribution in IICS — just drops a chmod-600 file in their home
directory. The multi-user model degrades gracefully in both directions.

**AWS primitives are right there.** An instance role means secrets can move to
Secrets Manager (`IDMC_SECRET_NAME`) with no plaintext password on disk and no
code change — `config.py` already supports it. Egress is controlled at the
security group. Nothing about that is available on a laptop.

Worth being precise about one thing: the box happens to run a Secure Agent, and
that is **incidental**. Every tool here calls the IDMC cloud REST API. The box
buys always-on, IAM, egress and a shared identity surface — not agent
proximity.

---

## The monitor as another worker

The framing that makes this architecture click: **the monitor is a teammate who
works the shift nobody else does.**

An MCP tool is a *pull* resource. It exists only while a human is in a chat
session, it only acts when asked, and it produces answers into a conversation
that ends. That is exactly right for investigation, and exactly wrong for
vigilance.

`monitor.py` is the *push* half. It has its own schedule, its own identity, its
own channel, and its own definition of doing the job properly:

- **It shows up on time** — every two hours, `Persistent=true` so a missed shift
  is made up on boot.
- **It doesn't lose its place** — the state file is its handover note. It knows
  where the last pass stopped and picks up exactly there, so nothing falls
  between shifts.
- **It only speaks when it has something to say** — healthy passes are silent.
- **It checks in daily** so you know it's alive, and it **says so loudly when it
  falls over**, rather than failing quietly.
- **It keeps records** — `failures.jsonl` is the notebook it hands back to the
  interactive side.

That last point is the important one, and it is why this is more than a metaphor.
The two processes are not just co-located, they **compose**: the monitor writes
the ledger unattended for weeks, and `find_failed_runs` reads it the moment a
human asks a question. The unattended worker accumulates the institutional
memory that the interactive worker draws on. Ask *"what failed last Tuesday?"*
and you get an instant answer from a file that a background process wrote at
3am, not a slow crawl through a paginated API that may no longer retain the data.

The shared `idmc_client.py` is what makes that composition cheap. Both workers
speak to IDMC through the same client, summarize errors with the same
`ERROR_RULES`, and parse timestamps with the same `parse_start_time`. A new error
rule improves the 3am Slack alert and the 9am chat answer in the same commit.

---

## Design principles

**1. One client, two consumers.** Login, session caching, 401 re-login, host
routing, XML-vs-JSON handling, timestamp parsing, and error summarization live in
`idmc_client.py` and nowhere else. Divergence between what the monitor reports
and what the chat session reports is structurally impossible.

**2. Curate tools; don't wrap endpoints.** The tool surface is chosen for the
questions people actually ask, with the order of operation baked into the
descriptions. Tools that turned out not to earn their place were removed —
`list_long_running_jobs` was deleted because the monitor already flags
long-runners, and a tool that duplicates a push signal just gives the model a
worse way to get the same answer.

**3. Preview by default; act only on explicit confirm.** A Tier 2/3 tool returns
a description of what it *would* change unless `confirm=True`, so nothing
production-affecting fires as a side effect of the model exploring. The preview
is the safety mechanism *and* the documentation of intent. The principle is only
worth stating if it is auditable against the code, so: `run_mapping_task` is
currently the one gap, and it is listed as an open defect rather than quietly
excepted.

**4. Guards encode what went wrong last time.** The key-file/user match check,
the already-migrated-with-a-different-key refusal, the `expect_name` assertion,
the conflicting-`role=` refusal — each was added after a real mismatch, and each
carries a dated comment saying so. `force=true` exists for the case where the
operator genuinely knows better.

**5. Secrets never traverse the chat.** Credential changes go through a plan file
that lives on the box; the tool reads it server-side and masks every
credential-looking value in its output with a recursive walk. The chat transcript
sees a diff, never a secret.

**6. Silence must be provable.** Alert-only output is only trustworthy alongside
a heartbeat and a failsafe. All three ship together or none of them mean
anything.

**7. State over assumptions.** The window is anchored to the last successful
pass, not to wall-clock arithmetic. Written atomically. Survives reboots, late
timers, and crashed passes.

**8. UTC internally, Central for humans.** One conversion point, at the display
boundary. The `startTimeUtc`-not-`startTime` trap — plain `startTime` carries a
misleading `Z` while actually being local time — is documented at every place
the field is touched, because it is exactly the kind of bug that produces
plausible-looking wrong numbers for months.

**9. Pin what will break.** `mcp[cli]<2` with a comment explaining that 2.x
renamed `FastMCP`, that a fresh install pulls 2.x, and the date it bit the shared
`/opt` install (2026-09-08). A pin without a reason gets removed by the next
person; a pin with a dated incident doesn't. (The pin only protects installs that
go through `requirements.txt` — the `/opt` procedure in `TEAM_SETUP.md` still
pip-installs unpinned and should be changed to use the requirements file.)

**10. Document decisions, not just behavior.** `informatica-mcp-design.md`
carries a resolved/open decision log. `TEAM_SETUP.md` is written for the
teammate, with an admin appendix for the operator. The code comments explain
*why*, and the docstrings — which the model reads as its instructions — explain
*when*.

---

## Security and governance

- **Nothing sensitive is committed.** `.gitignore` covers env files, key material
  (`*.p8`, `*.pem`, `*.key`, `*.pkcs8`, SSH keys), every migration plan file
  pattern, connection inventories with real ids and JDBC params, and the monitor's
  runtime state and ledger.
- **Secrets live in AWS Secrets Manager or a chmod-600/640 file**, never in code.
  `IDMC_SECRET_NAME` switches to Secrets Manager via the instance role with no
  code change.
- **Least-privilege service account**, scoped to the org and APIs the tools need.
- **Confirm gates** on every write and destructive tool; audit-log tools exist so
  writes can be reviewed after the fact.
- **The deploy rsync excludes** `.env`, plan files, inventories, ledger and state
  from the world-readable `/opt` copy — the shared code is shared, the secrets
  are not.
- **Known limitation, stated plainly:** with a shared team credential, IICS
  `startedBy`/`updatedBy` shows the team account for everyone. The record of
  *who* lives in SSH logs, and `TEAM_SETUP.md` asks people to announce prod
  writes in the team channel. Per-user IICS identity is available to anyone who
  wants it via `~/.informatica-mcp.env`.
- **Per-tool authorization does not exist in this model** — everyone sees the
  same confirm-gated tool set and IICS enforces per-user rights. Closing that
  boundary is Phase 5 (Auth0, per-tool access control) under the SPS MCP
  guardrails.

---

## Repository layout

```
informatica-mcp/
├── idmc_client.py                 shared REST client: logins, routing, retries,
│                                  ERROR_RULES, summarize_error, compact_failure
├── mcp_server.py                  42-tool FastMCP stdio server
├── monitor.py                     single-pass Slack monitor (systemd timer)
├── config.py                      env/Secrets-Manager config with layered loading
├── test_monitor.py                unit tests — no live tenant required
├── verify_apis.py                 read-only smoke test of every endpoint
├── generate_role_plan.py          build chunked role-append plan files
├── probe_dbmi_host.py             discover which host serves the DBMI status API
├── setup.sh                       idempotent dev setup / test runner
├── requirements.txt               mcp[cli]<2, httpx, boto3
├── .gitignore                     secrets, key material, plan files, runtime state
├── deploy/
│   ├── infra-monitor.service      oneshot unit, resource-capped, NoNewPrivileges
│   └── infra-monitor.timer        every 2h, Persistent=true
├── demo/                          architecture and flow diagrams (SVG)
├── informatica-mcp-design.md      design doc + decision log
├── TEAM_SETUP.md                  teammate onboarding + admin appendix
├── start-up-command.rtf           scratch note: the SSH bridge command
└── DEPRECATE_send_test_email.py   dead code from the retired email-alert design
```

Two SVGs (`snowflake-connection-update-flow.svg`,
`snowflake-connection-update-simple.svg`) exist at both the repo root and in
`demo/`; the root copies are duplicates and can go.

Runtime files that are *not* in git: `.env`, `monitor_state.json`,
`failures.jsonl`, `audit.log`, plan files (`connection_key_migration.json`,
`role_migration_*.json`), connection inventories.

---

## Testing and verification

**Unit tests — no live tenant needed.** `test_monitor.py` covers the behavior
that is easy to get wrong and expensive to get wrong in production:

- a healthy pass posts nothing
- a failure posts an alert carrying the summary and the parent taskflow
- a failure outside the window is ignored
- long-runner flagged / short-runner not flagged
- **a failure beyond the first activity-log page is still found** (the
  regression test for the incident that drove the redesign)
- the window starts at the last recorded pass
- the ledger records compactly and dedupes across re-runs
- alerts route to the team channel; heartbeats stay off it
- the heartbeat posts once daily and counts accumulate until it does
- `ERROR_RULES` match the prod-mined patterns; unmatched errors fall back
- `run_taskflow_smart` resolves suffixed names and stops immediately on 403

```bash
python -m unittest test_monitor -v
```

**Live smoke test.** `verify_apis.py` calls every read-only endpoint once against
the real tenant and prints OK/FAIL/SKIP per endpoint, exiting non-zero if
anything failed. Endpoints needing a specific id are skipped unless you pass the
matching argument:

```bash
python3 verify_apis.py
python3 verify_apis.py --mapping-id <v2_mapping_id> --conn <id_or_name> --object <objectName> --by id
```

It never runs or stops a job.

---

## Roadmap and open items

| Phase | Status | Work |
|---|---|---|
| 1 | ✅ done | Slack monitor — 2-hour systemd timer, alert-only, ledger, heartbeat, failsafe |
| 2 | ✅ done | Tool refinement. Order of operation **is** encoded in the module docstring and individual tool descriptions;
| 3 | ✅ done | Production move — prod credential, per-taskflow Allowed Users/Groups in IICS, SPS-approved Slack webhook, secrets moved to AWS Secrets Manager |
| 4 | planned | Hardening — monitor dead-man's-switch, retire the stock connector, extend `ERROR_RULES` from new failure shapes |
| 5 | planned | Productionize per SPS MCP guardrails — Streamable HTTP transport, Auth0 authorization with per-tool access control, kebab-case `sps-*` tool naming with explicit annotations, containerized deploy to Atlas EKS with a Tech Registry Service ID, Engineering Enablement Review. Monitor placement (EKS CronJob vs. EC2) decided then |

### Open items

- **Slack webhook** still needs SPS Slack-admin approval/creation; until then the
  monitor runs log-only.
- **Prod taskflow permissions** — the MCP account must be added to each
  taskflow's Allowed Users/Groups in IICS before it can start or terminate them.
- **`run_mapping_task` is not confirm-gated.** Every other write/destructive tool
  is. Defect, not a design choice.
- **`resume_taskflow`'s docstring says "SUSPENDED or FAILED"** — resume only
  works on SUSPENDED runs. Stale text the model reads as instructions; fix it.
- **`.env.example` is missing**, so a clean `./setup.sh` first run aborts.
- **`TEAM_SETUP.md`'s `/opt` install pip-installs unpinned**, bypassing the
  `mcp[cli]<2` pin that exists precisely because that install broke once.
- **`deploy/infra-monitor.service` hardcodes a user and home path** — it needs an
  edit (or `%h`/an `EnvironmentFile`) before it is portable to another account.
- **Taskflow runIds for `get_taskflow_status`** still come from the UI; no API
  path found yet.
- **The stock `job-management` connector** is still installed alongside this one,
  pending cutover (duplicate tool names until then).
- **`DEPRECATE_send_test_email.py`** is dead code from the retired email-alert
  design and should be deleted.

---

## What this project demonstrates

A deliberate summary of the engineering judgment on display here, for anyone
evaluating this repository as work product.

**Systems design under a hard platform constraint.** The central architectural
decision — two processes sharing one library — follows from a real constraint
that had to be discovered rather than looked up: MCP servers are session-scoped
and have no background loop, so anything unattended must live outside the
protocol. Recognizing that early, rather than fighting it with an increasingly
elaborate MCP tool, is the difference between this design and a broken one.

**Reverse-engineering an under-documented API surface.** Two API families with
incompatible id spaces, two login endpoints with interchangeable sessions, a
host-routing quirk where one API family drops the `/saas` prefix, a timestamp
field whose obvious name is silently wrong, XML responses in an otherwise JSON
API, and three separate job-status surfaces for three job types. All of it
mapped, all of it documented in the code, and — critically — the two id bridges
across the v2/v3 boundary found and exposed as first-class tools.

**Designing for a non-deterministic caller.** Tool descriptions are the
interface contract when the client is a language model. The order of operation
appears in the module docstring *and* in individual tool docstrings; ids are
labeled with where they legally come from; write tools state their blast radius
in the text the model reads. This is API design for a consumer that will
improvise, and it is a genuinely different discipline from designing for a
programmer who reads documentation once.

**Safety engineering that is proportionate, not performative.** Three tiers,
preview-by-default, confirm-gated writes, a highest-blast-radius tool explicitly
labeled as such, credential material routed around the chat entirely, and
recursive secret masking on output. The guardrails are specific to what can
actually go wrong, and each one names the incident it came from — and where the
implementation falls short of the stated principle (`run_mapping_task`), the
README says so rather than rounding up.

**Production operational thinking.** Window anchoring to survive missed timers.
Atomic state writes. Pagination because a fixed pull was proven to under-report.
Alert-only output *plus* a heartbeat *plus* a failsafe, because each is useless
without the others. Resource caps and `NoNewPrivileges` on the unit. A pinned
dependency with a dated incident in the comment. These are the habits of someone
who has been paged.

**Learning loops encoded into the artifact.** `ERROR_RULES` is mined from a real
failure ledger, not imagined. The plan-file guards were written after real
mismatches. The pagination was written after a real miss. The design doc carries
a resolved/open decision log. The system gets measurably better every time
production teaches it something, and the mechanism for that is built in.

**Multi-user operations, including the honest parts.** A shared-credential model
with a documented attribution limitation, a per-user override path, group-based
file permissions, an rsync deploy that excludes exactly the right things, and
one-command onboarding and offboarding. The documentation states the weakness
plainly and points at the phase that fixes it, rather than hiding it.

**Documentation as a deliverable.** Three audiences, three documents:
`TEAM_SETUP.md` for the teammate who wants tools working in ten minutes,
`informatica-mcp-design.md` for the engineer who needs to know why, and this
README for everyone else. Plus a troubleshooting table that maps real symptoms to
real causes, which only exists because those symptoms actually happened to
somebody.

---

**Author:** Joe Peck · [jpeckjr/informatica-mcp](https://github.com/jpeckjr/informatica-mcp)
