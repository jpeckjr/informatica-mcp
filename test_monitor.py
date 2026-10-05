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

    def __init__(self, running, completed, auth_mode="session-id",
                 auth_mode_changed=False, token_len=22,
                 token_ttl_seconds=None):
        self._running, self._completed = running, completed
        self.pages_fetched = 0
        # Auth-mode surface the monitor reads after each pass (JWT detection).
        # Defaults mirror a pre-JWT org: opaque session id, no transition.
        self.auth_mode = auth_mode
        self.auth_mode_changed = auth_mode_changed
        self.token_len = token_len
        self.token_ttl_seconds = token_ttl_seconds

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

    async def _run(self, cfg, running, completed, seed_state=None,
                   client=None):
        """One pass with mocked client + Slack.
        Returns (texts, state, ledger_lines). Client kept on self.client.
        `client` overrides the stub (used to simulate an auth-mode flip)."""
        cfg.state_file, cfg.ledger_file = self.state, self.ledger
        if seed_state is not None:
            with open(self.state, "w") as f:
                json.dump(seed_state, f)
        self.client = client or FakeClient(running, completed)
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

    # ── auth-mode flip (JWT migration early warning) ─────────────────────
    async def test_auth_mode_persisted_and_silent_when_unchanged(self):
        """A steady-state pass records the mode but posts nothing — the
        notice must not re-fire every 2 hours."""
        texts, state, _ = await self._run(
            make_cfg(), [running_job("ok_job", 1)], [log_entry("mt_fine", 1)])
        self.assertEqual(state["auth_mode"], "session-id")
        self.assertEqual(texts, [])

    async def test_auth_mode_flip_posts_notice_with_ttl(self):
        """The flip to JWT posts a one-time notice carrying the TTL — the
        number that decides whether long paginated pulls outlive a token."""
        flipped = FakeClient([], [log_entry("mt_fine", 1)],
                             auth_mode="jwt", auth_mode_changed=True,
                             token_len=1980, token_ttl_seconds=900)
        texts, state, _ = await self._run(make_cfg(), [], [], client=flipped)
        self.assertEqual(len(texts), 1)
        self.assertIn("JWT", texts[0])
        self.assertIn("1980 chars", texts[0])
        self.assertIn("TTL 15 min", texts[0])
        self.assertEqual(state["auth_mode"], "jwt")

    async def test_auth_mode_notice_stays_off_team_channel(self):
        """Housekeeping for the MCP owner, not signal for the team channel."""
        flipped = FakeClient([], [], auth_mode="jwt", auth_mode_changed=True,
                             token_ttl_seconds=900)
        await self._run(make_cfg(), [], [], client=flipped)
        for call in self.slack.call_args_list:
            self.assertFalse(call.kwargs.get("include_team", False))

    # ── weekly run counts (the weekly digest's failure-rate denominator) ──
    async def test_runs_are_counted_per_week(self):
        _, state, _ = await self._run(
            make_cfg(), [],
            [log_entry("a", 1), log_entry("b", 1, run_id="8")],
            seed_state={"runs_by_week": {"2020-01-06": 7}})
        weeks = state["runs_by_week"]
        self.assertEqual(weeks["2020-01-06"], 7)           # history kept
        self.assertEqual(sum(weeks.values()) - 7, 2)       # both runs counted
        for key in weeks:                                  # keys are Mondays
            self.assertEqual(datetime.fromisoformat(key).weekday(), 0)

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


class EnvironmentConfigTests(unittest.TestCase):
    """prod/dev selection. The invariant that matters: dev never reads a
    prod credential file, and never touches monitor surfaces."""

    BASE = {"IDMC_LOGIN_URL": "https://login", "IDMC_USERNAME": "u",
            "IDMC_PASSWORD": "p"}

    def _load(self, env_vars):
        import config
        with patch.dict(os.environ, env_vars, clear=True), \
             patch.object(config, "_load_env_files"):   # no real files
            return config.Config.load()

    def test_default_is_prod_and_unchanged(self):
        cfg = self._load(self.BASE)
        self.assertEqual(cfg.env, "prod")
        self.assertTrue(cfg.ledger_file.endswith("failures.jsonl"))
        self.assertEqual(cfg.expected_org_id, "")       # optional in prod

    def test_unknown_env_is_rejected(self):
        with self.assertRaises(ValueError):
            self._load({**self.BASE, "INFORMATICA_MCP_ENV": "staging"})

    def test_dev_reads_only_dev_files(self):
        """A teammate's ~/.informatica-mcp.env holds PROD credentials — if
        dev mode ever read it, the dev MCP would silently run against prod."""
        import config
        dev = config._env_file_paths("dev")
        prod = config._env_file_paths("prod")
        self.assertTrue(dev[0].endswith(".informatica-mcp-dev.env"))
        self.assertTrue(dev[1].endswith(".env.dev"))
        self.assertFalse(set(dev) & set(prod))

    def test_dev_selects_dev_files_at_load(self):
        import config
        seen = []
        with patch.dict(os.environ, {**self.BASE, "INFORMATICA_MCP_ENV": "dev",
                                     "IDMC_EXPECTED_ORG_ID": "DEV1"},
                        clear=True), \
             patch.object(config, "_load_env_files", side_effect=seen.append):
            config.Config.load()
        self.assertEqual(seen, ["dev"])

    def test_dev_disables_monitor_surfaces_even_if_set(self):
        """A stray LEDGER_FILE must never feed PROD failures to the dev MCP."""
        cfg = self._load({**self.BASE, "INFORMATICA_MCP_ENV": "dev",
                          "IDMC_EXPECTED_ORG_ID": "DEV1",
                          "LEDGER_FILE": "/home/x/failures.jsonl",
                          "SLACK_WEBHOOK_URL": "https://hooks.example/x"})
        self.assertEqual(cfg.ledger_file, "")
        self.assertEqual(cfg.state_file, "")
        self.assertEqual(cfg.slack_webhook_url, "")
        self.assertEqual(cfg.slack_webhook_url_team, "")
        self.assertEqual(cfg.org, "dev")

    def test_dev_requires_expected_org_id(self):
        with self.assertRaises(ValueError) as ctx:
            self._load({**self.BASE, "INFORMATICA_MCP_ENV": "dev"})
        self.assertIn("IDMC_EXPECTED_ORG_ID", str(ctx.exception))

    def test_template_placeholders_count_as_missing(self):
        """An unfilled template must fail at startup naming the fields —
        not reach IICS and come back as an opaque 'invalid password'."""
        with self.assertRaises(ValueError) as ctx:
            self._load({"IDMC_LOGIN_URL": "https://login",
                        "IDMC_USERNAME": "REPLACE_ME",
                        "IDMC_PASSWORD": "'REPLACE_ME'",
                        "INFORMATICA_MCP_ENV": "dev",
                        "IDMC_EXPECTED_ORG_ID": "REPLACE_ME"})
        msg = str(ctx.exception)
        for field in ("IDMC_USERNAME", "IDMC_PASSWORD", "IDMC_EXPECTED_ORG_ID"):
            self.assertIn(field, msg)

    def test_monitor_refuses_to_run_in_dev(self):
        with patch("monitor.Config.load",
                   return_value=make_cfg(env="dev", expected_org_id="DEV1")), \
             patch("monitor.configure_logging"):
            with self.assertRaises(SystemExit) as ctx:
                monitor.main()
        self.assertEqual(ctx.exception.code, 2)


class OrgGuardTests(unittest.IsolatedAsyncioTestCase):
    """Prod and dev share a login URL — the credential alone decides the org.
    The guard must refuse a session in the wrong org, and fail CLOSED."""

    def _client(self, expected):
        from idmc_client import IDMCClient
        c = IDMCClient("https://x", "u", "p", expected_org_id=expected)
        c._base_url, c._session_id = "https://x/saas", "s"
        return c

    async def test_no_expectation_is_a_noop(self):
        c = self._client("")
        c._http.get = AsyncMock(side_effect=AssertionError("no call expected"))
        await c._verify_org(None)

    async def test_matching_org_from_login_response_passes(self):
        c = self._client("DEV1")
        c._http.get = AsyncMock(side_effect=AssertionError("no fallback needed"))
        await c._verify_org("DEV1")
        self.assertEqual(c._session_id, "s")

    async def test_wrong_org_is_refused_and_session_discarded(self):
        c = self._client("DEV1")

        class R:          # the fallback lookup also reports the wrong org
            status_code = 200
            @staticmethod
            def json(): return {"orgId": "001KVB"}
        c._http.get = AsyncMock(return_value=R())
        with self.assertRaises(IDMCError) as ctx:
            await c._verify_org("001KVB")
        self.assertIn("001KVB", str(ctx.exception))
        self.assertIsNone(c._session_id)

    async def test_falls_back_to_org_endpoint_when_login_omits_it(self):
        c = self._client("DEV1")

        class R:
            status_code = 200
            @staticmethod
            def json(): return {"orgId": "DEV1"}
        c._http.get = AsyncMock(return_value=R())
        await c._verify_org(None)
        c._http.get.assert_awaited_once()

    async def test_uuid_expectation_accepts_v2_short_id(self):
        """v2 login reports the short orgId (001K5P); the env file may hold
        the 22-char orgUUID. Same org — must pass, via /api/v2/org."""
        c = self._client("8SLNtK67j8QcUupifUj9e2")

        class R:
            status_code = 200
            @staticmethod
            def json(): return {"orgId": "001K5P", "id": "001K5P",
                                "orgUUID": "8SLNtK67j8QcUupifUj9e2"}
        c._http.get = AsyncMock(return_value=R())
        await c._verify_org("001K5P")
        self.assertEqual(c._session_id, "s")

    async def test_short_expectation_accepts_v3_uuid(self):
        c = self._client("001K5P")

        class R:
            status_code = 200
            @staticmethod
            def json(): return {"orgId": "001K5P",
                                "orgUUID": "8SLNtK67j8QcUupifUj9e2"}
        c._http.get = AsyncMock(return_value=R())
        await c._verify_org("8SLNtK67j8QcUupifUj9e2")
        self.assertEqual(c._session_id, "s")

    async def test_prod_org_refused_in_either_form(self):
        """Dev expects 001K5P; a prod session (001KVB / its UUID) must fail."""
        c = self._client("001K5P")

        class R:
            status_code = 200
            @staticmethod
            def json(): return {"orgId": "001KVB",
                                "orgUUID": "48dKY117XQgjtQY3ADVpSy"}
        c._http.get = AsyncMock(return_value=R())
        with self.assertRaises(IDMCError):
            await c._verify_org("48dKY117XQgjtQY3ADVpSy")
        self.assertIsNone(c._session_id)

    async def test_unknown_org_fails_closed(self):
        c = self._client("DEV1")
        c._http.get = AsyncMock(side_effect=RuntimeError("network down"))
        with self.assertRaises(IDMCError):
            await c._verify_org(None)
        self.assertIsNone(c._session_id)


class LogHygieneTests(unittest.TestCase):
    """httpx logs full request URLs at INFO, and the Slack webhook URL IS a
    credential — anyone holding it can post to the channel as the monitor.
    journald here is readable by the shared team group, so this must not
    regress back to INFO."""

    def setUp(self):
        import logging as _l
        self._prev = _l.getLogger("httpx").level

    def tearDown(self):
        import logging as _l
        _l.getLogger("httpx").setLevel(self._prev)

    def test_httpx_is_quiet_by_default(self):
        import logging as _l
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HTTPX_LOG_LEVEL", None)
            monitor.configure_logging()
        self.assertEqual(_l.getLogger("httpx").level, _l.WARNING)

    def test_httpx_level_is_overridable_for_debugging(self):
        import logging as _l
        with patch.dict(os.environ, {"HTTPX_LOG_LEVEL": "INFO"}):
            monitor.configure_logging()
        self.assertEqual(_l.getLogger("httpx").level, _l.INFO)


class AuthModeDetectionTests(unittest.IsolatedAsyncioTestCase):
    """JWT migration: token-shape detection, proactive refresh, and the
    re-auth path that must re-run the ORIGINAL login (v2 vs v3)."""

    @staticmethod
    def _jwt(exp=None, filler=0):
        import base64, json as _json
        hdr = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
        claims = {"sub": "svc"}
        if exp is not None:
            claims["exp"] = exp
        if filler:
            claims["pad"] = "x" * filler
        body = base64.urlsafe_b64encode(
            _json.dumps(claims).encode()).rstrip(b"=").decode()
        return f"{hdr}.{body}.sig"

    def test_opaque_session_id_is_not_jwt(self):
        from idmc_client import looks_like_jwt, jwt_expiry
        self.assertFalse(looks_like_jwt("9KjQ2mXvBn4TzR8wLpY6Hs"))
        self.assertIsNone(jwt_expiry("9KjQ2mXvBn4TzR8wLpY6Hs"))

    def test_jwt_without_exp_still_reads_as_jwt(self):
        """Mode detection must not depend on `exp` — a JWT with no expiry is
        still a JWT, and misreporting the org's auth mode is the worse bug."""
        from idmc_client import looks_like_jwt, jwt_expiry
        tok = self._jwt()
        self.assertTrue(looks_like_jwt(tok))
        self.assertIsNone(jwt_expiry(tok))

    def test_b64_padding_across_residues(self):
        from idmc_client import jwt_expiry
        for filler in range(0, 40):          # every b64 pad residue
            self.assertIsNotNone(jwt_expiry(self._jwt(exp=1_900_000_000,
                                                      filler=filler)),
                                 f"padding broke at filler={filler}")

    def _client(self, known=None):
        from idmc_client import IDMCClient
        c = IDMCClient("https://x", "u", "p", known_auth_mode=known)
        c._base_url = "https://x/saas"
        return c

    def test_first_observation_is_not_a_change(self):
        c = self._client(known=None)
        c._session_id = "opaque"
        c._observe_token()
        self.assertEqual(c.auth_mode, "session-id")
        self.assertFalse(c.auth_mode_changed)

    def test_flip_to_jwt_is_flagged_with_ttl(self):
        import time as _t
        c = self._client(known="session-id")
        c._session_id = self._jwt(exp=_t.time() + 900)
        c._observe_token()
        self.assertEqual(c.auth_mode, "jwt")
        self.assertTrue(c.auth_mode_changed)
        self.assertAlmostEqual(c.token_ttl_seconds, 900, delta=5)

    def test_steady_state_jwt_is_not_a_change(self):
        import time as _t
        c = self._client(known="jwt")
        c._session_id = self._jwt(exp=_t.time() + 900)
        c._observe_token()
        self.assertFalse(c.auth_mode_changed)

    async def test_reauth_reruns_v2_login_when_session_came_from_v2(self):
        """A session established by login_v2 must be renewed by login_v2 —
        falling back to v3 silently relies on the interchangeability the JWT
        migration may invalidate."""
        c = self._client()
        called = []
        async def fake_v3(): called.append("v3")
        async def fake_v2(): called.append("v2")
        c.login, c.login_v2 = fake_v3, fake_v2

        c._login_mode = "v2"
        await c._reauth()
        c._login_mode = "v3"
        await c._reauth()
        self.assertEqual(called, ["v2", "v3"])

    async def test_expired_jwt_refreshes_before_request(self):
        """Proactive refresh fires inside the skew window, without waiting
        for a 401."""
        import time as _t
        c = self._client()
        c._session_id = self._jwt(exp=_t.time() + 10)   # inside 60s skew
        c._observe_token()
        refreshed = []

        async def fake_reauth():
            refreshed.append(1)
            c._session_id = self._jwt(exp=_t.time() + 3600)
            c._observe_token()
        c._reauth = fake_reauth

        class R:
            status_code, content = 200, b"{}"
            @staticmethod
            def json(): return {}
        async def fake_req(*a, **k): return R()
        c._http.request = fake_req

        await c._request("GET", "/api/v2/org")
        self.assertEqual(len(refreshed), 1)

    async def test_opaque_session_never_refreshes_proactively(self):
        """Pre-JWT behaviour must be byte-identical — no extra logins."""
        c = self._client()
        c._session_id = "opaque"
        c._observe_token()
        c._reauth = lambda: (_ for _ in ()).throw(
            AssertionError("must not re-auth pre-JWT"))

        class R:
            status_code, content = 200, b"{}"
            @staticmethod
            def json(): return {}
        async def fake_req(*a, **k): return R()
        c._http.request = fake_req
        await c._request("GET", "/api/v2/org")

    async def test_403_retries_only_under_jwt(self):
        """403 means 'possibly expired' under JWT, but must keep failing fast
        pre-JWT so genuine permission errors stay fast and loud."""
        import time as _t
        for token, expect_retry in ((self._jwt(exp=_t.time() + 3600), True),
                                    ("opaque", False)):
            c = self._client()
            c._session_id = token
            c._observe_token()
            reauths, calls = [], []

            async def fake_reauth(): reauths.append(1)
            c._reauth = fake_reauth

            class R:
                status_code, content, text = 403, b"{}", "denied"
            async def fake_req(*a, **k):
                calls.append(1)
                return R()
            c._http.request = fake_req

            with self.assertRaises(IDMCError):
                await c._request("GET", "/active-bpel/services/tf/status/1",
                                 saas=False)
            self.assertEqual(len(reauths), 1 if expect_retry else 0)
            self.assertEqual(len(calls), 2 if expect_retry else 1)

    async def test_401_stops_smart_resolver(self):
        """An expired token must not be misreported as 'no such taskflow'."""
        c = self._client()
        attempts = []
        async def fake(name):
            attempts.append(name)
            raise IDMCError("session expired", status_code=401)
        c.run_taskflow_by_name = fake
        with self.assertRaises(IDMCError):
            await c.run_taskflow_smart("FlowX")
        self.assertEqual(attempts, ["FlowX"])    # did not walk the suffixes


if __name__ == "__main__":
    unittest.main(verbosity=2)
