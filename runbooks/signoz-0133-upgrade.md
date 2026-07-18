# SigNoz v0.133.0 upgrade gate

This runbook upgrades only after a cold backup and an isolated restore check
have both passed. It applies to Pre-Dev first. Dev promotion is a separate,
human-approved maintenance window.

## Version contract

The pins match upstream Helm chart `signoz-0.133.0`:

| Component | Current target |
| --- | --- |
| SigNoz | `signoz/signoz:v0.133.0` |
| SigNoz OTel Collector and migrator | `signoz/signoz-otel-collector:v0.144.6` |
| ClickHouse | `clickhouse/clickhouse-server:25.12.5` (unchanged) |

The release contains authorization storage changes. On the current v0.132.2
Dev runtime, the Service Accounts page fails with `no active license found`
even though current upstream documentation lists service accounts for
self-hosted Community. After the upgrade, service-account access is an explicit
acceptance check; do not reuse the temporary Admin credential as a workaround.

## Preconditions

1. Merge the reviewed chart PR into `pre-dev`; do not run from an unreviewed
   local copy.
2. Announce a short Pre-Dev observability maintenance window. Application
   traffic continues, but SigNoz ingestion and queries pause during the cold
   archive.
3. Confirm at least twice the current SigNoz + ClickHouse used space is free:

   ```bash
   mkdir -p /var/backups/oriso/signoz
   df -h /var/backups/oriso/signoz
   du -sh \
     /var/lib/rancher/k3s/storage/*_caritas_signoz-data-oriso-platform-signoz-0 \
     /var/lib/rancher/k3s/storage/*_caritas_clickhouse-data-oriso-platform-clickhouse-0
   ```

4. Confirm all three workloads are healthy and record their exact images:

   ```bash
   kubectl --context default -n caritas get deployment/oriso-platform-otel-collector \
     statefulset/oriso-platform-signoz statefulset/oriso-platform-clickhouse \
     -o custom-columns=KIND:.kind,NAME:.metadata.name,READY:.status.readyReplicas,IMAGE:.spec.template.spec.containers[0].image
   ```

## Backup and isolated restore proof

Run on the K3s node that owns the local-path volumes:

```bash
sudo python3 scripts/signoz_upgrade_guard.py backup \
  --context default \
  --expected-node hassan-dev \
  --namespace caritas \
  --release oriso-platform \
  --backup-root /var/backups/oriso/signoz \
  --confirm-maintenance-window
```

The tool always restores the previous replica counts in reverse dependency
order, including when archive creation fails. It writes a mode-`0600` manifest
with image names, replica counts and archive checksums, but no secret values.

Use the directory printed by the backup command:

```bash
sudo python3 scripts/signoz_upgrade_guard.py verify \
  --context default \
  --expected-node hassan-dev \
  --namespace caritas \
  --backup-root /var/backups/oriso/signoz \
  --backup-dir /var/backups/oriso/signoz/<UTC_TIMESTAMP>
```

Verification must return:

- `sqlite_integrity: ok`
- a positive `clickhouse_table_count`

The ClickHouse check extracts into a temporary host directory and starts a
no-Service verification pod against that copy. It references the existing
ClickHouse Secret; it never reads or prints the password. The pod and extracted
copy are removed even when verification fails.

## Pre-Dev apply and acceptance

Render and review before applying through the normal Pre-Dev deployment path:

```bash
helm lint . -f values.yaml.default -f values-pre-dev.yaml -f ci/placeholder-secrets.yaml
helm template oriso-platform . --namespace caritas \
  -f values.yaml.default -f values-pre-dev.yaml \
  -f ci/placeholder-secrets.yaml >/tmp/oriso-signoz-0133.yaml
```

After deployment, prove all of the following:

1. SigNoz, collector, migrator and ClickHouse pods are healthy and use the
   target images above.
2. Logs, metrics and traces continue advancing after the deployment timestamp.
3. Existing dashboards and alert rules load.
4. `Settings > Service Accounts` loads without the license error.
5. Create `quality-hub-viewer`, assign `signoz-viewer`, generate a time-bounded
   key, store it only in Infisical, and verify a GET/query succeeds while a
   mutation is denied.
6. Run the existing daily-triage workflow and confirm its bounded read queries.

## Rollback

If migrations, ingestion, dashboards or authorization fail:

1. Read the exact pre-upgrade Helm revision from `manifest.json`; do not infer
   it from the current release state.
2. Stop the collector, SigNoz and ClickHouse in that order.
3. Preserve the failed post-upgrade PVC contents separately for diagnosis.
4. Replace both PVC directories from the verified archives while all three
   workloads remain stopped.
5. Restore the complete chart-rendered state and start the old workloads with
   `helm --kube-context default --namespace caritas rollback oriso-platform
   <manifest revision> --wait --timeout 10m`. This restores ConfigMaps,
   Services, RBAC, environment and migrator configuration in addition to image
   tags and replica counts.
6. Verify SQLite integrity, ClickHouse tables, `/api/v1/health`, ingestion and
   the daily-triage read queries again.

Do not promote to Dev until Pre-Dev upgrade and rollback evidence are attached
to ORISO-Helm issue #90 and a human reviewer approves the separate promotion.
