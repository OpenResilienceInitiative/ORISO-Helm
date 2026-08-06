import json
import unittest
from pathlib import Path


REALM_PATH = (
    Path(__file__).resolve().parents[1]
    / "charts"
    / "keycloak"
    / "keycloak-resources"
    / "realm.json"
)


class KeycloakOtpAdminClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.realm = json.loads(REALM_PATH.read_text())

    def test_admin_cli_access_tokens_keep_identity_and_roles_for_otp_spi(self):
        admin_cli = next(
            client
            for client in self.realm["clients"]
            if client["clientId"] == "admin-cli"
        )

        self.assertEqual(
            "false",
            admin_cli["attributes"].get(
                "client.use.lightweight.access.token.enabled"
            ),
            "The OTP SPI authenticates the technical user and checks its realm role. "
            "Lightweight admin-cli tokens omit both and make consultant OTP lookups "
            "fail with HTTP 401.",
        )


if __name__ == "__main__":
    unittest.main()
