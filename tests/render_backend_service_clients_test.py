#!/usr/bin/env python3
"""Keep explicitly configured legacy credentials for unmigrated consumers only."""
import subprocess
import re
import yaml
from render_public_entry_rate_limit_test import CHART_DIR, VALUES


class UniqueKeysLoader(yaml.SafeLoader):
    """A duplicate annotations key would silently discard a rollout checksum."""


def unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        assert key not in result, 'Duplicate YAML mapping key: ' + str(key)
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueKeysLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def render(*args):
    result = subprocess.run(['helm','template','service-clients',CHART_DIR,*VALUES,*args],capture_output=True,text=True)
    docs=[]
    if result.returncode==0:
        for raw in re.split(r'^---\s*$',result.stdout,flags=re.MULTILINE):
            doc=yaml.safe_load(raw)
            if doc:
                if doc.get('kind')=='Deployment' and doc['metadata']['name']=='agencyservice':
                    doc=yaml.load(raw,Loader=UniqueKeysLoader)
                docs.append(doc)
    return result,{(doc['kind'],doc['metadata']['name']):doc for doc in docs}


def main():
    result,docs=render()
    assert result.returncode==0,result.stderr
    agency_config=docs[('ConfigMap','agencyservice-configmap-env')]['data']
    assert agency_config['IDENTITY_TECHNICAL_CLIENT_ID']=='backend-technical'
    assert agency_config['TECHNICAL_SERVICE_SUBJECT']=='00000000-0000-4000-8000-000000000000'
    agency_pod=docs[('Deployment','agencyservice')]['spec']['template']
    agency_env={item['name']:item for item in agency_pod['spec']['containers'][0]['env']}
    for key in ('IDENTITY_TECHNICAL_CLIENT_ID','TECHNICAL_SERVICE_SUBJECT'):
        assert agency_env[key]['valueFrom']['configMapKeyRef']=={'name':'agencyservice-configmap-env','key':key}
    assert not any(name.endswith('_CLIENT_SECRET') or name in {'IDENTITY_TECHNICAL_USER_PASSWORD','KEYCLOAK_CONFIG_ADMIN_PASSWORD'} for name in agency_env)
    annotations=agency_pod['metadata']['annotations']
    assert 'checksum/technical-service-identity' in annotations and 'checksum/task-bindings' in annotations
    for override in ('global.keycloak.serviceTechUserId=22222222-2222-4222-8222-222222222222',
                     'global.keycloak.backendTechnicalClientId=backend-technical-rotated'):
        rotated,changed=render('--set-string',override)
        assert rotated.returncode==0,rotated.stderr
        changed_annotations=changed[('Deployment','agencyservice')]['spec']['template']['metadata']['annotations']
        assert annotations['checksum/technical-service-identity']!=changed_annotations['checksum/technical-service-identity']
        assert annotations['checksum/task-bindings']==changed_annotations['checksum/task-bindings']
        assert docs[('Secret','keycloak-backend-client-secrets')]==changed[('Secret','keycloak-backend-client-secrets')]
        assert docs[('Deployment','frontend')]==changed[('Deployment','frontend')]
        key,value=override.split('=',1)
        env_key='TECHNICAL_SERVICE_SUBJECT' if key.endswith('serviceTechUserId') else 'IDENTITY_TECHNICAL_CLIENT_ID'
        assert changed[('ConfigMap','agencyservice-configmap-env')]['data'][env_key]==value
    task_rotated,changed=render('--set-string','global.taskIdentities.subjects.CONFIG_WIZARD=33333333-3333-4333-8333-333333333333')
    assert task_rotated.returncode==0,task_rotated.stderr
    changed_annotations=changed[('Deployment','agencyservice')]['spec']['template']['metadata']['annotations']
    assert annotations['checksum/task-bindings']!=changed_annotations['checksum/task-bindings']
    assert annotations['checksum/technical-service-identity']==changed_annotations['checksum/technical-service-identity']
    disabled,changed=render('--set-string','global.keycloak.serviceTechUserId=')
    assert disabled.returncode==0,disabled.stderr
    disabled_config=changed[('ConfigMap','agencyservice-configmap-env')]['data']
    disabled_env={item['name'] for item in changed[('Deployment','agencyservice')]['spec']['template']['spec']['containers'][0]['env']}
    for key in ('IDENTITY_TECHNICAL_CLIENT_ID','TECHNICAL_SERVICE_SUBJECT'):
        assert key not in disabled_config and key not in disabled_env
    assert 'IDENTITY_CONFIG_WIZARD_SERVICE_SUBJECT' in disabled_env
    for override in ('global.keycloak.serviceTechUserId=not-a-subject',
                     'global.keycloak.backendTechnicalClientId=',
                     'global.keycloak.backendTechnicalClientId=backend-admin',
                     'global.keycloak.backendTechnicalClientId=app',
                     'global.keycloak.backendTechnicalClientId=backend-config-wizard'):
        invalid,_=render('--set-string',override)
        assert invalid.returncode!=0,'Configured legacy binding must be exact and distinct'
    assert ('Secret','keycloak-backend-client-secrets') in docs
    for name in ('userservice','consultingtypeservice'):
        env=docs[('Deployment',name)]['spec']['template']['spec']['containers'][0]['env']
        names={item['name'] for item in env}
        assert not names & {'KEYCLOAK_BACKEND_ADMIN_CLIENT_SECRET','KEYCLOAK_BACKEND_TECHNICAL_CLIENT_SECRET',
                            'IDENTITY_TECHNICAL_USER_PASSWORD','KEYCLOAK_CONFIG_ADMIN_PASSWORD'}
    fresh,resources=render('--set-string','global.secrets.keycloakBackendTechnicalClientSecret=',
                          '--set-string','global.secrets.keycloakBackendAdminClientSecret=')
    assert fresh.returncode==0,fresh.stderr
    assert ('Secret','keycloak-backend-client-secrets') not in resources
    partial,_=render('--set-string','global.secrets.keycloakBackendAdminClientSecret=')
    assert partial.returncode!=0,'Partially configured legacy credentials must fail'
    unpinned,_=render('--set','global.taskIdentities.retireLegacy=true')
    assert unpinned.returncode!=0,'Retirement requires all four verified legacy owner UUIDs'
    pins=[]
    for index,key in enumerate(('TECHNICAL_PASSWORD','SERVICE_ADMIN_PASSWORD','TECHNICAL_CLIENT','ADMIN_CLIENT'),1):
        pins.extend(['--set-string','global.taskIdentities.legacySubjects.'+key+'='+str(index)*8+'-'+str(index)*4+'-'+str(index)*4+'-'+str(index)*4+'-'+str(index)*12])
    retired,resources=render('--set','global.taskIdentities.retireLegacy=true',*pins)
    assert retired.returncode==0,retired.stderr
    assert ('Secret','keycloak-backend-client-secrets') not in resources
    bridge,_=render('--set','global.taskIdentities.retireLegacy=true','--set','global.taskIdentities.legacyOtpCompatibility=true',*pins)
    assert bridge.returncode!=0,'OTP compatibility must be disabled before legacy retirement'
    print('PASS: exact optional legacy binding, independent rollout checksums, migrated runtime exclusion and pinned retirement gate')

if __name__=='__main__':
    main()
