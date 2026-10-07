#!/usr/bin/env python3
"""Exact runtime task mappings; installation administrator is hook-local only."""
import json
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

MAX_RESPONSE = 1024 * 1024

class ReconcileError(Exception):
    """Fixed messages only; Keycloak errors may include credential payloads."""

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_):
        raise ReconcileError('TASK_IDENTITY_REDIRECT_DENIED')

class KeycloakApi:
    def __init__(self, url, realm, token):
        self.base=url.rstrip('/')+'/admin/realms/'+quote(realm,safe='')+'/'
        self.token=token
        self.opener=build_opener(ProxyHandler({}),NoRedirect())
    def request(self, method, path, body=None, missing=False):
        data=json.dumps(body,separators=(',',':')).encode() if body is not None else None
        req=Request(self.base+path,data=data,method=method,headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'})
        try:
            with self.opener.open(req,timeout=10) as response:
                raw=response.read(MAX_RESPONSE+1)
                if len(raw)>MAX_RESPONSE:raise ReconcileError('TASK_IDENTITY_RESPONSE_LIMIT')
                return json.loads(raw) if raw else None
        except HTTPError as error:
            status=error.code;error.close()
            if missing and status==404:return None
            raise ReconcileError('TASK_IDENTITY_ADMIN_API_REJECTED') from None
        except (URLError,ValueError,UnicodeError):
            raise ReconcileError('TASK_IDENTITY_ADMIN_API_UNAVAILABLE') from None

def preflight(tasks):
    if not isinstance(tasks,list) or not tasks:raise ReconcileError('TASK_IDENTITY_CONFIGURATION_MISSING')
    seen={name:set() for name in ['key','clientId','subject','secret']}
    for task in tasks:
        if set(task)!={'key','clientId','subject','secret','roles','audiences'}:raise ReconcileError('TASK_IDENTITY_SCHEMA_INVALID')
        for name,values in seen.items():
            value=task[name]
            if not isinstance(value,str) or not value.strip() or value=='changeme' or value in values:raise ReconcileError('TASK_IDENTITY_DUPLICATE_OR_MISSING')
            values.add(value)
        if not task['clientId'].startswith('backend-') or not isinstance(task['roles'],list) or not task['roles'] or not isinstance(task['audiences'],list):raise ReconcileError('TASK_IDENTITY_SCHEMA_INVALID')
        forbidden={'technical','tenant-admin','realm-admin','manage-users','impersonation'}
        if forbidden.intersection(task['roles']):raise ReconcileError('TASK_IDENTITY_BROAD_PERMISSION_DENIED')


def native_basic_scope(api):
    """The only retained native scope supplies identity claims, never grants."""
    matches = [scope for scope in api.request('GET', 'client-scopes') if scope.get('name') == 'basic']
    if len(matches) != 1:
        raise ReconcileError('TASK_IDENTITY_BASIC_SCOPE_MISSING')
    scope = matches[0]
    attributes = scope.get('attributes', {})
    if (scope.get('protocol') != 'openid-connect'
            or attributes.get('include.in.token.scope') != 'false'
            or attributes.get('display.on.consent.screen') != 'false'):
        raise ReconcileError('TASK_IDENTITY_BASIC_SCOPE_DRIFT')
    expected = {
        'auth_time': ('oidc-usersessionmodel-note-mapper', {'user.session.note': 'AUTH_TIME',
                     'id.token.claim': 'true', 'introspection.token.claim': 'true',
                     'access.token.claim': 'true', 'claim.name': 'auth_time', 'jsonType.label': 'long'}),
        'sub': ('oidc-sub-mapper', {'introspection.token.claim': 'true', 'access.token.claim': 'true'})}
    mappers = scope.get('protocolMappers', [])
    if len(mappers) != 2 or {mapper.get('name') for mapper in mappers} != set(expected):
        raise ReconcileError('TASK_IDENTITY_BASIC_SCOPE_DRIFT')
    for mapper in mappers:
        provider, config = expected[mapper['name']]
        if (mapper.get('protocol') != 'openid-connect' or mapper.get('protocolMapper') != provider
                or mapper.get('config') != config or mapper.get('consentRequired')):
            raise ReconcileError('TASK_IDENTITY_BASIC_SCOPE_DRIFT')
    mappings = api.request('GET', 'client-scopes/' + quote(scope['id'], safe='') + '/scope-mappings') or {}
    if mappings.get('realmMappings') or mappings.get('clientMappings'):
        raise ReconcileError('TASK_IDENTITY_BASIC_SCOPE_GRANTS_DENIED')
    return scope


def prepare_legacy_retirement(api, actors, administrator, tasks):
    """Verify the entire operator-pinned ownership set before any mutation."""
    if not actors:
        return []
    if not isinstance(actors, list) or len(actors) != 4:
        raise ReconcileError('LEGACY_RETIREMENT_CONFIGURATION_INVALID')
    clients = {'backend-technical', 'backend-admin'}
    expected_names = {'technical', 'svc-keycloak-admin',
                      'service-account-backend-technical', 'service-account-backend-admin'}
    subjects = set()
    prepared = []
    task_subjects = {task['subject'] for task in tasks}
    names = set()
    for actor in actors:
        if not isinstance(actor, dict) or set(actor) - {'subject', 'username', 'clientId'}:
            raise ReconcileError('LEGACY_RETIREMENT_CONFIGURATION_INVALID')
        subject, name = actor.get('subject'), actor.get('username')
        if (name not in expected_names or name in names or not isinstance(subject, str)
                or not subject or subject in subjects or subject in task_subjects
                or name.lower() == administrator.lower()):
            raise ReconcileError('LEGACY_RETIREMENT_OWNER_COLLISION')
        subjects.add(subject)
        names.add(name)
        user = api.request('GET', 'users/' + quote(subject, safe=''), missing=True)
        if not user or user.get('username') != name:
            raise ReconcileError('LEGACY_RETIREMENT_OWNER_MISMATCH')
        client = None
        if actor.get('clientId'):
            if actor['clientId'] not in clients:
                raise ReconcileError('LEGACY_RETIREMENT_CONFIGURATION_INVALID')
            matches = api.request('GET', 'clients?' + urlencode({'clientId': actor['clientId']}))
            if not isinstance(matches, list) or len(matches) != 1:
                raise ReconcileError('LEGACY_RETIREMENT_OWNER_MISMATCH')
            client = matches[0]
            linked = api.request('GET', 'clients/' + quote(client['id'], safe='') + '/service-account-user')
            if linked.get('id') != subject:
                raise ReconcileError('LEGACY_RETIREMENT_OWNER_MISMATCH')
        elif name.startswith('service-account-'):
            raise ReconcileError('LEGACY_RETIREMENT_CONFIGURATION_INVALID')
        prepared.append((user, client))
    return prepared


def retire_legacy(api, prepared):
    """Only called after explicit live consumer readback and exact owner checks."""
    for user, client in prepared:
        uid = quote(user['id'], safe='')
        for group in api.request('GET', 'users/' + uid + '/groups'):
            api.request('DELETE', 'users/' + uid + '/groups/' + quote(group['id'], safe=''))
        mappings = api.request('GET', 'users/' + uid + '/role-mappings') or {}
        if mappings.get('realmMappings'):
            api.request('DELETE', 'users/' + uid + '/role-mappings/realm', mappings['realmMappings'])
        for mapping in mappings.get('clientMappings', {}).values():
            if mapping.get('mappings'):
                api.request('DELETE', 'users/' + uid + '/role-mappings/clients/' + quote(mapping['id'], safe=''), mapping['mappings'])
        api.request('PUT', 'users/' + uid, {'enabled': False})
        if client:
            api.request('PUT', 'clients/' + quote(client['id'], safe=''), {'enabled': False})
        actual = api.request('GET', 'users/' + uid)
        grants = api.request('GET', 'users/' + uid + '/role-mappings') or {}
        if actual.get('enabled') or grants.get('realmMappings') or grants.get('clientMappings'):
            raise ReconcileError('LEGACY_RETIREMENT_READBACK_FAILED')
    if prepared and api.request('GET', 'roles/TECHNICAL_DEFAULT', missing=True):
        api.request('DELETE', 'roles/TECHNICAL_DEFAULT')


def reconcile(url, realm, token, tasks, legacy_actors=None, administrator=""):

    preflight(tasks)
    api=KeycloakApi(url,realm,token)
    basic = native_basic_scope(api)
    legacy = prepare_legacy_retirement(api, legacy_actors, administrator, tasks)
    prepared=[]
    # Check every ownership collision before any mutation. Do not overwrite a
    # human/service account's UUID to make configuration appear consistent.
    for task in tasks:
        clients=api.request('GET','clients?'+urlencode({'clientId':task['clientId']}))
        if not isinstance(clients,list) or len(clients)>1:raise ReconcileError('TASK_IDENTITY_CLIENT_COLLISION')
        client=clients[0] if clients else None
        user=api.request('GET','clients/'+quote(client['id'],safe='')+'/service-account-user',missing=True) if client else None
        occupied=api.request('GET','users/'+quote(task['subject'],safe=''),missing=True)
        if user and user.get('id')!=task['subject']:raise ReconcileError('TASK_IDENTITY_SUBJECT_MISMATCH')
        if occupied and (not user or occupied.get('id')!=user.get('id')):raise ReconcileError('TASK_IDENTITY_SUBJECT_COLLISION')
        usernames = api.request('GET', 'users?' + urlencode({'exact': 'true', 'username': 'service-account-' + task['clientId']}))
        if (not isinstance(usernames, list) or len(usernames) > 1
                or any(not user or entry.get('id') != user.get('id') for entry in usernames)):
            raise ReconcileError('TASK_IDENTITY_USERNAME_COLLISION')
        roles=[]
        for name in task['roles']:
            role=api.request('GET','roles/'+quote(name,safe=''),missing=True)
            if role and role.get('composite'):raise ReconcileError('TASK_IDENTITY_COMPOSITE_PERMISSION_DENIED')
            roles.append((name,role))
        prepared.append((task,client,user,roles))
    for task,client,user,roles in prepared:
        desired=[]
        for name,role in roles:
            # Several clients intentionally share the narrow account-read role.
            # Preflight ran before mutations, so an earlier task may have created it.
            if not role:
                role = api.request('GET', 'roles/' + quote(name, safe=''), missing=True)
            if not role:
                api.request('POST','roles',{'name':name,'description':'Config Wizard' if name=='config-wizard' else 'Restricted ORISO task: '+name,'composite':False})
                role=api.request('GET','roles/'+quote(name,safe=''))
            desired.append(role)
        config={'clientId':task['clientId'],'enabled':True,'protocol':'openid-connect','publicClient':False,'bearerOnly':False,'clientAuthenticatorType':'client-secret','secret':task['secret'],'standardFlowEnabled':False,'implicitFlowEnabled':False,'directAccessGrantsEnabled':False,'fullScopeAllowed':False,'serviceAccountsEnabled':False,'defaultClientScopes':['basic'],'optionalClientScopes':[]}
        if not client:
            api.request('POST','clients',config)
            clients=api.request('GET','clients?'+urlencode({'clientId':task['clientId']}))
            if len(clients)!=1:raise ReconcileError('TASK_IDENTITY_CLIENT_READBACK_FAILED')
            client=clients[0]
        cid=quote(client['id'],safe='')
        if not user:
            # Supported Keycloak 26.6.3 partial import retains a provided UUID
            # and resolves serviceAccountClientId to the newly created client.
            api.request('POST','partialImport',{'ifResourceExists':'FAIL','users':[{'id':task['subject'],'username':'service-account-'+task['clientId'],'enabled':True,'serviceAccountClientId':task['clientId']}]})
        config['serviceAccountsEnabled']=True
        api.request('PUT','clients/'+cid,config)
        user=api.request('GET','clients/'+cid+'/service-account-user')
        if user.get('id')!=task['subject']:raise ReconcileError('TASK_IDENTITY_SERVICE_ACCOUNT_READBACK_FAILED')
        uid=quote(user['id'],safe='')
        # Remove direct mappings AND inherited group/scoping grants on this
        # verified task account only. Unrelated human accounts are untouched.
        groups=api.request('GET','users/'+uid+'/groups')
        for group in groups:api.request('DELETE','users/'+uid+'/groups/'+quote(group['id'],safe=''))
        mappings=api.request('GET','users/'+uid+'/role-mappings') or {}
        if mappings.get('realmMappings'):api.request('DELETE','users/'+uid+'/role-mappings/realm',mappings['realmMappings'])
        for mapping in mappings.get('clientMappings',{}).values():
            if mapping.get('mappings'):api.request('DELETE','users/'+uid+'/role-mappings/clients/'+quote(mapping['id'],safe=''),mapping['mappings'])
        api.request('POST','users/'+uid+'/role-mappings/realm',desired)
        for kind in ['default','optional']:
            for scope in api.request('GET','clients/'+cid+'/'+kind+'-client-scopes'):
                if kind != 'default' or scope['id'] != basic['id']:
                    api.request('DELETE','clients/'+cid+'/'+kind+'-client-scopes/'+quote(scope['id'],safe=''))
        api.request('PUT', 'clients/' + cid + '/default-client-scopes/' + quote(basic['id'], safe=''))
        for mapper in api.request('GET','clients/'+cid+'/protocol-mappers/models'):
            api.request('DELETE','clients/'+cid+'/protocol-mappers/models/'+quote(mapper['id'],safe=''))
        role_mapper={'name':'task-realm-roles','protocol':'openid-connect','protocolMapper':'oidc-usermodel-realm-role-mapper','config':{'multivalued':'true','claim.name':'realm_access.roles','jsonType.label':'String','access.token.claim':'true','id.token.claim':'false'}}
        api.request('POST','clients/'+cid+'/protocol-mappers/models',role_mapper)
        for audience in task['audiences']:
            api.request('POST','clients/'+cid+'/protocol-mappers/models',{'name':'audience-'+audience,'protocol':'openid-connect','protocolMapper':'oidc-audience-mapper','config':{'included.custom.audience':audience,'access.token.claim':'true','id.token.claim':'false'}})
        current=api.request('GET','clients/'+cid+'/scope-mappings/realm')
        if current:api.request('DELETE','clients/'+cid+'/scope-mappings/realm',current)
        api.request('POST','clients/'+cid+'/scope-mappings/realm',desired)
        actual=api.request('GET','users/'+uid+'/role-mappings')
        if sorted(r['name'] for r in actual.get('realmMappings',[]))!=sorted(task['roles']) or actual.get('clientMappings'):raise ReconcileError('TASK_IDENTITY_MAPPING_READBACK_FAILED')
    retire_legacy(api, legacy)
    return len(tasks)


def login(env):
    required=['KEYCLOAK_URL','KEYCLOAK_REALM','KEYCLOAK_ADMIN_USERNAME','KEYCLOAK_ADMIN_PASSWORD','ORISO_TASK_IDENTITIES_JSON']
    if any(not env.get(name) for name in required):raise ReconcileError('TASK_IDENTITY_CONFIGURATION_MISSING')
    parsed=urlsplit(env['KEYCLOAK_URL'])
    if parsed.scheme not in ('http','https') or parsed.username or parsed.password or parsed.query or parsed.fragment:raise ReconcileError('TASK_IDENTITY_URL_INVALID')
    # Only the configured internal cluster Keycloak may use HTTP.
    namespace=env.get('POD_NAMESPACE','default')
    allowed={'keycloak','keycloak.'+namespace,'keycloak.'+namespace+'.svc','keycloak.'+namespace+'.svc.cluster.local','localhost','127.0.0.1'}
    if parsed.scheme=='http' and parsed.hostname not in allowed:raise ReconcileError('TASK_IDENTITY_PUBLIC_HTTP_DENIED')
    body=urlencode({'grant_type':'password','client_id':'admin-cli','username':env['KEYCLOAK_ADMIN_USERNAME'],'password':env['KEYCLOAK_ADMIN_PASSWORD']}).encode()
    opener=build_opener(ProxyHandler({}),NoRedirect())
    for attempt in range(60):
        try:
            req=Request(env['KEYCLOAK_URL'].rstrip('/')+'/realms/master/protocol/openid-connect/token',data=body,headers={'Content-Type':'application/x-www-form-urlencoded'})
            with opener.open(req,timeout=10) as result:
                raw=result.read(MAX_RESPONSE+1)
                if len(raw)>MAX_RESPONSE:raise ReconcileError('TASK_IDENTITY_RESPONSE_LIMIT')
                token=json.loads(raw).get('access_token')
                if not token:raise ReconcileError('TASK_IDENTITY_ADMIN_TOKEN_INVALID')
                return token
        except (HTTPError,URLError):
            if attempt==59:raise ReconcileError('TASK_IDENTITY_KEYCLOAK_NOT_READY') from None
            time.sleep(5)
    raise ReconcileError('TASK_IDENTITY_KEYCLOAK_NOT_READY')

if __name__=='__main__':
    try:
        tasks=json.loads(os.environ.get('ORISO_TASK_IDENTITIES_JSON','null'));preflight(tasks)
        token=login(os.environ)
        count=reconcile(os.environ['KEYCLOAK_URL'],os.environ['KEYCLOAK_REALM'],token,tasks,
                        json.loads(os.environ.get('ORISO_LEGACY_RETIREMENT_JSON', '[]')),
                        os.environ['KEYCLOAK_ADMIN_USERNAME'])
        print('TASK_IDENTITIES_RECONCILED: '+str(count)+' bounded clients; legacy retirement requires verified consumer readback')
    except Exception as error:
        print(str(error) if isinstance(error,ReconcileError) else 'TASK_IDENTITY_RECONCILE_FAILED',file=sys.stderr)
        sys.exit(1)
