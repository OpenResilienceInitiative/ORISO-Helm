import importlib.util
import json
import io
from unittest.mock import patch
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('helper', Path(__file__).with_name('predev-keycloak-smtp.py'))
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


class SmtpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.marker = Path(self.tmp.name) / 'marker.json'
        self.smtp = {}
        self.calls = []
        self.settings = {f'globalSmtp{k}': v for k, v in dict(Enabled=True, Host='smtp.example.org', Port=465, Secure=True, From='service@example.org').items()}
        self.creds = {'globalSmtpUsername': 'private-user', 'globalSmtpPassword': 'private-password'}
        self.auth = {'platformToken': 'private-platform-token', 'keycloakToken': 'private-admin-token'}

    def tearDown(self):
        self.tmp.cleanup()

    def http(self, method, url, token, body=None):
        self.calls.append((method, url, token, body))
        if url.endswith('/smtp-credentials'):
            return self.creds
        if url.endswith('/service/settings'):
            return self.settings
        if method == 'PUT':
            self.assertEqual(set(body), {'smtpServer'})
            self.smtp = dict(body['smtpServer'])
            return None
        return {'realm': 'online-beratung', 'smtpServer': dict(self.smtp), 'resetPasswordAllowed': False}

    def run_action(self, action, **kwargs):
        return h.run(action, 'https://predev.oriso.org', 'online-beratung', self.marker, self.auth, request=self.http, **kwargs)

    def test_apply_check_restore_without_secret_marker(self):
        self.assertEqual(self.run_action('apply')['state'], 'candidate-verified')
        self.assertEqual(self.smtp['password'], 'private-password')
        marker = self.marker.read_text()
        self.assertNotIn('private', marker)
        self.assertEqual(self.run_action('check')['state'], 'candidate-verified')
        self.assertEqual(self.run_action('restore')['state'], 'empty-restored')
        self.assertEqual(self.smtp, {})
        self.assertEqual(self.run_action('restore')['state'], 'empty-restored')

    def test_wrong_target_never_requests(self):
        for origin, realm in [('https://dev.oriso.org', 'online-beratung'), ('https://predev.oriso.org', 'master')]:
            with self.assertRaises(h.Refused):
                h.run('apply', origin, realm, self.marker, self.auth, request=self.http)
        self.assertFalse(self.calls)

    def test_existing_smtp_or_marker_prevents_apply(self):
        self.smtp = {'host': 'existing'}
        with self.assertRaises(h.Refused): self.run_action('apply')
        self.smtp = {}
        self.marker.write_text('{}')
        with self.assertRaises(h.Refused): self.run_action('apply')
        self.assertFalse(any(c[0] == 'PUT' for c in self.calls))

    def test_restore_requires_valid_marker_and_refuses_concurrent_config(self):
        with self.assertRaises(h.Refused): self.run_action('restore')
        self.run_action('apply')
        self.smtp['host'] = 'changed.example.org'
        with self.assertRaises(h.Refused): self.run_action('restore')
        self.assertEqual(self.smtp['host'], 'changed.example.org')

    def test_strict_starttls_cannot_be_mapped_silently(self):
        self.settings['globalSmtpSecure'] = False
        with self.assertRaisesRegex(h.Refused, 'STARTTLS'): self.run_action('apply')
        self.assertFalse(self.marker.exists())
        self.assertFalse(any(c[0] == 'PUT' for c in self.calls))

    def test_notification_preference_does_not_disable_security_mail(self):
        self.settings['globalFeatureSystemNotificationEmailsEnabled'] = False
        self.assertEqual(self.run_action('apply')['state'], 'candidate-verified')

    def test_bad_credentials_or_disabled_transport_refused(self):
        for password in ['', 'ENC:unusable']:
            self.creds['globalSmtpPassword'] = password
            with self.assertRaises(h.Refused): self.run_action('apply')
        self.creds['globalSmtpPassword'] = 'private-password'
        self.settings['globalSmtpEnabled'] = False
        with self.assertRaises(h.Refused): self.run_action('apply')
        self.assertFalse(any(c[0] == 'PUT' for c in self.calls))

    def test_uncertain_put_keeps_empty_backup_and_no_secret_error(self):
        original = self.http
        def failing(method, url, token, body=None):
            if method == 'PUT': raise RuntimeError('private-password')
            return original(method, url, token, body)
        with self.assertRaises(h.Refused) as error:
            h.run('apply', 'https://predev.oriso.org', 'online-beratung', self.marker, self.auth, request=failing)
        self.assertNotIn('private', str(error.exception))
        self.assertEqual(json.loads(self.marker.read_text())['originalSmtp'], {})

    def test_cli_never_logs_auth_or_raw_errors(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        args = ['helper', 'apply', '--origin', 'https://predev.oriso.org', '--realm', 'online-beratung', '--marker', str(self.marker)]
        with patch.object(h.sys, 'argv', args), patch.object(h.sys, 'stdin', io.StringIO(json.dumps(self.auth))), patch.object(h.sys, 'stdout', stdout), patch.object(h.sys, 'stderr', stderr), patch.object(h, 'run', side_effect=RuntimeError('private-password')):
            self.assertEqual(h.main(), 1)
        self.assertNotIn('private', stdout.getvalue() + stderr.getvalue())

    def test_marker_target_tampering_blocks_restore(self):
        self.run_action('apply')
        record = json.loads(self.marker.read_text())
        record['realm'] = 'master'
        self.marker.write_text(json.dumps(record))
        before = len(self.calls)
        with self.assertRaises(h.Refused): self.run_action('restore')
        self.assertEqual(len(self.calls), before)



if __name__ == '__main__': unittest.main()
