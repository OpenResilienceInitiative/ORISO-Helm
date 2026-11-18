"""Helm#535: realm import and existing-realm reconcile agree on the hardening."""
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REALM = json.loads((ROOT / "charts/keycloak/keycloak-resources/realm.json").read_text(encoding="utf-8"))
spec = importlib.util.spec_from_file_location("service_clients", ROOT / "files/keycloak-reconcile-service-clients.py")
service_clients = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service_clients)


class KeycloakRealmHardeningContractTest(unittest.TestCase):
    def test_published_secret_client_is_not_imported(self):
        name, secret = service_clients.PUBLISHED_CLIENT
        self.assertNotIn(name, {c["clientId"] for c in REALM["clients"]})
        self.assertNotIn(secret, json.dumps(REALM))

    def test_events_match_the_reconciled_config(self):
        for key, value in service_clients.EVENTS_CONFIG.items():
            self.assertEqual(REALM[key], value, key)

    def test_events_are_bounded_and_admin_details_are_off(self):
        self.assertTrue(REALM["eventsEnabled"])
        self.assertGreater(REALM["eventsExpiration"], 0)
        self.assertTrue(REALM["enabledEventTypes"])
        self.assertTrue(REALM["adminEventsEnabled"])
        self.assertFalse(REALM["adminEventsDetailsEnabled"])
        # eventsExpiration covers login events only; admin events need the realm attribute.
        self.assertEqual(REALM["attributes"]["adminEventsExpiration"], service_clients.ADMIN_EVENTS_EXPIRATION)

    def test_access_token_lifespan_stays_until_frontend_refresh_is_fixed(self):
        # A shorter lifespan logs out background tabs; see Helm#535 "Out of scope".
        self.assertEqual(REALM["accessTokenLifespan"], 18000)


if __name__ == "__main__":
    unittest.main()
