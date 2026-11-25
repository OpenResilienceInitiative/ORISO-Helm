{
  "clientId": "health-dashboard",
  "protocol": "openid-connect",
  "enabled": true,
  "publicClient": true,
  "standardFlowEnabled": true,
  "implicitFlowEnabled": false,
  "directAccessGrantsEnabled": false,
  "serviceAccountsEnabled": false,
  "fullScopeAllowed": false,
  "redirectUris": ["{{ include "oriso.publicOrigin" . }}/health/auth/callback"],
  "webOrigins": ["{{ include "oriso.publicOrigin" . }}"],
  "attributes": {
    "pkce.code.challenge.method": "S256",
    "post.logout.redirect.uris": "{{ include "oriso.publicOrigin" . }}/health/"
  },
  "defaultClientScopes": [],
  "optionalClientScopes": [],
  "protocolMappers": [
    {
      "name": "Dashboard audience",
      "protocol": "openid-connect",
      "protocolMapper": "oidc-audience-mapper",
      "config": {
        "included.client.audience": "health-dashboard",
        "access.token.claim": "true",
        "id.token.claim": "false"
      }
    },
    {
      "name": "Dashboard roles",
      "protocol": "openid-connect",
      "protocolMapper": "oidc-usermodel-client-role-mapper",
      "config": {
        "usermodel.clientRoleMapping.clientId": "health-dashboard",
        "claim.name": "resource_access.health-dashboard.roles",
        "jsonType.label": "String",
        "multivalued": "true",
        "access.token.claim": "true",
        "id.token.claim": "false"
      }
    }
  ]
}
