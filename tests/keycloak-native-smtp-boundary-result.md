# Keycloak57 — native SMTP boundary result

**Measured on 14 October 2026: native-only SMTP field restriction does not fit the selected scenario C requirements.** This is a capability result for [Keycloak57](https://github.com/OpenResilienceInitiative/ORISO-Keycloak/issues/57), not technical approval of an exception or a production change.

The current SMTP control token has `realm-management/manage-realm`. The actual Keycloak receiver accepts unrelated settings with this token. Once native fine-grained admin permissions are enabled, the same role also allows editing the native policy store and granting itself user-management access. Ordinary application mail callers must not inherit this role.

## Receiver evidence

These are real client-credentials tokens and HTTP calls against the pinned Keycloak26.6.3 container. Every method creates fresh test realms; the fixture removes its realms and container. SMTP control has exactly one realm-management role, `manage-realm`. The mail fixture has only an application delivery role and no native administration.

| Test method | Actual receiver result | Fit for the SMTP-only requirement |
|---|---|---|
| `test_application_mail_role_has_no_native_administration` | Realm/SMTP updates, user operations, password reset, role writes and native policy creation return403; denied changes do not appear on readback. | Yes for native denials; actual application mail delivery is a separate proof. |
| `test_manage_realm_updates_smtp_but_also_unrelated_and_mixed_fields` | SMTP-only PUT204. Unrelated registration setting PUT204. Mixed SMTP/display-name PUT204. Both intended and unrelated changes persist. | **No.** There is no SMTP-field-only receiver limit. |
| `test_manage_realm_is_confined_to_its_own_realm` | Both actors are denied on a foreign realm; SMTP control is denied on master. Foreign SMTP remains unchanged. | Yes for realm isolation; does not limit fields inside its realm. |
| `test_removing_manage_realm_denies_smtp_and_unrelated_updates` | Without the role, SMTP and unrelated PUT403; unauthenticated PUT401. Realm remains unchanged. | Fail closed, but SMTP cannot be updated. |
| `test_fgap_rejects_realm_and_smtp_resource_permissions` | Native policy creation201; scope permissions for resource types `Realms` and `SMTP` both400. | **No.** Native FGAP does not expose this boundary. |
| `test_manage_realm_can_change_fgap_and_then_modify_users` | Existing-user PUT403 initially. SMTP token creates its own user policy201 and `Users/manage` permission201. Existing-user PUT then204, confirmed by readback. Ordinary mail remains403. | **No.** Grouping this privilege adds a native policy-management escape. |

The tests which expect204 on unrelated updates and policy escalation are **characterization of a blocking gap**, not acceptance of those permissions. The initial candidate test requiring403 for a mixed SMTP/unrelated update failed against the receiver with204. No production implementation was added to turn that failed requirement into an accepted behavior.

## Reproduce

**For developers — isolated real-token test, fail instead of skipping when Docker is absent:**
```sh
ORISO_REQUIRE_KEYCLOAK_DOCKER=1 python3 -B tests/keycloak_native_smtp_permissions_test.py -v
```

The existing CI discovery pattern `keycloak_*_test.py` includes this file. Its green regression result means the native capability and its limits were measured; it does not make the five-identity rollout ready.

Local verification on14 October2026: all6 methods passed in17.822 seconds with `ORISO_REQUIRE_KEYCLOAK_DOCKER=1`, zero skips. The earlier proposed narrow-boundary assertion failed with403 expected /204 actual. Python compilation and `git diff --check` also passed. CI and deployment were not run by this proof worker.

## Source explanation

The pinned upstream [RealmAdminResource](https://github.com/keycloak/keycloak/blob/26.6.3/services/src/main/java/org/keycloak/services/resources/admin/RealmAdminResource.java) checks realm-management permission before applying the complete realm representation. [RealmPermissions](https://github.com/keycloak/keycloak/blob/26.6.3/services/src/main/java/org/keycloak/services/resources/admin/fgap/RealmPermissions.java) uses `manage-realm` for this permission; [RealmPermissionsV2](https://github.com/keycloak/keycloak/blob/26.6.3/services/src/main/java/org/keycloak/services/resources/admin/fgap/RealmPermissionsV2.java) leaves that check in place and uses it for administration of the native permission client. [AdminPermissionsSchema](https://github.com/keycloak/keycloak/blob/26.6.3/server-spi-private/src/main/java/org/keycloak/authorization/fgap/AdminPermissionsSchema.java) registers Clients, Groups, Roles and Users, with no realm or SMTP field resource type.

## Minimum safe proposal and unresolved decision

Keep the existing short SMTP Job's control credential isolated. Ordinary grouped mail actors retain no `manage-realm` role. Do not change production bindings until Hassan and Shazia approve an explicit boundary and update the coordinated contract and credential inventory in [Helm367](https://github.com/OpenResilienceInitiative/ORISO-Helm/issues/367) / [Helm423](https://github.com/OpenResilienceInitiative/ORISO-Helm/issues/423).

This proposal preserves current isolation, but does **not** satisfy exactly five total actors when SMTP control remains separate. That exception and its native policy-management power must be recorded, or an SMTP-only receiving boundary must be approved and proved. This result does not choose or implement a provider, broker or extra accepted credential.

Still open: actual application delivery, the chosen SMTP-only boundary or approved temporary exception, missing-production-configuration behavior, coordinated production policy loading, runtime/mailbox acceptance and human technical acceptance. No Dev deployment, live exploit or real account change was performed.
