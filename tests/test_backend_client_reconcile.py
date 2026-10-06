"""Exercise the deployment helper through its real HTTP/CLI boundary."""
import base64
import time
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'files/keycloak-reconcile-service-clients.py'

class ReconcileClientsTest(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.clients = {'realm-management': {'id':'realm-management','clientId':'realm-management'}}
        self.users = {'legacy-tech': {'username':'technical','enabled':True},
                      'legacy-admin': {'username':'svc-keycloak-admin','enabled':True},
                      'recovery': {'username':'realmadmin','enabled':True}}
        self.role_maps = {}
        self.scope_maps = {}
        self.fail_mapping = False
        self.bad_claims = {}
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self): self.handle_api()
            def do_POST(self): self.handle_api()
            def do_PUT(self): self.handle_api()
            def do_DELETE(self): self.handle_api()
            def handle_api(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                path = urlsplit(self.path).path
                payload = json.loads(body) if body and self.headers.get('Content-Type') == 'application/json' else None
                outer.events.append((self.command, path, payload))
                status, answer = 200, None
                if path.endswith('/protocol/openid-connect/token'):
                    values = parse_qs(body.decode())
                    if values.get('grant_type') == ['client_credentials']:
                        client=values['client_id'][0]
                        if values.get('client_secret') != [outer.clients.get(client,{}).get('secret')]: status=401
                        else:
                            roles=outer.role_maps['sa-'+client]
                            claims={'sub':'sa-'+client,'azp':client,'exp':time.time()+60,'realm_access':{'roles':[r['name'] for r in roles['realmMappings']]},'resource_access':{k:{'roles':[r['name'] for r in v['mappings']]} for k,v in roles['clientMappings'].items()}}
                            claims.update(outer.bad_claims)
                            payload=base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
                            answer={'access_token':'header.'+payload+'.fixture-signature'}
                    elif values.get('client_id') != ['admin-cli'] or values.get('username') != ['master-recovery']:
                        status = 403
                    else: answer = {'access_token': 'fixture-master-session'}
                elif '/admin/realms/online-beratung/' in path:
                    key = path.split('/admin/realms/online-beratung/')[1]
                    parts = key.split('/')
                    if key == 'clients':
                        if self.command == 'GET':
                            client_id = parse_qs(urlsplit(self.path).query).get('clientId', [''])[0]
                            answer = [v for v in outer.clients.values() if v['clientId'] == client_id]
                        else:
                            cid = payload['clientId']
                            outer.clients[cid] = {'id':cid, **payload}
                            outer.users['sa-'+cid] = {'id':'sa-'+cid,'username':'service-account-'+cid,'enabled':True}
                            status = 201
                    elif key == 'users':
                        username = parse_qs(urlsplit(self.path).query).get('username', [''])[0]
                        answer = [{'id':uid, **value} for uid,value in outer.users.items() if value['username']==username]
                    elif parts[0] == 'clients' and len(parts) == 2:
                        if self.command == 'PUT': outer.clients[parts[1]].update(payload); status = 204
                        else: answer = outer.clients[parts[1]]
                    elif parts[0] == 'clients' and parts[2] == 'service-account-user':
                        answer = {'id':'sa-'+parts[1], **outer.users['sa-'+parts[1]]}
                    elif parts[0] == 'roles':
                        answer = {'id':parts[1], 'name':parts[1]}
                    elif parts[0] == 'clients' and parts[2] == 'roles':
                        answer = {'id':parts[3], 'name':parts[3]}
                    elif parts[0] == 'users' and len(parts)==2 and self.command=='PUT':
                        outer.users[parts[1]].update(payload); status=204
                    elif parts[0]=='users' and parts[2]=='groups': answer=[]
                    elif parts[0]=='users' and parts[2]=='credentials': answer=[]
                    elif parts[0]=='users' and parts[2]=='logout': status=204
                    elif 'role-mappings' in parts or 'scope-mappings' in parts:
                        mapping = outer.role_maps if parts[0]=='users' else outer.scope_maps
                        owner = parts[1]
                        if len(parts)==3: answer = mapping.get(owner, {'realmMappings': [],'clientMappings':{}})
                        elif self.command=='POST':
                            if outer.fail_mapping: status=500
                            else:
                                m=mapping.setdefault(owner, {'realmMappings':[],'clientMappings':{}})
                                if parts[3]=='realm': m['realmMappings']=payload
                                else: m['clientMappings'][parts[4]]={'id':parts[4],'mappings':payload}
                                status=204
                        elif self.command=='DELETE': status=204
                    elif parts[0]=='clients' and parts[2] in ('default-client-scopes','optional-client-scopes'): answer=[]
                    else: status=404
                else: status=404
                raw = json.dumps(answer).encode() if answer is not None else b''
                self.send_response(status); self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(raw)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self): self.server.shutdown();self.server.server_close();self.thread.join()
    def run_helper(self, **overrides):
        env={**os.environ,'KEYCLOAK_URL':f'http://127.0.0.1:{self.server.server_port}',
             'POD_NAMESPACE':'fixture','KEYCLOAK_REALM':'online-beratung',
             'KEYCLOAK_ADMIN_USERNAME':'master-recovery','KEYCLOAK_ADMIN_PASSWORD':'synthetic-master-canary',
             'TECHNICAL_CLIENT_ID':'backend-technical','ADMIN_CLIENT_ID':'backend-admin',
             'HUMAN_CLIENT_ID':'app','TECHNICAL_CLIENT_SECRET':'technical-fixture-secret-32-characters',
             'ADMIN_CLIENT_SECRET':'admin-fixture-secret-32-characters',
             'ADMIN_SERVICE_SUBJECT':'sa-backend-admin','TECHNICAL_SERVICE_SUBJECT':'sa-backend-technical','PREPARE_ONLY':'false',
             'RETIRE_LEGACY_USERS':'false','LEGACY_TECHNICAL_USERNAME':'technical',
             'LEGACY_ADMIN_USERNAME':'svc-keycloak-admin','BOOTSTRAP_ADMIN_USERNAME':'realmadmin',**overrides}
        return subprocess.run(['python3',str(SCRIPT)],env=env,capture_output=True,text=True)
    def prepare(self):
        result=self.run_helper(PREPARE_ONLY='true')
        self.assertEqual(result.returncode,0,result.stderr)
        return result
    def mutations(self): return [e for e in self.events if e[0]!='GET' and '/admin/realms/' in e[1]]
    def test_prepare_creates_separate_confidential_clients_and_keeps_old_users(self):
        result=self.prepare()
        for cid in ('backend-technical','backend-admin'):
            c=self.clients[cid]; self.assertFalse(c['publicClient']);self.assertTrue(c['serviceAccountsEnabled'])
            for key in ('directAccessGrantsEnabled','standardFlowEnabled','implicitFlowEnabled','fullScopeAllowed'): self.assertFalse(c[key])
        self.assertIn('sa-backend-technical',result.stdout)
        self.assertTrue(self.users['legacy-tech']['enabled']);self.assertTrue(self.users['legacy-admin']['enabled'])
    def test_missing_clients_fail_before_changes_outside_prepare(self):
        result=self.run_helper();self.assertNotEqual(result.returncode,0);self.assertEqual(self.mutations(),[])
    def test_wrong_subject_fails_before_any_mutation(self):
        self.prepare();self.events.clear()
        result=self.run_helper(TECHNICAL_SERVICE_SUBJECT='wrong')
        self.assertNotEqual(result.returncode,0);self.assertEqual(self.mutations(),[])
    def test_human_or_built_in_client_and_equal_secrets_are_rejected(self):
        for values in ({'TECHNICAL_CLIENT_ID':'app'},{'ADMIN_CLIENT_ID':'admin-cli'},
                       {'ADMIN_CLIENT_SECRET':'technical-fixture-secret-32-characters'}):
            self.events.clear();result=self.run_helper(**values)
            self.assertNotEqual(result.returncode,0);self.assertEqual(self.events,[])
    def test_existing_public_client_is_never_converted(self):
        self.clients['backend-technical']={'id':'backend-technical','clientId':'backend-technical','publicClient':True}
        result=self.run_helper(PREPARE_ONLY='true');self.assertNotEqual(result.returncode,0);self.assertEqual(self.mutations(),[])
    def test_exact_role_scope_and_explicit_retirement_order(self):
        self.prepare();self.events.clear()
        self.role_maps['sa-backend-technical']={'realmMappings': [],'clientMappings':{}}
        result=self.run_helper(RETIRE_LEGACY_USERS='true');self.assertEqual(result.returncode,0,result.stderr)
        tech=self.role_maps['sa-backend-technical'];self.assertEqual([r['name'] for r in tech['realmMappings']],['technical']);self.assertEqual(tech['clientMappings'],{})
        admin=self.role_maps['sa-backend-admin'];self.assertEqual([r['name'] for r in admin['realmMappings']],['otp-config-admin'])
        self.assertEqual({r['name'] for r in admin['clientMappings']['realm-management']['mappings']},{'manage-users','view-users','query-users','view-realm'})
        self.assertEqual(self.scope_maps['backend-technical'],tech)
        self.assertFalse(self.users['legacy-tech']['enabled']);self.assertFalse(self.users['legacy-admin']['enabled']);self.assertTrue(self.users['recovery']['enabled'])
        disable=[i for i,e in enumerate(self.events) if e[2] and isinstance(e[2],dict) and e[2].get('enabled') is False]
        mapping=[i for i,e in enumerate(self.events) if e[0]=='POST' and 'mappings' in e[1]]
        self.assertGreater(min(disable),max(mapping))
    def test_role_failure_preserves_legacy_accounts_and_scrubs_credentials(self):
        self.prepare();self.role_maps['sa-backend-technical']={'realmMappings': [],'clientMappings':{}};self.fail_mapping=True
        result=self.run_helper(RETIRE_LEGACY_USERS='true');self.assertNotEqual(result.returncode,0)
        self.assertTrue(self.users['legacy-tech']['enabled'])
        for secret in ('technical-fixture-secret-32-characters','admin-fixture-secret-32-characters','synthetic-master-canary'):
            self.assertNotIn(secret,result.stdout+result.stderr)
    def test_admin_pin_mismatch_prevents_all_mutations(self):
        self.prepare();self.events.clear()
        result=self.run_helper(ADMIN_SERVICE_SUBJECT='wrong-admin')
        self.assertNotEqual(result.returncode,0);self.assertEqual(self.mutations(),[])
    def test_bad_token_contract_prevents_legacy_retirement(self):
        for wrong in ({'sub':'wrong'}, {'azp':'app'}, {'exp':0}, {'realm_access':{'roles':['technical','tenant-admin']}}, {'resource_access':{'realm-management':{'roles':['realm-admin']}}}):
            self.prepare();self.bad_claims=wrong
            result=self.run_helper(RETIRE_LEGACY_USERS='true')
            self.assertNotEqual(result.returncode,0);self.assertIn('TOKEN_CONTRACT_MISMATCH',result.stderr)
            self.assertTrue(self.users['legacy-tech']['enabled']);self.assertTrue(self.users['legacy-admin']['enabled'])
            self.bad_claims={}
    def test_idempotent_reconcile(self):
        self.prepare();first=copy.deepcopy(self.clients)
        result=self.run_helper();self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(self.clients,first)

if __name__=='__main__': unittest.main()
