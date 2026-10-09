#!/usr/bin/env python3
"""Verify feed controls reach the process and changes replace existing Pods."""
import subprocess
import yaml
from render_public_entry_rate_limit_test import CHART_DIR, VALUES


def render(*overrides):
    result = subprocess.run(
        ['helm', 'template', 'feed-signal', CHART_DIR, *VALUES, *overrides],
        capture_output=True, text=True, check=True,
    )
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    config = next(doc for doc in docs if doc.get('kind') == 'ConfigMap'
                  and doc['metadata']['name'] == 'userservice-configmap-env')
    pod = next(doc for doc in docs if doc.get('kind') == 'Deployment'
               and doc['metadata']['name'] == 'userservice')['spec']['template']
    return config['data'], pod


def main():
    default, original = render()
    disabled, opt_out = render('--set', 'userService.feedSignal.enabled=false')
    immediate, no_delay = render('--set', 'userService.feedSignal.coalesceMillis=0')
    delayed, slower = render('--set', 'userService.feedSignal.coalesceMillis=1200')
    missing, _ = render('--set', 'userService.feedSignal=null')
    assert default['MATRIX_FEED_SIGNAL_ENABLED'] == 'true'
    assert default['MATRIX_FEED_SIGNAL_COALESCE_MILLIS'] == '500'
    assert disabled['MATRIX_FEED_SIGNAL_ENABLED'] == 'false'
    assert immediate['MATRIX_FEED_SIGNAL_COALESCE_MILLIS'] == '0'
    assert delayed['MATRIX_FEED_SIGNAL_COALESCE_MILLIS'] == '1200'
    assert missing['MATRIX_FEED_SIGNAL_ENABLED'] == 'true'
    assert missing['MATRIX_FEED_SIGNAL_COALESCE_MILLIS'] == '500'
    env = {item['name']: item for item in original['spec']['containers'][0]['env']}
    for key in ('MATRIX_FEED_SIGNAL_ENABLED', 'MATRIX_FEED_SIGNAL_COALESCE_MILLIS'):
        assert env[key]['valueFrom']['configMapKeyRef'] == {
            'name': 'userservice-configmap-env', 'key': key,
        }
    for changed in (opt_out, no_delay, slower):
        assert original != changed, 'Changing feed controls must replace Pods that imported old env values'
    assert original['metadata']['annotations']['checksum/email-identity']
    print('PASS: feed defaults, false/zero, env references, and rollout changes')


if __name__ == '__main__':
    main()
