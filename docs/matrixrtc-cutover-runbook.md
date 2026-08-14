# MatrixRTC cutover runbook

This runbook is the release contract for the Matrix-only chat and Element Call
widget cutover. It uses the existing Helm deployment process; it does not add a
second release workflow.

## One-time secret and identity preparation

Configure a dedicated, non-admin Matrix user such as
`@matrixrtc-auth:matrix.oriso.org` in `matrixrtcAuth.membershipReaderUserId` and
set `matrixrtcAuth.membershipReaderPassword` in the environment `secrets.yaml`.
During Helm install, the chart registers or reuses that user through Synapse's
shared registration secret, logs in, and patches the resulting client access
token into `matrixrtc-auth-secrets`. The Frontend invites this user to each new
call room. The policy gateway can therefore accept that invite and call
`joined_members`, but it cannot inspect any unrelated room or use Synapse
administration APIs.

Populate `matrixrtcAuth.membershipReaderPassword` and `livekit.api.*` in the
environment `secrets.yaml`. `matrixrtcAuth.membershipToken` is only a manual
override; leave it blank for automatic bootstrap. Helm renders
`matrixrtc-auth-secrets` during install. It contains:

- `matrix-membership-token`
- `livekit-api-key`
- `livekit-api-secret`
- `redis-url`

Helm also renders `livekit-config` with the `config.yaml` key. The LiveKit
configuration contains the same API key and secret, the authorization service
webhook, and the shared Redis connection required for a multi-node LiveKit
cluster. These values must stay in ignored environment secret files and must
not be committed to Git.

That configuration must also pin the address LiveKit advertises to clients:

```yaml
rtc:
  use_external_ip: false
  node_ip: "<the node's public IPv4>"
```

Without it LiveKit enumerates every interface it can find and offers all of
them as ICE candidates — including the node's IPv6 address and the Docker and
flannel ranges, which no client can reach. ICE then prefers the IPv6 pair, its
connectivity checks pass, and the call reports itself connected — but no media
survives that path, so every session died with

```
error reading data channel … error: "dtls timeout: read/write timeout"
```

between 30 seconds and roughly two minutes in, which reads to a user as "the
call works and then breaks". Pinning the IPv4 address collapses the candidate
set to one reachable address and is the reason Pre-Dev calls connect at all.

## Release preflight

1. Record the reviewed source commit and published OCI digest for Frontend,
   Element Call, UserService, AgencyService, both MatrixRTC authorization
   images, LiveKit, Synapse, and the shared BusyBox init/healthcheck image.
2. Run `scripts/cutover-release-preflight.py` against the coordinated bundle.
   Zero digests, mutable tags, missing evidence, and a partial repository set
   are stop-ship conditions.
3. Render the exact production values and confirm that no Secret object
   contains LiveKit or Matrix membership credentials.
4. Generate the verified digest overlay, then capture the current Helm revision,
   complete before-image set, target-image set, and bounded rollback command:

   ```sh
   ./scripts/capture-cutover-rollback.py \
     --release oriso-platform \
     --namespace caritas \
     --target-values <generated-digest-overlay> \
     --output-dir <new-evidence-directory>
   ```

   The command is read-only against the cluster, fails if any current cutover
   Deployment is unready or mutable, and refuses to overwrite evidence.
5. Confirm the single PreDev node is healthy and no call is active. PreDev uses
   one host-networked LiveKit replica with `Recreate` and a bounded 60-second
   termination grace period. A rollout therefore interrupts active calls and
   must run inside the announced maintenance window.

UserService and Frontend are one compatibility unit: the `rcGroupId` to
`matrixRoomId` and `rcUserId` to `matrixUserId` API change must never be
released independently.

## Deploy

PreDev currently has the historical `oriso-platform` Helm release, while recent
service changes are applied through the established host-side scripts in
`/root/predev-deploy-script`. PR #157 was closed without merge and is not a
deployment authority. Until a protected Helm deployment workflow replaces the
host path, evidence must identify the exact script, reviewed source commit,
target digest, previous digest, rollout result, and generated rollback command.

For the coordinated chart release, use the existing Helm release with the
reviewed digest overlay and atomic rollback:

```sh
helm upgrade --install oriso-platform . \
  --namespace caritas \
  --values values.yaml.default \
  --values <environment-values> \
  --values <generated-digest-overlay> \
  --atomic --wait --timeout 15m
```

The Element Call pod has a startup gate on the MatrixRTC policy gateway, so new
widget traffic cannot become ready before authorization is available. The
single-node LiveKit Deployment uses `Recreate`; Kubernetes must fully terminate
the old process before starting the replacement on the same host ports.

## Mandatory verification

- All Deployments show exactly the reviewed image digests.
- Both MatrixRTC authorization Deployments and the LiveKit pod are Ready.
- The public gateway health check succeeds and its CORS response allows only
  the configured ORISO application origin.
- A two-browser encrypted call passes reactions, hand raise, reconnect and
  hangup.
- The test proves there is no `ORISO_CALL_*` device, no iframe
  `matrix-auth-store`, no second Matrix sync, and no Rocket.Chat or embedded
  Jitsi request.
- SigNoz shows no Matrix crypto, OpenID, membership or LiveKit authorization
  error increase.

## Rollback

Do not roll back a single repository or Deployment. Roll back the complete Helm
release to the digest set captured before deployment:

```sh
helm rollback oriso-platform <previous-revision> \
  --namespace caritas --wait --timeout 15m
```

Use the exact command captured in `rollback-command.txt`; do not reconstruct the
revision from memory. Keep the external Secrets in place during rollback. If the UserService
Liquibase migration has already run, use its tested rollback together with the
same complete application bundle; never restore only the old Frontend or only
the old UserService.

The approved platform target is exclusively the ORISO frontend with Matrix,
the ORISO-controlled MatrixRTC / Element Call fork, and LiveKit. Rocket.Chat
and Jitsi are neither supported providers nor fallback paths, and no chart
dependency, secret, route, or runtime configuration for them may remain.
