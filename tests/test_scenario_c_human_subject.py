"""The real app access token must carry the native persistent user subject."""

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class HumanSubjectMappingTest(unittest.TestCase):
    def test_app_default_scope_maps_native_subject_into_access_token(self):
        realm = json.loads(
            (ROOT / "charts/keycloak/keycloak-resources/realm.json").read_text()
        )
        apps = [client for client in realm["clients"] if client["clientId"] == "app"]
        self.assertEqual(1, len(apps))
        app = apps[0]
        self.assertIs(True, app["enabled"])
        self.assertIn("app-custom", app["defaultClientScopes"])
        effective = list(app.get("protocolMappers", []))
        for scope_name in app["defaultClientScopes"]:
            scopes = [scope for scope in realm["clientScopes"] if scope["name"] == scope_name]
            self.assertEqual(1, len(scopes))
            effective.extend(scopes[0].get("protocolMappers", []))
        subject = [mapper for mapper in effective
                   if mapper["protocolMapper"] == "oidc-sub-mapper"]
        self.assertEqual(1, len(subject), "app requires exactly one native subject mapper")
        self.assertEqual("openid-connect", subject[0]["protocol"])
        self.assertIs(False, subject[0]["consentRequired"])
        self.assertEqual("true", subject[0]["config"].get("access.token.claim"))


if __name__ == "__main__":
    unittest.main()
