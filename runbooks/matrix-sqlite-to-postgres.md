# Matrix Synapse: SQLite to PostgreSQL

Synapse's database is chosen by `matrix.database.engine`:

- `sqlite3` (default): `/data/homeserver.db` on the `matrix-synapse-data` PVC.
- `postgres`: the chart's `matrix-postgres` StatefulSet.

Changing the value moves no data. On an install that already holds Matrix
data, flipping it alone starts Synapse on an empty database. Follow this
runbook instead. A fresh install with no Matrix data may set `postgres` directly
(after step 4).

Media (`/data/media_store`) and the signing key stay on the PVC in both modes.

## 0. Before you start

- Announce a chat downtime of about 30 minutes; Synapse is stopped from step 2
  until step 6.
- If the environment is synced by ArgoCD, pause auto-sync for the release, or
  it scales Synapse back up during the port.
- Make sure the PVC has room for two extra copies of `homeserver.db`.

```sh
NS=caritas   # namespace of the release
IMAGE=$(kubectl -n "$NS" get deploy matrix-synapse -o jsonpath='{.spec.template.spec.containers[0].image}')
PGHOST=matrix-postgres-service   # matrix.postgresHost if set, else matrix.postgresServiceName
STAMP=$(date +%Y%m%d-%H%M)
```

## 1. Record the baseline

Note, for the verification in step 7: two test accounts (one counsellor, one
asker), one room with history, one image or file in it.

## 2. Stop Synapse

```sh
kubectl -n "$NS" scale deploy/matrix-synapse --replicas=0
kubectl -n "$NS" wait --for=delete pod -l app=matrix-synapse --timeout=5m
```

## 3. Back up SQLite and media

Start a helper pod with the Synapse image and the Synapse PVC. It reads the
database credentials from `matrix-postgres-secret`; nothing is printed.

```sh
kubectl -n "$NS" apply -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: matrix-port-db
spec:
  restartPolicy: Never
  securityContext: {runAsUser: 991, runAsGroup: 991}
  containers:
    - name: port
      image: ${IMAGE}
      command: ["sleep", "86400"]
      env:
        - {name: PGHOST, value: "${PGHOST}"}
        - {name: POSTGRES_USER, valueFrom: {secretKeyRef: {name: matrix-postgres-secret, key: POSTGRES_USER}}}
        - {name: POSTGRES_PASSWORD, valueFrom: {secretKeyRef: {name: matrix-postgres-secret, key: POSTGRES_PASSWORD}}}
        - {name: POSTGRES_DB, valueFrom: {secretKeyRef: {name: matrix-postgres-secret, key: POSTGRES_DB}}}
      volumeMounts: [{name: data, mountPath: /data}]
  volumes:
    - {name: data, persistentVolumeClaim: {claimName: matrix-synapse-data}}
EOF
kubectl -n "$NS" wait --for=condition=Ready pod/matrix-port-db --timeout=5m

# On-PVC copy (rollback source) and a separate port source; the original stays untouched.
kubectl -n "$NS" exec matrix-port-db -- sh -c "cp -a /data/homeserver.db /data/homeserver.db.pre-postgres-${STAMP} && cp -a /data/homeserver.db /data/port-source.db"
# Off-cluster copies.
kubectl -n "$NS" cp matrix-port-db:/data/homeserver.db "./homeserver.db.pre-postgres-${STAMP}"
kubectl -n "$NS" exec matrix-port-db -- tar -C /data -czf - media_store > "./media_store-${STAMP}.tgz"
```

## 4. Prepare PostgreSQL

Synapse refuses a database whose collation is not `C`. The target database
must also be empty.

```sh
kubectl -n "$NS" exec matrix-postgres-0 -- sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT datcollate, datctype FROM pg_database WHERE datname = current_database(); SELECT count(*) FROM pg_tables WHERE schemaname = '\''public'\'';"'
```

Expected: `C|C` and `0`. If the collation is not `C` and the table count is
`0`, recreate the empty database:

```sh
kubectl -n "$NS" exec matrix-postgres-0 -- sh -c 'psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 -c "DROP DATABASE \"$POSTGRES_DB\"" -c "CREATE DATABASE \"$POSTGRES_DB\" OWNER \"$POSTGRES_USER\" ENCODING '\''UTF8'\'' LC_COLLATE '\''C'\'' LC_CTYPE '\''C'\'' TEMPLATE template0"'
```

If the table count is not `0`, stop and find out what wrote there.

## 5. Port the data

```sh
kubectl -n "$NS" exec matrix-port-db -- sh -c 'umask 077; cat > /tmp/pg.yaml <<EOF
database:
  name: psycopg2
  args:
    user: "$POSTGRES_USER"
    password: "$POSTGRES_PASSWORD"
    database: "$POSTGRES_DB"
    host: "$PGHOST"
    port: 5432
EOF
synapse_port_db --sqlite-database /data/port-source.db --postgres-config /tmp/pg.yaml'
```

It must end without an error. It can be re-run after an interruption.

## 6. Switch Synapse to PostgreSQL

Set `matrix.database.engine: postgres` in the environment's values file (for
Dev: `values-dev.yaml`), then deploy the chart the usual way. The upgrade brings
Synapse back to `matrix.replicas`. Delete the helper pod and resume ArgoCD
auto-sync:

```sh
kubectl -n "$NS" delete pod matrix-port-db
```

## 7. Verify

```sh
kubectl -n "$NS" logs deploy/matrix-synapse | grep -m1 "Preparing database"   # expect 'psycopg2'
kubectl -n "$NS" rollout status deploy/matrix-synapse --timeout=5m
```

Then, with the accounts from step 1: both can log in, the room shows its old
history, a new message arrives on the other side, and the old image or file
opens.

## Rollback

Set `matrix.database.engine: sqlite3` again and deploy. Synapse restarts on the
untouched `/data/homeserver.db`. Anything written after step 6 exists only in
PostgreSQL and is lost, so decide on rollback within the verification window.
If `homeserver.db` itself is damaged, scale Synapse to 0 and restore it from
`/data/homeserver.db.pre-postgres-<STAMP>` (or the off-cluster copy) first.

## Rehearsal on Dev (before any other environment)

1. Run steps 0–7 on Dev and record the result in the tracking issue.
2. Roll back once (Rollback above) and check that the room history from step 1
   is still there on SQLite.
3. Go forward again: stop Synapse (step 2), start the helper pod and refresh
   `port-source.db` from `homeserver.db` (step 3), drop and recreate the
   database with the command from step 4 (it is no longer empty, by design),
   port (step 5), switch (step 6) and verify (step 7).
4. Keep the SQLite copies until the environment has run a week on PostgreSQL
   without a rollback.
