#!/usr/bin/env python3
"""Keep explicitly configured legacy credentials for unmigrated consumers only."""
import subprocess
import yaml
from render_public_entry_rate_limit_test import CHART_DIR, VALUES


def render(*args):
    result = subprocess.run(['helm','template','service-clients',CHART_DIR,*VALUES,*args],capture_output=True,text=True)
    docs=[doc for doc in yaml.safe_load_all(result.stdout) if doc] if result.returncode==0 else []
    return result,{(doc['kind'],doc['metadata']['name']):doc for doc in docs}


def main():
    result,docs=render()
    assert result.returncode==0,result.stderr
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
    print('PASS: explicit legacy retention, migrated runtime exclusion and pinned retirement gate')

if __name__=='__main__':
    main()
