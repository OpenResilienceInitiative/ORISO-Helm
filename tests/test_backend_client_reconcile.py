"""The superseded two-client administrator installer must not be shipped."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]

class RetiredAdministratorInstallerTest(unittest.TestCase):
    def test_retired_installer_cannot_restore_shared_admin_privileges(self):
        self.assertFalse((ROOT / 'files/keycloak-reconcile-service-clients.py').exists())
        hooks = list((ROOT / 'templates').glob('*service-identities*'))
        self.assertTrue(hooks)
        for hook in hooks:
            self.assertNotIn('keycloak-reconcile-service-clients.py', hook.read_text())

if __name__ == '__main__':
    unittest.main()
