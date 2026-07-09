import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "seed-keycloak-users.sh"
README = ROOT / "test-data" / "README.md"


class SeedKeycloakUsersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = SCRIPT.read_text(encoding="utf-8")
        cls.readme = README.read_text(encoding="utf-8")

    def test_write_back_uses_store_filename_for_sops_creation_rules(self):
        self.assertIn('sops --filename-override "$STORE" -e "$tmp" > "$out"', self.script)
        self.assertNotIn('sops -e "$tmp" > "$STORE"', self.script)

    def test_write_back_is_atomic(self):
        self.assertIn('out="$(mktemp "${store_dir}/.$(basename "$STORE").XXXXXX")"', self.script)
        self.assertIn("trap 'rm -f \"$tmp\" \"${tmp}.new\" \"$out\"' EXIT", self.script)
        self.assertIn('mv "$out" "$STORE"', self.script)
        self.assertNotIn('> "$STORE"', self.script)
        self.assertNotIn("RETURN", self.script)

    def test_write_back_log_only_when_credentials_are_written(self):
        self.assertIn("written=0", self.script)
        self.assertIn("written=$((written + 1))", self.script)
        self.assertIn('[[ "$written" -gt 0 ]] && log "credentials written', self.script)

    def test_readme_uses_filename_override_for_initial_store_creation(self):
        self.assertIn(
            "sops --filename-override test-data/test-users.enc.json -e /tmp/seed.json",
            self.readme,
        )


if __name__ == "__main__":
    unittest.main()
