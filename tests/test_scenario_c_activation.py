"""Closed activation preflight, ownership and fail-safe controls; not runtime acceptance."""
import base64
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'files'))
import scenario_c_activation as activation
from test_scenario_c_chart_bindings import bindings, render

REGISTRY=json.loads((Path(__file__).resolve().parents[1]/'files/scenario-c-task-identities.json').read_text())

class ActivationControls(unittest.TestCase):
    def setUp(self):
        self.actors={actor['key']:actor for actor in REGISTRY['actors']}
        self.bindings={name:{'clientId':actor['clientId'],'serviceSubject':f'20000000-0000-4000-8000-{i:012d}'} for i,(name,actor) in enumerate(self.actors.items(),1)}
        self.value={'version':1,'realmId':'native-realm','realmName':'isolated','issuer':'https://auth.example/realms/isolated','contextKeyVersion':'1','actors':self.bindings,'ordinaryGroups':{'/human-roles/consultant':['consultant']},'ordinaryPolicy':{'native':{'realmId':'native-realm','issuer':'https://auth.example/realms/isolated'}},'protectedPolicy':{'realmId':'native-realm','realmName':'isolated','issuer':'https://auth.example/realms/isolated','actorSubjects':{n:b['serviceSubject'] for n,b in self.bindings.items()}},'providerJarSha256':{n:'a'*64 for n in ['oriso-account-lifecycle','otp-config','oriso-smtp-control']}}
        self.env={'ORISO_TASK_CONTEXT_KEY':base64.b64encode(bytes(range(32))).decode(),**{'IDENTITY_'+n+'_CLIENT_SECRET':'synthetic-'+n for n in self.actors}}
        self.clients={'/clients/'+n:{'enabled':False,'attributes':{'oriso-c.native-policy':'receiver-only-v1'}} for n in self.actors}
        self.writes=[]
        class Admin:
            def request(inner,method,path,body=None):
                if method=='GET' and path.endswith('/service-account-user'):
                    return {'id':self.bindings[path.split('/')[2]]['serviceSubject']}
                if method=='GET':return copy.deepcopy(self.clients[path])
                self.writes.append((path,copy.deepcopy(body)));self.clients[path].update(copy.deepcopy(body))
        self.admin=Admin()
        self.snapshot={'realm':{'id':'native-realm','realm':'isolated'},'subjects':{n:b['serviceSubject'] for n,b in self.bindings.items()},'pending':False,'receiver_policy_prepared':True,'permission_client':'permission-client','clients':{n:n for n in self.actors}}
        for name,replacement in [('inspect',lambda *a,**kw:self.snapshot),('permission_inventory',lambda *a:{n:{} for n in activation.POLICY_NAMES|activation.PERMISSION_NAMES}),('desired_policies',lambda *a:[]),('verify_activation_runtime',lambda *a:activation.activation_contract(self.value,self.actors))]:
            mock=patch.object(activation,name,replacement);mock.start();self.addCleanup(mock.stop)
    def run_activation(self):return activation.activate(self.admin,'http://isolated',self.actors,{},self.value,self.env)
    def test_success_then_repeat_preserves_receipts_subjects_and_secrets(self):
        self.assertEqual(15,self.run_activation());before=copy.deepcopy(self.clients);writes=len(self.writes)
        self.assertEqual(0,self.run_activation());self.assertEqual(before,self.clients);self.assertEqual(writes,len(self.writes))
        self.assertTrue(all(c['enabled'] for c in self.clients.values()))
        self.assertFalse(any('secret' in body for _,body in self.writes))
    def test_unavailable_actual_probe_disables_every_owned_staged_client(self):
        with patch.object(activation,'verify_activation_runtime',side_effect=activation.ReconcileError('unavailable')):
            with self.assertRaisesRegex(activation.ReconcileError,'owned staged clients disabled'):self.run_activation()
        self.assertTrue(all(c['enabled'] is False and c['attributes']['oriso-c.activation-state']=='staged-v1' for c in self.clients.values()))
    def test_failure_never_modifies_foreign_receipt_or_later_clients(self):
        names=list(self.actors);foreign='/clients/'+names[2];self.clients[foreign]['attributes']['oriso-c.activation-receipt']='foreign'
        before=copy.deepcopy(self.clients[foreign]);later=copy.deepcopy(self.clients['/clients/'+names[3]])
        with self.assertRaises(activation.ReconcileError):self.run_activation()
        self.assertEqual(before,self.clients[foreign]);self.assertEqual(later,self.clients['/clients/'+names[3]])
        self.assertFalse(any(path==foreign for path,_ in self.writes))
    def test_repeat_preflight_drift_disables_only_prior_owned_activation(self):
        self.run_activation()
        self.clients['/clients/foreign']={'id':'foreign','clientId':'unowned','enabled':True,'attributes':{}}
        original=self.admin.request
        def listing(method,path,body=None):
            if method=='GET' and path.endswith('/service-account-user'):
                return {'id':self.bindings[path.split('/')[2]]['serviceSubject']}
            if method=='GET' and path=='/clients':
                return [{'id':path.rsplit('/',1)[1],'clientId':self.actors[path.rsplit('/',1)[1]]['clientId'] if path.rsplit('/',1)[1] in self.actors else 'unowned',**copy.deepcopy(value)} for path,value in self.clients.items()]
            return original(method,path,body)
        self.admin.request=listing
        with patch.object(activation,'inspect',side_effect=activation.ReconcileError('profile drift')):
            with self.assertRaises(activation.ReconcileError):self.run_activation()
        self.assertTrue(all(self.clients['/clients/'+name]['enabled'] is False for name in self.actors))
        self.assertTrue(self.clients['/clients/foreign']['enabled'])

    def fail_repeat_actor_read(self, drift=None, rollback_failure=None):
        original=self.admin.request
        failed=False
        def request(method,path,body=None):
            nonlocal failed
            if method=='GET' and path=='/clients/ACCOUNT_CREATE' and not failed:
                failed=True
                if drift:drift()
                raise activation.ReconcileError('one-time actor read unavailable')
            if failed and rollback_failure:rollback_failure(method,path,body)
            return original(method,path,body)
        self.admin.request=request

    def test_repeat_actor_read_failure_disables_all_five_prior_owned_clients(self):
        self.run_activation();self.writes.clear()
        self.fail_repeat_actor_read()
        with self.assertRaisesRegex(activation.ReconcileError,'owned staged clients disabled'):
            self.run_activation()
        self.assertTrue(all(c['enabled'] is False and c['attributes']['oriso-c.activation-state']=='staged-v1' for c in self.clients.values()))
        self.assertEqual(set(self.clients),{path for path,body in self.writes if body.get('enabled') is False})

    def test_repeat_failure_preserves_foreign_receipt_policy_and_subject_clients(self):
        self.run_activation();self.writes.clear()
        saved={}
        def drift():
            self.clients['/clients/ACCOUNT_ADMIN']['attributes']['oriso-c.activation-receipt']='foreign'
            self.clients['/clients/MAIL']['attributes']['oriso-c.native-policy']='foreign'
            # The native subject can change independently of an old copied receipt.
            self.bindings['RUNTIME']['serviceSubject']='foreign-subject'
            for name in ('ACCOUNT_ADMIN','MAIL','RUNTIME'):
                saved[name]=copy.deepcopy(self.clients['/clients/'+name])
        # Keep the approved manifest immutable while the native fixture subject drifts.
        self.value['actors']=copy.deepcopy(self.bindings)
        self.fail_repeat_actor_read(drift=drift)
        with self.assertRaises(activation.ReconcileError):self.run_activation()
        for name,state in saved.items():
            self.assertEqual(state,self.clients['/clients/'+name])
            self.assertFalse(any(path=='/clients/'+name for path,_ in self.writes))
        self.assertFalse(self.clients['/clients/SETUP']['enabled'])
        self.assertFalse(self.clients['/clients/ACCOUNT_CREATE']['enabled'])

    def test_repeat_failure_continues_other_owned_rollbacks_after_read_failure(self):
        self.run_activation();self.writes.clear()
        def unavailable(method,path,body):
            if method=='GET' and path=='/clients/ACCOUNT_ADMIN':
                raise activation.ReconcileError('rollback read unavailable')
        self.fail_repeat_actor_read(rollback_failure=unavailable)
        with self.assertRaisesRegex(activation.ReconcileError,'disable readback incomplete'):
            self.run_activation()
        self.assertTrue(self.clients['/clients/ACCOUNT_ADMIN']['enabled'])
        self.assertTrue(all(self.clients['/clients/'+name]['enabled'] is False for name in self.actors if name!='ACCOUNT_ADMIN'))

    def test_repeat_failure_reports_ignored_disable_readback(self):
        self.run_activation();self.writes.clear()
        def ignored(method,path,body):
            if method=='GET' and path=='/clients/MAIL':
                self.clients[path]['enabled']=True
        self.fail_repeat_actor_read(rollback_failure=ignored)
        with self.assertRaisesRegex(activation.ReconcileError,'disable readback incomplete'):
            self.run_activation()
        self.assertTrue(self.clients['/clients/MAIL']['enabled'])
        self.assertTrue(all(self.clients['/clients/'+name]['enabled'] is False for name in self.actors if name!='MAIL'))

    def test_incomplete_rollback_is_reported(self):
        original=self.admin.request
        def fail(method,path,body=None):
            if method=='PUT' and body.get('enabled') is False:raise activation.ReconcileError('unavailable')
            return original(method,path,body)
        self.admin.request=fail
        with patch.object(activation,'verify_activation_runtime',side_effect=activation.ReconcileError('unavailable')):
            with self.assertRaisesRegex(activation.ReconcileError,'disable readback incomplete'):self.run_activation()
    def test_foreign_realm_or_subject_fails_before_any_write(self):
        for field in ('realm','subjects'):
            before=copy.deepcopy(self.snapshot);self.snapshot[field]={}
            with self.assertRaises(activation.ReconcileError):self.run_activation()
            self.assertEqual([],self.writes);self.snapshot=before
    def test_mixed_keys_duplicate_secrets_and_unknown_manifest_fields_closed(self):
        self.env['IDENTITY_SETUP_CLIENT_SECRET']=self.env['ORISO_TASK_CONTEXT_KEY']
        with self.assertRaises(activation.ReconcileError):self.run_activation()
        self.assertEqual([],self.writes)
        self.value['unreviewed']='authority'
        with self.assertRaises(activation.ReconcileError):activation.activation_contract(self.value,self.actors)
    def test_policy_binding_drift_is_rejected(self):
        self.value['protectedPolicy']['actorSubjects']['SETUP']='foreign'
        with self.assertRaises(activation.ReconcileError):self.run_activation()
        self.assertEqual([],self.writes)

class ActivationChart(unittest.TestCase):
    def test_default_disabled(self):
        result,docs=render(bindings());self.assertEqual(0,result.returncode,result.stderr)
        self.assertFalse(any('activate-scenario-c' in d['metadata']['name'] for d in docs))
    def test_enabled_job_packages_actual_reconciler_and_existing_bindings(self):
        config=bindings();config['activation']={'enabled':True,'confirmIsolatedRealm':True,'installerTokenSecretRef':{'name':'one-time-installer','key':'token'},'mappingConfigMapRef':{'name':'reviewed-groups','key':'mapping.json'},'providerJarSha256':{n:'a'*64 for n in ['oriso-account-lifecycle','otp-config','oriso-smtp-control']}}
        result,docs=render(config);self.assertEqual(0,result.returncode,result.stderr)
        job=next(d for d in docs if d['kind']=='Job' and 'activate-scenario-c' in d['metadata']['name'])
        container=job['spec']['template']['spec']['containers'][0];env={v['name']:v for v in container['env']}
        self.assertEqual(len(env),len(container['env']));self.assertEqual('true',env['ORISO_SCENARIO_C_ACTIVATE']['value'])
        self.assertEqual({'name':'one-time-installer','key':'token'},env['ORISO_KEYCLOAK_INSTALLER_TOKEN']['valueFrom']['secretKeyRef'])
        data=next(d['data'] for d in docs if d['kind']=='ConfigMap' and 'activate-scenario-c' in d['metadata']['name'])
        self.assertIn('def activate(',data['scenario_c_activation.py']);self.assertIn('verify_activation_runtime',data['scenario_c_activation.py'])
        self.assertIn('scenario_c_reconcile.py',data)
        config['activation']['confirmIsolatedRealm']=False
        denied,_=render(config);self.assertNotEqual(0,denied.returncode)

if __name__=='__main__':unittest.main()
