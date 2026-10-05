"""
config.py — centralized configuration for the Informatica MCP project.

Loads from environment variables (a .env file is fine for dev; it's loaded
automatically below). For production-scoped credentials, set IDMC_SECRET_NAME
to pull secrets from AWS Secrets Manager via the EC2 instance role instead of
storing them in plaintext.

Notifications: the monitor posts a 2-hour digest to a SLACK incoming webhook
(SLACK_WEBHOOK_URL). If unset, the digest is logged instead (log-only mode).
Email/auto-stop/taskflow-heal settings were removed with the digest redesign.

Environments: INFORMATICA_MCP_ENV=prod (default) | dev selects which IICS org
and which credential files are used. Dev is MCP-only — no monitor, Slack,
state file or ledger — and requires IDMC_EXPECTED_ORG_ID. See ENVIRONMENTS.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

# ── Environment selection (prod / dev) ─────────────────────────────────────
# One codebase serves two IICS orgs. INFORMATICA_MCP_ENV picks which one;
# it defaults to "prod", so every existing launcher (the monitor's systemd
# unit, the team's Claude Desktop entry) behaves exactly as before.
#
#   prod -> ~/.informatica-mcp.env      then  .env       (unchanged)
#   dev  -> ~/.informatica-mcp-dev.env  then  .env.dev
#
# Each environment loads ONLY its own files. That isolation is the point:
# a teammate's personal ~/.informatica-mcp.env holds PROD credentials, and
# if dev mode read it, the "dev" MCP would silently run against prod with
# write tools enabled.
#
# Priority within an environment (first value seen wins, because
# python-dotenv never overrides an already-set variable):
#   1. Real environment variables (systemd, shell exports)
#   2. the per-user file — the INVOKING USER's own credentials (multi-user
#      box: chmod 600, so a session runs under their own IICS identity)
#   3. the shared file next to this module — the team/service credential
ENVIRONMENTS = {
    "prod": ("~/.informatica-mcp.env", ".env"),
    "dev":  ("~/.informatica-mcp-dev.env", ".env.dev"),
}


def current_env() -> str:
    env = os.environ.get("INFORMATICA_MCP_ENV", "prod").strip().lower()
    if env not in ENVIRONMENTS:
        raise ValueError(f"INFORMATICA_MCP_ENV must be one of "
                         f"{sorted(ENVIRONMENTS)}, got {env!r}")
    return env


def _env_file_paths(env: str) -> tuple[str, str]:
    per_user, shared = ENVIRONMENTS[env]
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.expanduser(per_user), os.path.join(here, shared)


def _load_env_files(env: str) -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for path in _env_file_paths(env):
        load_dotenv(path)


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes")


def _json(name: str, default):
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"{name} must be valid JSON: {e}") from e


def _default_ledger_path() -> str:
    """Ledger lives next to this module, NOT the process cwd — the monitor
    (systemd, WorkingDirectory set) and the MCP server (launched over ssh
    from the home dir) run with different cwds and must see the same file."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "failures.jsonl")


def _secrets_from_aws(secret_name: str, region: str) -> dict:
    """Fetch a JSON secret from AWS Secrets Manager using the instance role."""
    import boto3
    client = boto3.client("secretsmanager", region_name=region)
    resp = client.get_secret_value(SecretId=secret_name)
    return json.loads(resp["SecretString"])


@dataclass
class Config:
    # Auth / connection
    login_url: str
    username: str
    password: str
    org: str

    # Pass cadence: the systemd timer fires every `cycle_hours`; a running
    # job is FLAGGED once it exceeds `long_run_cycles` cycles
    # (2h cycle x 2 cycles = flagged after 4h, per the design doc).
    # `heartbeat_hour` (Central time): first pass at/after this hour each day
    # posts the alive/summary heartbeat.
    cycle_hours: float
    long_run_cycles: int
    heartbeat_hour: int

    # Notifications — Slack incoming webhooks (log-only when empty).
    # Primary = ops/personal channel: gets EVERYTHING (alerts, heartbeat,
    # monitor failsafe). Team (#team-data-platform) = pure signal: gets ONLY
    # failure/long-run alerts, no heartbeat/housekeeping.
    slack_webhook_url: str
    slack_webhook_url_team: str

    # State (window bookkeeping between passes) + failure ledger (queried by
    # the find_failed_runs MCP tool days/weeks later)
    state_file: str
    ledger_file: str

    # Which IICS org this process targets ("prod" | "dev"); see ENVIRONMENTS.
    env: str = "prod"

    # Org-id guard. When set, the client refuses to run if the credential
    # logs into any OTHER org. Required in dev: prod and dev share the same
    # login URL, so the credential is the ONLY thing separating them — one
    # wrong line in .env.dev would otherwise turn the dev MCP into an
    # unlabeled prod MCP with write tools.
    expected_org_id: str = ""

    # Weekly failure digest channel (weekly_digest.py). Prod only.
    slack_webhook_url_weekly: str = ""

    @classmethod
    def load(cls) -> "Config":
        env = current_env()
        _load_env_files(env)

        region = os.environ.get("AWS_REGION", "us-east-2")
        password = os.environ.get("IDMC_PASSWORD", "")
        slack = os.environ.get("SLACK_WEBHOOK_URL", "")
        slack_team = os.environ.get("SLACK_WEBHOOK_URL_TEAM", "")
        slack_weekly = os.environ.get("SLACK_WEBHOOK_URL_WEEKLY", "")

        secret_name = os.environ.get("IDMC_SECRET_NAME")
        if secret_name:
            secrets = _secrets_from_aws(secret_name, region)
            password = secrets.get("idmc_password", password)
            slack = secrets.get("slack_webhook_url", slack)
            slack_team = secrets.get("slack_webhook_url_team", slack_team)
            slack_weekly = secrets.get("slack_webhook_url_weekly", slack_weekly)

        if env == "dev":
            # Dev has no monitor: no Slack, no state file, no failure ledger.
            # Forced empty rather than merely unset, so a stray LEDGER_FILE in
            # the environment can never feed PROD failures to the dev MCP.
            slack = slack_team = slack_weekly = ""
            state_file = ledger_file = ""
        else:
            state_file = os.environ.get("STATE_FILE", "./monitor_state.json")
            ledger_file = os.environ.get("LEDGER_FILE", _default_ledger_path())

        cfg = cls(
            login_url=os.environ.get("IDMC_LOGIN_URL", "").rstrip("/"),
            username=os.environ.get("IDMC_USERNAME", ""),
            password=password,
            org=os.environ.get("IDMC_ORG", env),
            cycle_hours=float(os.environ.get("CYCLE_HOURS", "2")),
            long_run_cycles=int(os.environ.get("LONG_RUN_CYCLES", "2")),
            heartbeat_hour=int(os.environ.get("HEARTBEAT_HOUR", "8")),
            slack_webhook_url=slack,
            slack_webhook_url_team=slack_team,
            state_file=state_file,
            ledger_file=ledger_file,
            env=env,
            expected_org_id=os.environ.get("IDMC_EXPECTED_ORG_ID", "").strip(),
            slack_webhook_url_weekly=slack_weekly,
        )
        cfg.validate()
        return cfg

    # Template placeholders count as MISSING, not set. A value like
    # REPLACE_ME is non-empty, so a bare emptiness check let an unfilled
    # env file through to IICS — which answered with an opaque
    # "User name or password is not valid" instead of naming the real
    # problem. It also let the org guard "pass" with a placeholder id.
    _PLACEHOLDERS = {"replace_me", "changeme", "<dev-team-account>",
                     "<dev-team-password>", "<dev-org-id>", "<team-account>",
                     "<team-password>"}

    @classmethod
    def _unset(cls, value: str) -> bool:
        v = (value or "").strip().strip("'\"").lower()
        return not v or v in cls._PLACEHOLDERS

    def validate(self) -> None:
        missing = [n for n, v in {
            "IDMC_LOGIN_URL": self.login_url,
            "IDMC_USERNAME": self.username,
            "IDMC_PASSWORD": self.password,
        }.items() if self._unset(v)]
        if self.env == "dev" and self._unset(self.expected_org_id):
            missing.append("IDMC_EXPECTED_ORG_ID")
        if missing:
            per_user, shared = _env_file_paths(self.env)
            raise ValueError(
                f"[{self.env}] Missing required config: {', '.join(missing)} "
                f"(looked in {per_user} and {shared})")
