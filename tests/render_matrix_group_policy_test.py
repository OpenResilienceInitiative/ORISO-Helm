#!/usr/bin/env python3
"""The opt-in AVV pair must share only its dedicated credential and private route."""
import subprocess
import yaml
from render_public_entry_rate_limit_test import CHART_DIR, VALUES


def render(*args, success=True):
    result = subprocess.run(['helm', 'template', 'avv-policy', CHART_DIR, *VALUES, *args],
                            capture_output=True, text=True)
    assert (result.returncode == 0) == success, result.stderr
    return [item for item in yaml.safe_load_all(result.stdout) if item] if success else []


def find(docs, kind, name):
    return next(item for item in docs if item['kind'] == kind and item['metadata']['name'] == name)


def main():
    baseline = render()
    env = find(baseline, 'ConfigMap', 'userservice-configmap-env')['data']
    assert env['MATRIX_GROUP_POLICY_ENABLED'] == 'false'
    assert not any(x['metadata']['name'] == 'matrix-group-history' for x in baseline)
    enabled = ['--set', 'matrix.groupPolicy.enabled=true',
               '--set', 'matrix.groupPolicy.existingSecret.name=dedicated-avv-policy',
               '--set', 'matrix.groupPolicy.existingSecret.tokenKey=token',
               '--set', 'matrix.groupPolicy.tokenRevision=fixture-1']
    docs = render(*enabled)
    env = find(docs, 'ConfigMap', 'userservice-configmap-env')['data']
    assert env['MATRIX_GROUP_POLICY_ENABLED'] == 'true'
    assert env['MATRIX_GROUP_PARTICIPATION_HISTORY_URL'] == 'http://matrix-group-history:8801/oriso-internal/group-participation-history'
    user = find(docs, 'Deployment', 'userservice')['spec']['template']
    user_env = {x['name']: x for x in user['spec']['containers'][0]['env']}
    assert user_env['MATRIX_GROUP_POLICY_TOKEN']['valueFrom']['secretKeyRef'] == {
        'name': 'dedicated-avv-policy', 'key': 'token'}
    synapse = find(docs, 'Deployment', 'matrix-synapse')['spec']['template']
    secret = next(v for v in synapse['spec']['volumes'] if v['name'] == 'group-policy-token')
    assert secret['secret']['secretName'] == 'dedicated-avv-policy'
    homeserver = yaml.safe_load(find(docs, 'ConfigMap', 'matrix-homeserver-oidc')['data']['homeserver.yaml'])
    module = homeserver['modules'][0]
    assert module['module'] == 'oriso_group_policy.GroupPolicy'
    private = next(x for x in homeserver['listeners'] if x['port'] == 8801)
    resource = private['additional_resources']['/oriso-internal/group-participation-history']
    assert resource['module'] == 'oriso_group_policy.HistoryResource'
    assert private['resources'] == []
    assert not any('additional_resources' in x for x in homeserver['listeners'] if x['port'] != 8801)
    service = find(docs, 'Service', 'matrix-group-history')
    assert service['spec']['type'] == 'ClusterIP'
    assert service['spec']['ports'] == [{'name': 'history', 'port': 8801, 'targetPort': 8801, 'protocol': 'TCP'}]
    ingress = find(docs, 'NetworkPolicy', 'matrix-group-policy-ingress')['spec']
    private_rule = next(x for x in ingress['ingress'] if x['ports'][0]['port'] == 8801)
    assert private_rule['from'] == [{'podSelector': {'matchLabels': {'app': 'userservice'}}}]
    for obj in docs:
        if obj['kind'] != 'Ingress':
            continue
        for rule in obj['spec'].get('rules', []):
            for path in rule.get('http', {}).get('paths', []):
                assert path['backend']['service']['name'] != 'matrix-group-history'
    assert synapse['metadata']['annotations']['checksum/group-policy'] != find(
        baseline, 'Deployment', 'matrix-synapse')['spec']['template']['metadata']['annotations'].get('checksum/group-policy')
    render('--set', 'matrix.groupPolicy.enabled=true', success=False)
    render(*enabled, '--set', 'matrix.groupPolicy.enabled="true"', success=False)
    render(*enabled, '--set', 'matrix.groupPolicy.existingSecret.name=matrix-secrets', success=False)
    render(*enabled, '--set', 'matrix.image=matrixdotorg/synapse:v1.157.0', success=False)
    render(*enabled, '--set', 'matrix.replicas=2', success=False)
    print('PASS: opt-in pair, dedicated credential, private listener/routing, invalid-config rejection')


if __name__ == '__main__':
    main()
