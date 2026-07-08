# Test-credential store (SOPS + age)

A **central, structured, multi-user** store of test-user credentials that both
humans and automation (AI agents) can read, kept encrypted in git. Solves two
things at once: rapidly creating many test users without fighting 2FA, and
never losing the credentials once created.

Background and rationale: `docs/infrastructure-report-2026-07.md` §6. Tracking:
issue #23.

> **Production credentials never go here.** This store is for `predev`/dev test
> accounts only. Production secrets remain the hoster's domain (Vault / ArgoCD).

## Why 2FA is not in the way

OTP/2FA is enforced only at *interactive login*. Users created through the
Keycloak Admin API get **no `CONFIGURE_TOTP` required action**, so they log in
(or fetch a direct-grant token) with username + password alone. The seeding
script below creates users exactly that way — see
`scripts/seed-keycloak-users.sh`.

## Files

| File | Committed? | Contents |
|---|---|---|
| `.sops.yaml` | yes | Recipients + which fields get encrypted |
| `test-users.example.json` | yes | Schema example, `changeme` placeholders only |
| `test-users.enc.json` | yes | The real store — **only `password` values are ciphertext**; usernames/env/role stay readable so diffs are reviewable |
| `*.plain.json`, `test-users.json` | **no** (gitignored) | Any decrypted copy — never commit |

## One-time setup (per person)

1. Install [`sops`](https://github.com/getsops/sops) and
   [`age`](https://github.com/FiloSottile/age).
2. Generate your key and note the **public** line:
   ```bash
   mkdir -p ~/.config/sops/age
   age-keygen -o ~/.config/sops/age/keys.txt      # prints: public key: age1...
   ```
3. Send your **public** key to a maintainer. They add it to `.sops.yaml` and run
   `sops updatekeys test-data/test-users.enc.json`. You can now decrypt.
   (Private keys never leave your machine and are never committed.)

### AI / automation access

The agent gets its **own** age key, supplied via an environment secret
(`SOPS_AGE_KEY`), listed as a separate recipient in `.sops.yaml`. To grant or
revoke AI access, add/remove that one line and run `sops updatekeys` — no other
key is affected. This is the "separate password for the AI" from the brief.

## Everyday use

Read a credential:
```bash
sops -d test-data/test-users.enc.json | jq '.users[] | select(.role=="consultant")'
```

Hand-edit the store (opens decrypted in $EDITOR, re-encrypts on save):
```bash
sops test-data/test-users.enc.json
```

Create the encrypted store the first time from the example:
```bash
jq '{users: []}' <<<'{}' > /tmp/seed.json
sops -e /tmp/seed.json > test-data/test-users.enc.json && rm /tmp/seed.json
```

## Bulk-create test users (and auto-save them)

```bash
export KEYCLOAK_URL=https://<host>/auth
export KEYCLOAK_REALM=online-beratung
export KEYCLOAK_ADMIN_USER=admin
export KEYCLOAK_ADMIN_PASSWORD=...          # never on the command line

# create 10 consultants, writing each generated credential into the store
scripts/seed-keycloak-users.sh \
  --count 10 --role consultant --prefix test-consultant \
  --write-back --store test-data/test-users.enc.json --env predev --tenant t1
```

Re-drive an existing set (idempotent — existing usernames are skipped):
```bash
scripts/seed-keycloak-users.sh \
  --users-file <(sops -d test-data/test-users.enc.json)
```

Preview without touching Keycloak: add `--dry-run`. Full options: `--help`.

## Follow-up

Long-term this store can move into a dedicated private `ORISO-TestData` repo
(keeping the same `.sops.yaml` + schema) so test data has its own access
control separate from the deployment chart. Tracked in #23.
