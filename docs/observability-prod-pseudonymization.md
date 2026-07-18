# OBS-P6 — KDG-safe prod telemetry pseudonymization

**Status: BUILT, OFF everywhere. Pending human/legal (DPO) sign-off before
being turned on for production.** Nothing in this document is live. Turning
this on is a deliberate future decision, not an automatic consequence of
merging this work.

Tracking: ORISO-Helm#62 (epic). Plan: `PLAN-observability-two-track-2026-07-11.md`
(local doc, `~/ORISO/0 - Docs/`).

## Why this exists

Today, prod telemetry to SigNoz is gated by one blunt switch:
`global.observability.otlpEnabled`, hard-set `false` in `values-prod.yaml`.
That means SigNoz gets *no* data at all from prod — safe, but not a real
answer, since the whole point of Track D (developer observability) is to
eventually see prod errors too. This work builds the pipeline that would let
`otlpEnabled` be turned on for prod *safely*, under German church
data-protection law (KDG/DSG-EKD) — but it does not turn it on. That remains
a second, separate decision after this pipeline has been reviewed.

## The two flags

| Flag | Default | What it does |
|---|---|---|
| `global.observability.otlpEnabled` | `true` (dev/pre-dev), `false` (prod) | Master kill switch: do the 4 backend services export OTLP traces/metrics at all. |
| `global.observability.telemetryPseudonymizationEnabled` | `false` everywhere | Adds the pseudonymization/allow-list processors to the gateway collector (`templates/signoz/templates/otel-collector-configmap.yaml`). |

They are independent on purpose. Pseudonymization can be reviewed and merged
long before anyone flips the prod kill switch, and the kill switch must never
be turned on for prod without pseudonymization also being on — turning on
`otlpEnabled` alone for prod would export raw, unscrubbed identifiers.

## What gets pseudonymized / dropped, per signal

All three transform processors below run in the **gateway** otel-collector
(the single collector instance the release deploys for whichever environment
the values overlay targets), immediately after `memory_limiter` and before
any other processor — so nothing downstream (`k8sattributes`, the
span-metrics connector, the exporters) ever sees a raw identifier.

### Traces (`transform/pseudonymize_traces`)

Verified against this codebase (grepped all 4 backend services' OBS-P2
telemetry code, 2026-07-13): **none of the 4 backends currently sets a
custom user/agency/tenant/session span attribute.** The only correlation
surface that exists today is `CorrelationIdFilter` (header
`X-Correlation-ID`, MDC key `CID`), and that currently only reaches **logs**,
not span attributes.

- **Hashed, then deleted** (defense in depth: belt-and-suspenders with the
  allow-list below): `user.id`, `enduser.id`, `agency.id`, `tenant.id`,
  `session.id`, `cid` — via a keyed hash, see below. Mostly pre-emptive
  (guards a future custom attribute), since none of these exist in span
  attributes today.
- **Deleted outright, no hash** (not identifiers to correlate on, just risk):
  - IP addresses (`net.peer.ip`, `net.sock.peer.addr`, `client.address`,
    `http.client_ip`) — the plan doc's "no IP storage" constraint, applied
    regardless of which semantic-convention name is in use.
  - Raw URL/target (`http.url`, `http.target`, `url.full`, `url.path`,
    `url.query`) — these commonly embed a real ID in a path segment (e.g.
    `/api/v1/users/12345`). The **templated** `http.route`
    (`/api/v1/users/{id}`) carries no literal identifier and stays.
  - `db.statement` (can carry literal row data).
  - `exception.message`, `exception.stacktrace` (free text, can carry names
    or message content). `exception.type` (just the Java class name) is
    kept — same treatment applied to span **events**, where exceptions are
    actually recorded.
- **Fail-closed allow-list** (`keep_keys`): after the above, every span
  attribute not on a short, explicit allow-list is dropped — HTTP
  method/status/route, RPC/DB/messaging system+operation names, OTel status,
  and the `*.hash` fields. **Any attribute a future code change adds that
  isn't on this list — a new correlation field, a debug dump, anything — is
  dropped by construction**, not "not yet handled." This is the same
  philosophy as the app-layer statistics module's small-cell-suppression
  guard (`UserService AdminDashboardStatisticsService`,
  `SMALL_CELL_MINIMUM_COUNSELORS`, which forces suppression back on in the
  `prod` Spring profile no matter what config says): the enforcement point is
  an explicit allow-list, not an error path.

### Metrics (`transform/pseudonymize_metrics`)

Requirement: prod metrics must be counter/aggregate-only, no per-user or
per-session label. Verified: **no custom high-cardinality metric exists in
any of the 4 backends today** (no `Observation`/Brave tag carries an
identifier). So this processor is primarily the fail-closed guard against
one being added later without anyone updating the allow-list: any
identifier-shaped datapoint attribute (`user.id`, `agency.id`, `tenant.id`,
`session.id`, `cid`, IP) is deleted, and every datapoint attribute not on the
allow-list (HTTP method/status/route, JVM/K8s dimensions, service name/
namespace, deployment environment) is dropped via `keep_keys`.

If a future need arises for a genuinely per-tenant/per-agency metric
breakdown, that must go through the app-layer statistics module (which
already has the ≥2-counsellor small-cell suppression guard), never raw
SigNoz metrics — this mirrors the existing plan-doc constraint "SigNoz ≠
statistics backend."

### Logs (`transform/pseudonymize_logs`) — the hard case, read this carefully

The filelog receiver's `container` operator (`otel-agent-configmap.yaml`)
does **not** parse the application's own JSON log line — the entire
logstash-encoder JSON string (including the free-text `log.message` field)
arrives as one opaque string in the LogRecord `body`. There were two options:

- **(a)** restrict export to a strict allow-list of known-safe structured
  fields, dropping the raw message body — keep only level/logger/timestamp/
  pseudonymized correlation id, or
- **(b)** if full-body pseudonymization can't be proven reliable via OTTL,
  keep prod log export off entirely and say so plainly.

**We implemented (a)**, but built so it degrades to (b)'s safety guarantee
even when the JSON-shape assumption is wrong: the processor tries to
`ParseJSON` the body and promote `request.correlationId` (hashed),
`trace.traceId`, `trace.spanId`, `log.level`, `log.logger`, `serviceName` —
but the statement that **replaces the body with a fixed placeholder** and
the `keep_keys` allow-list run **unconditionally**, independent of whether
the parse matched. Worst case (the JSON shape doesn't match what's assumed
here), you lose the correlation-id/level/logger breadcrumbs — you never get
a worse case of a raw-body leak, because the body is wiped regardless.

**Open question needing a second pair of eyes before this is ever turned
on**: the exact JSON key shape assumed here (a flat key literally named
`"request.correlationId"`, per the TenantService OBS-P2 logback config) has
**not been confirmed against a live prod log sample**. If a service's actual
JSON nests it differently (e.g. `{"request": {"correlationId": "..."}}`),
the field-promotion silently does nothing (safe, but you get fewer
breadcrumbs than intended) — this should be verified with a real log line
from each of the 4 services before the flag is ever flipped, and the OTTL
`cache["..."]` paths adjusted if needed.

## Keyed hash implementation note (why it says "HMAC-style", not "HMAC")

The hashing is: `SHA256(id || ":" || secret_salt)`, where `secret_salt` comes
from `global.secrets.telemetryPseudonymizationHmacKey` via a K8s Secret
(`<release>-otel-collector-pseudonymization`), injected as the
`PSEUDONYM_HMAC_KEY` container env var and referenced from OTTL via
`${env:PSEUDONYM_HMAC_KEY}` (the collector's own env-var expansion, the same
mechanism already used for the ClickHouse credentials in this same
ConfigMap). This is a **keyed/salted hash** — an attacker without the salt
cannot dictionary/rainbow-table their way back to the real ID — but it is
not textbook HMAC-SHA256 (which requires two padded hash passes over
inner/outer keys). The OTTL function library shipped with
`signoz-otel-collector` v0.144.6 (pinned in `values.yaml.default`) has no
literal HMAC construction, only `SHA256`/`Concat`. **If pkg/ottl ever adds a
real HMAC function, switch to it** rather than the current
salt-then-hash construction.

Rotating `telemetryPseudonymizationHmacKey` changes every pseudonymized ID
going forward; it does not retroactively re-hash already-exported data.

## Fail-closed enforcement

Every `transform/pseudonymize_*` processor sets `error_mode: propagate`: if
any OTTL statement errors while processing a record, that error propagates
and the record is dropped rather than passed through — the alternative,
`error_mode: ignore`, would keep the **original, unpseudonymized** record on
error, which is exactly the failure mode this must prevent. The primary
enforcement mechanism, though, is not the error path at all — it's the
`keep_keys` allow-list at the end of each pipeline, which unconditionally
drops any attribute not explicitly named, regardless of whether anything
"errored." A future attribute nobody has thought to add to the allow-list
yet is dropped by default, not exported by default.

## What is verified by test

`tests/render_obs_p6_test.py` (run in CI via `.github/workflows/chart-validate.yml`):

- the pipeline is fully absent (no processor, no Secret, no env var) on dev,
  pre-dev, and the prod overlay as committed today — confirming "off by
  default" actually holds,
- when explicitly turned on, all three transform processors exist, each with
  `error_mode: propagate`, wired immediately after `memory_limiter`,
- no raw identifier field name (`user.id`, `enduser.id`, `agency.id`,
  `tenant.id`, `session.id`, `cid`) appears verbatim in any `keep_keys`
  allow-list — only its `.hash` counterpart may,
- the prod metrics allow-list carries no high-cardinality/identifying label,
- the HMAC salt reaches the collector via `secretKeyRef`, never a literal.

A Helm render only produces the collector's *processing rules*, never live
telemetry data, so "a sample raw identifier value doesn't appear verbatim"
is checked structurally (the rule set can never pass one through) rather
than by inspecting exported data — there is no live data to inspect at
render time.

## Before this can ever be turned on in prod

1. **Legal/DPO sign-off** that the keyed-hash (not textbook HMAC)
   construction and the specific attribute allow-lists above satisfy KDG
   §29 Abs. 11 and the Caritas/Diakonie AVV/TOM requirements for this
   processing.
2. **Verify the log JSON shape** assumption above against a real log line
   from each of the 4 backend services (see "Open question" under Logs).
3. **Retention**: this doc does not set retention — the plan doc calls for
   short retention on pseudonymized prod signals (deletion-evidence traces
   ≥90d for Art. 5(2) accountability, everything else short); that's a
   ClickHouse TTL / SigNoz retention-settings decision, not yet made here.
4. **Observability access = order processing**: document ops access to this
   data in the AVV/TOMs before enabling (plan doc constraint #6) — not done
   here, this is Helm/collector config only.
5. Only then: set `global.observability.telemetryPseudonymizationEnabled:
   true` **and** `global.observability.otlpEnabled: true` together in a
   prod values overlay, in a reviewed PR, not as a default.
