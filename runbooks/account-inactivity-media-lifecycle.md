# Account inactivity: media lifecycle activation

This is an opt-in deployment contract, not a deployment record. The chart keeps
`matrixrtcLifecycle.enabled: false`; image pins and existing runtime Secrets
are not changed by this feature. The inactivity worker remains separately
disabled/dry-run by default. Registered-only account deletion and deletion after
session purge are disabled so they cannot bypass assigned inactivity periods.
Other content-retention workflows are unchanged.

## Prerequisites

1. Deploy reviewed UserService and MatrixRTC policy-gateway images implementing
   the lifecycle RPCs before enabling this flag. Drain existing calls and have
   participants rejoin through the new gateway before enabling expiry:
   pre-rollout participants have no durable identity mapping. The gateway must
   return 503 for an unmapped active participant instead of removing unrelated
   people or falsely acknowledging a completed revocation. The current chart pins do not
   imply that those implementations have shipped.
2. Provision a **separate** Kubernetes Secret outside Helm with a new,
   high-entropy `media-lifecycle-token`. Do not reuse a Keycloak credential,
   participant JWT, LiveKit API secret, or the existing call-policy token.
   Helm references the Secret; it does not create it or store its value in
   release history. Both UserService and the gateway must receive the same token.
3. Provision a **different LiveKit config Secret**, outside Helm, preserving the
   existing LiveKit keys, Redis credentials and current authorization webhook.
   Add the signed lifecycle webhook:

   ```yaml
   webhook:
     api_key: <existing LiveKit API key>
     urls:
       - <existing authorization-service webhook URL>
       - http://matrixrtc-auth-policy-gateway:3010/internal/lifecycle/webhook
   ```

   The lifecycle webhook uses LiveKit's signature over the raw request body,
   not the new shared lifecycle token. Keep `room.auto_create: false`.
   The new config must use RTC TCP 7881 and UDP mux 7882, no UDP port range,
   and the operator-verified public IPv4 of the SFU node with
   `rtc.use_external_ip: false`. Enabling lifecycle also enforces those RTC
   settings through LiveKit v1.13.5 CLI flags. No runtime Secret is rewritten.
4. Supply that real public IPv4 as `matrixrtcLifecycle.livekit.nodeIp`.
   Only one SFU replica is supported in this mode. Verify the scheduled node
   owns that public address; constrain scheduling to that node operationally
   before activation. Multi-node support needs a per-node address design.
5. Verify that the cluster CNI supports the configured hostPorts and enforces
   the rendered NetworkPolicies. Allow inbound **TCP 7881 and UDP 7882** at
   the node firewall. Do not expose TCP 7880 through a host listener,
   hostPort, NodePort, load balancer or a second ingress.

Activation values (names and IP are operator-supplied, not defaults):

```yaml
matrixrtcLifecycle:
  enabled: true
  existingSecret:
    name: <dedicated lifecycle Secret>
    tokenKey: media-lifecycle-token
  livekit:
    existingConfigSecret:
      name: <separate lifecycle LiveKit config Secret>
      key: config.yaml
    nodeIp: <verified public IPv4 of the SFU node>
```

Helm rejects empty/reused prerequisite Secret names, invalid/private/loopback
IPv4 addresses, and more than one SFU replica. Secret contents still require
operator readback; a valid name is not proof of correct provisioning.
Use a coordinated Recreate rollout: the old host-network process must fully
exit before the new pod-network process starts. Reverting the flag restores
the legacy host-network topology and removes the media admission gate, so it
is not a security-preserving rollback while accounts are suspended.

## Service contracts

- UserService uses `MATRIXRTC_LIFECYCLE_ENABLED`,
  `MATRIXRTC_LIFECYCLE_BASE_URL=http://matrixrtc-auth-policy-gateway:3010`,
  and `MATRIXRTC_LIFECYCLE_TOKEN` from the dedicated Secret.
- UserService calls `POST /internal/lifecycle/revoke`, `/restore`, or
  `/forget` with
  `Authorization: Bearer <dedicated token>` and
  `{"matrixUserIds":["<Matrix identity>"]}`.
  After successful account deletion, `/forget` purges the raw Matrix identity
  registry while retaining SHA-256 room/subject tombstones for a fixed 24 hours.
  Keep every signaling/read timeout below 24 hours; the current SFU ingress
  proxy read/send timeouts are 3600 seconds.
- The gateway calls
  `POST http://userservice:8080/internal/matrixrtc/media-access` with
  `x-matrixrtc-lifecycle-token`. Only 204 permits admission; 403 denies and
  503/unavailable must fail closed.
- Every ingress path targeting the SFU checks
  `GET /internal/lifecycle/admit`. The subrequest forwards the original URI
  including its participant access token and the original Authorization header.
  It does **not** replace participant identity with the shared token or cache
  allow decisions. Avoid logging token-bearing request URIs.
- The gateway's Redis URL and LiveKit API key/secret come from the existing
  auth Secret, mounted read-only. Its separate lifecycle token is mounted at
  `/run/lifecycle/token`.

With the flag enabled the SFU uses pod networking. Service `livekit:7880`
remains ClusterIP; only the authenticated ingress controller and lifecycle
gateway can reach that TCP port under the rendered policy. Public RTC media
uses hostPorts 7881/TCP and 7882/UDP.

## Required acceptance before enabling account expiry

Run `python3 tests/render_media_lifecycle_test.py` and
`python3 tests/render_account_inactivity_gate_test.py` for source contracts.
Render success does not prove live admission or networking.

On the actual target environment, record the image digests and runtime config,
then prove all of these with managed synthetic accounts:

1. An active account establishes audio/video; remote media advances over UDP.
   Test TCP fallback separately with UDP unavailable.
2. Every public signaling path rejects missing/invalid tokens and
   suspended-account tokens, including a saved token issued before suspension.
   Verify reconnect is rejected after disconnect.
3. An external request to each node's TCP 7880 cannot reach LiveKit; verify
   there is no stale host-network SFU process. Also check secondary ingress,
   load balancer and NodePort routes. Do not infer this from a manifest.
4. Expiry removes the participant from an existing call, persists the media
   revocation, and prevents rejoin. Core suspension must not complete when the
   media effect is unavailable.
5. Gateway/Redis/UserService policy outages deny admission. Wrong shared tokens
   deny internal lifecycle RPCs; incorrect webhook signatures are rejected.
6. Authorized reactivation restores admission only after UserService confirms
   the account is active. Restart the gateway to prove durable revocation
   state and repeat the saved-token check.

Keep the inactivity worker off/dry-run until that deployed acceptance passes.

## Upstream configuration reference

LiveKit v1.13.5 defines the [RTC mux and public IP settings](https://github.com/livekit/livekit/blob/v1.13.5/config-sample.yaml)
and [CLI overrides](https://github.com/livekit/livekit/blob/v1.13.5/pkg/config/config.go).
The enabled chart mode clears the UDP range and pins the matching hostPorts;
the default-off mode retains the existing topology.

### Signaling log protection and binary verification

When enabled, SFU ingress access logging is disabled because signaling URLs can
carry `access_token`. LiveKit is forced to `logging.level=info`: v1.13.5 debug
signaling response logs can contain refreshed participant tokens. Its HTTP server
has no general access-log middleware. The lifecycle gateway emits fixed error
codes, never original URIs, headers, request bodies, or SDK exception payloads.
Any additional external proxy must also suppress token-bearing URLs; verify this
in deployment acceptance, including controller error logs.

The configured `docker.io/livekit/livekit-server:v1.13.5` image was checked locally
with `help-verbose` and `ports` on 2026-09-16. The pulled manifest digest was
`sha256:3497163e15c48fef6e7830c78716f9e9d5edc28abf7aa90b61c86e93bbc306b1`.
All rendered network and logging CLI flags were accepted; `ports` reported HTTP
7880, ICE/TCP 7881, and ICE/UDP 7882. This checks the binary configuration only,
not deployed CNI/firewall reachability. No image pin was changed.
