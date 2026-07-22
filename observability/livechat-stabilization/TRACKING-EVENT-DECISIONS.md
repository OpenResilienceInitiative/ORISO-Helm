# Tracking-event decision log

Short decisions captured during the stabilization audit. These are candidates,
not permission to emit personal data. Signal names enter the catalog only with
an owner, bounded dimensions, and a test.

## 2026-07-22 08:10 CEST — Browser exporter health

- Decision: Yes. Add aggregate exporter initialization and export outcome,
  grouped by application and deployment environment.
- Why: Missing Web Vitals must be distinguishable from a healthy but idle app;
  repeated CORS failures currently look like unrelated browser noise.
- Privacy: No page path, user agent, tenant, user, request body, or IP-derived
  attribute.
- Cardinality: Low; application, environment, and bounded outcome only.

## 2026-07-22 08:20 CEST — Runtime identity and deployment drift

- Decision: Yes. Emit immutable commit, image digest, environment, and selected
  non-secret E2EE/availability configuration as resource attributes.
- Why: PreDev and Dev were running different revisions, so performance and bug
  comparisons were otherwise misleading.
- Privacy: Deployment metadata only; no Secret value or credential.
- Cardinality: Bounded by deployed service revisions.

## 2026-07-22 08:30 CEST — Provisioning integrity

- Decision: Yes. Record create-stage outcome, authoritative readback, and
  compensation outcome for tenant, counselling-centre relation, and user
  provisioning.
- Why: A generic 500 cannot explain whether identity, relation, or rollback
  failed, which is the reported disappearing-user symptom.
- Privacy: Generated correlation id and counts only; never form values, email,
  username, invite link, or token.
- Cardinality: Bounded stages and outcomes; no raw entity identifiers.

## 2026-07-22 08:40 CEST — Replica safety

- Decision: Yes. Add local-state component count, scheduler execution owner,
  duplicate execution count, and cross-replica conformance result.
- Why: UserService is a Deployment but still has local caches, active-view and
  Matrix maps, and scheduled jobs; workload kind alone cannot prove statelessness.
- Privacy: Component names, replica id, job name, and outcome only.
- Cardinality: Bounded by services, replicas, and declared jobs.

## 2026-07-22 08:50 CEST — E2EE outcome

- Decision: Yes. Count successful room creation by encryption-enabled outcome
  and correlate aggregate decryption-failure counters.
- Why: Configuration says E2EE should be on, but only an outcome signal proves
  that newly created rooms are actually encrypted.
- Privacy: Pseudonymous room/session correlation only when needed; never event
  content, key material, sender, access token, or raw room id.
- Cardinality: Bounded outcome plus HMAC-pseudonymous correlation with expiry.

## 2026-07-22 09:00 CEST — Message-level tracking

- Decision: No generic per-message analytics event. Keep only bounded Matrix
  processing stage/outcome and side-effect counters.
- Why: Per-message events add cost and privacy risk without improving the
  diagnosis beyond stage, encryption flag, and outcome.
- Privacy: No message body, preview, event id, sender, recipient, or device
  token.
- Cardinality: Low bounded stages and outcomes; aggregate counts rather than
  one retained analytics record per message.
