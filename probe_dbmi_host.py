"""
probe_dbmi_host.py — find which host serves the DBMI job-status API.

Tries GET /dbmi/public/api/v2/job/status (JSON body {"jobId": 5632}) against
candidate hosts, printing status codes and response snippets. Read-only.

Run on the box:  .venv/bin/python probe_dbmi_host.py [jobId]
"""

from __future__ import annotations

import asyncio
import sys

import httpx

from config import Config
from idmc_client import IDMCClient

CANDIDATE_HOSTS = [
    "https://na2.dm-us.informaticacloud.com",
    "https://na2.dm-us.informaticacloud.com/saas",
]
PATHS = [
    "/dbmi/public/api/v2/job/status",
    "/dbmi/api/v2/job/status",
    "/dbmi/api/v1/job/status",
    "/mihub/api/v2/job/status",
    "/cmi/api/v2/job/status",
]
METHODS = ["GET", "POST"]


async def main() -> None:
    job_id = int(sys.argv[1]) if len(sys.argv) > 1 else 5632
    cfg = Config.load()
    async with IDMCClient(cfg.login_url, cfg.username, cfg.password) as c:
        sid = c._session_id
        async with httpx.AsyncClient(timeout=30) as http:
            for host in CANDIDATE_HOSTS:
                for path in PATHS:
                    for method in METHODS:
                        url = f"{host}{path}"
                        try:
                            r = await http.request(
                                method, url,
                                headers={"IDS-SESSION-ID": sid,
                                         "INFA-SESSION-ID": sid,
                                         "icSessionId": sid,
                                         "Content-Type": "application/json",
                                         "Accept": "application/json"},
                                json={"jobId": job_id})
                            print(f"{r.status_code}  {method} {url}\n"
                                  f"      {r.text[:160]}")
                        except Exception as e:  # noqa: BLE001
                            print(f"ERR  {method} {url}\n      {e}")


if __name__ == "__main__":
    asyncio.run(main())
