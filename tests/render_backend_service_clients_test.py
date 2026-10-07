#!/usr/bin/env python3
"""Backend service credentials must be independent of human password sign-in."""
import subprocess
import yaml
from render_public_entry_rate_limit_test import CHART_DIR, VALUES


def render(*args):
    result = subprocess.run(['helm', 'template', 'service-clients', CHART_DIR, *VALUES, *args],
                            capture_output=True, text=True)
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc] if result.returncode == 0 else []
    return result, {(doc['kind'], doc['metadata']['name']): doc for doc in docs}


def main():
    result, docs = render()
    assert result.returncode == 0, result.stderr
    config = docs[('ConfigMap', 'userservice-configmap-env')]['data']
    assert config['IDENTITY_TECHNICAL_CLIENT_ID'] == 'backend-technical'
    assert config['KEYCLOAK_CONFIG_ADMIN_CLIENTID'] == 'backend-admin'
    assert config['KEYCLOAK_CONFIG_APP_CLIENTID'] == 'app'
    for deployment in ('userservice', 'consultingtypeservice'):
        pod = docs[('Deployment', deployment)]['spec']['template']
        env = {item['name']: item for item in pod['spec']['containers'][0]['env']}
        assert env['KEYCLOAK_BACKEND_TECHNICAL_CLIENT_SECRET']['valueFrom']['secretKeyRef'] == {
            'name': 'keycloak-backend-client-secrets', 'key': 'KEYCLOAK_BACKEND_TECHNICAL_CLIENT_SECRET'}
        assert 'IDENTITY_TECHNICAL_USER_PASSWORD' not in env
        assert 'IDENTITY_TECHNICAL_USER_USERNAME' not in env
        assert 'IDENTITY_TECHNICAL_CLIENT_ID' in env
    user_env = {item['name']: item for item in docs[('Deployment', 'userservice')]['spec']['template']['spec']['containers'][0]['env']}
    assert user_env['KEYCLOAK_BACKEND_ADMIN_CLIENT_SECRET']['valueFrom']['secretKeyRef']['key'] == 'KEYCLOAK_BACKEND_ADMIN_CLIENT_SECRET'
    assert 'KEYCLOAK_CONFIG_ADMIN_PASSWORD' not in user_env
    for key in ('keycloakBackendTechnicalClientSecret', 'keycloakBackendAdminClientSecret'):
        missing, _ = render('--set-string', 'global.secrets.' + key + '=')
        assert missing.returncode != 0, 'Missing backend credentials must fail before deployment'
    unsafe, _ = render('--set-string', 'global.keycloak.backendTechnicalClientId=app')
    assert unsafe.returncode != 0, 'Never turn the public human app into a service client'
    for override in ('global.secrets.keycloakBackendTechnicalClientSecret=rotated-technical-secret-render-only-canary',
                     'global.secrets.keycloakBackendAdminClientSecret=rotated-admin-secret-render-only-canary',
                     'global.keycloak.serviceAdminSubject=11111111-1111-4111-8111-111111111111'):
        rotated, changed = render('--set-string', override)
        assert rotated.returncode == 0, rotated.stderr
        for name in ('userservice', 'consultingtypeservice'):
            assert docs[('Deployment', name)]['spec']['template']['metadata']['annotations'] != changed[('Deployment', name)]['spec']['template']['metadata']['annotations']
    missing_admin, _ = render('--set-string', 'global.keycloak.serviceAdminSubject=')
    assert missing_admin.returncode != 0
    prepared, resources = render('--set', 'global.keycloak.backendServiceClients.prepareOnly=true',
                                 '--show-only', 'templates/userservice/backend-service-clients-secret.yaml',
                                 '--show-only', 'templates/keycloak-reconcile-service-identities-job.yaml')
    assert prepared.returncode == 0, prepared.stderr
    assert set(resources) == {('Secret', 'keycloak-backend-client-secrets'),
                              ('ConfigMap', 'keycloak-reconcile-service-clients-script'),
                              ('Job', 'keycloak-prepare-backend-clients')}
    prepare_env = resources[('Job', 'keycloak-prepare-backend-clients')]['spec']['template']['spec']['containers'][0]['env']
    assert not any('configMapKeyRef' in e.get('valueFrom', {}) for e in prepare_env)
    unsafe_upgrade, _ = render('--is-upgrade', '--set', 'global.keycloak.backendServiceClients.prepareOnly=true')
    assert unsafe_upgrade.returncode != 0, 'Preparation must never roll out backends before its post-upgrade job'
    print('PASS: dedicated clients, secret references, no password fallback, missing-secret/public-client guards')


if __name__ == '__main__':
    main()
