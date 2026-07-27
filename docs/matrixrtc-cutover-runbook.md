# MatrixRTC cutover runbook

This runbook is the release contract for the Matrix-only chat and Element Call
widget cutover. It uses the existing Helm deployment process; it does not add a
second release workflow.

## One-time secret and identity preparation

Create a dedicated, non-admin Matrix user such as
`@matrixrtc-auth:matrix.oriso.org` and obtain a client access token for it. The
Frontend invites this user to each new call room. The policy gateway can
therefore accept that invite and call `joined_members`, but it cannot inspect
any unrelated room or use Synapse administration APIs.

Create `matrixrtc-auth-secrets` outside Helm through the environment's secret
manager. It must contain:

- `matrix-membership-token`
- `livekit-api-key`
- `livekit-api-secret`
- `redis-url`

Create `livekit-config` outside Helm with the `config.yaml` key. The LiveKit
configuration must contain the same API key and secret, the authorization
service webhook, and the shared Redis connection required for a multi-node
LiveKit cluster. Neither Secret may be supplied through values files or stored
in Git.

## Release preflight

1. Record the reviewed source commit and published OCI digest for Frontend,
   Element Call, UserService, AgencyService, both MatrixRTC authorization
   images, and Synapse.
2. Run `scripts/cutover-release-preflight.py` against the coordinated bundle.
   Zero digests, mutable tags, missing evidence, and a partial repository set
   are stop-ship conditions.
3. Render the exact production values and confirm that no Secret object
   contains LiveKit or Matrix membership credentials.
4. Capture the current Deployment image digests before changing the release.
5. Confirm two schedulable nodes for the two host-networked LiveKit replicas.

UserService and Frontend are one compatibility unit: the `rcGroupId` to
`matrixRoomId` and `rcUserId` to `matrixUserId` API change must never be
released independently.

## Deploy

Use the existing release command with the reviewed digest overlay and atomic
rollback:

```sh
helm upgrade --install oriso-platform . \
  --namespace caritas \
  --values values.yaml.default \
  --values <environment-values> \
  --values <generated-digest-overlay> \
  --atomic --wait --timeout 5h10m
```

The Element Call pod has a startup gate on the MatrixRTC policy gateway, so new
widget traffic cannot become ready before authorization is available. The
LiveKit Deployment keeps two replicas on different nodes and gives terminating
servers five hours to drain active rooms before replacement.

## Mandatory verification

- All Deployments show exactly the reviewed image digests.
- Both MatrixRTC authorization Deployments and both LiveKit pods are Ready.
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
  --namespace caritas --wait --timeout 5h10m
```

Keep the external Secrets in place during rollback. If the UserService
Liquibase migration has already run, use its tested rollback together with the
same complete application bundle; never restore only the old Frontend or only
the old UserService.

Future Jitsi, Google Meet or Microsoft Teams integrations are separate provider
adapters. They must not receive the host Matrix session, alter the Element Call
crypto boundary, or act as an automatic fallback for this service.
