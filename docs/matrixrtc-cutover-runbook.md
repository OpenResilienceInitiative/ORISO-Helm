# MatrixRTC cutover runbook

This runbook is the release contract for the Matrix-only chat and Element Call
widget cutover. It uses the existing Helm deployment process; it does not add a
second release workflow.

## One-time secret and identity preparation

Configure a dedicated, non-admin Matrix user such as
`@matrixrtc-auth:matrix.oriso.org` in `matrixrtcAuth.membershipReaderUserId`.
Provision `matrixrtc-auth-runtime` outside Helm with:

- `matrix-membership-token`
- `call-policy-token`
- `livekit-api-key`
- `livekit-api-secret`
- `redis-url`

Provision `livekit-config-runtime` outside Helm with the `config.yaml` key.
The LiveKit configuration contains the same API key and secret, the
authorization-service webhook, and the shared Redis connection. Use files or
the environment's secret controller; never pass these values as Helm values or
command-line arguments. The chart references both Secrets but renders neither,
so they never enter Helm release history.

Provision the membership reader and obtain its access token through the
protected environment process before deployment. Helm has no Synapse
registration credential and no permission to create identities or patch
runtime Secrets. The Frontend invites this user to each new call room. The policy gateway can therefore call
`joined_members`, but it cannot inspect unrelated rooms or use Synapse admin
APIs.

PreDev currently has the old Helm-managed `matrixrtc-auth-secrets` and
`livekit-config`. Before the baseline normalization, copy their data to the new
external names without printing it and verify all required keys. Do not reuse the
old names: Helm prunes its old managed objects during the first upgrade. Delete
the old Secrets only after the complete cutover is verified.

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
   images, LiveKit, Synapse, the Synapse BusyBox init image, the shared curl
   healthcheck image, and the MatrixRTC Redis-check image.
2. Run `scripts/cutover-release-preflight.py` against the coordinated bundle.
   Zero digests, mutable tags, missing evidence, and a partial repository set
   are stop-ship conditions.
3. Confirm the two external Runtime Secrets exist with every required key.
   Render the exact production values and confirm that no Secret object named
   `matrixrtc-auth-runtime` or `livekit-config-runtime` is present and none of
   their credential values occurs anywhere in the render.
4. Generate the verified digest overlay, then capture the current Helm revision,
   complete before-image set, target-image set, and bounded rollback command:

   ```sh
   ./scripts/capture-cutover-rollback.py \
     --release caritas \
     --namespace caritas \
     --target-values <generated-digest-overlay> \
     --output-dir <new-evidence-directory>
   ```

   The command is read-only against the cluster, fails if any current cutover
   Deployment is unready or mutable, and refuses to overwrite evidence. It also
   compares the live images with the manifest stored in the captured Helm
   revision. A revision that differs from the live cluster is not an atomic
   rollback point.
5. Confirm the single PreDev node is healthy and no call is active. PreDev uses
   one host-networked LiveKit replica with `Recreate` and a bounded 60-second
   termination grace period. A rollout therefore interrupts active calls and
   must run inside the announced maintenance window.

UserService and Frontend are one compatibility unit: the `rcGroupId` to
`matrixRoomId` and `rcUserId` to `matrixUserId` API change must never be
released independently.

## Deploy

PreDev's Helm release is named `caritas` in namespace `caritas`. Recent service
changes were applied through the host-side scripts in
`/root/predev-deploy-script`; those scripts can leave Helm revision history
behind the actual Deployment images. PR #157 was closed without merge and is
not a deployment authority.

If the rollback capture reports Helm/live drift, stop. First normalize a
baseline using this reviewed chart and an overlay that maps every currently
running binary to its immutable digest. Deploy that baseline through Helm,
verify all workloads and public probes, and rerun the capture. This creates a
new revision whose stored manifest exactly matches the cluster while retaining
the no-Rocket.Chat/no-Jitsi chart boundary. The target cutover is a second Helm
revision; its atomic rollback point is the verified baseline revision. Do not
use a historical revision whose images differ from the live cluster.

For the coordinated chart release, use the existing Helm release with the
reviewed digest overlay and atomic rollback:

```sh
helm upgrade --install caritas . \
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
helm rollback caritas <previous-revision> \
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
