import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from gnucash_to_wealthfolio.editor_server import create_editor_server
from gnucash_to_wealthfolio.parser import parse_gnucash_file


class TestEditorApi(unittest.TestCase):
    def setUp(self):
        fixture_dir = Path(__file__).parent / "fixtures"
        sample_gnc = fixture_dir / "sample_book.gnucash"
        sample_db = fixture_dir / "sample_wealthfolio.db"

        self.book = parse_gnucash_file(sample_gnc)
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="wf_api_test_"))
        self.mapping_path = self.tmp_dir / "mapping.json"
        self.source_db = sample_db
        self.target_db = self.tmp_dir / "target.db"
        self.backup_dir = self.tmp_dir / "backups"

        # Start editor server on free ephemeral port (port 0)
        self.server = create_editor_server(
            self.book,
            self.mapping_path,
            self.source_db,
            self.target_db,
            self.backup_dir,
            port=0,
        )
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"

        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_onboarding_status_and_initialize(self):
        # 1. Onboarding status before file exists
        req = urllib.request.Request(f"{self.base_url}/api/onboarding/status")
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data["is_first_launch"])
            self.assertFalse(data["mapping_exists"])

        # 2. Trigger auto-initialize
        post_data = json.dumps({"currency": "USD"}).encode("utf-8")
        post_req = urllib.request.Request(
            f"{self.base_url}/api/onboarding/initialize",
            data=post_data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(post_req) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(res["status"], "ok")
            self.assertGreater(res["accounts_mapped"], 0)

        # 3. Status should now report first_launch=False and mapping_exists=True
        with urllib.request.urlopen(req) as resp:
            data2 = json.loads(resp.read().decode("utf-8"))
            self.assertFalse(data2["is_first_launch"])
            self.assertTrue(data2["mapping_exists"])
            self.assertEqual(data2["currency"], "USD")

    def test_mapping_export_import_and_backups(self):
        # Initialize mapping first
        post_data = json.dumps({"currency": "AUD"}).encode("utf-8")
        post_req = urllib.request.Request(
            f"{self.base_url}/api/onboarding/initialize",
            data=post_data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(post_req) as resp:
            self.assertEqual(resp.status, 200)

        # 1. Export mapping
        with urllib.request.urlopen(f"{self.base_url}/api/mapping/export") as resp:
            self.assertEqual(resp.status, 200)
            exported_content = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(exported_content["version"], 2)

        # 2. Check mapping backups endpoint
        with urllib.request.urlopen(f"{self.base_url}/api/backups/mappings") as resp:
            b_data = json.loads(resp.read().decode("utf-8"))
            self.assertIn("backups", b_data)

        # 3. Import modified mapping
        exported_content["metadata"]["currency"] = "EUR"
        import_req = urllib.request.Request(
            f"{self.base_url}/api/mapping/import",
            data=json.dumps({"mapping": exported_content}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(import_req) as resp:
            imp_res = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(imp_res["status"], "ok")

        # 4. Verify reloaded mapping has EUR
        with urllib.request.urlopen(f"{self.base_url}/api/onboarding/status") as resp:
            s_data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(s_data["currency"], "EUR")

    def test_paths_in_state(self):
        req = urllib.request.Request(f"{self.base_url}/api/state")
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            self.assertIn("paths", data)
            paths = data["paths"]
            self.assertIn("gnucash", paths)
            self.assertIn("mapping", paths)
            self.assertIn("wealthfolio_db", paths)

    def test_mapping_backup_create_and_restore(self):
        # 1. Initialize mapping
        post_data = json.dumps({"currency": "AUD"}).encode("utf-8")
        post_req = urllib.request.Request(
            f"{self.base_url}/api/onboarding/initialize",
            data=post_data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(post_req) as resp:
            self.assertEqual(resp.status, 200)

        # 2. Create named snapshot
        create_data = json.dumps({"label": "test_checkpoint"}).encode("utf-8")
        create_req = urllib.request.Request(
            f"{self.base_url}/api/backups/mappings/create",
            data=create_data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(create_req) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(res["status"], "ok")
            snapshot_filename = res["filename"]
            self.assertIn("test_checkpoint", snapshot_filename)

        # 3. Modify current mapping
        with urllib.request.urlopen(f"{self.base_url}/api/mapping/export") as resp:
            cfg_dict = json.loads(resp.read().decode("utf-8"))
        cfg_dict["metadata"]["currency"] = "CAD"
        import_req = urllib.request.Request(
            f"{self.base_url}/api/mapping/import",
            data=json.dumps({"mapping": cfg_dict}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(import_req) as resp:
            self.assertEqual(resp.status, 200)

        # Verify it changed to CAD
        with urllib.request.urlopen(f"{self.base_url}/api/onboarding/status") as resp:
            self.assertEqual(json.loads(resp.read().decode("utf-8"))["currency"], "CAD")

        # 4. Restore the snapshot (which was AUD)
        restore_data = json.dumps({"filename": snapshot_filename}).encode("utf-8")
        restore_req = urllib.request.Request(
            f"{self.base_url}/api/backups/mappings/restore",
            data=restore_data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(restore_req) as resp:
            restore_res = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(restore_res["status"], "ok")

        # Verify restored back to AUD
        with urllib.request.urlopen(f"{self.base_url}/api/onboarding/status") as resp:
            self.assertEqual(json.loads(resp.read().decode("utf-8"))["currency"], "AUD")


if __name__ == "__main__":
    unittest.main()

