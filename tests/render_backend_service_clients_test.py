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
    agency_config = docs[('ConfigMap', 'agencyservice-configmap-env')]['data']
    assert agency_config['IDENTITY_TECHNICAL_CLIENT_ID'] == 'backend-technical'
    assert agency_config['TECHNICAL_SERVICE_SUBJECT'] == '00000000-0000-4000-8000-000000000000'
    agency_pod = docs[('Deployment', 'agencyservice')]['spec']['template']
    agency_env = {item['name']: item for item in agency_pod['spec']['containers'][0]['env']}
    for key in ('IDENTITY_TECHNICAL_CLIENT_ID', 'TECHNICAL_SERVICE_SUBJECT'):
        assert agency_env[key]['valueFrom']['configMapKeyRef'] == {
            'name': 'agencyservice-configmap-env', 'key': key}
    assert 'KEYCLOAK_BACKEND_TECHNICAL_CLIENT_SECRET' not in agency_env
    for override in ('global.keycloak.serviceTechUserId=22222222-2222-4222-8222-222222222222',
                     'global.keycloak.backendTechnicalClientId=backend-technical-rotated'):
        rotated, changed = render('--set-string', override)
        assert rotated.returncode == 0, rotated.stderr
        changed_pod = changed[('Deployment', 'agencyservice')]['spec']['template']
        assert agency_pod['metadata']['annotations'] != changed_pod['metadata']['annotations']
        assert docs[('Secret', 'keycloak-backend-client-secrets')] == changed[('Secret', 'keycloak-backend-client-secrets')]
        assert docs[('Deployment', 'frontend')] == changed[('Deployment', 'frontend')]
        changed_config = changed[('ConfigMap', 'agencyservice-configmap-env')]['data']
        key, value = override.split('=', 1)
        env_key = 'TECHNICAL_SERVICE_SUBJECT' if key.endswith('serviceTechUserId') else 'IDENTITY_TECHNICAL_CLIENT_ID'
        assert changed_config[env_key] == value
    for override in ('global.keycloak.serviceTechUserId=',
                     'global.keycloak.serviceTechUserId=not-a-subject',
                     'global.keycloak.backendTechnicalClientId=',
                     'global.keycloak.backendTechnicalClientId=backend-admin',
                     'global.keycloak.backendTechnicalClientId=app'):
        invalid, _ = render('--set-string', override)
        assert invalid.returncode != 0, 'AgencyService must never receive an unbound service identity'
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
    assert user_env['KEYCLOAK_BACKEND_ADMIN_SERVICE_SUBJECT']['valueFrom']['configMapKeyRef'] == {
        'name': 'userservice-configmap-env', 'key': 'KEYCLOAK_BACKEND_ADMIN_SERVICE_SUBJECT'}
    assert config['KEYCLOAK_BACKEND_ADMIN_SERVICE_SUBJECT'] == '00000000-0000-4000-8000-000000000001'
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
        if override.startswith('global.keycloak.serviceAdminSubject='):
            assert changed[('ConfigMap', 'userservice-configmap-env')]['data']['KEYCLOAK_BACKEND_ADMIN_SERVICE_SUBJECT'] == override.split('=', 1)[1]
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
