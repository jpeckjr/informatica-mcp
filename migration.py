"""
migration.py — pure helpers for DEV -> PROD asset migration (export/import).

No network calls here: mcp_server.py does the I/O through idmc_client.py and
uses these functions to (a) keep packages in one known directory, (b) record
what was exported in a manifest next to each ZIP, and (c) build the import
specification. Kept separate so the parts that decide WHAT gets overwritten
in prod are unit-testable without a tenant (see test_migration.py).

API reference (Platform REST API v3, verified against Informatica docs
2026-10-08):
  POST /public/core/v3/export                    start export (objects[].id)
  GET  /public/core/v3/export/{id}?expand=objects status + object list
  GET  /public/core/v3/export/{id}/package        ZIP stream
  GET  /public/core/v3/export/{id}/log            text log
  POST /public/core/v3/import/package             multipart, part "package"
  POST /public/core/v3/import/{id}                start import (importSpecification)
  GET  /public/core/v3/import/{id}?expand=objects status + per-object result
  GET  /public/core/v3/import/{id}/log            text log

SAFETY MODEL (why the defaults are what they are):
  An export with includeDependencies=true (Informatica's default) packages the
  mapping, connections, runtime environment, project and folder alongside the
  task. Many tasks share one mapping template (e.g. all ~70 staging/salesforce
  MTTs use one mp_template_stage_*), so blindly overwriting every object would
  replace the PROD template under every one of them. Therefore:
    - defaultConflictResolution = REUSE  -> anything that already exists in
      prod is left alone; anything missing is created.
    - Only the assets the user explicitly asked to export (the "requested"
      ids recorded in the manifest) get conflictResolution = OVERWRITE.
    - Mapping tasks keep prod's persisted in-out parameter values
      (mapping_conflict_policy_existing = RETAIN); new ones start from the
      template defaults (mapping_conflict_policy_new = RESET), so DEV
      watermarks never leak into PROD.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_EXPORT_DIR = "~/informatica-mcp-exports"

# Terminal states for export/import jobs. Docs show IN_PROGRESS /
# SUCCESSFUL / FAILED; NOT_STARTED is returned by the package upload.
TERMINAL_STATES = {"SUCCESSFUL", "FAILED", "CANCELLED", "PARTIAL_SUCCESS"}


def export_dir() -> Path:
    """Directory that holds every package and manifest.

    Both the dev and prod MCP run as the invoking Linux user on the same
    box, so a per-user home directory is shared between them while staying
    private to that user. Override with IDMC_EXPORT_DIR.
    """
    d = Path(os.path.expanduser(os.environ.get("IDMC_EXPORT_DIR",
                                                DEFAULT_EXPORT_DIR)))
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


def safe_package_path(zip_path: str, base: Path | None = None) -> Path:
    """Resolve `zip_path` and refuse anything outside the export directory.

    The import tool reads and uploads a file from the server's disk; without
    this check a chat could point it at any readable file (e.g. an env file).
    A bare file name is resolved inside the export directory.
    """
    base = (base or export_dir()).resolve()
    p = Path(os.path.expanduser(zip_path))
    if not p.is_absolute():
        p = base / p
    p = p.resolve()
    if base != p.parent and base not in p.parents:
        raise ValueError(f"Package must be inside {base}, got {p}")
    if p.suffix.lower() != ".zip":
        raise ValueError(f"Package must be a .zip file, got {p.name}")
    if not p.is_file():
        raise FileNotFoundError(f"No such package: {p}")
    return p


def package_file_name(env: str, job_name: str, job_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", job_name).strip("_")[:80] or "export"
    return f"{env}_{slug}_{job_id}.zip"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def summarize_objects(status: dict) -> list[dict]:
    """Compact list of objects from an export status (?expand=objects)."""
    out = []
    for o in (status or {}).get("objects") or []:
        if not isinstance(o, dict):
            continue
        st = o.get("status") or {}
        out.append({
            "id": o.get("id"),
            "name": o.get("name"),
            "path": o.get("path"),
            "type": o.get("type"),
            "state": st.get("state"),
        })
    return out


def build_manifest(*, env: str, org_id: str, org_name: str, job: dict,
                   requested_ids: list[str], include_dependencies: bool,
                   objects: list[dict], zip_name: str, sha256: str) -> dict:
    return {
        "manifest_version": 1,
        "source_env": env,
        "source_org_id": org_id,
        "source_org_name": org_name,
        "export_job_id": job.get("id"),
        "export_job_name": job.get("name"),
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "requested_ids": list(requested_ids),
        "include_dependencies": include_dependencies,
        "objects": objects,
        "package": zip_name,
        "sha256": sha256,
    }


def manifest_path(zip_path: Path) -> Path:
    return zip_path.with_suffix(".manifest.json")


def write_manifest(zip_path: Path, manifest: dict) -> Path:
    mp = manifest_path(zip_path)
    tmp = mp.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=2))
    os.replace(tmp, mp)
    os.chmod(mp, 0o600)
    return mp


def read_manifest(zip_path: Path) -> dict:
    mp = manifest_path(zip_path)
    if not mp.is_file():
        raise FileNotFoundError(
            f"No manifest for {zip_path.name}. Only packages created by "
            "export_assets can be imported (the manifest records which "
            "objects may be overwritten).")
    return json.loads(mp.read_text())


def check_target_org(manifest: dict, target_org_id: str,
                     rollback: bool) -> str | None:
    """Decide whether this package may be imported into this org.

    Returns an error message, or None when the import is allowed.

      - Normal promotion (rollback=False): the package must come from a
        DIFFERENT org (DEV -> PROD). Importing a package back into the org it
        came from is refused, because that is almost always a mistake.
      - Rollback (rollback=True): the package must come from THIS org — it is
        the copy of the PROD task taken before the change, re-imported to
        restore it. A rollback from another org is refused: that would be a
        promotion mislabelled as a rollback.
    """
    source = manifest.get("source_org_id") or ""
    if not target_org_id or not source:
        return "Cannot determine source or target org; refusing to import."
    same = source == target_org_id
    if rollback and not same:
        return (f"rollback=True but this package was exported from org "
                f"{source}, not this org ({target_org_id}). A rollback package "
                f"must be the copy taken from this org before the change.")
    if not rollback and same:
        return (f"Package was exported from this org ({target_org_id}). To "
                f"restore it (a rollback), call again with rollback=True.")
    return None


def build_import_specification(manifest: dict,
                               overwrite_ids: list[str] | None = None,
                               keep_persisted_values: bool = True) -> dict:
    """Build `importSpecification` for POST /public/core/v3/import/{id}.

    overwrite_ids: objects to OVERWRITE in the target. Defaults to the ids
    the user explicitly exported (manifest["requested_ids"]). Every other
    object in the package is REUSEd (kept if present, created if missing).
    Ids must be in the package; anything else is refused.
    """
    package_ids = {o["id"] for o in manifest.get("objects") or [] if o.get("id")}
    requested = list(manifest.get("requested_ids") or [])
    overwrite = list(overwrite_ids) if overwrite_ids is not None else requested

    unknown = [i for i in overwrite if i not in package_ids]
    if unknown:
        raise ValueError(f"Not in this package: {unknown}")

    types = {o["id"]: (o.get("type") or "").upper()
             for o in manifest.get("objects") or [] if o.get("id")}
    container_types = {"PROJECT", "FOLDER", "SAAS_CONNECTION",
                       "SAAS_RUNTIME_ENVIRONMENT", "CONNECTION",
                       "RUNTIME_ENVIRONMENT"}
    bad = [i for i in overwrite if types.get(i) in container_types]
    if bad:
        # Docs: conflictResolution is for assets; "Do not use for projects,
        # folders, runtime environments, or connections."
        raise ValueError(f"Cannot OVERWRITE projects/folders/connections/"
                         f"runtime environments: {bad}")

    specs = []
    for oid in overwrite:
        spec = {"sourceObjectId": oid, "conflictResolution": "OVERWRITE"}
        # Mapping tasks: v3 type "MTT" (export listings may prefix it).
        t = types.get(oid, "")
        if t == "MAPPING_TASK" or "MTT" in t or "MCT" in t:
            spec["additionalProviderFlags"] = [
                {"key": "mapping_conflict_policy_existing",
                 "value": "RETAIN" if keep_persisted_values else "REPLACE"},
                {"key": "mapping_conflict_policy_new", "value": "RESET"},
            ]
        specs.append(spec)

    return {
        "defaultConflictResolution": "REUSE",
        "objectSpecification": specs,
    }


def import_plan(manifest: dict, spec: dict) -> list[dict]:
    """Human-readable per-object plan for the confirm preview."""
    over = {s["sourceObjectId"] for s in spec.get("objectSpecification") or []
            if s.get("conflictResolution") == "OVERWRITE"}
    rows = []
    for o in manifest.get("objects") or []:
        rows.append({
            "name": o.get("name"),
            "path": o.get("path"),
            "type": o.get("type"),
            "id": o.get("id"),
            "action": ("OVERWRITE" if o.get("id") in over
                       else "REUSE (keep existing; create if missing)"),
        })
    rows.sort(key=lambda r: (r["action"] != "OVERWRITE", r["type"] or "", r["path"] or ""))
    return rows
