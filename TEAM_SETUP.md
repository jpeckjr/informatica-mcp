# Informatica MCP — Team Setup (shared team login)

Run the Informatica IDMC MCP server in your own Claude Desktop, over SSH to
the shared EC2 box. All sessions use the **shared team IICS credential**
configured centrally on the box — you don't need your own IICS login, and
there is nothing credential-related for you to set up.

> **Attribution note (read once):** because everyone shares one IICS login,
> `startedBy`/`updatedBy` in Informatica — and in the Slack monitor alerts —
> will show the team account for every action, no matter who did it. The
> record of *who* did something lives in the box's SSH logs (you connect as
> yourself). Announce prod writes in #team-data-platform as you make them.

## Prerequisites

- SPS VPN access (the box is private — VPN must be up whenever you use this)
- A Linux account on the box (ask Joe Peck — send him your SSH **public**
  key, never the private one):

  ```bash
  ssh-keygen -t ed25519 -C "you@spscommerce.com"
  cat ~/.ssh/id_ed25519.pub     # send this line to Joe
  ```

- Claude Desktop installed

## 1. SSH config (your Mac)

Add to `~/.ssh/config` (replace `youruser` and the host):

```
Host idmc-box
    HostName <box-hostname-or-ip>
    User youruser
    IdentityFile ~/.ssh/id_ed25519
```

Test: `ssh idmc-box 'echo ok'` (VPN up).

## 2. Claude Desktop config (your Mac)

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`
under `mcpServers`:

```jsonc
"informatica": {
  "command": "ssh",
  "args": ["-T", "idmc-box",
           "/opt/informatica-mcp/.venv/bin/python /opt/informatica-mcp/mcp_server.py"]
}
```

Fully restart Claude Desktop (Cmd+Q, reopen). You should see the
`informatica` tools. Smoke test: ask Claude to run `list_running_jobs`.

That's the whole setup — the team credential is already on the box.

## Rules of the road

- **The team login points at PROD.** Read tools (jobs, logs, connections,
  lineage, dependencies, ingestion status) are safe to use freely. Write
  tools (`run_mapping_task`, `run_taskflow_by_name`, `update_connection(s)`,
  `stop_running_job`) always show a PREVIEW first and require an explicit
  confirm — treat every confirm as pressing a button on a production console.
- Say what you're doing in #team-data-platform before prod writes — with a
  shared login, that message is the audit trail your teammates can see.
- Connection updates via plan files: put your file in `/opt/informatica-mcp/`
  (or ask Joe) — secrets in plan files never appear in chat (masked).
- The 2-hour Slack monitor runs under its own service setup and is
  unaffected by anything you do here.
- Power users: you *may* override the team login by creating your own
  `~/.informatica-mcp.env` (chmod 600) with personal IICS credentials — the
  per-user file takes precedence over the shared one. Delete it to go back.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Tools missing in Claude Desktop | Fully quit (Cmd+Q) + reopen; SSH must work first |
| `Missing required config: IDMC_...` | You're not in the `mcpteam` group (ask Joe), or the shared env moved |
| `403 Access denied` on known ids | Id from the wrong org (dev id against the prod team login) |
| `Could not resolve hostname idmc-box` | VPN is down, or `~/.ssh/config` entry missing |
| Login failed (401) | Team credential rotated/expired — tell Joe, don't retry repeatedly (lockout) |

---

## Admin appendix (Joe)

### One-time: shared install + team credential

```bash
# 1. shared read-only code at /opt (never sync .env or plan files from home)
sudo mkdir -p /opt/informatica-mcp
sudo rsync -a --exclude='.env' --exclude='*.json' --exclude='__pycache__' \
  --exclude='.venv' --exclude='monitor_state.json' --exclude='audit.log' \
  --exclude='failures.jsonl' ~/informatica-mcp/ /opt/informatica-mcp/

# 2. shared venv
sudo python3 -m venv /opt/informatica-mcp/.venv
sudo /opt/informatica-mcp/.venv/bin/pip install "mcp[cli]" httpx boto3

# 3. team group — members can read the shared credential, nothing else can
sudo groupadd mcpteam
sudo usermod -aG mcpteam <each-member>          # repeat per member

# 4. the TEAM credential (this is the only secret on disk)
sudo tee /opt/informatica-mcp/.env > /dev/null <<'EOF'
IDMC_LOGIN_URL=https://dm-us.informaticacloud.com
IDMC_USERNAME=<team-account>@spscommerce.com.prod
IDMC_PASSWORD=<team-password>
IDMC_ORG=prod
EOF
sudo chown root:mcpteam /opt/informatica-mcp/.env
sudo chmod 640 /opt/informatica-mcp/.env

# 5. code world-readable, env excluded from that
sudo chmod -R a+rX /opt/informatica-mcp
sudo chmod 640 /opt/informatica-mcp/.env       # re-assert after the -R
```

Per new member: create their Linux account (public key into
`authorized_keys`) and `sudo usermod -aG mcpteam <user>`.
To revoke someone: delete their Linux account. To rotate the team password:
edit one file (they pick it up on their next Claude Desktop restart).

**Better than a password on disk:** the config supports AWS Secrets Manager.
Put the team credential in a secret via the EC2 instance role and replace
`IDMC_PASSWORD` in the shared env with `IDMC_SECRET_NAME=<secret-name>` —
then rotation happens in Secrets Manager and no plaintext password exists on
the box. (Clear the shared-prod-credential approach with security either way.)

### After each code change (append to your usual deploy)

```bash
ssh idmc-box 'sudo rsync -a --delete --exclude=".env" --exclude="*.json" \
  --exclude="*.csv" --exclude=".venv" --exclude="__pycache__" \
  --exclude="failures.jsonl" --exclude="monitor_state.json" \
  --exclude="audit.log" \
  ~/informatica-mcp/ /opt/informatica-mcp/ \
  && sudo chmod -R a+rX /opt/informatica-mcp \
  && sudo chmod 640 /opt/informatica-mcp/.env'
```

(Excludes keep secrets, plan files, inventory CSVs, and monitor state out of
the world-readable shared copy.)

### Optional: share the monitor's failure ledger

```bash
chmod o+X /home/jbpeck /home/jbpeck/informatica-mcp
chmod o+r /home/jbpeck/informatica-mcp/failures.jsonl
echo 'LEDGER_FILE=/home/jbpeck/informatica-mcp/failures.jsonl' | \
  sudo tee -a /opt/informatica-mcp/.env
```

(Without this, `find_failed_runs` still works for the team via the live-API
fallback — just slower and limited to IDMC log retention.)

### Your own setup

Unchanged: the monitor and your Claude Desktop keep using
`~/informatica-mcp` and its `.env`. Don't create a `~/.informatica-mcp.env`
for yourself unless you intend it to take over your interactive sessions
(and note the monitor, running as you, would pick it up too).
