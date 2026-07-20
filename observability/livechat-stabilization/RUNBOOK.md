# Live Chat stabilization diagnostics

Use the `ORISO Live Chat Stabilization Diagnostics` dashboard with the exact
environment and immutable service version from the E2E artifact. Start with the
deployment-drift panel; do not compare Pre-Dev and Dev under a mutable tag.

`dashboard-livechat-diagnostics.json` is a versioned build specification, not a
claim of a currently imported dashboard. Build the panels in the deployed
SigNoz version and export its native JSON for review only after the named
signals arrive; SigNoz's supported self-hosted workflow is UI import/export of
dashboard JSON. `alerts.yaml` likewise defines reviewed alert intent and query
contracts; create rules through `POST /api/v1/rules`, then export/read them back
before enabling notification routing.

## Symptom-to-query map

- `no_demand`: the queue count is zero and no routing attempts exist. Confirm
  fresh synthetic enquiry creation before treating this as a failure.
- `no_eligible_consultant`: query `oriso.live_chat.routing.decisions` grouped by
  `reason`. Separate zero relations, zero candidates, and zero active leases.
- `expired_availability`: query
  `oriso.live_chat.availability.store.operations` by `operation,outcome`. A rise
  in `refresh/missing` indicates stale browser state or expiry; a read failure
  deliberately empties routing and is urgent.
- `relation_failure`: correlate `oriso.admin.provisioning.operations` stage and
  rollback outcome with routing relation counts using the generated operation
  correlation id. Never search by email or username.
- `matrix_processing_failure`: correlate `oriso.matrix.event.processing` with
  each `oriso.matrix.side_effect.operations` outcome. A mobile-push failure is
  best effort; persisted notification or statistic failure is durable impact.
- `unencrypted_room_creation`: query `oriso.matrix.room.creation` where
  `outcome=success` and `encryption.enabled=false`. Stop the gate immediately;
  do not inspect or export room content.

## Runtime proof

For both Pre-Dev and Dev attach:

1. full Git commit and `container.image.digest` for every participating service;
2. effective `MATRIX_ENCRYPTION_ENABLED`, Redis host reference, and availability
   TTL, with Secret values omitted;
3. first and last matching SigNoz timestamps for the synthetic operation;
4. the E2E actor-role mapping and pseudonymous tenant/topic/session/room values.

The gate is blocked, not passed, when no SigNoz event can be matched to the
immutable runtime evidence. Query results and screenshots must not contain
message content, invitation URLs, access tokens, mobile tokens, email, or raw
usernames.

## Rollout order

Import the dashboard and alerts only after the emitting service PRs are merged
into `pre-dev`. Validate event arrival on Pre-Dev, promote reviewed immutable
digests to Dev, then repeat the same queries there. Alert evaluation remains in
burn-in until both environments produce the expected synthetic success and
failure signals.
