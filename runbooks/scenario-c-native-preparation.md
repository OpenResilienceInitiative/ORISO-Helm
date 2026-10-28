# Scenario C native policy preparation

This standalone installer tool prepares the five selected actors and the tested
group permission model on an **isolated realm copy**. It leaves every new actor
client disabled. The chart does not invoke it and no runtime caller uses these
credentials yet. It does not close [Helm413](https://github.com/OpenResilienceInitiative/ORISO-Helm/issues/413)
or [Helm423](https://github.com/OpenResilienceInitiative/ORISO-Helm/issues/423).

The registry is `files/scenario-c-task-identities.json`. The five actor clients
are `backend-setup`, `backend-account-create`, `backend-account-admin`,
`backend-mail` and `backend-runtime`. A purpose identifies an operation, not a
sixth credential. Roles and bearer audiences match the selected C contract.
Preparation installs direct subject, realm-role and fixed audience mappers,
with the exact realm-role scope union and no inherited default or optional
client scopes. It reads those settings back before reporting convergence.
Unknown mappers, broader role scopes and inherited scopes cause refusal before
any preparation writes. Secret distribution and complete caller activation
remain coordinated source and installation gates.

## Prepare a reviewed realm copy

1. Use the [human role migration](../scripts/migrate-keycloak-human-role-groups.py)
   from [Helm424](https://github.com/OpenResilienceInitiative/ORISO-Helm/issues/424).
   Review the exact role combinations and the unknown-user report before
   switching any production permissions. Reuse that version 1 mapping JSON.
2. All reviewed groups must already exist. The realm also needs empty roots
   `/human-roles`, `/protected-admins`, `/pending-provisioning`, and the empty
   `/protected-admins/technical-identities` group. The tool refuses unknown
   descendants because group permissions inherit through the hierarchy.
3. Provide a short-lived **installer** token through
   `ORISO_KEYCLOAK_INSTALLER_TOKEN`. Do not supply a runtime actor token or put a
   token in command arguments. The tool neither reads passwords nor emits
   credentials. It uses native Admin REST operations, not realm export re-import.

An ordinary reviewed counselling-centre administrator combination may remain
under `/human-roles/agency-admin`. Platform and Träger administrator combinations
belong under `/protected-admins`. The explicit mapping preserves the approved
effective roles; it does not classify every name ending in `admin` the same way.

**For developers — validate and inspect the isolated copy:**
```bash
python3 scripts/reconcile-keycloak-scenario-c.py --validate-only
python3 scripts/reconcile-keycloak-scenario-c.py \
  --url https://isolated-keycloak.example.invalid/auth \
  --realm reviewed-copy --mapping reviewed-human-role-groups.json
```

The default run reads groups, grants, client metadata and existing user UUIDs.
It performs no client creation, role mapping or lazy service-account GET. Its
managed subject receipt is a preparation record; dry-run explicitly reports
`native_service_accounts_verified=false` because ordinary user representations
do not independently expose the service-account link.

## Apply only to the isolated copy

**For developers — explicit sandbox preparation and convergence check:**
```bash
python3 scripts/reconcile-keycloak-scenario-c.py \
  --url https://isolated-keycloak.example.invalid/auth \
  --realm reviewed-copy --mapping reviewed-human-role-groups.json \
  --apply --confirm-isolated-realm
```

Repeat the same command: the second successful run reports `changes=0`.
Apply binds each managed client to its authoritative native service-account
UUID before role or policy updates. The original disabled-client POST includes
an owned pending receipt bound to the actual realm, actor, registry and reviewed
mapping. After an interrupted POST or subject-receipt PUT, repeat the same
command with the same inputs. Read-only inspection must first confirm the exact
pending shape without invoking the lazy service-account API. Confirmed apply
then verifies the native UUID and unchanged safe shape before replacing the
pending marker atomically with the completed receipt. A lost final PUT reply
also converges on that same UUID. Existing actors without either exact receipt
are refused: migration/adoption requires separate operator inspection; the tool
does not guess ownership from a username.

The tool creates disabled confidential clients with service accounts, no
interactive/password grants and no full scopes. It assigns the exact actor
role unions, removes the realm's actual native default-role mappings from
these actors only, and places them in the protected technical group. Legacy
clients/users and unrelated human accounts are preserved.

Native reconciliation installs four user policies and five **Groups-only**
scope permissions. Only `ACCOUNT_CREATE` reaches the pending group and may join
ordinary role groups; `ACCOUNT_ADMIN` separately manages ordinary role
members. Protected groups deny those member operations. No Users/Roles or broad
native administration permission is used. Unknown permissions, native
administration grants, composite actor roles and unreviewed group children
cause refusal. Policy associations, native resource group IDs, scopes, subjects
and actor grants must match the actual API readback before success is reported.

## Gates that remain open

- [Keycloak56](https://github.com/OpenResilienceInitiative/ORISO-Keycloak/issues/56)
  still needs technical acceptance of the native fit and production lifecycle.
  This tool does not authorize protected account creation or prove safe account
  activation after a crash. Ordinary combined role groups must preserve their
  exact domain authorization in UserService and the receiving services.
- [Keycloak57](https://github.com/OpenResilienceInitiative/ORISO-Keycloak/issues/57)
  owns the SMTP gap. MAIL receives **no** native realm-setting permission here;
  an SMTP call is denied until an accepted receiving boundary exists. Do not add
  `manage-realm` to make that call work.
- Native session exchange, custom OTP target authorization, existing-user
  migration/adoption, caller/audience/secret wiring, legacy consumer retirement,
  deployment and browser/mail acceptance remain separate gates.

## Verification

**For developers — public CLI/HTTP and pinned native receiver tests:**
```bash
python3 -m unittest discover -s tests -p test_reconcile_keycloak_scenario_c.py -v
ORISO_REQUIRE_KEYCLOAK_DOCKER=1 python3 tests/keycloak_scenario_c_reconcile_test.py -v
```

The native test imports the rendered Helm realm into a fresh pinned Keycloak
26.6.3, prepares the policy through installer REST, checks a second reconciliation,
preserves existing ordinary/protected humans, and verifies allowed/denied calls
with real actor tokens. Enabling clients and setting synthetic secrets happens
only inside the isolated permission fixture. It is not runtime cutover proof.


## Receiver-only preparation (source completion work)

The measured native group permissions permit changes that the account workflow must refuse. Once bounded account receivers/callers have been installed and verified together, runtime activation must remove those stock Admin paths. The additional preparation mode below makes all nine managed native policies refuse account subjects. It keeps all five clients disabled and does not activate the runtime, rotate secrets or retire old consumers.

**For operators — isolated copy only, before any separately reviewed cutover:**
```text
scripts/reconcile-keycloak-scenario-c.py --url https://<copy>/auth \
  --realm <reviewed-realm> --mapping <reviewed-human-role-groups.json> \
  --apply --confirm-isolated-realm --receivers-only
```

Run the same command twice: the second report must show no changes. A native receipt on each managed client prevents the older preparation command from restoring the unsafe paths. Unknown actor/role/group/native permission state is still refused. This receipt is preparation metadata, not proof that a receiver or a legitimate maintenance/OTP caller works.

The source has process/HTTP regression tests for the policy change, second-run convergence and downgrade refusal. Real native denial plus intended lifecycle/maintenance/OTP acceptance remains a separate required gate. Existing native preparation/spike results describe the previous permission model and are preserved separately.

The additional native token test enables only the five disposable fixture
clients after disabled preparation has converged twice. It checks each native
subject, issuer, client, exact realm-role union and exact audience union, and
refuses unexpected client roles. This verifies prepared token claims; it does
not enable actors on Dev or Staging or prove the combined account workflows.

## Bind the reviewed protected invitation policy

The five-actor chart requires an existing ConfigMap holding the native protected
invitation policy and the two profile identifiers used by UserService. The
native policy contains installation metadata: actual realm/issuer, five actor
subjects, human bindings, protected profiles and current native group/role IDs.
It contains no credentials. Review it against the installed realm before
activation; the chart does not invent human subjects or adopt existing accounts.

**For operators — values pointing at independently reviewed installation data:**
```yaml
global:
  taskIdentities:
    accountMaintenanceConfigMapRef:
      name: reviewed-ordinary-account-policy
      key: policy.json
    protectedInvitations:
      configMapRef:
        name: reviewed-protected-invitation-policy
        key: policy.json
      profileIds:
        TENANT_ADMIN: reviewed-tenant-admin-v1
        PLATFORM_ADMIN: reviewed-platform-admin-v1
```

The ConfigMap key must contain the complete JSON accepted by the installed
Keycloak `ProtectedInvitationConfiguration`. Both profile identifiers must match
that policy exactly. UserService and Keycloak receive the same policy ConfigMap
reference. Only Keycloak receives provider enablement; only UserService receives
the profile-ID mapping. The other services receive neither. The existing shared
context-key Secret remains the sole key.

The separate ordinary policy reference supplies the installed native
`GeneralPolicy` JSON to UserService and Keycloak through
`ORISO_ACCOUNT_MAINTENANCE_CONFIG`.
Its native-human checks and finite ordinary self-operation list remain separate
from protected invitation and account-operation grants. It may use another key
in the same ConfigMap, but it cannot alias the protected policy key. No other
service receives this policy. The UserService password caller derives its bounded
catalog from these reviewed profile IDs and explicit versions. Native current
profile, provenance and permission checks still apply on every request. A mounted
policy is installation data; it does not authorize a command or prove readiness.

The final `global.taskIdentities.model=FIVE_ACTORS` registry replaces the earlier
`global.keycloak.taskIdentities` preparation. Set both historical `enabled` and
`provisionOnly` flags to `false` before selecting the final registry. The chart
rejects a mixed upgrade so the historical reconciler and context secrets cannot
override the reviewed final actor bindings and shared context key.

The retained asker contact-email and deletion-start flows require explicit
`account.email` and `account.deactivate` entries on the installed asker profile.
They are SELF grants: the current human must still own the exact native account.
Do not add a self hard-delete grant or grant another role's profile operations.
The complete asker list below fits the existing ten-operation bound. Keep the
actual installed profile ID, version, group IDs and role set unchanged; this is
the operation list within that reviewed profile, not a replacement policy.

**For operators — asker SELF operations when both existing OTP modes are enabled:**
```json
{
  "selfOperations": [
    "account.read", "account.locale", "account.email", "account.password",
    "account.deactivate", "otp.read", "otp.app-setup", "otp.delete",
    "otp.email-start", "otp.email-finish"
  ]
}
```

Verify the same installed JSON with the matching UserService and Keycloak
builds before activation. UserService must accept this list without changing
its derived account selectors. Rendering a ConfigMap reference does not prove
that these grants are installed or that the account flow has passed its tests.

Missing, ambiguous or duplicate bindings stop rendering in five-actor mode. A
missing ConfigMap/key stops container configuration; invalid native policy stops
the affected protected provider from becoming available. These checks do not
assert receiver readiness or authorize cutover. Legacy mode emits none of these
bindings. Source/native pairing and coordinated startup checks remain required.

The ordinary native policy's optional `adminOperations` list defaults to empty.
It contains reviewed grants for `PLATFORM` or `TENANT_PEER`, exact installed
`targetProfiles`, a finite `operations` list and `selfOnly: false`. This does not
replace the separate self-operation list or protected-account policy. Configure
only the operations required by the corresponding human role; an empty list
does not authorize another person's account maintenance.

UserService selects an ordinary role change from the existing
`global.taskIdentities.ordinaryGroups` map. That same reviewed map is mounted as
`ORISO_ACCOUNT_LIFECYCLE_GROUPS`; there is no second mapping or built-in group
name. Its complete role sets must match the installed native profiles. A combined
role and profile edit requires both operations on the current and desired
profiles. Missing or ambiguous matches refuse the change.

The chart mounts references to existing policy data. It cannot validate the
contents of an external ConfigMap or prove that every service runs the matching
image. Before cutover, verify the configured caller graph and the native
receivers together on the final source revisions. Rendering this chart alone
does not satisfy that gate.

## Current SMTP credential contract

SMTP_SYNC, smtp-sync and the oriso-task-commands bearer audience belong to SETUP.
MAIL keeps SYSTEM_EMAIL_DELIVERY and NOTIFICATION_DISPATCH; its roles are exactly
system-email-delivery, notification-dispatch and notifications-technical and its
four audiences are tenantservice, agencyservice, consultingtypeservice and userservice.
The SMTP helper receives only the SETUP secret plus the sole ORISO_TASK_CONTEXT_KEY.
MAIL with a valid common-key command proof is still refused by the native receiver.
UserService holds all five actor credentials: this is credential separation and
makes no process-isolation guarantee for a compromised UserService process.


## Explicit receiver activation (default off)

Prepare the isolated realm with the existing reconciler's `--receivers-only`
mode. Keep all five clients disabled while installing the three same-source
provider JARs and the reviewed ordinary/protected policy and actor bindings.
Record the actual build fingerprints for account-lifecycle, OTP and SMTP; these
inputs are compared with the running factories, not accepted as runtime evidence.

The opt-in chart path is `global.taskIdentities.activation.enabled: true` with
`confirmIsolatedRealm: true`, an existing short-lived installer-token Secret,
reviewed mapping ConfigMap, and the exact three `providerJarSha256` values.
The Job invokes the packaged `keycloak-activate-scenario-c.py`; it uses the same
five actor secrets, subjects, sole context key and policy references as UserService.
The standalone equivalent is the existing reconciler with `--apply`,
`--confirm-isolated-realm`, `--activate-receivers` and `--activation-bindings`.
Neither mode retires legacy clients or consumers.

Activation checks native groups, roles, client ownership and subjects before it
enables only the managed set. It then issues all five real tokens, checks exact
claims and native Admin denial, requests the fixed authenticated installation
status from the loaded lifecycle provider, and verifies SETUP SMTP access plus
MAIL refusal with a valid shared-key proof. Loaded policy/binding/JAR hashes and
these behavior probes are separate checks. An unavailable or mismatching probe
disables owned staged clients. On repeat, detected native drift also disables
only clients whose prior receipt and current subject still prove ownership.
Foreign clients remain untouched and cause refusal. Repeated success changes no
mapping or secret. An unsuccessful disable readback is explicitly reported.

The native fixture `tests/scenario_c_activation_native.py --keycloak <owned-source>`
mounts the three locally packaged JARs into the pinned stock image (no image
build), runs the chart entry point, and verifies repeat, drift shutdown and
recovery. Final caller/receiver and installed-image evidence must still be pinned
to the common source tuple before any authorized environment cutover.


## Reviewed ordinary operator policy

The optional `ordinaryAdminOperations` entries in the existing maintenance-policy
ConfigMap name exact installed `sourceProfiles`, `targetProfiles` and `operations`.
Keep the default empty. Current ordinary Agency Admin/combined sources require
original native birth or authenticated adoption, a current native human session,
exact grants and a positive same tenant. UserService additionally checks SQL agency
reach. ASKER targets allow only read/deactivate. Consultant profiles permit full
profile/email fields and preserving group-chat toggles; combined Consultant/Admin
profiles permit those Consultant edits but never whole-account deactivation.
`account.ordinary-profile` requires `account.profile` on both installed profiles.
Nonsecret two-factor booleans use the same account.read grant; administrative OTP
mutation and peer Agency Admin CRUD are not granted.

For the existing same-person first Consultant assignment, declare both the original
Agency Admin and each permitted combined destination ID in the same entry's
`sourceProfiles` and `targetProfiles`, with account.read/account.profile-transition.
The catalogue supplies the exact installed versions. Declare legitimate variants
with and without group-chat explicitly. Only source==target plus
ADD_CONSULTANT_IDENTITY/group is accepted; missing destination acknowledgement
refuses before effect. Current fresh native-token replay follows the authenticated
lineage. This does not decide the separately pending self-Administrator-removal
product policy.
