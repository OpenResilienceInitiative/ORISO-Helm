import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "demo-baseline"
MANIFEST = BASELINE / "manifest.json"
SYNC_SQL = BASELINE / "demo-baseline-sync.sql"
CHECK_SQL = BASELINE / "demo-baseline-check.sql"
GATE_SCRIPT = ROOT / "scripts" / "demo-baseline-gate.sh"


class DemoBaselineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        cls.sync_sql = SYNC_SQL.read_text(encoding="utf-8")
        cls.check_sql = CHECK_SQL.read_text(encoding="utf-8")
        cls.gate_script = GATE_SCRIPT.read_text(encoding="utf-8")

    def test_required_topics_are_synced_and_checked(self):
        for topic in self.manifest["topics"]:
            with self.subTest(topic=topic["id"]):
                self.assertIn(f"  {topic['id']},", self.sync_sql)
                self.assertIn(topic["name"]["de"], self.sync_sql)
                self.assertIn(topic["name"]["de"], self.check_sql)
                self.assertIn(topic["slug"], self.sync_sql)

    def test_visibility_checks_are_synced_checked_and_smoked(self):
        for check in self.manifest["visibilityChecks"]:
            with self.subTest(topic=check["topicId"]):
                self.assertIn(f"@demo_postcode := '{check['postcode']}'", self.sync_sql)
                self.assertIn(f"at.topic_id = {check['topicId']}", self.check_sql)
                self.assertIn(f"topic_id = {check['topicId']}", self.check_sql)
                self.assertIn('"visibilityChecks"', self.gate_script)
                self.assertIn('"topicId"', self.gate_script)
                self.assertIn('"postcode"', self.gate_script)
                self.assertIn('"consultingType"', self.gate_script)

    def test_agency_topic_sync_is_idempotent(self):
        inserts = re.findall(r"INSERT INTO agency_topic", self.sync_sql)
        duplicate_updates = re.findall(r"ON DUPLICATE KEY UPDATE", self.sync_sql)

        self.assertEqual(
            len(self.manifest["visibilityChecks"]),
            len(inserts),
            "Each visibility check should have one agency_topic upsert.",
        )
        self.assertGreaterEqual(
            len(duplicate_updates),
            len(self.manifest["visibilityChecks"]),
            "agency_topic upserts must not create duplicate rows on rerun.",
        )
        self.assertIn("UNIQUE KEY `uq_agency_topic`", (ROOT / "charts/mariadb/sql-schemas/agencyservice-schema.sql").read_text())
        self.assertIn("duplicate agency_topic rows", self.check_sql)

    def test_gate_exposes_one_command_all_mode(self):
        self.assertIn("all)", self.gate_script)
        self.assertIn("sync_baseline", self.gate_script)
        self.assertIn("check_baseline", self.gate_script)
        self.assertIn("smoke_baseline", self.gate_script)


if __name__ == "__main__":
    unittest.main()
