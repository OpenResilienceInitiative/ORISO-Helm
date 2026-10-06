# Backend service clients

Internal service requests now use two independent confidential clients. Human app and Admin sign-in keep their existing password and OTP flow. Complete this migration before enabling brute-force protection (ORISO-UserService#1351; ORISO-Helm#402).

Fresh realm imports contain the clients. Importing a realm never updates an existing realm. **Never run a full Helm install or upgrade with preparation enabled.** Only the three reviewed preparation resources may be applied. An upgrade with preparation enabled fails during rendering. The preparation job is a deliberate operator step; a routine release does not create missing clients or retire old password users silently.

**For operators — configuration to prepare before deployment:**
```text
global.keycloak.backendTechnicalClientId: backend-technical
global.keycloak.serviceAdminClientId: backend-admin
global.secrets.keycloakBackendTechnicalClientSecret: independently generated persistent secret
global.secrets.keycloakBackendAdminClientSecret: a different persistent secret

global.keycloak.serviceTechUserId: actual technical service-account UUID
global.keycloak.serviceAdminSubject: actual admin service-account UUID
Fresh realm export UUIDs only:
technical = 12316d09-a9da-41b9-a13e-ee2c515800b5
admin     = 615a7bf8-3e12-40c7-a949-f88640acea8e
Existing realms have their own UUIDs; never use fresh export IDs there.
```

1. Review the realm export, UserService and ConsultingTypeService companions together. Keep the old password users available until the new backends have been checked.
2. On an existing realm, run the preparation job alone with the new client secrets, before deploying the backend changes. It prints both actual service-account UUIDs. Store those IDs in persistent deployment values. Use placeholders only for this explicit preparation invocation; normal mode validates both actual IDs before changing anything.
3. Run normal reconciliation with the actual IDs. It reconciles the exact roles and token scopes, issues both client tokens, and verifies their account IDs, client IDs, expiry and rights. A failure prevents retirement of the old users.
4. Deploy the Java companions and this chart with the same independent secrets and actual IDs. Exercise invitations, settings, user management and mail. Secret, client ID or subject changes replace the affected backend Pods automatically.
5. Only after that verification, set retirement true for an explicitly reviewed upgrade. This disables the two old password users and revokes their sessions. Keep the master-realm recovery identity separate.

**For operators — bounded preparation using rendered review files:**
```bash
# Render the candidate with the reviewed deployment values; keep this output private.
# This does not apply the whole chart. Run only the script ConfigMap and Job below.
helm template service-clients . --namespace "$NAMESPACE" \
  -f values.yaml.default -f "$ENVIRONMENT_VALUES" -f "$PERSISTENT_SECRET_VALUES" \
  --set global.keycloak.backendServiceClients.prepareOnly=true \
  --set global.keycloak.backendServiceClients.retireLegacyUsers=false \
  --set-string global.keycloak.serviceTechUserId=00000000-0000-4000-8000-000000000000 \
  --set-string global.keycloak.serviceAdminSubject=00000000-0000-4000-8000-000000000001 \
  --show-only templates/userservice/backend-service-clients-secret.yaml \
  --show-only templates/keycloak-reconcile-service-identities-job.yaml \
  > "$PRIVATE_REVIEW_DIR/backend-clients-prepare.yaml"
# Review these three resources: dedicated client Secret, script ConfigMap, preparation Job.
# Their names must match the prepared private values and existing master-recovery Secret.
# Apply ONLY this reviewed file; it does not alter old backend Secrets or ConfigMaps.
kubectl --namespace "$NAMESPACE" apply -f "$PRIVATE_REVIEW_DIR/backend-clients-prepare.yaml"
kubectl --namespace "$NAMESPACE" wait --for=condition=complete \
  job/keycloak-prepare-backend-clients --timeout=600s
kubectl --namespace "$NAMESPACE" logs job/keycloak-prepare-backend-clients
# Read both printed UUIDs into persistent values, then restore prepareOnly=false.
# Keep retireLegacyUsers=false until the new backends have passed the checks above.
# Do not log Secret contents, bearer tokens, or private rendered deployment values.
```

**For developers — permission and release contract:**
```text
Technical: realm role technical; no realm-management roles.
Admin: realm role otp-config-admin; manage-users, view-users, query-users, view-realm.
Existing view-users composite also yields query-groups; no realm-admin/impersonation grant.
Clients: confidential, serviceAccountsEnabled, fullScopeAllowed false,
no direct password grant, no browser/implicit flows, only the standard roles scope.
Job prepareOnly default false; retireLegacyUsers default false.
Normal mode rejects missing clients, wrong UUIDs, public/human-flow clients,
and invalid final grants before legacy retirement.
No password fallback. Legacy bootstrap never recreates technical.
Token exchange retains existing requested_subject semantics and Optional.empty
on unsupported exchanges; no preview feature or new impersonation permission.
No shared realm is changed by source tests or PR creation.
```

Rollback requires a reviewed operator action: restore the prior application/chart versions and credentials, re-enable the old users deliberately if they were retired, and verify their rights before use. Disabling brute-force protection alone does not restore retired service users.
