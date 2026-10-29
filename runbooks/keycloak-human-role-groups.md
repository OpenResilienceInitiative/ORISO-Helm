# Human role groups: prepare and migrate before native enforcement

This is the opt-in migration tool for [Helm #424](https://github.com/OpenResilienceInitiative/ORISO-Helm/issues/424).
It moves classifiable human accounts into one explicitly reviewed role group without changing their effective realm or client roles. It does not enable native administration permissions, deploy anything, change user profiles, or migrate technical accounts.

The conditions in [Keycloak #56](https://github.com/OpenResilienceInitiative/ORISO-Keycloak/issues/56) remain a cutover gate. Passing these tests does not establish Dev acceptance or permission for a production migration.

## Review the mapping first

Supply a JSON file with `version: 1` and a `groups` array. Each entry has one canonical group path and its complete, exact application-role combination. Paths and combinations must be unique. The operator owns review of the realm inventory and mapping; the tool never invents account kinds or creates missing realm roles.

A mapping may use direct child groups under `/human-roles` or `/protected-admins`. Known platform/Träger administrator roles (`tenant-admin`, `single-tenant-admin`, `topic-admin`, `global-support-admin`) require a protected path. The reserved `/protected-admins/technical-identities` path cannot be used for human migration. Technical roles and Keycloak baseline roles cannot appear in a human mapping.

**For operators — mapping shape; select exact combinations from the realm copy and the approved account-kind contract:**

```json
{
  "version": 1,
  "groups": [
    {"path": "/human-roles/user", "roles": ["user"]},
    {"path": "/human-roles/consultant", "roles": ["consultant", "group-chat-consultant"]},
    {"path": "/human-roles/consultant-legacy", "roles": ["consultant"]},
    {"path": "/human-roles/agency-admin", "roles": ["restricted-agency-admin", "user-admin"]},
    {"path": "/human-roles/consultant-agency-admin", "roles": ["consultant", "group-chat-consultant", "restricted-agency-admin", "user-admin"]},
    {"path": "/protected-admins/platform-admins", "roles": ["tenant-admin", "agency-admin", "user-admin"]},
    {"path": "/protected-admins/platform-admins-with-topics", "roles": ["tenant-admin", "agency-admin", "user-admin", "topic-admin"]},
    {"path": "/protected-admins/traeger-admins", "roles": ["single-tenant-admin", "restricted-agency-admin"]}
  ]
}
```

These entries demonstrate distinct known source combinations; they are not a claim that every installation uses them. A Träger account may have a different exact combination. Preserve it in its own reviewed protected group, or leave it unclassified. Do not add a role to an existing user merely to match this example.

Existing target groups must already carry exactly the reviewed realm grants, with no extra client grants or inherited grants. An apply run can establish an explicitly mapped missing group using roles that already exist. It checks the new group and its ancestors before adding any user. It does not repair an overprivileged existing group automatically.

## Run on an isolated, quiescent realm copy

Use a restorable realm copy and stop account/group/role writes for the migration window. Native enforcement stays off until all users and policy conditions have been accepted. Concurrent administrator edits cannot be treated as an atomic migration or automatically overwritten.

Provide a short-lived, suitably privileged Admin API token through `ORISO_KEYCLOAK_ADMIN_TOKEN`, using the protected account-access mechanism. Do not paste the token into command arguments, logs, documentation or shell history. The tool prints account IDs and role/group names only. Treat the report as restricted operator data.

**For operators — read-only preview and report; the URL includes `/auth` if the installation uses it:**

```bash
umask 077
python3 scripts/migrate-keycloak-human-role-groups.py \
  --url https://auth.example.invalid/auth \
  --realm online-beratung \
  --mapping /secure/operator/reviewed-human-role-groups.json \
  > /secure/operator/human-role-groups-preview.json
```

Preview makes GET requests only. Inspect every unclassifiable or failed account before deciding whether to apply. Review the mapping, target groups and role inventory with the technical owners. This example URL is a placeholder; it is not a Dev deployment instruction.

**For operators — explicit apply on the same realm copy after reviewing its preview:**

```bash
python3 scripts/migrate-keycloak-human-role-groups.py \
  --url https://auth.example.invalid/auth \
  --realm online-beratung \
  --mapping /secure/operator/reviewed-human-role-groups.json \
  --apply \
  > /secure/operator/human-role-groups-apply.json
```

Run apply twice. Every successfully migrated account must say `unchanged` on the second run. Technical clients are reported separately as preserved. The native default users listing excludes service accounts by their model link, including renamed ones; the lazy-creating service-account-user endpoint is never used. `scanned` and `total` count only that human inventory.

The migration preserves direct Keycloak baseline roles. It removes only the direct application roles covered by the exact group mapping. Effective role snapshots include all realm clients, so default-role composites and client grants cannot disappear unnoticed.

## Reports and failure handling

| Exit | Meaning | Action |
|---|---|---|
| `0` | All reported humans classifiable and processed; technical clients preserved | Review readback, then run twice and verify browser behavior on the realm copy |
| `3` | Unknown role combination or client-role grants; those users unchanged | Review the inventory and provide an explicitly approved mapping or separate remediation |
| `2` | Unsafe group, API failure, invalid input, or incomplete readback | Stop; inspect the failed account and `scanned`/`total` before any retry |

For each known human the tool checks the exact group and current memberships, joins the group, and verifies both membership and unchanged effective roles. Only then does it remove covered direct grants. It reads roles and membership again afterwards.

If a request/readback fails after joining, the tool restores removed direct grants before leaving a newly joined group. A lost DELETE response is treated as possibly committed. If recovery cannot be verified, it reports **recovery incomplete**, stops further user processing and leaves any remaining group grant in place when restoration failed. It does not claim rollback succeeded or native enforcement is safe.

Users with unknown application-role combinations, direct client-role mappings, effective client permissions beyond the existing `account` baseline (`manage-account`, `view-profile`), or conflicting role-group memberships are not guessed into another kind. Missing roles are not created. All such findings remain a migration/cutover gate.

## Verification and remaining work

**For developers — public Admin HTTP tests; only disposable synthetic Keycloak realms are used:**

```bash
python3 tests/test_migrate_keycloak_human_role_groups.py -v
ORISO_REQUIRE_KEYCLOAK_DOCKER=1 python3 tests/keycloak_human_role_groups_migration_test.py -v
```

The first suite injects external API failures (ignored join, lost DELETE response) and unsafe mappings. The second suite verifies actual Keycloak 26.6.3 behavior on a digest-pinned isolated container: fresh/existing/twice, exact combined-role groups, protected variants, inherited overgrant rejection, unknown users, and unchanged technical accounts. The fixture removes the container it launches.

Still required before production enforcement: reviewed actual human inventory, new-account group creation in UserService, accepted protected-admin provisioning/pending-cleanup ownership, fresh policy reconciliation, real actor-token permissions for each ordinary combined kind, and Dev browser acceptance. Migration membership by an operator does not grant an account-create actor permission to join that group. Protected variants remain under the protected parent; the separate native policy owner must retain its denial cascade.
