"""
weekly_digest.py — weekly IDMC failure summary for a dedicated Slack channel.

WHY THIS EXISTS: the 2-hour monitor answers "what broke just now?". This
answers "what keeps breaking?" — top failing jobs, causes, new breakage, and
the week-over-week trend — once a week, Monday morning Central.

SOURCE: the monitor's failure ledger (failures.jsonl) plus the per-week run
counts the monitor keeps in monitor_state.json. It makes NO IICS calls: the
ledger already holds compact, deduplicated, cause-summarized failures, so
there is no reason to page the multi-MB activity log again.

WEEKS: fixed calendar weeks, Mon 00:00 → next Mon 00:00 Central. Every
failure lands in exactly one week, so weeks are comparable and nothing is
double-counted. Built from dates (see monitor.week_start_ct), so DST weeks
are 167h / 169h as they should be.

SCHEDULE: the timer fires DAILY at 08:00 CT. The script posts only when the
most recently completed week hasn't been posted yet — so it posts Monday,
and if Monday's post failed (Slack down, box off), Tuesday catches up. One
post per week; a missed Monday is retried rather than lost.

FAILSAFE: any failure (missing ledger, missing webhook, failed post) alerts
the PRIMARY monitor webhook and exits non-zero. A weekly post that silently
doesn't appear is indistinguishable from a quiet week — so it never does.

Run:  python3 weekly_digest.py            post if this week's digest is due
      python3 weekly_digest.py --dry-run  print the message; post and record
                                          nothing (safe to run any time)
      python3 weekly_digest.py --force    re-post the last completed week
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from config import Config
from idmc_client import parse_start_time
from monitor import (configure_logging, load_state, post_to_webhooks,
                     save_state, week_start_ct)

log = logging.getLogger("weekly_digest")

TOP_N = 10               # jobs / new-job names shown before "+N more"
CAUSE_MAX_CHARS = 80     # ERROR_RULES summaries are full sentences; trim


class DigestError(Exception):
    """A condition that must page the owner rather than post a wrong digest."""


def default_state_path() -> str:
    # Next to this module, not the cwd — same reasoning as the ledger path.
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "weekly_digest_state.json")


# ── week math ──────────────────────────────────────────────────────────────
def last_completed_week(now: datetime) -> tuple[datetime, datetime]:
    """[start, end) of the most recent FULLY completed Mon→Mon CT week."""
    end = week_start_ct(now)                       # this week's Monday 00:00
    start = week_start_ct(end - timedelta(days=1))  # the Monday before it
    return start, end


def week_label(start: datetime, end: datetime) -> str:
    last_day = end - timedelta(days=1)             # the Sunday
    return f"{start:%b} {start.day} – {last_day:%b} {last_day.day}"


# ── ledger ─────────────────────────────────────────────────────────────────
def read_ledger(path: str) -> list[dict]:
    """All ledger records, deduped on task:run (the monitor dedups too, but
    its key memory is bounded; a re-covered window could repeat a run)."""
    if not path or not os.path.exists(path):
        # NOT an empty week: a missing ledger means the setup is broken, and
        # reporting "0 failures" from it would be a false all-clear.
        raise DigestError(f"Failure ledger not found at {path!r}")
    out, seen = [], set()
    with open(path) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = f"{rec.get('task_id')}:{rec.get('run_id')}"
            if key in seen:
                continue
            seen.add(key)
            out.append(rec)
    return out


def in_window(rec: dict, start: datetime, end: datetime) -> bool:
    t = parse_start_time(rec.get("ended_utc"))
    return t is not None and start <= t < end


def job_of(rec: dict) -> str:
    return rec.get("name") or "<unknown>"


def cause_of(rec: dict) -> str:
    return ((rec.get("error_summary") or {}).get("summary")
            or "No error message recorded.")


def short(text: str, n: int = CAUSE_MAX_CHARS) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _trend(n: int, prev: int) -> str:
    if n == prev:
        return f"(last week: {prev}, no change)"
    arrow = "↑" if n > prev else "↓"
    return f"(last week: {prev}, {arrow} {abs(n - prev)})"


# ── message ────────────────────────────────────────────────────────────────
def build_digest(this_week: list[dict], prev_week: list[dict],
                 start: datetime, end: datetime,
                 runs_by_week: dict[str, int]) -> str:
    """Slack mrkdwn for one week. Pure function — no I/O — so it's testable."""
    key = start.date().isoformat()
    runs = runs_by_week.get(key)
    header = f"*IDMC weekly failures — {week_label(start, end)}*"
    n, prev_n = len(this_week), len(prev_week)

    # Coverage check: if the monitor HAS been recording weekly run counts
    # (an earlier week exists) but recorded none for this week, the monitor
    # was likely down — so "few failures" may just mean "nobody looked".
    gap = ""
    if runs_by_week and not runs and min(runs_by_week) < key:
        gap = ("\n:warning: _The 2-hour monitor recorded no runs this week — "
               "it may have been down, so these numbers may be incomplete._")

    if n == 0:
        tail = f" across {runs:,} runs" if runs else ""
        return (f":white_check_mark: {header}\n"
                f"0 failures{tail} (last week: {prev_n}).{gap}")

    jobs = Counter(job_of(r) for r in this_week)
    prev_jobs = {job_of(r) for r in prev_week}
    rate = f" · {n / runs:.2%} of {runs:,} runs" if runs else ""

    lines = [f":bar_chart: {header}",
             f"*{_plural(n, 'failure')}* across *{_plural(len(jobs), 'job')}* "
             f"{_trend(n, prev_n)}{rate}"]

    new = sorted(j for j in jobs if j not in prev_jobs)
    if new:
        shown = ", ".join(f"`{j}`" for j in new[:TOP_N])
        more = f" +{len(new) - TOP_N} more" if len(new) > TOP_N else ""
        lines.append(f"*New this week ({len(new)}):* {shown}{more}")

    causes_by_job: dict[str, Counter] = defaultdict(Counter)
    taskflow_by_job: dict[str, str] = {}
    for r in this_week:
        causes_by_job[job_of(r)][cause_of(r)] += 1
        if r.get("parent_taskflow"):
            taskflow_by_job[job_of(r)] = r["parent_taskflow"]

    lines.append("*Top failing jobs*")
    for i, (name, cnt) in enumerate(jobs.most_common(TOP_N), 1):
        cause = short(causes_by_job[name].most_common(1)[0][0])
        tf = (f" · taskflow `{taskflow_by_job[name]}`"
              if name in taskflow_by_job else "")
        lines.append(f"{i}. `{name}` — {cnt}× — {cause}{tf}")
    if len(jobs) > TOP_N:
        lines.append(f"_…and {_plural(len(jobs) - TOP_N, 'more job')}._")

    lines.append("*By cause*")
    for cause, cnt in Counter(cause_of(r) for r in this_week).most_common():
        lines.append(f"• {short(cause)} — {cnt}")

    lines.append("_Mapping-task failures recorded by the 2-hour monitor; "
                 "taskflow and ingestion-job failures aren't included. "
                 "Details: `find_failed_runs` in the Informatica MCP._"
                 + gap)
    return "\n".join(lines)


# ── run ────────────────────────────────────────────────────────────────────
async def run(cfg: Config, state_path: str, now: datetime,
              dry_run: bool = False, force: bool = False) -> str | None:
    """Post the digest for the last completed week if it's due.
    Returns the message text (or None when nothing was due).
    Raises DigestError on anything that must page the owner."""
    start, end = last_completed_week(now)
    week_key = start.date().isoformat()
    dstate = load_state(state_path)

    if not (force or dry_run) and dstate.get("last_posted_week") == week_key:
        log.info("Week of %s already posted; nothing to do.", week_key)
        return None

    prev_start = week_start_ct(start - timedelta(days=1))
    ledger = read_ledger(cfg.ledger_file)
    this_week = [r for r in ledger if in_window(r, start, end)]
    prev_week = [r for r in ledger if in_window(r, prev_start, start)]
    runs = load_state(cfg.state_file).get("runs_by_week", {})
    text = build_digest(this_week, prev_week, start, end, runs)

    if dry_run:
        print(text)
        return text
    if not cfg.slack_webhook_url_weekly:
        raise DigestError("SLACK_WEBHOOK_URL_WEEKLY is not set")
    if not await post_to_webhooks([cfg.slack_webhook_url_weekly], text):
        # State is NOT saved, so tomorrow's 08:00 run retries this week.
        raise DigestError("Posting to the weekly channel failed "
                          "(will retry at the next daily run)")

    dstate["last_posted_week"] = week_key
    dstate["last_posted_utc"] = now.astimezone(timezone.utc).isoformat()
    save_state(state_path, dstate)
    log.info("Posted weekly digest for week of %s (%d failures)",
             week_key, len(this_week))
    return text


def main() -> None:
    configure_logging()               # keeps httpx from logging webhook URLs
    cfg = Config.load()
    if cfg.env != "prod":
        log.error("weekly_digest.py runs against prod only "
                  "(INFORMATICA_MCP_ENV=%s)", cfg.env)
        sys.exit(2)
    try:
        asyncio.run(run(cfg, default_state_path(),
                        datetime.now(timezone.utc),
                        dry_run="--dry-run" in sys.argv,
                        force="--force" in sys.argv))
    except Exception as e:
        log.exception("Weekly digest FAILED")
        err = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        try:
            asyncio.run(post_to_webhooks(
                [cfg.slack_webhook_url],
                f":rotating_light: *IDMC weekly digest FAILED* — {err}\n"
                f"Check `journalctl -u weekly-digest.service` on the box."))
        except Exception:                          # noqa: BLE001
            log.error("Also failed to post the failure alert to Slack")
        sys.exit(1)


if __name__ == "__main__":
    main()
