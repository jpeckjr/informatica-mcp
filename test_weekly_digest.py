"""
test_weekly_digest.py — unit tests for the weekly failure digest.

No IICS tenant and no Slack webhook needed: the digest reads only the ledger
and the monitor's state file, and posting is mocked.

Run:  python -m unittest test_weekly_digest -v
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import weekly_digest as wd
from test_monitor import make_cfg


def U(*a) -> datetime:
    return datetime(*a, tzinfo=timezone.utc)


# Monday Oct 5 2026, 08:00 CDT — the normal post time. Last completed week
# is Mon Sep 28 00:00 CDT → Mon Oct 5 00:00 CDT.
MONDAY_8AM = U(2026, 10, 5, 13, 0)
THIS_WEEK_KEY = "2026-09-28"
PREV_WEEK_KEY = "2026-09-21"


def rec(name, ended, cause="Skipped: the previous run was still going.",
        run_id=None, taskflow=""):
    return {"name": name, "run_id": run_id or f"{name}-{ended.isoformat()}",
            "task_id": f"T-{name}", "ended_utc": ended.isoformat(),
            "error_summary": {"summary": cause}, "parent_taskflow": taskflow}


class WeekMathTests(unittest.TestCase):
    def test_monday_morning_reports_previous_week(self):
        s, e = wd.last_completed_week(MONDAY_8AM)
        self.assertEqual(s.date().isoformat(), THIS_WEEK_KEY)
        self.assertEqual(e.date().isoformat(), "2026-10-05")

    def test_boundary_sunday_night_vs_monday_morning(self):
        # Sun 23:30 CDT is still inside the Sep 28 week…
        s, _ = wd.last_completed_week(U(2026, 10, 5, 4, 30))
        self.assertEqual(s.date().isoformat(), PREV_WEEK_KEY)
        # …and Mon 00:30 CDT is the first moment it counts as completed.
        s, _ = wd.last_completed_week(U(2026, 10, 5, 5, 30))
        self.assertEqual(s.date().isoformat(), THIS_WEEK_KEY)

    def test_dst_weeks_are_167_and_169_hours(self):
        # Convert to UTC before subtracting. Python subtracts two datetimes
        # that share the SAME tzinfo object as naive wall-clock times,
        # ignoring their offsets — so CT-minus-CT always reports 168h, even
        # across a DST change. Real elapsed time needs a common frame.
        utc = lambda d: d.astimezone(timezone.utc)
        hours = lambda s, e: (utc(e) - utc(s)) / timedelta(hours=1)
        s, e = wd.last_completed_week(U(2026, 11, 2, 14, 0))   # DST ends
        self.assertEqual(hours(s, e), 169)
        s, e = wd.last_completed_week(U(2026, 3, 9, 13, 0))    # DST starts
        self.assertEqual(hours(s, e), 167)

    def test_window_is_half_open(self):
        s, e = wd.last_completed_week(MONDAY_8AM)
        self.assertTrue(wd.in_window(rec("a", s), s, e))       # start counts
        self.assertFalse(wd.in_window(rec("a", e), s, e))      # end doesn't


class BuildDigestTests(unittest.TestCase):
    def setUp(self):
        self.s, self.e = wd.last_completed_week(MONDAY_8AM)
        self.mid = self.s + timedelta(days=2)

    def test_all_clear_is_posted_not_silent(self):
        text = wd.build_digest([], [rec("x", self.mid)], self.s, self.e,
                               {THIS_WEEK_KEY: 5812})
        self.assertIn(":white_check_mark:", text)
        self.assertIn("0 failures across 5,812 runs", text)
        self.assertIn("last week: 1", text)

    def test_totals_trend_and_rate(self):
        this = [rec("a", self.mid, run_id=str(i)) for i in range(3)]
        text = wd.build_digest(this, [rec("a", self.mid)] * 5, self.s,
                               self.e, {THIS_WEEK_KEY: 300})
        self.assertIn("*3 failures* across *1 job*", text)
        self.assertIn("↓ 2", text)
        self.assertIn("1.00% of 300 runs", text)

    def test_new_this_week_excludes_jobs_that_failed_last_week(self):
        this = [rec("old_job", self.mid), rec("new_job", self.mid)]
        text = wd.build_digest(this, [rec("old_job", self.mid)],
                               self.s, self.e, {})
        new_line = next(l for l in text.splitlines()
                        if l.startswith("*New this week"))
        self.assertIn("`new_job`", new_line)
        self.assertNotIn("old_job", new_line)

    def test_top_jobs_ranked_with_cause_and_taskflow(self):
        this = ([rec("noisy", self.mid, cause="Deadlock victim.",
                     run_id=str(i), taskflow="staging/nightly")
                 for i in range(4)] + [rec("quiet", self.mid)])
        text = wd.build_digest(this, [], self.s, self.e, {})
        self.assertIn("1. `noisy` — 4× — Deadlock victim. · taskflow "
                      "`staging/nightly`", text)
        self.assertIn("2. `quiet` — 1×", text)

    def test_top_n_truncates(self):
        this = [rec(f"job{i:02d}", self.mid) for i in range(13)]
        text = wd.build_digest(this, [], self.s, self.e, {})
        self.assertIn("…and 3 more jobs", text)
        self.assertIn("+3 more", text)                 # new-this-week list

    def test_long_causes_are_trimmed(self):
        text = wd.build_digest([rec("a", self.mid, cause="x" * 200)], [],
                               self.s, self.e, {})
        self.assertNotIn("x" * 81, text)
        self.assertIn("…", text)

    def test_monitor_outage_is_flagged(self):
        """Run counts exist for earlier weeks but not this one → the monitor
        was probably down; a low count must not read as a good week."""
        text = wd.build_digest([], [], self.s, self.e, {PREV_WEEK_KEY: 4000})
        self.assertIn("recorded no runs this week", text)

    def test_no_outage_flag_before_run_counts_existed(self):
        """First week after deploy: no history yet, so no false alarm."""
        text = wd.build_digest([], [], self.s, self.e, {})
        self.assertNotIn("recorded no runs", text)


class RunTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = self._tmp.name
        self.ledger = os.path.join(d, "failures.jsonl")
        self.mon_state = os.path.join(d, "monitor_state.json")
        self.dstate = os.path.join(d, "weekly_digest_state.json")
        s, _ = wd.last_completed_week(MONDAY_8AM)
        with open(self.ledger, "w") as f:
            f.write(json.dumps(rec("a", s + timedelta(days=1))) + "\n")
            f.write(json.dumps(rec("a", s + timedelta(days=1))) + "\n")  # dup
            f.write("not json\n")
        with open(self.mon_state, "w") as f:
            json.dump({"runs_by_week": {THIS_WEEK_KEY: 100}}, f)
        self.cfg = make_cfg(ledger_file=self.ledger,
                            state_file=self.mon_state,
                            slack_webhook_url_weekly="https://hooks.example/w")

    def tearDown(self):
        self._tmp.cleanup()

    async def test_posts_once_then_is_idempotent(self):
        post = AsyncMock(return_value=True)
        with patch("weekly_digest.post_to_webhooks", post):
            first = await wd.run(self.cfg, self.dstate, MONDAY_8AM)
            again = await wd.run(self.cfg, self.dstate,
                                 MONDAY_8AM + timedelta(days=1))  # Tuesday
        self.assertIn("*1 failure*", first)            # duplicate collapsed
        self.assertIsNone(again)
        post.assert_awaited_once()

    async def test_failed_post_is_retried_next_day(self):
        with patch("weekly_digest.post_to_webhooks",
                   AsyncMock(return_value=False)):
            with self.assertRaises(wd.DigestError):
                await wd.run(self.cfg, self.dstate, MONDAY_8AM)
        post = AsyncMock(return_value=True)
        with patch("weekly_digest.post_to_webhooks", post):
            await wd.run(self.cfg, self.dstate,
                         MONDAY_8AM + timedelta(days=1))
        post.assert_awaited_once()                     # Tuesday caught up

    async def test_dry_run_posts_and_records_nothing(self):
        post = AsyncMock(return_value=True)
        with patch("weekly_digest.post_to_webhooks", post), \
             patch("builtins.print"):
            text = await wd.run(self.cfg, self.dstate, MONDAY_8AM,
                                dry_run=True)
        self.assertIn("IDMC weekly failures", text)
        post.assert_not_awaited()
        self.assertFalse(os.path.exists(self.dstate))

    async def test_missing_ledger_is_an_error_not_an_all_clear(self):
        self.cfg.ledger_file = os.path.join(self._tmp.name, "nope.jsonl")
        with self.assertRaises(wd.DigestError):
            await wd.run(self.cfg, self.dstate, MONDAY_8AM)

    async def test_missing_webhook_is_an_error(self):
        self.cfg.slack_webhook_url_weekly = ""
        with self.assertRaises(wd.DigestError):
            await wd.run(self.cfg, self.dstate, MONDAY_8AM)

    async def test_force_reposts_a_posted_week(self):
        post = AsyncMock(return_value=True)
        with patch("weekly_digest.post_to_webhooks", post):
            await wd.run(self.cfg, self.dstate, MONDAY_8AM)
            await wd.run(self.cfg, self.dstate, MONDAY_8AM, force=True)
        self.assertEqual(post.await_count, 2)

    def test_refuses_to_run_in_dev(self):
        with patch("weekly_digest.Config.load",
                   return_value=make_cfg(env="dev", expected_org_id="D")), \
             patch("weekly_digest.configure_logging"):
            with self.assertRaises(SystemExit) as ctx:
                wd.main()
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
