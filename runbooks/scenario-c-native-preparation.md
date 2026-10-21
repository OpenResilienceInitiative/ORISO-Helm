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
UUID before role or policy updates. Existing actors without the preparation
receipt are refused: migration/adoption is a separate explicit gate. An
interrupted preparation without a completed receipt needs operator inspection;
the tool does not guess ownership from a username.

The tool creates disabled confidential clients with service accounts, no
interactive/password grants and no full scopes. It assigns the exact actor
role unions, removes the realm's actual native default-role mappings from
these actors only, and places them in the protected technical group. Legacy
clients/users and unrelated human accounts are preserved.

Native reconciliation installs three user policies and four **Groups-only**
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

The measured native group permissions permit changes that the account workflow must refuse. Once bounded account receivers/callers have been installed and verified together, runtime activation must remove those stock Admin paths. The additional preparation mode below makes all seven managed native policies refuse account subjects. It keeps all five clients disabled and does not activate the runtime, rotate secrets or retire old consumers.

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
