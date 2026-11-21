#!/usr/bin/env python3
"""Prepare dedicated backend clients; retire password users only after rollout.

Uses the existing pinned SMTP helper runtime and transport. Secrets stay in
memory and never enter a subprocess, file, argv or error message.
"""
import math
import time
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import quote, urlencode

spec = importlib.util.spec_from_file_location('smtp_transport', Path(__file__).with_name('keycloak-reconcile-smtp.py'))
smtp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smtp)
Error = smtp.ReconcileError
decode_claims = smtp.decode_claims

# Helm#535: client shipped in realm.json with a secret published in the repo.
PUBLISHED_CLIENT = ('user-service', 'user-service-secret')
# Helm#535: must match realm.json. Retention (30 days) pending DPO sign-off.
EVENTS_CONFIG = {'eventsEnabled': True, 'eventsExpiration': 2592000,
                 'enabledEventTypes': ['LOGIN', 'LOGIN_ERROR', 'LOGOUT', 'REFRESH_TOKEN_ERROR'],
                 'adminEventsEnabled': True, 'adminEventsDetailsEnabled': False}
# Admin events expire through a realm attribute, not events/config (seconds, same 30 days).
ADMIN_EVENTS_EXPIRATION = '2592000'
APP_CLIENT_ID = 'app'
ADMIN_API_SCOPE = 'kc-admin-api'
POST_LOGOUT_REDIRECT_URIS = 'post.logout.redirect.uris'
DNS_LABEL = r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?'


def required(env, name):
    return smtp.required(env, name)


def flag(env, name):
    value = env.get(name, 'false')
    if value not in ('true', 'false'):
        raise Error('BACKEND_CLIENT_CONFIGURATION_INVALID: ' + name)
    return value == 'true'


def app_client_url_policy(env):
    failure = 'APP_CLIENT_URL_POLICY_INVALID'
    try:
        policy = json.loads(required(env, 'APP_CLIENT_URL_POLICY'))
    except ValueError:
        raise Error(failure) from None
    origins = policy.get('webOrigins') if isinstance(policy, dict) else None
    if not isinstance(origins, list) or len(origins) != 1:
        raise Error(failure)
    origin = origins[0]
    if not isinstance(origin, str) or not re.fullmatch(rf'https?://{DNS_LABEL}(?:\.{DNS_LABEL})*', origin):
        raise Error(failure)
    if policy != {'webOrigins': [origin], 'redirectUris': [origin + '/*']}:
        raise Error(failure)
    return policy


def configuration(env):
    names = ('KEYCLOAK_URL', 'KEYCLOAK_REALM', 'POD_NAMESPACE', 'KEYCLOAK_ADMIN_USERNAME',
             'KEYCLOAK_ADMIN_PASSWORD', 'TECHNICAL_CLIENT_ID', 'ADMIN_CLIENT_ID', 'HUMAN_CLIENT_ID',
             'TECHNICAL_CLIENT_SECRET', 'ADMIN_CLIENT_SECRET', 'TECHNICAL_SERVICE_SUBJECT', 'ADMIN_SERVICE_SUBJECT',
             'BOOTSTRAP_ADMIN_USERNAME', 'LEGACY_TECHNICAL_USERNAME', 'LEGACY_ADMIN_USERNAME')
    c = {name: required(env, name) for name in names}
    c['KEYCLOAK_URL'] = smtp.base_url(c['KEYCLOAK_URL'], 'keycloak', c['POD_NAMESPACE'])
    if c['KEYCLOAK_REALM'] == 'master':
        raise Error('BACKEND_CLIENT_CONFIGURATION_INVALID: protected realm')
    reserved = {APP_CLIENT_ID, c['HUMAN_CLIENT_ID'], 'admin-cli', 'account', 'broker', 'security-admin-console', 'realm-management'}
    ids = [c['TECHNICAL_CLIENT_ID'], c['ADMIN_CLIENT_ID']]
    if ids[0] == ids[1] or any(i in reserved or not re.fullmatch(r'[A-Za-z0-9_-]+', i) for i in ids):
        raise Error('BACKEND_CLIENT_CONFIGURATION_INVALID: client ids')
    secrets = [c['TECHNICAL_CLIENT_SECRET'], c['ADMIN_CLIENT_SECRET']]
    if secrets[0] == secrets[1] or any(len(s) < 32 or not all(32 <= ord(ch) < 127 for ch in s) for s in secrets):
        raise Error('BACKEND_CLIENT_CONFIGURATION_INVALID: independent secrets')
    legacy = [c['LEGACY_TECHNICAL_USERNAME'].casefold(), c['LEGACY_ADMIN_USERNAME'].casefold()]
    if legacy[0] == legacy[1] or c['BOOTSTRAP_ADMIN_USERNAME'].casefold() in legacy or c['KEYCLOAK_ADMIN_USERNAME'].casefold() in legacy:
        raise Error('BACKEND_CLIENT_CONFIGURATION_INVALID: recovery identity')
    c['prepare'] = flag(env, 'PREPARE_ONLY')
    c['retire'] = flag(env, 'RETIRE_LEGACY_USERS')
    if c['prepare'] and c['retire']:
        raise Error('BACKEND_CLIENT_CONFIGURATION_INVALID: preparation cannot retire users')
    # Helm#422: ORISO-realm break-glass user; disabled only on explicit opt-in.
    c['disable_realm_admin'] = flag(env, 'DISABLE_REALM_ADMIN')
    if c['disable_realm_admin']:
        name = env.get('REALM_ADMIN_USERNAME', '')
        if c['prepare'] or not re.fullmatch(r'[A-Za-z0-9._@-]+', name) or name.casefold().startswith('service-account-'):
            raise Error('BACKEND_CLIENT_CONFIGURATION_INVALID: realm admin')
        c['REALM_ADMIN_USERNAME'] = name
    if not c['prepare']:
        c['app_client_urls'] = app_client_url_policy(env)
        # SMTP sync realm client (Helm#420); preparation leaves it alone.
        c['SMTP_SYNC_CLIENT_ID'] = required(env, 'SMTP_SYNC_CLIENT_ID')
        c['SMTP_SYNC_CLIENT_SECRET'] = required(env, 'SMTP_SYNC_CLIENT_SECRET')
        sync_id, sync_secret = c['SMTP_SYNC_CLIENT_ID'], c['SMTP_SYNC_CLIENT_SECRET']
        if sync_id in reserved or sync_id in ids or not re.fullmatch(r'[A-Za-z0-9_-]+', sync_id):
            raise Error('BACKEND_CLIENT_CONFIGURATION_INVALID: client ids')
        if sync_secret in secrets or len(sync_secret) < 32 or not all(32 <= ord(ch) < 127 for ch in sync_secret):
            raise Error('BACKEND_CLIENT_CONFIGURATION_INVALID: independent secrets')
    return c


class Admin:
    def __init__(self, config):
        self.http = smtp.HttpClient()
        token = self.http.login(config['KEYCLOAK_URL'], 'master', 'admin-cli',
                                config['KEYCLOAK_ADMIN_USERNAME'], config['KEYCLOAK_ADMIN_PASSWORD'], 'ADMIN_UNAVAILABLE')
        self.base = config['KEYCLOAK_URL'] + '/admin/realms/' + quote(config['KEYCLOAK_REALM'], safe='') + '/'
        self.headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}

    def request(self, method, path, data=None):
        return self.http.request(method, self.base + path,
                                 json.dumps(data).encode() if data is not None else None,
                                 self.headers, 'ADMIN_UNAVAILABLE', admin_api=True)

    def find_client(self, name):
        rows = self.request('GET', 'clients?' + urlencode({'clientId': name}))
        if not isinstance(rows, list) or len(rows) > 1:
            raise Error('BACKEND_CLIENT_INVALID: ambiguous client')
        return rows[0] if rows else None

    def validate_client(self, c):
        if (c.get('protocol') != 'openid-connect' or c.get('publicClient') is not False
                or c.get('serviceAccountsEnabled') is not True or c.get('bearerOnly', False)
                or any(c.get(k) is not False for k in ('directAccessGrantsEnabled', 'standardFlowEnabled', 'implicitFlowEnabled', 'fullScopeAllowed'))):
            raise Error('BACKEND_CLIENT_INVALID: existing client is not dedicated')

    def admin_api_scope(self):
        scopes = self.request('GET', 'client-scopes')
        if not isinstance(scopes, list) or any(not isinstance(scope, dict) for scope in scopes):
            raise Error('BACKEND_CLIENT_INVALID: realm client scopes')
        matches = [scope for scope in scopes if scope.get('name') == ADMIN_API_SCOPE]
        if not matches:
            # Keycloak versions before the Admin REST audience check have no built-in scope.
            return None
        scope = matches[0]
        if (len(matches) != 1 or scope.get('protocol') != 'openid-connect'
                or not isinstance(scope.get('id'), str) or not scope['id']):
            raise Error('BACKEND_CLIENT_INVALID: Admin REST client scope')
        return scope['id']

    def subject(self, client):
        user = self.request('GET', 'clients/' + quote(client['id'], safe='') + '/service-account-user')
        if not isinstance(user, dict) or not isinstance(user.get('id'), str) or not user['id']:
            raise Error('BACKEND_CLIENT_INVALID: service subject missing')
        return user['id']

    def replace_mappings(self, path, realm_roles, management_roles, management_id):
        current = self.request('GET', path)
        if not isinstance(current, dict):
            raise Error('BACKEND_CLIENT_INVALID: role mappings')
        def sync(bucket, present, wanted):
            present_names = {role['name'] for role in present}
            wanted_names = {role['name'] for role in wanted}
            extra = [role for role in present if role['name'] not in wanted_names]
            missing = [role for role in wanted if role['name'] not in present_names]
            if extra:
                self.request('DELETE', bucket, extra)
            if missing:
                self.request('POST', bucket, missing)
        sync(path + '/realm', current.get('realmMappings', []), realm_roles)
        mappings = current.get('clientMappings', {})
        for mapping in mappings.values():
            wanted = management_roles if mapping['id'] == management_id else []
            sync(path + '/clients/' + quote(mapping['id'], safe=''), mapping.get('mappings', []), wanted)
        if management_roles and not any(m['id'] == management_id for m in mappings.values()):
            self.request('POST', path + '/clients/' + quote(management_id, safe=''), management_roles)

    def reconcile(self, client, subject, secret, realm_roles, management_roles, management_id, admin_api_scope=None):
        cid = quote(client['id'], safe='')
        uid = quote(subject, safe='')
        # Inherited group roles would escape direct-role reconciliation.
        for group in self.request('GET', 'users/' + uid + '/groups'):
            self.request('DELETE', 'users/' + uid + '/groups/' + quote(group['id'], safe=''))
        self.replace_mappings('users/' + uid + '/role-mappings', realm_roles, management_roles, management_id)
        self.replace_mappings('clients/' + cid + '/scope-mappings', realm_roles, management_roles, management_id)
        default_scopes = 'clients/' + cid + '/default-client-scopes'
        optional_scopes = 'clients/' + cid + '/optional-client-scopes'
        defaults = self.request('GET', default_scopes)
        for scope in defaults:
            if scope.get('name') != 'roles' and scope['id'] != admin_api_scope:
                self.request('DELETE', default_scopes + '/' + quote(scope['id'], safe=''))
        for scope in self.request('GET', optional_scopes):
            self.request('DELETE', optional_scopes + '/' + quote(scope['id'], safe=''))
        if admin_api_scope and all(scope['id'] != admin_api_scope for scope in defaults):
            self.request('PUT', default_scopes + '/' + quote(admin_api_scope, safe=''))
        self.request('PUT', 'clients/' + cid, {'secret': secret, 'enabled': True})

    def realm(self):
        realm = self.http.request('GET', self.base.rstrip('/'), None, self.headers, 'ADMIN_UNAVAILABLE', admin_api=True,
                                  max_response_bytes=smtp.MAX_REALM_RESPONSE_BYTES)
        if not isinstance(realm, dict):
            raise Error('BACKEND_CLIENT_INVALID: realm')
        return realm

    def retire(self, username):
        users = self.request('GET', 'users?' + urlencode({'username': username, 'exact': 'true'}))
        if not isinstance(users, list) or len(users) > 1:
            raise Error('BACKEND_CLIENT_INVALID: legacy identity')
        if users:
            uid = quote(users[0]['id'], safe='')
            self.request('PUT', 'users/' + uid, {'enabled': False})
            # Revoke existing sessions as well as future password grants.
            self.request('POST', 'users/' + uid + '/logout')
        return bool(users)


def verify_client_token(http, config, client, subject, admin):
    token = http.service_login(config['KEYCLOAK_URL'], config['KEYCLOAK_REALM'], client,
                               config['ADMIN_CLIENT_SECRET' if admin else 'TECHNICAL_CLIENT_SECRET'], 'CLIENT_UNAVAILABLE')
    try:
        claims = decode_claims(token)
        expiry = claims.get('exp')
        realm_roles = claims.get('realm_access', {}).get('roles')
        resources = claims.get('resource_access', {})
        expected_realm = {'otp-config-admin'} if admin else {'technical'}
        valid = (claims.get('sub') == subject and claims.get('azp') == client
                 and isinstance(expiry, (int, float)) and not isinstance(expiry, bool)
                 and math.isfinite(expiry) and expiry > time.time()
                 and isinstance(realm_roles, list) and set(realm_roles) == expected_realm
                 and isinstance(resources, dict))
        if admin:
            management = resources.get('realm-management', {}).get('roles')
            # query-clients: UserService's role check lists the realm's clients (ORISO-UserService#1601).
            required_roles = {'manage-users', 'view-users', 'query-users', 'view-realm', 'query-clients'}
            # Existing view-users is a composite including query-groups.
            valid = (valid and set(resources) == {'realm-management'} and isinstance(management, list)
                     and required_roles <= set(management) <= required_roles | {'query-groups'})
        else:
            valid = valid and not resources
    except (ValueError, UnicodeError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise Error('BACKEND_CLIENT_TOKEN_CONTRACT_MISMATCH')


def reconcile_smtp_sync(api, config, client, management, admin_api_scope):
    # Native Keycloak needs manage-realm to update SMTP settings; nothing else.
    name = config['SMTP_SYNC_CLIENT_ID']
    if not client:
        api.request('POST', 'clients', {'clientId': name, 'protocol': 'openid-connect', 'enabled': True,
                    'publicClient': False, 'bearerOnly': False, 'serviceAccountsEnabled': True,
                    'directAccessGrantsEnabled': False, 'standardFlowEnabled': False,
                    'implicitFlowEnabled': False, 'fullScopeAllowed': False})
        client = api.find_client(name)
        if not client:
            raise Error('BACKEND_CLIENT_INVALID: client creation failed')
    manage_realm = api.request('GET', 'clients/' + quote(management['id'], safe='') + '/roles/manage-realm')
    subject = api.subject(client)
    api.reconcile(client, subject, config['SMTP_SYNC_CLIENT_SECRET'], [], [manage_realm], management['id'],
                  admin_api_scope=admin_api_scope)
    verify_smtp_sync_token(api.http, config, subject)
    print('SMTP_SYNC_CLIENT_RECONCILED')


def verify_smtp_sync_token(http, config, subject):
    client = config['SMTP_SYNC_CLIENT_ID']
    token = http.service_login(config['KEYCLOAK_URL'], config['KEYCLOAK_REALM'], client,
                               config['SMTP_SYNC_CLIENT_SECRET'], 'CLIENT_UNAVAILABLE')
    try:
        claims = decode_claims(token)
        expiry = claims.get('exp')
        realm_roles = claims.get('realm_access', {}).get('roles', [])
        management = claims.get('resource_access', {}).get('realm-management', {}).get('roles')
        valid = (claims.get('sub') == subject and claims.get('azp') == client
                 and isinstance(expiry, (int, float)) and not isinstance(expiry, bool)
                 and math.isfinite(expiry) and expiry > time.time() and realm_roles == []
                 and set(claims.get('resource_access', {})) == {'realm-management'}
                 and isinstance(management, list) and set(management) == {'manage-realm'})
    except (ValueError, UnicodeError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise Error('BACKEND_CLIENT_TOKEN_CONTRACT_MISMATCH')


def retire_published_client(api):
    name, published = PUBLISHED_CLIENT
    client = api.find_client(name)
    if not client:
        return 'PUBLISHED_CLIENT_ABSENT'
    cid = quote(client['id'], safe='')
    secret = api.request('GET', 'clients/' + cid + '/client-secret')
    # A rotated secret means an operator adopted the client; leave it to them.
    if not isinstance(secret, dict) or secret.get('value') != published:
        return 'PUBLISHED_CLIENT_KEPT_ROTATED_SECRET'
    api.request('DELETE', 'clients/' + cid)
    return 'PUBLISHED_CLIENT_DELETED'


def reconcile_events(api):
    current = api.request('GET', 'events/config')
    if not isinstance(current, dict):
        raise Error('BACKEND_CLIENT_INVALID: events config')
    # PUT replaces the whole config; keep listeners and anything else as-is.
    api.request('PUT', 'events/config', {**current, **EVENTS_CONFIG})
    attributes = api.realm().get('attributes') or {}
    if attributes.get('adminEventsExpiration') != ADMIN_EVENTS_EXPIRATION:
        # A realm PUT replaces all attributes; send the current ones back.
        body = json.dumps({'attributes': {**attributes, 'adminEventsExpiration': ADMIN_EVENTS_EXPIRATION}}).encode()
        api.http.request('PUT', api.base.rstrip('/'), body, api.headers, 'ADMIN_UNAVAILABLE', admin_api=True)
    print('REALM_EVENTS_RECONCILED')


def app_client(api):
    client = api.find_client(APP_CLIENT_ID)
    if (not client or client.get('clientId') != APP_CLIENT_ID
            or client.get('protocol') != 'openid-connect' or client.get('publicClient') is not True
            or not isinstance(client.get('id'), str) or not client['id']):
        raise Error('APP_CLIENT_URL_POLICY_CLIENT_INVALID')
    return client


def bounded_logout_policy(configured, origin, root_url):
    # Keycloak's absent value and "+" inherit login redirects. "-" explicitly disables them.
    if not configured:
        return configured
    entries = re.split(r'\s*##\s*', configured)
    if entries[0] == '-':
        return configured
    relative_to_installation = not root_url or root_url == origin or root_url.startswith(origin + '/')
    bounded = [
        origin + '/*' if entry == '*' else entry
        for entry in entries
        if (entry in ('*', '+', origin) or entry.startswith(origin + '/')
            or relative_to_installation and entry.startswith('/') and not entry.startswith('//'))
    ]
    if bounded == entries:
        return configured
    # An explicit list that lost every return must not fall back to the login policy.
    return '##'.join(dict.fromkeys(bounded)) or '-'


def reconcile_app_client_urls(api, client, policy):
    path = 'clients/' + quote(client['id'], safe='')
    current = api.request('GET', path)
    attributes = current.get('attributes') or {}
    configured = attributes.get(POST_LOGOUT_REDIRECT_URIS)
    logout = bounded_logout_policy(configured, policy['webOrigins'][0], current.get('rootUrl'))
    changes = {key: value for key, value in policy.items() if current.get(key) != value}
    if logout != configured:
        # Keycloak's client update applies supplied attributes one key at a time.
        changes['attributes'] = {POST_LOGOUT_REDIRECT_URIS: logout}
    if changes:
        api.request('PUT', path, changes)
        observed = api.request('GET', path)
        if (any(observed.get(key) != value for key, value in policy.items())
                or (observed.get('attributes') or {}).get(POST_LOGOUT_REDIRECT_URIS) != logout):
            raise Error('APP_CLIENT_URL_POLICY_READBACK_FAILED')
    print('APP_CLIENT_URL_POLICY_RECONCILED')


def reconcile(config):
    api = Admin(config)
    names = (config['TECHNICAL_CLIENT_ID'], config['ADMIN_CLIENT_ID'])
    clients = [api.find_client(name) for name in names]
    for client in clients:
        if client:
            api.validate_client(client)
    if not config['prepare'] and not all(clients):
        raise Error('BACKEND_CLIENT_PREPARATION_REQUIRED: clients are missing; nothing changed')
    browser_client = None if config['prepare'] else app_client(api)
    sync_client = None if config['prepare'] else api.find_client(config['SMTP_SYNC_CLIENT_ID'])
    if sync_client:
        api.validate_client(sync_client)
    if not config['prepare'] and any(api.subject(client) != config[pin] for client, pin in zip(clients, ('TECHNICAL_SERVICE_SUBJECT', 'ADMIN_SERVICE_SUBJECT'))):
        raise Error('BACKEND_CLIENT_SUBJECT_MISMATCH: set serviceTechUserId to the prepared service account; nothing changed')
    if not config['prepare'] and api.realm().get('adminPermissionsEnabled'):
        # With native admin permissions, manage-realm can rewrite the permission
        # store and grant itself user management (Keycloak57 boundary result).
        raise Error('SMTP_SYNC_FGAP_ESCALATION: smtp-sync would get manage-realm on a realm with '
                    'adminPermissionsEnabled; needs an approved boundary first; nothing changed')
    management = api.find_client('realm-management')
    if not management:
        raise Error('BACKEND_CLIENT_INVALID: realm-management missing')
    admin_api_scope = api.admin_api_scope()
    roles = [api.request('GET', 'roles/' + role) for role in ('technical', 'otp-config-admin')]
    management_roles = [api.request('GET', 'clients/' + quote(management['id'], safe='') + '/roles/' + role)
                        for role in ('manage-users', 'view-users', 'query-users', 'view-realm', 'query-clients')]
    actual_subjects = []
    for i, name in enumerate(names):
        if not clients[i]:
            api.request('POST', 'clients', {'clientId': name, 'protocol': 'openid-connect', 'enabled': True,
                        'publicClient': False, 'bearerOnly': False, 'serviceAccountsEnabled': True,
                        'directAccessGrantsEnabled': False, 'standardFlowEnabled': False,
                        'implicitFlowEnabled': False, 'fullScopeAllowed': False})
            clients[i] = api.find_client(name)
            if not clients[i]:
                raise Error('BACKEND_CLIENT_INVALID: client creation failed')
        subject = api.subject(clients[i])
        api.reconcile(clients[i], subject, config['TECHNICAL_CLIENT_SECRET' if i == 0 else 'ADMIN_CLIENT_SECRET'],
                      [roles[i]], [] if i == 0 else management_roles, management['id'],
                      admin_api_scope=admin_api_scope if i == 1 else None)
        actual_subjects.append(subject)
    for i, name in enumerate(names):
        verify_client_token(api.http, config, name, actual_subjects[i], i == 1)
    print('TECHNICAL_SERVICE_SUBJECT=' + actual_subjects[0])
    print('ADMIN_SERVICE_SUBJECT=' + actual_subjects[1])
    if not config['prepare']:
        reconcile_app_client_urls(api, browser_client, config['app_client_urls'])
        reconcile_smtp_sync(api, config, sync_client, management, admin_api_scope)
        print(retire_published_client(api))
        reconcile_events(api)
    if config['retire']:
        api.retire(config['LEGACY_TECHNICAL_USERNAME'])
        api.retire(config['LEGACY_ADMIN_USERNAME'])
    if config['disable_realm_admin']:
        found = api.retire(config['REALM_ADMIN_USERNAME'])
        print('REALM_ADMIN_DISABLED' if found else 'REALM_ADMIN_ABSENT')
    print('BACKEND_CLIENTS_PREPARED' if config['prepare'] else 'BACKEND_CLIENTS_RECONCILED')


def main():
    try:
        reconcile(configuration(os.environ))
        return 0
    except Error as error:
        print(str(error), file=sys.stderr)
        return 2
    except Exception:
        print('BACKEND_CLIENT_RECONCILE_FAILED', file=sys.stderr)
        return 2

if __name__ == '__main__':
    sys.exit(main())
