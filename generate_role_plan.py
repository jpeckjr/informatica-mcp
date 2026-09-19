"""
generate_role_plan.py — build role-append plan files from the connection
inventory (snowflake_connparams.json, produced by sf_connparams_report.py).

For every Snowflake connection whose recorded user matches TARGET_USER and
whose pre-migration `role` was non-empty, emits a role-only plan entry:
    {"connection_id": ..., "expect_name": ..., "role": <recorded role>}

The update_connections_from_file tool then appends 'role=<ROLE>' to each
connection's Additional JDBC URL Parameters (read live) — needed because
KeyPair auth hides the connector's role field.

Output is CHUNKED into role_migration_1.json, role_migration_2.json, ...
(CHUNK_SIZE entries each) because each entry costs a live GET during
preview/apply and one huge file would blow the MCP request timeout.

Run on the box:  .venv/bin/python generate_role_plan.py
Read-only inputs; overwrites role_migration_*.json outputs.
"""

from __future__ import annotations

import glob
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
INVENTORY = os.path.join(HERE, "snowflake_connparams.json")
TARGET_USER = "DEV_CORPSYS_INFORMATICA"
CHUNK_SIZE = 15


def main() -> None:
    with open(INVENTORY) as f:
        conns = json.load(f)

    entries, skipped = [], []
    for c in conns:
        params = c.get("connParams") or {}
        user = str(params.get("user") or "")
        role = str(params.get("role") or "").strip()
        if user != TARGET_USER:
            skipped.append((c.get("name"), f"user={user or '?'}"))
            continue
        if not role:
            skipped.append((c.get("name"), "no recorded role"))
            continue
        entries.append({"connection_id": c.get("id"),
                        "expect_name": c.get("name"),
                        "role": role})

    # Clear stale chunks so old files can't be re-run by mistake.
    for old in glob.glob(os.path.join(HERE, "role_migration_*.json")):
        os.remove(old)

    chunks = [entries[i:i + CHUNK_SIZE]
              for i in range(0, len(entries), CHUNK_SIZE)]
    for n, chunk in enumerate(chunks, 1):
        path = os.path.join(HERE, f"role_migration_{n}.json")
        with open(path, "w") as f:
            json.dump({"connections": chunk}, f, indent=2)
        print(f"wrote {os.path.basename(path)}: {len(chunk)} entries")

    print(f"\n{len(entries)} role entries across {len(chunks)} files; "
          f"{len(skipped)} connections skipped:")
    for name, why in skipped:
        print(f"  - {name}: {why}")


if __name__ == "__main__":
    main()
