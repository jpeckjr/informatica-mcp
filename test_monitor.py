"""
test_monitor.py — unit tests for the failure-focused Slack monitor.

Mocks the IDMC client and the Slack poster so the alerting decisions
(failures-only posting, window pagination, long-run flagging, heartbeat,
ledger writes) can be verified locally with NO live IDMC tenant and NO
Slack webhook.

Run:  python -m unittest test_monitor -v
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import monitor
from config import Config
from idmc_client import IDMCError, summarize_error


def make_cfg(**overrides) -> Config:
    base = dict(
        login_url="https://example", username="svc", password="x", org="prod",
        cycle_hours=2.0, long_run_cycles=2,
        heartbeat_hour=24,              # 24 = heartbeat never fires in tests
        slack_webhook_url="",           # log-only in tests unless overridden
        slack_webhook_url_team="",
        state_file="", ledger_file="",
    )
    base.update(overrides)
    return Config(**base)


def running_job(name, hours_ago):
    # Mirrors real activityMonitor fields: taskName, type, startTimeUtc.
    start = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return {"taskName": name, "type": "MTT", "runId": "r1", "taskId": "t1",
            "startTimeUtc": start.isoformat(), "startTime": start.isoformat(),
            "executionState": "RUNNING"}


def log_entry(name, ended_hours_ago, state=1, error_msg=None,
              ok_rows=10, bad_rows=0, run_id="9", task_id="T9"):
    # Mirrors real activityLog fields: objectName, endTimeUtc, state, rows.
    end = datetime.now(timezone.utc) - timedelta(hours=ended_hours_ago)
    start = end - timedelta(minutes=30)
    e = {"objectName": name, "type": "MTT", "runId": run_id,
         "objectId": task_id,
         "startTimeUtc": start.isoformat(), "endTimeUtc": end.isoformat(),
         "state": state, "totalSuccessRows": ok_rows,
         "totalFailedRows": bad_rows,
         "parentTaskFederatedId": "TFID1"}
    if error_msg:
        e["errorMsg"] = error_msg
    return e


SNOWFLAKE_ERR = ("[ERROR] ... SEVERE: State: COPY_INTO_TABLE, COPY INTO \"X\" "
                 "SQL compilation error: invalid URL prefix")


class FakeClient:
    """Serves the activity log in PAGES like the real API (newest first)."""

    def __init__(self, running, completed):
        self._running, self._completed = running, completed
        self.pages_fetched = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def list_running_jobs(self):
        return self._running

    async def get_activity_log(self, run_id=None, row_limit=50,
                               task_id=None, offset=0):
        self.pages_fetched += 1
        return self._completed[offset or 0:(offset or 0) + row_limit]

    async def lookup_objects(self, object_ids=None, objects=None):
        return {"objects": [{"id": (object_ids or [""])[0],
                             "path": "staging/controller_stage_daily_night",
                             "type": "TASKFLOW"}]}


class AlertTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self._tmp.name, "state.json")
        self.ledger = os.path.join(self._tmp.name, "failures.jsonl")

    def tearDown(self):
        self._tmp.cleanup()

    async def _run(self, cfg, running, completed, seed_state=None):
        """One pass with mocked client + Slack.
        Returns (texts, state, ledger_lines). Client kept on self.client."""
        cfg.state_file, cfg.ledger_file = self.state, self.ledger
        if seed_state is not None:
            with open(self.state, "w") as f:
                json.dump(seed_state, f)
        self.client = FakeClient(running, completed)
        slack = self.slack = AsyncMock()
        with patch("monitor.IDMCClient", return_value=self.client), \
             patch("monitor.post_to_slack", slack):
            await monitor.run_once(cfg)
        texts = [c.args[1] for c in slack.call_args_list]
        with open(self.state) as f:
            state = json.load(f)
        ledger = []
        if os.path.exists(self.ledger):
            with open(self.ledger) as f:
                ledger = [json.loads(x) for x in f if x.strip()]
        return texts, state, ledger

    # ── failures-only posting ────────────────────────────────────────────
    async def test_healthy_pass_posts_nothing(self):
        texts, _, ledger = await self._run(
            make_cfg(), [running_job("ok_job", 1)],
            [log_entry("mt_fine", 1)])
        self.assertEqual(texts, [])
        self.assertEqual(ledger, [])

    async def test_failure_posts_alert_with_summary_and_taskflow(self):
        texts, _, _ = await self._run(
            make_cfg(), [],
            [log_entry("mt_bad", 1, state=3, error_msg=SNOWFLAKE_ERR,
                       ok_rows=0)])
        self.assertEqual(len(texts), 1)
        self.assertIn("IDMC FAILURES", texts[0])
        self.assertIn("mt_bad", texts[0])
        self.assertIn("Snowflake rejected the SQL", texts[0])
        self.assertIn("Why (speculative)", texts[0])
        self.assertIn("controller_stage_daily_night", texts[0])

    async def test_failure_outside_window_ignored(self):
        texts, _, ledger = await self._run(
            make_cfg(), [],
            [log_entry("mt_old_fail", 5, state=3, error_msg="boom")])
        self.assertEqual(texts, [])
        self.assertEqual(ledger, [])

    async def test_long_runner_posts_warning(self):
        texts, _, _ = await self._run(
            make_cfg(), [running_job("mt_slow", 5)], [])
        self.assertEqual(len(texts), 1)
        self.assertIn("Long-running", texts[0])
        self.assertIn("mt_slow", texts[0])

    async def test_short_runner_no_post(self):
        texts, _, _ = await self._run(
            make_cfg(), [running_job("mt_quick", 3)], [])
        self.assertEqual(texts, [])         # 3h < 4h (2 cycles)

    # ── window pagination (the prod missed-failure bug) ──────────────────
    async def test_failure_beyond_first_page_is_found(self):
        # 250 successes newer than the failure -> failure sits on page 3.
        entries = [log_entry(f"mt_ok_{i}", 0.1 + i * 0.001,
                             run_id=str(i), task_id=f"T{i}")
                   for i in range(250)]
        entries.append(log_entry("mt_deep_fail", 1.5, state=3,
                                 error_msg="boom", run_id="999",
                                 task_id="T999"))
        texts, _, ledger = await self._run(make_cfg(), [], entries)
        self.assertEqual(len(texts), 1)
        self.assertIn("mt_deep_fail", texts[0])
        self.assertGreaterEqual(self.client.pages_fetched, 3)
        self.assertEqual(len(ledger), 1)

    async def test_window_starts_at_last_run(self):
        # Prior pass 6h ago -> a 5h-old failure IS in the window (no gaps).
        last = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
        texts, _, _ = await self._run(
            make_cfg(), [],
            [log_entry("mt_old_fail", 5, state=3, error_msg="boom")],
            seed_state={"last_run_utc": last})
        self.assertEqual(len(texts), 1)
        self.assertIn("mt_old_fail", texts[0])

    # ── ledger ───────────────────────────────────────────────────────────
    async def test_ledger_records_failure_compactly(self):
        _, state, ledger = await self._run(
            make_cfg(), [],
            [log_entry("mt_bad", 1, state=3, error_msg=SNOWFLAKE_ERR,
                       run_id="42", task_id="T42")])
        self.assertEqual(len(ledger), 1)
        rec = ledger[0]
        self.assertEqual(rec["name"], "mt_bad")
        self.assertEqual(rec["run_id"], "42")
        self.assertEqual(rec["parent_taskflow"],
                         "staging/controller_stage_daily_night")
        self.assertIn("Snowflake", rec["error_summary"]["summary"])
        self.assertIn("T42:42", state["ledger_keys"])

    async def test_ledger_dedup_across_reruns(self):
        entry = log_entry("mt_bad", 1, state=3, error_msg="boom",
                          run_id="42", task_id="T42")
        texts, _, ledger = await self._run(
            make_cfg(), [], [entry],
            seed_state={"ledger_keys": ["T42:42"]})
        self.assertEqual(len(texts), 1)     # still alerts...
        self.assertEqual(ledger, [])        # ...but no duplicate ledger row

    # ── channel routing ──────────────────────────────────────────────────
    async def test_alert_routes_to_team_channel(self):
        texts, _, _ = await self._run(
            make_cfg(), [],
            [log_entry("mt_bad", 1, state=3, error_msg="boom")])
        self.assertEqual(len(texts), 1)
        self.assertTrue(self.slack.call_args_list[0].kwargs.get("include_team"))

    async def test_heartbeat_stays_off_team_channel(self):
        cfg = make_cfg(heartbeat_hour=0)
        texts, _, _ = await self._run(cfg, [], [log_entry("mt_fine", 1)])
        self.assertEqual(len(texts), 1)             # heartbeat only
        self.assertFalse(
            self.slack.call_args_list[0].kwargs.get("include_team", False))

    # ── heartbeat ────────────────────────────────────────────────────────
    async def test_heartbeat_posts_once_daily(self):
        cfg = make_cfg(heartbeat_hour=0)    # any hour qualifies
        texts, state, _ = await self._run(cfg, [], [log_entry("mt_fine", 1)])
        self.assertEqual(len(texts), 1)
        self.assertIn("heartbeat", texts[0])
        self.assertIn("1 runs completed", texts[0])
        # Second pass same day: no heartbeat again.
        texts2, _, _ = await self._run(cfg, [], [],
                                       seed_state=state)
        self.assertEqual(texts2, [])

    async def test_heartbeat_counts_accumulate_until_posted(self):
        cfg = make_cfg(heartbeat_hour=24)   # suppressed -> accumulate only
        _, state, _ = await self._run(cfg, [], [log_entry("a", 1),
                                                log_entry("b", 1, state=3,
                                                          error_msg="x",
                                                          run_id="2",
                                                          task_id="T2")])
        self.assertEqual(state["heartbeat"]["completed"], 2)
        self.assertEqual(state["heartbeat"]["failed"], 1)


class SummarizeErrorTests(unittest.TestCase):
    def test_no_error_returns_none(self):
        self.assertIsNone(summarize_error(None))
        self.assertIsNone(summarize_error("No errors encountered."))

    def test_snowflake_rule_matches(self):
        s = summarize_error("SQL compilation error: invalid URL prefix")
        self.assertIn("Snowflake", s["summary"])
        self.assertTrue(s["likely_cause"])

    def test_relation_does_not_exist_is_categorized_or_falls_back(self):
        s = summarize_error('[FATAL] [informatica][PostgreSQL JDBC Driver]'
                            '[PostgreSQL]relation "return_document_report" '
                            'does not exist.')
        self.assertIsNotNone(s)
        self.assertTrue(s["raw_first_line"].startswith("[FATAL]"))

    def test_prod_mined_rules_match(self):
        cases = {
            "The job was stopped by user jbpeck@spscommerce.com.prod using the API.":
                "manually stopped",
            "The Mapping task failed to run. Another instance of the task is currently running.":
                "previous run",
            "[FATAL] The Snowflake Connector ... Numeric value '2026-07-23' is not recognized":
                "doesn't fit the column type",
            "SEVERE: State: INGEST_DATA, MERGE INTO ... Duplicate row detect":
                "duplicate join keys",
            "Transaction (Process ID 119) was deadlocked on lock ... deadlock victim":
                "deadlock victim",
            "Internal error. The DTM process terminated unexpectedly.":
                "crashed on the Secure Agent",
            "[ERROR] [informatica][PostgreSQL JDBC Driver]No more data available to read.":
                "dropped mid-read",
            "java.net.UnknownHostException: id.spsc.io":
                "DNS could not resolve",
            "com.informatica.saas.repository.exception.RepoException: HTTP/1.1 500":
                "IICS internal service error",
        }
        for raw, expect in cases.items():
            s = summarize_error(raw)
            self.assertIn(expect, s["summary"] + s["likely_cause"],
                          f"rule miss for: {raw[:60]}")

    def test_uncategorized_falls_back_to_first_line(self):
        s = summarize_error("some brand new weird failure\nstack line 2")
        self.assertIn("uncategorized", s["summary"])
        self.assertEqual(s["raw_first_line"], "some brand new weird failure")


class RunTaskflowSmartTests(unittest.IsolatedAsyncioTestCase):
    """Directly exercises the REAL IDMCClient.run_taskflow_smart resolver."""

    def _client(self):
        from idmc_client import IDMCClient
        c = IDMCClient("https://x", "u", "p")
        c._session_id, c._base_url = "s", "https://x/saas"
        return c

    async def test_resolves_to_suffixed_name(self):
        c = self._client()
        attempts = []

        async def fake(name):
            attempts.append(name)
            if name != "FlowX-2":
                raise IDMCError("404", status_code=404)
            return {"RunId": "R"}

        c.run_taskflow_by_name = fake
        resp, used = await c.run_taskflow_smart("FlowX")
        self.assertEqual(used, "FlowX-2")
        self.assertEqual(resp["RunId"], "R")
        self.assertEqual(attempts, ["FlowX", "FlowX-1", "FlowX-2"])

    async def test_403_stops_immediately(self):
        c = self._client()
        attempts = []

        async def fake(name):
            attempts.append(name)
            raise IDMCError("not authorized", status_code=403)

        c.run_taskflow_by_name = fake
        with self.assertRaises(IDMCError):
            await c.run_taskflow_smart("FlowX")
        self.assertEqual(attempts, ["FlowX"])   # did not try suffixes


if __name__ == "__main__":
    unittest.main(verbosity=2)
