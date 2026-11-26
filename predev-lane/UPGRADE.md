# Upgrading Pre-Dev to the pre-dev chart (Oct 2026 → now) and installing wasm-artcraft

For an operator or agent session **with SSH to the Pre-Dev server** (predev.oriso.org).
Goal: Pre-Dev runs the current `pre-dev` chart (= `dev` of 2026-11-26) with the AddonService
on, and the wasm-artcraft add-on works for one test Träger (OpenResilienceInitiative/ORISO-Frontend#2579).

State on 2026-11-26 (read-only checks from CI):
- Helm release `caritas` in namespace `caritas`, revision 5 from 2026-10-16, chart 2.0.1. Since
  then services were rolled out image by image by `/root/predev-deploy-script/*.sh`.
- Pre-Dev's own values and secrets: `/root/ORISO-Helm/values.yaml` and `/root/ORISO-Helm/secrets.yaml`
  (checkout on `release/oriso-2.0.5`; only read them, edit copies).
- First blockers of a dry run: `global.emailLegalOrganisationName` is missing, then
  "Backend service client ids must be distinct …" (Keycloak service clients not migrated).

Rules for the whole run:
- Never print or paste values, secrets, tokens or rendered Secrets (logs, chat, PRs).
- Pre-Dev may lose data; a fresh Pre-Dev database is acceptable (product owner, 2026-11-26).
  Still take the backup in step 1, it costs little.
- Stop and report if a step fails in a way this file does not cover.

## 0. Prepare a working directory

```sh
ssh root@predev.oriso.org
W=/root/predev-upgrade-$(date +%Y%m%d); mkdir -p $W && chmod 700 $W && cd $W
git clone -q --branch pre-dev https://github.com/OpenResilienceInitiative/ORISO-Helm chart
cp /root/ORISO-Helm/values.yaml  $W/values.yaml      # Pre-Dev values (edit this copy)
cp /root/ORISO-Helm/secrets.yaml $W/secrets.yaml     # Pre-Dev secrets (edit this copy)
chmod 600 $W/*.yaml
VALUES="-f $W/values.yaml -f $W/chart/values-dev.yaml -f $W/chart/values-pre-dev.yaml -f $W/secrets.yaml"
```

## 1. Backup

```sh
helm get values caritas -n caritas -o yaml > $W/helm-values-rev5.yaml   # private, mode 600
kubectl -n caritas exec deploy/mariadb -- sh -c 'mariadb-dump -uroot -p"$MARIADB_ROOT_PASSWORD" --all-databases' > $W/mariadb.sql  # adapt if the pod/env differs
kubectl -n caritas exec deploy/mongodb -- mongodump --archive > $W/mongo.archive                                                   # adapt likewise
chmod 600 $W/*
```

## 2. Find every missing value

Compare the keys (not values) the chart now expects with what Pre-Dev has:

```sh
cd $W/chart
grep -E '^\s+[A-Za-z]' secrets.yaml.default | sed -E 's/:.*//' | sort -u > $W/keys-expected.txt
grep -E '^\s+[A-Za-z]' $W/secrets.yaml      | sed -E 's/:.*//' | sort -u > $W/keys-predev.txt
comm -23 $W/keys-expected.txt $W/keys-predev.txt      # secrets Pre-Dev lacks (names only)
```

Then loop the dry run until it passes (it names one missing or invalid setting at a time):

```sh
helm upgrade caritas . -n caritas $VALUES --dry-run=server > /dev/null
```

Expected additions, each **generated freshly for Pre-Dev** (`openssl rand -hex 32`) unless noted:

| Setting | Where | Note |
|---|---|---|
| `global.emailLegalOrganisationName` | values.yaml | **Legal operator name for mail footers. Ask the product owner (Frank); do not guess.** Must match the platform tenant's (ID 0) legal identity. |
| `global.emailBrandingName` | — | `ORISO`, already set by `values-dev.yaml` |
| `global.secrets.keycloakAdminUsername` | secrets.yaml | Existing master admin name on upgrades (`runbooks/keycloak-break-glass.md`) |
| `global.keycloak.serviceTechUserId` | values.yaml | Actual UUID of realm user `technical` (Keycloak admin console › Users › technical › ID). Not the fresh-export ID. |
| `global.secrets.keycloakBackendTechnicalClientSecret`, `keycloakBackendAdminClientSecret`, `keycloakSmtpSyncClientSecret` | secrets.yaml | Three different secrets |
| `global.keycloak.serviceAdminSubject` | values.yaml | UUID printed by the preparation Job in step 3 |
| `userService.statisticsMessageCountHmacSecret`, `userService.matrixRtcCallPolicyHmacSecret` | secrets.yaml | |
| `matrix.groupPolicy.*`, `matrixrtcLifecycle.*` existing secrets | secrets/values | Follow `runbooks/matrixrtc-cutover-runbook.md` / `docs/avv-native-group-policy.md` if the dry run asks for them |
| `global.secrets.addonServiceDbPassword`, `addonService.tokenPrivateKeyPem` | secrets.yaml | Step 5 |

Keep `values-pre-dev.yaml`'s own notes in mind: `userService.supportAccess.enabled` (Dev pins
`false`), Spring profile `prod` on Pre-Dev.

## 3. Keycloak service clients (before the upgrade)

Follow `runbooks/backend-service-clients.md`, section *bounded preparation*. Summary:

```sh
cd $W/chart; NAMESPACE=caritas; PRIVATE_REVIEW_DIR=$W
helm template service-clients . --namespace caritas $VALUES \
  --set global.keycloak.backendServiceClients.prepareOnly=true \
  --set global.keycloak.backendServiceClients.retireLegacyUsers=false \
  --set-string global.keycloak.serviceTechUserId=00000000-0000-4000-8000-000000000000 \
  --set-string global.keycloak.serviceAdminSubject=00000000-0000-4000-8000-000000000001 \
  --show-only templates/userservice/backend-service-clients-secret.yaml \
  --show-only templates/keycloak-reconcile-service-identities-job.yaml \
  > $W/backend-clients-prepare.yaml
# review the three resources, then:
kubectl -n caritas delete job keycloak-prepare-backend-clients --ignore-not-found --wait=true
kubectl -n caritas apply -f $W/backend-clients-prepare.yaml
kubectl -n caritas wait --for=condition=complete job/keycloak-prepare-backend-clients --timeout=600s
kubectl -n caritas logs job/keycloak-prepare-backend-clients   # prints the two UUIDs
```

Put both UUIDs into `$W/values.yaml` (`global.keycloak.serviceTechUserId`,
`global.keycloak.serviceAdminSubject`). **Never run the full upgrade with `prepareOnly=true`.**
Keep `retireLegacyUsers=false`.

## 4. Upgrade

```sh
cd $W/chart
helm upgrade caritas . -n caritas $VALUES --dry-run=server > /dev/null && echo DRY-RUN-OK
helm upgrade caritas . -n caritas $VALUES --wait-for-jobs --timeout 20m
kubectl -n caritas get pods | awk 'NR==1 || $3!="Running" && $3!="Completed" || $4>0'
```

Then run the normal reconciliation Job from the runbook (section *Normal reconciliation*), and
check: login as counsellor and as admin, a chat with an attachment, an invitation mail.
If Liquibase/Mongo migrations fail on old Pre-Dev data, a fresh database is acceptable; reset
it rather than patching data by hand, and say so in the report.

Afterwards the per-service scripts in `/root/predev-deploy-script` still work for image-only
rollouts; Helm values now match the chart again.

## 5. AddonService on

Add to `$W/secrets.yaml` (mode 600):

```yaml
global:
  secrets:
    addonServiceDbPassword: "<openssl rand -hex 24>"
addonService:
  tokenPrivateKeyPem: |
    <openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072>
```

and to `$W/values.yaml`:

```yaml
addonService:
  enabled: true
  image: { repository: ghcr.io/openresilienceinitiative/oriso-addonservice, tag: latest }
  discovery: { createNamespace: true }
```

`latest` is ORISO-AddonService `main`; it contains the frame fix (ORISO-AddonService#3). Upgrade
again as in step 4, then:

```sh
kubectl -n caritas rollout status deploy/addonservice
curl -sI https://predev.oriso.org/service/addons/.well-known/jwks.json | head -1   # HTTP 200
```

## 6. The wasm-artcraft add-on

```sh
git clone -q https://github.com/Storypapst/oriso-wasm-artcraft $W/addon
helm upgrade --install wasm-artcraft $W/addon/chart -n addons
kubectl -n addons rollout restart deploy/wasm-artcraft   # picks up a rebuilt image with the same tag
kubectl -n addons rollout status deploy/wasm-artcraft
B=https://predev.oriso.org/service/addons/wasm-artcraft/ui
curl -sI $B/craft/wordcraft/index.html | grep -iE 'x-frame|frame-ancestors'    # SAMEORIGIN / 'self'
```

The image `ghcr.io/storypapst/oriso-wasm-artcraft:wasm-artcraft-v1` is public. More checks and
rollback: `docs/DEPLOY-PREDEV.md` in that repo.

## 7. Frontend

The Frontend image `:pre-dev` already contains the add-on host. If the upgrade did not roll it
out, run the existing workflow: ORISO-Frontend › Actions › *ORISO Frontend Deploy* ›
ref `pre-dev`, environment `pre-dev`, component `frontend`.

## 8. Switch on and accept

ORISO-Admin on Pre-Dev as SuperAdmin › Add-ons › **wasm-artcraft**: on globally, `ON` for one
test Träger, `OFF` for all others. Then the acceptance list in the add-on repo's
`docs/DEPLOY-PREDEV.md`, step 6.

## 9. Report

Comment on OpenResilienceInitiative/ORISO-Frontend#2579 (English): what was upgraded, which
values were added (names only), test results, anything that failed. Keep `$W` on the server
(private); it holds the backup and the values that now describe Pre-Dev. Copy the final
`values.yaml`/`secrets.yaml` back to `/root/ORISO-Helm/` only after everything works.
