"""
config.py — centralized configuration for the Informatica MCP project.

Loads from environment variables (a .env file is fine for dev; it's loaded
automatically below). For production-scoped credentials, set IDMC_SECRET_NAME
to pull secrets from AWS Secrets Manager via the EC2 instance role instead of
storing them in plaintext.

Notifications: the monitor posts a 2-hour digest to a SLACK incoming webhook
(SLACK_WEBHOOK_URL). If unset, the digest is logged instead (log-only mode).
Email/auto-stop/taskflow-heal settings were removed with the digest redesign.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

# Credential loading, in priority order (first value seen wins, because
# python-dotenv never overrides an already-set variable):
#   1. Real environment variables (systemd, shell exports)
#   2. ~/.informatica-mcp.env — the INVOKING USER's own credentials
#      (multi-user box: each teammate keeps their IICS login here, chmod 600,
#      so every MCP session runs under their own IICS identity/audit trail)
#   3. .env next to this module — shared/service fallback (the monitor's
#      service account, single-user setups)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.expanduser("~/.informatica-mcp.env"))
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass


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

    @classmethod
    def load(cls) -> "Config":
        region = os.environ.get("AWS_REGION", "us-east-2")
        password = os.environ.get("IDMC_PASSWORD", "")
        slack = os.environ.get("SLACK_WEBHOOK_URL", "")
        slack_team = os.environ.get("SLACK_WEBHOOK_URL_TEAM", "")

        secret_name = os.environ.get("IDMC_SECRET_NAME")
        if secret_name:
            secrets = _secrets_from_aws(secret_name, region)
            password = secrets.get("idmc_password", password)
            slack = secrets.get("slack_webhook_url", slack)
            slack_team = secrets.get("slack_webhook_url_team", slack_team)

        cfg = cls(
            login_url=os.environ.get("IDMC_LOGIN_URL", "").rstrip("/"),
            username=os.environ.get("IDMC_USERNAME", ""),
            password=password,
            org=os.environ.get("IDMC_ORG", "prod"),
            cycle_hours=float(os.environ.get("CYCLE_HOURS", "2")),
            long_run_cycles=int(os.environ.get("LONG_RUN_CYCLES", "2")),
            heartbeat_hour=int(os.environ.get("HEARTBEAT_HOUR", "8")),
            slack_webhook_url=slack,
            slack_webhook_url_team=slack_team,
            state_file=os.environ.get("STATE_FILE", "./monitor_state.json"),
            ledger_file=os.environ.get("LEDGER_FILE", _default_ledger_path()),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        missing = [n for n, v in {
            "IDMC_LOGIN_URL": self.login_url,
            "IDMC_USERNAME": self.username,
            "IDMC_PASSWORD": self.password,
        }.items() if not v]
        if missing:
            raise ValueError(f"Missing required config: {', '.join(missing)}")
