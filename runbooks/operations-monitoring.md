# Operations monitoring concept

What to monitor once ORISO runs in production and we are responsible for
operation and deployment. It covers the signals, where each one comes from, the
thresholds that trigger an alert and the first response.

The goal is not "the pods are running". The goal is: **people can reach
counselling, safely and anonymously, and their data is not lost.** Every alert
below exists because one of those three would otherwise fail unnoticed.

All thresholds are **starting values**. Tune them after two to four weeks of real
traffic and write the tuned values back into this document.

Related: [signoz-runtime-acceptance.md](signoz-runtime-acceptance.md) (does
telemetry reach SigNoz), [url-change.md](url-change.md).

---

## 0. Take stock of your environment

Run these once per environment before setting up alerts, and again after every
release. They show what actually runs, which differs from what the values files
say more often than expected. `NS` is the release namespace.

**Cluster and capacity**

```bash
kubectl get nodes -o wide                           # how many nodes, versions
kubectl describe nodes | grep -A8 "Allocated resources"   # requested vs. allocatable
kubectl get storageclass                            # check allowVolumeExpansion
```

Note the number of nodes and their size. With few nodes and single replicas
(the chart default), losing one node takes a large part of the platform down.

**Workloads, replicas and images**

```bash
kubectl get deploy,statefulset,daemonset -n "$NS" \
  -o custom-columns=KIND:.kind,NAME:.metadata.name,READY:.status.readyReplicas,WANT:.spec.replicas,IMAGE:.spec.template.spec.containers[0].image
helm history <release> -n "$NS" --max 3             # deployed chart and revision
```

Compare the images with the release's pins. An environment that keeps its own
`values.yaml` does not pick up `values.yaml.default` changes.

**Persistent volumes and what they hold**

```bash
kubectl get pvc -n "$NS" -o custom-columns=NAME:.metadata.name,SIZE:.status.capacity.storage,CLASS:.spec.storageClassName
```

The ones that matter: `mariadb-data-*` (all service databases and Keycloak),
`mongodb-data-*` (consulting type and Admin settings, including platform SMTP),
`matrix-synapse-data` (**the Synapse database itself**, `homeserver.db`, plus media
and the signing key), and the ClickHouse volume (SigNoz telemetry).

`matrix-postgres-pvc` and `matrix-postgres-backup-pvc` belong to a Postgres that
Synapse does **not** use (gap 2). Check which database Synapse really uses:

```bash
kubectl exec -n "$NS" deploy/matrix-synapse -c synapse -- grep -A3 "^database:" /data/homeserver.yaml
```

**Backups and scheduled jobs**

```bash
kubectl get cronjob -A
kubectl api-resources | grep -i volumesnapshot      # empty = no snapshot API
```

**Telemetry**

```bash
kubectl get deploy,daemonset -n "$NS" | grep -i -E "otel|signoz|k8s-infra"
```

The four Spring services export traces and metrics when `signoz.enabled=true`.
Kubernetes, node and volume metrics and container logs need
`signozCollector.enabled=true`. nginx, Keycloak and Synapse metrics need
`global.observability.appMetrics=true`; MariaDB, MongoDB, RabbitMQ and Redis
metrics need `global.observability.dbMetrics=true` (section 3.1).

**Certificates**

```bash
kubectl get certificate -A -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,READY:.status.conditions[0].status,EXPIRES:.status.notAfter
```

### Gaps in the chart (as of 2.0.11)

These apply to every environment installed from the chart and are more urgent
than any dashboard:

1. **No backup of MariaDB or MongoDB.** The chart ships no CronJob and no volume
   snapshots. MariaDB holds all user, agency and tenant data plus Keycloak; MongoDB
   holds Admin Settings, including the encrypted platform SMTP password.
2. **Synapse runs on SQLite, and the Matrix backup machinery targets an unused
   Postgres.** `homeserver.yaml` sets `database: name: sqlite3`
   (`/data/homeserver.db` on `matrix-synapse-data`, in WAL mode). The chart still
   deploys `matrix-postgres` with WAL archiving into `matrix-postgres-backup-pvc`
   and ships `backup.sh` / `create-base-backup.sh` for it, but Synapse never
   connects to it, and no CronJob runs the scripts anyway. So the chat database
   has **no backup at all**, while an empty database is archived. SQLite is also
   not supported by Synapse for production use (single writer, no concurrent
   access, slower with growing rooms). Either migrate Synapse to Postgres (then the
   existing scripts become useful) or remove `matrix-postgres` and back up the
   SQLite file (section 4.1).
3. **The GitHub backup-sync script must not be used in production.** The same
   ConfigMap contains a script that pushes Matrix database dumps to a GitHub
   repository. Even with end-to-end encrypted messages, the dump contains who
   talks to whom and when, and room metadata. That breaks the anonymity promise.
4. **Every workload defaults to a single replica.** A node failure or a stuck
   rollout of `ingress-nginx-controller`, `ip-anonymizer` or `keycloak` takes the
   whole platform down.
5. **SigNoz runs inside the cluster it monitors.** If the cluster is down, so is
   the alerting. See the external checks and the heartbeat in section 1.
6. **The central SigNoz collector has no privacy processor and no
   `memory_limiter`.** The node agent suppresses log bodies, but traces from the
   Spring services reach ClickHouse unfiltered (section 6.2).

---

## 1. Severity levels and alert routing

| Severity | Meaning | Examples | Response (proposal) |
|---|---|---|---|
| **P1** | Users cannot reach counselling, or data is at risk | Site down, login broken, ip-anonymizer down, database down, backup failed twice | Acknowledge within 15 min, 24/7 |
| **P2** | Degraded or at risk soon | High latency, one feature broken (mail, video), disk > 85%, cert < 14 days | Within business hours, same day |
| **P3** | Needs attention | Disk > 75%, slow growth trends, a single failed job | Next business day |

Counselling also runs in the evenings and at weekends. **Whether P1 is covered
outside office hours is a decision for Caritas and us, and it must be made before
go-live.** If it isn't covered, document that openly.

**Channels:**
- SigNoz alert channels: e-mail plus one chat channel (Teams, Slack or a webhook).
- Do **not** rely on e-mail alone. Platform mail goes through Brevo; if Brevo is the
  problem, e-mail alerts about it will not arrive.
- Every alert names its runbook section in the description.

**Heartbeat (dead man's switch):** SigNoz can only alert while the cluster runs.
Configure one SigNoz alert that always fires (for example "count of spans > 0 in
the last 5 min") and sends to an external heartbeat monitor. If the heartbeat stops
for 10 minutes, the external monitor alerts: cluster, SigNoz or collector is down.

---

## 2. Availability from the user's point of view (synthetic checks)

These are the most important checks. They must run **outside the cluster and
outside the hosting provider** (a small VM elsewhere, or an uptime service), so that
they still work when the cluster is down.

Use a dedicated **monitoring tenant/agency** with a synthetic user. It must never
appear in statistics and must never receive real enquiries. Exclude it from
reports, or tag it clearly.

| ID | Check | Interval | Pass when | Alert |
|---|---|---|---|---|
| S1 | `GET https://<domain>/` | 1 min | HTTP 200, body contains `<title>` | 2 consecutive failures from 2 locations → **P1** |
| S2 | `GET https://<domain>/auth/realms/online-beratung/.well-known/openid-configuration` | 1 min | HTTP 200, JSON `issuer` equals `https://<domain>/auth/realms/online-beratung` | 2 consecutive failures → **P1** |
| S3 | Login: `POST /auth/realms/online-beratung/protocol/openid-connect/token` (client `app`, synthetic user) | 5 min | HTTP 200 with `access_token` | 2 consecutive failures → **P1** |
| S4 | Authenticated API call with the token from S3 (e.g. the user's own data under `/service/users/...`) | 5 min | HTTP 200 | 2 consecutive failures → **P1** |
| S5 | `GET https://<domain>/admin/` | 5 min | HTTP 200 | 3 consecutive failures → **P2** |
| S6 | Matrix: `GET /_matrix/client/versions` on the public Matrix host | 5 min | HTTP 200, JSON contains `versions` | 3 consecutive failures → **P1** (chat is the core feature) |
| S7 | TLS certificate of `<domain>` | 1 h | expires in > 21 days | < 21 days → **P2**, < 7 days → **P1** |
| S8 | Mail end to end: trigger a password reset or magic link for the synthetic user | 1 h | mail arrives in a monitored inbox within 10 min | 2 consecutive misses → **P1** |
| S9 | `GET https://<domain>/health` with Basic Auth | 5 min | HTTP 200 | 3 consecutive failures → **P3** (the dashboard itself, not the platform) |
| S10 | Video call: a real call between two test accounts through a restrictive network | weekly, manual or scripted | audio and video connect within 30 s | failure → **P2** |

Notes:
- **S3 and 2FA:** the synthetic user needs 2FA handling. Either give the monitor the
  TOTP secret and let it compute the code, or exempt this single account from OTP.
  Document which one was chosen; an exempted account is a weak spot, so give it the
  fewest roles possible.
- **S3 and rate limiting:** the public entry points are rate limited per client IP.
  Keep the login check at 5 min so the monitor does not throttle itself.
- **S8 and Brevo:** hourly mails count against the Brevo quota and sender
  reputation. Hourly is enough; do not go below.

**SLO (proposal):** 99.5% of S1 + S3 + S6 checks pass per calendar month, which is
about 3.6 h of allowed downtime per month. Report it monthly to Caritas. Planned
maintenance windows are announced in advance and excluded.

---

## 3. Platform health (SigNoz alerts)

### 3.1 Telemetry that has to be added first

Several signals below do not exist until these sources are switched on (section 0). Before go-live:

| Source | How | Gives |
|---|---|---|
| Kubernetes cluster state | `signozCollector.enabled=true` (cluster collector: `k8s_cluster`, `k8s_events`) | pod phase, restarts, deployment availability, `Pending` pods, events |
| Nodes and volumes | same, node agent (`kubeletstats`, `hostmetrics`) | CPU, memory, disk per node, PVC usage. PVC metrics need the agent to read `persistentvolumes`; without it they are silently dropped (ORISO-Helm#412) |
| Container logs | same, node agent (`filelog`, release namespace only) | level, logger, service and trace ids per log line; the body is suppressed by the privacy policy |
| ingress-nginx | `global.observability.appMetrics=true`: `--enable-metrics`, port `10254`, scraped by the cluster collector (`service.name=ingress-nginx`) | request rate, status codes, latency per ingress (labels carry the ingress path pattern, never client IPs) |
| Keycloak | same switch: `KC_METRICS_ENABLED` + `KC_EVENT_METRICS_USER_ENABLED`, management port `9000`, path `/auth/metrics` (`service.name=keycloak`) | logins and errors per realm/client (never per user), HTTP, JVM, DB pool |
| Synapse | same switch: `enable_metrics: true` plus a `metrics` listener on `9000`, path `/_synapse/metrics` (`service.name=synapse`) | request latency, event persistence, caches |
| MariaDB, MongoDB, RabbitMQ | `global.observability.dbMetrics=true`: collector `mysql`, `mongodb`, `rabbitmq` receivers with read-only monitoring users (`oriso_monitor` / `oriso-monitor`), passwords in the Secret `oriso-db-monitoring`, users maintained by the `db-monitoring-reconcile` hook | connections, threads, buffer pool, operations, memory, queue lengths |
| Redis | same switch: the existing `redis-exporter` sidecar on `9121`, scraped (`service.name=redis`) | memory, clients, keys, evictions |

The metrics ports are cluster-internal; no ingress routes them. After enabling,
check every source once in ClickHouse or the SigNoz metrics explorer: the
`up` metric per `service.name` must be `1`.

Metric names below are the usual OpenTelemetry and Prometheus names. **Check the
exact names in SigNoz after instrumenting**, and adjust the alert queries.

### 3.2 Entry: ingress, anonymizer, certificates

| Signal | Source / metric | Threshold | Severity | First response |
|---|---|---|---|---|
| 5xx rate at the ingress | `nginx_ingress_controller_requests{status=~"5.."}` / all requests | > 2% over 5 min | **P1** | Check ip-anonymizer first (see below), then which ingress (`ingress` label) returns the errors, then that service's logs |
| p95 latency at the ingress | `nginx_ingress_controller_request_duration_seconds` | > 2 s over 10 min | P2 | Find the slow ingress; check that backend's latency and the database |
| ip-anonymizer not available | deployment available replicas | < 1 for 1 min | **P1** | Every request depends on it (fail-closed on purpose). Restart it; **never** remove the auth-url from nginx to "get it working", that switches anonymization off |
| ip-anonymizer auth errors | nginx error log `auth request unexpected status` or `could not be resolved` | > 0 over 5 min | **P1** | This once caused a full outage (DNS name without `.svc.cluster.local`): nginx answered every request with 500. Check the `global-auth-url` in the `ingress-nginx-controller` ConfigMap |
| Certificate not ready | cert-manager `Certificate` condition `Ready` | `False` for 30 min | P2 | `kubectl describe certificate`, check the ACME challenge; DNS for the ACME host must point to the cluster |
| Certificate expiry | cert-manager metric or S7 | < 21 days | P2 | cert-manager renews 30 days before expiry. If it hasn't, renewal is failing |

### 3.3 Workloads (all deployments and StatefulSets)

| Signal | Source / metric | Threshold | Severity | First response |
|---|---|---|---|---|
| Deployment not fully available | `k8s.deployment.available` < `k8s.deployment.desired` | for 5 min | **P1** for ingress, ip-anonymizer, keycloak, userservice, matrix-synapse, frontend; P2 for the rest | `kubectl describe pod`, logs of the failing container |
| Crash loop | `k8s.container.restarts` increase | > 3 in 15 min | P1 | Since 2.0.9 the services **refuse to start** when a URL variable is missing; the log names the variable |
| OOM kill | container termination reason `OOMKilled` | any | P2 | Raise the memory limit or look for a leak (JVM heap) |
| Pod stuck in Pending | `k8s.pod.phase = Pending` | > 10 min | P2 | Usually no room left on the nodes; see node resources |
| Image pull failure | pod event `ErrImagePull` / `ImagePullBackOff` | any | P1 during a deploy | Tag missing on GHCR, or the package is private (happened with ip-anonymizer) |
| Helm hook job failed | `Job` with `status.failed > 0` in the release namespace | any | P2 | A failed post-upgrade hook marks the Helm release `failed` while the pods keep running the new version. Read the job log; successful hooks delete themselves, so follow them during the upgrade |

### 3.4 Backend services (userservice, tenantservice, agencyservice, consultingtypeservice)

| Signal | Source / metric | Threshold | Severity | First response |
|---|---|---|---|---|
| 5xx rate per service | `http.server.requests` with `status` 5xx | > 2% over 5 min | P1 (userservice), P2 (others) | Traces in SigNoz filtered by `status_code = ERROR` |
| p95 latency per service | `http.server.requests` | > 1.5 s over 10 min | P2 | Traces: database call or downstream service? |
| JVM heap | `jvm.memory.used` / `jvm.memory.max` (area heap) | > 90% for 10 min | P2 | Likely heading for an OOM kill |
| GC pauses | `jvm.gc.pause` | > 1 s total per minute | P3 | |
| DB connection pool exhausted | `hikaricp.connections.pending` | > 0 for 5 min | P2 | Slow queries in MariaDB, or too many connections (`maxconnections: 1000`) |
| Mail sending errors | userservice logs: SMTP exceptions | > 0 over 15 min | **P1** | No mail means no magic link, no invite, no reset. Check Brevo status and SMTP credentials |

### 3.5 Keycloak

| Signal | Source / metric | Threshold | Severity | First response |
|---|---|---|---|---|
| Token endpoint errors | Keycloak metrics or ingress 5xx on `/auth/realms/.../token` | > 2% over 5 min | **P1** | Keycloak logs; database connection to MariaDB |
| Failed logins | `keycloak_user_events_total` with `event="login"` and an `error` label | > 5× the normal rate over 10 min | P2 | Possible credential stuffing. Check the brute-force protection and which accounts are targeted |
| 2FA flow drift | `oriso-verify-2fa-flow.sh` in the Keycloak pod, run daily | any drift except the known browser e-mail OTP gap | P2 | See PR #374, "2FA realm flows". Do not run the apply script blindly: it briefly rebinds the login flow to one without 2FA |
| Keycloak SMTP out of sync | realm `smtpServer` empty or different from Admin Settings | after every SMTP change in Admin Settings | **P1** | Keycloak sends the e-mail OTP codes itself; without SMTP, e-mail 2FA users cannot log in. Check the `keycloak-reconcile-smtp` log for `SMTP_RECONCILE_REQUEST_INVALID` |
| Backend service clients | `keycloak-reconcile-service-identities` hook result; token errors for `backend-technical` / `backend-admin` | hook ends without `BACKEND_CLIENTS_RECONCILED`, or 401 from Keycloak in service logs | **P1** | Subject mismatch (`serviceTechUserId` / `serviceAdminSubject`), wrong client secret, or the hook lost the race with a restarting Keycloak (re-run the upgrade) |

### 3.6 Chat and video (Synapse, LiveKit, MatrixRTC)

| Signal | Source / metric | Threshold | Severity | First response |
|---|---|---|---|---|
| Synapse request latency | `synapse_http_server_response_time_seconds` | p95 > 3 s over 10 min | P2 | With SQLite (gap 2): file size, WAL size, disk I/O of `matrix-synapse-data` |
| Message send latency | `synapse_http_server_response_time_seconds` filtered to the room-send servlet (check the exact `servlet` label value in the metrics explorer; persistence counters only appear once events have flowed since the last restart) | p95 > 2 s over 10 min | P1 | Messages are delayed. With SQLite, one writer serialises everything; check the database file and the node's disk |
| Synapse not scraped | `up{service.name="synapse"}` | 0 for 5 min | P2 | The pod is down, or restarted and stuck (Synapse uses `Recreate` because its volume is `ReadWriteOnce`) |
| LiveKit up | deployment available | < 1 for 2 min | P2 | Uses `hostNetwork` and `Recreate`: the old pod must fully stop before the new one starts |
| MatrixRTC gateway errors | ingress 5xx on the gateway path | > 5% over 10 min | P2 | Calls fail to start |

### 3.7 Data stores

| Signal | Source / metric | Threshold | Severity | First response |
|---|---|---|---|---|
| PVC usage | `k8s.volume.available` / `k8s.volume.capacity` (type `persistentVolumeClaim`; needs the agent to read `persistentvolumes`, ORISO-Helm#412) | > 75% P3, > 85% P2, > 95% P1 | see left | Grow the PVC (online if the storage class allows expansion) or clean up. `matrix-synapse-data` holds the chat database and media |
| Data store not reachable | receiver scrape errors in the cluster collector log; no fresh `mysql.*` / `mongodb.*` / `rabbitmq.*` / `redis_up` samples | none for 5 min | P1 | `Access denied` / `401` right after an upgrade is expected for one cycle (the users are reconciled after the collector restarts); persisting means the reconcile hook failed or the store is down |
| MariaDB connections | `mysql.threads` (connected) vs. `max_connections` | > 80% → P2 | | |
| MariaDB slow queries | slow query log | trend | P3 | |
| MongoDB connections and memory | `mongodb.connection.count`, `mongodb.memory.usage` | trend; connections near the limit → P2 | | |
| Redis memory | `redis_memory_used_bytes` vs. `redis_memory_max_bytes` (exporter) | > 80% → P2 | | Consultant availability lives here (TTL 120 s) |
| RabbitMQ queue length | `rabbitmq.message.current` per queue (queue-level metrics appear only once queues exist; node-level ones like `rabbitmq.published` / `rabbitmq.consumed` always do) | a queue grows for 15 min → P2 | | A consumer is stuck; check which service consumes that queue |

### 3.8 Nodes

| Signal | Threshold | Severity | First response |
|---|---|---|---|
| Node `NotReady` | 2 min | **P1** | With few nodes and single replicas, a large part of the platform is gone |
| Node CPU | > 85% for 15 min | P2 | |
| Node memory | > 90% for 10 min | P2 | Evictions follow |
| Node disk (root/ephemeral) | > 85% | P2 | Image garbage collection, container logs |

---

## 4. Data safety

The biggest gap today (section 0). Monitoring cannot help as long as there is no
backup to monitor.

### 4.1 What must be backed up

| Data | Where | Method (proposal) | Frequency | Retention |
|---|---|---|---|---|
| MariaDB (all service databases + Keycloak) | `mariadb-0` | `mariadb-dump --single-transaction --all-databases`, compressed and encrypted | daily, plus binlog for point-in-time recovery if the RPO requires it | 30 days (align with the DPO) |
| MongoDB | `mongodb-0` | `mongodump --archive --gzip`, encrypted | daily | 30 days |
| Synapse database (SQLite) | `/data/homeserver.db` in the `matrix-synapse` pod, on `matrix-synapse-data` | SQLite **online backup API**, run inside the Synapse container (`python3` with the `sqlite3` module is in the image): `sqlite3.connect('/data/homeserver.db').backup(sqlite3.connect('/tmp/homeserver-backup.db'))`, then copy out, compress, encrypt. **Never copy the `.db` file alone**: it runs in WAL mode (`-wal`/`-shm` beside it), so a plain file copy is inconsistent. The volume is `ReadWriteOnce`, so a separate CronJob pod cannot mount it next to Synapse | daily | 30 days |
| Synapse media and signing key | `matrix-synapse-data` (`media_store/`, `*.signing.key`) | file copy from the Synapse pod, encrypted. Without the signing key the server identity is lost | daily (media), once + on rotation (key) | 30 days |
| Matrix Postgres | `matrix-postgres-0` | **only if Synapse is migrated to it** (gap 2); until then it holds no chat data | — | — |
| Kubernetes secrets and Helm values | the operator's secret store / private repo | out of band | on every change | versioned |

**All backups must leave the cluster**, encrypted, to storage at a different
provider or site. A backup on the cluster's own storage class does not survive
the loss of the cluster.

Back up `consultingTypeService.smtpPasswordEncryptionSecret` together with
MongoDB: without the exact key, the restored platform SMTP password cannot be
decrypted.

**Never** use the GitHub backup-sync script from `matrix-backup-script` for
production data (section 0, gap 3).

### 4.2 Backup monitoring

| Signal | Threshold | Severity |
|---|---|---|
| Backup job failed | any | P2; second failure in a row → **P1** |
| No successful backup | > 26 h since the last success, per database | **P1** |
| Backup size | < 50% or > 200% of the 7-day average | P2 (empty dumps and runaway growth both look like "success") |
| Offsite copy missing | > 26 h | P1 |
| Restore test | not done or failed this month | P2 |

The jobs should report their result actively (for example a heartbeat URL, or a
metric pushed to the collector). "No error seen" is not the same as "backup ran".

### 4.3 Restore tests

Monthly, and after every major version change of a database:

1. Restore the latest backup of each database into a separate namespace.
2. Start the services against it, or at least run checks: row counts of core tables,
   and a login with a test account.
3. Write down the time it took. That is the real recovery time; compare it with
   the RTO agreed with Caritas.
4. Delete the restored copy: it contains real personal data.

### 4.4 Retention and deletion jobs

Here deleting on time is part of the promise, not an optional cleanup:

| Job | Setting | Monitor |
|---|---|---|
| Inactive session deletion | `userService.sessionInactiveDeleteWorkflowEnabled` (currently `false`), cron `0 0 * * * ?`, `…CheckDays: 30` | When enabled: log line per run; alert if it hasn't run for 26 h |
| Agency delete workflow | `agencyDeleteworkflowCron: "0 20 4 * * *"` | same |
| Team discussion archive purge | `teamDiscussion.archiveRetentionDays` (0 = off until the DPO signs off) | same, once enabled |
| Telemetry retention in SigNoz | see section 6 | the ClickHouse volume must not only grow |

---

## 5. Security

| Signal | Source | Threshold | Severity | First response |
|---|---|---|---|---|
| Failed logins spike | Keycloak events | see 3.5 | P2 | |
| Admin role changes | Keycloak admin events (enable "save admin events") | any change of realm-management roles, `tenant-admin`, `technical` | P2, review daily | Was it planned? |
| Service identity activity | Keycloak events for the clients `backend-technical` and `backend-admin`, and for the legacy users `technical` / `svc-keycloak-admin` until they are retired | tokens issued to requests from outside the cluster; any password login of a legacy user after retirement | P1 | They are service identities; they should only authenticate from inside the cluster |
| Kubernetes changes | Kubernetes audit log (if the provider offers it) | `exec`, reading Secrets, RBAC changes, new ServiceAccounts | review weekly; unknown actor → P1 | Keep a list of everyone holding a kubeconfig, with its expiry (section 7) |
| Rate limit hits | ingress 429 responses on public entry paths | > 10× normal | P3 | Abuse or a misbehaving client |
| Vulnerable images | Trivy or a similar scan of the running image tags | new critical CVE with a fix available | P2 | Plan a patch release |
| Unexpected images | running images not pinned to a release tag (`:latest`, `:dev`) | any in production | P2 | Today `ip-anonymizer:latest` is the exception; give it a pinned tag and a CI build |

---

## 6. Anonymity and privacy

Caritas promises users anonymity. These checks prove continuously that we keep it.

### 6.1 No real IP addresses anywhere

- The ip-anonymizer replaces `X-Real-IP` and `X-Forwarded-For` with a session UUID
  (`X-Anon-Id`). The nginx log format deliberately drops `$remote_addr`.
- **Daily automated scan** (a CronJob or an external job) of the last 24 h of logs
  in SigNoz and of the service logs for IPv4/IPv6 addresses outside the cluster
  ranges (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `127.0.0.0/8`, the pod
  and service CIDRs). Any hit → **P1**, because it breaks the promise.
- Check the span attributes `client.address`, `http.client_ip`,
  `net.sock.peer.addr` once after instrumenting. They should only contain UUIDs or
  cluster-internal addresses.

### 6.2 No tokens or query strings in telemetry

The nginx log format drops query strings because magic-link and password-reset
tokens travel as GET parameters (KDG epic #282). **Traces can bring them back**:
OpenTelemetry and Micrometer often record the full URL (`url.full`, `http.url`,
`url.query`).

Strip them in the OTel collector before anything reaches ClickHouse:

```yaml
processors:
  transform/privacy:
    trace_statements:
      - context: span
        statements:
          - replace_pattern(attributes["url.full"], "\\?.*$", "")
          - replace_pattern(attributes["http.url"], "\\?.*$", "")
          - delete_key(attributes, "url.query")
          - delete_key(attributes, "client.address")
          - delete_key(attributes, "http.client_ip")
          - delete_key(attributes, "net.sock.peer.addr")
    log_statements:
      - context: log
        statements:
          - replace_pattern(body, "(token|code|key)=[^&\\s\"]+", "$$1=REDACTED")
```

The node agent (`signozCollector`) already suppresses every container log body
and keeps only trace/span ids, level, logger and service name. The central
collector of the SigNoz subchart (`<release>-signoz-otel-collector`), which
receives the traces of the Spring services, has no such processor.

Add `transform/privacy` to every traces and logs pipeline of the central
collector, **before** the exporters. Then include the check in
the daily scan: search for `token=` and `?` in `url.full` over the last 24 h; any
hit → **P1**.

### 6.3 The anonymizer stays on

- `ipAnonymizer.enabled` must be `true` in production values. Review it in every
  deploy PR.
- The `global-auth-url` in the `ingress-nginx-controller` ConfigMap must be present.
  A daily check that the key exists; missing → **P1**. When the anonymizer fails,
  the platform stops (fail-closed). That is intended. The dangerous "quick fix" is
  removing the key, which silently turns anonymization off.
- The ip-anonymizer keeps its sessions in memory. With more than one replica, the
  same user would get different UUIDs from different replicas. Keep it at one
  replica, or give it a shared store, before scaling it.

### 6.4 Retention and access in SigNoz

Telemetry is personal data under the KDG too.

| Data | Proposed retention | Where |
|---|---|---|
| Traces | 7 days | SigNoz settings → retention |
| Logs | 7 days | same |
| Metrics | 30 days | same |

- Agree the retention with the data protection officer and document it.
- SigNoz access: named accounts only, no shared login. Review the user list
  quarterly.
- The health dashboard at `/health` is behind Basic Auth (`health-dashboard-basic-auth`).
  Rotate the password when someone leaves.

---

## 7. Expiry calendar

Things that stop working on a date, with nobody noticing until then. Keep this list
current; set a reminder 30 days before each date.

| Item | Expires | Renewal |
|---|---|---| 
| GHCR personal access token (image pushes) | ≤ 366 days after creation (org policy) | GitHub settings |
| Brevo SMTP key | check in Brevo | Brevo account |
| LiveKit API key and secret | no expiry; rotate yearly | `secrets.yaml`, then deploy |
| Health dashboard password | no expiry; rotate on staff changes | `secrets.yaml` (`healthDashboard.ingress.htpasswd`) |

---

## 8. Deployments

Most incidents start with a change. For every production deploy:

**Before:**
- [ ] All image tags of the release exist on GHCR and are public (image pull errors
      happened with ip-anonymizer).
- [ ] Release notes read for "fail fast" or required new variables (2.0.9 made the
      services refuse to start without their URL variables).
- [ ] The environment's own `values.yaml` carries the release's image pins and new
      required keys; `values.yaml.default` is never loaded by Helm.
- [ ] Local patches re-applied if the release doesn't contain them yet, and removed
      once it does.
- [ ] Manual preflight steps from the release PR done (Keycloak identities, client
      preparation, secrets that must not rotate).
- [ ] `helm template … --dry-run=server` passes against the cluster.
- [ ] Backups of the last night succeeded (section 4.2).
- [ ] Note the current Helm revision: `helm history <release> -n "$NS"`.

**During:**
- [ ] `kubectl get jobs -n "$NS" -w` in a second window; follow each Keycloak
      hook with `kubectl logs -f job/<name>`. Successful hooks delete themselves.

**After:**
- [ ] `helm status <release> -n "$NS"` shows `deployed`, not `failed`.
- [ ] All deployments ready (section 3.3).
- [ ] Synthetic checks S1–S6 pass.
- [ ] Watch the ingress 5xx rate and latency for **30 minutes**.
- [ ] The daily privacy scan (section 6) runs once manually.

**Rollback:** `helm rollback <release> <previous-revision> -n "$NS" --wait`.
Changes the Keycloak hooks made to the realm (roles, clients, retired users) are
**not** undone by a rollback.
Database migrations (Liquibase) are **not** rolled back by this. Check the release
notes for migrations before relying on a rollback, and restore from backup if a
migration has to be undone.

---

## 9. Organisation

- **On-call:** who is reachable for P1, and when (section 1). A rota, not "whoever
  sees it".
- **A runbook per alert:** the "first response" column above is the minimum. Link
  each alert to its section.
- **Communication during outages:** one channel to Caritas and the counselling
  coordinators, a status page or at least a fixed mail distribution list, and a
  short template ("since HH:MM, feature X unavailable, next update at HH:MM").
- **Post-mortems:** for every P1, within a week, blameless. Result: cause, what
  detected it (or why nothing did), and one or two concrete improvements.
- **Monthly report to Caritas:** SLO result, incidents, backup and restore-test
  results, upcoming expiries, planned changes.
- **Capacity review quarterly:** user and enquiry numbers, database and media growth,
  node utilisation. Check that the remaining nodes could carry the load of a
  failed one.

---

## 10. Rollout order

**Before go-live (blocking):**
1. Backups for MariaDB, MongoDB, the Synapse SQLite database (online backup API),
   Synapse media and signing key, offsite and encrypted, with monitoring
   (section 4). One restore test done. Decide whether Synapse moves to Postgres
   before go-live (gap 2).
2. External synthetic checks S1–S4, S6, S7, S8 and the heartbeat (sections 1 and 2).
3. Privacy processor in the collector and the daily IP/token scan (section 6).
4. Kubernetes infrastructure telemetry (`signozCollector.enabled=true`, with the
   `persistentvolumes` permission) and the alerts in 3.2, 3.3 and 3.7 (PVC usage).
5. On-call and alert routing decided (section 1).

**First month:**
6. `global.observability.appMetrics=true` (ingress-nginx, Keycloak, Synapse);
   alerts 3.2, 3.5, 3.6.
7. `global.observability.dbMetrics=true` (MariaDB, MongoDB, RabbitMQ, Redis);
   alerts 3.7 and 3.8.
8. Security signals (section 5).
9. Second replica for `ingress-nginx-controller`, `keycloak` and `frontend`; decide
   how to handle the ip-anonymizer session store before scaling it.

**Ongoing:**
10. Tune the thresholds with real data, and update this document.
11. Monthly restore test and report.

---

## Open decisions

| Decision | Owner |
|---|---|
| P1 coverage outside office hours | Caritas + operations |
| SLO target (proposed 99.5%) and RTO/RPO for data | Caritas + operations |
| Retention of telemetry and backups | data protection officer |
| Where offsite backups are stored (provider, region, encryption key custody) | operations + DPO |
| External monitoring tool and location | operations |
| Synthetic user: TOTP secret in the monitor, or OTP exemption | operations + security |
