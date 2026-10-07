"""Reconcile through the Keycloak HTTP API; observe persistent receiving state."""
import importlib.util
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class ReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.state = {"clients": {}, "users": {}, "roles": {}, "mutations": []}
        self.state['basic'] = {'id': 'native-basic-id', 'name': 'basic', 'protocol': 'openid-connect',
            'attributes': {'include.in.token.scope': 'false', 'display.on.consent.screen': 'false'},
            'protocolMappers': [
                {'name': 'auth_time', 'protocol': 'openid-connect', 'protocolMapper': 'oidc-usersessionmodel-note-mapper',
                 'config': {'user.session.note': 'AUTH_TIME', 'id.token.claim': 'true', 'introspection.token.claim': 'true',
                            'access.token.claim': 'true', 'claim.name': 'auth_time', 'jsonType.label': 'long'}},
                {'name': 'sub', 'protocol': 'openid-connect', 'protocolMapper': 'oidc-sub-mapper',
                 'config': {'introspection.token.claim': 'true', 'access.token.claim': 'true'}}]}
        state = self.state
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def dispatch(self):
                route = self.path.split('?')[0].removeprefix('/admin/realms/test/')
                body_length = int(self.headers.get('Content-Length', '0'))
                body = json.loads(self.rfile.read(body_length)) if body_length else None
                method = self.command
                status, out = 200, None
                if method == 'GET' and route == 'client-scopes':
                    out = [state['basic']]
                elif method == 'GET' and route == 'clients':
                    from urllib.parse import parse_qs,urlsplit
                    name = parse_qs(urlsplit(self.path).query)['clientId'][0]
                    out = [c for c in state['clients'].values() if c['clientId']==name]
                elif method == 'GET' and route.startswith('roles/'):
                    out = state['roles'].get(route.split('/')[-1]); status = 200 if out else 404
                elif method == 'GET' and route == 'users':
                    from urllib.parse import parse_qs, urlsplit
                    name = parse_qs(urlsplit(self.path).query)['username'][0]
                    out = [u for u in state['users'].values() if u.get('username') == name]
                elif method == 'GET' and route.startswith('users/'):
                    uid = route.split('/')[1]
                    if route.endswith('/role-mappings'): out = state['users'][uid].get('mappings',{})
                    elif route.endswith('/groups'): out = []
                    else: out = state['users'].get(uid); status = 200 if out else 404
                elif method == 'GET' and route.endswith('/service-account-user'):
                    cid=route.split('/')[1];out=next((u for u in state['users'].values() if u.get('clientLink')==cid),None); status=200 if out else 404
                elif method == 'GET' and ('client-scopes' in route or 'protocol-mappers' in route or 'scope-mappings' in route): out=[]
                elif method == 'POST' and route == 'roles':
                    if body['name'] in state['roles']: status=409
                    else: state['roles'][body['name']]={'id':'role-'+body['name'],**body};status=201
                elif method == 'POST' and route == 'clients':
                    cid='id-'+body['clientId']; state['clients'][cid]={'id':cid,**body};status=201
                elif method == 'PUT' and '/default-client-scopes/' in route:
                    status = 204
                elif method == 'PUT' and route.startswith('clients/'):
                    state['clients'][route.split('/')[1]].update(body);status=204
                elif method == 'POST' and route == 'partialImport':
                    user=body['users'][0];cid=next(c['id'] for c in state['clients'].values() if c['clientId']==user['serviceAccountClientId']);state['users'][user['id']]={**user,'serviceAccountClientId':cid,'clientLink':cid};out={'success':True}
                elif method == 'POST' and '/role-mappings/realm' in route:
                    uid=route.split('/')[1];state['users'][uid].setdefault('mappings',{})['realmMappings']=body;status=204
                elif method == 'DELETE' and '/role-mappings/' in route:
                    uid=route.split('/')[1]
                    if route.endswith('/realm'):state['users'][uid].setdefault('mappings',{})['realmMappings']=[]
                    else:state['users'][uid].setdefault('mappings',{})['clientMappings']={}
                    status=204
                elif method in ('POST','DELETE') and ('protocol-mappers' in route or 'scope-mappings' in route):status=204
                else:status=400;out={'error':'unsupported test route'}
                if method != 'GET':state['mutations'].append((method,route))
                encoded=json.dumps(out).encode() if out is not None else b''
                self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(encoded)));self.end_headers();self.wfile.write(encoded)
            do_GET=dispatch;do_POST=dispatch;do_PUT=dispatch;do_DELETE=dispatch
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        path=ROOT/'files/keycloak-reconcile-task-identities.py'
        spec=importlib.util.spec_from_file_location('task_reconcile',path);self.module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.module)
        self.task={'key':'CONFIG_WIZARD','clientId':'backend-config-wizard','subject':'11111111-1111-4111-8111-111111111111','secret':'synthetic-test-credential','roles':['config-wizard'],'audiences':['tenantservice','agencyservice','consultingtypeservice']}
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join()
    def run_reconcile(self,tasks=None):
        self.module.reconcile('http://127.0.0.1:'+str(self.server.server_port),'test','synthetic-install-admin-token',tasks or [self.task])
    def test_fresh_and_repeat_converge_with_no_native_management_power(self):
        self.run_reconcile();cid='id-backend-config-wizard';user=self.state['users'][self.task['subject']]
        self.assertEqual([x['name'] for x in user['mappings']['realmMappings']],['config-wizard'])
        self.assertFalse(self.state['clients'][cid]['fullScopeAllowed']);self.assertFalse(self.state['clients'][cid]['directAccessGrantsEnabled'])
        first=json.loads(json.dumps(self.state['users']));self.run_reconcile();self.assertEqual(first,self.state['users'])
        self.assertFalse(any('realm-management' in route for _,route in self.state['mutations']))
    def test_shared_realm_role_is_created_once_for_multiple_task_clients(self):
        first={**self.task,'roles':['account-read']}
        second={**first,'key':'MAINTENANCE','clientId':'backend-maintenance',
                'subject':'22222222-2222-4222-8222-222222222222','secret':'independent-synthetic-credential'}
        self.run_reconcile([first,second])
        for task in [first,second]:
            mappings=self.state['users'][task['subject']]['mappings']['realmMappings']
            self.assertEqual(['account-read'],[role['name'] for role in mappings])

    def test_existing_task_loses_unwanted_direct_and_management_roles(self):
        self.run_reconcile();user=self.state['users'][self.task['subject']];user['mappings']={'realmMappings':[{'id':'broad','name':'technical'}],'clientMappings':{'realm-management':{'id':'management-client','mappings':[{'id':'manage','name':'manage-users'}]}}}
        self.run_reconcile();self.assertEqual([x['name'] for x in user['mappings']['realmMappings']],['config-wizard']);self.assertEqual(user['mappings'].get('clientMappings',{}),{})
    def test_legacy_retirement_requires_every_pinned_owner_before_any_mutation(self):
        api = self.module.KeycloakApi('http://127.0.0.1:' + str(self.server.server_port), 'test', 'installer')
        retirement = [{'username': 'technical', 'subject': '44444444-4444-4444-8444-444444444444'}]
        self.state['users'][retirement[0]['subject']] = {'id': retirement[0]['subject'], 'username': 'ordinary-human'}
        with self.assertRaises(self.module.ReconcileError):
            self.module.prepare_legacy_retirement(api, retirement, 'installer', [self.task])
        self.assertEqual([], self.state['mutations'])

    def test_inherited_basic_scope_mapper_drift_fails_before_mutation(self):
        self.state['basic']['protocolMappers'].append({'name': 'hidden-admin', 'protocolMapper': 'oidc-usermodel-realm-role-mapper'})
        with self.assertRaises(self.module.ReconcileError):
            self.run_reconcile()
        self.assertEqual([], self.state['mutations'])

    def test_preexisting_username_collision_cannot_create_or_mutate_a_client(self):
        self.state['users']['human-id'] = {'id': 'human-id', 'username': 'service-account-backend-config-wizard'}
        with self.assertRaises(self.module.ReconcileError):
            self.run_reconcile()
        self.assertEqual([], self.state['mutations'])

    def test_subject_collision_and_duplicate_credentials_fail_before_mutation(self):
        self.state['users'][self.task['subject']]={'id':self.task['subject'],'username':'ordinary-human'}
        with self.assertRaises(self.module.ReconcileError):self.run_reconcile()
        self.assertEqual(self.state['mutations'],[])
        self.state['users'].clear()
        other={**self.task,'key':'OTHER','clientId':'backend-other','subject':'22222222-2222-4222-8222-222222222222'}
        with self.assertRaises(self.module.ReconcileError):self.run_reconcile([self.task,other])
        self.assertEqual(self.state['mutations'],[])

if __name__=='__main__':unittest.main()
