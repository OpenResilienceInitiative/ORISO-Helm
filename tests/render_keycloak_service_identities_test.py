"""Public chart contracts for dedicated service clients and recovery separation."""
import subprocess
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[1]
JOB='keycloak-reconcile-service-identities'
def render(*args):
 p=subprocess.run(['helm','template','identities',str(ROOT),'-f',str(ROOT/'values.yaml.default'),'-f',str(ROOT/'tests/fixtures/values-render-domain.yaml'),'-f',str(ROOT/'secrets.yaml.default'),'-f',str(ROOT/'tests/fixtures/render-required-secrets.yaml'),*args],capture_output=True,text=True)
 assert p.returncode==0,p.stderr
 return {(d['kind'],d['metadata']['name']):d for d in yaml.safe_load_all(p.stdout) if d}
def main():
 d=render();job=d[('Job',JOB)];pod=job['spec']['template']['spec'];c=pod['containers'][0]
 assert job['metadata']['annotations']['helm.sh/hook']=='post-install,post-upgrade'
 assert int(job['metadata']['annotations']['helm.sh/hook-weight'])==15
 assert job['spec']['activeDeadlineSeconds']==600
 assert '@sha256:' in c['image']
 assert pod['automountServiceAccountToken'] is False
 assert c['securityContext']['readOnlyRootFilesystem'] is True
 assert c['command']==['python3','/scripts/keycloak-reconcile-service-clients.py']
 env={v['name']:v for v in c['env']}
 for name,key in [('TECHNICAL_CLIENT_SECRET','KEYCLOAK_BACKEND_TECHNICAL_CLIENT_SECRET'),('ADMIN_CLIENT_SECRET','KEYCLOAK_BACKEND_ADMIN_CLIENT_SECRET')]:
  assert env[name]['valueFrom']['secretKeyRef']=={'name':'keycloak-backend-client-secrets','key':key}
 assert env['PREPARE_ONLY']['value']=='false';assert env['RETIRE_LEGACY_USERS']['value']=='false'
 assert env['TECHNICAL_SERVICE_SUBJECT']['value']=='00000000-0000-4000-8000-000000000000'
 assert env['ADMIN_SERVICE_SUBJECT']['value']=='00000000-0000-4000-8000-000000000001'
 scripts=d[('ConfigMap','keycloak-reconcile-service-clients-script')]['data']
 for name in ('keycloak-reconcile-service-clients.py','keycloak-reconcile-smtp.py'):
  assert scripts[name].rstrip()==(ROOT/'files'/name).read_text().rstrip()
 for name in ('userservice','consultingtypeservice'):
  backend={v['name'] for v in d[('Deployment',name)]['spec']['template']['spec']['containers'][0]['env']}
  assert not {'KEYCLOAK_CONFIG_ADMIN_PASSWORD','KEYCLOAK_CONFIG_ADMIN_USERNAME','IDENTITY_TECHNICAL_USER_PASSWORD','IDENTITY_TECHNICAL_USER_USERNAME'}&backend
 bootstrap=d[('Job','keycloak-bootstrap-users')]['spec']['template']['spec']['containers'][0]
 assert not any(v['name'].startswith('TECHNICAL_') for v in bootstrap['env'])
 assert 'ensure_user "$TECHNICAL_USERNAME"' not in bootstrap['command'][-1]
 assert ('Job',JOB) in render('--set','global.keycloak.bootstrapUsers.enabled=false')
 for name in ('userservice','consultingtypeservice','tenantservice'):
  assert d[('Deployment',name)]['spec']['template']['metadata']['annotations']
 changed=render('--set-string','global.keycloak.serviceTechUserId=11111111-1111-4111-8111-111111111111')
 for name in ('userservice','consultingtypeservice','tenantservice'):
  assert d[('Deployment',name)]['spec']['template']['metadata']['annotations']!=changed[('Deployment',name)]['spec']['template']['metadata']['annotations']
 print('PASS: dedicated-client hook, immutable image, secret/pin references, no legacy bootstrap, subject rollout')
if __name__=='__main__':main()
