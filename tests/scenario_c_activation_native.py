#!/usr/bin/env python3
"""Isolated actual three-provider activation, convergence and rollback; no image build."""
import argparse,base64,copy,hashlib,json,os,signal,subprocess,sys,tempfile,time,unittest,uuid
from pathlib import Path
from urllib.error import HTTPError,URLError
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'files'))
import scenario_c_reconcile as reconciler
import scenario_c_activation as activation
import _keycloak_container as native
from test_reconcile_keycloak_scenario_c import MAPPING
from scenario_c_mail_native import sources, ensure_headroom

class NativeActivation(unittest.TestCase):
    def runTest(self):
        ensure_headroom()
        root=Path(os.environ['ACTIVATION_KEYCLOAK_SOURCE'])
        jars={'oriso-account-lifecycle':root/'keycloak-image/account-lifecycle-spi/target/account-lifecycle-spi-1.0-SNAPSHOT.jar',
            'otp-config':root/'keycloak-image/otp-config-spi/target/keycloak-otp-config-spi-1.0-SNAPSHOT-keycloak.jar',
            'oriso-smtp-control':root/'keycloak-image/smtp-control-spi/target/smtp-control-spi-1.0-SNAPSHOT.jar'}
        for jar in jars.values():self.assertTrue(jar.is_file())
        hashes={name:hashlib.sha256(path.read_bytes()).hexdigest() for name,path in jars.items()}
        container_name='oriso-activation-'+uuid.uuid4().hex[:10];volume=container_name+'-data';realm='activation-fixture';issuer='http://activation-fixture.invalid/auth/realms/'+realm
        subprocess.run(['docker','volume','create','--label','oriso-activation-test=true',volume],check=True,capture_output=True)
        self.addCleanup(lambda:subprocess.run(['docker','volume','rm',volume],check=True,capture_output=True))
        self.addCleanup(lambda:subprocess.run(['docker','rm','-f',container_name],check=True,capture_output=True))
        common=['docker','run','-d','--name',container_name,'--label','oriso-activation-test=true','--memory','1g','--cpus','2','-p','127.0.0.1::8080',
            '-v',volume+':/opt/keycloak/data','-e','JAVA_OPTS_APPEND=-Xms128m -Xmx512m -XX:ActiveProcessorCount=2',
            '-e','EMAIL_BRANDING_NAME=Activation Fixture','-e','EMAIL_LEGAL_ORGANISATION_NAME=Fixture Operator','-e','ORISO_APP_BASE_URL=http://127.0.0.1:18080','-e','KC_BOOTSTRAP_ADMIN_USERNAME='+native.INSTALLER[0],'-e','KC_BOOTSTRAP_ADMIN_PASSWORD='+native.INSTALLER[1]]
        for provider,path in jars.items():common+=['-v',str(path.resolve())+':/opt/keycloak/providers/'+provider+'.jar:ro']
        def start(environment):
            command=common.copy()
            for key,value in environment.items():command+=['-e',key+'='+value]
            command +=[native.IMAGE,'start-dev','--http-relative-path=/auth','--hostname=http://activation-fixture.invalid/auth']
            subprocess.run(command,check=True,capture_output=True)
            endpoint=subprocess.check_output(['docker','port',container_name,'8080/tcp'],text=True).strip().splitlines()[0]
            base='http://'+endpoint+'/auth';deadline=time.monotonic()+180
            while True:
                try:native.master_admin(base);return base
                except (HTTPError,URLError,ConnectionError,TimeoutError,OSError):
                    state=subprocess.check_output(['docker','inspect','--format','{{.State.Running}}',container_name],text=True).strip()
                    if state!='true':
                        logs=subprocess.run(['docker','logs','--tail','40',container_name],capture_output=True,text=True)
                        diagnostic=logs.stdout+logs.stderr
                        for key,value in environment.items():
                            if 'SECRET' in key or 'KEY' in key:diagnostic=diagnostic.replace(value,'[redacted]')
                        Path('/tmp/task2-activation-container.log').write_text(diagnostic)
                        raise AssertionError('Actual activation provider exited; sanitized local diagnostic retained') from None
                    if time.monotonic()>deadline:raise AssertionError('Actual activation provider readiness expired') from None
                    time.sleep(1)
        base=start({})
        roles=sorted({r for g in MAPPING['groups'] for r in g['roles']})
        groups=[{'name':'pending-provisioning'},{'name':'human-roles','subGroups':[
            {'name':g['path'].rsplit('/',1)[1],'realmRoles':g['roles']} for g in MAPPING['groups'] if g['path'].startswith('/human-roles/')]},
            {'name':'protected-admins','subGroups':[{'name':'technical-identities'}]+[
            {'name':g['path'].rsplit('/',1)[1],'realmRoles':g['roles']} for g in MAPPING['groups'] if g['path'].startswith('/protected-admins/')]}]
        human=str(uuid.uuid4())
        seed={'realm':realm,'enabled':True,'roles':{'realm':[{'name':r} for r in roles]},'groups':groups,
            'clients':[{'clientId':'activation-human','enabled':True,'publicClient':True,'protocol':'openid-connect','standardFlowEnabled':True,'directAccessGrantsEnabled':True,'serviceAccountsEnabled':False,'fullScopeAllowed':False}],
            'users':[{'id':human,'username':'reviewed-primary','enabled':True,'realmRoles':['agency-admin','tenant-admin'],'attributes':{'tenantId':['0']}}]}
        native.request(base,'POST','/admin/realms',seed,token=native.master_admin(base))
        admin=reconciler.migration_module().Admin(base,realm,native.master_admin(base))
        actors=reconciler.registry(ROOT/'files/scenario-c-task-identities.json')
        snapshot=reconciler.inspect(admin,actors,MAPPING,verify_service_accounts=True)
        reconciler.reconcile(admin,actors,MAPPING,snapshot,True)
        snapshot=reconciler.inspect(admin,actors,MAPPING,verify_service_accounts=True,exact_grants=True)
        realm_id=snapshot['realm']['id'];bindings={n:{'clientId':a['clientId'],'serviceSubject':snapshot['subjects'][n]} for n,a in actors.items()}
        def group(path):
            return admin.request('GET','/group-by-path/'+path.lstrip('/'))['id']
        technical=list(snapshot['subjects'].values())
        for client in admin.request('GET','/clients'):
            if client.get('serviceAccountsEnabled'):
                subject=admin.request('GET','/clients/'+client['id']+'/service-account-user')['id']
                if subject not in technical:technical.append(subject)
        ordinary_groups={g['path']:sorted(g['roles']) for g in MAPPING['groups'] if g['path'].startswith('/human-roles/')}
        technical_config={'realmId':realm_id,'issuer':issuer,'technicalSubjects':sorted(technical),'technicalGroupIds':[group('/protected-admins/technical-identities')],
            'technicalRoles':sorted({r for a in actors.values() for r in a['roles']})}
        ordinary={'version':1,'native':{**technical_config,'clientIds':['activation-human'],'audienceSets':[['userservice']]},'ordinaryProfiles':[
            {'id':'consultant-v1','version':1,'groupId':group('/human-roles/consultant'),'groupPath':'/human-roles/consultant','groupParentId':group('/human-roles'),
             'realmRoles':['consultant','group-chat-consultant'],'clientRoles':{},'allowUnassignedTenant':False,'selfOperations':['account.read']}]}
        protected={**technical_config,'realmName':realm,'actorSubjects':{n:b['serviceSubject'] for n,b in bindings.items()},
            'machineAudienceSets':{n:sorted(actors[n]['bearerAudiences']) for n in ['SETUP','ACCOUNT_CREATE','ACCOUNT_ADMIN']},
            'bindings':[{'subject':human,'authority':'PLATFORM','authorizerTenantIds':[0],'humanClientIds':['activation-human'],'audienceSets':[['userservice']],
                'protectedProfileIds':['platform-v1'],'realmRoles':['agency-admin','tenant-admin'],'clientRoles':{},'version':1}],
            'profiles':[{'id':'platform-v1','version':1,'kind':'PLATFORM_ADMIN','tenantModes':['EXISTING'],'groupId':group('/protected-admins/platform-admins'),
                'groupPath':'/protected-admins/platform-admins','realmRoles':sorted(next(g['roles'] for g in MAPPING['groups'] if g['path']=='/protected-admins/platform-admins')),
                'clientRoles':{},'usernamePolicy':'USERNAME_TRANSCODER_V1','firstNamePolicy':'INVITE_WITH_EMAIL_LOCAL_PART_FALLBACK','lastNamePolicy':'INVITE_WITH_ADMIN_FALLBACK'}]}
        environment={'TASK_IDENTITY_MODEL':'FIVE_ACTORS','KEYCLOAK_REALM_INTERNAL_ID':realm_id,'ORISO_TASK_CONTEXT_KEY_VERSION':'1',
            'ORISO_TASK_CONTEXT_KEY':base64.b64encode(bytes(range(32))).decode(),'ORISO_ACCOUNT_LIFECYCLE_ENABLED':'true','ORISO_ACCOUNT_LIFECYCLE_REALM':realm,
            'ORISO_IDENTITY_DUMMY_EMAIL_SUFFIX':'@fixture.invalid','ORISO_ACCOUNT_LIFECYCLE_GROUPS':json.dumps(ordinary_groups),
            'ORISO_ACCOUNT_MAINTENANCE_CONFIG':json.dumps(ordinary),'ORISO_PROTECTED_INVITATIONS_ENABLED':'true','ORISO_PROTECTED_INVITATIONS_CONFIG':json.dumps(protected),
            'ORISO_SMTP_CONTROL_ENABLED':'true','ORISO_SMTP_CONTROL_REALM':realm}
        for name,actor in actors.items():
            environment['IDENTITY_'+name+'_CLIENT_ID']=actor['clientId'];environment['IDENTITY_'+name+'_SERVICE_SUBJECT']=snapshot['subjects'][name]
            environment['IDENTITY_'+name+'_CLIENT_SECRET']=admin.request('GET','/clients/'+snapshot['clients'][name]+'/client-secret')['value']
        value={'version':1,'realmId':realm_id,'realmName':realm,'issuer':issuer,'contextKeyVersion':'1','actors':bindings,
            'ordinaryGroups':ordinary_groups,'ordinaryPolicy':ordinary,'protectedPolicy':protected,'providerJarSha256':hashes}
        subprocess.run(['docker','stop','-t','5',container_name],check=True,capture_output=True);subprocess.run(['docker','rm',container_name],check=True,capture_output=True)
        base=start(environment);admin=reconciler.migration_module().Admin(base,realm,native.master_admin(base))
        original_call=activation.runtime_call
        observations=[]
        def observed_call(url,method,*args,**kwargs):
            result=original_call(url,method,*args,**kwargs)
            entry={'path':url.split('/realms/')[-1],'method':method,'status':result[0]}
            if url.endswith('/installation-status') and isinstance(result[1],dict):
                expected=activation.activation_contract(value,actors)
                entry['differentFields']=[key for key in set(expected)|set(result[1]) if expected.get(key)!=result[1].get(key)]
            observations.append(entry);return result
        activation.runtime_call=observed_call
        try:changes=activation.activate(admin,base,actors,MAPPING,value,environment)
        finally:Path('/tmp/task2-activation-runtime-observations.json').write_text(json.dumps(observations,indent=2))
        self.assertGreater(changes,0)
        self.assertEqual(0,activation.activate(admin,base,actors,MAPPING,value,environment))
        # Execute the actual chart-mounted source entry point against the same providers.
        chart_env=dict(os.environ,**environment)
        chart_env.update(ORISO_SCENARIO_C_ACTIVATE='true',ORISO_SCENARIO_C_ISOLATED_REALM='true',
            KEYCLOAK_URL=base,POD_NAMESPACE='fixture',TASK_IDENTITY_ISSUER=issuer,
            ORISO_KEYCLOAK_INSTALLER_TOKEN=native.master_admin(base),
            ORISO_ACTIVATION_MAPPING=json.dumps(MAPPING),ORISO_PROVIDER_JAR_SHA256=json.dumps(hashes))
        result=subprocess.run([sys.executable,'-B',str(ROOT/'files/keycloak-activate-scenario-c.py')],
            env=chart_env,capture_output=True,text=True,timeout=60)
        self.assertEqual(0,result.returncode,result.stderr)
        self.assertEqual({'actor_count':5,'runtime_cutover':True,'changes':0,
            'runtime_provider_bindings_verified':True,'legacy_retired':False},json.loads(result.stdout))
        # A live loaded policy drift (group grant) must fail actual status and disable only the owned set.
        drift=admin.request('GET','/roles/user');admin.request('POST','/groups/'+group('/human-roles/consultant')+'/role-mappings/realm',[drift])
        with self.assertRaises(activation.ReconcileError):activation.activate(admin,base,actors,MAPPING,value,environment)
        self.assertTrue(all(admin.request('GET','/clients/'+cid)['enabled'] is False for cid in snapshot['clients'].values()))
        admin.request('DELETE','/groups/'+group('/human-roles/consultant')+'/role-mappings/realm',[drift])
        self.assertGreater(activation.activate(admin,base,actors,MAPPING,value,environment),0)
        self.assertEqual(0,activation.activate(admin,base,actors,MAPPING,value,environment))
        for name in actors:
            self.assertEqual(environment['IDENTITY_'+name+'_CLIENT_SECRET'],
                admin.request('GET','/clients/'+snapshot['clients'][name]+'/client-secret')['value'])
            self.assertEqual(snapshot['subjects'][name],
                admin.request('GET','/clients/'+snapshot['clients'][name]+'/service-account-user')['id'])
        self.assertEqual(hashes,{n:hashlib.sha256(p.read_bytes()).hexdigest() for n,p in jars.items()})
        print('NATIVE_ACTIVATION_RUNTIME_CONVERGENCE_ROLLBACK_PASSED')

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--keycloak',required=True,type=Path)
    parser.add_argument('--evidence',type=Path);args=parser.parse_args()
    keycloak=args.keycloak.resolve();os.environ['ACTIVATION_KEYCLOAK_SOURCE']=str(keycloak)
    before={'Helm':sources(ROOT),'Keycloak':sources(keycloak)}
    revisions={name:subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True).strip()
        for name,root in [('Helm',ROOT),('Keycloak',keycloak)]}
    def deadline(signum,frame):raise RuntimeError('Native activation finite600s deadline reached')
    signal.signal(signal.SIGALRM,deadline);signal.setitimer(signal.ITIMER_REAL,600)
    started=time.monotonic()
    try:result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([NativeActivation()]))
    finally:signal.setitimer(signal.ITIMER_REAL,0)
    stable=before=={'Helm':sources(ROOT),'Keycloak':sources(keycloak)}
    passed=result.wasSuccessful() and result.testsRun==1 and not result.skipped and stable
    if args.evidence:
        args.evidence.mkdir(parents=True,exist_ok=True)
        (args.evidence/'activation-source.json').write_text(json.dumps({'revisions':revisions,'sourceManifest':before},indent=2))
        (args.evidence/'activation-result.json').write_text(json.dumps({'passed':passed,'tests':result.testsRun,
            'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),
            'sourceStable':stable,'durationSeconds':time.monotonic()-started,'image':native.IMAGE,
            'boundary':'Native runtime activation; final caller tuple remains a separate gate.'},indent=2))
    sys.exit(0 if passed else 1)
