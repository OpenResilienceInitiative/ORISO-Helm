import unittest
from realm_contract_fixture import rendered_realm

class BackendServiceClients(unittest.TestCase):
    def test_legacy_export_preserves_ids_without_enabling_shared_privileges(self):
        realm = rendered_realm()
        clients = {c['clientId']: c for c in realm['clients']}
        users = {u.get('serviceAccountClientId'): u for u in realm['users']}
        for name, subject in {'backend-technical':'12316d09-a9da-41b9-a13e-ee2c515800b5',
                              'backend-admin':'615a7bf8-3e12-40c7-a949-f88640acea8e'}.items():
            with self.subTest(client=name):
                self.assertFalse(clients[name]['enabled'])
                for field in ('publicClient','directAccessGrantsEnabled','standardFlowEnabled','implicitFlowEnabled','fullScopeAllowed'):
                    self.assertFalse(clients[name][field])
                user=users[name]
                self.assertEqual(subject,user['id'])
                self.assertFalse(user['enabled'])
                self.assertFalse(user.get('realmRoles'))
                self.assertFalse(user.get('clientRoles'))
                self.assertFalse(user.get('credentials'))
                self.assertFalse([m for m in realm['scopeMappings'] if m.get('client')==name])
                self.assertFalse([m for m in realm['clientScopeMappings'].get('realm-management',[]) if m.get('client')==name])
        self.assertTrue(clients['app']['publicClient'])
        self.assertTrue(clients['app']['standardFlowEnabled'])
        for user in realm['users']:
            if user['username'] in ('technical','svc-keycloak-admin'):
                self.assertFalse(user['enabled'])
                self.assertFalse(user.get('credentials'))

if __name__ == '__main__':
    unittest.main()
