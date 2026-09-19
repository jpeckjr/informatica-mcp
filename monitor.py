"""
monitor.py — failure-focused IDMC Slack monitor (2-hour passes).

WHY THIS EXISTS: Informatica's own alerting is per-taskflow failure emails —
fine for one inbox, useless for a team channel. This monitor makes failures
VISIBLE in Slack and queryable long after the fact.

Each pass (systemd timer, every 2 hours):
  1. Pull activityMonitor (running MTTs) and the activityLog PAGINATED back
     to the window start — prod volume can exceed one page, and a fixed
     newest-N pull silently misses failures (this happened; see design doc).
  2. Post to Slack ONLY when something needs attention:
       - FAILED runs in the window: simplified error + speculative cause +
         parent taskflow (resolved via v3 lookup when available)
       - running jobs past 2 cycles (2 x 2h = 4h): long-running flag
     Healthy passes post nothing.
  3. Append every failure to a local LEDGER (failures.jsonl) so failures can
     be queried days/weeks later via the find_failed_runs MCP tool without
     paging the huge activity log.
  4. Post a small daily HEARTBEAT (first pass at/after HEARTBEAT_HOUR CT,
     default 8am) with counts since the last heartbeat — proof of life, so
     Slack silence means "healthy", not "monitor dead".
  5. FAILSAFE: a crashed pass posts a :rotating_light: failure alert to Slack
     and exits non-zero — a broken monitor is never silent.

All displayed times are Central (America/Chicago); internals are UTC.

Run:  python3 monitor.py          (one pass; exit 0 on success)
      python3 monitor.py --test   (send a test Slack message and exit)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from config import Config
from idmc_client import (IDMCClient, compact_failure, elapsed_hours,
                         parse_start_time)

log = logging.getLogger("monitor")

# activityLog `state` codes (final status of a mapping-task run).
STATE_FAILED = 3

# All Slack-displayed times are Central (America/Chicago handles CST/CDT
# automatically). Internal window math stays in UTC.
CENTRAL = ZoneInfo("America/Chicago")

PAGE_SIZE = 100        # activityLog page size for the window fetch
MAX_PAGES = 20         # safety cap (20 x 100 = 2000 runs per window)
LEDGER_DEDUP_KEYS = 500  # remembered task:run keys to avoid double-ledgering


def ct(dt: datetime) -> datetime:
    return dt.astimezone(CENTRAL)


def fmt_ct(dt, fmt: str = "%I:%M %p") -> str:
    return ct(dt).strftime(fmt).lstrip("0") if dt else "?"


# ── field extraction ───────────────────────────────────────────────────────
# activityMonitor: name lives in `taskName` (objectName is empty), and the
# true UTC start is `startTimeUtc` — plain `startTime` carries a misleading
# `Z` and is local time, hours off. Never use it.
# activityLog: name lives in `objectName`; end time is `endTimeUtc`.
def job_name(job: dict) -> str:
    return (job.get("taskName") or job.get("objectName")
            or job.get("name") or "<unknown>")


def job_start_utc(job: dict):
    return parse_start_time(job.get("startTimeUtc") or job.get("startTime"))


def job_end_utc(job: dict):
    return parse_start_time(job.get("endTimeUtc") or job.get("endTime"))


# ── state (window bookkeeping) ─────────────────────────────────────────────
def load_state(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


# ── Slack ──────────────────────────────────────────────────────────────────
# Routing: primary webhook gets everything (alerts, heartbeat, failsafe);
# the team webhook (#team-data-platform) gets ONLY failure/long-run alerts
# so the team channel stays pure signal.
async def post_to_slack(cfg: Config, text: str,
                        include_team: bool = False) -> None:
    """Post via the incoming webhook(s); log-only when none configured."""
    urls = [u for u in [cfg.slack_webhook_url,
                        cfg.slack_webhook_url_team if include_team else ""]
            if u]
    if not urls:
        log.info("[slack disabled — message follows]\n%s", text)
        return
    async with httpx.AsyncClient(timeout=15) as http:
        for url in urls:
            try:
                r = await http.post(url, json={"text": text})
                if r.status_code >= 400:
                    # Never let a Slack failure abort the pass; log, move on.
                    log.error("Slack post failed (%s): %s",
                              r.status_code, r.text[:200])
                else:
                    log.info("Posted to Slack (%d chars)", len(text))
            except Exception as e:                 # noqa: BLE001
                log.error("Slack post failed for one webhook: %s", e)


# ── data gathering ─────────────────────────────────────────────────────────
async def fetch_completed_window(client, window_start: datetime,
                                 now: datetime) -> list[dict]:
    """Page through the activity log (newest first) until the whole window
    is covered. A single newest-N pull is NOT enough on a busy org — entries
    beyond page one silently vanish, and failures with them."""
    out: list[dict] = []
    offset = 0
    for _ in range(MAX_PAGES):
        page = await client.get_activity_log(row_limit=PAGE_SIZE, offset=offset)
        entries = page if isinstance(page, list) else page.get("entries", [])
        if not entries:
            break
        reached_window_start = False
        for e in entries:
            end = job_end_utc(e)
            if end is None:
                continue
            if end < window_start:
                reached_window_start = True
            elif end <= now:
                out.append(e)
        if reached_window_start:
            break
        offset += PAGE_SIZE
    out.sort(key=lambda e: job_end_utc(e) or now, reverse=True)
    return out


async def resolve_parent_taskflows(client, failures: list[dict]) -> dict:
    """Map parentTaskFederatedId -> taskflow path via v3 lookup. Best-effort:
    lookup failures never block the alert."""
    paths: dict[str, str] = {}
    ids = {f.get("parent_task_federated_id") for f in failures
           if f.get("parent_task_federated_id")}
    for pid in ids:
        try:
            r = await client.lookup_objects(object_ids=[pid])
            objs = (r or {}).get("objects") or []
            if objs:
                paths[pid] = objs[0].get("path") or ""
        except Exception as e:                     # noqa: BLE001
            log.warning("Parent taskflow lookup failed for %s: %s", pid, e)
    return paths


# ── failure ledger ─────────────────────────────────────────────────────────
def ledger_append(cfg: Config, records: list[dict], state: dict) -> list[dict]:
    """Append compact failure records to failures.jsonl, deduped across
    passes (a crash-retried window may re-see the same run). Returns the
    records that were actually new."""
    seen = state.get("ledger_keys", [])
    new: list[dict] = []
    for rec in records:
        key = f"{rec['task_id']}:{rec['run_id']}"
        if key in seen:
            continue
        seen.append(key)
        new.append(rec)
    if new:
        os.makedirs(os.path.dirname(cfg.ledger_file) or ".", exist_ok=True)
        with open(cfg.ledger_file, "a") as f:
            for rec in new:
                f.write(json.dumps(rec) + "\n")
    state["ledger_keys"] = seen[-LEDGER_DEDUP_KEYS:]
    return new


# ── message building ───────────────────────────────────────────────────────
def build_alert(failures: list[dict], parent_paths: dict,
                long_runners: list[dict], long_run_hours: float,
                window_start: datetime, now: datetime) -> str:
    parts = []
    if failures:
        parts.append(f":rotating_light: *IDMC FAILURES — {len(failures)} in "
                     f"window {fmt_ct(window_start)} → {fmt_ct(now)} CT "
                     f"({ct(now).strftime('%Y-%m-%d')})*")
        for f in failures:
            start = parse_start_time(f.get("started_utc"))
            end = parse_start_time(f.get("ended_utc"))
            tf = parent_paths.get(f.get("parent_task_federated_id") or "", "")
            line = (f"• :x: `{f['name']}` — {fmt_ct(start)} → {fmt_ct(end)} CT"
                    f" | rows ok/failed: {f.get('success_rows')}/"
                    f"{f.get('failed_rows')}"
                    + (f" | taskflow: `{tf}`" if tf else ""))
            err = f.get("error_summary")
            if err:
                line += (f"\n    ↳ *{err['summary']}*"
                         f"\n    ↳ Why (speculative): {err['likely_cause']}"
                         f"\n    ↳ raw: {err['raw_first_line']}")
            parts.append(line)
    if long_runners:
        parts.append(f":warning: *Long-running (over {long_run_hours:g}h, "
                     f"2+ cycles):*")
        for j in long_runners:
            start = job_start_utc(j)
            hrs = elapsed_hours(start, now) if start else 0
            parts.append(f"• `{job_name(j)}` — started "
                         f"{fmt_ct(start, '%Y-%m-%d %I:%M %p')} CT "
                         f"({hrs:.1f}h ago)")
    return "\n".join(parts)


def build_heartbeat(hb: dict, running_count: int, now: datetime) -> str:
    return (f":white_check_mark: *IDMC monitor heartbeat — "
            f"{ct(now).strftime('%Y-%m-%d %I:%M %p').lstrip('0')} CT*\n"
            f"Since last heartbeat: {hb.get('completed', 0)} runs completed, "
            f"{hb.get('failed', 0)} failed. Currently running: "
            f"{running_count}. (No news between heartbeats = no failures.)")


# ── core ───────────────────────────────────────────────────────────────────
async def run_once(cfg: Config) -> None:
    state = load_state(cfg.state_file)
    now = datetime.now(timezone.utc)

    # Window start = last successful pass (no gaps/overlaps even if a timer
    # fire is late or missed); first run falls back to one cycle back.
    last = parse_start_time(state.get("last_run_utc"))
    window_start = last or (now - timedelta(hours=cfg.cycle_hours))
    long_run_hours = cfg.cycle_hours * cfg.long_run_cycles

    # 120s timeout: activity-log pages are heavy and blew 30s in production.
    async with IDMCClient(cfg.login_url, cfg.username, cfg.password,
                          timeout=120.0) as client:
        running = await client.list_running_jobs()
        completed = await fetch_completed_window(client, window_start, now)
        failures = [compact_failure(e) for e in completed
                    if e.get("state") == STATE_FAILED]
        parent_paths = await resolve_parent_taskflows(client, failures)
        for f in failures:
            f["parent_taskflow"] = parent_paths.get(
                f.get("parent_task_federated_id") or "", "")

    long_runners = [j for j in running
                    if (s := job_start_utc(j)) is not None
                    and elapsed_hours(s, now) >= long_run_hours]

    log.info("Window %s→%s: %d completed, %d failed, %d running (%d long)",
             window_start.isoformat(), now.isoformat(), len(completed),
             len(failures), len(running), len(long_runners))

    new_failures = ledger_append(cfg, failures, state)

    # ALERT: only when something needs attention. (Alert on all window
    # failures, not just newly-ledgered ones — a re-covered window after a
    # crash should still alert; the ledger dedup is for the file only.)
    if failures or long_runners:
        await post_to_slack(cfg, build_alert(
            failures, parent_paths, long_runners, long_run_hours,
            window_start, now), include_team=True)

    # HEARTBEAT: counts accumulate every pass; post on the first pass at or
    # after heartbeat_hour CT each day, then reset.
    hb = state.get("heartbeat", {})
    hb["completed"] = hb.get("completed", 0) + len(completed)
    hb["failed"] = hb.get("failed", 0) + len(failures)
    today_ct = ct(now).date().isoformat()
    if hb.get("last_date") != today_ct and ct(now).hour >= cfg.heartbeat_hour:
        await post_to_slack(cfg, build_heartbeat(hb, len(running), now))
        hb = {"last_date": today_ct, "completed": 0, "failed": 0}
    state["heartbeat"] = hb

    state["last_run_utc"] = now.isoformat()
    save_state(cfg.state_file, state)
    if new_failures:
        log.info("Ledgered %d new failure(s) to %s",
                 len(new_failures), cfg.ledger_file)


async def send_test(cfg: Config) -> None:
    # Tests BOTH webhooks so a new team-channel webhook can be verified.
    await post_to_slack(cfg, (f":wave: IDMC monitor test — "
                              f"{datetime.now(timezone.utc).isoformat()}. "
                              f"Webhook wiring works."), include_team=True)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = Config.load()
    if "--test" in sys.argv:
        asyncio.run(send_test(cfg))
        return
    try:
        asyncio.run(run_once(cfg))
    except Exception as e:
        # FAILSAFE: a crashed pass must never be silent — post the failure
        # to Slack, then exit non-zero so systemd records the failure too.
        log.exception("Monitor pass FAILED")
        err = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        try:
            asyncio.run(post_to_slack(
                cfg, f":rotating_light: *IDMC monitor pass FAILED* — {err}\n"
                     f"Check `journalctl -u infra-monitor.service` on the box."))
        except Exception:
            log.error("Also failed to post the failure alert to Slack")
        sys.exit(1)


if __name__ == "__main__":
    main()
