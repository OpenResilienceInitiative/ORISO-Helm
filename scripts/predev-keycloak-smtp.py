#!/usr/bin/env python3
"""One-shot PreDev SMTP configuration test. Auth JSON comes only from stdin."""
import argparse
import json
import os
from pathlib import Path
import sys
import socket
import ssl
import urllib.request

ORIGIN = 'https://predev.oriso.org'
REALM = 'online-beratung'
PUBLIC_KEYS = {'host', 'port', 'from', 'auth', 'ssl', 'starttls'}
CANDIDATE_KEYS = PUBLIC_KEYS | {'user', 'password'}


class Refused(Exception):
    """Safe error text only; never embed HTTP bodies, tokens or SMTP values."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise Refused('HTTP redirect refused')


def http(method, url, token, body=None):
    try:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method, headers={
            'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=30) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except Exception:
        raise Refused('HTTP operation failed; inspect sanitized operator state') from None


def setting(settings, name):
    value = settings.get('globalSmtp' + name)
    return value.get('value') if isinstance(value, dict) else value


def candidate(settings, credentials, implicit_tls_port=None):
    if setting(settings, 'Enabled') is not True:
        raise Refused('Platform SMTP transport is not enabled')
    secure = setting(settings, 'Secure')
    if secure not in (True, False) or not isinstance(secure, bool):
        raise Refused('Invalid SMTP secure setting')
    if secure is not True and implicit_tls_port is None:
        # Keycloak 26.6.3 enables STARTTLS but does not require it. Do not weaken
        # UserService's mail.smtp.starttls.required=true or guess a different port.
        raise Refused('Required STARTTLS cannot be represented by this Keycloak provider')
    port = setting(settings, 'Port')
    if isinstance(port, bool) or not str(port).isdigit() or not 1 <= int(port) <= 65535:
        raise Refused('Invalid SMTP port')
    result = {'host': setting(settings, 'Host'), 'port': str(implicit_tls_port if implicit_tls_port is not None else port),
              'from': setting(settings, 'From'), 'auth': 'true',
              'ssl': 'true', 'starttls': 'false',
              'user': credentials.get('globalSmtpUsername'),
              'password': credentials.get('globalSmtpPassword')}
    if any(not isinstance(v, str) or not v.strip() for v in result.values()):
        raise Refused('Incomplete platform SMTP source')
    if result['password'].startswith('ENC:'):
        raise Refused('Credential endpoint returned encrypted data')
    return result


def public_shape(smtp):
    return {key: smtp[key] for key in sorted(PUBLIC_KEYS) if key in smtp}


def matches(smtp, expected):
    return (set(smtp) == CANDIDATE_KEYS and public_shape(smtp) == expected
            and isinstance(smtp.get('user'), str) and bool(smtp['user'])
            and isinstance(smtp.get('password'), str) and bool(smtp['password']))


def verify_implicit_tls(host, port):
    # The default context requires certificate-chain and hostname validation.
    # Probe only TLS; no SMTP authentication or message is sent.
    context = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=15) as connection:
        with context.wrap_socket(connection, server_hostname=host):
            pass


def run(action, origin, realm, marker, auth, request=http, keycloak_prefix='/auth',
        implicit_tls_port=None, tls_probe=verify_implicit_tls):
    if origin != ORIGIN or realm != REALM or keycloak_prefix not in ('', '/auth'):
        raise Refused('Only the fixed PreDev realm target is permitted')
    if action not in ('apply', 'restore', 'check'):
        raise Refused('Unknown operation')
    if implicit_tls_port is not None and (implicit_tls_port != 465 or action != 'apply'):
        raise Refused('Only apply permits the explicit implicit-TLS port 465 override')
    marker = Path(marker)
    token = auth.get('keycloakToken')
    if not isinstance(token, str) or not token:
        raise Refused('Keycloak token required on stdin')
    realm_url = origin + keycloak_prefix + '/admin/realms/' + realm

    def call(method, url, bearer, body=None):
        try:
            return request(method, url, bearer, body)
        except Exception:
            raise Refused('HTTP operation failed; marker retained for explicit recovery') from None

    def read_smtp():
        representation = call('GET', realm_url, token)
        if not isinstance(representation, dict) or representation.get('realm') != realm:
            raise Refused('Unexpected realm response')
        smtp = representation.get('smtpServer')
        if not isinstance(smtp, dict):
            raise Refused('Realm SMTP state unavailable')
        return smtp

    if action == 'apply':
        if marker.exists() or marker.is_symlink():
            raise Refused('Existing marker prevents apply')
        if read_smtp() != {}:
            raise Refused('Initial realm SMTP must be exactly empty')
        platform_token = auth.get('platformToken')
        if not isinstance(platform_token, str) or not platform_token:
            raise Refused('Platform tenant-zero token required on stdin')
        settings = call('GET', origin + '/service/settings', platform_token)
        credentials = call('GET', origin + '/service/settingsadmin/smtp-credentials', platform_token)
        if not isinstance(settings, dict) or not isinstance(credentials, dict):
            raise Refused('Platform SMTP source unavailable')
        smtp = candidate(settings, credentials, implicit_tls_port)
        transport_override = None
        if implicit_tls_port is not None:
            try:
                tls_probe(smtp['host'], implicit_tls_port)
            except Exception:
                raise Refused('Implicit-TLS certificate verification failed; no realm write') from None
            transport_override = {'type': 'explicit-implicit-tls',
                                  'sourcePort': setting(settings, 'Port'),
                                  'sourceSecure': setting(settings, 'Secure'),
                                  'selectedPort': implicit_tls_port,
                                  'certificateVerified': True}
        record = {'version': 1, 'origin': origin, 'realm': realm,
                  'keycloakPrefix': keycloak_prefix, 'originalSmtp': {},
                  'candidatePublic': public_shape(smtp)}
        if transport_override is not None:
            record['transportOverride'] = transport_override
        # Exclusive write before PUT preserves an empty-original marker even if
        # the response is lost. Never persist user/password or token hashes.
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as out:
            json.dump(record, out)
            out.flush()
            os.fsync(out.fileno())
        if read_smtp() != {}:
            raise Refused('Realm changed before apply; no write performed')
        call('PUT', realm_url, token, {'smtpServer': smtp})
        if not matches(read_smtp(), record['candidatePublic']):
            raise Refused('Candidate readback mismatch; explicit recovery required')
        return {'state': 'candidate-verified'}

    try:
        if marker.is_symlink():
            raise ValueError()
        record = json.loads(marker.read_text())
        valid = (record['version'] == 1 and record['origin'] == origin
                 and record['realm'] == realm and record['keycloakPrefix'] == keycloak_prefix
                 and record['originalSmtp'] == {} and set(record['candidatePublic']) == PUBLIC_KEYS)
        if not valid:
            raise ValueError()
    except Exception:
        raise Refused('Valid empty-original marker required') from None
    current = read_smtp()
    if current == {}:
        return {'state': 'empty-restored'}
    if not matches(current, record['candidatePublic']):
        raise Refused('Concurrent SMTP configuration differs; restore refused')
    if action == 'check':
        return {'state': 'candidate-verified'}
    call('PUT', realm_url, token, {'smtpServer': {}})
    if read_smtp() != {}:
        raise Refused('Empty SMTP restoration not verified')
    return {'state': 'empty-restored'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['apply', 'restore', 'check'])
    parser.add_argument('--origin', required=True)
    parser.add_argument('--realm', required=True)
    parser.add_argument('--marker', required=True)
    parser.add_argument('--keycloak-prefix', choices=['', '/auth'], default='/auth')
    parser.add_argument('--implicit-tls-port', type=int, choices=[465])
    args = parser.parse_args()
    try:
        auth = json.load(sys.stdin)
        result = run(args.action, args.origin, args.realm, args.marker, auth,
                     keycloak_prefix=args.keycloak_prefix, implicit_tls_port=args.implicit_tls_port)
        print(json.dumps(result))
    except Refused as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        # Even unexpected parsing/filesystem/HTTP errors must not leak auth input.
        print('SMTP helper failed; no secret details logged. Preserve marker and check state.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
