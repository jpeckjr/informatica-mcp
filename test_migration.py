"""Unit tests for migration.py — no tenant required.

    python -m unittest test_migration -v
"""

import os
import tempfile
import unittest
from pathlib import Path

import migration

MTT = "40I6QamRUa7jvIzYGBnYYL"          # the mapping task the user exported
TEMPLATE = "tmplShared000000000000"      # shared mapping template (dependency)
CONN = "connSalesforce0000000000"
FOLDER = "folderSalesforce00000000"

MANIFEST = {
    "requested_ids": [MTT],
    "objects": [
        {"id": MTT, "name": "mt_stage_salesforce_contract",
         "path": "/staging/salesforce", "type": "MTT"},
        {"id": TEMPLATE, "name": "mp_template_stage_salesforce",
         "path": "/staging/templates", "type": "DTEMPLATE"},
        {"id": CONN, "name": "cn_app_salesforce", "path": None,
         "type": "SAAS_CONNECTION"},
        {"id": FOLDER, "name": "salesforce", "path": "/staging", "type": "Folder"},
    ],
}


class ImportSpecTests(unittest.TestCase):
    def test_default_overwrites_only_requested_asset(self):
        spec = migration.build_import_specification(MANIFEST)
        self.assertEqual(spec["defaultConflictResolution"], "REUSE")
        over = [s["sourceObjectId"] for s in spec["objectSpecification"]]
        self.assertEqual(over, [MTT])          # template/connection NOT overwritten

    def test_mapping_task_keeps_prod_persisted_values(self):
        spec = migration.build_import_specification(MANIFEST)
        flags = {f["key"]: f["value"]
                 for f in spec["objectSpecification"][0]["additionalProviderFlags"]}
        self.assertEqual(flags["mapping_conflict_policy_existing"], "RETAIN")
        self.assertEqual(flags["mapping_conflict_policy_new"], "RESET")

    def test_can_opt_into_replacing_persisted_values(self):
        spec = migration.build_import_specification(MANIFEST,
                                                    keep_persisted_values=False)
        flags = {f["key"]: f["value"]
                 for f in spec["objectSpecification"][0]["additionalProviderFlags"]}
        self.assertEqual(flags["mapping_conflict_policy_existing"], "REPLACE")

    def test_explicit_overwrite_of_template_allowed(self):
        spec = migration.build_import_specification(MANIFEST, [MTT, TEMPLATE])
        self.assertEqual({s["sourceObjectId"] for s in spec["objectSpecification"]},
                         {MTT, TEMPLATE})

    def test_refuses_ids_not_in_package(self):
        with self.assertRaises(ValueError):
            migration.build_import_specification(MANIFEST, ["notInPackage"])

    def test_refuses_overwriting_connections_and_folders(self):
        for oid in (CONN, FOLDER):
            with self.assertRaises(ValueError):
                migration.build_import_specification(MANIFEST, [oid])

    def test_plan_lists_overwrite_first(self):
        spec = migration.build_import_specification(MANIFEST)
        plan = migration.import_plan(MANIFEST, spec)
        self.assertEqual(plan[0]["id"], MTT)
        self.assertEqual(plan[0]["action"], "OVERWRITE")
        self.assertTrue(all(r["action"].startswith("REUSE") for r in plan[1:]))


class TargetOrgTests(unittest.TestCase):
    DEV, PROD = "001K5P", "001KVB"

    def test_promotion_dev_to_prod_allowed(self):
        self.assertIsNone(migration.check_target_org(
            {"source_org_id": self.DEV}, self.PROD, rollback=False))

    def test_promotion_back_into_source_org_refused(self):
        msg = migration.check_target_org(
            {"source_org_id": self.PROD}, self.PROD, rollback=False)
        self.assertIn("rollback=True", msg)

    def test_rollback_into_same_org_allowed(self):
        self.assertIsNone(migration.check_target_org(
            {"source_org_id": self.PROD}, self.PROD, rollback=True))

    def test_rollback_from_other_org_refused(self):
        self.assertIsNotNone(migration.check_target_org(
            {"source_org_id": self.DEV}, self.PROD, rollback=True))

    def test_unknown_org_refused(self):
        self.assertIsNotNone(migration.check_target_org({}, self.PROD, False))


class PackagePathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        (self.base / "dev_job_1.zip").write_bytes(b"PK")

    def tearDown(self):
        self.tmp.cleanup()

    def test_bare_name_resolves_inside_base(self):
        p = migration.safe_package_path("dev_job_1.zip", self.base)
        self.assertEqual(p, (self.base / "dev_job_1.zip").resolve())

    def test_refuses_path_outside_base(self):
        outside = Path(self.tmp.name).parent / "secret.zip"
        with self.assertRaises(ValueError):
            migration.safe_package_path(str(outside), self.base)
        with self.assertRaises(ValueError):
            migration.safe_package_path("../secret.zip", self.base)

    def test_refuses_non_zip(self):
        (self.base / ".env").write_text("x")
        with self.assertRaises(ValueError):
            migration.safe_package_path(".env", self.base)

    def test_manifest_round_trip(self):
        zp = self.base / "dev_job_1.zip"
        migration.write_manifest(zp, {"requested_ids": [MTT]})
        self.assertEqual(migration.read_manifest(zp)["requested_ids"], [MTT])
        self.assertEqual(oct(os.stat(migration.manifest_path(zp)).st_mode & 0o777),
                         "0o600")

    def test_missing_manifest_refused(self):
        with self.assertRaises(FileNotFoundError):
            migration.read_manifest(self.base / "dev_job_1.zip")


class NamingTests(unittest.TestCase):
    def test_package_file_name_is_safe(self):
        self.assertEqual(
            migration.package_file_name("dev", "mcp export / contract", "ABC"),
            "dev_mcp_export_contract_ABC.zip")


if __name__ == "__main__":
    unittest.main()
